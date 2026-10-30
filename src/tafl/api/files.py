"""Reading one file or all files of a folder (and its subfolders), for import actions, and
writing, copying, moving and deleting files safely, for exports and commands.

An import action puts ``source_params(...)`` first in its parameters, reads each file of
``source_files(p, ctx)`` and returns ``combine(...)``; ``fingerprint=sources_fingerprint``
makes the app notice new, changed and deleted files, and ``endpoints=source_endpoints`` tells
it the file or folder that the step reads::

    @action("my.import", "Import my files", kind="source",
            params=[*source_params("My files (*.my)", "*.my")],
            fingerprint=sources_fingerprint, endpoints=source_endpoints)
    def import_my(df, p, ctx):
        files, folder = source_files(p, ctx)
        return combine([(path, None, read_my(path)) for path in files], p, folder)

An export action writes into the file that ``replace_file`` gives it: the old file stays as it
was until the new one is complete::

    with replace_file(ctx.resolve_path(p.file), ctx.warn) as path:
        write_my(df, path)

``copy_file``, ``move_file`` and ``delete_file`` either do their work or change nothing (a step
that copies or moves many files does it in ``with batch():``), and ``read_error`` turns an error
from reading a file into a plain message. ``name_problem`` and ``check_folder`` say why a file or
folder name isn't allowed in Windows.

For a folder, ``source_files`` also notes the files and the folder in ``ctx.imported_files`` and
``ctx.imported_folder``: the later steps of the table get them there, e.g. to move all files
that the import read (see ``Context``).
"""

from __future__ import annotations

import contextlib
import contextvars
import ctypes
import errno
import fnmatch
import functools
import os
import re
import secrets
import shutil
import socket
import stat
import tempfile
import threading
import zlib
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import polars as pl

from . import params as P
from .context import Context
from .endpoints import FOLDER, IN, Endpoint, file_endpoint
from .errors import ActionError, SkipRest

_IN_FOLDER = {"source": "folder"}
ONE_COLUMN = "\x1f"  # the delimiter for files that have none: text doesn't contain it
# In the settings of an import step: the file was written by a data sheet of the app. Its value
# is {"file": the file's name, "stamp": ...}. A mark that names another file counts for nothing.
SHEET_KEY = "_sheet"


def source_params(filter: str, patterns: str) -> list[P.Param]:
    """Parameters for reading one file or all files of a folder: source, file, folder, pattern,
    subfolders, no_files, file_column, path_column."""
    return [
        P.Choice("source", "Read", [("file", "One file"), ("folder", "All files in a folder")]),
        P.File("file", "File", filter=filter, visible_if={"source": "file"}),
        P.Folder("folder", "Folder", visible_if=_IN_FOLDER),
        P.Text(
            "pattern",
            "File names",
            default=patterns,
            visible_if=_IN_FOLDER,
            help="* stands for any text, e.g. orders_*.csv; several patterns with ;",
        ),
        P.Bool("subfolders", "Include subfolders (all levels)", default=False, visible_if=_IN_FOLDER),
        P.Choice(
            "no_files",
            "When there are no files",
            [("error", "Stop with an error"), ("empty", "Continue with an empty table"), ("skip", "Skip the rest of the table")],
            visible_if=_IN_FOLDER,
            help="e.g. for a folder that is empty on some days: skipping leaves out the steps after this "
            "one. An empty table (with a warning) has only the columns with the file name and the path, "
            "as far as they are switched on below: a later step that names another column fails.",
        ),
        P.Bool("file_column", "Add a column with the file name", default=True, visible_if=_IN_FOLDER),
        P.Bool(
            "path_column",
            "Add a column with the path below the folder",
            default=False,
            visible_if=_IN_FOLDER,
            help=r"e.g. 2025\01\orders.csv: a calculated column can take the year or month from it",
        ),
    ]


def find_files(folder: Path, patterns: str = "*", subfolders: bool = False) -> list[Path]:
    """Files in ``folder`` (and all subfolders) whose names match one of the patterns
    (``*.csv;*.txt``, not case sensitive), sorted by their path below the folder, with numbers
    in natural order (day2 before day10). Office lock files (``~$...``) are left out."""
    wanted = _name_patterns(patterns)
    candidates = folder.rglob("*") if subfolders else folder.glob("*")
    found = [path for path in candidates if _name_matches(path.name, wanted) and path.is_file()]
    return sorted(found, key=lambda path: _natural(path.relative_to(folder).as_posix().lower()))


def in_folder(path: Path, folder: Path, patterns: str = "*", subfolders: bool = False) -> bool:
    """Whether ``find_files`` would find the file ``path``, without a look at the other files."""
    below = path.parent == folder or (subfolders and path.is_relative_to(folder))
    return below and _name_matches(path.name, _name_patterns(patterns))


def _name_patterns(patterns: str) -> list[str]:
    return [p.strip().lower() for p in re.split(r"[;,]", patterns or "") if p.strip()] or ["*"]


def _name_matches(name: str, wanted: list[str]) -> bool:
    return not name.startswith("~$") and any(fnmatch.fnmatchcase(name.lower(), w) for w in wanted)


def _natural(text: str) -> list[Any]:
    return [(0, int(part), part) if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", text) if part]


def source_files(params: Any, ctx: Context) -> tuple[list[Path], Path | None]:
    """The files to read and, for a folder, the folder. Raises a readable error when there
    is nothing to read.

    A folder without matching files is an ActionError, or as chosen in ``no_files``: no files
    (an empty list) and a warning, or a SkipRest with an empty table (``combine`` gives the
    same table for no files). For a folder, the files and the folder are noted in
    ``ctx.imported_files`` and ``ctx.imported_folder`` for the later steps of the table; an
    import that leaves some of the files out (e.g. workbooks without a chosen sheet) notes only
    the files it read in ``ctx.imported_files`` afterwards."""
    files, folder = _sources(params, ctx)
    if folder is not None:
        ctx.imported_files, ctx.imported_folder = tuple(files), folder
    if files or folder is None:
        return files, folder
    where = f"{folder} or its subfolders" if params.get("subfolders") else str(folder)
    message = f"No files named {params.get('pattern') or '*'} in {where}"
    choice = params.get("no_files", "error")
    if choice == "skip":
        raise SkipRest(message, combine([], params, folder))
    if choice != "empty":
        raise ActionError(message)
    ctx.warn(f"{message}: the table is empty")
    return files, folder


def _sources(params: Any, ctx: Context) -> tuple[list[Path], Path | None]:
    if params.get("source", "file") != "folder":
        path = ctx.resolve_path(params["file"])
        if not path.is_file():
            raise ActionError(f"File not found: {path}")
        return [path], None
    folder = ctx.resolve_path(params["folder"])
    if not folder.is_dir():
        raise ActionError(f"Folder not found: {folder}")
    return find_files(folder, params.get("pattern", "*"), params.get("subfolders", False)), folder


def source_endpoints(params: Any) -> list[Endpoint]:
    """``endpoints=`` for import actions with ``source_params``: the file, or the folder with the
    file names (and "with subfolders"), whose files the later steps of the table can take."""
    if params.get("source", "file") != "folder":
        file = params.get("file")
        return [file_endpoint(file, IN, setting="file")] if isinstance(file, str) and file.strip() else []
    folder, pattern = params.get("folder"), params.get("pattern")
    if not (isinstance(folder, str) and folder.strip()):
        return []
    pattern = pattern.strip() if isinstance(pattern, str) else ""
    subfolders = bool(params.get("subfolders"))
    return [
        Endpoint(
            IN,
            FOLDER,
            place=folder.strip(),
            name=pattern or "*",
            mode="with subfolders" if subfolders else "",
            setting="folder",
            mode_setting="subfolders" if subfolders else "",
            folder_import=True,
        )
    ]


def sources_fingerprint(params: Any, ctx: Context) -> Any:
    """A ``fingerprint`` for actions with ``source_params``: it changes when a file is added,
    removed or changed, so the result is read again."""
    try:
        files, _ = _sources(params, ctx)
    except ActionError as error:
        return str(error)
    stats = []
    for path in files:
        try:
            stat = path.stat()
            stats.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            stats.append((str(path), None, None))
    return tuple(stats)


def combine(
    parts: Iterable[tuple[Path, str | None, pl.DataFrame]],
    params: Any,
    folder: Path | None,
    sheet_column: bool = False,
    prepare: Callable[[pl.DataFrame], pl.DataFrame] | None = None,
) -> pl.DataFrame:
    """One table from several files (or sheets): ``parts`` are (file, sheet or None, data).

    Columns are matched by name; a column that is missing in a file stays empty there, and a
    column with different types in different files gets a type that fits all (often text).
    ``prepare`` runs once on all the data (e.g. to detect types); it must keep the rows as
    they are. For a folder, columns with the file name and/or the path below the folder come
    first (as chosen in ``params``); with ``sheet_column``, one with the sheet name. Without
    parts, the table has only these columns and no rows."""
    parts = list(parts)
    frames = [frame for _, _, frame in parts]
    if not frames:
        data = pl.DataFrame()
    elif len(frames) == 1:
        data = frames[0]
    else:
        try:
            data = pl.concat(frames, how="diagonal_relaxed")
        except pl.exceptions.PolarsError as error:
            raise ActionError(f"The files don't fit together: {str(error).splitlines()[0]}") from None
    if prepare is not None and frames:
        data = prepare(data)
    extras: list[str] = []
    if folder is not None and params.get("file_column"):
        extras.append("File")
    if folder is not None and params.get("path_column"):
        extras.append("Path")
    if sheet_column:
        extras.append("Sheet")
    if not extras:
        return data
    columns = []
    for extra, name in zip(extras, _free_names(extras, set(data.columns)), strict=True):
        pieces = [
            pl.repeat(_source_value(extra, path, sheet, folder), frame.height, dtype=pl.String, eager=True)
            for path, sheet, frame in parts
        ]
        columns.append(pl.concat(pieces).alias(name) if pieces else pl.Series(name, [], dtype=pl.String))
    return pl.concat([pl.DataFrame(columns), data], how="horizontal")


def _source_value(extra: str, path: Path, sheet: str | None, folder: Path | None) -> str | None:
    if extra == "File":
        return path.name
    if extra == "Path":
        return str(path.relative_to(folder)) if folder is not None else str(path)
    return sheet


def source_summary(params: Any) -> str:
    """The file or the files, for a step's one-line description: ``orders.csv`` or
    ``*.csv in daily (with subfolders)``."""
    if params.get("source", "file") != "folder":
        return Path(str(params.get("file") or "")).name or "?"
    folder = str(params.get("folder") or "")
    text = f"{params.get('pattern') or '*'} in {Path(folder).name or folder or '?'}"
    return text + " (with subfolders)" if params.get("subfolders") else text


def _free_names(names: Sequence[str], taken: set[str]) -> list[str]:
    """``names`` that don't clash with columns of the data: File, or File (source), ..."""
    result = []
    for name in names:
        candidate, number = name, 1
        while candidate in taken:
            number += 1
            candidate = f"{name} (source)" if number == 2 else f"{name} (source {number})"
        result.append(candidate)
        taken = taken | {candidate}
    return result


def detect_delimiter(sample: str, skip_lines: int = 0) -> str:
    """The delimiter of the first line that splits most lines the same way. Without any in the
    first line, the file has one column (its values can still have commas, as in 12,50)."""
    lines = [line for line in sample.splitlines()[skip_lines : skip_lines + 50] if line.strip()][:20]
    candidates = (";", ",", "\t", "|")
    if lines and not any(count_outside_quotes(lines[0], candidate) for candidate in candidates):
        return ONE_COLUMN
    best, best_score = ",", -1.0
    for candidate in candidates:
        counts = [count_outside_quotes(line, candidate) for line in lines]
        if not counts or counts[0] == 0:
            continue
        consistency = sum(c == counts[0] for c in counts) / len(counts)
        score = consistency * 1000 + counts[0]
        if score > best_score:
            best, best_score = candidate, score
    return best


def count_outside_quotes(line: str, char: str) -> int:
    count, quoted = 0, False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == char and not quoted:
            count += 1
    return count


def sheet_marked(params: Any) -> bool:
    """Whether the settings of an import step read one file that a data sheet wrote: they hold
    the mark (``SHEET_KEY``), and it names this file. After another file was chosen, it doesn't."""
    mark, file = params.get(SHEET_KEY), params.get("file")
    if not (isinstance(mark, dict) and isinstance(file, str)) or params.get("source", "file") != "file":
        return False
    return str(mark.get("file", "")).lower() == Path(file).name.lower()


# --- names --------------------------------------------------------------------------------------

_DEVICES = frozenset(["CON", "PRN", "AUX", "NUL", *(f"{port}{n}" for port in ("COM", "LPT") for n in "0123456789¹²³")])
_LONGEST = 255  # characters in the name of a file or folder in Windows


def name_problem(name: str, text: str | None = None) -> str | None:
    """Why ``name`` can't be the name of a file or folder in Windows, as a message that shows
    ``text`` (e.g. the whole path; by default the name), or None."""
    return _name_problem(name, name if text is None else text)


def _name_problem(name: str, text: str, ends: bool = True) -> str | None:
    """``ends=False`` allows a dot or a space at the end (Windows leaves them out)."""
    if name in (".", ".."):
        return f"'{name}' can't be a file name: . and .. stand for folders"
    if ":" in name:  # the message of Context.resolve_path
        return (
            f"':' can't be part of a file or folder name: {text}. For a time in a name, write it without ':', "
            "e.g. STRFTIME(NOW(), '%H-%M')"
        )
    if any(ord(c) < 32 for c in name):
        return f"Line breaks, tabs and other control characters can't be part of a file or folder name: {text!r}"
    if bad := [f"'{c}'" for c in dict.fromkeys(name) if c in '<>"|?*']:
        return f"{' and '.join(bad)} can't be part of a file or folder name: {text}"
    if ends and name.endswith((".", " ")):
        return f"A file or folder name can't end with a dot or a space: '{text}'"
    if (device := name.split(".")[0].rstrip(" ").upper()) in _DEVICES:
        return f"{device} is reserved for a device in Windows and can't be a file or folder name: {text}"
    if (length := _units(name)) > _LONGEST:
        start = name if len(name) <= 60 else f"{name[:50]}…"
        return f"A file or folder name can have at most {_LONGEST} characters: {start} has {length:,}"
    return None


def _units(text: str) -> int:
    """The length of ``text`` as Windows counts it (an emoji counts twice)."""
    return len(text.encode("utf-16-le", "surrogatepass")) // 2


def check_folder(folder: Path) -> None:
    """An ActionError with a plain message instead of the system's when ``folder`` can't be
    created: a file is in the way, or the name of a folder still to be created isn't allowed."""
    _check_folder(folder, ends=True)


def _check_folder(folder: Path, ends: bool) -> None:
    for path in (folder, *folder.parents):
        if path.is_dir():
            break
        if path.name not in ("", ".", "..") and (problem := _name_problem(path.name, str(folder), ends)):
            raise ActionError(problem)  # before exists(): in Windows, folder\CON exists (the console)
        if path.exists():
            raise ActionError(f"{path} is a file, not a folder")


# --- writing, copying, moving and deleting files ------------------------------------------------


def _computer_name() -> str:
    """This computer in the names of temporary files: its Windows name (letters, digits and -,
    at most 15), or else a number worked out from its name."""
    name = os.environ.get("COMPUTERNAME", "") if os.name == "nt" else ""
    if not name:
        with contextlib.suppress(OSError):
            name = socket.gethostname()
    if re.fullmatch(r"[A-Za-z0-9-]{1,15}", name):
        return name.upper()
    return f"{zlib.crc32(name.encode('utf-8', 'surrogatepass')):08X}"


# Other computers may write into the same shared folder: their process ids say nothing here
_COMPUTER = _computer_name()
_ROOM = _LONGEST - len(f"~$.{_COMPUTER}-ffffffff-ffffff.old")  # for the name and extension in a temporary file's name
_CHUNK = 1 << 20
_READ, _WRITE, _DELETE = 0x80000000, 0x40000000, 0x00010000  # access rights in Windows
_IN_USE = (32, 33)  # Windows' errors when another program has the file open
_ACTIVE: set[str] = set()  # the temporary files this process is writing
# Re-entrant: Ctrl+C can come right after a thread took the lock, before the ``with`` block that
# releases it begins. That thread (the command line's only one) can still take it then.
_ACTIVE_LOCK = threading.RLock()


@contextlib.contextmanager
def replace_file(target: Path, warn: Callable[[str], None] | None = None) -> Iterator[Path]:
    """A new, empty file next to ``target`` for the block to write. When the block ends without
    an error, the new file replaces ``target``; otherwise it is deleted and ``target`` stays as it
    was, also when the run is stopped. The folder is created if needed. Problems are ActionErrors
    that say why: the file is open in another program or read-only, the name isn't allowed, no
    permission to write in the folder.

    The new file is named ``~$name.<computer>-<process>-<random>.csv``: folder imports leave
    such files out, and the next export of ``target`` deletes the ones of a process of this
    computer that has ended (e.g. one that was ended in the Task Manager). Those of other
    computers stay: in a shared folder, their process ids say nothing here. In a folder where
    files may be changed but no new ones created, the block writes a file in the temporary
    folder that then is written over ``target``; ``warn`` is told that a failure while writing
    could leave it half-written."""
    with _written(target, True, warn, target.name) as path:
        yield path


def copy_file(source: Path, target: Path, *, replace: bool = False, warn: Callable[[str], None] | None = None) -> None:
    """Copies the file ``source`` to ``target`` with its modification time (the folder is created
    if needed). As in ``replace_file``, the copy is written next to the target and gets its name
    only when it is complete: a failed copy leaves no half-written file, and an old target stays
    as it was. Without ``replace``, an existing target is an error. Problems are ActionErrors
    that say why, e.g. which of the two files is open in another program."""
    if not replace and os.path.lexists(target):
        raise ActionError(f"{target} exists already")
    with _written(target, replace, warn, str(target)) as temporary:
        _copy_into(source, temporary)


def move_file(source: Path, target: Path, *, replace: bool = False) -> None:
    """Moves the file ``source`` to ``target``: another name, folder or drive (the folder is
    created if needed). Either the file is moved, or nothing changes: on the same drive it is
    renamed in one step; to another drive it is copied next to the target, the copy gets the
    target's name (an old target is put aside until then), and then the source is deleted, last
    the old target. Without ``replace``, an existing target is an error. Problems are
    ActionErrors that say why, e.g. which of the two files is open in another program."""
    _prepare(target)
    if not replace and os.path.lexists(target) and not _same(source, target):
        raise ActionError(f"{target} exists already")
    try:
        if replace:
            os.replace(source, target)
        else:
            _rename_new(source, target)
        return
    except FileExistsError:
        raise ActionError(f"{target} exists already") from None
    except OSError as error:
        if error.errno != errno.EXDEV:  # EXDEV: another drive
            raise ActionError(_move_problem(error, source, target)) from None
    _move_to_other_drive(source, target, replace)


def delete_file(path: Path, *, recycle: bool = False) -> None:
    """Deletes the file ``path``: for good, or with ``recycle`` to the Recycle Bin. A read-only
    file isn't deleted for good. Problems are ActionErrors that say why, e.g. the file is open in
    another program."""
    try:
        if recycle:
            import send2trash

            send2trash.send2trash(path)
        else:
            os.remove(path)
    except OSError as error:  # send2trash's TrashPermissionError is one too
        raise ActionError(_delete_problem(error, path, recycle=recycle)) from None


def read_error(error: BaseException, path: Path) -> ActionError:
    """A plain message for a file that exists but can't be read, e.g. one that another program
    has open without letting others read it. ``error`` may also come from a library that read
    the file: its text decides then (e.g. Polars' "(os error 32)")."""
    return ActionError(_read_problem(error, path, path.name))


# In a batch: folder -> the names of temporary files of this computer found in it
_BATCH: contextvars.ContextVar[dict[str, list[str]] | None] = contextvars.ContextVar("tafl_files_batch", default=None)


@contextlib.contextmanager
def batch() -> Iterator[None]:
    """For a step that writes, copies or moves many files: in the block, each folder is searched
    only once for the temporary files that ended processes left (see ``replace_file``), not once
    for each file, which takes long in a folder of many files (on a network drive even longer)."""
    if _BATCH.get() is not None:
        yield
        return
    token = _BATCH.set({})
    try:
        yield
    finally:
        _BATCH.reset(token)


@contextlib.contextmanager
def _written(target: Path, replace: bool, warn: Callable[[str], None] | None, shown: str) -> Iterator[Path]:
    """``replace_file``, also for a target that must not exist (``replace=False``). Messages
    name the target as ``shown``."""
    folder = _prepare(target)
    try:
        temporary = _new_temporary(target)
    except PermissionError:  # no new files in the folder
        if not (replace and target.is_file()):
            raise ActionError(f"No permission to write in {folder}") from None
        temporary = None
    except OSError as error:
        raise ActionError(f"Can't write {shown}: {_reason(error)}") from None
    if temporary is None:
        with _written_over(target, warn, shown) as path:
            yield path
        return
    try:
        try:
            yield temporary
        except OSError as error:  # e.g. the disk is full
            raise ActionError(f"Can't write {shown}: {_reason(error)}") from None
        try:
            if replace:
                _keep_hidden(target, temporary)
                os.replace(temporary, target)
            else:
                _rename_new(temporary, target)
        except FileExistsError:
            raise ActionError(f"{target} exists already") from None
        except PermissionError:  # the new file could be written next to it: the old one is in use
            raise ActionError(_replace_problem(target, shown)) from None
        except OSError as error:
            raise ActionError(f"Can't write {shown}: {_reason(error)}") from None
    finally:
        _finish(temporary)


def _keep_hidden(target: Path, new: Path) -> None:
    """In Windows, a file that replaces a hidden file is hidden too."""
    if os.name != "nt":
        return
    try:
        hidden = os.stat(target).st_file_attributes & (stat.FILE_ATTRIBUTE_HIDDEN | stat.FILE_ATTRIBUTE_SYSTEM)
        if hidden:
            _kernel32().SetFileAttributesW(str(new), os.stat(new).st_file_attributes | hidden)
    except OSError:  # e.g. there is no old file
        pass


_SCRATCH = re.compile(r"tafl-([0-9a-f]{1,8})-[a-z0-9_]+")  # tafl-<process>-<random>


def _new_scratch_folder() -> Path:
    """A new folder in the temporary folder, named ``tafl-<process>-…``. The folders of processes
    that have ended (e.g. one ended in the Task Manager) are deleted first."""
    root = Path(tempfile.gettempdir())
    try:
        with os.scandir(root) as entries:
            names = [entry.name for entry in entries if entry.name.startswith("tafl-") and entry.is_dir()]
    except OSError:
        names = []
    for name in names:
        if (found := _SCRATCH.fullmatch(name)) is None:
            continue
        process = int(found.group(1), 16)
        if process != os.getpid() and not _running(process):
            shutil.rmtree(root / name, ignore_errors=True)
    return Path(tempfile.mkdtemp(prefix=f"tafl-{os.getpid():x}-", dir=root))


@contextlib.contextmanager
def _scratch_folder() -> Iterator[Path]:
    """``_new_scratch_folder`` for the block; deleted afterwards."""
    folder = _new_scratch_folder()
    try:
        yield folder
    finally:
        shutil.rmtree(folder, ignore_errors=True)


@contextlib.contextmanager
def _written_over(target: Path, warn: Callable[[str], None] | None, shown: str) -> Iterator[Path]:
    """For a folder where no new files may be created: the block writes a file in the temporary
    folder, which then is written over ``target``."""
    try:
        with open(target, "r+b"):
            pass
    except OSError as error:
        raise ActionError(_write_problem(error, target, shown)) from None
    with _scratch_folder() as scratch:
        temporary = scratch / f"new{target.suffix}"
        temporary.touch()
        try:
            yield temporary
        except OSError as error:
            raise ActionError(f"Can't write {shown}: {_reason(error)}") from None
        try:
            with open(temporary, "rb") as new, open(target, "r+b") as old:
                shutil.copyfileobj(new, old, _CHUNK)
                old.truncate()
        except OSError as error:
            raise ActionError(f"Can't write {shown}: {_reason(error)}. It may be half-written now") from None
        with contextlib.suppress(OSError):
            shutil.copystat(temporary, target)
    if warn is not None:
        warn(
            f"{shown} was written over directly, as no new files can be created in {target.parent}: "
            "a failure while writing would leave it half-written"
        )


def _prepare(target: Path) -> Path:
    """Checks the name of ``target`` and creates its folder, which it returns."""
    if problem := _name_problem(target.name, target.name, ends=False):
        raise ActionError(problem)
    if target.is_dir():
        raise ActionError(f"{target} is a folder: choose a file name")
    folder = target.parent
    _check_folder(folder, ends=False)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise ActionError(f"No permission to create the folder {folder}") from None
    except OSError as error:
        raise ActionError(f"Can't create the folder {folder}: {_reason(error)}") from None
    return folder


def _copy_into(source: Path, copy: Path) -> None:
    """Copies the content and modification time of ``source`` into the file ``copy``. A source
    that can't be read is an ActionError; a failure while writing an OSError."""
    try:
        reader = open(source, "rb")
    except OSError as error:
        raise ActionError(_read_problem(error, source, str(source))) from None
    with reader, open(copy, "wb") as writer:
        shutil.copyfileobj(reader, writer, _CHUNK)
    with contextlib.suppress(OSError):
        shutil.copystat(source, copy)


def _move_to_other_drive(source: Path, target: Path, replace: bool) -> None:
    """Copy next to the target, put an old target aside, give the copy the target's name, delete
    the source, then the old target. At every moment the file is complete in the source or the
    target, and an old target in its place or aside. A failure, or a stop (Ctrl+C), before the
    source is deleted puts everything back. An old target that a process left aside when it
    ended (``~$name.<computer>-<process>-<random>.old.csv``) is deleted only once a file has the
    target's name again."""
    move = _Move(source, target)
    try:
        move.run(replace)
    except BaseException as error:
        note = _to_the_end(move.end)
        if note and isinstance(error, ActionError):
            raise ActionError(f"{error}{note}") from None
        raise


class _Move:
    """A move to another drive, and how far it got: ``end`` finishes it or puts everything back."""

    def __init__(self, source: Path, target: Path) -> None:
        self.source, self.target = source, target
        self.copy: Path | None = None  # the copy next to the target, until it gets the target's name
        self.old: Path | None = None  # where an old target is put aside
        self.aside = self.placing = self.deleting = False  # which of these steps began
        self.put_aside = False  # the old target is aside
        self.mode: int | None = None  # of the source: deleting a read-only file changes it

    def run(self, replace: bool) -> None:
        source, target = self.source, self.target
        with contextlib.suppress(OSError):
            self.mode = os.stat(source).st_mode
        self.copy = _temporary_for(source, target)
        try:
            _copy_into(source, self.copy)
        except OSError as error:  # e.g. the disk is full
            raise ActionError(f"Can't write {target}: {_reason(error)}") from None
        if replace and os.path.lexists(target):
            if not os.access(target, os.W_OK):  # as on the same drive, where Windows refuses it
                raise ActionError(f"{target} is read-only, so it can't be replaced")
            self.old = _temporary_for(source, target, old=True)
            self.aside = True
            try:
                os.replace(target, self.old)
            except OSError as error:
                self.aside = False
                if isinstance(error, PermissionError):
                    raise ActionError(_replace_problem(target, str(target))) from None
                raise ActionError(f"Could not move {source} to {target}: {_reason(error)}") from None
            self.put_aside = True
        self.placing = True
        try:
            _rename_new(self.copy, target)
        except FileExistsError:  # a file came there meanwhile
            raise ActionError(f"{target} exists already") from None
        except OSError as error:
            raise ActionError(f"Could not move {source} to {target}: {_reason(error)}") from None
        self.deleting = True
        try:
            _delete(source)
        except OSError as error:
            raise ActionError(_delete_problem(error, source, moving=True)) from None
        self.end()

    def end(self) -> str:
        """When the source is deleted, the old target is deleted too: the move is complete.
        Otherwise the copy is deleted and an old target gets its name again. Running it again
        (when Ctrl+C cut it short) changes nothing more. Returns what to add to the message when
        a file can't be put back."""
        source, target, copy, old = self.source, self.target, self.copy, self.old
        if self.deleting and not os.path.lexists(source):
            for path in (old, copy):
                if path is not None:
                    _finish(path)
            return ""
        if self.deleting and self.mode is not None:  # e.g. read-only again after Ctrl+C cut that short
            with contextlib.suppress(OSError):
                if os.stat(source).st_mode != self.mode:
                    os.chmod(source, self.mode)
        placed = self.placing and copy is not None and not os.path.lexists(copy)  # the target is the copy
        if copy is not None:
            _finish(copy)  # the source is complete: deletes the copy if it doesn't have the target's name
        if old is None:
            if placed:
                try:
                    _delete(target)  # no file there, as before
                except FileNotFoundError:
                    pass
                except OSError:
                    return f". {target} is a copy of it now"
            return ""
        if not (self.put_aside or (self.aside and not os.path.lexists(target))):
            _finish(old)  # still the empty file that kept the name free
            return ""
        if not os.path.lexists(old):  # the old target got its name again already
            _forget(old)
            return ""
        if not placed and os.path.lexists(target):  # another file came there meanwhile
            return _keep_old(old, target)
        try:
            if placed:
                _writable(target)  # Windows doesn't replace a read-only file
            os.replace(old, target)
        except OSError:
            return _keep_old(old, target)
        _forget(old)
        return ""


def _keep_old(old: Path, target: Path) -> str:
    """Gives an old target that can't get its name again a name of its own (a later export
    would delete it as a left-over temporary file). Returns what to add to the message."""
    for number in range(1, 100):
        name = f"{target.stem} (old{'' if number == 1 else f' {number}'}){target.suffix}"
        try:
            _rename_new(old, target.with_name(name))
        except FileExistsError:
            continue
        except OSError:
            break
        _forget(old)
        return f". The old {target.name} is {name} now"
    return f". The old {target.name} is {old} now"


def _to_the_end(work: Callable[[], str]) -> str:
    """Runs ``work``, which may run again without harm, to its end: when Ctrl+C cuts it short,
    it runs again, and the KeyboardInterrupt comes afterwards."""
    interrupted: KeyboardInterrupt | None = None
    for _ in range(100):
        try:
            result = work()
        except KeyboardInterrupt as error:
            interrupted = error
            continue
        if interrupted is not None:
            raise interrupted
        return result
    raise interrupted or KeyboardInterrupt()


def _temporary_for(source: Path, target: Path, old: bool = False) -> Path:
    try:
        return _new_temporary(target, old)
    except PermissionError:
        raise ActionError(f"No permission to write in {target.parent}") from None
    except OSError as error:
        raise ActionError(f"Could not move {source} to {target}: {_reason(error)}") from None


def _writable(path: Path) -> None:
    with contextlib.suppress(OSError):
        mode = os.stat(path).st_mode
        if not mode & stat.S_IWRITE:
            os.chmod(path, mode | stat.S_IWRITE)


def _rename_new(source: Path, target: Path) -> None:
    """Renames ``source`` to ``target``, which must not exist (Windows' rename checks that)."""
    if os.name != "nt" and os.path.lexists(target):
        raise FileExistsError(errno.EEXIST, "File exists", str(target))
    os.rename(source, target)


def _same(path: Path, other: Path) -> bool:
    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def _delete(path: Path) -> None:
    """Deletes the file ``path``, also when it is read-only (a copy keeps that attribute)."""
    try:
        os.remove(path)
    except PermissionError:
        mode = os.stat(path).st_mode
        if mode & stat.S_IWRITE:
            raise
        try:
            os.chmod(path, mode | stat.S_IWRITE)
            os.remove(path)
        except BaseException:  # also Ctrl+C: the file stays read-only
            with contextlib.suppress(OSError):
                os.chmod(path, mode)
            raise


# --- temporary files ------------------------------------------------------------------------------


def _new_temporary(target: Path, old: bool = False) -> Path:
    """An empty file next to ``target`` named ``~$name.<computer>-<process>-<random>.csv`` (with
    ``old``, for an old target put aside: ``….old.csv``), after the ones of ``target`` that a
    process of this computer left when it ended were deleted. OSError when it can't be created."""
    _remove_left_overs(target)
    stem = _short_stem(target)
    for _ in range(3):
        name = f"~${stem}.{_COMPUTER}-{os.getpid():x}-{secrets.token_hex(3)}{'.old' if old else ''}{target.suffix}"
        path = target.parent / name
        with _ACTIVE_LOCK:
            _ACTIVE.add(_key(path))
        try:
            with open(path, "xb"):
                pass
            return path
        except FileExistsError:
            _forget(path)
        except OSError:
            _forget(path)
            raise
        except BaseException:  # Ctrl+C, maybe right after the file was created
            with contextlib.suppress(OSError):
                os.remove(path)
            _forget(path)
            raise
    raise FileExistsError(errno.EEXIST, "Can't find a free name for a temporary file", str(target.parent))


def _short_stem(target: Path) -> str:
    """The name of ``target`` without its extension, shortened so that the name of a temporary
    file stays within the 255 characters of Windows."""
    room = _ROOM - _units(target.suffix)
    stem = target.stem[: max(room, 0)]
    while stem and _units(stem) > room:
        stem = stem[:-1]
    return stem


_OURS = re.compile(rf"~\$.*\.{re.escape(_COMPUTER)}-[0-9a-f]{{1,8}}-[0-9a-f]{{6}}(\.old)?(\.[^.]*)?", re.IGNORECASE)


def _remove_left_overs(target: Path) -> None:
    """Deletes the temporary files of ``target`` that a process of this computer left when it
    ended (e.g. it was ended in the Task Manager while writing); an old target put aside only
    when a file has the target's name again."""
    start = f"~${_short_stem(target)}.{_COMPUTER}-"
    pattern = re.compile(re.escape(start) + r"([0-9a-f]{1,8})-[0-9a-f]{6}(\.old)?" + re.escape(target.suffix), re.IGNORECASE)
    for name in _temporary_names(target.parent):
        if (found := pattern.fullmatch(name)) is None:
            continue
        if found.group(2) and not os.path.lexists(target):  # the only copy of the old target
            continue
        path = target.parent / name
        with _ACTIVE_LOCK:
            if _key(path) in _ACTIVE:
                continue
        process = int(found.group(1), 16)
        if process == os.getpid() or not _running(process):
            with contextlib.suppress(OSError):
                _delete(path)


def _temporary_names(folder: Path) -> list[str]:
    """The names of this computer's temporary files in ``folder``. In a ``batch``, the folder is
    looked at only the first time."""
    seen = _BATCH.get()
    key = os.path.normcase(os.path.abspath(folder))
    if seen is not None and key in seen:
        return seen[key]
    try:
        with os.scandir(folder) as entries:
            names = [entry.name for entry in entries if entry.name.startswith("~$") and _OURS.fullmatch(entry.name)]
    except OSError:
        names = []
    if seen is not None:
        seen[key] = names
    return names


def _finish(temporary: Path) -> None:
    """Deletes a temporary file (if it is still there) and forgets it."""
    with contextlib.suppress(OSError):
        _delete(temporary)
    _forget(temporary)


def _forget(temporary: Path) -> None:
    with _ACTIVE_LOCK:
        _ACTIVE.discard(_key(temporary))


def _key(temporary: Path) -> str:
    """The name is enough: it holds the process id and a random part."""
    return os.path.normcase(temporary.name)


# --- messages -------------------------------------------------------------------------------------


def _open_elsewhere(shown: str) -> str:
    return f"{shown} is open in another program (e.g. Excel): close it and run again"


def _read_problem(error: BaseException, path: Path, shown: str) -> str:
    code = _code(error)
    if code in _IN_USE or (isinstance(error, PermissionError) and _probe(path, _READ) in _IN_USE):
        return _open_elsewhere(shown)
    if isinstance(error, PermissionError) or code == 5:
        return f"No permission to read {shown}"
    return f"Can't open {shown}: {_reason(error)}"


def _write_problem(error: OSError, target: Path, shown: str) -> str:
    """Why ``target`` can't be opened for writing."""
    if not isinstance(error, PermissionError):
        return f"Can't write {shown}: {_reason(error)}"
    if not os.access(target, os.W_OK):  # on Windows: the read-only attribute
        return f"{shown} is read-only, so it can't be replaced"
    if _probe(target, _WRITE) in _IN_USE:
        return _open_elsewhere(shown)
    return f"No permission to write in {target.parent}"


def _replace_problem(target: Path, shown: str) -> str:
    """Why ``target`` can't be replaced by another file (a PermissionError)."""
    if target.is_file() and not os.access(target, os.W_OK):  # on Windows: the read-only attribute
        return f"{shown} is read-only, so it can't be replaced"
    if _probe(target, _DELETE) == 5:  # access denied: no right to delete it
        return f"No permission to replace {shown}"
    return _open_elsewhere(shown)


def _move_problem(error: OSError, source: Path, target: Path) -> str:
    """Why ``source`` can't be renamed to ``target`` on the same drive."""
    if _code(error) in _IN_USE:  # renaming needs the source free
        return _open_elsewhere(str(source))
    if not isinstance(error, PermissionError):
        return f"Could not move {source} to {target}: {_reason(error)}"
    if os.path.lexists(target) and not _same(source, target):
        if not os.access(target, os.W_OK) or _probe(target, _DELETE) in (5, *_IN_USE):
            return _replace_problem(target, str(target))
    if _probe(source, _DELETE) in _IN_USE:
        return _open_elsewhere(str(source))
    if not _can_create(target):
        return f"No permission to write in {target.parent}"
    return f"No permission to move or rename {source}"


def _can_create(target: Path) -> bool:
    """Whether a new file can be created next to ``target`` (True when that can't be told)."""
    try:
        _finish(_new_temporary(target))
    except PermissionError:
        return False
    except OSError:
        pass
    return True


def _delete_problem(error: OSError, path: Path, *, moving: bool = False, recycle: bool = False) -> str:
    if _code(error) in _IN_USE:
        return _open_elsewhere(str(path))
    if path.is_file() and not os.access(path, os.W_OK):
        return f"{path} is read-only, so it can't be deleted"
    if isinstance(error, PermissionError):
        return f"No permission to delete {path}" + (", so it can't be moved" if moving else "")
    failed = f"Could not move {path} to the Recycle Bin" if recycle else f"Could not delete {path}"
    return f"{failed}: {reason}" if (reason := _reason(error, "")) else failed


def _code(error: BaseException) -> int | None:
    """The Windows error number of ``error`` (also in an HRESULT, as the Recycle Bin gives it),
    or the one in a library's "(os error 32)"."""
    code = getattr(error, "winerror", None)
    if isinstance(code, int):
        code &= 0xFFFFFFFF
        return code & 0xFFFF if code >> 16 == 0x8007 else code
    found = re.search(r"\(os error (\d+)\)", str(error))
    return int(found.group(1)) if found else None


def _reason(error: BaseException, default: str | None = None) -> str:
    """The error's own words, without the path (Polars adds "(os error 32): path"). Without
    words: ``default``, or the error's type."""
    text = getattr(error, "strerror", None)
    if not text:
        lines = str(error).splitlines()
        text = re.sub(r"^\[(Errno|WinError) -?\d+\] (None\b.*)?", "", lines[0] if lines else "")
        if not text and os.name == "nt" and (code := _code(error)):
            text = ctypes.FormatError(code)  # e.g. an OSError with only an HRESULT
    text = re.sub(r"\s*\(os error \d+\).*", "", text or "").rstrip(". ")
    return text or (type(error).__name__ if default is None else default)


# --- Windows --------------------------------------------------------------------------------------


def _probe(path: Path, access: int) -> int:
    """0 if ``path`` can be opened with ``access`` now, else Windows' error number (32: another
    program has the file open). Always 0 on other systems."""
    if os.name != "nt":
        return 0
    kernel = _kernel32()
    handle = kernel.CreateFileW(str(path), access, 7, None, 3, 0, None)  # all sharing, an existing file
    if handle is None or handle == ctypes.c_void_p(-1).value:
        return ctypes.get_last_error() or -1
    kernel.CloseHandle(handle)
    return 0


def _running(process: int) -> bool:
    """Whether the process with this id still runs (True when that can't be told)."""
    if os.name != "nt":
        try:
            os.kill(process, 0)
        except ProcessLookupError:
            return False
        except OSError:
            return True
        return True
    kernel = _kernel32()
    handle = kernel.OpenProcess(0x1000, False, process)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: there is no such process
    try:
        code = ctypes.c_ulong()
        return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259  # STILL_ACTIVE
    finally:
        kernel.CloseHandle(handle)


@functools.cache
def _kernel32() -> Any:
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.GetExitCodeProcess.restype = wintypes.BOOL
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.SetFileAttributesW.restype = wintypes.BOOL
    kernel.SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
    return kernel

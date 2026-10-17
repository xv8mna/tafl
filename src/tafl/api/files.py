"""Reading one file or all files of a folder (and its subfolders), for import actions, and
writing, copying, moving and deleting files safely, for exports and commands.

An import action puts ``source_params(...)`` first in its parameters, reads each file of
``source_files(p, ctx)`` and returns ``combine(...)``; ``fingerprint=sources_fingerprint``
makes the app notice new, changed and deleted files::

    @action("my.import", "Import my files", kind="source",
            params=[*source_params("My files (*.my)", "*.my")],
            fingerprint=sources_fingerprint)
    def import_my(df, p, ctx):
        files, folder = source_files(p, ctx)
        return combine([(path, None, read_my(path)) for path in files], p, folder)

An export action writes into the file that ``replace_file`` gives it: the old file stays as it
was until the new one is complete::

    with replace_file(ctx.resolve_path(p.file), ctx.warn) as path:
        write_my(df, path)

``copy_file``, ``move_file`` and ``delete_file`` either do their work or change nothing, and
``read_error`` turns an error from reading a file into a plain message. ``name_problem`` and
``check_folder`` say why a file or folder name isn't allowed in Windows.
"""

from __future__ import annotations

import contextlib
import ctypes
import errno
import fnmatch
import functools
import os
import re
import secrets
import shutil
import stat
import tempfile
import threading
from collections.abc import Callable, Iterable, Iterator, Sequence
from pathlib import Path
from typing import Any

import polars as pl

from . import params as P
from .context import Context
from .errors import ActionError, SkipRest

_IN_FOLDER = {"source": "folder"}


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
            "one. An empty table (with a warning) has only the file and path columns: a later step that "
            "names another column fails.",
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
    wanted = [p.strip().lower() for p in re.split(r"[;,]", patterns or "") if p.strip()] or ["*"]
    candidates = folder.rglob("*") if subfolders else folder.glob("*")
    found = [
        path
        for path in candidates
        if not path.name.startswith("~$") and any(fnmatch.fnmatchcase(path.name.lower(), w) for w in wanted) and path.is_file()
    ]
    return sorted(found, key=lambda path: _natural(path.relative_to(folder).as_posix().lower()))


def _natural(text: str) -> list[Any]:
    return [(0, int(part), part) if part.isdigit() else (1, 0, part) for part in re.split(r"(\d+)", text) if part]


def source_files(params: Any, ctx: Context) -> tuple[list[Path], Path | None]:
    """The files to read and, for a folder, the folder. Raises a readable error when there
    is nothing to read.

    A folder without matching files is an ActionError, or as chosen in ``no_files``: no files
    (an empty list) and a warning, or a SkipRest with an empty table (``combine`` gives the
    same table for no files)."""
    files, folder = _sources(params, ctx)
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

_ROOM = _LONGEST - len("~$.ffffffff-ffffff")  # for the name and extension in a temporary file's name
_CHUNK = 1 << 20
_READ, _WRITE, _DELETE = 0x80000000, 0x40000000, 0x00010000  # access rights in Windows
_IN_USE = (32, 33)  # Windows' errors when another program has the file open
_ACTIVE: set[str] = set()  # the temporary files this process is writing
_ACTIVE_LOCK = threading.Lock()


@contextlib.contextmanager
def replace_file(target: Path, warn: Callable[[str], None] | None = None) -> Iterator[Path]:
    """A new, empty file next to ``target`` for the block to write. When the block ends without
    an error, the new file replaces ``target``; otherwise it is deleted and ``target`` stays as it
    was, also when the run is stopped. The folder is created if needed. Problems are ActionErrors
    that say why: the file is open in another program or read-only, the name isn't allowed, no
    permission to write in the folder.

    The new file is named ``~$name.<process>-<random>.csv``: folder imports leave such files out,
    and the next export of ``target`` deletes the ones of a process that has ended (e.g. one that
    was ended in the Task Manager). In a folder where files may be changed but no new ones
    created, the block writes a file in the temporary folder that then is written over
    ``target``; ``warn`` is told that a failure while writing could leave it half-written."""
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
    renamed in one step; to another drive it is copied next to the target, the source is deleted,
    and then the copy gets the target's name (an old target is kept aside until then). Without
    ``replace``, an existing target is an error. Problems are ActionErrors that say why, e.g.
    which of the two files is open in another program."""
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
    """Copy next to the target, put an old target aside, delete the source, give the copy the
    target's name. After a failure, everything is put back."""
    copy: Path | None = _temporary_for(source, target)
    old: Path | None = None
    try:
        try:
            _copy_into(source, copy)
        except OSError as error:  # e.g. the disk is full
            raise ActionError(f"Can't write {target}: {_reason(error)}") from None
        if replace and os.path.lexists(target):
            if not os.access(target, os.W_OK):  # as on the same drive, where Windows refuses it
                raise ActionError(f"{target} is read-only, so it can't be replaced")
            old = _temporary_for(source, target)
            try:
                os.replace(target, old)
            except PermissionError:
                raise ActionError(_replace_problem(target, str(target))) from None
            except OSError as error:
                raise ActionError(f"Could not move {source} to {target}: {_reason(error)}") from None
        try:
            _delete(source)
        except OSError as error:
            old, aside = None, old
            raise ActionError(_delete_problem(error, source, moving=True) + _put_back(aside, target)) from None
        try:
            os.rename(copy, target)
        except OSError as error:  # the target's place was free a moment ago
            copy, made = None, copy
            old, aside = None, old
            notes = _put_back(aside, target) + _bring_back(made, source)
            raise ActionError(f"Could not move {source} to {target}: {_reason(error)}{notes}") from None
        copy = None
    finally:
        for path in (copy, old):
            if path is not None:
                _finish(path)


def _temporary_for(source: Path, target: Path) -> Path:
    try:
        return _new_temporary(target)
    except PermissionError:
        raise ActionError(f"No permission to write in {target.parent}") from None
    except OSError as error:
        raise ActionError(f"Could not move {source} to {target}: {_reason(error)}") from None


def _put_back(aside: Path | None, target: Path) -> str:
    """Gives an old target that was put aside its name again. Returns what to add to the
    message when that fails."""
    if aside is None:
        return ""
    try:
        os.replace(aside, target)
        return ""
    except OSError:
        return f". The old {target.name} is {aside} now"
    finally:
        _forget(aside)


def _bring_back(copy: Path, source: Path) -> str:
    """Writes the source again from its copy, after it was deleted for a move that failed.
    Returns what to add to the message when that fails."""
    try:
        shutil.copy2(copy, source)
    except OSError:
        _forget(copy)
        return f". {source.name} is {copy} now"
    _finish(copy)
    return ""


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
        os.chmod(path, mode | stat.S_IWRITE)
        try:
            os.remove(path)
        except OSError:
            with contextlib.suppress(OSError):
                os.chmod(path, mode)
            raise


# --- temporary files ------------------------------------------------------------------------------


def _new_temporary(target: Path) -> Path:
    """An empty file next to ``target`` named ``~$name.<process>-<random>.csv``, after the ones
    of ``target`` that a process left when it ended were deleted. OSError when it can't be
    created."""
    _remove_left_overs(target)
    stem = _short_stem(target)
    for _ in range(3):
        path = target.parent / f"~${stem}.{os.getpid():x}-{secrets.token_hex(3)}{target.suffix}"
        with _ACTIVE_LOCK:
            _ACTIVE.add(_key(path))
        try:
            with open(path, "xb"):
                pass
            return path
        except FileExistsError:
            _forget(path)
        except BaseException:
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


def _remove_left_overs(target: Path) -> None:
    """Deletes the temporary files of ``target`` that a process left when it ended (e.g. it was
    ended in the Task Manager while writing)."""
    start = f"~${_short_stem(target)}."
    pattern = re.compile(re.escape(start) + r"([0-9a-f]{1,8})-[0-9a-f]{6}" + re.escape(target.suffix), re.IGNORECASE)
    try:
        with os.scandir(target.parent) as entries:
            names = [entry.name for entry in entries if entry.name.startswith("~$")]
    except OSError:
        return
    for name in names:
        if (found := pattern.fullmatch(name)) is None:
            continue
        path = target.parent / name
        with _ACTIVE_LOCK:
            if _key(path) in _ACTIVE:
                continue
        process = int(found.group(1), 16)
        if process == os.getpid() or not _running(process):
            with contextlib.suppress(OSError):
                _delete(path)


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
    return f"No permission to move or rename {source}"


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

"""Reading one file or all files of a folder (and its subfolders), for import actions, and
writing a file safely, for export actions.

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

    with replace_file(ctx.resolve_path(p.file)) as path:
        write_my(df, path)
"""

from __future__ import annotations

import contextlib
import fnmatch
import os
import re
import secrets
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
            help="e.g. for a folder that is empty on some days. An empty table comes with a warning; "
            "skipping leaves out the steps after this one.",
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


@contextlib.contextmanager
def replace_file(target: Path) -> Iterator[Path]:
    """A new, empty file next to ``target`` for the block to write (named ``~$name.1a2b3c4d.csv``:
    folder imports leave such files out). When the block ends without an error, the new file
    replaces ``target``; otherwise it is deleted and ``target`` stays as it was, also when the
    run is stopped. The folder is created if needed. Problems are ActionErrors that say why:
    the file is open in another program, read-only, no permission to write in the folder."""
    folder = target.parent
    if target.is_dir():
        raise ActionError(f"{target} is a folder: choose a file name")
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise ActionError(f"No permission to create the folder {folder}") from None
    except OSError as error:
        raise ActionError(f"Can't create the folder {folder}: {_reason(error)}") from None
    temporary = folder / f"~${target.stem}.{secrets.token_hex(4)}{target.suffix}"
    try:
        with open(temporary, "xb"):
            pass
    except PermissionError:
        raise ActionError(f"No permission to write in {folder}") from None
    except OSError as error:
        raise ActionError(f"Can't write {target.name}: {_reason(error)}") from None
    try:
        try:
            yield temporary
        except OSError as error:  # e.g. the disk is full
            raise ActionError(f"Can't write {target.name}: {_reason(error)}") from None
        try:
            os.replace(temporary, target)
        except PermissionError:  # the new file could be written next to it: the old one is in use
            raise ActionError(_in_use(target)) from None
        except OSError as error:
            raise ActionError(f"Can't write {target.name}: {_reason(error)}") from None
    finally:
        with contextlib.suppress(OSError):
            temporary.unlink(missing_ok=True)


def read_error(error: OSError, path: Path) -> ActionError:
    """A plain message for a file that exists but can't be opened, e.g. a workbook that is
    open in Excel."""
    if isinstance(error, PermissionError):
        return ActionError(_in_use(path))
    return ActionError(f"Can't open {path.name}: {_reason(error)}")


def _in_use(path: Path) -> str:
    if path.is_file() and not os.access(path, os.W_OK):  # on Windows: the read-only attribute
        return f"{path.name} is read-only, so it can't be replaced"
    return f"{path.name} is open in another program (e.g. Excel): close it and run again"


def _reason(error: OSError) -> str:
    """The error's own words, without the path (Polars adds "(os error 32): path")."""
    text = error.strerror or str(error).splitlines()[0]
    return re.sub(r"\s*\(os error \d+\).*", "", text).rstrip(".")


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

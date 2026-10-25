"""Files and folders: a table of the files in a folder, and commands that copy, move, rename
or delete the files listed in a table (copy, move and delete also all files the table's import
read), or create a folder. The commands work out what happens to every file before they change
the first one: a missing file, a name that isn't allowed, a read-only file to replace or delete,
or two files that would get the same name stop the step with nothing changed. Each file is
copied, moved, renamed or deleted completely or not at all (see ``tafl.api.files``)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

from tafl.api import ActionError, Cancelled, Endpoint, P, action, endpoints, files, find_files


def _folder_endpoints(p, direction: str, key: str, *, name: str = "", mode: str = "", how: str = "") -> list[Endpoint]:
    """The folder in setting ``key``, if one is named (``how``: the setting that ``mode`` comes from)."""
    folder, how = p[key].strip(), how if mode else ""
    found = Endpoint(direction, endpoints.FOLDER, place=folder, name=name, mode=mode, setting=key, mode_setting=how)
    return [found] if folder else []


def _subfolders(p) -> str:
    return "with subfolders" if p.subfolders else ""


def _listing_fingerprint(p, ctx):
    """Changes when a file is added, removed or changed."""
    folder = ctx.resolve_path(p.folder)
    if not folder.is_dir():
        return str(folder)
    stats = []
    for path in find_files(folder, p.pattern, p.subfolders):
        try:
            stat = path.stat()
            stats.append((str(path), stat.st_mtime_ns, stat.st_size))
        except OSError:
            stats.append((str(path), None, None))
    return tuple(stats)


@action(
    "files.list",
    "Files in a folder",
    category="Import",
    kind="source",
    params=[
        P.Folder("folder", "Folder"),
        P.Text("pattern", "File names", default="*", help="* stands for any text, e.g. *.csv; several patterns with ;"),
        P.Bool("subfolders", "Include subfolders (all levels)", default=False),
    ],
    summary=lambda p: f"Files in {Path(str(p.folder)).name or p.folder}" + (" and its subfolders" if p.subfolders else ""),
    fingerprint=_listing_fingerprint,
    endpoints=lambda p: _folder_endpoints(
        p, endpoints.IN, "folder", name=p.pattern.strip() or "*", mode=_subfolders(p), how="subfolders"
    ),
)
def list_files(df, p, ctx):
    """A table of the files in a folder (and its subfolders): name, path below the folder,
    subfolder, extension, size in bytes, when the file was last changed, and the full path.
    Handy for checking what arrived in a folder, and for the file commands (copy, move, ...)."""
    folder = ctx.resolve_path(p.folder)
    if not folder.is_dir():
        raise ActionError(f"Folder not found: {folder}")
    rows = []
    for path in find_files(folder, p.pattern, p.subfolders):
        ctx.check_cancelled()
        try:
            stat = path.stat()
        except OSError:
            continue
        relative = path.relative_to(folder)
        rows.append(
            (
                path.name,
                str(relative),
                "" if str(relative.parent) == "." else str(relative.parent),
                path.suffix.lower().lstrip("."),
                stat.st_size,
                dt.datetime.fromtimestamp(stat.st_mtime).replace(microsecond=0),
                str(path),
            )
        )
    schema = {
        "File": pl.String,
        "Path": pl.String,
        "Folder": pl.String,
        "Extension": pl.String,
        "Size": pl.Int64,
        "Modified": pl.Datetime("us"),
        "Full path": pl.String,
    }
    return pl.DataFrame(rows, schema=schema, orient="row")


# --- commands on the files listed in a table ---------------------------------------------------

_EXISTING = [
    ("number", "Keep both: name (2).csv"),
    ("replace", "Replace it"),
    ("skip", "Leave it, skip this file"),
    ("error", "Stop with an error"),
]


_IN_COLUMN = {"which": "column"}


def _file_params(which: bool = True) -> list:
    """``which``: the files can also be all files the table's import read."""
    listed = _IN_COLUMN if which else None
    choice = P.Choice(
        "which",
        "Which files",
        [("column", "Listed in a column"), ("imported", "All files this table's import read")],
        help="All files …: every file that the first step, an import of all files in a folder, read, also files "
        "without rows (only a header row, or a filter left out their rows). A file that arrives after the import "
        "stays where it is",
    )
    return [
        *([choice] if which else []),
        P.Column("column", "Column with the files", help="Full paths, or paths below the folder chosen next", visible_if=listed),
        P.Folder(
            "base",
            "Paths in the column are below",
            required=False,
            help=r"For paths like 2025\01\orders.csv (the Path column of a folder import); empty = the project's folder",
            visible_if=listed,
        ),
        P.Choice("missing", "If a file doesn't exist", [("error", "Stop with an error"), ("skip", "Skip it")]),
    ]


_NOT_IMPORTED = (
    "This table doesn't start with an import of all files in a folder, so no files were read: "
    "choose “Listed in a column” in Which files"
)


def _check_which(p, vc) -> list[str]:
    """All files the import read, in a table that doesn't start with a folder import: also a
    warning while the project is edited, so that it doesn't wait for a run."""
    return [_NOT_IMPORTED] if p.get("which") == "imported" and vc.imported is False else []


def _migrate_which(params: dict, from_version: int) -> dict:
    """Version 1 took the files from a column."""
    if from_version < 2:
        params.setdefault("which", "column")
    return params


def _files_endpoints(p, direction: str, mode: str = "", how: str = "", *, which: bool = True) -> list[Endpoint]:
    """The files a command works on: those listed in a column, or (``which``) those the table's
    import read (``how``: the setting that ``mode`` comes from)."""
    if which and p.get("which") == "imported":
        return [Endpoint(direction, endpoints.FILE, mode=mode, setting="which", imported=True, mode_setting=how)]
    if not p.column.strip():
        return []
    return [
        Endpoint(direction, endpoints.FILE, place=p.base.strip(), mode=mode, setting="column", column=p.column, mode_setting=how)
    ]


def _copy_endpoints(p, direction: str) -> list[Endpoint]:
    """The files, then the folder they go to."""
    target = _folder_endpoints(p, endpoints.OUT, "target", mode="keep subfolders" if p.subfolders else "", how="subfolders")
    return _files_endpoints(p, direction) + target


def _which(p) -> str:
    """The files of the step, for its summary."""
    return "the files the import read" if p.get("which") == "imported" else f"the files in {p.column or '?'}"


def _base(p, ctx) -> Path:
    return ctx.resolve_path(p.base) if p.base else (ctx.project_dir or Path.cwd())


def _resolve(value: str, base: Path) -> tuple[Path, Path | None]:
    """The file a cell names, and its path below the base folder (None for a full path)."""
    path = Path(os.path.expandvars(value.strip()))
    return (path, None) if path.is_absolute() else (base / path, path)


def _key(path: Path) -> str:
    """The same text for all spellings of a path (in Windows: upper and lower case, / and \\)."""
    return os.path.normcase(os.path.abspath(path))


def _same_file(path: Path, other: Path) -> bool:
    if _key(path) == _key(other):
        return True
    try:
        return os.path.samefile(path, other)
    except OSError:  # ``other`` doesn't exist
        return False


def _listed_files(df: pl.DataFrame, p, ctx) -> list[tuple[Path, Path | None]]:
    """(file, path below the base folder or None) for each file named in the column, once each;
    or for each file the table's import read (with its path below the import's folder)."""
    if p.get("which") == "imported":
        found, folder = ctx.imported_files, ctx.imported_folder
        if found is None or folder is None:
            raise ActionError(_NOT_IMPORTED)
        return [(path, _below(path, folder)) for path in found]
    if p.column not in df.columns:
        raise ActionError(f"There is no column {p.column}")
    base = _base(p, ctx)
    found: dict[str, tuple[Path, Path | None]] = {}
    for value in df[p.column].cast(pl.String).to_list():
        if value is not None and value.strip():
            path, relative = _resolve(value, base)
            found.setdefault(_key(path), (path, relative))
    return list(found.values())


def _below(path: Path, folder: Path) -> Path | None:
    try:
        return path.relative_to(folder)
    except ValueError:
        return None


def _listed_in(p) -> str:
    """Where the files of the step come from, for "…, which <it> too"."""
    return "the import read" if p.get("which") == "imported" else f"is in the column {p.column}"


def _exists(path: Path, p, skipped: list[Path]) -> bool:
    if path.is_file():
        return True
    if p.missing == "skip":
        skipped.append(path)
        return False
    raise ActionError(f"{path} is a folder, not a file" if path.is_dir() else f"File not found: {path}")


def _numbered(target: Path, exists) -> Path:
    number = 2
    while exists(candidate := target.with_name(f"{target.stem} ({number}){target.suffix}")):
        number += 1
    return candidate


class _Targets:
    """Where the files of one step go, worked out for all of them before the first file is
    changed: a step never replaces a file it has just written, nor one of its own listed files."""

    def __init__(self, sources: list[Path], *, moves: bool, done: str, listed_in: str) -> None:
        self.listed = {_key(source) for source in sources}
        self.written: dict[str, Path] = {}  # a target -> the file that goes there
        self.gone: set[str] = set()  # listed files moved away by the step
        self.moves, self.done, self.listed_in = moves, done, listed_in

    def exists(self, path: Path) -> bool:
        """Whether ``path`` is taken when the step gets to the next file."""
        key = _key(path)
        return key in self.written or (key not in self.gone and path.exists())

    def place(self, source: Path, wanted: Path, existing: str, hint: str = "") -> tuple[Path, bool] | None:
        """Where ``source`` goes (``wanted`` or a numbered name), and whether it replaces a file
        there; None = skip it. ``hint`` ends the message when two files would get the same name."""
        destination, replaces = wanted, False
        if self.exists(wanted):
            first = self.written.get(_key(wanted))
            if existing == "skip":
                return None
            if existing == "number":
                destination = _numbered(wanted, self.exists)
            elif first is not None:
                raise ActionError(
                    f"{first} and {source} would both be {self.done} {wanted}. "
                    f"Choose “Keep both” to number them{hint}."
                )
            elif existing == "error":
                raise ActionError(f"{wanted} exists already")
            elif _key(wanted) in self.listed:
                raise ActionError(
                    f"{source} would replace {wanted}, which {self.listed_in} too. Choose “Keep both” to number it."
                )
            elif wanted.is_dir():
                raise ActionError(f"{wanted} is a folder: a file can't replace it")
            elif not os.access(wanted, os.W_OK):  # on Windows: the read-only attribute
                raise ActionError(f"{wanted} is read-only, so it can't be replaced")
            else:
                replaces = True
        if problem := files.name_problem(destination.name, str(destination)):  # e.g. too long with (2)
            raise ActionError(problem)
        self.take(source, destination)
        return destination, replaces

    def take(self, source: Path, destination: Path) -> None:
        self.written[_key(destination)] = source
        if self.moves:
            self.gone.add(_key(source))


def _failed(error: ActionError, done: int, past: str) -> ActionError:
    """The message of a file that failed part-way, with how many files were done before it."""
    if not done:
        return error
    return ActionError(f"{error}. {done:,} file{'s were' if done != 1 else ' was'} {past} before it")


def _change_each(ctx, plan: list[tuple], change, changed, past: str, stopped: tuple[str, str]) -> int:
    """Runs ``change(*item)`` for each item of ``plan``; returns how many there were. A file that
    fails stops the step with how many files were done before it (``past``: "moved"). After a
    stop (Stop, or Ctrl+C on the command line) the step says how many files it changed: with
    ``stopped`` ("Moved", " to archive"), "Moved 3 files to archive before the stop".
    ``changed(*item)`` tells whether the file that was being changed then is done."""
    number = 0
    try:
        for number, item in enumerate(plan):
            ctx.check_cancelled()
            try:
                change(*item)
            except ActionError as error:
                raise _failed(error, number, past) from None
        return len(plan)
    except (Cancelled, KeyboardInterrupt) as stop:  # Stop comes before a file, Ctrl+C at any moment
        done = number + (1 if isinstance(stop, KeyboardInterrupt) and plan and _safely(changed, *plan[number]) else 0)
        ctx.log(f"{stopped[0]} {done:,} file{'s' if done != 1 else ''}{stopped[1]} before the stop")
        raise


def _safely(check, *args) -> bool:
    try:
        return bool(check(*args))
    except OSError:
        return False


def _moved(source: Path, destination: Path, *_) -> bool:
    return not os.path.lexists(source) and os.path.lexists(destination)


def _copied(source: Path, destination: Path, *_) -> bool:
    """The copy has the target's name only when it is complete, with the source's time and size."""
    old, new = source.stat(), destination.stat()
    return (old.st_mtime_ns, old.st_size) == (new.st_mtime_ns, new.st_size)


def _deleted(source: Path, *_) -> bool:
    return not os.path.lexists(source)


def _report(ctx, verb: str, done: int, skipped: list[Path], where: str = "", unchanged: int = 0) -> None:
    text = f"{verb} {done:,} file{'s' if done != 1 else ''}{where}"
    notes = [f"{len(skipped):,} skipped"] if skipped else []
    if unchanged:
        notes.append(f"{unchanged:,} already had {'its' if unchanged == 1 else 'their'} new name")
    if notes:
        text += f" ({', '.join(notes)})"
    ctx.log(text)


def _copy_or_move(df, p, ctx, move: bool) -> pl.DataFrame:
    target = ctx.resolve_path(p.target)
    sources = _listed_files(df, p, ctx)
    done_to = "moved to" if move else "copied to"
    targets = _Targets([s for s, _ in sources], moves=move, done=done_to, listed_in=_listed_in(p))
    plan, skipped, folders = [], [], set()
    for source, relative in sources:  # all files are checked before the first one is changed
        ctx.check_cancelled()
        if not _exists(source, p, skipped):
            continue
        wanted = target / relative if p.subfolders and relative is not None else target / source.name
        if _same_file(source, wanted):
            skipped.append(source)
            continue
        if wanted.parent not in folders:
            files.check_folder(wanted.parent)
            folders.add(wanted.parent)
        hint = ", or keep the subfolders" if relative is not None and not p.subfolders else ""
        if (place := targets.place(source, wanted, p.existing, hint)) is None:
            skipped.append(source)
            continue
        plan.append((source, *place))

    def change(source: Path, destination: Path, replaces: bool) -> None:  # a failed one leaves both files as they were
        if move:
            files.move_file(source, destination, replace=replaces)
        else:
            files.copy_file(source, destination, replace=replaces, warn=ctx.warn)  # with the modification time

    verb = "Moved" if move else "Copied"
    with files.batch():  # each folder is searched once for temporary files that were left
        done = _change_each(ctx, plan, change, _moved if move else _copied, verb.lower(), (verb, f" to {target}"))
    _report(ctx, verb, done, skipped, f" to {target}")
    return df


_TARGET_PARAMS = [
    P.Folder("target", "To folder"),
    P.Bool(
        "subfolders",
        "Keep the subfolders of the paths",
        default=False,
        help=r"2025\01\orders.csv goes to <folder>\2025\01\orders.csv instead of <folder>\orders.csv",
    ),
    P.Choice("existing", "If a file of that name is there already", _EXISTING),
]


@action(
    "files.copy",
    "Copy files",
    category="Files",
    kind="command",
    params=[*_file_params(), *_TARGET_PARAMS],
    version=2,
    migrate=_migrate_which,
    checks=_check_which,
    summary=lambda p: f"Copy {_which(p)} to {Path(str(p.target)).name or p.target or '?'}",
    endpoints=lambda p: _copy_endpoints(p, endpoints.IN),
)
def copy_files(df, p, ctx):
    """Copies the files listed in a column of the table, or all files the table's import read,
    into a folder (it is created when needed). Runs only when the project is run; the table
    passes through unchanged."""
    return _copy_or_move(df, p, ctx, move=False)


@action(
    "files.move",
    "Move files",
    category="Files",
    kind="command",
    params=[*_file_params(), *_TARGET_PARAMS],
    version=2,
    migrate=_migrate_which,
    checks=_check_which,
    summary=lambda p: f"Move {_which(p)} to {Path(str(p.target)).name or p.target or '?'}",
    endpoints=lambda p: _copy_endpoints(p, endpoints.CMD),
)
def move_files(df, p, ctx):
    """Moves the files listed in a column of the table, or all files the table's import read,
    into a folder, e.g. imported daily exports into an archive folder. Runs only when the
    project is run; the table passes through unchanged."""
    return _copy_or_move(df, p, ctx, move=True)


@action(
    "files.rename",
    "Rename files",
    category="Files",
    kind="command",
    params=[
        *_file_params(which=False),
        P.Column("new_name", "Column with the new names", help="Only the name, e.g. orders_done.csv: the file stays in its folder"),
        P.Choice("existing", "If a file of that name is there already", _EXISTING),
    ],
    summary=lambda p: f"Rename the files in {p.column or '?'} to {p.new_name or '?'}",
    endpoints=lambda p: _files_endpoints(p, endpoints.CMD, f"new names in {p.new_name or '?'}", "new_name", which=False),
)
def rename_files(df, p, ctx):
    """Gives the files listed in a column the names in another column (calculate them with a
    formula, e.g. REPLACE([File], '.csv', '.done')). Runs only when the project is run."""
    for column in (p.column, p.new_name):
        if column not in df.columns:
            raise ActionError(f"There is no column {column}")
    base = _base(p, ctx)
    renames: dict[str, tuple[Path, str]] = {}
    for value, new in df.select(pl.col(p.column).cast(pl.String), pl.col(p.new_name).cast(pl.String)).rows():
        if value is not None and value.strip():
            source, _ = _resolve(value, base)
            renames.setdefault(_key(source), (source, (new or "").strip()))  # each file once
    targets = _Targets([source for source, _ in renames.values()], moves=True, done="renamed to", listed_in=_listed_in(p))
    plan, skipped, unchanged = [], [], 0
    for source, new in renames.values():  # all files are checked before the first one is renamed
        ctx.check_cancelled()
        if not new:
            skipped.append(source)
            continue
        if any(sep in new for sep in ("\\", "/")):
            raise ActionError(f"The new name {new} is a path: only a name is allowed (the file stays in its folder)")
        if problem := files.name_problem(new):
            raise ActionError(problem)
        if not _exists(source, p, skipped):
            continue
        wanted = source.with_name(new)
        if _same_file(source, wanted):
            if new == _real_name(source):
                unchanged += 1
                continue
            targets.take(source, wanted)  # only upper and lower case change
            place = wanted, False
        elif (place := targets.place(source, wanted, p.existing)) is None:
            skipped.append(source)
            continue
        plan.append((source, *place))

    def rename(source: Path, destination: Path, replaces: bool) -> None:
        files.move_file(source, destination, replace=replaces)

    done = _change_each(ctx, plan, rename, _moved, "renamed", ("Renamed", ""))
    _report(ctx, "Renamed", done, skipped, unchanged=unchanged)
    return df


def _real_name(path: Path) -> str:
    """The name as it is written on the disk (in Windows ``path`` may have it in another case)."""
    real = os.path.basename(os.path.realpath(path))
    return real if os.path.normcase(real) == os.path.normcase(path.name) else path.name


@action(
    "files.delete",
    "Delete files",
    category="Files",
    kind="command",
    params=[
        *_file_params(),
        P.Choice(
            "how", "Delete", [("recycle", "To the Recycle Bin (can be restored)"), ("permanent", "For good (can't be undone)")]
        ),
    ],
    version=2,
    migrate=_migrate_which,
    checks=_check_which,
    summary=lambda p: f"Delete {_which(p)}" + (" for good" if p.how == "permanent" else ""),
    endpoints=lambda p: _files_endpoints(p, endpoints.CMD, "for good" if p.how == "permanent" else "to the Recycle Bin", "how"),
)
def delete_files(df, p, ctx):
    """Deletes the files listed in a column of the table, or all files the table's import read:
    to the Recycle Bin, or for good. Runs only when the project is run; the table passes through
    unchanged."""
    chosen, skipped = [], []
    for source, _ in _listed_files(df, p, ctx):  # all files are checked before the first one is deleted
        ctx.check_cancelled()
        if _exists(source, p, skipped):
            if p.how == "permanent" and not os.access(source, os.W_OK):  # on Windows: the read-only attribute
                raise ActionError(f"{source} is read-only, so it can't be deleted")
            chosen.append(source)

    def delete(source: Path) -> None:
        files.delete_file(source, recycle=p.how != "permanent")

    past, stopped = ("deleted", ("Deleted", "")) if p.how == "permanent" else ("moved to the Recycle Bin", ("Moved", " to the Recycle Bin"))
    done = _change_each(ctx, [(source,) for source in chosen], delete, _deleted, past, stopped)
    _report(ctx, "Deleted" if p.how == "permanent" else "Moved to the Recycle Bin:", done, skipped)
    return df


@action(
    "files.mkdir",
    "Create folder",
    category="Files",
    kind="command",
    params=[P.Folder("folder", "Folder", help="Created with all the folders above it; nothing happens if it exists")],
    summary=lambda p: f"Create folder {Path(str(p.folder)).name or p.folder or '?'}",
    endpoints=lambda p: _folder_endpoints(p, endpoints.CMD, "folder", mode="create"),
)
def create_folder(df, p, ctx):
    """Creates a folder (and the folders above it) if it doesn't exist yet. Runs only when the
    project is run; the table passes through unchanged."""
    folder = ctx.resolve_path(p.folder)
    files.check_folder(folder)
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        raise ActionError(f"No permission to create the folder {folder}") from None
    except OSError as e:
        raise ActionError(f"Could not create {folder}: {e.strerror or e}") from None
    ctx.log(f"Folder {folder} is there")
    return df

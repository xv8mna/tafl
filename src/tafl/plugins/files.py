"""Files and folders: a table of the files in a folder, and commands that copy, move, rename
or delete the files listed in a table, or create a folder. The commands work out what happens
to every file before they change the first one: a missing file, a name that isn't allowed, a
read-only file to replace or delete, or two files that would get the same name stop the step
with nothing changed. Each file is copied, moved, renamed or deleted completely or not at all
(see ``tafl.api.files``)."""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path

import polars as pl

from tafl.api import ActionError, P, action, files, find_files


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


def _file_params() -> list:
    return [
        P.Column("column", "Column with the files", help="Full paths, or paths below the folder chosen next"),
        P.Folder(
            "base",
            "Paths in the column are below",
            required=False,
            help=r"For paths like 2025\01\orders.csv (the Path column of a folder import); empty = the project's folder",
        ),
        P.Choice("missing", "If a file doesn't exist", [("error", "Stop with an error"), ("skip", "Skip it")]),
    ]


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
    """(file, path below the base folder or None) for each file named in the column, once each."""
    if p.column not in df.columns:
        raise ActionError(f"There is no column {p.column}")
    base = _base(p, ctx)
    found: dict[str, tuple[Path, Path | None]] = {}
    for value in df[p.column].cast(pl.String).to_list():
        if value is not None and value.strip():
            path, relative = _resolve(value, base)
            found.setdefault(_key(path), (path, relative))
    return list(found.values())


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

    def __init__(self, sources: list[Path], *, moves: bool, done: str, column: str) -> None:
        self.listed = {_key(source) for source in sources}
        self.written: dict[str, Path] = {}  # a target -> the file that goes there
        self.gone: set[str] = set()  # listed files moved away by the step
        self.moves, self.done, self.column = moves, done, column

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
                    f"{source} would replace {wanted}, which is in the column {self.column} too. "
                    "Choose “Keep both” to number it."
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
    targets = _Targets([s for s, _ in sources], moves=move, done="moved to" if move else "copied to", column=p.column)
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
    done = 0
    for source, destination, replaces in plan:
        ctx.check_cancelled()
        try:  # a failed copy or move leaves both files as they were
            if move:
                files.move_file(source, destination, replace=replaces)
            else:
                files.copy_file(source, destination, replace=replaces, warn=ctx.warn)  # with the modification time
        except ActionError as error:
            raise _failed(error, done, "moved" if move else "copied") from None
        done += 1
    _report(ctx, "Moved" if move else "Copied", done, skipped, f" to {target}")
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
    summary=lambda p: f"Copy the files in {p.column or '?'} to {Path(str(p.target)).name or p.target or '?'}",
)
def copy_files(df, p, ctx):
    """Copies the files listed in a column of the table into a folder (it is created when
    needed). Runs only when the project is run; the table passes through unchanged."""
    return _copy_or_move(df, p, ctx, move=False)


@action(
    "files.move",
    "Move files",
    category="Files",
    kind="command",
    params=[*_file_params(), *_TARGET_PARAMS],
    summary=lambda p: f"Move the files in {p.column or '?'} to {Path(str(p.target)).name or p.target or '?'}",
)
def move_files(df, p, ctx):
    """Moves the files listed in a column of the table into a folder, e.g. imported daily
    exports into an archive folder. Runs only when the project is run; the table passes
    through unchanged."""
    return _copy_or_move(df, p, ctx, move=True)


@action(
    "files.rename",
    "Rename files",
    category="Files",
    kind="command",
    params=[
        *_file_params(),
        P.Column("new_name", "Column with the new names", help="Only the name, e.g. orders_done.csv: the file stays in its folder"),
        P.Choice("existing", "If a file of that name is there already", _EXISTING),
    ],
    summary=lambda p: f"Rename the files in {p.column or '?'} to {p.new_name or '?'}",
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
    targets = _Targets([source for source, _ in renames.values()], moves=True, done="renamed to", column=p.column)
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
    done = 0
    for source, destination, replaces in plan:
        ctx.check_cancelled()
        try:
            files.move_file(source, destination, replace=replaces)
        except ActionError as error:
            raise _failed(error, done, "renamed") from None
        done += 1
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
    summary=lambda p: f"Delete the files in {p.column or '?'}" + (" for good" if p.how == "permanent" else ""),
)
def delete_files(df, p, ctx):
    """Deletes the files listed in a column of the table: to the Recycle Bin, or for good.
    Runs only when the project is run; the table passes through unchanged."""
    chosen, skipped = [], []
    for source, _ in _listed_files(df, p, ctx):  # all files are checked before the first one is deleted
        ctx.check_cancelled()
        if _exists(source, p, skipped):
            if p.how == "permanent" and not os.access(source, os.W_OK):  # on Windows: the read-only attribute
                raise ActionError(f"{source} is read-only, so it can't be deleted")
            chosen.append(source)
    done = 0
    for source in chosen:
        ctx.check_cancelled()
        try:
            files.delete_file(source, recycle=p.how != "permanent")
        except ActionError as error:
            raise _failed(error, done, "deleted" if p.how == "permanent" else "moved to the Recycle Bin") from None
        done += 1
    _report(ctx, "Deleted" if p.how == "permanent" else "Moved to the Recycle Bin:", done, skipped)
    return df


@action(
    "files.mkdir",
    "Create folder",
    category="Files",
    kind="command",
    params=[P.Folder("folder", "Folder", help="Created with all the folders above it; nothing happens if it exists")],
    summary=lambda p: f"Create folder {Path(str(p.folder)).name or p.folder or '?'}",
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

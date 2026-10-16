"""Files and folders: a table of the files in a folder, and commands that copy, move, rename
or delete the files listed in a table, or create a folder."""

from __future__ import annotations

import datetime as dt
import os
import shutil
from pathlib import Path

import polars as pl
import send2trash

from tafl.api import ActionError, P, action, find_files


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


def _listed_files(df: pl.DataFrame, p, ctx) -> list[tuple[Path, Path | None]]:
    """(file, path below the base folder or None) for each file named in the column, once each."""
    if p.column not in df.columns:
        raise ActionError(f"There is no column {p.column}")
    base = _base(p, ctx)
    files: dict[Path, Path | None] = {}
    for value in df[p.column].cast(pl.String).to_list():
        if value is not None and value.strip():
            files.setdefault(*_resolve(value, base))
    return list(files.items())


def _exists(path: Path, p, skipped: list[Path]) -> bool:
    if path.is_file():
        return True
    if p.missing == "skip":
        skipped.append(path)
        return False
    raise ActionError(f"File not found: {path}")


def _destination(target: Path, existing: str) -> Path | None:
    """Where a file goes when ``target`` may exist already; None = skip it."""
    if not target.exists() or existing == "replace":
        return target
    if existing == "skip":
        return None
    if existing == "error":
        raise ActionError(f"{target} exists already")
    number = 2
    while (candidate := target.with_name(f"{target.stem} ({number}){target.suffix}")).exists():
        number += 1
    return candidate


def _report(ctx, verb: str, done: int, skipped: list[Path], where: str = "") -> None:
    text = f"{verb} {done:,} file{'s' if done != 1 else ''}{where}"
    if skipped:
        text += f" ({len(skipped):,} skipped)"
    ctx.log(text)


def _copy_or_move(df, p, ctx, move: bool) -> pl.DataFrame:
    target = ctx.resolve_path(p.target)
    done, skipped = 0, []
    for source, relative in _listed_files(df, p, ctx):
        ctx.check_cancelled()
        if not _exists(source, p, skipped):
            continue
        wanted = target / relative if p.subfolders and relative is not None else target / source.name
        if wanted.resolve() == source.resolve():
            skipped.append(source)
            continue
        destination = _destination(wanted, p.existing)
        if destination is None:
            skipped.append(source)
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            if move:
                if destination.exists():
                    destination.unlink()  # "replace": os.replace can't cross drives, shutil.move can
                shutil.move(source, destination)
            else:
                shutil.copy2(source, destination)  # keeps the modification time
        except OSError as e:
            raise ActionError(f"Could not {'move' if move else 'copy'} {source.name}: {e.strerror or e}") from None
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
    done, skipped, seen = 0, [], set()
    for value, new in df.select(pl.col(p.column).cast(pl.String), pl.col(p.new_name).cast(pl.String)).rows():
        if value is None or not value.strip():
            continue
        source, _ = _resolve(value, base)
        if source in seen:  # each file once
            continue
        seen.add(source)
        ctx.check_cancelled()
        if not new or not new.strip():
            skipped.append(source)
            continue
        if any(sep in new for sep in ("\\", "/")):
            raise ActionError(f"The new name {new} is a path: only a name is allowed (the file stays in its folder)")
        if not _exists(source, p, skipped):
            continue
        destination = _destination(source.with_name(new.strip()), p.existing)
        if destination is None or destination == source:
            skipped.append(source)
            continue
        try:
            os.replace(source, destination)
        except OSError as e:
            raise ActionError(f"Could not rename {source.name}: {e.strerror or e}") from None
        done += 1
    _report(ctx, "Renamed", done, skipped)
    return df


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
    done, skipped = 0, []
    for source, _ in _listed_files(df, p, ctx):
        ctx.check_cancelled()
        if not _exists(source, p, skipped):
            continue
        try:
            if p.how == "permanent":
                source.unlink()
            else:
                send2trash.send2trash(source)
        except (OSError, send2trash.TrashPermissionError) as e:
            raise ActionError(f"Could not delete {source.name}: {e}") from None
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
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ActionError(f"Could not create {folder}: {e.strerror or e}") from None
    ctx.log(f"Folder {folder} is there")
    return df

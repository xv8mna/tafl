"""The context an action runs in."""

from __future__ import annotations

import os
import re
import threading
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING

from .errors import ActionError, Cancelled

if TYPE_CHECKING:
    import polars as pl

    from .database import Database


class Mode(StrEnum):
    PREVIEW = "preview"  # interactive editing: exports and commands are skipped
    RUN = "run"  # a real run: everything executes and sources are re-read


class Context:
    """Passed to every action run. The engine supplies a subclass wired to the project.

    ``imported_files``: the files that the first step of the table, an import of all files in a
    folder, read (all of them, also files that gave no rows), and ``imported_folder`` that
    folder. ``source_files`` notes them in the import's context, and the engine gives them to
    the later steps of the same table, also when the import's result comes from the cache.
    Both are None in a table that doesn't start with such an import (e.g. one that starts from
    another table)."""

    def __init__(
        self,
        *,
        mode: Mode | str = Mode.PREVIEW,
        project_dir: str | Path | None = None,
        cancel: threading.Event | None = None,
    ) -> None:
        self.mode = Mode(mode)
        self.project_dir = Path(project_dir) if project_dir else None
        self.messages: list[tuple[str, str]] = []
        self.imported_files: tuple[Path, ...] | None = None
        self.imported_folder: Path | None = None
        self._cancel = cancel

    def table(self, table_id: str) -> pl.DataFrame:
        """The result of another table. Only tables chosen in this step's parameters."""
        raise ActionError(f"Table '{table_id}' is not available here")

    def table_name(self, table_id: str) -> str:
        return table_id

    def database(self, connection: str, *, create: bool = False) -> Database:
        """Open a connection defined in the project. Use the result as a context manager.

        ``create=True`` lets file databases (SQLite) be created if missing.
        """
        raise ActionError(f"Connection '{connection}' is not available here")

    def resolve_path(self, path: str | Path) -> Path:
        """Expand ``%VARS%`` and ``~``; relative paths are relative to the project file. On
        Windows a drive without a ``\\`` after it (``C:data.csv`` is in the current folder of
        drive C) and a ':' after the drive (``report 08:30.csv`` would write into a hidden part
        of the file ``report 08``) are an ActionError."""
        text = str(path).strip()
        if not text:
            raise ActionError("No file or folder specified")
        resolved = Path(os.path.expandvars(os.path.expanduser(text)))
        if os.name == "nt":
            drive, rest = os.path.splitdrive(str(resolved))
            if re.fullmatch(r"[A-Za-z]:", drive) and not resolved.root:
                where = "" if text == drive else f" (in {text})"
                raise ActionError(f"'{drive}' needs a \\ after it: {drive}\\ is the top folder of drive {drive[0].upper()}{where}")
            if ":" in rest or (drive.endswith(":") and not resolved.root):  # 8:30.csv: "8:" would be a drive
                raise ActionError(
                    f"':' can't be part of a file or folder name: {text}. For a time in a name, write it without ':', "
                    "e.g. STRFTIME(NOW(), '%H-%M')"
                )
        if not resolved.is_absolute() and self.project_dir is not None:
            resolved = self.project_dir / resolved
        return resolved

    def log(self, message: str) -> None:
        self.messages.append(("info", message))

    def warn(self, message: str) -> None:
        self.messages.append(("warning", message))

    def check_cancelled(self) -> None:
        """Call this inside long loops so the user can cancel."""
        if self._cancel is not None and self._cancel.is_set():
            raise Cancelled()

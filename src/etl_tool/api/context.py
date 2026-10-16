"""The context an action runs in."""

from __future__ import annotations

import os
import threading
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ActionError, Cancelled

if TYPE_CHECKING:
    import polars as pl

    from .database import Database


class Mode(StrEnum):
    PREVIEW = "preview"  # interactive editing: exports and commands are skipped
    RUN = "run"  # a real run: everything executes and sources are re-read


class Context:
    """Passed to every action run. The engine supplies a subclass wired to the project."""

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
        self._cancel = cancel
        # The project's parameters (name -> value). Their {Name} in the step's settings is filled
        # in before the action runs; actions only need this for values they build themselves.
        self.parameters: dict[str, Any] = {}

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
        """Expand ``%VARS%`` and ``~``; relative paths are relative to the project file."""
        text = str(path).strip()
        if not text:
            raise ActionError("No file or folder specified")
        resolved = Path(os.path.expandvars(os.path.expanduser(text)))
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

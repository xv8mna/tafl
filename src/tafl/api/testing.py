"""Helpers for testing plugins without a project.

    from tafl.api.testing import StubContext

    ctx = StubContext(tables={"other": other_df})
    result = my_action(df, {"other": "other", ...}, ctx)
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import polars as pl

from .context import Context, Mode
from .database import Connector, Database
from .errors import ActionError


class StubContext(Context):
    """A context with given tables (by id) and connections (name -> (connector, settings))."""

    def __init__(
        self,
        tables: Mapping[str, pl.DataFrame] | None = None,
        connections: Mapping[str, tuple[Connector, Mapping[str, Any]]] | None = None,
        *,
        mode: Mode | str = Mode.PREVIEW,
        project_dir: str | Path | None = None,
    ) -> None:
        super().__init__(mode=mode, project_dir=project_dir)
        self.tables = dict(tables or {})
        self.connections = dict(connections or {})

    def table(self, table_id: str) -> pl.DataFrame:
        if table_id not in self.tables:
            raise ActionError(f"Table '{table_id}' is not available here")
        return self.tables[table_id]

    def database(self, connection: str, *, create: bool = False) -> Database:
        if connection not in self.connections:
            raise ActionError(f"Connection '{connection}' is not available here")
        connector, settings = self.connections[connection]
        return connector.open(settings, self, create=create)

    @property
    def warnings(self) -> list[str]:
        return [text for level, text in self.messages if level == "warning"]

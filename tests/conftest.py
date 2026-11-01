from __future__ import annotations

import sqlite3
import struct
from contextlib import closing

import polars as pl
import pytest

from tafl.api import Action
from tafl.api.testing import StubContext
from tafl.core import Registry


@pytest.fixture(scope="session")
def registry() -> Registry:
    """Built-in plugins only (not the user's plugin folders)."""
    registry = Registry()
    registry.load_builtin()
    assert not registry.errors, registry.errors
    return registry


@pytest.fixture
def act(registry):
    """Run a built-in action directly: act("rows.sort", df, {...}, ctx=...)."""

    def run(action_id: str, df: pl.DataFrame | None = None, params: dict | None = None, ctx: StubContext | None = None):
        action: Action = registry.actions[action_id]
        return action(df, params or {}, ctx or StubContext())

    return run


@pytest.fixture
def leave_journal():
    """A rollback journal beside a SQLite database, as if a program had stopped during a change.
    It has nothing to restore and names ``super_journal``: a connection that recovers the database
    deletes that file."""

    def leave(database, super_journal) -> None:
        with closing(sqlite3.connect(database)) as connection:
            pages = connection.execute("PRAGMA page_count").fetchone()[0]
            page_size = connection.execute("PRAGMA page_size").fetchone()[0]
        magic = bytes.fromhex("d9d505f920a163d7")
        sector = 512
        header = (magic + struct.pack(">5I", 0, 0x1234ABCD, pages, sector, page_size)).ljust(sector, b"\0")
        name = str(super_journal).encode()
        tail = name + struct.pack(">II", len(name), sum(name)) + magic
        (database.parent / f"{database.name}-journal").write_bytes(header + tail)

    return leave


@pytest.fixture
def people() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "Name": ["Anna", "ben", "Carla", None, "Dieter"],
            "City": ["Berlin", "Hamburg", "Berlin", "Köln", None],
            "Age": [34, 27, 41, 19, 58],
            "Score": [1.5, None, 3.25, 2.0, 0.5],
        }
    )

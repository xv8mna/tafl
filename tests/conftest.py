from __future__ import annotations

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
def people() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "Name": ["Anna", "ben", "Carla", None, "Dieter"],
            "City": ["Berlin", "Hamburg", "Berlin", "Köln", None],
            "Age": [34, 27, 41, 19, 58],
            "Score": [1.5, None, 3.25, 2.0, 0.5],
        }
    )

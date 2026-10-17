"""Exceptions shared by the engine and plugins."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import polars as pl


class TaflError(Exception):
    """Base class for errors whose message is meant for the user."""


class ActionError(TaflError):
    """A problem the user can fix: bad settings, a missing column, an unreadable file.

    Actions raise this with a plain-language message; it is shown on the step without a traceback.
    """


class Cancelled(TaflError):
    """The user cancelled the calculation."""

    def __init__(self, message: str = "Cancelled") -> None:
        super().__init__(message)


class SkipRest(TaflError):
    """Ends the table at this step without an error, e.g. when there is nothing to do.

    The step is OK and shows the message; the steps after it are skipped with the message. The
    table's result is ``df`` if given, else the table that went into the step (for an import: an
    empty table). Tables that use this table get that result.
    """

    def __init__(self, message: str, df: pl.DataFrame | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.df = df

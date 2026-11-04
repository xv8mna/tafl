"""Columns by their exact names.

Polars reads some column names as patterns: exactly ``*`` is every column, and a name that
starts with ``^`` and ends with ``$`` is a regular expression (``pl.col("^a.*$")`` is the column
``abc``). It does so in ``pl.col`` and wherever a method takes column names as text: ``select``,
``drop``, ``group_by``, ``sort``, ``unique``, ``join``, ``unpivot``, ``explode``, ``over``,
``pl.struct``, ``pl.coalesce``, ... Column names come from the data (a header ``*`` in a CSV
file, a value ``*`` that Pivot makes a column name) and from settings, so take the columns with
``col`` and ``cols`` and give those to such methods instead of the names::

    df.with_columns(col(p.column).str.to_uppercase())
    df.select(cols(p.columns))

``selector`` is for a method that needs one selector of several columns: ``unpivot(on=...)``,
and ``drop``, which nests a list of expressions: a few thousand of them overflow the stack, and
the process ends.

Polars reads names as patterns even then in ``DataFrame.cast({name: type})``: write
``with_columns(col(name).cast(type))``. The methods of a Series find the Series by its name too:
many of them stop with an IndexError for a Series whose name starts with ``^`` and ends with
``$`` (they change no value).
"""

from __future__ import annotations

from collections.abc import Iterable

import polars as pl
import polars.selectors as cs


def col(name: str) -> pl.Expr:
    """The column ``name`` by its exact name, as an expression: ``pl.col(name)`` reads ``*`` and
    ``^...$`` as patterns."""
    return cs.by_name(name).as_expr()


def cols(names: Iterable[str]) -> list[pl.Expr]:
    """The columns ``names``, each by its exact name, in this order. A name given twice is there
    twice, as with names as text: a ``select`` of them stops with a DuplicateError."""
    return [col(name) for name in names]


def selector(names: Iterable[str]) -> cs.Selector:
    """The columns ``names`` as one selector, by their exact names, in this order, for a method
    that needs a selector: ``unpivot(on=...)`` keeps the order of a selector, but takes a list of
    expressions in the order of the table. ``drop``, ``unpivot`` and ``pivot(index=...)`` nest a
    list of expressions, and a few thousand of them overflow the stack: give them one selector.
    A name given twice counts once, and ``over`` refuses a selector of several columns: give it
    ``cols``."""
    return cs.by_name(*names)

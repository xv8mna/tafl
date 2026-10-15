"""SQL-style formulas, evaluated with DuckDB.

Formulas use DuckDB's SQL expression syntax plus conveniences for SQL Server users:

* ``[Column Name]`` refers to a column (like T-SQL); standard ``"Column Name"`` works too.
* T-SQL helpers: ``IIF(cond, a, b)``, ``ISNULL(a, b)``, ``LEN(text)``, ``GETDATE()``,
  ``CHARINDEX(find, text)``.
* Text is joined with ``||`` or ``CONCAT(a, b, ...)`` (``+`` only adds numbers).
* Window functions work: ``SUM([Amount]) OVER (PARTITION BY [Customer])``.

Examples: ``[Price] * [Quantity]``, ``UPPER(TRIM([Name]))``,
``CASE WHEN [Amount] > 1000 THEN 'big' ELSE 'small' END``, ``[Date] + INTERVAL 30 DAY``.
"""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence

import duckdb
import polars as pl

from .dtypes import normalize
from .errors import ActionError

_MACROS = (
    "CREATE MACRO iif(c, a, b) AS CASE WHEN c THEN a ELSE b END",
    "CREATE MACRO len(s) AS length(rtrim(CAST(s AS VARCHAR)))",
    "CREATE MACRO getdate() AS current_localtimestamp()",
    "CREATE MACRO charindex(find, s) AS instr(s, find)",
)
# Reserved words in DuckDB that cannot be macros; rewritten when called as functions.
_REWRITES = {"isnull": "coalesce"}

_VIEW = "__input"
_ROW = "__etl_row__"
_local = threading.local()


def translate(formula: str) -> str:
    """Turn a user formula into DuckDB SQL: ``[Col]`` -> ``"Col"``, ``ISNULL(`` -> ``coalesce(``.

    Text in single quotes and identifiers in double quotes are left untouched.
    """
    out: list[str] = []
    i, n = 0, len(formula)
    while i < n:
        ch = formula[i]
        if ch in "'\"":
            end = _closing_quote(formula, i, ch)
            out.append(formula[i : end + 1])
            i = end + 1
        elif ch == "[":
            name, i = _bracket_name(formula, i)
            out.append('"' + name.replace('"', '""') + '"')
        elif ch.isalpha() or ch == "_":
            j = i
            while j < n and (formula[j].isalnum() or formula[j] == "_"):
                j += 1
            word = formula[i:j]
            rest = formula[j:].lstrip()
            out.append(_REWRITES.get(word.lower(), word) if rest.startswith("(") else word)
            i = j
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def evaluate(df: pl.DataFrame, formula: str) -> pl.Series:
    """Evaluate a formula for every row; returns one value per row, in row order."""
    if not formula or not formula.strip():
        raise ActionError("The formula is empty")
    sql = f"SELECT ({translate(formula)}) AS value FROM {_VIEW} ORDER BY {_ROW}"
    con = _connection()
    con.register(_VIEW, df.with_row_index(_ROW))
    try:
        result = con.sql(sql).pl()
    except duckdb.Error as e:
        raise ActionError(_clean_error(e, formula)) from None
    finally:
        con.unregister(_VIEW)
    if result.height != df.height:
        raise ActionError(
            "The formula must give one value per row. For totals use the Aggregate action, "
            "or a window function such as SUM([Amount]) OVER ()."
        )
    return normalize(result.to_series(0))


def add_columns(df: pl.DataFrame, columns: Sequence[tuple[str, str]]) -> pl.DataFrame:
    """Add columns calculated from formulas; a column that exists is replaced in place.
    Later formulas can use the columns calculated before them."""
    for name, formula in columns:
        if not name.strip():
            raise ActionError("A new column needs a name")
        df = df.with_columns(evaluate(df, formula).alias(name))
    return df


def filter_rows(df: pl.DataFrame, condition: str, keep: bool = True) -> pl.DataFrame:
    """Keep (or remove) the rows where ``condition`` is true. Empty results count as false."""
    mask = evaluate(df, condition)
    if mask.dtype != pl.Boolean:
        raise ActionError(
            f"The condition must be true or false for every row, e.g. [Amount] > 100 "
            f"(this formula gives {mask.dtype})"
        )
    mask = mask.fill_null(False)
    return df.filter(mask if keep else ~mask)


def _connection() -> duckdb.DuckDBPyConnection:
    con = getattr(_local, "con", None)
    if con is None:
        con = duckdb.connect(":memory:")
        for macro in _MACROS:
            con.execute(macro)
        _local.con = con
    return con


def _closing_quote(text: str, start: int, quote: str) -> int:
    i = start + 1
    while i < len(text):
        if text[i] == quote:
            if i + 1 < len(text) and text[i + 1] == quote:  # doubled quote = escaped
                i += 2
                continue
            return i
        i += 1
    raise ActionError(f"Missing closing {quote} in formula")


def _bracket_name(text: str, start: int) -> tuple[str, int]:
    """Read ``[name]`` starting at ``start``; ``]]`` inside stands for ``]``."""
    name: list[str] = []
    i = start + 1
    while i < len(text):
        if text[i] == "]":
            if i + 1 < len(text) and text[i + 1] == "]":
                name.append("]")
                i += 2
                continue
            return "".join(name), i + 1
        name.append(text[i])
        i += 1
    raise ActionError("Missing ] in formula")


def _clean_error(error: Exception, formula: str) -> str:
    text = str(error)
    if _ROW in text and "GROUP BY" in text:  # an aggregate such as SUM([x]) without OVER (...)
        return (
            "Formula error: the formula must give one value per row. For totals use the Aggregate "
            f"action, or a window function such as SUM([Amount]) OVER ().\nFormula: {formula}"
        )
    text = re.split(r"\n\s*\nLINE \d+:|\nLINE \d+:", text)[0]
    text = text.replace(f'"{_VIEW}".', "").replace(f"{_VIEW}.", "")
    text = re.sub(r"\s+", " ", text).strip()
    missing = re.search(r'Referenced column "(.+?)" not found in FROM clause!(?: Candidate bindings: (.*))?', text)
    if missing:  # the most common mistake: say it plainly, without DuckDB's internals
        candidates = [c for c in re.findall(r'"([^"]+)"', missing.group(2) or "") if c != _ROW]
        hint = f" (did you mean [{candidates[0]}]?)" if candidates else ""
        return f"Formula error: column [{missing.group(1)}] not found{hint}\nFormula: {formula}"
    if "+(" in text and "VARCHAR" in text:
        text += " — to join text use || or CONCAT()"
    return f"Formula error: {text}\nFormula: {formula}"

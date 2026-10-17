"""Rows: filter, sort, remove duplicates, fill empty values."""

from __future__ import annotations

import datetime as dt
import re
from typing import Any

import polars as pl

from tafl.api import ActionError, P, action, dtypes, expressions

_OPERATORS = [
    ("=", "equals"),
    ("!=", "does not equal"),
    (">", "is greater than"),
    (">=", "is greater than or equal to"),
    ("<", "is less than"),
    ("<=", "is less than or equal to"),
    ("contains", "contains"),
    ("not_contains", "does not contain"),
    ("starts", "starts with"),
    ("ends", "ends with"),
    ("in", "is one of (separate with ;)"),
    ("not_in", "is none of (separate with ;)"),
    ("empty", "is empty"),
    ("not_empty", "is not empty"),
]
_LABELS = dict(_OPERATORS)
_KEEP = [("keep", "Keep matching rows"), ("remove", "Remove matching rows")]


@action(
    "rows.filter",
    "Filter rows",
    category="Rows",
    params=[
        P.List(
            "conditions",
            "Conditions",
            fields=[
                P.Column("column", "Column"),
                P.Choice("operator", "Operator", _OPERATORS),
                P.Text("value", "Value", visible_if={"operator": tuple(o for o, _ in _OPERATORS if "empty" not in o)}),
            ],
            min_items=1,
        ),
        P.Choice("match", "Rows must match", [("all", "all conditions (AND)"), ("any", "any condition (OR)")]),
        P.Choice("keep", "Then", _KEEP),
        P.Bool("case_sensitive", "Upper/lower case matters", default=False),
    ],
    summary=lambda p: ("Keep" if p.keep == "keep" else "Remove")
    + " rows where "
    + (" and " if p.match == "all" else " or ").join(
        f"{c.column} {_LABELS.get(c.operator, c.operator)}" + ("" if "empty" in c.operator else f" {c.value}")
        for c in p.conditions
    ),
)
def filter_rows(df, p, ctx):
    """Keeps (or removes) the rows that match the conditions.

    Values are compared with the column's type: numbers as numbers (1,5 or 1.5), dates as dates
    (31.12.2025, 2025-12-31 or "today"). Empty cells never equal a value, but they count as
    "does not equal" and "does not contain".
    """
    mask_parts = [_condition(df, c, p.case_sensitive) for c in p.conditions]
    mask = pl.all_horizontal(mask_parts) if p.match == "all" else pl.any_horizontal(mask_parts)
    mask = mask.fill_null(False)
    return df.filter(mask if p.keep == "keep" else ~mask)


@action(
    "rows.filter_formula",
    "Filter rows by formula",
    category="Rows",
    params=[
        P.Expression("condition", "Condition", condition=True, required=True, placeholder="[Amount] > 100 AND [Country] = 'DE'"),
        P.Choice("keep", "Then", _KEEP),
    ],
    summary=lambda p: ("Keep" if p.keep == "keep" else "Remove") + f" rows where {p.condition}",
)
def filter_formula(df, p, ctx):
    """Keeps (or removes) the rows for which a SQL-style condition is true, e.g.
    [Amount] > 100 AND [Country] IN ('DE', 'AT') or [Date] >= DATE '2025-01-01'."""
    return expressions.filter_rows(df, p.condition, keep=p.keep == "keep")


@action(
    "rows.sort",
    "Sort",
    category="Rows",
    params=[
        P.List(
            "by",
            "Sort by",
            fields=[
                P.Column("column", "Column"),
                P.Choice("order", "Order", [("asc", "Ascending (A→Z, 1→9)"), ("desc", "Descending (Z→A, 9→1)")]),
            ],
            min_items=1,
        ),
        P.Bool("empty_last", "Empty values last", default=True),
    ],
    summary=lambda p: "Sort by " + ", ".join(b.column + (" ↓" if b.order == "desc" else "") for b in p.by),
)
def sort(df, p, ctx):
    """Sorts the rows. Text is sorted like in Excel: upper and lower case count the same, letters
    with accents sort with their base letter (ä with a), ß like ss. Rows with equal values keep
    their order."""
    text = {name for name, dtype in df.schema.items() if dtype == pl.String}
    return df.sort(
        [dtypes.text_sort_key(df[b.column]) if b.column in text else pl.col(b.column) for b in p.by],
        descending=[b.order == "desc" for b in p.by],
        nulls_last=p.empty_last,
        maintain_order=True,
    )


@action(
    "rows.dedupe",
    "Remove duplicates",
    category="Rows",
    params=[
        P.Columns("columns", "Compare these columns (empty = all)"),
        P.Choice(
            "keep",
            "Of rows that are equal, keep",
            [("first", "the first"), ("last", "the last"), ("none", "none (remove all of them)")],
        ),
    ],
    summary=lambda p: "Remove duplicates" + (f" of {', '.join(p.columns)}" if p.columns else ""),
)
def dedupe(df, p, ctx):
    """Removes rows that are equal in the compared columns. The row order is kept."""
    return df.unique(subset=p.columns or None, keep=p.keep, maintain_order=True)


@action(
    "rows.fill",
    "Fill empty values",
    category="Rows",
    params=[
        P.Columns("columns", "Columns", required=True),
        P.Choice(
            "method",
            "Fill with",
            [("down", "the value above (fill down)"), ("up", "the value below (fill up)"), ("value", "a fixed value")],
        ),
        P.Text("value", "Value", visible_if={"method": "value"}),
    ],
    summary=lambda p: {"down": "Fill down", "up": "Fill up"}.get(p.method, f"Fill with '{p.value}'")
    + ": "
    + ", ".join(p.columns),
)
def fill(df, p, ctx):
    """Fills empty cells: with the value above (typical for Excel lists where a group name is
    only written once), the value below, or a fixed value. Empty text counts as empty."""
    updates = []
    for name in p.columns:
        column = pl.col(name)
        if df.schema[name] == pl.String:
            column = pl.when(column.str.strip_chars() == "").then(None).otherwise(column)
        if p.method == "value":
            updates.append(column.fill_null(_literal(p.value, df.schema[name], name)).alias(name))
        else:
            updates.append(column.fill_null(strategy="forward" if p.method == "down" else "backward").alias(name))
    return df.with_columns(updates)


def _condition(df: pl.DataFrame, c: Any, case_sensitive: bool) -> pl.Expr:
    column = pl.col(c.column)
    dtype = df.schema[c.column]
    text_column = dtype == pl.String
    op = c.operator

    if op in ("empty", "not_empty"):
        empty = column.is_null()
        if text_column:
            empty = empty | (column.str.strip_chars() == "")
        return empty if op == "empty" else ~empty

    if op in ("contains", "not_contains", "starts", "ends"):
        text, needle = column.cast(pl.String), c.value
        if not case_sensitive:
            text, needle = text.str.to_lowercase(), needle.lower()
        if op == "starts":
            return text.str.starts_with(needle)
        if op == "ends":
            return text.str.ends_with(needle)
        found = text.str.contains(needle, literal=True)
        return found if op == "contains" else (~found).fill_null(True)

    if text_column and not case_sensitive:
        column = column.str.to_lowercase()

    if op in ("in", "not_in"):
        items = [item.strip() for item in re.split(r"[;\n]", c.value) if item.strip()]
        values = [i.lower() if text_column and not case_sensitive else _literal(i, dtype, c.column) for i in items]
        found = column.is_in(values) if values else pl.lit(False)
        return found if op == "in" else (~found).fill_null(True)

    if text_column:
        value: Any = c.value if case_sensitive else c.value.lower()
    else:
        value = _literal(c.value, dtype, c.column)
    compared = {
        "=": column == value,
        "!=": column != value,
        ">": column > value,
        ">=": column >= value,
        "<": column < value,
        "<=": column <= value,
    }[op]
    return compared.fill_null(True) if op == "!=" else compared


def _literal(text: str, dtype: pl.DataType, column: str) -> Any:
    """A value typed by the user, converted to the column's type."""
    value = text.strip()
    if dtype == pl.String:
        return text
    if dtype.is_numeric():
        number = dtypes.parse_numbers(pl.Series([value]), _decimal_of(value))[0]
        if number is None:
            raise ActionError(f"'{text}' is not a number (column '{column}')")
        if isinstance(dtype, pl.Decimal):  # exact decimals: all digits
            exact = dtypes.parse_decimal(value, _decimal_of(value))
            return number if exact is None else exact
        if dtype.is_integer() and number.is_integer():  # all digits, also beyond 15
            whole = dtypes.parse_integers(pl.Series([value]), _decimal_of(value))[0]
            return number if whole is None else whole
        return number
    if dtype == pl.Date or dtype == pl.Datetime:
        if value.lower() in ("today", "heute"):
            day = dt.date.today()
            return day if dtype == pl.Date else dt.datetime.combine(day, dt.time())
        if value.lower() in ("now", "jetzt") and dtype == pl.Datetime:
            return dt.datetime.now()
        if dtype == pl.Date:
            parsed = dtypes.parse_dates(pl.Series([value]))[0]
            if parsed is None:
                raise ActionError(f"'{text}' is not a date (column '{column}'); use e.g. 31.12.2025 or 2025-12-31")
            return parsed
        parsed = dtypes.parse_datetimes(pl.Series([value]))[0]
        if parsed is None:
            raise ActionError(
                f"'{text}' is not a date-time (column '{column}'); use e.g. 31.12.2025 08:30 or 2025-12-31 08:30"
            )
        return parsed
    if dtype == pl.Boolean:
        parsed = dtypes.parse_booleans(pl.Series([value]))[0]
        if parsed is None:
            raise ActionError(f"'{text}' is not true/false (column '{column}')")
        return parsed
    return text


def _decimal_of(value: str) -> str:
    """Which separator is the decimal one in a number someone typed: the last of , and ."""
    comma, point = value.rfind(","), value.rfind(".")
    if comma >= 0 and point >= 0:
        return "," if comma > point else "."
    if comma >= 0:
        return ","
    if point >= 0 and re.fullmatch(r"[+-]?[0-9]{1,3}(\.[0-9]{3})+", value):
        return ","  # 1.234 / 1.234.567 = thousands
    return "."

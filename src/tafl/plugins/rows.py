"""Rows: filter, sort, remove duplicates, fill empty values."""

from __future__ import annotations

import datetime as dt
import operator
import re
from decimal import ROUND_FLOOR, Context, Decimal
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
_COMPARE = {
    "=": operator.eq,
    "!=": operator.ne,
    ">": operator.gt,
    ">=": operator.ge,
    "<": operator.lt,
    "<=": operator.le,
}
_DECIMAL_DIGITS = 38  # an exact decimal (Decimal) holds 38 digits, decimal places included
_CONTEXT = Context(prec=2 * _DECIMAL_DIGITS)  # room for all of them (the default context keeps 28)


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
    (31.12.2025, 2025-12-31 or "today"), times as times (08:30 or 08:30:15). Empty cells never
    equal a value, but they count as "does not equal" and "does not contain".
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
            value = _literal(p.value, df.schema[name], name)
            if isinstance(value, Decimal) and not _fits_decimal(value, df.schema[name].scale):
                raise ActionError(
                    f"'{p.value}' has too many digits for column '{name}': "
                    f"an exact decimal holds {_DECIMAL_DIGITS}, decimal places included"
                )
            if isinstance(value, Decimal) and (example := _too_long_with(df[name], value)) is not None:
                raise ActionError(
                    f"'{p.value}' has too many decimal places for the values of column '{name}' (e.g. {example}): "
                    f"an exact decimal holds {_DECIMAL_DIGITS} digits, decimal places included"
                )
            if isinstance(value, int) and (too_big := _too_big_with(df[name], value)) is not None:
                raise ActionError(
                    f"'{p.value}' doesn't fit column '{name}': with numbers below 0 the column holds whole numbers "
                    f"up to {too_big[1]}, but it has bigger ones (e.g. {too_big[0]})"
                )
            updates.append(column.fill_null(_for_polars(value, df.schema[name])).alias(name))
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
        if dtype == pl.Duration:
            raise ActionError(_no_typed_value(c.column))
        text, needle = column.cast(pl.String), c.value
        if dtype == pl.Binary and not dtypes.bytes_are_text(df[c.column]):  # as hex digits, as Join texts writes them
            text = dtypes.text_expr(column, dtype)
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
        if isinstance(dtype, pl.Decimal):  # values the column can't hold aren't in it
            values = [v for v in (_on_scale(v, dtype.scale) for v in values) if v is not None]
        elif dtype.is_integer():  # the same for whole numbers: not 1,5, and 300 not for an Int8
            low, high = _limits(dtype)
            values = [v for v in values if isinstance(v, int) and low <= v <= high]
        listed = pl.Series(values, dtype=dtype).implode() if dtype.is_integer() else values
        found = column.is_in(listed) if values else pl.lit(False)
        return found if op == "in" else (~found).fill_null(True)

    if text_column:
        value: Any = c.value if case_sensitive else c.value.lower()
    else:
        value = _literal(c.value, dtype, c.column)
        if dtype.is_integer() and isinstance(value, float):  # no whole number the column holds: with all its digits
            exact = dtypes.parse_decimal(c.value.strip(), _decimal_of(c.value.strip()))
            if exact is not None:
                compared = _whole_comparison(column, op, exact, dtype)
                return compared.fill_null(True) if op == "!=" else compared
    if isinstance(value, Decimal):
        compared = _decimal_comparison(column, op, value, dtype.scale)
    else:
        compared = _COMPARE[op](column, _for_polars(value, dtype))
    return compared.fill_null(True) if op == "!=" else compared


def _for_polars(value: Any, dtype: pl.DataType) -> Any:
    """A typed value as Polars takes it: a Python number only below 2^127, so a higher one (only
    ``_whole`` gives those, for a UInt128 column) needs the column's type."""
    return pl.lit(value, dtype=dtype) if isinstance(value, int) and value >= 2**127 else value


def _decimal_comparison(column: pl.Expr, op: str, value: Decimal, scale: int) -> pl.Expr:
    """An exact decimal column with ``scale`` decimal places compared with a typed value, with
    all its digits, also when the value has more digits than the column can hold."""
    limit = Decimal(1).scaleb(_DECIMAL_DIGITS - scale)  # the column's values are smaller
    floor = value
    if value.copy_abs() < limit:
        floor = value.quantize(Decimal(1).scaleb(-scale), rounding=ROUND_FLOOR, context=_CONTEXT)
    if floor.copy_abs() >= limit:  # beyond all values of the column
        below = value > 0  # all of them are below it
        answer = {"=": False, "!=": True, ">": not below, ">=": not below, "<": below, "<=": below}[op]
        return pl.when(column.is_not_null()).then(pl.lit(answer))
    if floor != value:  # more decimal places than the column has: between two of its values
        if op in ("=", "!="):
            return pl.when(column.is_not_null()).then(pl.lit(op == "!="))
        op = ">" if op in (">", ">=") else "<="
    return _COMPARE[op](column, pl.lit(floor, dtype=pl.Decimal(_DECIMAL_DIGITS, scale)))


def _whole_comparison(column: pl.Expr, op: str, value: Decimal, dtype: pl.DataType) -> pl.Expr:
    """A whole-number column compared with a typed value that it can't hold (decimal places, or
    beyond its range), exactly: through the whole number below the value, in the column's type."""
    floor = int(value.to_integral_value(rounding=ROUND_FLOOR))
    if floor != value:  # between two whole numbers
        if op in ("=", "!="):
            return pl.when(column.is_not_null()).then(pl.lit(op == "!="))
        op = ">" if op in (">", ">=") else "<="
    low, high = _limits(dtype)
    if not low <= floor <= high:  # beyond all values of the column
        below = floor > high  # all of them are below it
        answer = {"=": False, "!=": True, ">": not below, ">=": not below, "<": below, "<=": below}[op]
        return pl.when(column.is_not_null()).then(pl.lit(answer))
    return _COMPARE[op](column, pl.lit(floor, dtype=dtype))


def _on_scale(value: Any, scale: int) -> Decimal | None:
    """A typed value with the decimal places of an exact decimal column, or None when no value of
    such a column equals it."""
    if not isinstance(value, Decimal) or value.copy_abs() >= Decimal(1).scaleb(_DECIMAL_DIGITS - scale):
        return None
    exact = value.quantize(Decimal(1).scaleb(-scale), context=_CONTEXT)
    return exact if exact == value else None


def _fits_decimal(value: Decimal, scale: int) -> bool:
    """Whether a column of exact decimals with ``scale`` decimal places can take ``value``, with
    more decimal places if it needs them."""
    places = max(scale, -value.as_tuple().exponent)
    return places <= _DECIMAL_DIGITS and value.copy_abs() < Decimal(1).scaleb(_DECIMAL_DIGITS - places)


def _too_long_with(values: pl.Series, value: Decimal) -> Decimal | None:
    """A value of the exact decimals ``values`` that doesn't fit when the column gets the decimal
    places of ``value`` too (123.45 with 37 decimal places has 40 digits), or None."""
    places = max(values.dtype.scale, -value.as_tuple().exponent)
    extremes = [v for v in (values.min(), values.max()) if v is not None]
    longest = max(extremes, key=abs, default=None)
    return longest if longest is not None and abs(longest) >= Decimal(1).scaleb(_DECIMAL_DIGITS - places) else None


def _too_big_with(values: pl.Series, value: int) -> tuple[int, int] | None:
    """The highest of the whole numbers ``values`` and the highest number the column can hold when
    it takes ``value`` too, if that is lower; else None. For a number below 0 Polars makes a UInt64
    column Int64 and a UInt128 column Int128."""
    if value >= 0 or values.dtype not in (pl.UInt64, pl.UInt128):
        return None
    high = _limits(pl.Int64 if values.dtype == pl.UInt64 else pl.Int128)[1]
    top = values.max()
    return (top, high) if top is not None and top > high else None


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
            whole = _whole(value, dtype)
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
    if dtype == pl.Time:
        parsed = dtypes.parse_times(pl.Series([value]))[0]
        if parsed is None:
            raise ActionError(f"'{text}' is not a time (column '{column}'); use e.g. 08:30 or 08:30:15")
        return parsed
    if dtype == pl.Boolean:
        parsed = dtypes.parse_booleans(pl.Series([value]))[0]
        if parsed is None:
            raise ActionError(f"'{text}' is not true/false (column '{column}')")
        return parsed
    if dtype == pl.Duration:
        raise ActionError(_no_typed_value(column))
    if dtype == pl.Binary:
        return pl.select(pl.lit(text).cast(pl.Binary)).item()
    return text


def _no_typed_value(column: str) -> str:
    return f"Column '{column}' holds time spans: a typed value can't be used with it"


def _whole(value: str, dtype: pl.DataType) -> int | None:
    """A typed whole number with all its digits. None when it has decimal places, or when neither
    an Int64 nor the column can hold it: it is then beyond all values of the column."""
    number = dtypes.parse_decimal(value, _decimal_of(value))
    if number is None or number != number.to_integral_value():
        return None
    if -(2**63) <= number < 2**63:
        return int(number)
    low, high = _limits(dtype)
    return int(number) if low <= number <= high else None


def _limits(dtype: pl.DataType) -> tuple[int, int]:
    """The lowest and the highest value of a whole-number column."""
    return pl.select(low=dtype.min(), high=dtype.max()).row(0)


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

"""Rows: filter, sort, remove duplicates, fill empty values."""

from __future__ import annotations

import datetime as dt
import functools
import operator
import re
from collections.abc import Callable
from decimal import ROUND_FLOOR, Context, Decimal
from typing import Any, NamedTuple

import polars as pl

from tafl.api import ActionError, P, action, col, cols, dtypes, expressions, parameters

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
_TEXT_OPERATORS = ("contains", "not_contains", "starts", "ends")  # they look at a value's text
_PART = 1 << 16  # the fewest decimal numbers that a part of _decimal_texts writes
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
# The moments that date-times in nanoseconds hold, in nanoseconds since 1970 (UTC): from
# 1677-09-21 00:12:43.145224192 to 2262-04-11 23:47:16.854775807
_NANOSECONDS = (-(2**63), 2**63 - 1)
_PLACES = {"ms": 3, "us": 6, "ns": 9}  # the decimal places of a second that a unit of date-times holds, coarsest first
_UNIT_NAMES = {"us": "microseconds", "ns": "nanoseconds"}
_SHOWN = (-62_135_596_800, 253_402_300_800)  # seconds since 1970 (UTC) of the years 1 to 9999, which Tafl writes


class _Moment(NamedTuple):
    """A typed date-time or time, exactly: ``ns`` nanoseconds since 1970-01-01 00:00 (in UTC in a
    column with a time zone) or since midnight (a time). ``finer``: it has digits below a
    nanosecond that aren't 0, so it is between ``ns`` and the next nanosecond, where no value of a
    column can be. (Polars leaves out the digits that a unit doesn't hold when it reads a text.)"""

    ns: int
    finer: bool = False


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
    schema = df.schema  # once: Polars makes it anew for every call, with all columns (seconds for thousands)
    typed: dict[tuple[Any, ...], Any] = {}  # each typed value read once for each type of column (see _typed)
    texts = _decimal_texts(df, schema, [c.column for c in p.conditions if c.operator in _TEXT_OPERATORS])
    warned: list[tuple[str, str]] = []  # (column, warning): 3 of them in full, the others in one line
    mask_parts = [
        _condition(df, c, p.case_sensitive, schema, lambda text, c=c: warned.append((c.column, text)), typed, texts)
        for c in p.conditions
    ]
    for _, text in warned[:3]:
        ctx.warn(text)
    if len(warned) > 3:
        more = len(warned) - 3
        named = ", ".join(f"'{column}'" for column, _ in warned[3:6]) + (", …" if more > 3 else "")
        ctx.warn(
            f"{more:,} more condition{'s compare' if more > 1 else ' compares'} texts character by character: {named}"
        )
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
        [dtypes.text_sort_key(df[b.column]) if b.column in text else col(b.column) for b in p.by],
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
    return df.unique(subset=cols(p.columns) or None, keep=p.keep, maintain_order=True)


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
    schema = df.schema  # once: Polars makes it anew for every call, with all columns (seconds for thousands)
    typed: dict[tuple[Any, ...], Any] = {}  # the fixed value read once for each type of column (see _typed)
    updates = []
    for name in p.columns:
        dtype = schema[name]
        column = col(name)
        if dtype == pl.String:
            column = pl.when(column.str.strip_chars() == "").then(None).otherwise(column)
        if p.method == "value":
            if (held := _held_values(dtype)) is not None:  # lists, records: Polars made them text, or stopped
                raise ActionError(_no_typed_value(name, held))
            value = _typed(p.value, dtype, name, typed)
            if isinstance(value, _Moment):  # in the column's unit, or in a finer one that holds it
                target, value = _moment_fill(df[name], p.value, value, name)
                updates.append((column if target == dtype else column.cast(target)).fill_null(value).alias(name))
                continue
            if isinstance(value, Decimal) and not _fits_decimal(value, dtype.scale):
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
            if (rounded := _rounded_with(df[name], value)) is not None:
                raise ActionError(
                    f"'{p.value}' doesn't fit column '{name}': with it the column holds decimal numbers, which keep "
                    f"about 15 digits, but it has longer whole numbers (e.g. {rounded} would become {int(float(rounded))})"
                )
            if (becomes := _rounded_typed(p.value, value, dtype)) is not None:
                raise ActionError(
                    f"'{p.value}' doesn't fit column '{name}': with it the column holds decimal numbers, which keep "
                    f"about 15 digits, so it would become {becomes}"
                )
            updates.append(column.fill_null(_for_polars(value, dtype)).alias(name))
        else:
            updates.append(column.fill_null(strategy="forward" if p.method == "down" else "backward").alias(name))
    return df.with_columns(updates)


def _condition(
    df: pl.DataFrame,
    c: Any,
    case_sensitive: bool,
    schema: pl.Schema | None = None,
    warn: Callable[[str], None] | None = None,
    typed: dict[tuple[Any, ...], Any] | None = None,
    texts: dict[str, pl.Series] | None = None,
) -> pl.Expr:
    column = col(c.column)
    dtype = (df.schema if schema is None else schema)[c.column]
    text_column = dtype == pl.String
    op = c.operator

    if op in ("empty", "not_empty"):
        empty = column.is_null()
        if text_column:
            empty = empty | (column.str.strip_chars() == "")
        return empty if op == "empty" else ~empty

    if _held_values(dtype) is not None:  # lists, records: only whether they are empty
        raise ActionError(_not_comparable(c.column, dtype))

    if op in _TEXT_OPERATORS:
        needle = c.value
        if dtype == pl.Binary and dtypes.bytes_are_text(df[c.column]):  # binary values that are all text: that text
            text = column.cast(pl.String)
        elif texts is not None and c.column in texts:  # decimal numbers: written once for all (see _decimal_texts)
            text = pl.lit(texts[c.column])
        else:  # their text as Split column and Pivot write it: 17 (Polars: 17.0), 2025-12-31 08:30:00 in a zone's
            text = dtypes.text_expr(column, dtype)  # local time (Polars: ...08:30:00.000000+01:00), 1d 2h 3m; bytes 0xFF00
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
        if isinstance(c.value, parameters.DateTimeText):  # a date-time parameter alone: its value goes with it (see _literal)
            items = [parameters.DateTimeText(items[0], c.value.value)]
        values = [i.lower() if text_column and not case_sensitive else _typed(i, dtype, c.column, typed) for i in items]
        if isinstance(dtype, pl.Decimal):  # values the column can't hold aren't in it
            values = [v for v in (_on_scale(v, dtype.scale) for v in values) if v is not None]
        elif dtype.is_integer():  # the same for whole numbers: not 1,5, and 300 not for an Int8
            low, high = _limits(dtype)
            values = [v for v in values if isinstance(v, int) and low <= v <= high]
        listed = pl.Series(values, dtype=dtype).implode() if dtype.is_integer() else values
        if dtype in (pl.Time, pl.Datetime) and values:  # in the column's unit: those it can't hold aren't in it
            held = [number for number in (_held(v, dtype) for v in values) if number is not None]
            listed = pl.Series(held, dtype=pl.Int64).cast(dtype).implode()
        found = column.is_in(listed) if values else pl.lit(False)
        return found if op == "in" else (~found).fill_null(True)

    if text_column:
        if op in (">", ">=", "<", "<=") and warn is not None and (kind := _reads_as(c.value)) is not None:
            shown = c.value.strip()
            how = "change the column's type before this step (Change type / format)"
            if kind == "time":  # Change type makes no times: a formula does
                name = "[" + c.column.replace("]", "]]") + "]"
                how = f"use Filter rows by formula, e.g. TRY_CAST({name} AS TIME) {op} TIME '{shown}'"
            warn(
                f"Column '{c.column}' holds text, so the condition {_LABELS[op]} '{shown}' compares the texts "
                f"character by character, not as {kind}s: to compare {kind}s, {how}"
            )
        value: Any = c.value if case_sensitive else c.value.lower()
    else:
        value = _typed(c.value, dtype, c.column, typed)
        if dtype.is_integer() and not _holds(dtype, value):  # no whole number the column holds: with all its digits
            exact = dtypes.parse_decimal(c.value.strip(), _decimal_of(c.value.strip()))
            if exact is not None:
                compared = _whole_comparison(column, op, exact, dtype)
                return compared.fill_null(True) if op == "!=" else compared
    if isinstance(value, Decimal):
        compared = _decimal_comparison(column, op, value, dtype.scale)
    elif isinstance(value, _Moment):
        compared = _moment_comparison(column, op, value, dtype)
    else:
        compared = _COMPARE[op](column, _for_polars(value, dtype))
    return compared.fill_null(True) if op == "!=" else compared


def _decimal_texts(df: pl.DataFrame, schema: pl.Schema, names: list[str]) -> dict[str, pl.Series]:
    """The text of the decimal-number columns among ``names`` (``dtypes.text_expr``), written before
    the filter, once for all of them. That text runs Python: inside a filter once for each column
    and each thread of Polars (a few hundred columns took seconds), and for each condition. The
    values of all columns of a type are written in one go (each value's text depends on itself
    only), in parts that Polars writes at the same time; each type apart, as Float32 values have
    their own digits."""
    floats = [name for name in dict.fromkeys(names) if name in schema and schema[name].is_float()]
    texts = {}
    for kind in dict.fromkeys(schema[name] for name in floats):
        group = [name for name in floats if schema[name] == kind]
        # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
        values = pl.concat([df[name].alias("value") for name in group])
        count = max(1, min(pl.thread_pool_size(), -(-values.len() // _PART)))
        size = -(-values.len() // count)
        values = values.extend_constant(None, count * size - values.len())  # parts of one length, for one select
        parts = [dtypes.text_expr(pl.lit(values.slice(k * size, size)), kind).alias(f"part{k}") for k in range(count)]
        written = pl.concat([part.alias("text") for part in pl.select(parts).get_columns()])
        texts.update((name, written.slice(index * df.height, df.height)) for index, name in enumerate(group))
    return texts


def _for_polars(value: Any, dtype: pl.DataType) -> Any:
    """A typed value as Polars takes it: a Python number only below 2^127, so a higher one (only
    ``_whole`` gives those, for a UInt128 column) needs the column's type."""
    return pl.lit(value, dtype=dtype) if isinstance(value, int) and value >= 2**127 else value


def _moment_comparison(column: pl.Expr, op: str, moment: _Moment, dtype: pl.DataType) -> pl.Expr:
    """A date-time or time column compared with a typed one, exactly: one with digits below the
    column's unit (08:30:00.0005 for milliseconds) is between two of its values. One beyond
    nanoseconds (before 1677 or after 2262) is beyond all values of a column of them: it goes to
    Polars in microseconds, which then compares it as a Python date-time, without converting the
    column. (A Python date-time can't be in the year 10000, which 31.12.9999 23:59 in New York is
    in UTC, and its summer times in 2300 aren't Polars'.)"""
    if dtype == pl.Datetime and dtype.time_unit == "ns" and not _NANOSECONDS[0] <= moment.ns <= _NANOSECONDS[1]:
        micros = moment.ns // 1000  # just after the last nanosecond that is the last one's microsecond: the next one
        micros = micros if moment.ns < 0 else max(micros, _NANOSECONDS[1] // 1000 + 1)
        return _COMPARE[op](column, pl.lit(micros, dtype=pl.Datetime("us", dtype.time_zone)))
    number, rest = divmod(moment.ns, _size(dtype))
    if rest or moment.finer:  # between two values of the column
        if op in ("=", "!="):
            return pl.when(column.is_not_null()).then(pl.lit(op == "!="))
        op = ">" if op in (">", ">=") else "<="
    return _COMPARE[op](column, pl.lit(number, dtype=dtype))


def _held(moment: _Moment, dtype: pl.DataType) -> int | None:
    """A typed date-time or time as a number of the unit of a column (see ``_size``), when it can
    be one of its values; else None: 08:30:00.0005 for milliseconds, 9999-12-31 for nanoseconds."""
    number, rest = divmod(moment.ns, _size(dtype))
    beyond = dtype == pl.Datetime and dtype.time_unit == "ns" and not _NANOSECONDS[0] <= number <= _NANOSECONDS[1]
    return None if rest or moment.finer or beyond else number


def _size(dtype: pl.DataType) -> int:
    """The nanoseconds in the unit of a date-time or time column (times are in nanoseconds)."""
    return 10 ** (9 - _PLACES[dtype.time_unit if dtype == pl.Datetime else "ns"])


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


def _rounded_with(values: pl.Series, value: Any) -> int | None:
    """A whole number of ``values`` that changes when ``value`` makes the column decimal numbers
    (Float64: a value with decimal places, or beyond the column's type), or None."""
    if not values.dtype.is_integer() or not isinstance(value, float):
        return None
    rounded = values.filter(dtypes.float_loses_digits(values))
    return rounded[0] if rounded.len() else None


def _rounded_typed(text: str, value: Any, dtype: pl.DataType) -> int | str | None:
    """What a typed number ``value`` becomes when it makes a whole-number column decimal numbers
    (Float64) and they can't keep it, as ``_rounded_with`` checks the column's values:
    12345678901234567891 becomes 12345678901234567168, 1234567890123456.78 becomes
    1234567890123456.8 (as the cell shows it). None when it stays as it is, also when the cell
    shows it as it was typed (0.1, 10 followed by 37 zeros)."""
    typed = text.strip()
    if not dtype.is_integer() or not isinstance(value, float) or not abs(value) < float("inf"):
        return None
    exact = dtypes.parse_decimal(typed, _decimal_of(typed))
    if exact is None or exact in (Decimal(value), Decimal(repr(value))):
        return None
    return int(value) if value.is_integer() else pl.select(dtypes.text_expr(pl.lit(value), pl.Float64)).item()


def _typed(text: str, dtype: pl.DataType, column: str, typed: dict[tuple[Any, ...], Any] | None) -> Any:
    """``_literal``, kept in ``typed`` for the next column of the same type: reading a value takes
    Polars a millisecond or more, and a step can have thousands of columns."""
    if typed is None:
        return _literal(text, dtype, column)
    key = (type(text), text, dtype)  # a date-time parameter (DateTimeText) is a moment: not the same as its text
    if key not in typed:
        typed[key] = _literal(text, dtype, column)
    return typed[key]


def _literal(text: str, dtype: pl.DataType, column: str) -> Any:
    """A value typed by the user, converted to the column's type. A time or a date-time is a
    ``_Moment``, with all its digits: Python's have no nanoseconds (08:30:00.000000001), and a unit
    of Polars holds only some of them; nanoseconds reach only from 1677 to 2262.

    A typed date-time is the local time of the column's zone, as the grid shows its values. A
    date-time parameter alone (a ``DateTimeText``) is a moment of this PC's clock, as in formulas
    (``{Now}`` is this moment there too). ``today`` and ``now`` are in the column's unit."""
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
            return dt.date.today() if dtype == pl.Date else _moment(_now(dtype).dt.truncate("1d"))  # the start of the local day
        if value.lower() in ("now", "jetzt") and dtype == pl.Datetime:
            return _moment(_now(dtype))
        if dtype == pl.Date:
            parsed = dtypes.parse_dates(pl.Series([value]))[0]
            if parsed is None:
                raise ActionError(f"'{text}' is not a date (column '{column}'); use e.g. 31.12.2025 or 2025-12-31")
            return parsed
        if dtype.time_zone is not None and isinstance(text, parameters.DateTimeText):
            return _moment(expressions.moment(text.value))
        parsed = dtypes.parse_datetimes(pl.Series([value]))  # microseconds reach from the year 1 to 9999
        if parsed.null_count():
            raise ActionError(
                f"'{text}' is not a date-time (column '{column}'); use e.g. 31.12.2025 08:30 or 2025-12-31 08:30"
            )
        # the moment of the local time, and the digits below its microseconds: the local time itself can be
        # beyond nanoseconds (2262-04-12 01:00 in Lord Howe is 2262-04-11 14:00 in UTC), where Polars would
        # make it a time of 1677
        return _moment(parsed if dtype.time_zone is None else _in_zone(parsed, text, column, dtype.time_zone), value)
    if dtype == pl.Time:
        parsed = dtypes.parse_times(pl.Series([value]))
        if parsed.null_count():
            raise ActionError(f"'{text}' is not a time (column '{column}'); use e.g. 08:30 or 08:30:15")
        return _moment(parsed, value)
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


def _now(dtype: pl.DataType) -> pl.Series:
    """This moment as a value of a date-time column (see ``_literal``): the local time of the
    column's time zone, or of this PC for a column without one."""
    if dtype.time_zone is None:
        return pl.Series([dt.datetime.now()], dtype=pl.Datetime(dtype.time_unit))
    return pl.Series([dt.datetime.now(dt.UTC)]).dt.convert_time_zone(dtype.time_zone).dt.cast_time_unit(dtype.time_unit)


def _in_zone(parsed: pl.Series, text: str, column: str, zone: str) -> pl.Series:
    """A typed date-time as the local time of a column's time zone, as the grid shows its values.
    A time that a clock change skips or repeats there is not one moment: it stops the step."""
    moment = parsed.dt.replace_time_zone(zone, ambiguous="null", non_existent="null")
    if moment.null_count():
        if parsed.dt.replace_time_zone(zone, ambiguous="earliest", non_existent="null").null_count():
            raise ActionError(f"'{text}' doesn't happen in the time zone of column '{column}' ({zone}): a clock change skips it")
        raise ActionError(f"'{text}' happens twice in the time zone of column '{column}' ({zone}): a clock change repeats it")
    return moment


def _moment(value: pl.Series, text: str = "") -> _Moment:
    """A date-time or a time (a Series of one) as a ``_Moment``, with the digits of the fraction
    of a second in ``text`` that its unit doesn't hold: Polars leaves them out when it reads it."""
    places = _PLACES[value.dtype.time_unit if value.dtype == pl.Datetime else "ns"]
    fraction = re.search(r":[0-9]{2}\.([0-9]+)$", text)
    below = fraction.group(1)[places:] if fraction else ""  # the digits after those of the unit
    nanoseconds = int(below[: 9 - places].ljust(9 - places, "0") or 0)
    return _Moment(value.to_physical()[0] * 10 ** (9 - places) + nanoseconds, below[9 - places :].strip("0") != "")


def _moment_fill(values: pl.Series, text: str, moment: _Moment, name: str) -> tuple[pl.DataType, pl.Expr]:
    """The type of a date-time or time column ``values`` filled with a typed value, and the value
    in it. A unit that can't hold the value gets finer (milliseconds, microseconds, nanoseconds)
    when every value of the column fits; a value beyond nanoseconds (before 1677 or after 2262)
    makes a column of them microseconds, as in 0.6.2, when none of its values has digits below a
    microsecond. Else the step stops: no value changes without a word."""
    dtype = values.dtype
    if moment.finer:
        kind = "times" if dtype == pl.Time else "date-times"
        raise ActionError(f"'{text}' doesn't fit column '{name}': {kind} hold no digits below a nanosecond")
    if dtype == pl.Time:
        return dtype, pl.lit(moment.ns, dtype=dtype)
    units = list(_PLACES)
    needed = next(unit for unit in units if moment.ns % 10 ** (9 - _PLACES[unit]) == 0)  # the coarsest that holds it
    unit = max(dtype.time_unit, needed, key=units.index)
    if unit == "ns" and not _NANOSECONDS[0] <= moment.ns <= _NANOSECONDS[1]:  # beyond nanoseconds
        if needed == "ns":
            raise ActionError(
                f"'{text}' doesn't fit column '{name}': its digits below a microsecond need nanoseconds, which reach "
                "only from 1677 to 2262"
            )
        unit = "us"
    target = pl.Datetime(unit, dtype.time_zone)
    if units.index(unit) < units.index(dtype.time_unit) and (cut := _cut_with(values, target)) is not None:
        raise ActionError(
            f"'{text}' doesn't fit column '{name}': nanoseconds reach only from 1677 to 2262, so with it the "
            f"column holds microseconds, but it has values with nanoseconds (e.g. {cut[0]} would become {cut[1]})"
        )
    if units.index(unit) > units.index(dtype.time_unit) and (beyond := _beyond(values, target)).len():
        reach = ", which reach only from 1677 to 2262" if unit == "ns" else ""
        per_second = 10 ** _PLACES[dtype.time_unit]  # Polars can't write the text of a year far beyond 9999: it panics
        shown = beyond.filter(beyond.to_physical().is_between(_SHOWN[0] * per_second, _SHOWN[1] * per_second))
        example = f" (e.g. {pl.select(dtypes.text_expr(pl.lit(shown.head(1)), dtype)).item()})" if shown.len() else ""
        raise ActionError(
            f"'{text}' doesn't fit column '{name}': with it the column holds {_UNIT_NAMES[unit]}{reach}, but it has "
            f"values beyond them{example}"
        )
    if dtype.time_zone is not None and not 1 <= (year := _local_year(moment.ns, dtype.time_zone)) <= 9999:
        raise ActionError(
            f"'{text}' doesn't fit column '{name}': in its time zone ({dtype.time_zone}) this moment "
            f"is in the year {year}, and date-times reach from the year 1 to 9999"
        )
    return target, pl.lit(moment.ns // 10 ** (9 - _PLACES[unit]), dtype=target)


def _cut_with(values: pl.Series, unit: pl.DataType) -> tuple[str, str] | None:
    """A date-time of ``values`` (in nanoseconds) with digits below a microsecond, which it loses
    in ``unit`` (microseconds): as text, and as it would become; or None."""
    # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    cut = values.filter(values.alias("value").to_physical() % 1000 != 0)
    if not cut.len():
        return None
    example = cut.head(1)
    return tuple(pl.select(dtypes.text_expr(pl.lit(s), s.dtype)).item() for s in (example, example.cast(unit)))


def _beyond(values: pl.Series, unit: pl.DataType) -> pl.Series:
    """The date-times of ``values`` that ``unit``, a finer one, can't hold (9999-12-31 in nanoseconds)."""
    values = values.alias("value")  # a fixed name, as in _cut_with
    return values.filter(values.is_not_null() & values.cast(unit, strict=False).is_null())


@functools.lru_cache(maxsize=64)
def _local_year(moment: int, zone: str) -> int:
    """The year of a moment (in nanoseconds) in the local time of a time zone: 9999-12-31 23:00 of
    this PC's clock can be in the year 10000 there."""
    return pl.select(pl.lit(moment // 1000, dtype=pl.Datetime("us", zone)).dt.year()).item()


def _no_typed_value(column: str, held: str = "time spans") -> str:
    return f"Column '{column}' holds {held}: a typed value can't be used with it"


def _held_values(dtype: pl.DataType) -> str | None:
    """What a column holds whose values are no single values, so that no typed value compares with
    them: lists, records, values of a plugin's own type. None for other columns."""
    if isinstance(dtype, pl.List | pl.Array):
        return "lists"
    if isinstance(dtype, pl.Struct):
        return "records"
    return "values of a plugin's own type" if isinstance(dtype, pl.Object) else None


def _not_comparable(column: str, dtype: pl.DataType) -> str:
    """The message of Filter rows for a column of ``_held_values``: what it holds, and the formula
    that does it."""
    held, name = _held_values(dtype), "[" + column.replace("]", "]]") + "]"
    message = f"Column '{column}' holds {held}, which Filter rows can't compare with a value"
    if held == "lists":  # a value of the type of the items, or a formula for any list
        value = _formula_value(dtype.inner)
        return f"{message}: use Filter rows by formula, e.g. " + (
            f"LIST_CONTAINS({name}, {value})" if value is not None else f"LEN({name}) > 0"
        )
    if held == "records" and dtype.fields:
        field = dtype.fields[0]
        value = _formula_value(field.dtype)
        test = f"= {value}" if value is not None else "IS NOT NULL"
        return f"{message}: use Filter rows by formula, e.g. STRUCT_EXTRACT({name}, {_formula_text(field.name)}) {test}"
    return message


def _formula_value(dtype: pl.DataType) -> str | None:
    """A value of type ``dtype`` as a formula writes it, for the example of a message; None for a
    type that has no such value (empty values, a plugin's own values)."""
    if dtype == pl.String or dtype == pl.Categorical or isinstance(dtype, pl.Enum):
        return "'a'"
    if dtype == pl.Boolean:
        return "true"
    if dtype.is_integer():
        return "1"
    if dtype.is_float() or isinstance(dtype, pl.Decimal):
        return "1.5"
    if dtype == pl.Date:
        return "DATE '2025-12-31'"
    if dtype == pl.Datetime:  # in the unit of the column: DuckDB doesn't find microseconds among nanoseconds
        kind = {"ms": "TIMESTAMP_MS", "us": "TIMESTAMP", "ns": "TIMESTAMP_NS"}[dtype.time_unit]
        return f"CAST('2025-12-31 08:30:00' AS {'TIMESTAMPTZ' if dtype.time_zone else kind})"
    if dtype == pl.Time:  # Polars' times are nanoseconds, which DuckDB keeps apart from its times
        return "CAST('08:30:00' AS TIME_NS)"
    if dtype == pl.Duration:
        return "INTERVAL 1 HOUR"
    if dtype == pl.Binary:
        return "'a'::BLOB"
    if isinstance(dtype, pl.List | pl.Array):  # not [...]: a formula reads that as a column's name
        inner = _formula_value(dtype.inner)
        return None if inner is None else f"LIST_VALUE({inner})"
    if isinstance(dtype, pl.Struct) and dtype.fields:
        values = [_formula_value(field.dtype) for field in dtype.fields]
        if None in values:
            return None
        return "{" + ", ".join(f"{_formula_text(f.name)}: {v}" for f, v in zip(dtype.fields, values)) + "}"
    return None


def _formula_text(text: str) -> str:
    """A text in a formula: in single quotes, with each single quote doubled."""
    return "'" + text.replace("'", "''") + "'"


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


def _holds(dtype: pl.DataType, value: Any) -> bool:
    """Whether a whole-number column can hold a typed value. Polars compares a UInt128 column with
    -1 as Int128 numbers, in which its values from 2^127 up are empty."""
    low, high = _limits(dtype)
    return isinstance(value, int) and low <= value <= high


@functools.lru_cache(maxsize=256)  # a step can compare thousands of columns with one value
def _reads_as(text: str) -> str | None:
    """What a typed value would be in a column of dates, times or numbers: "date" (31.03.2026, also
    with a time), "time" (08:30, 8:30:15) or "number" (-16, 1.234,5); None for other text, also for
    the words that Polars reads as numbers (nan, inf, infinity): a number has a digit."""
    value = text.strip()
    if dtypes.parse_dates(pl.Series([value]))[0] is not None:
        return "date"
    if dtypes.parse_times(pl.Series([value]))[0] is not None:
        return "time"
    if not re.search("[0-9]", value):
        return None
    return "number" if dtypes.parse_numbers(pl.Series([value]), _decimal_of(value))[0] is not None else None


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

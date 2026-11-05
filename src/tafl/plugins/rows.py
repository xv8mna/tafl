"""Rows: filter, sort, remove duplicates, fill empty values."""

from __future__ import annotations

import datetime as dt
import operator
import re
from decimal import ROUND_FLOOR, Context, Decimal
from typing import Any

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
    schema = df.schema  # once: Polars makes it anew for every call, with all columns (seconds for thousands)
    mask_parts = [_condition(df, c, p.case_sensitive, schema) for c in p.conditions]
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
    typed: dict[pl.DataType, Any] = {}  # the fixed value for each type of column, read once
    updates = []
    for name in p.columns:
        dtype = schema[name]
        column = col(name)
        if dtype == pl.String:
            column = pl.when(column.str.strip_chars() == "").then(None).otherwise(column)
        if p.method == "value":
            if dtype not in typed:
                typed[dtype] = _literal(p.value, dtype, name)
            value = typed[dtype]
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
            if (cut := _cut_with(df[name], value)) is not None:
                raise ActionError(
                    f"'{p.value}' doesn't fit column '{name}': nanoseconds reach only from 1677 to 2262, so with it the "
                    f"column holds microseconds, but it has values with nanoseconds (e.g. {cut[0]} would become {cut[1]})"
                )
            if (year := _year_beyond(value)) is not None:
                raise ActionError(
                    f"'{p.value}' doesn't fit column '{name}': in its time zone ({dtype.time_zone}) this moment "
                    f"is in the year {year}, and date-times reach from the year 1 to 9999"
                )
            updates.append(column.fill_null(_for_polars(value, dtype)).alias(name))
        else:
            updates.append(column.fill_null(strategy="forward" if p.method == "down" else "backward").alias(name))
    return df.with_columns(updates)


def _condition(df: pl.DataFrame, c: Any, case_sensitive: bool, schema: pl.Schema | None = None) -> pl.Expr:
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

    if op in ("contains", "not_contains", "starts", "ends"):
        needle = c.value
        if dtype == pl.Binary and dtypes.bytes_are_text(df[c.column]):  # binary values that are all text: that text
            text = column.cast(pl.String)
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
        values = [i.lower() if text_column and not case_sensitive else _literal(i, dtype, c.column) for i in items]
        if isinstance(dtype, pl.Decimal):  # values the column can't hold aren't in it
            values = [v for v in (_on_scale(v, dtype.scale) for v in values) if v is not None]
        elif dtype.is_integer():  # the same for whole numbers: not 1,5, and 300 not for an Int8
            low, high = _limits(dtype)
            values = [v for v in values if isinstance(v, int) and low <= v <= high]
        listed = pl.Series(values, dtype=dtype).implode() if dtype.is_integer() else values
        if dtype in (pl.Time, pl.Datetime) and values:  # Series of one value each (see _literal)
            # in the column's unit: a value it can't hold (9999-12-31 in nanoseconds) isn't in the column
            listed = pl.concat([v.cast(dtype, strict=False) for v in values]).drop_nulls().implode()
        found = column.is_in(listed) if values else pl.lit(False)
        return found if op == "in" else (~found).fill_null(True)

    if text_column:
        value: Any = c.value if case_sensitive else c.value.lower()
    else:
        value = _literal(c.value, dtype, c.column)
        if dtype.is_integer() and not _holds(dtype, value):  # no whole number the column holds: with all its digits
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
    ``_whole`` gives those, for a UInt128 column) needs the column's type. A date-time beyond the
    nanoseconds of the column (see ``_literal``) goes by its number: a Python date-time can't be in
    the year 10000 (31.12.9999 23:59 in New York, in UTC), and its summer times in 2300 aren't Polars'."""
    if isinstance(value, pl.Series) and value.dtype != dtype:  # a far date-time in microseconds
        return pl.lit(value.to_physical()[0], dtype=value.dtype)  # one value, so that Polars doesn't convert the column
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


def _literal(text: str, dtype: pl.DataType, column: str) -> Any:
    """A value typed by the user, converted to the column's type. A time or a date-time is a
    Series of one value: Python's have no nanoseconds (08:30:00.000000001), and a date-time is in
    the column's time zone and in its unit (Polars compares nanoseconds with microseconds in
    microseconds) when that holds it: nanoseconds reach only from 1677 to 2262, so 9999-12-31
    stays in microseconds (in nanoseconds it would become a date of another century).

    A typed date-time is the local time of the column's zone, as the grid shows its values. A
    date-time parameter alone (a ``DateTimeText``) is a moment of this PC's clock, as in formulas
    (``{Now}`` is this moment there too)."""
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
            return dt.date.today() if dtype == pl.Date else _now(dtype).dt.truncate("1d")  # the start of the local day
        if value.lower() in ("now", "jetzt") and dtype == pl.Datetime:
            return _now(dtype)
        if dtype == pl.Date:
            parsed = dtypes.parse_dates(pl.Series([value]))[0]
            if parsed is None:
                raise ActionError(f"'{text}' is not a date (column '{column}'); use e.g. 31.12.2025 or 2025-12-31")
            return parsed
        if dtype.time_zone is not None and isinstance(text, parameters.DateTimeText):
            moment = expressions.moment(text.value).dt.convert_time_zone(dtype.time_zone)
            return moment.cast(dtype) if _unit_holds(moment, dtype.time_unit) else moment
        parsed = dtypes.parse_datetimes(pl.Series([value]))  # microseconds reach from the year 1 to 9999
        if parsed.null_count():
            raise ActionError(
                f"'{text}' is not a date-time (column '{column}'); use e.g. 31.12.2025 08:30 or 2025-12-31 08:30"
            )
        moment = parsed if dtype.time_zone is None else _in_zone(parsed, text, column, dtype.time_zone)
        if dtype.time_unit == "us" or not _unit_holds(moment, dtype.time_unit):
            return moment
        if dtype.time_unit == "ms" or not re.search(r"\.[0-9]{7}", value):  # no digits below a microsecond
            return moment.cast(dtype)
        exact = dtypes.parse_datetimes(pl.Series([value]), time_unit="ns")  # with its nanoseconds
        return exact if dtype.time_zone is None else _in_zone(exact, text, column, dtype.time_zone)
    if dtype == pl.Time:
        parsed = dtypes.parse_times(pl.Series([value]))
        if parsed.null_count():
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


def _unit_holds(moment: pl.Series, unit: str) -> bool:
    """Whether date-times in ``unit`` hold a typed one, ``moment`` in microseconds, also with
    digits below a microsecond: nanoseconds reach only from 1677 to 2262."""
    around = pl.concat([moment - dt.timedelta(microseconds=1), moment + dt.timedelta(microseconds=1)])
    return not around.cast(pl.Datetime(unit, moment.dtype.time_zone), strict=False).null_count()


def _cut_with(values: pl.Series, value: Any) -> tuple[str, str] | None:
    """A date-time of ``values`` (in nanoseconds) with digits below a microsecond, which it loses
    when ``value``, a date-time beyond the nanoseconds' range (see ``_literal``), makes the column
    microseconds: as text, and as it would become; or None."""
    if not isinstance(values.dtype, pl.Datetime) or values.dtype.time_unit != "ns":
        return None
    if not isinstance(value, pl.Series) or value.dtype.time_unit == "ns":
        return None
    # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    cut = values.filter(values.alias("value").to_physical() % 1000 != 0)
    if not cut.len():
        return None
    example = cut.head(1)
    return tuple(pl.select(dtypes.text_expr(pl.lit(s), s.dtype)).item() for s in (example, example.cast(value.dtype)))


def _year_beyond(value: Any) -> int | None:
    """The year of a date-time (see ``_literal``) when it is not 1 to 9999, which the grid can't
    show: 9999-12-31 23:00 of this PC's clock can be in the year 10000 in a column's time zone.
    None for a year from 1 to 9999."""
    if not isinstance(value, pl.Series) or not isinstance(value.dtype, pl.Datetime):
        return None
    year = value.dt.year()[0]
    return None if 1 <= year <= 9999 else year


def _no_typed_value(column: str) -> str:
    return f"Column '{column}' holds time spans: a typed value can't be used with it"


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
    if held == "lists":
        return f"{message}: use Filter rows by formula, e.g. LIST_CONTAINS({name}, 'a')"
    if held == "records" and dtype.fields:
        field = dtype.fields[0].name.replace("'", "''")
        return f"{message}: use Filter rows by formula, e.g. STRUCT_EXTRACT({name}, '{field}') = 1"
    return message


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

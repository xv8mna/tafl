"""Instant filters: pick values of a column to see only the rows that have them.

They change only what the grid shows — never the data, a run or an export — and they stay
while you click through the steps of a table, so you can follow rows through it. Several values
of one filter: any of them (OR); several filters: all of them (AND).

A filter can group the cells of its column first (its mode, called facet here):

* ``values``: every value on its own (the default)
* ``period``: dates by year, quarter, month, ISO week or day
* ``ranges``: numbers in ranges between thresholds
* ``length``: texts by their number of characters
* ``regex``: texts that match a pattern, or don't

A filter is stored with its table in the project file:
``{"column": "Date", "facet": "period", "options": {"by": "month"}, "values": ["2025-01"]}``.
Values are JSON (dates as ISO text) and are converted to the column's type when the filter is
used, so a filter keeps working when a step changes a column's type. No values = the filter is
shown but doesn't filter. ``"except": true`` turns it around: all values except these. That
keeps a selection of most of many values short (all of them: ``"values": []``).
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import polars as pl

VALUE, COUNT = "value", "count"  # the columns of a facet
MATCH, NO_MATCH = "match", "no match"
_LONG = 500  # more chosen values are stored as the ones left out, if those are fewer
FACETS = {
    "values": "Values",
    "period": "Time periods",
    "ranges": "Number ranges",
    "length": "Text length",
    "regex": "Pattern (regex)",
}
PERIODS = {"year": "Year", "quarter": "Year-Quarter", "month": "Year-Month", "week": "Year-Week (ISO)", "date": "Date"}
# strftime formats: for the filter (Polars) and for a step made from it (DuckDB)
_PERIOD_FORMATS = {"year": "%Y", "month": "%Y-%m", "week": "%G-W%V", "date": "%Y-%m-%d"}
_NEEDS = {
    "period": "time periods need a date column",
    "ranges": "number ranges need a number column",
    "length": "text length needs a text column",
    "regex": "a pattern needs a text column",
}


@dataclass
class Filter:
    column: str
    values: list[Any] = field(default_factory=list)  # the applied values, as JSON
    facet: str = "values"
    options: dict[str, Any] = field(default_factory=dict)
    excluded: bool = False  # the values are the ones left out: all others are applied

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"column": self.column}
        if self.facet != "values":
            data["facet"] = self.facet
        if self.options:
            data["options"] = dict(self.options)
        if self.excluded:
            data["except"] = True
        data["values"] = list(self.values)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Filter:
        facet = str(data.get("facet") or "values")
        return cls(
            str(data.get("column") or ""),
            list(data.get("values") or []),
            facet if facet in FACETS else "values",
            dict(data.get("options") or {}),
            data.get("except") is True,
        )

    def picker(self) -> Callable[[Any], bool]:
        """A function: whether a value (as stored) is one the filter lets through."""
        chosen = {value_key(v) for v in self.values}
        if self.excluded:
            return lambda stored: value_key(stored) not in chosen
        return lambda stored: value_key(stored) in chosen


def value_key(value: Any) -> Any:
    """A stored value in a form that can be looked up in a set (NaN never equals itself)."""
    return ("NaN",) if isinstance(value, float) and math.isnan(value) else value


def selection(chosen: list[Any], offered: list[Any]) -> tuple[list[Any], bool]:
    """How a filter stores ``chosen`` of the ``offered`` values (all as stored): the values, or
    the values left out (then the second item is True) when all are chosen or the list would be
    long: a value that isn't there yet (e.g. after another step) passes then."""
    picked = {value_key(v) for v in chosen}
    left_out = [v for v in offered if value_key(v) not in picked]
    everything = bool(chosen) and not left_out
    if everything or (len(chosen) > _LONG and len(left_out) < len(chosen)):
        return left_out, True
    return list(chosen), False


def facets_for(dtype: pl.DataType) -> list[str]:
    """The ways a column of this type can be filtered."""
    kinds = ["values"]
    if dtype == pl.Date or dtype == pl.Datetime:
        kinds.append("period")
    if dtype.is_numeric():
        kinds.append("ranges")
    if dtype == pl.String:
        kinds += ["length", "regex"]
    return kinds


def default_options(facet: str) -> dict[str, Any]:
    return {"period": {"by": "month"}, "ranges": {"thresholds": [], "lower_inclusive": True}, "regex": {"pattern": ""}}.get(
        facet, {}
    )


def unusable(f: Filter, schema: pl.Schema) -> str | None:
    """Why the filter can't be used with this data, or None if it can."""
    if f.column not in schema:
        return "there is no such column yet (or any more)"
    if f.facet not in facets_for(schema[f.column]):
        return _NEEDS[f.facet]
    if f.facet == "regex" and (error := pattern_error(str(f.options.get("pattern") or ""))):
        return f"the pattern is not valid: {error}"
    return None


# --- what a filter picks from ---------------------------------------------------------------------


def keys(f: Filter, schema: pl.Schema) -> pl.Expr | None:
    """What the filter's values are for each row: the cell itself, its period, its range, its
    length or whether it matches. None when the filter can't be used with this data."""
    if unusable(f, schema):
        return None
    column = pl.col(f.column)
    if f.facet == "period":
        by = f.options.get("by", "month")
        if by == "quarter":
            return pl.format("{} Q{}", column.dt.year(), column.dt.quarter())
        if by == "week":
            return pl.format("{}-W{}", column.dt.iso_year(), column.dt.week().cast(pl.String).str.zfill(2))
        return column.dt.strftime(_PERIOD_FORMATS.get(by, "%Y-%m"))
    if f.facet == "ranges":
        above = [(column >= t) if f.options.get("lower_inclusive", True) else (column > t) for t in thresholds(f)]
        index = pl.sum_horizontal([a.cast(pl.Int64) for a in above]) if above else pl.lit(0, pl.Int64)
        return pl.when(column.is_null()).then(None).otherwise(index)
    if f.facet == "length":
        return column.str.len_chars().cast(pl.Int64)
    if f.facet == "regex":
        matches = column.str.contains(str(f.options.get("pattern") or ""))
        return pl.when(column.is_null()).then(None).when(matches).then(pl.lit(MATCH)).otherwise(pl.lit(NO_MATCH))
    return column


def key_type(f: Filter, dtype: pl.DataType) -> pl.DataType:
    return {"period": pl.String, "regex": pl.String, "ranges": pl.Int64, "length": pl.Int64}.get(f.facet, dtype)


def thresholds(f: Filter) -> list[float]:
    """The thresholds of a "ranges" filter, sorted, without duplicates."""
    values = []
    for value in f.options.get("thresholds") or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(number):
            values.append(number)
    return sorted(set(values))


def range_labels(limits: list[float], lower_inclusive: bool, number: Callable[[float], str]) -> list[str]:
    """Names of the ranges between thresholds, e.g. "< 100", "100 to < 1000", "≥ 1000"."""
    if not limits:
        return ["all numbers"]
    texts = [number(limit) for limit in limits]
    below, above = ("<", "≥") if lower_inclusive else ("≤", ">")
    labels = [f"{below} {texts[0]}"]
    for low, high in zip(texts, texts[1:]):
        labels.append(f"{low} to {below} {high}" if lower_inclusive else f"{above} {low} to {below} {high}")
    labels.append(f"{above} {texts[-1]}")
    return labels


def parse_thresholds(text: str, decimal: str = ".") -> list[float]:
    """Numbers typed by the user, separated by ";" or spaces: 1000, 1.5, 1,5, 1,000 or 1.000,5
    (with both separators, the last one is the decimal one; ``decimal`` decides "1,000")."""
    numbers = []
    for part in re.split(r"[;\s]+", text.strip()):
        number = _number(part, decimal)
        if number is not None and math.isfinite(number):
            numbers.append(number)
    return sorted(set(numbers))


def _number(text: str, decimal: str) -> float | None:
    if "," in text and "." in text:
        last = "," if text.rfind(",") > text.rfind(".") else "."
        text = text.replace("." if last == "," else ",", "").replace(",", ".")
    elif text.count(",") > 1:  # 1,000,000
        text = text.replace(",", "")
    elif "," in text:
        thousands = decimal != "," and len(text.split(",")[1]) == 3  # 1,000 (but 1,5 is one and a half)
        text = text.replace(",", "" if thousands else ".")
    try:
        return float(text)
    except ValueError:
        return None


@functools.lru_cache(maxsize=64)
def pattern_error(pattern: str) -> str | None:
    """Why a regular expression can't be used (Polars' syntax), or None."""
    try:
        pl.Series([""], dtype=pl.String).str.contains(pattern)
    except pl.exceptions.PolarsError as error:
        found = re.search(r"^error: (.+)$", str(error), re.MULTILINE)  # e.g. "error: unclosed group"
        return found.group(1).strip() if found else "invalid"
    return None


# --- values ---------------------------------------------------------------------------------------


def to_json(value: Any) -> Any:
    """A cell value as it is stored in the project file."""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.timedelta):
        return value.total_seconds()
    return value


def from_json(values: list[Any], dtype: pl.DataType) -> list[Any]:
    """Stored values (without the empty one) in a column's type; values that don't fit are left out."""
    converted = []
    for value in values:
        if value is None:
            continue
        try:
            converted.append(_convert(value, dtype))
        except (TypeError, ValueError, ArithmeticError):
            continue
    return converted


def _convert(value: Any, dtype: pl.DataType) -> Any:
    if dtype == pl.String:
        return value if isinstance(value, str) else str(value)
    if dtype == pl.Boolean:
        if isinstance(value, bool):
            return value
        raise ValueError(value)
    if isinstance(value, bool):  # True is an int in Python: don't let it match 1
        raise ValueError(value)
    if dtype.is_integer():
        number = float(value) if isinstance(value, str) else value
        if not float(number).is_integer():
            raise ValueError(value)
        return int(number)
    if dtype.is_float():
        return float(value)
    if isinstance(dtype, pl.Decimal):
        return Decimal(str(value))
    if dtype == pl.Date:
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])
    if dtype == pl.Datetime:
        return value if isinstance(value, dt.datetime) else dt.datetime.fromisoformat(str(value))
    if dtype == pl.Time:
        return value if isinstance(value, dt.time) else dt.time.fromisoformat(str(value))
    if dtype == pl.Duration:
        return dt.timedelta(seconds=float(value))
    return value


# --- filtering ------------------------------------------------------------------------------------


def condition(f: Filter, schema: pl.Schema) -> pl.Expr | None:
    """The filter as a Polars condition for a table with this schema; None when it doesn't
    filter there (no values, or it can't be used with this data)."""
    if not f.values:
        return None
    picked = keys(f, schema)
    if picked is None:
        return None
    dtype = key_type(f, schema[f.column])
    values = from_json(f.values, dtype)
    try:
        found = picked.is_in(pl.Series(values, dtype=dtype).implode()) if values else pl.lit(False)
    except (TypeError, ValueError, pl.exceptions.PolarsError):
        found = pl.lit(False)
    if None in f.values:
        found = found | picked.is_null()
    return ~found.fill_null(False) if f.excluded else found.fill_null(False)


def apply(df: pl.DataFrame, filters: list[Filter]) -> pl.DataFrame:
    """The rows that pass all filters."""
    conditions = [c for f in filters if (c := condition(f, df.schema)) is not None]
    return df.filter(pl.all_horizontal(conditions)) if conditions else df


def facet(df: pl.DataFrame, filters: list[Filter], index: int) -> tuple[pl.DataFrame, int] | None:
    """The values filter ``index`` offers (cells, periods, ranges, ...) with how often each
    occurs among the rows that pass the *other* filters (0 = not possible with them), and how
    many rows those are. None if the filter can't be used with this data."""
    picked = keys(filters[index], df.schema)
    if picked is None:
        return None
    base = apply(df, [f for i, f in enumerate(filters) if i != index])
    try:
        values = df.select(picked.alias(VALUE)).unique()
        counts = base.group_by(picked.alias(VALUE)).agg(pl.len().alias(COUNT))
        table = values.join(counts, on=VALUE, how="left", nulls_equal=True)
    except pl.exceptions.PolarsError:
        return None
    return table.with_columns(pl.col(COUNT).fill_null(0)), base.height


# --- as a step ------------------------------------------------------------------------------------


def formula(f: Filter, dtype: pl.DataType) -> str:
    """The filter as a condition for the step "Filter rows by formula" (same rows)."""
    name = "[" + f.column.replace("]", "]]") + "]"
    values = from_json(f.values, key_type(f, dtype))
    parts = []
    if values and f.facet == "ranges":
        limits, inclusive = thresholds(f), f.options.get("lower_inclusive", True)
        for index in sorted(v for v in values if 0 <= v <= len(limits)):
            parts.append(_range_sql(name, limits, index, inclusive))
    elif values and f.facet == "regex":
        test = f"regexp_matches({name}, {_sql(str(f.options.get('pattern') or ''))})"
        parts += [test if value == MATCH else f"NOT {test}" for value in values if value in (MATCH, NO_MATCH)]
    elif values:
        parts.append(f"{_picked_sql(f, name)} IN ({', '.join(_sql(v) for v in values)})")
    if f.excluded:  # empty cells pass unless they are left out too
        if len(parts) > 1:
            parts = [f"({p})" if " AND " in p else p for p in parts]
        left_out = f"NOT coalesce({' OR '.join(parts)}, FALSE)" if parts else ""
        if None in f.values:
            return f"{name} IS NOT NULL" + (f" AND {left_out}" if left_out else "")
        return left_out or "TRUE"
    if None in f.values:
        parts.append(f"{name} IS NULL")
    if len(parts) > 1:
        parts = [f"({p})" if " AND " in p else p for p in parts]
    return " OR ".join(parts) if parts else "FALSE"


def _picked_sql(f: Filter, name: str) -> str:
    if f.facet == "period":
        by = f.options.get("by", "month")
        if by == "quarter":
            return f"year({name}) || ' Q' || quarter({name})"
        return f"strftime({name}, '{_PERIOD_FORMATS.get(by, '%Y-%m')}')"
    if f.facet == "length":
        return f"length({name})"
    return name


def _range_sql(name: str, limits: list[float], index: int, lower_inclusive: bool) -> str:
    low = limits[index - 1] if index > 0 else None
    high = limits[index] if index < len(limits) else None
    above, below = (">=", "<") if lower_inclusive else (">", "<=")
    sides = []
    if low is not None:
        sides.append(f"{name} {above} {_sql(low)}")
    if high is not None:
        sides.append(f"{name} {below} {_sql(high)}")
    return " AND ".join(sides) if sides else f"{name} IS NOT NULL"


def _sql(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isfinite(value):
        return "'NaN'::DOUBLE" if math.isnan(value) else f"'{'-' if value < 0 else ''}Infinity'::DOUBLE"
    if isinstance(value, (int, float, Decimal)):
        return str(value)
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, dt.time):
        return f"TIME '{value.isoformat()}'"
    if isinstance(value, dt.timedelta):
        return f"INTERVAL '{value.total_seconds()} seconds'"
    return "'" + str(value).replace("'", "''") + "'"

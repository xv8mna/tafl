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
Values are JSON (dates as ISO text, also with nanoseconds: 2025-01-01T08:30:00.000000001, binary
data as hex text: 0x0102FF, time spans as seconds, or as ISO text where seconds would lose digits:
PT0.000000001S) and are converted to the column's type when the filter is used, so a filter keeps
working when a step changes a column's type. No values = the filter is shown but doesn't filter.
``"except": true`` turns it around: all values except these. That keeps a selection of most of many
values short (all of them: ``"values": []``).
"""

from __future__ import annotations

import datetime as dt
import functools
import math
import re
import struct
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from decimal import Context, Decimal
from typing import Any

import polars as pl

from tafl.api import ActionError, col, dtypes, expressions

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
_OPTIONS = {"by": str, "thresholds": list, "lower_inclusive": bool, "pattern": str}  # the type of each option
_NEEDS = {
    "period": "time periods need a date column",
    "ranges": "number ranges need a number column",
    "length": "text length needs a text column",
    "regex": "a pattern needs a text column",
}
_EPOCH = dt.datetime(1970, 1, 1)
_EPOCH_UTC = dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
_MICROSECOND = dt.timedelta(microseconds=1)
_UNIT_NS = {"ns": 1, "us": 1_000, "ms": 1_000_000}  # nanoseconds of a time unit of Polars
_DAY_US = 86_400_000_000  # microseconds of a day
_FIRST_DAY = -96_465_292  # -262143-01-01: Polars' calendar begins there (days since 1970)
_END_DAY = 95_026_237  # +262143-01-01: Polars' calendar ends with +262142-12-31
_HOUR_US = 3_600_000_000  # microseconds of an hour
_FEW_RANGES = 4  # more runs of periods: the number of the period of a moment's local day (see _local_periods_sql)
_CHECKED_VALUES = 1_000  # Add as step checks the rows of a step of at most so many picked values (see _may_differ)
_PERIOD_TEXTS = {  # the periods of the card (Polars): a year before 0 has a -, one after 9999 a + (not in a week)
    "year": re.compile(r"([-+]?[0-9]+)"),
    "quarter": re.compile(r"([-+]?[0-9]+) Q([1-4])"),
    "month": re.compile(r"([-+]?[0-9]+)-([0-9]{2})"),
    "week": re.compile(r"([-+]?[0-9]+)-W([0-9]{2})"),
    "date": re.compile(r"([-+]?[0-9]+)-([0-9]{2})-([0-9]{2})"),
}
_NANOSECONDS = re.compile(r"(:\d{2}\.\d{6})(\d{1,3})(?!\d)")  # the digits of a fraction after the 6th
_SPAN = re.compile(r"(-)?P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)(?:\.(\d{1,9}))?S)?)?")  # PT1H, P2DT0.000000001S
# Polars' \w, \d and \s are those of Unicode, RE2's (in DuckDB) those of ASCII: what to write for
# them in a step. RE2 has no class for Unicode's Alphabetic: Polars' \w is the letters, the marks,
# Nd, Nl, Pc, the two joiners, and the letters in a circle or a square (Ⓐ, 🄰).
_WORD_CHARS = (
    r"\p{L}\p{Nl}\p{M}\p{Nd}\p{Pc}\x{200C}\x{200D}\x{24B6}-\x{24E9}\x{1F130}-\x{1F149}\x{1F150}-\x{1F169}\x{1F170}-\x{1F189}"
)
_SPACE_CHARS = r"\t\n\x0B\f\r \x{85}\x{A0}\x{1680}\x{2000}-\x{200A}\x{2028}\x{2029}\x{202F}\x{205F}\x{3000}"
_CLASSES = {
    "d": r"\p{Nd}",
    "D": r"\P{Nd}",
    "s": f"[{_SPACE_CHARS}]",
    "S": f"[^{_SPACE_CHARS}]",
    "w": f"[{_WORD_CHARS}]",
    "W": f"[^{_WORD_CHARS}]",
}
_IN_CLASS = {"d": r"\p{Nd}", "D": r"\P{Nd}", "s": _SPACE_CHARS, "w": _WORD_CHARS}  # \S and \W can't be part of a [...]
_REPEAT = re.compile(r"\{\s*\d+\s*(?:,\s*\d*\s*)?\}")  # a counted repetition as Polars reads it, also with spaces
_CODE_POINT = re.compile(r"\\[uU](?:\{([0-9A-Fa-f]+)\}|((?<=u)[0-9A-Fa-f]{4}|(?<=U)[0-9A-Fa-f]{8}))")  # \u{E4}, \u00E4


class NotAStep(ValueError):
    """A filter that the step "Filter rows by formula" can't say: it would keep other rows. The
    text says why, in plain words."""


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
        """A filter as it is stored. A project file can hold anything here: what doesn't have the
        right type counts as not set, so that the window can show such a filter."""
        facet = str(data.get("facet") or "values")
        values, options = data.get("values"), data.get("options")
        f = cls(
            str(data.get("column") or ""),
            [v for v in values if not isinstance(v, (list, dict))] if isinstance(values, list) else [],
            facet if facet in FACETS else "values",
            {k: v for k, v in options.items() if isinstance(v, _OPTIONS.get(k, object))} if isinstance(options, dict) else {},
            data.get("except") is True,
        )
        if f.facet == "period" and f.options.get("by") == "week":  # stored before the year had 4 digits: 24-W18
            f.values = [re.sub(r"^-?\d{1,3}(?=-W)", lambda m: m[0].zfill(4), v) if isinstance(v, str) else v for v in f.values]
        return f

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
    if schema[f.column].is_nested():
        return "a column of lists or records can't be filtered"
    if schema[f.column] == pl.Object:  # e.g. a filter made at a step where the column holds texts
        return "a column of a plugin's own values can't be filtered"
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
    column = col(f.column)
    if f.facet == "period":
        by = f.options.get("by", "month")
        if by == "quarter":
            return pl.format("{} Q{}", column.dt.year(), column.dt.quarter())
        if by == "week":  # the year with 4 digits, as the step's strftime writes it: 0001-W01
            year = column.dt.iso_year().cast(pl.String).str.zfill(4)
            return pl.format("{}-W{}", year, column.dt.week().cast(pl.String).str.zfill(2))
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
        except (TypeError, ValueError, ArithmeticError):
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


def parse_thresholds(text: str) -> list[float]:
    """Numbers typed by the user, separated by ";" or spaces, read as the steps read a number
    typed into a setting (Filter rows, Fill empty values, Replace: ``dtypes.decimal_separator``):
    1.000 is a thousand, 1.5 and 1,5 are one and a half, 1.000,5 and 1,000.5 a thousand and a
    half. What is no number is left out."""
    numbers = []
    for part in re.split(r"[;\s]+", text.strip()):
        number = _number(part) if part else None
        if number is not None and math.isfinite(number):
            numbers.append(number)
    return sorted(set(numbers))


def _number(text: str) -> float | None:
    return dtypes.parse_numbers(pl.Series([text], dtype=pl.String), dtypes.decimal_separator(text))[0]


def threshold_text(shown: str) -> str:
    """A threshold as its field shows it, so that the field reads it back as the same number:
    ``shown`` as the grid shows the number. With a point as the decimal separator, 1.234 would
    be a thousand two hundred and thirty-four there: it is 1.2340."""
    return shown + "0" if dtypes.decimal_separator(shown) == "," and "," not in shown else shown


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
    """A cell value as it is stored in the project file. ``stored_values`` keeps the nanoseconds
    of a column's values, which Python's values don't have."""
    if isinstance(value, (dt.datetime, dt.date, dt.time)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.timedelta):
        return _span_json(value // _MICROSECOND * 1000)
    if isinstance(value, bytes):  # as the step that changes a column's type writes it
        return "0x" + value.hex().upper()
    if isinstance(value, list):
        return [to_json(v) for v in value]
    if isinstance(value, dict):
        return {k: to_json(v) for k, v in value.items()}
    return value


def stored_values(values: pl.Series) -> list[Any]:
    """The cells of a column as a filter stores them (``to_json``), with their nanoseconds:
    08:30:00.000000001 is not 08:30:00, and 1 ns is not 2 ns."""
    dtype = values.dtype
    column = values.alias("value")  # Polars' Series methods would read a name like ^a.*$ as a pattern
    if dtype == pl.Duration:
        factor = _UNIT_NS[_unit(dtype)]
        return [None if count is None else _span_json(count * factor) for count in column.cast(pl.Int64).to_list()]
    if dtype == pl.Datetime and _zone(dtype) is not None:
        return _zoned_texts(column)
    cells = column.to_list()
    if dtype == pl.Time or (dtype == pl.Datetime and _unit(dtype) == "ns"):
        below = (column.dt.nanosecond() % 1000).to_list()  # Python's values end at the microsecond (rounded down)
        return [_with_nanoseconds(cell, extra) if extra else to_json(cell) for cell, extra in zip(cells, below, strict=True)]
    return [to_json(cell) for cell in cells]


def _span_json(nanoseconds: int) -> float | str:
    """A time span as it is stored: seconds as before, or as ISO text (PT0.000000001S) where they
    would lose digits."""
    if nanoseconds % 1000 == 0 and abs(nanoseconds // 1000) < 2**52:  # the microseconds of a float
        return nanoseconds // 1000 / 1_000_000
    sign = "-" if nanoseconds < 0 else ""
    days, rest = divmod(abs(nanoseconds), 86_400 * 10**9)
    hours, rest = divmod(rest, 3_600 * 10**9)
    minutes, rest = divmod(rest, 60 * 10**9)
    seconds, fraction = divmod(rest, 10**9)
    time = (f"{hours}H" if hours else "") + (f"{minutes}M" if minutes else "")
    if seconds or fraction or not (days or time):
        time += str(seconds) + (f".{fraction:09d}".rstrip("0") if fraction else "") + "S"
    return f"{sign}P" + (f"{days}D" if days else "") + (f"T{time}" if time else "")


def _zoned_texts(column: pl.Series) -> list[str | None]:
    """Date-times with a time zone as ISO text with their offset, as Python writes them
    (2025-02-01T00:30:00+01:00), from Polars' own local time and offset. Python's rules of a zone can
    differ from Polars' (America/Vancouver from 2026-11-01): its offset for Polars' local time would
    make the text another moment."""
    factor = _UNIT_NS[_unit(column.dtype)]
    local = column.dt.replace_time_zone(None)  # the local time, by Polars' rules
    frame = pl.DataFrame({"local": local, "offset": (local.cast(pl.Int64) - column.cast(pl.Int64)) * factor // 10**9})
    wall, offset = pl.col("local"), pl.col("offset")
    nanoseconds = wall.dt.nanosecond()
    fraction = (  # as Python writes it: 6 digits when there are microseconds; here 9 with nanoseconds
        pl.when(nanoseconds % 1000 != 0).then(pl.lit(".") + nanoseconds.cast(pl.String).str.zfill(9))
        .when(nanoseconds != 0).then(pl.lit(".") + (nanoseconds // 1000).cast(pl.String).str.zfill(6))
        .otherwise(pl.lit(""))
    )  # fmt: skip
    seconds = offset.abs()
    hours, minutes, rest = (seconds // 3600, seconds % 3600 // 60, seconds % 60)
    offset_text = pl.concat_str(
        pl.when(offset < 0).then(pl.lit("-")).otherwise(pl.lit("+")),
        hours.cast(pl.String).str.zfill(2),
        pl.lit(":"),
        minutes.cast(pl.String).str.zfill(2),
        pl.when(rest != 0).then(pl.lit(":") + rest.cast(pl.String).str.zfill(2)).otherwise(pl.lit("")),
    )  # +01:00, -03:30, +00:53:28
    return frame.select(pl.concat_str(wall.dt.strftime("%Y-%m-%dT%H:%M:%S"), fraction, offset_text)).to_series().to_list()


def _with_nanoseconds(value: dt.datetime | dt.time, nanoseconds: int) -> str:
    """The ISO text of a date-time or time with the nanoseconds below its microseconds:
    2025-01-01T08:30:00.000000001 (Python writes the seconds at a fixed place)."""
    text = value.isoformat(timespec="microseconds")
    end = 26 if isinstance(value, dt.datetime) else 15  # after the microseconds: 08:30:00.000000
    return f"{text[:end]}{nanoseconds:03d}{text[end:]}"


def from_json(values: list[Any], dtype: pl.DataType) -> list[Any]:
    """Stored values (without the empty one) in a column's type; values that don't fit are left out.
    Date-times, times and time spans are whole numbers of the column's time unit (since 1970, in UTC
    with a time zone; since midnight): Python's values have no nanoseconds (see ``_series``)."""
    convert = _temporal(dtype) or (lambda value: _convert(value, dtype))
    converted = []
    for value in values:
        if value is None:
            continue
        try:
            converted.append(convert(value))
        except (TypeError, ValueError, ArithmeticError):
            continue
    return converted


def _temporal(dtype: pl.DataType) -> Callable[[Any], int] | None:
    """For date-times, times and time spans: ``_convert``, with the type looked at once."""
    if dtype == pl.Datetime:
        zoned, unit = _zone(dtype) is not None, _unit(dtype)
        return lambda value: _in_unit(_moment(_not_boolean(value), zoned=zoned), unit)
    if dtype == pl.Time:
        return lambda value: _time_of_day(_not_boolean(value))
    if dtype == pl.Duration:
        unit = _unit(dtype)
        return lambda value: _in_unit(_span_nanoseconds(_not_boolean(value)), unit)
    return None


def _not_boolean(value: Any) -> Any:
    if isinstance(value, bool):  # True is an int in Python: don't let it match 1
        raise ValueError(value)
    return value


def _series(values: list[Any], dtype: pl.DataType) -> pl.Series:
    """Values of ``from_json`` as a Series of the column's type."""
    if dtype in (pl.Datetime, pl.Time, pl.Duration):
        return pl.Series(values, dtype=pl.Int64).cast(dtype)
    return pl.Series(values, dtype=dtype)


def _unit(dtype: Any) -> str:
    """The time unit of a type of date-times or time spans (also of pl.Datetime itself: "us")."""
    return getattr(dtype, "time_unit", None) or "us"


def _zone(dtype: Any) -> str | None:
    return getattr(dtype, "time_zone", None)


def _convert(value: Any, dtype: pl.DataType) -> Any:
    if dtype == pl.String:
        return value if isinstance(value, str) else str(value)
    if dtype == pl.Boolean:
        if isinstance(value, bool):
            return value
        raise ValueError(value)
    if isinstance(value, bool):  # True is an int in Python: don't let it match 1
        raise ValueError(value)
    if dtype == pl.Binary:  # only as to_json writes it: the card compares the stored text with that form
        if not isinstance(value, str) or len(value) % 2 or not re.fullmatch(r"0x[0-9A-F]*", value):
            raise ValueError(value)
        return bytes.fromhex(value[2:])
    if dtype.is_integer():
        number = float(value) if isinstance(value, str) else value
        if not float(number).is_integer():
            raise ValueError(value)
        return int(number)
    if dtype.is_float():
        return float(value)
    if isinstance(dtype, pl.Decimal):
        # quantize refuses what has too many digits or isn't finite. Polars would round the decimal
        # places, and stop at some values with a panic that no except sees.
        number = Decimal(str(value))
        exact = number.quantize(Decimal(1).scaleb(-dtype.scale), context=Context(prec=dtype.precision or 38))
        if exact != number:
            raise ValueError(value)
        return exact
    if dtype == pl.Date:
        return value if isinstance(value, dt.date) else dt.date.fromisoformat(str(value)[:10])
    temporal = _temporal(dtype)
    return value if temporal is None else temporal(value)


def _time_of_day(value: Any) -> int:
    """The nanoseconds since midnight of a stored time of day."""
    if isinstance(value, dt.time):
        text, extra = value.isoformat(), 0
    else:
        text, extra = _without_nanoseconds(str(value))
    time = dt.time.fromisoformat(text)
    if time.tzinfo is not None:  # a time of day of a column has no time zone
        raise ValueError(value)
    return ((time.hour * 60 + time.minute) * 60 + time.second) * 10**9 + time.microsecond * 1000 + extra


def _moment(value: Any, *, zoned: bool) -> int:
    """The nanoseconds since 1970 of a stored date-time (in UTC on a column with a time zone). On
    such a column a date-time without an offset is one in UTC, as Polars reads it; one with an
    offset doesn't fit a column without a time zone."""
    if isinstance(value, dt.datetime):
        moment, extra = value, 0
    else:
        text, extra = _without_nanoseconds(str(value))
        moment = dt.datetime.fromisoformat(text)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC) if zoned else moment
    elif not zoned:
        raise ValueError(value)
    since = moment - (_EPOCH if moment.tzinfo is None else _EPOCH_UTC)
    return since // _MICROSECOND * 1000 + extra


def _without_nanoseconds(text: str) -> tuple[str, int]:
    """An ISO date-time or time without the digits after the 6th of its fraction (Python's values
    end at the microsecond), and those digits as nanoseconds."""
    found = _NANOSECONDS.search(text)
    if found is None:
        return text, 0
    return text[: found.end(1)] + text[found.end(2) :], int(found.group(2).ljust(3, "0"))


def _span_nanoseconds(value: Any) -> int:
    """The nanoseconds of a stored time span: seconds (a number, also as text: "3600", as 0.6.3 read
    it), or ISO text (PT1H30M)."""
    if isinstance(value, dt.timedelta):
        return value // _MICROSECOND * 1000
    if isinstance(value, str):
        found = _SPAN.fullmatch(value)
        if found is None or not any(found.groups()[1:]):
            return int(Decimal(value.strip()).scaleb(9).to_integral_value())  # not a number: an error, the value is left out
        sign, days, hours, minutes, seconds, fraction = found.groups()
        whole = ((int(days or 0) * 24 + int(hours or 0)) * 60 + int(minutes or 0)) * 60 + int(seconds or 0)
        nanoseconds = whole * 10**9 + int((fraction or "").ljust(9, "0"))
        return -nanoseconds if sign else nanoseconds
    return int(Decimal(repr(float(value))).scaleb(9).to_integral_value())  # 1e-06: the digits it was written with


def _in_unit(nanoseconds: int, unit: str) -> int:
    """Nanoseconds as whole numbers of a time unit of Polars; they must fit."""
    count, rest = divmod(nanoseconds, _UNIT_NS[unit])
    if rest or not -(2**63) <= count < 2**63:
        raise ValueError(nanoseconds)
    return count


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
        found = picked.is_in(_series(values, dtype).implode()) if values else pl.lit(False)
        pl.DataFrame(schema={f.column: schema[f.column]}).select(found)  # Polars compares the types only now
    except (TypeError, ValueError, ArithmeticError, RuntimeError, pl.exceptions.PolarsError):
        found = pl.lit(False)
    if None in f.values:
        found = found | picked.is_null()
    return ~found.fill_null(False) if f.excluded else found.fill_null(False)


def apply(df: pl.DataFrame, filters: list[Filter]) -> pl.DataFrame:
    """The rows that pass all filters."""
    conditions = [c for f in filters if (c := condition(f, df.schema)) is not None]
    return df.filter(pl.all_horizontal(conditions)) if conditions else df


def facet(
    df: pl.DataFrame, filters: list[Filter], index: int, rows: pl.DataFrame | None = None
) -> tuple[pl.DataFrame, int] | None:
    """The values filter ``index`` offers (cells, periods, ranges, ...) with how often each
    occurs among the rows that pass the *other* filters (0 = not possible with them), and how
    many rows those are. ``rows``: the rows of ``df`` that passed more filters already. None if
    the filter can't be used with this data."""
    picked = keys(filters[index], df.schema)
    if picked is None:
        return None
    base = apply(df if rows is None else rows, [f for i, f in enumerate(filters) if i != index])
    try:
        values = df.select(picked.alias(VALUE)).unique()
        counts = base.group_by(picked.alias(VALUE)).agg(pl.len().alias(COUNT))
        table = values.join(counts, on=VALUE, how="left", nulls_equal=True)
    except pl.exceptions.PolarsError:
        return None
    return table.with_columns(col(COUNT).fill_null(0)), base.height


# --- as a step ------------------------------------------------------------------------------------


def formula(f: Filter, dtype: pl.DataType, data: pl.Series | None = None) -> str:
    """The filter as a condition for the step "Filter rows by formula" (same rows). ``data``: the
    column at the step: the condition must keep the rows there that the filter keeps. Raises
    NotAStep for a filter that a step can't say the same way, and for values that can't be
    calculated (such as a date-time beyond the years that Polars knows): only NotAStep."""
    try:
        step = _formula(f, dtype, data)
        if data is not None and (_may_differ(f, dtype) or not _exact(f, dtype)):
            _check_rows(f, step, data)
    except NotAStep:
        raise
    except (ArithmeticError, ValueError, TypeError, pl.exceptions.PolarsError):
        raise NotAStep("a step can't say these values") from None
    except BaseException as e:  # a crash inside Polars (Rust) is not an Exception
        if not expressions.is_panic(e):
            raise
        raise NotAStep("a step can't say these values") from None
    return step


def _may_differ(f: Filter, dtype: pl.DataType) -> bool:
    """Whether the step's rows can differ from the card's in a way that no rule here foresees, so
    that _check_rows looks: patterns (DuckDB's tables of Unicode), periods (calendars and the
    rules of zones), date-times with a time zone or with nanoseconds, times of day and time spans.
    Not values, ranges and lengths of texts, numbers, dates and other date-times: DuckDB compares
    these as Polars does, and the check would take as long as the step (a second for 20,000 texts
    on a million rows). Not for more than 1,000 picked values either: the check of such a step takes
    seconds (20,000 date-times with a time zone 2.5), and it writes them as it writes a few."""
    if len(f.values) > _CHECKED_VALUES:
        return False
    if f.facet in ("regex", "period") or dtype in (pl.Time, pl.Duration):
        return True
    return dtype == pl.Datetime and (_zone(dtype) is not None or _unit(dtype) == "ns")


def _exact(f: Filter, dtype: pl.DataType) -> bool:
    """Whether each stored value of the filter is a value of the column's type as it is. A project
    file can hold others: a number on a column of categories, a whole number beyond an Int8, a text
    that is no category of an Enum, a threshold that is no finite number of the column's type, a
    value that is left out. With these the card and a step part ways (the card keeps no row, a step
    keeps some or stops), so _check_rows looks."""
    kind = key_type(f, dtype)
    stored = [value for value in f.values if value is not None]
    try:
        values = from_json(stored, kind)
        if len(values) != len(stored):
            return False
        if values:
            series = _series(values, kind)
            back = series.to_physical().to_list() if kind in (pl.Datetime, pl.Time, pl.Duration) else series.to_list()
            if series.null_count() or not all(_same(a, b) for a, b in zip(back, values, strict=True)):
                return False
        for limit in f.options.get("thresholds") or [] if f.facet == "ranges" else []:
            if isinstance(limit, bool) or not isinstance(limit, (int, float)) or not math.isfinite(limit):
                return False
            if pl.Series([float(limit)]).cast(dtype, strict=True).cast(pl.Float64)[0] != float(limit):
                return False
    except Exception:  # noqa: BLE001 - a value that Polars can't take is no value of the type
        return False
    return True


def _same(a: Any, b: Any) -> bool:
    """Equal, also NaN and NaN."""
    return a == b or (isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b))


def _check_rows(f: Filter, step: str, data: pl.Series) -> None:
    """Whether the step's condition keeps the rows of the card on the column at the step (``data``):
    for what no rule here foresees, such as letters that DuckDB's tables of Unicode don't know yet."""
    frame = pl.DataFrame([data.alias(f.column)])
    card = _card_rows(f, frame)
    try:
        rows = expressions.evaluate(frame, step, condition=True).fill_null(False)
    except ActionError:
        raise NotAStep("the step would stop with an error on these values") from None
    differ = int((card.alias("card") != rows.alias("step")).sum())
    if differ:
        raise NotAStep(f"a step would keep other rows than the filter: {differ:,} of the {frame.height:,} rows here")


def _card_rows(f: Filter, frame: pl.DataFrame) -> pl.Series:
    """The rows that the card keeps (see condition). The period of a date-time is that of its date,
    in its time zone: the periods of a column of date-times are worked out for each date once (the
    texts of a million date-times take 0.25 seconds)."""
    column = frame.get_column(f.column)
    if f.facet == "period" and column.dtype == pl.Datetime:
        dates = column.alias("value").dt.date()  # Polars' Series methods would read a name like ^a.*$ as a pattern
        unique = dates.unique()
        kept = condition(f, pl.Schema({f.column: pl.Date}))
        if kept is not None:
            keeps = pl.DataFrame([unique.alias(f.column)]).select(kept).to_series()
            return dates.is_in(unique.filter(keeps).implode(), nulls_equal=True)
    kept = condition(f, frame.schema)
    return frame.select(kept).to_series() if kept is not None else pl.repeat(True, frame.height, eager=True)


def _formula(f: Filter, dtype: pl.DataType, data: pl.Series | None) -> str:
    """``formula`` without the check of its rows."""
    if not f.values and not f.excluded:  # nothing picked yet: the filter keeps all rows
        return "TRUE"
    name = "[" + f.column.replace("]", "]]") + "]"
    values = from_json(f.values, key_type(f, dtype))
    if f.facet == "values" and dtype == pl.Float32:  # Polars compares them as Float32 values
        values = [_single(v) for v in values]
    if values:
        _check_nanoseconds(f, dtype, values, data)
    parts = []
    if values and f.facet == "ranges":
        limits, inclusive = thresholds(f), f.options.get("lower_inclusive", True)
        if dtype == pl.Float32:  # Polars makes a threshold a Float32 value for such a column
            limits = [_single(limit) for limit in limits]
        column = name
        if isinstance(dtype, pl.Decimal) and (dtype.precision or 38) > 15:
            # Polars compares an exact decimal with a threshold as a correctly rounded Float64; DuckDB's
            # cast of a decimal with more digits than a Float64 holds can miss by one unit, its reading
            # of the text can't
            column = f"CAST(CAST({name} AS VARCHAR) AS DOUBLE)"
        for index in sorted(v for v in values if 0 <= v <= len(limits)):
            parts.append(_range_sql(column, limits, index, inclusive))
    elif values and f.facet == "regex":
        test = f"regexp_matches({name}, {_sql(_step_pattern(str(f.options.get('pattern') or '')))})"
        parts += [test if value == MATCH else f"NOT {test}" for value in values if value in (MATCH, NO_MATCH)]
    elif values and f.facet == "period" and dtype == pl.Datetime and _zone(dtype) is not None:
        # The periods by Polars' rules of the zone, as the card has them: DuckDB's rules of a zone can
        # differ (America/Vancouver from 2026-11-01)
        by, zone = f.options.get("by", "month"), _zone(dtype)
        days = [found for period in values if (found := _period_days(period, by)) is not None]
        runs = _joined(days)  # periods that meet as one: 122 years with 5 left out are 6 runs
        if len(runs) > _FEW_RANGES:
            parts.append(_local_periods_sql(name, by, days, runs, zone))
        elif runs and (ranges := _joined(_period_moments(runs, zone))):
            parts.append(_ranges_sql(name, ranges))
    elif values:
        kind = key_type(f, dtype)
        parts.append(f"{_picked_sql(f, name)} IN ({', '.join(_literal(v, kind) for v in values)})")
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
    if f.facet == "period":  # a column without a time zone: its own time
        by = f.options.get("by", "month")
        if by == "quarter":
            return f"year({name}) || ' Q' || quarter({name})"
        return f"strftime({name}, '{_PERIOD_FORMATS.get(by, '%Y-%m')}')"
    if f.facet == "length":
        return f"length({name})"
    return name


def _joined(ranges: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
    """Ranges (of days or moments) in order, those that meet as one: 300 days in a row are one."""
    joined: list[tuple[int, int]] = []
    for start, end in sorted(ranges):
        if joined and start <= joined[-1][1]:
            joined[-1] = (joined[-1][0], max(joined[-1][1], end))
        else:
            joined.append((start, end))
    return joined


def _ranges_sql(name: str, ranges: list[tuple[float, float]]) -> str:
    """The condition that a moment is in one of ``ranges`` (microseconds, in order, apart; -inf and
    inf beyond the ends of Polars' calendar). More than four (a clock change can split a run of
    periods into three) become a binary search with CASE."""
    if len(ranges) <= 4:
        each = [_range_condition(name, start, end) for start, end in ranges]
        return each[0] if len(each) == 1 else " OR ".join(f"({condition})" for condition in each)
    middle = len(ranges) // 2
    return (
        f"CASE WHEN {name} < {_moment_sql(ranges[middle][0])} THEN {_ranges_sql(name, ranges[:middle])} "
        f"ELSE {_ranges_sql(name, ranges[middle:])} END"
    )


def _range_condition(name: str, start: float, end: float) -> str:
    bounds = [] if start == -math.inf else [f"{name} >= {_moment_sql(int(start))}"]
    bounds += [] if end == math.inf else [f"{name} < {_moment_sql(int(end))}"]
    return " AND ".join(bounds) or f"{name} IS NOT NULL"


def _local_periods_sql(name: str, by: str, days: list[tuple[int, int]], runs: list[tuple[int, int]], zone: str) -> str:
    """More than four runs of periods of a column with a time zone (``days``: the periods, see
    _period_days; ``runs``: those that meet, joined): whether the period of the local day of a
    moment is one of them, by its number (IN, which DuckDB looks up): the day itself, its ISO week,
    or its month, quarter or year (DuckDB's date_diff by months from a 1 January). The local day
    takes the offset of the moment from UTC, which a binary search (CASE) finds among the clock
    changes that Polars knows near the ends of the runs (see _boundary_offsets). On a million rows,
    3,000 dates apart take 0.2 seconds and 170 MB, as ranges of moments 1.9 seconds and 1 GB; the
    text of 122 years in 6 runs has 1 KB."""
    first = runs[0][0] - 10  # earlier by more than any offset: no picked period has this day, nor those before it
    if by == "week":
        origin = first - (first + 3) % 7  # a Monday (1970-01-01 was a Thursday)
    elif by == "date":
        origin = first
    else:
        origin = _days(_civil(first)[0] - 1, 1, 1)  # 1 January of the year before
    offset, changes = _boundary_offsets(runs, zone)
    moment = f"epoch_us({name})"
    if abs(origin) > 9_000_000:  # 25,000 years from 1970: a moment at the other end of the calendar is 2^63 µs away
        moment = f"CAST({moment} AS HUGEINT)"
    local = f"({moment} + {_offset_sql(moment, offset, changes)} - ({origin * _DAY_US})) // {_DAY_US}"  # >= 0 from origin on
    starts = sorted({start for start, _ in days})
    if by == "date":
        index, picked = local, [start - origin for start in starts]
    elif by == "week":
        index, picked = f"{local} // 7", [(start - origin) // 7 for start in starts]
    else:  # DuckDB's date_diff by quarters is wrong before the year 0; by months it is right in all years
        size = {"quarter": 3, "year": 12}.get(by, 1)
        date = _date_text(origin)
        months = f"date_diff('month', DATE '{date}', DATE '{date}' + CAST({local} AS INTEGER))"
        index = months if size == 1 else f"{months} // {size}"
        year = _civil(origin)[0]
        picked = [((civil := _civil(start))[0] - year) * 12 + civil[1] - 1 for start in starts]
        picked = [months_from // size for months_from in picked]
    return f"{index} IN ({', '.join(str(number) for number in sorted(set(picked)))})"


def _boundary_offsets(runs: list[tuple[int, int]], zone: str) -> tuple[int, list[tuple[int, int]]]:
    """The offset of ``zone`` from UTC and its changes (see _clock_changes) where the local day of a
    moment decides whether it is in a run of periods: within five days of an end of a run. Further
    away a moment is more than three local days from every end, and with any offset of the zone
    (they differ by 26 hours at most) its local day stays in its run or gap: 122 years of periods in
    6 runs need no clock change of the zone, as none is near a 1 January."""
    low, high = (_FIRST_DAY + 2) * _DAY_US, (_END_DAY - 2) * _DAY_US  # where Polars knows the local time
    windows: list[tuple[int, int]] = []
    for day in sorted({end for run in runs for end in run}):
        start, stop = max(low, (day - 5) * _DAY_US), min(high, (day + 5) * _DAY_US)
        if start >= stop:
            continue
        if windows and start <= windows[-1][1]:
            windows[-1] = (windows[-1][0], max(windows[-1][1], stop))
        else:
            windows.append((start, stop))
    if not windows:
        return 0, []
    offsets = _offsets(pl.Series([start for start, _ in windows], dtype=pl.Int64), zone).to_list()
    days = [(start, start + ((stop - start) // _DAY_US + 1) * _DAY_US, offset) for (start, stop), offset in zip(windows, offsets, strict=True)]
    entries: list[tuple[int, int]] = []
    for (start, _), offset, changes in zip(windows, offsets, _clock_changes(days, zone, _DAY_US), strict=True):
        entries += [(start, offset), *changes]
    first, kept = entries[0][1], []
    for moment, after in entries[1:]:
        if after != (kept[-1][1] if kept else first):
            kept.append((moment, after))
    return first, kept


def _clock_changes(spans: list[tuple[int, int, int]], zone: str, step: int) -> list[list[tuple[int, int]]]:
    """For each span (start, stop, the offset at start; microseconds since 1970, ``stop - start`` a
    multiple of ``step``): the changes of the clocks of ``zone`` after start until stop, as the
    moment of each and the offset from then on, by Polars' rules. Polars looks at each step of all
    spans at once (a day), then at each hour of the days with a change and at each second of the
    hours with a change: the rules change the clocks at whole seconds, and never twice within a day
    (all zones, 1850 to 2100)."""
    counts = [(stop - start) // step for start, stop, _ in spans]
    moments = [start + k * step for (start, _, _), count in zip(spans, counts, strict=True) for k in range(1, count + 1)]
    offsets = _offsets(pl.Series(moments, dtype=pl.Int64), zone).to_list() if moments else []
    found: list[list[tuple[int, int]]] = []
    finer: list[tuple[int, int, int]] = []  # the steps with a change, for the next pass
    owners: list[int] = []
    index = 0
    for number, ((start, _, offset), count) in enumerate(zip(spans, counts, strict=True)):
        changes: list[tuple[int, int]] = []
        before_moment, before = start, offset
        for moment, after in zip(moments[index : index + count], offsets[index : index + count], strict=True):
            if after != before:
                if step == 1_000_000:
                    changes.append((moment, after))
                else:
                    finer.append((before_moment, moment, before))
                    owners.append(number)
            before_moment, before = moment, after
        index += count
        found.append(changes)
    if finer:
        for owner, changes in zip(owners, _clock_changes(finer, zone, _HOUR_US if step == _DAY_US else 1_000_000), strict=True):
            found[owner] += changes
    return found


def _offsets(moments: pl.Series, zone: str) -> pl.Series:
    """The offsets from UTC of ``zone`` at moments (microseconds since 1970), in microseconds."""
    utc = moments.cast(pl.Datetime("us")).dt.replace_time_zone("UTC")
    return utc.dt.convert_time_zone(zone).dt.replace_time_zone(None).cast(pl.Int64) - moments


def _offset_sql(moment: str, offset: int, changes: list[tuple[int, int]]) -> str:
    """The offset of a moment (``moment`` in SQL, microseconds) that is ``offset`` until the first of
    ``changes``: a binary search with CASE."""
    if not changes:
        return str(offset)
    if len(changes) <= 4:
        whens = []
        for when, after in changes:
            whens.append(f"WHEN {moment} < {when} THEN {offset}")
            offset = after
        return f"CASE {' '.join(whens)} ELSE {offset} END"
    middle = len(changes) // 2
    return (
        f"CASE WHEN {moment} < {changes[middle][0]} THEN {_offset_sql(moment, offset, changes[:middle])} "
        f"ELSE {_offset_sql(moment, changes[middle - 1][1], changes[middle:])} END"
    )


def _period_moments(days: list[tuple[int, int]], zone: str) -> list[tuple[float, float]]:
    """The moments of the card's periods (``days``: see _period_days, also joined) in the time zone
    ``zone``, as ranges of microseconds since 1970 (UTC): from the first moment of a period to the
    first moment of the period after it; -inf and inf at the ends of Polars' calendar. Where a clock
    change soon after midnight turns the clock back to the day before (America/Goose_Bay at 00:01
    until 2011), that day goes on after the first moment of the next one: from the change until
    midnight comes again. Polars works out the moments of all periods at once."""
    walls = sorted({day for pair in days for day in pair})
    bounds = dict(zip(walls, _midnights([day * _DAY_US for day in walls], zone), strict=True))
    ranges = []
    for start, end in days:
        if bounds[start] is None or bounds[end] is None:
            continue
        (first, back, again), (last, back_end, again_end) = bounds[start], bounds[end]
        if back is not None:  # the start: from its first midnight to the change, and from midnight again
            ranges.append((first, back))
            first = again
        ranges.append((first, last))
        if back_end is not None:  # the end: the time before the next midnight comes again
            ranges.append((back_end, again_end))
    return ranges


def _period_days(period: str, by: str) -> tuple[int, int] | None:
    """The first day of a period of the card and the first day of the period after it, as days
    since 1970, in any year (Polars writes 0000, -0001 and +10000); None for a text that is no
    period, and for a period beyond the ends of Polars' calendar (-262143-01-01 to +262142-12-31),
    which only a project file can hold: no value of a column is in it. Not with Python's dates:
    they end at the years 1 and 9999."""
    days = _calendar_days(period, by)
    return days if days is not None and _FIRST_DAY <= days[0] and days[1] <= _END_DAY else None


def _calendar_days(period: str, by: str) -> tuple[int, int] | None:
    """``_period_days`` in all years. Only for a text as the card writes it: a project file can hold
    02026 or 26-03, which no value of a column has as its period, but the year 2026 or 26 does."""
    found = _PERIOD_TEXTS.get(by, _PERIOD_TEXTS["month"]).fullmatch(period)
    if found is None:
        return None
    year, *more = map(int, found.groups())
    if period != _period_text(by, year, *more):
        return None
    if by == "year":
        return _days(year, 1, 1), _days(year + 1, 1, 1)
    if by == "quarter":
        quarter = more[0]
        return _days(year, 3 * quarter - 2, 1), _days(year + quarter // 4, 3 * quarter % 12 + 1, 1)
    if by == "week":
        start = _week_one(year) + 7 * (more[0] - 1)
        return (start, start + 7) if more[0] >= 1 and start < _week_one(year + 1) else None
    month = more[0]
    if not 1 <= month <= 12:
        return None
    if by == "date":
        start = _days(year, month, more[1])
        return (start, start + 1) if _civil(start) == (year, month, more[1]) else None
    return _days(year, month, 1), _days(year + month // 12, month % 12 + 1, 1)


def _period_text(by: str, year: int, *more: int) -> str:
    """A period as the card writes it (Polars): the year 2026, 0026, -0001, +10000; 2026 Q4, 26 Q1;
    2026-W05, -001-W52; 2026-11; 2026-11-30."""
    if by == "quarter":
        return f"{year} Q{more[0]}"
    if by == "week":
        return f"{str(year).zfill(4)}-W{more[0]:02d}"
    text = f"{year:04d}" if 0 <= year <= 9999 else (f"+{year}" if year > 9999 else f"-{-year:04d}")
    return text + "".join(f"-{part:02d}" for part in more)


def _date_text(day: int) -> str:
    """A day (since 1970) as DuckDB reads a DATE, in any year (see _timestamp_text)."""
    return _timestamp_text(day * _DAY_US * 1000).split(" ")[0]


def _days(year: int, month: int, day: int) -> int:
    """The days since 1970-01-01 of a date of the Gregorian calendar, in any year (0 is 1 BC, as
    Polars and DuckDB count). The method of "chrono-Compatible Low-Level Date Algorithms"."""
    year -= month <= 2
    era, year_of_era = divmod(year, 400)
    day_of_year = (153 * ((month + 9) % 12) + 2) // 5 + day - 1  # from 1 March
    return era * 146_097 + year_of_era * 365 + year_of_era // 4 - year_of_era // 100 + day_of_year - 719_468


def _civil(days: int) -> tuple[int, int, int]:
    """The year, month and day of a number of days since 1970-01-01 (see _days)."""
    era, day_of_era = divmod(days + 719_468, 146_097)
    year_of_era = (day_of_era - day_of_era // 1460 + day_of_era // 36_524 - day_of_era // 146_096) // 365
    day_of_year = day_of_era - (365 * year_of_era + year_of_era // 4 - year_of_era // 100)  # from 1 March
    shifted = (5 * day_of_year + 2) // 153
    month = shifted + 3 if shifted < 10 else shifted - 9
    return era * 400 + year_of_era + (month <= 2), month, day_of_year - (153 * shifted + 2) // 5 + 1


def _week_one(year: int) -> int:
    """The Monday of week 1 of an ISO year (the week of 4 January), as days since 1970."""
    fourth = _days(year, 1, 4)
    return fourth - (fourth + 3) % 7  # 1970-01-01 was a Thursday


def _midnights(walls: list[int], zone: str) -> list[tuple[float, int | None, float] | None]:
    """For local times in ``zone`` (microseconds since 1970): the first moment at or after each
    (see _first_moment); where a clock change after it turns the clock back to before it, the
    moment of that change and the moment the local time is reached again; None where Polars can't
    tell. Polars converts them together; those that a clock change skips are looked for one by one."""
    inside = [wall for wall in walls if _FIRST_DAY * _DAY_US < wall < _END_DAY * _DAY_US]
    series = pl.Series(inside, dtype=pl.Int64).cast(pl.Datetime("us"))
    earliest, latest = (
        dict(zip(inside, series.dt.replace_time_zone(zone, ambiguous=which, non_existent="null").cast(pl.Int64).to_list(), strict=True))
        for which in ("earliest", "latest")
    )
    found: list[tuple[float, int | None, float] | None] = []
    for wall in walls:
        if wall not in earliest:  # the first or the last midnight of Polars' calendar: no moment is before or after it
            end = -math.inf if wall <= _FIRST_DAY * _DAY_US else math.inf
            found.append((end, None, end))
            continue
        first, again = earliest[wall], latest[wall]
        if first is None:  # a clock change skips it
            moment = _first_moment(wall, zone)
            found.append(None if moment is None else (moment, None, moment))
        elif again > first and _local_of(again - 1, zone) < wall:  # the clock goes back to before it
            low, high = first, again - 1  # the local time of low is at least wall, that of high before it
            while high - low > 1:
                middle = (low + high) // 2
                low, high = (middle, high) if _local_of(middle, zone) >= wall else (low, middle)
            found.append((first, high, again))
        else:  # once, or twice with the clock between them never before it (Havana: 00:00 to 01:00 twice)
            found.append((first, None, first))
    return found


def _local_of(moment: int, zone: str) -> int:
    """The local time in ``zone`` of a moment (microseconds since 1970, both), by Polars' rules."""
    return pl.Series([moment], dtype=pl.Int64).cast(pl.Datetime("us", zone)).dt.replace_time_zone(None).cast(pl.Int64)[0]


def _first_moment(wall: int, zone: str) -> int | None:
    """The first moment whose local time in ``zone`` (by Polars' rules) is ``wall`` or later (both
    as microseconds since 1970): of a time that a clock change repeats the earlier one, of a time
    that it skips (as midnight in Havana in March) the moment of the change. In microseconds also
    for a column of nanoseconds: Polars' nanoseconds end in the years 1677 and 2262, and there its
    replace_time_zone gives a wrong moment, not an error."""
    if not (_FIRST_DAY + 1) * _DAY_US < wall - _DAY_US < wall + _DAY_US < (_END_DAY - 1) * _DAY_US:
        return None  # a skipped midnight next to the ends of Polars' calendar
    found, before, after = (
        pl.Series([wall, wall - _DAY_US, wall + _DAY_US], dtype=pl.Int64).cast(pl.Datetime("us"))
        .dt.replace_time_zone(zone, ambiguous="earliest", non_existent="null").cast(pl.Int64).to_list()
    )  # fmt: skip
    if found is not None:
        return found
    if before is None or after is None:
        return None
    # The change is after the moment that has local - the later offset, and not after local - the earlier one
    low, high = wall - (wall + _DAY_US - after), wall - (wall - _DAY_US - before)
    while high - low > 1:
        middle = (low + high) // 2
        low, high = (low, middle) if _local_of(middle, zone) >= wall else (middle, high)
    return high if _local_of(high - 1, zone) < wall <= _local_of(high, zone) else None


def _moment_sql(microseconds: int) -> str:
    """A moment (microseconds since 1970, UTC) as a step writes it: TIMESTAMPTZ '2026-12-01
    07:30:00+00:00' (a TIMESTAMP would be in this PC's time zone)."""
    return f"TIMESTAMPTZ '{_timestamp_text(microseconds * 1000)}+00:00'"


def _timestamp_text(nanoseconds: int) -> str:
    """A date-time (nanoseconds since 1970) as DuckDB reads it, in any year: 2026-12-01 07:30:00,
    with 6 digits of a fraction, or 9 for nanoseconds. A year before 1 is written as Polars and
    DuckDB count it (0000 is 1 BC, -0001 2 BC), one after 9999 without a + (10000)."""
    microseconds, below = divmod(nanoseconds, 1000)
    days, rest = divmod(microseconds, _DAY_US)
    seconds, fraction = divmod(rest, 1_000_000)
    year, month, day = _civil(days)
    text = f"{year:04d}" if year >= 0 else f"-{-year:04d}"
    text += f"-{month:02d}-{day:02d} {seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"
    if below:
        return text + f".{fraction:06d}{below:03d}"
    return text + (f".{fraction:06d}" if fraction else "")


def _check_nanoseconds(f: Filter, dtype: pl.DataType, values: list[Any], data: pl.Series | None) -> None:
    """In a step (DuckDB), date-times with a time zone and time spans have only microseconds: it can
    tell their cells apart only where these have no nanoseconds."""
    if dtype == pl.Duration and _unit(dtype) == "ns":
        what = "time spans"
    elif dtype == pl.Datetime and _unit(dtype) == "ns" and _zone(dtype) is not None:
        what = "date-times with a time zone"
    else:
        return
    if f.facet == "values" and any(value % 1000 for value in values):
        raise NotAStep(f"a step compares {what} only to the microsecond, and a picked value has nanoseconds")
    if data is not None and f.facet in ("values", "period"):
        units = data.alias("value").cast(pl.Int64)  # Polars' Series methods would read a name like ^a.*$ as a pattern
        below = (units % 1000) != 0
        if f.facet == "period":  # cut off toward 1970, a moment before 1970 can move into the next day
            below = below & (units < 0)
        if below.any():
            raise NotAStep(f"a step compares {what} only to the microsecond, and some in [{f.column}] have nanoseconds")


def _literal(value: Any, dtype: pl.DataType) -> str:
    """A value of ``from_json`` as a step writes it, in the column's type."""
    if dtype == pl.Datetime:
        nanoseconds = value * _UNIT_NS[_unit(dtype)]
        if _zone(dtype) is not None:  # to the microsecond (see _check_nanoseconds)
            return _moment_sql(nanoseconds // 1000)
        return f"TIMESTAMP{'_NS' if nanoseconds % 1000 else ''} '{_timestamp_text(nanoseconds)}'"
    if dtype == pl.Time:
        microseconds, nanoseconds = divmod(value, 1000)
        time = (dt.datetime.min + dt.timedelta(microseconds=microseconds)).time()
        return f"TIME_NS '{_with_nanoseconds(time, nanoseconds)}'" if nanoseconds else f"TIME '{time.isoformat()}'"
    if dtype == pl.Duration:
        return f"INTERVAL '{_seconds(value * _UNIT_NS[_unit(dtype)] // 1000)} seconds'"
    return _sql(value)


def _seconds(microseconds: int) -> str:
    """Microseconds as seconds, with all digits and no exponent: 0.000001."""
    sign = "-" if microseconds < 0 else ""
    whole, fraction = divmod(abs(microseconds), 1_000_000)
    return sign + str(whole) + (f".{fraction:06d}".rstrip("0") if fraction else "")


def _step_pattern(pattern: str) -> str:
    """A filter's pattern (Polars: \\w, \\d and \\s are the letters, digits and spaces of Unicode)
    for the step's regexp_matches (RE2 in DuckDB: those of ASCII). Raises NotAStep where RE2 can't
    say the same."""
    translated = _re2_pattern(pattern)
    if (problem := _duckdb_problem(f"regexp_matches('', {_sql(translated)})")) is not None:
        if re.search(r"invalid perl operator: \(\?[-a-zA-Z]", problem):  # (?x), (?-u), (?R)
            raise NotAStep("a step knows only the flags i, m, s and U of a pattern, such as (?i)")
        if "bad repetition operator" in problem:  # a{2}{2}, a**
            raise NotAStep("a step can't repeat a repetition at once, such as a{2}{2}: write (a{2}){2}")
        raise NotAStep("a step can't read this pattern as the filter does")
    return translated


def _re2_pattern(pattern: str) -> str:
    """``_step_pattern`` without the check of DuckDB."""
    out: list[str] = []
    i, n = 0, len(pattern)
    in_class = False
    while i < n:
        char = pattern[i]
        if char == "\\" and i + 1 < n:
            letter = pattern[i + 1]
            if letter in "bB<>":
                raise NotAStep(
                    "a step reads \\b and \\B (the start or end of a word) only with the letters A to Z, not with ä or ß"
                )
            if letter in _CLASSES:
                if in_class and letter not in _IN_CLASS:
                    raise NotAStep(f"a step can't use \\{letter} inside [ ] as the filter does: write it outside them")
                out.append(_IN_CLASS[letter] if in_class else _CLASSES[letter])
                i += 2
                continue
            if code := _CODE_POINT.match(pattern, i):  # \u{E4}, \u00E4, \U{E4}: RE2 writes \x{E4}
                out.append("\\x{" + (code.group(1) or code.group(2)) + "}")
                i = code.end()
                continue
            end = i + 2
            if letter in "pPx" and pattern.startswith("{", end):  # \p{Greek}, \x{20AC}: with their braces
                end = pattern.find("}", end) + 1 or n
            out.append(pattern[i:end])
            i = end
            continue
        if in_class:
            if char == "[" and not pattern.startswith("[:", i):
                raise NotAStep("a step can't use a class inside a class, such as [a[b]]")
            if pattern.startswith(("&&", "--", "~~"), i):
                raise NotAStep("a step can't use &&, -- or ~~ inside [ ] as the filter does")
            if char == "[":  # [:alpha:]
                end = pattern.find(":]", i + 2)
                end = n if end < 0 else end + 2
                out.append(pattern[i:end])
                i = end
                continue
            in_class = char != "]"
        elif char == "[":
            in_class = True
            out.append(char)
            i += 1
            for start in ("^", "]"):  # [^...], and a ] right after [ or [^ is the character itself
                if pattern.startswith(start, i):
                    out.append(start)
                    i += 1
            continue
        elif pattern.startswith("(?<", i) and i + 3 < n and (pattern[i + 3].isalpha() or pattern[i + 3] == "_"):
            out.append("(?P<")  # a named group: RE2 writes (?P<name>...)
            i += 3
            continue
        elif char == "{" and (repeat := _REPEAT.match(pattern, i)):  # a{1, 2}: RE2 reads it as text with the spaces
            out.append(re.sub(r"\s+", "", repeat.group()))
            i = repeat.end()
            continue
        out.append(char)
        i += 1
    return "".join(out)


def _duckdb_problem(expression: str) -> str | None:
    """Why DuckDB, which calculates the steps, can't calculate a fixed expression, or None (not
    kept: an interrupt would be kept as a problem)."""
    try:
        expressions.evaluate(pl.DataFrame({"x": [0]}), expression)
    except ActionError as error:
        return str(error).splitlines()[0].removeprefix("Formula error: ")
    return None


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


def _single(value: Any) -> Any:
    """A number as a Float32 value (as a Python float), rounded as Polars rounds it: beyond the
    range of Float32 it is infinite."""
    if not isinstance(value, float):
        return value
    try:
        return struct.unpack("f", struct.pack("f", value))[0]
    except OverflowError:
        return math.copysign(math.inf, value)


def _sql(value: Any) -> str:
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isfinite(value):
        return "'NaN'::DOUBLE" if math.isnan(value) else f"'{'-' if value < 0 else ''}Infinity'::DOUBLE"
    if isinstance(value, float):  # as a DOUBLE: DuckDB reads 0.123 as an exact decimal, whose cast can miss by one unit
        text = repr(value)
        return text if "e" in text else text + "e0"
    if isinstance(value, Decimal):  # without an exponent: DuckDB reads 1.5E-18 as a DOUBLE
        return format(value, "f")
    if isinstance(value, int):
        return str(value)
    if isinstance(value, dt.datetime):  # with a time zone: that moment (a TIMESTAMP drops the offset)
        return f"{'TIMESTAMP' if value.tzinfo is None else 'TIMESTAMPTZ'} '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    if isinstance(value, dt.time):
        return f"TIME '{value.isoformat()}'"
    if isinstance(value, dt.timedelta):  # without an exponent: DuckDB can't read 1e-06 seconds
        return f"INTERVAL '{_seconds(value // _MICROSECOND)} seconds'"
    if isinstance(value, bytes):
        return f"from_hex('{value.hex()}')"
    text = str(value)
    if chr(0) in text:  # DuckDB's reading of the step ends there: it would stop at every run
        raise NotAStep("a step can't write a text with the character U+0000 in it")
    if not text.isascii() and any(0xD800 <= ord(char) <= 0xDFFF for char in text):  # half of a pair
        raise NotAStep("a step can't write a text with a broken character in it")
    return "'" + text.replace("'", "''") + "'"

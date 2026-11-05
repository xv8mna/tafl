"""Detecting, parsing and formatting values: numbers, dates, booleans.

The app uses Excel-style format codes everywhere (``dd.mm.yyyy hh:mm:ss``, ``#,##0.00``).
``to_strftime`` converts date codes for Polars; formats containing ``%`` are strftime already.
Month and day names (``mmm``, ``mmmm``, ``ddd``, ``dddd``) are English. Two-digit years are
read like in Excel: 00-29 are 2000-2029, 30-99 are 1930-1999.

Standard column types in the app: Int64, Float64, String, Date, Datetime, Boolean.
"""

from __future__ import annotations

import datetime as dt
import functools
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal

import polars as pl

from .errors import ActionError
from .names import col

TRUE_WORDS = frozenset({"true", "yes", "y", "ja", "j", "wahr", "x", "1"})
FALSE_WORDS = frozenset({"false", "no", "n", "nein", "falsch", "0"})

_INTEGER = r"^[+-]?(0|[1-9][0-9]{0,18})$"
_LEADING_ZERO = r"^[+-]?0[0-9]+$"  # "01067": an ID or postal code, not the number 1067
_VALUE = pl.col("value")  # see _apply
_PLAIN_NUMBER = r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)$"  # after _number_text
# Thousands separators come after 1 to 3 digits that don't start with 0: 0.500 is not 500.
_NUMBER = {
    ",": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)(,[0-9]+)?$",
    ".": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)(\.[0-9]+)?$",
}
_GROUPED = {
    ",": r"^[+-]?[1-9][0-9]{0,2}(\.[0-9]{3})+(,[0-9]*)?$",
    ".": r"^[+-]?[1-9][0-9]{0,2}(,[0-9]{3})+(\.[0-9]*)?$",
}
_SPACES = r"[   ]"
_CURRENCY = "€$£"

# (strftime format, regex every value must match first). The regex keeps e.g. "01.02.25" from
# being read as the year 25 by "%Y".
_DATE_PATTERNS = (
    ("%Y-%m-%d", r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}$"),
    ("%d.%m.%Y", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4}$"),
    ("%d.%m.%y", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2}$"),
    ("%Y/%m/%d", r"^[0-9]{4}/[0-9]{1,2}/[0-9]{1,2}$"),
)
_DATETIME_PATTERNS = (
    ("%Y-%m-%d %H:%M:%S%.f", r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2} [0-9]{1,2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?$"),
    ("%Y-%m-%dT%H:%M:%S%.f", r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}T[0-9]{1,2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?$"),
    ("%Y-%m-%d %H:%M", r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2} [0-9]{1,2}:[0-9]{2}$"),
    ("%d.%m.%Y %H:%M:%S", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4} [0-9]{1,2}:[0-9]{2}:[0-9]{2}$"),
    ("%d.%m.%Y %H:%M", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{4} [0-9]{1,2}:[0-9]{2}$"),
    ("%d.%m.%y %H:%M:%S", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2} [0-9]{1,2}:[0-9]{2}:[0-9]{2}$"),
    ("%d.%m.%y %H:%M", r"^[0-9]{1,2}\.[0-9]{1,2}\.[0-9]{2} [0-9]{1,2}:[0-9]{2}$"),
)

_EXCEL_EPOCH = dt.datetime(1899, 12, 30)
# The day numbers of the dates that the app shows: the years 1 to 9999 (2958465 is 31.12.9999)
_FIRST_DAY = (dt.datetime(1, 1, 1) - _EXCEL_EPOCH).days
_LAST_DAY = (dt.datetime(9999, 12, 31) - _EXCEL_EPOCH).days
# Quoted text ("Uhr") and backslash escapes are literal, like in Excel formats.
_DATE_TOKEN = re.compile(r'(?i)"[^"]*"|\\.|am/pm|y+|m+|d+|h+|s+|\.0+|.', re.S)
# Number formats: also _x (a space as wide as x), *x (fill with x) and [...] (color, currency).
_NUMBER_TOKEN = re.compile(r'"[^"]*"|\\.|_.|\*.|\[[^\]]*\]|[eE][+-]|.', re.S)
_COLORS = re.compile(r"(?i)black|blue|cyan|green|magenta|red|white|yellow|colou?r ?[0-9]+")
_DECIMAL_CONTEXT = Context(prec=400, rounding=ROUND_HALF_UP)  # room for every float's digits
_INFINITE = (float("inf"), float("-inf"))
# Letters that don't take their base letter apart in Unicode (NFKD): how they sort.
_FOLD = {"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "ł": "l", "þ": "th", "ı": "i"}
# The Latin letters with accents and the combining accents: their sort keys are worked out once.
_LATIN = ((0x80, 0x250), (0x300, 0x370), (0x1E00, 0x1F00))
_SAMPLE = 10_000  # values that tell whether a column has many repeated values


# --- date formats ----------------------------------------------------------------------------


def to_strftime(fmt: str) -> str:
    """Convert an Excel-style date format (``dd.mm.yyyy hh:mm:ss``) to strftime codes.

    ``m``/``mm`` mean minutes right after an hour code or right before a seconds code, otherwise
    month (the Excel rule). Formats that already contain ``%`` are returned unchanged.
    """
    if "%" in fmt:
        return fmt
    tokens = _DATE_TOKEN.findall(fmt)
    kinds = [_token_kind(t) for t in tokens]
    twelve_hour = "ampm" in kinds
    out = []
    for i, (token, kind) in enumerate(zip(tokens, kinds)):
        n = len(token)
        if kind == "y":
            out.append("%Y" if n > 2 else "%y")
        elif kind == "m":
            before = next((k for k in reversed(kinds[:i]) if k), None)
            after = next((k for k in kinds[i + 1 :] if k), None)
            if n <= 2 and (before == "h" or after == "s"):
                out.append("%M")
            else:
                out.append({1: "%-m", 2: "%m", 3: "%b"}.get(n, "%B"))
        elif kind == "d":
            out.append({1: "%-d", 2: "%d", 3: "%a"}.get(n, "%A"))
        elif kind == "h":
            code = "I" if twelve_hour else "H"
            out.append(f"%{code}" if n > 1 else f"%-{code}")
        elif kind == "s":
            out.append("%S")
        elif kind == "ampm":
            out.append("%p")
        elif kind == "fraction":
            out.append(f"%.{n - 1}f")
        elif token.startswith('"') and token.endswith('"') and n >= 2:
            out.append(token[1:-1])
        elif token.startswith("\\") and n == 2:
            out.append(token[1])
        else:
            out.append(token)
    return "".join(out)


def _token_kind(token: str) -> str | None:
    lower = token.lower()
    if lower.startswith(('"', "\\")):
        return None
    if lower == "am/pm":
        return "ampm"
    if lower[0] in "ymdhs" and len(set(lower)) == 1:
        return lower[0]
    if token.startswith(".") and len(token) > 1 and set(token[1:]) == {"0"}:
        return "fraction"
    return None


# --- parsing ---------------------------------------------------------------------------------


def number_expr(expr: pl.Expr, decimal: str = ",") -> pl.Expr:
    """Text -> Float64 expression. Ignores spaces, currency signs and thousands separators,
    understands a trailing minus (``123,45-``). Anything else becomes empty."""
    return _number_text(expr, decimal).cast(pl.Float64, strict=False)


def parse_numbers(s: pl.Series, decimal: str = ",") -> pl.Series:
    """Text -> Float64 (see ``number_expr``)."""
    return _apply(s, number_expr(_VALUE, decimal))


def parse_decimal(text: str, decimal: str = ",") -> Decimal | None:
    """One number typed as text -> an exact decimal (Python's Decimal), e.g. to compare it with
    a Decimal column: it keeps all the digits, ``parse_numbers`` about 15. None when the text
    isn't a number."""
    plain = _apply(pl.Series([text], dtype=pl.String), _number_text(_VALUE, decimal))[0]
    try:
        number = Decimal(plain)
    except (ArithmeticError, TypeError, ValueError):  # not a number
        return None
    return Decimal(format(number, "f")) if number.is_finite() else None  # 1e5 as 100000


def parse_integers(s: pl.Series, decimal: str = ",") -> pl.Series:
    """Text -> Int64, like ``parse_numbers`` but exact for all 19 digits (a Float64 keeps 15).
    Decimals are rounded half away from zero, like in Excel; numbers outside the range of Int64
    become empty."""
    return _apply(_apply(s, _number_text(_VALUE, decimal)), _integer(_VALUE))


def parse_whole_numbers(s: pl.Series, decimal: str = ",") -> tuple[pl.Series, pl.Series]:
    """Text -> Int64 as ``parse_integers``, and which values were rounded (as ``rounded_to_whole``
    says), from one reading of the texts: that reading is the slow part."""
    text = _apply(s, _number_text(_VALUE, decimal)).alias("value")
    out = text.to_frame().select(_integer(_VALUE).alias("whole"), _has_fraction(_VALUE).alias("rounded"))
    rounded = out["rounded"] & out["whole"].is_not_null()  # fixed names: see _clean_text
    return out["whole"].alias(s.name), rounded.alias(s.name)


def _has_fraction(text: pl.Expr) -> pl.Expr:
    """For text from ``_number_text``: whether the number has decimal places other than zeros."""
    number = text.cast(pl.Float64, strict=False)  # e.g. 1.5e0
    plain = text.str.contains(_PLAIN_NUMBER)
    return pl.when(plain).then(text.str.contains(r"\.[0-9]*[1-9]")).otherwise(number.round(0) != number).fill_null(False)


def _integer(text: pl.Expr) -> pl.Expr:
    """``parse_integers`` for text from ``_number_text``."""
    negative = text.str.starts_with("-")
    whole = text.str.replace(r"\.[0-9]*$", "")  # "-12.5" -> "-12", ".5" -> ""
    whole = pl.when(whole.str.contains("[0-9]")).then(whole).otherwise(whole + pl.lit("0"))
    truncated = whole.cast(pl.Int64, strict=False)
    limit = pl.when(negative).then(pl.lit(-(2**63), pl.Int64)).otherwise(pl.lit(2**63 - 1, pl.Int64))
    step = pl.when(negative).then(pl.lit(-1, pl.Int64)).otherwise(pl.lit(1, pl.Int64))
    up = text.str.contains(r"\.[5-9][0-9]*$")
    exact = pl.when(~up).then(truncated).when(truncated != limit).then(truncated + step)
    number = text.cast(pl.Float64, strict=False)  # e.g. 1e5
    rounded = pl.when(number.abs() < 2**53).then(number.round(0, mode="half_away_from_zero").cast(pl.Int64))
    return pl.when(text.str.contains(_PLAIN_NUMBER)).then(exact).otherwise(rounded)


def float_loses_digits(s: pl.Series, decimal: str = ",") -> pl.Series:
    """True for the values whose whole part a decimal number (Float64) can't keep exactly: whole
    numbers beyond 2^53 (9,007,199,254,740,992), as numbers or as text. For exact decimals
    (Decimal), all their decimal places count too."""
    name, s = s.name, s.alias("value")  # a fixed name, as in _clean_text
    if s.dtype.is_integer() or isinstance(s.dtype, pl.Decimal):
        back = s.cast(pl.Float64).cast(s.dtype, strict=False)  # empty when rounded beyond the type: 2^127 for 2^127 - 1
        return ((back != s).fill_null(True) & s.is_not_null()).alias(name)
    lost = pl.repeat(False, s.len(), eager=True).alias(name)
    if s.dtype != pl.String:
        return lost
    rows = (s.str.len_bytes() > 15).fill_null(False).arg_true()  # shorter texts have at most 15 digits
    if rows.len():
        digits = _number_text(_VALUE, decimal).str.extract(r"^[+-]?0*([0-9]+)", 1)
        whole = digits.cast(pl.Int128, strict=False)
        found = (digits.str.len_chars() > 38) | (whole.cast(pl.Float64).cast(pl.Int128, strict=False) != whole)
        lost = lost.scatter(rows, _apply(s.gather(rows), found.fill_null(False)))
    return lost


def rounded_to_whole(s: pl.Series, decimal: str = ",") -> pl.Series:
    """True for the values with decimal places other than zeros (2.5, the text "1.000,5"), which a
    whole number keeps only rounded (``parse_integers`` for text). False for empty values and for
    those that are no number."""
    name, s = s.name, s.alias("value")  # a fixed name, as in _clean_text
    if s.dtype == pl.String:
        rounded = pl.repeat(False, s.len(), eager=True).alias(name)
        rows = s.str.contains_any([".", ",", "e", "E"]).fill_null(False).arg_true()  # the others have no decimal places
        if rows.len():
            text = _apply(s.gather(rows), _number_text(_VALUE, decimal))  # once: the slow part
            rounded = rounded.scatter(rows, _apply(text, _has_fraction(_VALUE)))
        return rounded
    if s.dtype.is_float() or isinstance(s.dtype, pl.Decimal):
        return (s.round(0) != s).fill_null(False).alias(name)  # NaN and infinity are equal to themselves here
    return pl.repeat(False, s.len(), eager=True).alias(name)


def parse_dates(s: pl.Series, fmt: str = "") -> pl.Series:
    """Text -> Date. Without a format, the common formats are recognized (31.12.2025, 31.12.25,
    2025-12-31, 2025/12/31, also with a time). With a format of digits only, such as yyyymmdd, a
    text needs exactly its digits."""
    text = _VALUE
    if fmt:
        code = to_strftime(fmt)
        parsed = _datetime_expr(text, code).dt.date() if _has_time(code) else _date_expr(text, code)
        parsed = _all_digits(parsed, text, fmt)
    else:
        parsed = pl.coalesce(
            [pl.when(text.str.contains(pattern)).then(_date_expr(text, f)) for f, pattern in _DATE_PATTERNS]
            + [
                pl.when(text.str.contains(pattern)).then(_datetime_expr(text, f).dt.date())
                for f, pattern in _DATETIME_PATTERNS
            ]
        )
    return _apply(_clean_text(s), parsed)


def parse_datetimes(s: pl.Series, fmt: str = "", *, time_unit: str = "us") -> pl.Series:
    """Text -> Datetime. Without a format, the common formats are recognized (see
    ``parse_dates``). ``time_unit`` "ns" keeps nanoseconds (2025-12-31 08:30:00.000000001)."""
    text = _VALUE
    if fmt:
        code = to_strftime(fmt)
        parsed = _datetime_expr(text, code, time_unit) if _has_time(code) else _date_expr(text, code).cast(pl.Datetime(time_unit))
        parsed = _all_digits(parsed, text, fmt)
    else:
        parsed = pl.coalesce(
            [pl.when(text.str.contains(pattern)).then(_datetime_expr(text, f, time_unit)) for f, pattern in _DATETIME_PATTERNS]
            + [
                pl.when(text.str.contains(pattern)).then(_date_expr(text, f).cast(pl.Datetime(time_unit)))
                for f, pattern in _DATE_PATTERNS
            ]
        )
    return _apply(_clean_text(s), parsed)


def parse_times(s: pl.Series) -> pl.Series:
    """Text -> Time: 08:30, 08:30:15 or 08:30:15.250. Others become empty."""
    parsed = pl.coalesce(_VALUE.str.to_time("%H:%M:%S%.f", strict=False), _VALUE.str.to_time("%H:%M", strict=False))
    return _apply(_clean_text(s), parsed)


def parse_booleans(s: pl.Series) -> pl.Series:
    """Text -> Boolean: true/yes/ja/x/1 and false/no/nein/0 (any case). Others become empty."""
    mapping = {w: True for w in TRUE_WORDS} | {w: False for w in FALSE_WORDS}
    text = s.cast(pl.String).alias("text").str.strip_chars().str.to_lowercase()  # a fixed name, as in _clean_text
    return text.replace_strict(mapping, default=None, return_dtype=pl.Boolean).alias(s.name)


def excel_serial_to_datetime(s: pl.Series) -> pl.Series:
    """Excel day numbers (45658 = 2025-01-01, fractions = time of day) -> Datetime. A day number
    whose date is not in the years 1 to 9999 (after 2958465, which is 31.12.9999), NaN and
    infinity become empty."""
    days = s.alias("days").cast(pl.Float64)  # a fixed name, as in _clean_text
    ms = (pl.col("days") * 86_400_000).round(0)
    inside = ms.is_between(_FIRST_DAY * 86_400_000, (_LAST_DAY + 1) * 86_400_000, closed="left")  # not NaN or inf
    moment = pl.lit(_EXCEL_EPOCH) + pl.duration(milliseconds=pl.when(inside).then(ms).cast(pl.Int64))
    return days.to_frame().select(moment.alias(s.name)).to_series()


def numbers_to_datetimes(s: pl.Series, fmt: str = "") -> pl.Series:
    """Numbers -> Datetime. With a date format of digits only (yyyymmdd, ddmmyyyy, yymmdd,
    yyyymmddhhmmss), the digits of each whole number are read with it, as the text of the digits
    would be: 20260131 is 31.01.2026. A number with fewer digits than such a format lost its
    leading zeros, and gets them back (1012026 is 01012026 for ddmmyyyy), unless the format starts
    with a four-digit year. Decimal numbers that are whole count as whole (20260131.0); others
    can't be read with a format and become empty. With another format or none, the numbers are
    Excel day numbers (``excel_serial_to_datetime``)."""
    width = _digit_format(fmt)
    if width is None:
        return excel_serial_to_datetime(s)
    name, s = s.name, s.alias("value")  # a fixed name, as in _clean_text
    text = text_expr(_VALUE, s.dtype).str.replace(r"\.0+$", "")  # 20260131.00 of an exact decimal: its digits
    if width:
        text = pl.when(text.str.contains("^[0-9]+$")).then(text.str.pad_start(width, "0")).otherwise(text)
    return parse_datetimes(_apply(s, text), fmt).alias(name)


def is_digit_format(fmt: str) -> bool:
    """Whether ``numbers_to_datetimes`` reads the digits of numbers with this date format (one of
    digit codes only, such as yyyymmdd), rather than taking them as Excel day numbers."""
    return _digit_format(fmt) is not None


def _digit_format(fmt: str) -> int | None:
    """For a date format of digit codes only (yyyy, yy, mm, dd, hh, ss): the number of digits a
    number is padded to with leading zeros, or 0 for none (a four-digit year first can't have lost
    a zero). None for a format with anything else, such as a separator, a month name or a strftime
    code; one with d or m instead of dd or mm is an ActionError (see ``_digit_codes``)."""
    tokens = _digit_codes(fmt)
    if tokens is None:
        return None
    return 0 if tokens[0].lower() == "yyyy" else _digit_width(tokens)


def _digit_codes(fmt: str) -> list[str] | None:
    """The codes of a date format of digit codes only, or None (see ``_digit_format``). Without
    separators, a code such as d (one or two digits) can't be told apart from the next one: such a
    format is an ActionError (1012026 with dmyyyy would be 10.12.0026)."""
    if not fmt or "%" in fmt:
        return None
    tokens = _DATE_TOKEN.findall(fmt)
    for token in tokens:
        kind, length = _token_kind(token), len(token)
        if kind not in ("y", "m", "d", "h", "s") or (kind != "y" and length > 2):
            return None
    if _digit_width(tokens) is None:
        raise ActionError(
            f"The date format “{fmt}” has no separators, so each part needs all its digits: use dd and mm, "
            "not d and m (and hh, ss and yyyy or yy), e.g. ddmmyyyy"
        )
    return tokens


def _digit_width(tokens: list[str]) -> int | None:
    """How many digits these codes read, when that is fixed (yyyy, yy, mm, dd, hh, ss: 8 for
    yyyymmdd); None when a code such as d reads one or two."""
    if all(len(token) in ((2, 4) if token[0].lower() == "y" else (2,)) for token in tokens):
        return sum(len(token) for token in tokens)
    return None


def _all_digits(parsed: pl.Expr, text: pl.Expr, fmt: str) -> pl.Expr:
    """``parsed``, but empty for a text that hasn't exactly the digits of a date format of digit
    codes only, such as 8 for yyyymmdd: Polars reads 1012026 with ddmmyyyy as 10.12.0026, and a
    text can't have lost leading zeros."""
    tokens = _digit_codes(fmt)
    width = None if tokens is None else _digit_width(tokens)
    return parsed if width is None else pl.when(text.str.contains(f"^[0-9]{{{width}}}$")).then(parsed)


# --- one type for the values of several columns ----------------------------------------------

_UNITS = ("ns", "us", "ms")  # of date-times and durations, the finest first
_NARROW_WHOLE = (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32)  # whole numbers that Int64 holds


@dataclass(frozen=True)
class CommonType:
    """The type in which the values of several columns go into one column (``common_type``)."""

    dtype: pl.DataType | None  # None: lists, records or objects beside another type
    counted: tuple[bool, ...]  # for each column, whether its type counted (if not, its values are all empty)
    lost: int | None = None  # with text: the first column whose values the best common type would change


def common_type(columns: list[pl.Series]) -> CommonType:
    """The type for the values of these columns in one column, as Unpivot puts them into its value
    column and Append puts tables below each other. Columns of one type keep it. Whole numbers of
    other types become whole numbers (Int64), or beyond them exact decimals of up to 38 digits.
    Whole and decimal numbers become decimal numbers, whole numbers and exact decimals exact
    decimals, dates and date-times of one time zone date-times, durations durations (each in the
    finest unit that holds every value), when every value stays as it is. Any other mix becomes
    text, as ``to_common_type`` writes it: the same text on every PC. A column whose values are
    all empty doesn't count, whatever its type; when no column has a value (a table without
    rows), the types count, except Null. Lists, records and objects have no common type with
    another type (``dtype`` is None): they have no text."""
    columns = [s.alias("value") for s in columns]  # Polars' Series methods would read a name like ^a.*$ as a pattern
    has_values = [s.null_count() < s.len() for s in columns]
    counted = tuple(has_values) if any(has_values) else tuple(s.dtype != pl.Null for s in columns)
    types = list(dict.fromkeys(s.dtype for s, counts in zip(columns, counted, strict=True) if counts))
    if len(types) <= 1:
        return CommonType(types[0] if types else pl.Null, counted)
    if any(t.is_nested() or t == pl.Object for t in types):
        return CommonType(None, counted)
    first_lost = None
    for common in _common_types(types):
        changed = (i for i, s in enumerate(columns) if counted[i] and s.dtype != common and not _keeps_values(s, common))
        lost = next(changed, None)
        if lost is None:
            return CommonType(common, counted)
        first_lost = lost if first_lost is None else first_lost
    return CommonType(pl.String, counted, first_lost)


def to_common_type(expr: pl.Expr, dtype: pl.DataType, target: pl.DataType, *, text_bytes: bool = False) -> pl.Expr:
    """``expr``, of type ``dtype``, in the type that ``common_type`` gave: as text, the same text on
    every PC (``text_expr``: 2025-12-31, 2.5, 1d 2h 3m), and binary values with ``text_bytes``
    (they are all text, see ``bytes_are_text``) as that text; a date as midnight of a date-time's
    time zone (a plain cast would take midnight in UTC); else empty where a value doesn't fit."""
    if target == pl.String:
        return expr.cast(pl.String) if dtype == pl.Binary and text_bytes else text_expr(expr, dtype)
    if dtype == pl.Date and isinstance(target, pl.Datetime) and target.time_zone:  # 9999-12-31 is beyond ns: empty
        local = expr.cast(pl.Datetime(target.time_unit), strict=False)
        return local.dt.replace_time_zone(target.time_zone, ambiguous="earliest", non_existent="null")
    return expr.cast(target, strict=False)


def type_word(dtype: pl.DataType) -> str:
    """The word for a type in a message about columns of different types: ``type_label``, but an
    exact decimal and a date-time with a time zone are told apart ("date-time (UTC)")."""
    if isinstance(dtype, pl.Decimal):
        return "exact decimal"
    if isinstance(dtype, pl.Datetime) and dtype.time_zone:
        return f"date-time ({dtype.time_zone})"
    return type_label(dtype)


def with_article(text: str) -> str:
    """``text`` with "a" or "an" before it, for a message: "a date column", "an integer column",
    "an empty column"."""
    return f"{'an' if text[:1].lower() in tuple('aeiou') else 'a'} {text}"


def stack(frames: list[pl.DataFrame], warn: Callable[[str], None] | None = None, sources: str = "tables") -> pl.DataFrame:
    """The frames one below the other, their columns matched by name (a frame without a column
    gets empty values in it). A column of other types in other frames gets the type that
    ``common_type`` gives, in each of them, and ``warn`` is told which columns became text, with
    ``sources`` for the frames in its words ("tables", "files", "sheets"): up to three columns,
    then how many more. A list, record or object column beside another type is an
    ActionError. Each column is converted by its own name: Polars' own concat with types reads a
    name like ^a.*$ as a pattern."""
    schemas = [f.schema for f in frames]
    changes: list[list[pl.Expr]] = [[] for _ in frames]
    texts = []  # the columns that became text: (name, the words of their types, whether a value would change)
    for name in dict.fromkeys(c for f in frames for c in f.columns):
        where = [i for i, schema in enumerate(schemas) if name in schema]
        if len({schemas[i][name] for i in where}) == 1:
            continue
        common = common_type([frames[i][name] for i in where])
        counted = [schemas[i][name] for i, counts in zip(where, common.counted, strict=True) if counts]
        words = ", ".join(dict.fromkeys(type_word(dtype) for dtype in counted))
        if common.dtype is None:
            hint = " Remove the column from one of the tables before this step." if sources == "tables" else ""
            raise ActionError(
                f"“{name}” has different types in the {sources} ({words}): lists and records can't be in one column "
                f"with other values.{hint}"
            )
        if common.dtype == pl.String and not all(_is_text(dtype) for dtype in counted):  # categories are text too
            texts.append((name, words, common.lost is not None))
        # Bytes that are all text become that text, as in Clean text, Join texts and Merge: decided once
        # for the column, so that one value that is no text makes all of them hex digits
        binary = [i for i, counts in zip(where, common.counted, strict=True) if counts and schemas[i][name] == pl.Binary]
        text_bytes = all(bytes_are_text(frames[i][name].alias("value")) for i in binary)
        for i, counts in zip(where, common.counted, strict=True):
            if schemas[i][name] != common.dtype:  # a column with only empty values: empty values of that type
                if counts:
                    change = to_common_type(col(name), schemas[i][name], common.dtype, text_bytes=text_bytes)
                else:
                    change = pl.lit(None, common.dtype)
                changes[i].append(change.alias(name))
    if warn is not None:
        for name, words, lost in texts[:3]:
            if lost:  # e.g. whole numbers beyond 2^53 beside decimal numbers
                warn(f"“{name}” is text, so that none of its values changes ({words})")
            else:
                warn(f"“{name}” is text: it has different types in the {sources} ({words})")
        if len(texts) > 3:
            more = ", ".join(f"“{name}”" for name, *_ in texts[3:6]) + (", …" if len(texts) > 6 else "")
            if len(texts) == 4:
                warn(f"1 more column is text, as its types differ between the {sources}: {more}")
            else:
                warn(f"{len(texts) - 3:,} more columns are text, as their types differ between the {sources}: {more}")
    frames = [f.with_columns(change) if change else f for f, change in zip(frames, changes, strict=True)]
    return pl.concat(frames, how="diagonal")


def _is_text(dtype: pl.DataType) -> bool:
    """Text, also categories (Parquet's dictionary columns, a plugin's Enum): as text they stay as they are."""
    return dtype in (pl.String, pl.Categorical) or isinstance(dtype, pl.Enum)


def _common_types(types: list[pl.DataType]) -> list[pl.DataType]:
    """Types that can hold the values of all these types (if the values fit), the best first."""
    if all(t.is_integer() for t in types):  # beyond Int64, whole numbers keep their digits as exact decimals
        return [pl.Int64, pl.Decimal(38, 0)]
    if all(t.is_integer() or t.is_float() for t in types):
        return [pl.Float64]
    if all(t.is_integer() or isinstance(t, pl.Decimal) for t in types):
        return [pl.Decimal(38, max(t.scale for t in types if isinstance(t, pl.Decimal)))]
    # Dates and date-times of one time zone, or durations: date-times (durations) in the finest
    # unit of the columns, else in a coarser one (a nanosecond one ends in the year 2262)
    times = [t for t in types if t != pl.Date]
    if all(isinstance(t, pl.Datetime) for t in times) and len({t.time_zone for t in times}) == 1:
        return [pl.Datetime(unit, times[0].time_zone) for unit in _coarser(times)]
    if all(isinstance(t, pl.Duration) for t in types):
        return [pl.Duration(unit) for unit in _coarser(types)]
    return []


def _coarser(types: list[pl.DataType]) -> tuple[str, ...]:
    """The finest unit of these date-times or durations, and the coarser ones."""
    return _UNITS[min(_UNITS.index(t.time_unit) for t in types) :]


def _keeps_values(s: pl.Series, target: pl.DataType) -> bool:
    """Whether the values of ``s`` (named "value") come back unchanged from ``target``."""
    if s.dtype.is_float() and target == pl.Float64:
        return True
    if s.dtype.is_integer() and target == pl.Int64 and s.dtype in _NARROW_WHOLE:
        return True
    if s.dtype.is_integer() and (target in (pl.Float64, pl.Int64) or isinstance(target, pl.Decimal)):  # by their range
        low, high = s.min(), s.max()
        if target == pl.Float64:  # all whole numbers up to 2^53 are exact
            limits = (-(2**53), 2**53)
        elif target == pl.Int64:
            limits = (-(2**63), 2**63 - 1)
        else:  # the digits before the decimal point
            limits = (1 - 10 ** (target.precision - target.scale), 10 ** (target.precision - target.scale) - 1)
        return low is None or (limits[0] <= low and high <= limits[1])
    back = to_common_type(pl.col("value"), s.dtype, target)
    back = back.dt.date() if s.dtype == pl.Date else back.cast(s.dtype, strict=False)
    return s.to_frame().select((pl.col("value").is_null() | (back == pl.col("value")).fill_null(False)).all()).item()


# --- detecting types -------------------------------------------------------------------------


def infer_types(df: pl.DataFrame, decimal: str = ",", warn: Callable[[str], None] | None = None) -> pl.DataFrame:
    """Turn text columns into whole numbers, decimal numbers, dates, date-times or booleans when
    every value fits. Columns with leading zeros (IDs, postal codes) stay text, and so do numbers
    too long to be kept exactly (``warn`` is told about these)."""
    return df.with_columns([infer_series(df[name], decimal, warn) for name in df.columns])


def infer_series(s: pl.Series, decimal: str = ",", warn: Callable[[str], None] | None = None) -> pl.Series:
    if s.dtype != pl.String:
        return s
    text = _clean_text(s)
    values = text.alias("value").drop_nulls()  # a fixed name, as in _clean_text
    if values.len() == 0 or values.str.contains(_LEADING_ZERO).any():
        return s
    if values.str.contains(_INTEGER).all():
        whole = text.cast(pl.Int64, strict=False)
        if whole.null_count() == text.null_count():
            return whole
        return _too_long(s, values.filter(values.cast(pl.Int64, strict=False).is_null()), warn)
    if values.str.contains(_NUMBER[decimal]).all():
        lost = float_loses_digits(values, decimal)
        if lost.any():
            return _too_long(s, values.filter(lost), warn)
        return parse_numbers(text, decimal)
    for fmt, pattern in _DATE_PATTERNS:
        if values.str.contains(pattern).all():
            parsed = _apply(text, _date_expr(_VALUE, fmt))
            if parsed.null_count() == text.null_count():
                return parsed
    for fmt, pattern in _DATETIME_PATTERNS:
        if values.str.contains(pattern).all():
            parsed = _apply(text, _datetime_expr(_VALUE, fmt))
            if parsed.null_count() == text.null_count():
                if "%.f" in fmt and values.str.contains(_BELOW_MICROSECONDS).any():
                    return _with_nanoseconds(s.name, text, values, parsed, fmt, warn)
                return parsed
    if values.str.to_lowercase().is_in(["true", "false"]).all():
        return _apply(text, _VALUE.str.to_lowercase() == "true")
    return s


_BELOW_MICROSECONDS = r"\.[0-9]{6}[0-9]*[1-9]"  # a fraction of a second with digits below a microsecond
_BELOW_NANOSECONDS = r"\.[0-9]{9}[0-9]*[1-9]"
# Nanoseconds reach from 1677-09-21 to 2262-04-11 (Int64): those date-times as microseconds, with
# room for the digits below them
_NANOSECOND_YEARS = 9_223_372_036_854_774


def _with_nanoseconds(
    name: str, text: pl.Series, values: pl.Series, parsed: pl.Series, fmt: str, warn: Callable[[str], None] | None
) -> pl.Series:
    """Date-time texts with digits below a microsecond (``parsed``: as microseconds): a column of
    nanoseconds that keeps them, when all its date-times are within the years of nanoseconds; else
    the microseconds, and ``warn`` is told. Polars doesn't refuse a date-time beyond those years as
    nanoseconds: it would give another one, so the years are checked first."""
    micros = parsed.cast(pl.Int64)
    if -_NANOSECOND_YEARS <= micros.min() and micros.max() <= _NANOSECOND_YEARS:
        exact = _apply(text, _datetime_expr(_VALUE, fmt, "ns"))
        if exact.null_count() == text.null_count():
            cut = values.filter(values.str.contains(_BELOW_NANOSECONDS))
            if cut.len() and warn is not None:
                warn(f"{name}: the digits below a nanosecond were cut, e.g. in '{cut[0]}'")
            return exact
    if warn is not None:
        example = values.filter(values.str.contains(_BELOW_MICROSECONDS))[0]
        warn(
            f"{name}: the digits below a microsecond were cut, e.g. in '{example}': nanoseconds fit only from 1677 to "
            "2262, and the column has date-times beyond"
        )
    return parsed


def whole_numbers_to_int(df: pl.DataFrame) -> pl.DataFrame:
    """Float columns holding only whole numbers become Int64 (Excel stores all numbers as floats)."""
    updates = []
    for name, dtype in df.schema.items():
        if dtype.is_float():
            values = df[name].alias("value").drop_nulls()  # a fixed name, as in _clean_text
            if (
                values.len()
                and not values.is_nan().any()
                and values.abs().max() < 2**53
                and (values == values.floor()).all()
            ):
                updates.append(df[name].cast(pl.Int64))
    return df.with_columns(updates)


def midnight_to_date(df: pl.DataFrame) -> pl.DataFrame:
    """Datetime columns whose values are all at 00:00:00 become Date."""
    updates = []
    for name, dtype in df.schema.items():
        if dtype == pl.Datetime:
            column = df[name].alias("value")  # a fixed name, as in _clean_text
            values = column.drop_nulls()
            if values.len() and (values.dt.truncate("1d") == values).all():
                updates.append(column.dt.date().alias(name))
    return df.with_columns(updates)


def type_label(dtype: pl.DataType) -> str:
    """The word for a type that the app shows: text, integer, number, date, ... Categories
    (Polars' Categorical and Enum) are "text", as the grid shows them; lists of any length are
    "list", records (Parquet's structs) "record" and a plugin's own values "object"."""
    if dtype == pl.String or isinstance(dtype, (pl.Categorical, pl.Enum)):
        return "text"
    if dtype.is_integer():
        return "integer"
    if dtype.is_float() or isinstance(dtype, pl.Decimal):
        return "number"
    if dtype == pl.Date:
        return "date"
    if dtype == pl.Datetime:
        return "date-time"
    if dtype == pl.Time:
        return "time"
    if dtype == pl.Boolean:
        return "true/false"
    if dtype == pl.Duration:
        return "duration"
    if dtype == pl.Null:
        return "empty"
    if isinstance(dtype, (pl.List, pl.Array)):
        return "list"
    if isinstance(dtype, pl.Struct):
        return "record"
    if dtype == pl.Object:
        return "object"
    return str(dtype).lower()


def text_expr(expr: pl.Expr, dtype: pl.DataType) -> pl.Expr:
    """The values of ``expr`` (of type ``dtype``) as text that is the same on every PC: text as it
    is, 1234, 1.5 and 2 (not 2.0), 5.00 for an exact decimal, 2025-12-31, 2025-12-31 08:30:00
    (fractions of a second only when there are some; a time zone's local time, without the
    offset), 08:30:00.500, 1d 2h 3m, true and false, 0x01FF for binary values. Decimal numbers
    keep every digit that tells them apart (the shortest text that reads back as the same number:
    0.1, 0.30000000000000004), without an exponent (1e16 is 10000000000000000)."""
    if dtype == pl.String:
        return expr
    if dtype.is_float():
        return expr.map_batches(_float_texts, return_dtype=pl.String, is_elementwise=True)
    if dtype == pl.Datetime:
        return expr.dt.to_string("%Y-%m-%d %H:%M:%S%.f")
    if dtype == pl.Time:
        return expr.dt.to_string("%H:%M:%S%.f")
    if dtype == pl.Duration:
        return expr.dt.to_string("polars")
    if dtype == pl.Binary:  # as hex digits, as Change type writes them (and keeping the name of expr)
        return expr.bin.encode("hex").str.to_uppercase().str.replace("^", "0x")
    return expr.cast(pl.String)


def _float_texts(s: pl.Series) -> pl.Series:
    """Decimal numbers as text (see ``text_expr``). Polars writes the shortest text that reads
    back as the number (a Float32 value with its own digits: 0.1, not 0.10000000149011612); those
    with an exponent are written again with the same digits, without it: -1.5e-7 is -0.00000015,
    1e+16 is 10000000000000000."""
    text = s.cast(pl.String).alias("text")  # Polars' Series methods would read a name like ^a.*$ as a pattern
    rows = text.str.contains("e", literal=True).fill_null(False).arg_true()  # NaN and inf have no "e"
    if rows.len():
        number = text.gather(rows).str.split_exact("e", 1).struct.rename_fields(["mantissa", "exponent"])
        mantissa = pl.col("mantissa")
        digits = mantissa.str.strip_chars_start("-").str.replace(".", "", literal=True)
        point = pl.col("exponent").cast(pl.Int64) + 1  # digits before the point: 1 + the exponent
        zeros = (1 - point).clip(lower_bound=0)  # in front, so that there is a digit before the point
        point = point + zeros
        digits = digits.str.pad_start(digits.str.len_bytes() + zeros, "0").str.pad_end(point, "0")
        sign = pl.when(mantissa.str.starts_with("-")).then(pl.lit("-")).otherwise(pl.lit(""))
        written = pl.concat_str(sign, digits.str.slice(0, point), pl.lit("."), digits.str.slice(point))
        text = text.scatter(rows, number.struct.unnest().select(written.str.strip_chars_end(".")).to_series())
    return text.str.strip_suffix(".0").replace("-0", "0").alias(s.name)


def bytes_are_text(values: pl.Series) -> bool:
    """Whether all these binary values are UTF-8 text. Where binary values are used as text, a
    column with bytes that are no text gives hex digits instead (0xFF00, as ``text_expr`` writes
    them)."""
    try:
        values.cast(pl.String)
    except pl.exceptions.ComputeError:
        return False
    return True


def normalize(s: pl.Series, *, exact: bool = False) -> pl.Series:
    """Map other types to the app's standard ones (Decimal -> Int64/Float64, Int32 -> Int64, ...).

    Whole numbers, also decimals (Decimal) without decimal places, become Int64 when every value
    fits; else they keep all their digits as exact decimals (Decimal), or as text beyond their 38
    digits. With ``exact``, the other decimals keep all their digits too (they stay Decimal);
    without it they become Float64 (a formula's 1.5 * 2)."""
    dtype = s.dtype
    if isinstance(dtype, pl.Decimal) and dtype.scale:
        return s if exact else s.cast(pl.Float64)
    if isinstance(dtype, pl.Decimal) or dtype in (pl.Int128, pl.UInt128):
        try:
            return s.cast(pl.Int64)
        except Exception:  # does not fit into Int64
            return _exact_whole_numbers(s)
    if dtype.is_integer() and dtype not in (pl.Int64, pl.UInt64):
        return s.cast(pl.Int64)
    if dtype == pl.Float32:
        return s.cast(pl.Float64)
    if dtype in (pl.Null, pl.Categorical) or isinstance(dtype, pl.Enum):
        return s.cast(pl.String)
    return s


def _exact_whole_numbers(s: pl.Series) -> pl.Series:
    """Whole numbers beyond Int64 as exact decimals, or as text when one has more than their 38
    digits (a Decimal from DuckDB's HUGEINT can hold 39)."""
    exact = (s.cast(pl.Int128) if isinstance(s.dtype, pl.Decimal) else s).cast(pl.Decimal(38, 0), strict=False)
    if exact.null_count() > s.null_count():
        return s.cast(pl.String)
    return s if isinstance(s.dtype, pl.Decimal) else exact


# --- sorting ---------------------------------------------------------------------------------


def text_sort_key(texts: pl.Expr | pl.Series) -> pl.Expr | pl.Series:
    """A key that sorts text like Excel: upper and lower case count the same, letters with
    accents sort with their base letter (ä with a, é with e), ß like ss. Sort with
    ``maintain_order=True`` so that values with the same key keep their order.

    For an expression the key is an expression, for a Series a Series (that one is worked out
    right away)."""
    if isinstance(texts, pl.Series):
        return _text_keys(texts)
    return texts.map_batches(_text_keys, return_dtype=pl.String, is_elementwise=True)


def _text_keys(s: pl.Series) -> pl.Series:
    name, s = s.name, s.alias("text")  # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    if s.len() > 2 * _SAMPLE:
        sample = s.gather_every(s.len() // _SAMPLE)
        if 2 * sample.n_unique() < sample.len():  # many repeated values: each one is worked out once
            values = s.unique().drop_nulls()
            return s.replace_strict(values, _keys(values), default=None, return_dtype=pl.String).alias(name)
    return _keys(s).alias(name)


def _keys(s: pl.Series) -> pl.Series:
    """The keys of ``text_sort_key``. Plain ASCII only needs lower case and most Latin letters a
    table: only the other texts go through Unicode's decomposition, the slow part."""
    keys = s.str.to_lowercase()
    others = _not_ascii(s)
    if others.len():
        latin = keys.gather(others).str.replace_many(_latin_keys())
        rest = _not_ascii(latin)
        if rest.len():
            latin = latin.scatter(rest, _unicode_keys(s.gather(others).gather(rest)))
        keys = keys.scatter(others, latin)
    return keys


def _unicode_keys(s: pl.Series) -> pl.Series:
    folded = s.str.normalize("NFKD").str.to_lowercase().str.replace_all(r"\p{M}+", "")
    return folded.str.replace_many(list(_FOLD), list(_FOLD.values()))


@functools.cache
def _latin_keys() -> dict[str, str]:
    """The characters of ``_LATIN`` whose key is plain ASCII (é: e, Ł: l, ß: ss, an accent: none),
    with that key. A character's key doesn't depend on the characters around it, and it is also
    the key of its capital letter."""
    chars = pl.Series([chr(c) for start, end in _LATIN for c in range(start, end)], dtype=pl.String)
    return {c: key for c, key in zip(chars, _unicode_keys(chars), strict=True) if key.isascii()}


def _not_ascii(s: pl.Series) -> pl.Series:
    """The rows of the texts with other characters than ASCII (those take more than a byte)."""
    return (s.str.len_bytes() != s.str.len_chars()).arg_true()


def text_key(text: str) -> str:
    """``text_sort_key`` for one text, e.g. to sort a Python list."""
    folded = unicodedata.normalize("NFKD", text).lower()
    folded = "".join(ch for ch in folded if not unicodedata.category(ch).startswith("M"))
    return "".join(_FOLD.get(ch, ch) for ch in folded)


# --- formatting ------------------------------------------------------------------------------


def format_dates(s: pl.Series, fmt: str) -> pl.Series:
    """Date/Datetime -> text, e.g. ``dd.mm.yyyy``."""
    return s.alias("value").dt.strftime(to_strftime(fmt)).alias(s.name)  # a fixed name, as in _clean_text


def format_numbers(s: pl.Series, fmt: str = "", decimal: str = ",") -> pl.Series:
    """Numbers -> text with an Excel-style format: ``0``, ``0.00``, ``#,##0.00``, ``00000``,
    ``0%``, ``#,##0.00 €``, ``"No. "0``.

    An empty format writes the number as it is. ``decimal`` is the decimal separator of the
    result; the thousands separator is the other one of ``.`` and ``,``. Text before and after
    the digits is written as it is (in quotes if it contains format codes); ``%`` multiplies by
    100. Sections separated by ``;`` are for positive numbers, negative numbers and zero; colors
    in [ ] are left out.
    """
    sections = _number_sections(fmt)
    swap = str.maketrans({",": ".", ".": ","}) if decimal == "," else None

    def one(value) -> str:
        if sections is None or value != value or value in _INFINITE:
            text = str(value) if isinstance(value, int) else _plain_float(value)
            return text.translate(swap) if swap else text
        number = value if isinstance(value, Decimal) else Decimal(repr(value))
        section = sections[0]
        if number < 0 and len(sections) > 1:
            section, number = sections[1], number.copy_negate()  # the section writes its own minus, if any
        elif number == 0 and len(sections) > 2:
            section = sections[2]
        if section.spec is None:  # a section without digits: only its text
            return section.prefix
        scaled = number.scaleb(section.scale, context=_DECIMAL_CONTEXT)  # the default context keeps 28 digits
        rounded = scaled.quantize(section.quantum, context=_DECIMAL_CONTEXT)
        text = format(rounded, section.spec)
        sign = "-" if text.startswith("-") else ""
        if section.min_digits > 1 or sign:
            whole, dot, fraction = text.removeprefix("-").partition(".")
            text = whole.zfill(section.min_digits) + dot + fraction
        return sign + section.prefix + (text.translate(swap) if swap else text) + section.suffix

    return s.map_elements(one, return_dtype=pl.String, skip_nulls=True).alias(s.name)


@dataclass(frozen=True)
class _NumberSection:
    prefix: str
    suffix: str = ""
    spec: str | None = None  # for format(); None: no digits, only text
    quantum: Decimal = Decimal(1)  # the last decimal place
    min_digits: int = 0
    scale: int = 0  # a power of ten: each % multiplies by 100, each comma after the digits divides by 1000


def _number_sections(fmt: str) -> list[_NumberSection] | None:
    fmt = fmt.strip()
    if not fmt or fmt.lower() in ("general", "standard", "@"):
        return None
    parts: list[list[str]] = [[]]
    for token in _NUMBER_TOKEN.findall(fmt):
        if token == ";":
            parts.append([])
        else:
            parts[-1].append(token)
    sections = [_number_section(fmt, tokens) for tokens in parts[:3]]
    if sections[0].spec is None:
        raise ActionError(f"The number format “{fmt}” has no digits: use 0 or # for them, e.g. 0.00")
    return sections


def _number_section(fmt: str, tokens: list[str]) -> _NumberSection:
    if any(re.fullmatch(r"[eE][+-]", t) for t in tokens):
        raise ActionError(f"The number format “{fmt}”: numbers with an exponent (E+) are not supported")
    digits = [i for i, t in enumerate(tokens) if t in ("0", "#", "?")]
    if not digits:
        return _NumberSection(_format_text(fmt, tokens))
    first, last = digits[0], digits[-1]
    if first > 0 and tokens[first - 1] == ".":
        first -= 1
    number = tokens[first : last + 1]
    point = number.index(".") if "." in number else len(number)
    whole, fraction = number[:point], number[point + 1 :]
    if "," in fraction:  # #.##0,00
        raise ActionError(
            f"The number format “{fmt}”: write it with a comma for thousands and a point for decimals, "
            "as in Excel's codes: #,##0.00 (the result can still use a decimal comma)"
        )
    if any(t not in ("0", "#", "?", ",") for t in whole) or any(t not in ("0", "#", "?") for t in fraction):
        raise ActionError(f"The number format “{fmt}” has text between its digits: write text before or after them")
    end = last + 1
    while end < len(tokens) and tokens[end] == ",":
        end += 1
    return _NumberSection(
        prefix=_format_text(fmt, tokens[:first]),
        suffix=_format_text(fmt, tokens[end:]),
        spec=f"{',' if ',' in whole else ''}.{len(fraction)}f",
        quantum=Decimal(1).scaleb(-len(fraction)),
        min_digits=whole.count("0"),
        scale=2 * tokens.count("%") - 3 * (end - last - 1),
    )


def _format_text(fmt: str, tokens: list[str]) -> str:
    """The text that a part of a number format writes as it is."""
    out = []
    for token in tokens:
        if len(token) >= 2 and token[0] == token[-1] == '"':
            out.append(token[1:-1])
        elif len(token) == 2 and token[0] in "\\_*":
            out.append({"\\": token[1], "_": " ", "*": ""}[token[0]])  # _x: a space as wide as x
        elif token.startswith("[") and token.endswith("]"):
            inner = token[1:-1]
            if inner.startswith("$"):  # [$€-407]: a currency sign and a language
                out.append(inner[1:].split("-")[0])
            elif not _COLORS.fullmatch(inner):
                raise ActionError(f"The number format “{fmt}”: {token} is not supported")
        else:
            out.append(token)
    return "".join(out)


def _plain_float(value: float | Decimal) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    if isinstance(value, Decimal):  # all its digits
        text = format(value, "f")
        text = text.rstrip("0").rstrip(".") if "." in text else text
    else:
        text = f"{value:.10f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# --- internals -------------------------------------------------------------------------------


def _clean_text(s: pl.Series) -> pl.Series:
    text = s.cast(pl.String).alias("text").str.strip_chars()  # Polars' Series methods would read a name like ^a.*$ as a pattern
    return pl.select(pl.when(pl.lit(text) == "").then(None).otherwise(pl.lit(text)).alias(s.name)).to_series()


def _apply(s: pl.Series, expr: pl.Expr) -> pl.Series:
    """``expr``, written for the column ``_VALUE``, calculated for the values of ``s``."""
    return pl.DataFrame([s.alias("value")]).select(expr.alias(s.name)).to_series()


def _number_text(expr: pl.Expr, decimal: str) -> pl.Expr:
    """A number typed as text, without spaces, currency signs and thousands separators, with a
    leading minus and a point as the decimal separator."""
    thousands = "." if decimal == "," else ","
    text = expr.cast(pl.String).str.strip_chars().str.replace_all(_SPACES, "").str.strip_chars(_CURRENCY)
    text = pl.when(text.str.ends_with("-")).then(pl.lit("-") + text.str.strip_suffix("-")).otherwise(text)
    text = (
        pl.when(text.str.contains(_GROUPED[decimal]))
        .then(text.str.replace_all(thousands, "", literal=True))
        .otherwise(text)
    )
    return text.str.replace(",", ".", literal=True) if decimal == "," else text


def _too_long(s: pl.Series, examples: pl.Series, warn: Callable[[str], None] | None) -> pl.Series:
    if warn is not None:
        warn(f"{s.name} stays text: its numbers are too long to be kept exactly, e.g. '{examples[0]}'")
    return s


def _date_expr(text: pl.Expr, code: str) -> pl.Expr:
    parsed = text.str.to_date(code, strict=False)
    return _excel_years(parsed) if "%y" in code else parsed


def _datetime_expr(text: pl.Expr, code: str, time_unit: str = "us") -> pl.Expr:
    parsed = text.str.to_datetime(code, strict=False, time_unit=time_unit)
    return _excel_years(parsed) if "%y" in code else parsed


def _excel_years(parsed: pl.Expr) -> pl.Expr:
    """Dates read with a two-digit year, in the century Excel gives them: 00-29 are 2000-2029,
    30-99 are 1930-1999."""
    short = parsed.dt.year() % 100
    return parsed.dt.replace(year=pl.when(short < 30).then(short + 2000).otherwise(short + 1900))


def _has_time(code: str) -> bool:
    return any(c in code for c in ("%H", "%I", "%M", "%S", "%-H", "%-I", "%p"))

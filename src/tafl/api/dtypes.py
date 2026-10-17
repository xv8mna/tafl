"""Detecting, parsing and formatting values: numbers, dates, booleans.

The app uses Excel-style format codes everywhere (``dd.mm.yyyy hh:mm:ss``, ``#,##0.00``).
``to_strftime`` converts date codes for Polars; formats containing ``%`` are strftime already.
Month and day names (``mmm``, ``mmmm``, ``ddd``, ``dddd``) are English. Two-digit years are
read like in Excel: 00-29 are 2000-2029, 30-99 are 1930-1999.

Standard column types in the app: Int64, Float64, String, Date, Datetime, Boolean.
"""

from __future__ import annotations

import datetime as dt
import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Context, Decimal

import polars as pl

from .errors import ActionError

TRUE_WORDS = frozenset({"true", "yes", "y", "ja", "j", "wahr", "x", "1"})
FALSE_WORDS = frozenset({"false", "no", "n", "nein", "falsch", "0"})

_INTEGER = r"^[+-]?(0|[1-9][0-9]{0,18})$"
_LEADING_ZERO = r"^[+-]?0[0-9]+$"  # "01067": an ID or postal code, not the number 1067
_VALUE = pl.col("value")  # see _apply
_PLAIN_NUMBER = r"^[+-]?([0-9]+\.?[0-9]*|\.[0-9]+)$"  # after _number_text
_NUMBER = {
    ",": r"^[+-]?([0-9]+|[0-9]{1,3}(\.[0-9]{3})+)(,[0-9]+)?$",
    ".": r"^[+-]?([0-9]+|[0-9]{1,3}(,[0-9]{3})+)(\.[0-9]+)?$",
}
_GROUPED = {
    ",": r"^[+-]?[0-9]{1,3}(\.[0-9]{3})+(,[0-9]*)?$",
    ".": r"^[+-]?[0-9]{1,3}(,[0-9]{3})+(\.[0-9]*)?$",
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
)

_EXCEL_EPOCH = dt.datetime(1899, 12, 30)
# Quoted text ("Uhr") and backslash escapes are literal, like in Excel formats.
_DATE_TOKEN = re.compile(r'(?i)"[^"]*"|\\.|am/pm|y+|m+|d+|h+|s+|\.0+|.', re.S)
# Number formats: also _x (a space as wide as x), *x (fill with x) and [...] (color, currency).
_NUMBER_TOKEN = re.compile(r'"[^"]*"|\\.|_.|\*.|\[[^\]]*\]|[eE][+-]|.', re.S)
_COLORS = re.compile(r"(?i)black|blue|cyan|green|magenta|red|white|yellow|colou?r ?[0-9]+")
_DECIMAL_CONTEXT = Context(prec=400, rounding=ROUND_HALF_UP)  # room for every float's digits
_INFINITE = (float("inf"), float("-inf"))
# Letters that don't take their base letter apart in Unicode (NFKD): how they sort.
_FOLD = {"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "ł": "l", "þ": "th", "ı": "i"}


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


def parse_integers(s: pl.Series, decimal: str = ",") -> pl.Series:
    """Text -> Int64, like ``parse_numbers`` but exact for all 19 digits (a Float64 keeps 15).
    Decimals are rounded half away from zero, like in Excel; numbers outside the range of Int64
    become empty."""
    return _apply(_apply(s, _number_text(_VALUE, decimal)), _integer(_VALUE))


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
    numbers beyond 2^53 (9,007,199,254,740,992), as numbers or as text."""
    if s.dtype.is_integer():
        return (s.cast(pl.Float64).cast(pl.Int128, strict=False) != s.cast(pl.Int128)).fill_null(False)
    lost = pl.repeat(False, s.len(), eager=True).alias(s.name)
    if s.dtype != pl.String:
        return lost
    rows = (s.str.len_bytes() > 15).fill_null(False).arg_true()  # shorter texts have at most 15 digits
    if rows.len():
        digits = _number_text(_VALUE, decimal).str.extract(r"^[+-]?0*([0-9]+)", 1)
        whole = digits.cast(pl.Int128, strict=False)
        found = (digits.str.len_chars() > 38) | (whole.cast(pl.Float64).cast(pl.Int128, strict=False) != whole)
        lost = lost.scatter(rows, _apply(s.gather(rows), found.fill_null(False)))
    return lost


def parse_dates(s: pl.Series, fmt: str = "") -> pl.Series:
    """Text -> Date. Without a format, the common formats are recognized (31.12.2025, 31.12.25,
    2025-12-31, 2025/12/31, also with a time)."""
    text = _VALUE
    if fmt:
        code = to_strftime(fmt)
        parsed = _datetime_expr(text, code).dt.date() if _has_time(code) else _date_expr(text, code)
    else:
        parsed = pl.coalesce(
            [pl.when(text.str.contains(pattern)).then(_date_expr(text, f)) for f, pattern in _DATE_PATTERNS]
            + [
                pl.when(text.str.contains(pattern)).then(_datetime_expr(text, f).dt.date())
                for f, pattern in _DATETIME_PATTERNS
            ]
        )
    return _apply(_clean_text(s), parsed)


def parse_datetimes(s: pl.Series, fmt: str = "") -> pl.Series:
    """Text -> Datetime. Without a format, the common formats are recognized (see
    ``parse_dates``)."""
    text = _VALUE
    if fmt:
        code = to_strftime(fmt)
        parsed = _datetime_expr(text, code) if _has_time(code) else _date_expr(text, code).cast(pl.Datetime("us"))
    else:
        parsed = pl.coalesce(
            [pl.when(text.str.contains(pattern)).then(_datetime_expr(text, f)) for f, pattern in _DATETIME_PATTERNS]
            + [
                pl.when(text.str.contains(pattern)).then(_date_expr(text, f).cast(pl.Datetime("us")))
                for f, pattern in _DATE_PATTERNS
            ]
        )
    return _apply(_clean_text(s), parsed)


def parse_booleans(s: pl.Series) -> pl.Series:
    """Text -> Boolean: true/yes/ja/x/1 and false/no/nein/0 (any case). Others become empty."""
    mapping = {w: True for w in TRUE_WORDS} | {w: False for w in FALSE_WORDS}
    text = s.cast(pl.String).str.strip_chars().str.to_lowercase()
    return text.replace_strict(mapping, default=None, return_dtype=pl.Boolean).alias(s.name)


def excel_serial_to_datetime(s: pl.Series) -> pl.Series:
    """Excel day numbers (45658 = 2025-01-01, fractions = time of day) -> Datetime."""
    ms = (s.cast(pl.Float64) * 86_400_000).round(0).cast(pl.Int64)
    return pl.select((pl.lit(_EXCEL_EPOCH) + pl.duration(milliseconds=pl.lit(ms))).alias(s.name)).to_series()


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
    values = text.drop_nulls()
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
                return parsed
    if values.str.to_lowercase().is_in(["true", "false"]).all():
        return text.str.to_lowercase() == "true"
    return s


def whole_numbers_to_int(df: pl.DataFrame) -> pl.DataFrame:
    """Float columns holding only whole numbers become Int64 (Excel stores all numbers as floats)."""
    updates = []
    for name, dtype in df.schema.items():
        if dtype.is_float():
            values = df[name].drop_nulls()
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
            values = df[name].drop_nulls()
            if values.len() and (values.dt.truncate("1d") == values).all():
                updates.append(df[name].dt.date())
    return df.with_columns(updates)


def type_label(dtype: pl.DataType) -> str:
    """The word for a type that the app shows: text, integer, number, date, ..."""
    if dtype == pl.String:
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
    return str(dtype).lower()


def normalize(s: pl.Series) -> pl.Series:
    """Map other types to the app's standard ones (Decimal -> Int64/Float64, Int32 -> Int64, ...)."""
    dtype = s.dtype
    if isinstance(dtype, pl.Decimal):
        if dtype.scale == 0:
            try:
                return s.cast(pl.Int64)
            except Exception:  # does not fit into Int64
                return s.cast(pl.Float64)
        return s.cast(pl.Float64)
    if dtype.is_integer() and dtype not in (pl.Int64, pl.UInt64):
        return s.cast(pl.Int64)
    if dtype == pl.Float32:
        return s.cast(pl.Float64)
    if dtype in (pl.Null, pl.Categorical) or isinstance(dtype, pl.Enum):
        return s.cast(pl.String)
    return s


# --- sorting ---------------------------------------------------------------------------------


def text_sort_key(expr: pl.Expr) -> pl.Expr:
    """A key that sorts text like Excel: upper and lower case count the same, letters with
    accents sort with their base letter (ä with a, é with e), ß like ss. Sort with
    ``maintain_order=True`` so that values with the same key keep their order."""
    folded = expr.str.normalize("NFKD").str.to_lowercase().str.replace_all(r"\p{M}+", "")
    return folded.str.replace_many(list(_FOLD), list(_FOLD.values()))


def text_key(text: str) -> str:
    """``text_sort_key`` for one text, e.g. to sort a Python list."""
    folded = unicodedata.normalize("NFKD", text).lower()
    folded = "".join(ch for ch in folded if not unicodedata.category(ch).startswith("M"))
    return "".join(_FOLD.get(ch, ch) for ch in folded)


# --- formatting ------------------------------------------------------------------------------


def format_dates(s: pl.Series, fmt: str) -> pl.Series:
    """Date/Datetime -> text, e.g. ``dd.mm.yyyy``."""
    return s.dt.strftime(to_strftime(fmt)).alias(s.name)


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
        section = sections[0]
        if value < 0 and len(sections) > 1:
            section, value = sections[1], -value  # the section writes its own minus, if any
        elif value == 0 and len(sections) > 2:
            section = sections[2]
        if section.spec is None:  # a section without digits: only its text
            return section.prefix
        number = value if isinstance(value, Decimal) else Decimal(repr(value))
        rounded = number.scaleb(section.scale).quantize(section.quantum, context=_DECIMAL_CONTEXT)
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


def _plain_float(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# --- internals -------------------------------------------------------------------------------


def _clean_text(s: pl.Series) -> pl.Series:
    text = s.cast(pl.String).str.strip_chars()
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


def _datetime_expr(text: pl.Expr, code: str) -> pl.Expr:
    parsed = text.str.to_datetime(code, strict=False, time_unit="us")
    return _excel_years(parsed) if "%y" in code else parsed


def _excel_years(parsed: pl.Expr) -> pl.Expr:
    """Dates read with a two-digit year, in the century Excel gives them: 00-29 are 2000-2029,
    30-99 are 1930-1999."""
    short = parsed.dt.year() % 100
    return parsed.dt.replace(year=pl.when(short < 30).then(short + 2000).otherwise(short + 1900))


def _has_time(code: str) -> bool:
    return any(c in code for c in ("%H", "%I", "%M", "%S", "%-H", "%-I", "%p"))

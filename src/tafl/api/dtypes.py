"""Detecting, parsing and formatting values: numbers, dates, booleans.

The app uses Excel-style format codes everywhere (``dd.mm.yyyy hh:mm:ss``, ``#,##0.00``).
``to_strftime`` converts date codes for Polars; formats containing ``%`` are strftime already.

Standard column types in the app: Int64, Float64, String, Date, Datetime, Boolean.
"""

from __future__ import annotations

import datetime as dt
import re
from decimal import ROUND_HALF_UP, Decimal

import polars as pl

TRUE_WORDS = frozenset({"true", "yes", "y", "ja", "j", "wahr", "x", "1"})
FALSE_WORDS = frozenset({"false", "no", "n", "nein", "falsch", "0"})

_INTEGER = r"^[+-]?(0|[1-9][0-9]{0,17})$"
_LEADING_ZERO = r"^[+-]?0[0-9]+$"  # "01067": an ID or postal code, not the number 1067
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
    thousands = "." if decimal == "," else ","
    text = expr.cast(pl.String).str.replace_all(_SPACES, "").str.strip_chars(_CURRENCY)
    text = pl.when(text.str.ends_with("-")).then(pl.lit("-") + text.str.strip_suffix("-")).otherwise(text)
    text = (
        pl.when(text.str.contains(_GROUPED[decimal]))
        .then(text.str.replace_all(thousands, "", literal=True))
        .otherwise(text)
    )
    if decimal == ",":
        text = text.str.replace(",", ".", literal=True)
    return text.cast(pl.Float64, strict=False)


def parse_numbers(s: pl.Series, decimal: str = ",") -> pl.Series:
    """Text -> Float64 (see ``number_expr``)."""
    return pl.select(number_expr(pl.lit(s), decimal).alias(s.name)).to_series()


def parse_dates(s: pl.Series, fmt: str = "") -> pl.Series:
    """Text -> Date. Without a format, the common format that fits the most values is used."""
    text = _clean_text(s)
    if fmt:
        code = to_strftime(fmt)
        if _has_time(code):
            return text.str.to_datetime(code, strict=False, time_unit="us").dt.date()
        return text.str.to_date(code, strict=False)
    candidates = [text.str.to_date(f, strict=False) for f, _ in _DATE_PATTERNS]
    candidates += [
        text.str.to_datetime(f, strict=False, time_unit="us").dt.date() for f, _ in _DATETIME_PATTERNS
    ]
    return _most_complete(candidates).alias(s.name)


def parse_datetimes(s: pl.Series, fmt: str = "") -> pl.Series:
    """Text -> Datetime. Without a format, the common format that fits the most values is used."""
    text = _clean_text(s)
    if fmt:
        code = to_strftime(fmt)
        if _has_time(code):
            return text.str.to_datetime(code, strict=False, time_unit="us")
        return text.str.to_date(code, strict=False).cast(pl.Datetime("us"))
    candidates = [text.str.to_datetime(f, strict=False, time_unit="us") for f, _ in _DATETIME_PATTERNS]
    candidates += [text.str.to_date(f, strict=False).cast(pl.Datetime("us")) for f, _ in _DATE_PATTERNS]
    return _most_complete(candidates).alias(s.name)


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


def infer_types(df: pl.DataFrame, decimal: str = ",") -> pl.DataFrame:
    """Turn text columns into whole numbers, decimal numbers, dates, date-times or booleans when
    every value fits. Columns with leading zeros (IDs, postal codes) stay text."""
    return df.with_columns([infer_series(df[name], decimal) for name in df.columns])


def infer_series(s: pl.Series, decimal: str = ",") -> pl.Series:
    if s.dtype != pl.String:
        return s
    text = _clean_text(s)
    values = text.drop_nulls()
    if values.len() == 0 or values.str.contains(_LEADING_ZERO).any():
        return s
    if values.str.contains(_INTEGER).all():
        return text.cast(pl.Int64)
    if values.str.contains(_NUMBER[decimal]).all():
        return parse_numbers(text, decimal)
    for fmt, pattern in _DATE_PATTERNS:
        if values.str.contains(pattern).all():
            parsed = text.str.to_date(fmt, strict=False)
            if parsed.null_count() == text.null_count():
                return parsed
    for fmt, pattern in _DATETIME_PATTERNS:
        if values.str.contains(pattern).all():
            parsed = text.str.to_datetime(fmt, strict=False, time_unit="us")
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


# --- formatting ------------------------------------------------------------------------------


def format_dates(s: pl.Series, fmt: str) -> pl.Series:
    """Date/Datetime -> text, e.g. ``dd.mm.yyyy``."""
    return s.dt.strftime(to_strftime(fmt)).alias(s.name)


def format_numbers(s: pl.Series, fmt: str = "", decimal: str = ",") -> pl.Series:
    """Numbers -> text with an Excel-style format: ``0``, ``0.00``, ``#,##0.00``, ``00000``.

    An empty format writes the number as it is. ``decimal`` is the decimal separator of the
    result; the thousands separator is the other one of ``.`` and ``,``.
    """
    spec = _number_spec(fmt)
    swap = str.maketrans({",": ".", ".": ","}) if decimal == "," else None

    def one(value: float) -> str:
        if spec is None:
            text = str(value) if isinstance(value, int) else _plain_float(value)
        else:
            decimals, grouping, min_digits = spec
            rounded = Decimal(repr(value)).quantize(Decimal(1).scaleb(-decimals), rounding=ROUND_HALF_UP)
            text = format(rounded, f"{',' if grouping else ''}.{decimals}f")
            if min_digits > 1:
                sign = "-" if text.startswith("-") else ""
                whole, dot, frac = text.lstrip("-").partition(".")
                text = sign + whole.zfill(min_digits) + dot + frac
        return text.translate(swap) if swap else text

    return s.map_elements(one, return_dtype=pl.String, skip_nulls=True).alias(s.name)


def _number_spec(fmt: str) -> tuple[int, bool, int] | None:
    fmt = fmt.strip()
    if not fmt:
        return None
    whole, _, fraction = fmt.partition(".")
    decimals = sum(ch in "0#" for ch in fraction)
    return decimals, "," in whole, whole.count("0")


def _plain_float(value: float) -> str:
    if value != value or value in (float("inf"), float("-inf")):
        return str(value)
    text = f"{value:.10f}".rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


# --- internals -------------------------------------------------------------------------------


def _clean_text(s: pl.Series) -> pl.Series:
    text = s.cast(pl.String).str.strip_chars()
    return pl.select(pl.when(pl.lit(text) == "").then(None).otherwise(pl.lit(text)).alias(s.name)).to_series()


def _has_time(code: str) -> bool:
    return any(c in code for c in ("%H", "%I", "%M", "%S", "%-H", "%-I", "%p"))


def _most_complete(candidates: list[pl.Series]) -> pl.Series:
    return min(candidates, key=lambda c: c.null_count())

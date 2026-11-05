"""Columns: rename, keep/remove/reorder, change type and format."""

from __future__ import annotations

import polars as pl

from tafl.api import ActionError, P, action, cols, dtypes, selector

_TYPES = [
    ("text", "Text"),
    ("integer", "Whole number"),
    ("number", "Decimal number"),
    ("date", "Date"),
    ("datetime", "Date and time"),
    ("boolean", "True / false"),
]
_TYPE_LABELS = dict(_TYPES)


@action(
    "columns.rename",
    "Rename columns",
    category="Columns",
    params=[
        P.List(
            "columns",
            "Rename",
            fields=[P.Column("column", "Column"), P.Text("new_name", "New name", required=True)],
            min_items=1,
        ),
    ],
    summary=lambda p: "Rename " + ", ".join(f"{c.column} → {c.new_name}" for c in p.columns),
)
def rename(df, p, ctx):
    """Gives columns new names."""
    mapping = {c.column: c.new_name.strip() for c in p.columns if c.new_name.strip() and c.new_name.strip() != c.column}
    names = [mapping.get(name, name) for name in df.columns]
    if duplicates := sorted({n for n in names if names.count(n) > 1}):
        raise ActionError(f"After renaming, these names exist twice: {', '.join(duplicates)}")
    return df.rename(mapping)


@action(
    "columns.select",
    "Keep, remove or reorder columns",
    category="Columns",
    params=[
        P.Choice(
            "mode",
            "Mode",
            [
                ("keep", "Keep only these columns, in this order"),
                ("remove", "Remove these columns"),
                ("front", "Move these columns to the front"),
            ],
        ),
        P.Columns("columns", "Columns", required=True),
    ],
    summary=lambda p: {"keep": "Keep", "remove": "Remove", "front": "Move to front:"}[p.mode] + " " + ", ".join(p.columns),
)
def select(df, p, ctx):
    """Keeps only some columns (in the order listed), removes columns, or moves columns to
    the front while keeping all others behind them."""
    if p.mode == "keep":
        return df.select(cols(p.columns))
    if p.mode == "remove":  # one selector: Polars nests a list of columns here, and thousands overflow its stack
        return df.drop(selector(p.columns))
    return df.select(cols([*p.columns, *(c for c in df.columns if c not in p.columns)]))


@action(
    "columns.change_type",
    "Change type / format",
    category="Columns",
    params=[
        P.List(
            "columns",
            "Columns",
            fields=[
                P.Column("column", "Column"),
                P.Choice("type", "New type", _TYPES),
                P.Text("format", "Format", placeholder="dd.mm.yyyy  |  #,##0.00  |  00000"),
            ],
            min_items=1,
        ),
        P.Choice("decimal", "Decimal separator in text", [(",", "Comma: 1.234,56"), (".", "Point: 1,234.56")]),
        P.Choice(
            "on_error",
            "Values that can't be converted",
            [("empty", "become empty (with a warning)"), ("fail", "stop with an error")],
        ),
    ],
    summary=lambda p: "Change type: " + ", ".join(f"{c.column} → {_TYPE_LABELS.get(c.type, c.type)}" for c in p.columns),
)
def change_type(df, p, ctx):
    """Converts columns to another type.

    Format examples — dates: dd.mm.yyyy, yyyy-mm-dd, dd.mm.yyyy hh:mm, d. mmmm yyyy; numbers as
    text: 0.00, #,##0.00, 00000 (pads with zeros, e.g. postal codes), 0%, #,##0.00 €. Without a
    date format, common formats are detected; two-digit years are read like in Excel (25 = 2025,
    75 = 1975). Month and day names (mmm, mmmm, ddd, dddd) are English. Numbers become dates as
    Excel day numbers (45658 = 01.01.2025); with a format of digits only, such as yyyymmdd, their
    digits are read with it (20260131 = 31.01.2026). Whole numbers keep all their digits (up to
    9,223,372,036,854,775,807); decimal numbers keep about 15.
    """
    for c in p.columns:  # one after the other, so a column can be converted twice
        source, rounded = df[c.column], None
        if c.type == "integer" and source.dtype == pl.String:  # the numbers and the rounded values from one reading
            converted, rounded = dtypes.parse_whole_numbers(source, p.decimal)
        else:
            converted = _convert(source, c.type, c.format.strip(), p.decimal)
        # fixed names from here: Polars' Series methods would read a name like ^a.*$ as a pattern
        source, converted = source.alias("value"), converted.alias("value")
        failed = source.is_not_null() & converted.is_null()
        if source.dtype == pl.String:
            failed = failed & (source.str.strip_chars() != "")
        if count := failed.sum():
            examples = _examples(source.filter(failed))
            message = f"{c.column}: {count:,} value(s) could not be converted to {_TYPE_LABELS[c.type]}, e.g. {examples}"
            hint = ""
            if c.type == "integer" and _numbers(source, p.decimal).filter(failed).is_finite().any():
                hint = ". Whole numbers go up to 9,223,372,036,854,775,807: keep longer IDs as text"
            elif _beyond_day_numbers(source, failed, c):
                hint = ". Numbers are Excel day numbers, up to 2,958,465 (31.12.9999): for a number such as 20260131, give the format yyyymmdd"
            if p.on_error == "fail":
                raise ActionError(message + hint)
            ctx.warn(message + " (now empty)" + hint)
        if c.type == "number":
            _warn_rounded(c.column, source, converted, p.decimal, ctx)
        elif c.type == "integer":
            _warn_whole(c.column, source, converted, p.decimal, ctx, rounded)
        df = df.with_columns(converted.alias(c.column))
    return df


def _beyond_day_numbers(source: pl.Series, failed: pl.Series, c) -> bool:
    """Whether numbers that became no date are numbers beyond Excel's days (not NaN or infinity),
    read as day numbers: then they are probably dates written as digits, such as 20260131."""
    if c.type not in ("date", "datetime") or not source.dtype.is_numeric() or dtypes.is_digit_format(c.format.strip()):
        return False
    return source.filter(failed).cast(pl.Float64).is_finite().any()


def _warn_rounded(name: str, source: pl.Series, converted: pl.Series, decimal: str, ctx) -> None:
    """A decimal number keeps about 15 digits: say which values lost some."""
    rounded = dtypes.float_loses_digits(source, decimal) & converted.is_not_null()
    if rounded.any():
        examples = _examples(source.filter(rounded))
        ctx.warn(
            f"{name}: {rounded.sum():,} value(s) were rounded, e.g. {examples}: a decimal number keeps "
            "about 15 digits (Whole number and Text keep them all)"
        )


def _warn_whole(name: str, source: pl.Series, converted: pl.Series, decimal: str, ctx, rounded: pl.Series | None) -> None:
    """A whole number has no decimal places: say which values were rounded (half away from zero).
    ``rounded``: those values, when the conversion gave them already (from text)."""
    rounded = (dtypes.rounded_to_whole(source, decimal) if rounded is None else rounded) & converted.is_not_null()
    if rounded.any():
        examples = _examples(source.filter(rounded))
        ctx.warn(
            f"{name}: {rounded.sum():,} value(s) were rounded, e.g. {examples}: a whole number has no decimal "
            "places (Decimal number and Text keep them)"
        )


def _examples(values: pl.Series) -> str:
    """Up to three different ones of these values (named "value"), for a message: '1.5', '2.25'."""
    return ", ".join(f"'{v}'" for v in values.unique(maintain_order=True).head(3).to_list())


def _kind(dtype: pl.DataType) -> str:
    """A column of this type in a message: "a date column", "an object column"."""
    return dtypes.with_article(f"{dtypes.type_label(dtype)} column")


def _numbers(s: pl.Series, decimal: str) -> pl.Series:
    """The values as decimal numbers (empty where they aren't numbers)."""
    return dtypes.parse_numbers(s, decimal) if s.dtype == pl.String else s.cast(pl.Float64, strict=False)


def _convert(s: pl.Series, target: str, fmt: str, decimal: str) -> pl.Series:
    source = s.dtype
    if target == "text":
        if source == pl.String:
            return s
        if source == pl.Date:
            return _dates_as_text(s, fmt or "dd.mm.yyyy")
        if source == pl.Datetime:  # by default with the fractions of a second of a value that has some: .500, .000001
            return _dates_as_text(s, fmt or "%d.%m.%Y %H:%M:%S%.f")
        if source.is_numeric():
            return dtypes.format_numbers(s, fmt, decimal)
        if source == pl.Binary:  # as hex digits, like SQL Server shows them: 0x0102FF (a fixed name, as in change_type)
            return ("0x" + s.alias("value").bin.encode("hex").str.to_uppercase()).alias(s.name)
        return _text_of(s)
    if target in ("number", "integer"):
        if source == pl.String:
            if target == "integer":
                return dtypes.parse_integers(s, decimal)  # exact, also beyond 15 digits
            return dtypes.parse_numbers(s, decimal)
        if not (source.is_numeric() or source == pl.Boolean):
            raise ActionError(f"{s.name}: {_kind(source)} can't become a number")
        if target == "number":
            return s.cast(pl.Float64)
        if source.is_float() or isinstance(source, pl.Decimal):
            s = s.alias("value").round(0, mode="half_away_from_zero").alias(s.name)  # a fixed name, as in change_type
        return s.cast(pl.Int64, strict=False)
    if target in ("date", "datetime"):
        if source == pl.String:
            parsed = dtypes.parse_dates(s, fmt) if target == "date" else dtypes.parse_datetimes(s, fmt)
        elif source in (pl.Date, pl.Datetime):
            parsed = s.cast(pl.Datetime("us"))
        elif source.is_numeric():  # Excel day numbers; with a format of digits such as yyyymmdd, the digits
            parsed = dtypes.numbers_to_datetimes(s, fmt)
        else:
            raise ActionError(f"{s.name}: {_kind(source)} can't become a date")
        if target == "date" and parsed.dtype != pl.Date:
            return parsed.alias("value").dt.date().alias(s.name)  # a fixed name, as in change_type
        return parsed
    if target == "boolean":
        if source == pl.String:
            return dtypes.parse_booleans(s)
        if source.is_numeric():
            return s != 0
        if source == pl.Boolean:
            return s
        raise ActionError(f"{s.name}: {_kind(source)} can't become true/false")
    raise ActionError(f"Unknown type '{target}'")


# Two date-times that differ in every part that a date format writes, also in AM/PM and the weekday
_SAMPLES = pl.Series(["2001-02-03 04:05:06.789012", "2002-03-04 17:16:17.891234"]).str.to_datetime("%Y-%m-%d %H:%M:%S%.f")
_FORMATS: dict[tuple[str, bool, str | None], tuple[str, bool]] = {}  # the date formats checked so far: see _format_check


def _dates_as_text(s: pl.Series, fmt: str) -> pl.Series:
    """Dates or date-times as text with a date format; one that can't write them stops the step.
    With a time in the format, dates are written at midnight (00:00), as Excel writes them."""
    key = (fmt, s.dtype == pl.Date, s.dtype.time_zone if s.dtype == pl.Datetime else None)
    if key not in _FORMATS:
        _FORMATS[key] = _format_check(*key)
    problem, at_midnight = _FORMATS[key]
    if problem:
        raise ActionError(f"{s.name}: {problem}")
    if at_midnight:
        s = s.alias("value").cast(pl.Datetime("us")).alias(s.name)  # a fixed name, as in change_type
    return dtypes.format_dates(s, fmt)


def _format_check(fmt: str, dates: bool, zone: str | None) -> tuple[str, bool]:
    """Whether dates (``dates``) or date-times (with the time ``zone``) can be written with the
    format ``fmt``: why not (a number format such as 0.00, or a format that writes no part of a
    date: with 00000 each value would be 00000), and whether dates need a time of day for it
    (they get midnight). It doesn't depend on the values: also a column without any stops the
    step."""
    samples = _SAMPLES.dt.date() if dates else _SAMPLES
    if zone:  # for a format with the zone's name (%Z)
        samples = samples.dt.replace_time_zone("UTC").dt.convert_time_zone(zone)
    code = dtypes.to_strftime(fmt)
    if not _writes_date_parts(code):  # a number format such as 0.00, 0.000 or 00000 (.000 alone would be the milliseconds)
        return f"the format '{fmt}' writes no part of a date. Use codes such as dd.mm.yyyy hh:mm:ss", False
    texts, at_midnight = None, False
    try:
        texts = samples.dt.strftime(code).to_list()
    except pl.exceptions.ComputeError:
        if dates:  # a format with a time: the dates at midnight
            try:
                texts, at_midnight = samples.cast(pl.Datetime("us")).dt.strftime(code).to_list(), True
            except pl.exceptions.ComputeError:
                pass
    if texts is None:
        return (
            f"{'dates' if dates else 'date-times'} can't be written with the format '{fmt}'. Use codes such as "
            "dd.mm.yyyy hh:mm:ss, and .000, .000000 or .000000000 for fractions of a second"
        ), False
    if texts[0] == texts[1]:  # also hh:mm for dates: 00:00 for each one
        return f"the format '{fmt}' writes no part of a date. Use codes such as dd.mm.yyyy hh:mm:ss", False
    return "", at_midnight


def _writes_date_parts(code: str) -> bool:
    """Whether the strftime ``code`` (from ``dtypes.to_strftime``) has a code for a part of a date
    or time: a % and a letter, but not a fraction of a second alone (%.3f, %f) or %% (a % sign)."""
    for part in code.replace("%%", "").split("%")[1:]:
        letter = part.lstrip("-_0^#")[:1]
        if letter.isalpha() and letter != "f":
            return True
    return False


def _text_of(s: pl.Series) -> pl.Series:
    """The values as text, the same on every PC, as Pivot and Split column write them (see
    ``dtypes.text_expr``): 08:30:00.500 for a time (Polars' cast leaves out the fractions of a
    second), 1d 2h 3m for a time span (Polars has no cast for it), true and false. Lists, records
    and a plugin's own values stop the step."""
    if s.dtype.is_nested() or s.dtype == pl.Object:
        raise ActionError(f"{s.name}: the column holds {dtypes.type_label(s.dtype)} values, which can't become text")
    return s.alias("value").to_frame().select(dtypes.text_expr(pl.col("value"), s.dtype)).to_series().alias(s.name)

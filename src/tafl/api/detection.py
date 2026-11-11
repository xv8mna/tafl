"""Numbers and dates in text columns, as the CSV import detects them: the decimal separator the
values show, and the types the columns get (``dtypes.infer_series`` with the decimal separator of
each column, and also numbers with thousands separators only, numbers without a digit before the
decimal separator, amounts and percentages, and exact decimals for numbers too long for a decimal
number).

    decimal, own = detection.guess_decimal(df, default=".")
    typed = detection.detect_types(df, {name: own.get(name, decimal) for name in df.columns}, warn)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import polars as pl

from . import dtypes

# Numbers as text: 1.234,56 or 1234,56 or ,5 (decimal comma) and 1,234.56 or 1234.56 or .5 (decimal
# point). Thousands separators come after 1 to 3 digits that don't start with 0: 0.500 is a decimal.
_NUMBER = {
    ",": r"(([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)(,[0-9]+)?|,[0-9]+)",
    ".": r"(([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)(\.[0-9]+)?|\.[0-9]+)",
}
_WHOLE = {",": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)$", ".": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)$"}
_EITHER = r"^[+-]?([0-9]+|[1-9][0-9]{0,2}[.,][0-9]{3})$"  # 12, 1.250 and 1,250 fit both decimal separators
_SPACES = "[   ]"  # a space, a no-break space and a narrow no-break space
_CURRENCY = "€$£"
# Amounts and percentages can also have spaces between the thousands (1 234,50 €), only in groups
# of three digits: other digits with spaces between them (phone numbers, 2025 01 31) stay text.
_SPACED = {
    ",": rf"(([0-9]+|[1-9][0-9]{{0,2}}(\.[0-9]{{3}})+|[1-9][0-9]{{0,2}}({_SPACES}[0-9]{{3}})+)(,[0-9]+)?|,[0-9]+)",
    ".": rf"(([0-9]+|[1-9][0-9]{{0,2}}(,[0-9]{{3}})+|[1-9][0-9]{{0,2}}({_SPACES}[0-9]{{3}})+)(\.[0-9]+)?|\.[0-9]+)",
}
# The sign of an amount comes after its currency sign or before it: $-3.00, -$3.00, -3,00 €
_SIGNED = rf"([{_CURRENCY}]?{_SPACES}*[+-]?|[+-]{_SPACES}*[{_CURRENCY}])"
_CODE = r"^[+-]?0[0-9]"  # a leading zero before another digit: a code (0815, 00,5), not a number
_FLOAT_DIGITS = 15  # the significant digits that a decimal number (Float64) keeps, as Excel and the CSV export
_MOST_DIGITS = 38  # of an exact decimal (Decimal)
_SAMPLE = 1_000  # values that most columns are told apart by


def guess_decimal(df: pl.DataFrame, default: str) -> tuple[str, dict[str, str]]:
    """The decimal separator ("," or ".") of the text columns of ``df`` together, and of each
    column whose values show one. A value shows a decimal comma (1.234,56 / 12,5 / ,5 / 0,500 /
    1.234.567) or a decimal point (1,234.56 / 12.5 / .5 / 0.500) when it fits only that one; values
    like 1.000 and 1,000 fit both. A column with a value that shows a point has a decimal point,
    else one with a value that shows a comma a decimal comma. For all columns together, the
    separator that more values of a sample show; without a difference, ``default``."""
    comma = point = 0
    columns: dict[str, str] = {}
    sample = df.head(2000)
    texts = [name for name, dtype in df.schema.items() if dtype == pl.String]
    # Only a column with a comma or a point can show either (at once for all: column by column takes long)
    marked = df.select(pl.col(pl.String).str.contains("[.,]").any()).row(0) if texts else ()
    for name, separators in zip(texts, marked):
        if not separators:
            continue
        column = sample[name].alias("value")  # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
        commas, points = _shown_decimals(column)
        comma, point = comma + commas, point + points
        if not commas and not points and df.height > sample.height and column.str.contains(_EITHER).all():
            rest = df[name].slice(sample.height).alias("value")  # numbers like 12 or 1.250 so far: also 0.500 after them?
            commas, points = _shown_decimals(rest.filter(~rest.str.contains(_EITHER)))
        if commas or points:
            columns[name] = "." if points else ","
    if comma != point:
        return ("," if comma > point else "."), columns
    return default, columns


def _shown_decimals(s: pl.Series) -> tuple[int, int]:
    """How many values are numbers only with a decimal comma, and how many only with a decimal
    point (see ``guess_decimal``)."""
    values = _bare(s)
    values = values.filter(~values.str.contains(r"^[+-]?[0-9]+$"))
    fits_comma, fits_point = values.str.contains(f"^[+-]?{_NUMBER[',']}$"), values.str.contains(f"^[+-]?{_NUMBER['.']}$")
    return (fits_comma & ~fits_point).sum(), (fits_point & ~fits_comma).sum()


def detect_types(data: pl.DataFrame, decimals: Mapping[str, str], warn: Callable[[str], None]) -> pl.DataFrame:
    """``dtypes.infer_types`` with the decimal separator of each column (``decimals``: column ->
    "," or "."), and also: numbers with thousands separators only (1.000) become whole numbers,
    numbers without a digit before the decimal separator (,5) numbers; amounts (1.234,50 €,
    1 234,50 €, 123,45-, -$3.00) and percentages (12,5% is 0.125) become numbers when every value of
    the column is one. Digits with spaces between them are numbers only in amounts and percentages,
    as thousands (see ``_SPACED``): 1 234 and +49 30 1234567 stay text, and so do numbers with a
    leading zero (0815, 00,5: codes). Numbers that a decimal number (Float64) would change keep all
    their digits: a column with a number of more than 15 significant digits, or with a whole
    number beyond Int64, becomes exact decimals (Decimal) with the decimal places its values show;
    beyond the 38 digits of an exact decimal it stays text, and ``warn`` is told. All values of a
    column decide, not only the first ones."""
    notes: dict[str, list[str]] = {}  # the warnings of infer_types, by column
    typed = data.with_columns(
        [dtypes.infer_series(data[name], decimals[name], notes.setdefault(name, []).append) for name in data.columns]
    )
    schema = typed.schema  # once: each call builds the types of all columns again
    updates = []
    for name, dtype in data.schema.items():
        if dtype != pl.String or schema[name] not in (pl.Float64, pl.String):
            continue
        found = _numbers(data[name], decimals[name], floats=schema[name] == pl.Float64)
        if found is not None:
            column, message = found
            updates.append(column.alias(name))
            notes[name] = [message] if message else []
    for messages in notes.values():
        for message in messages:
            warn(message)
    return typed.with_columns(updates)


def _numbers(original: pl.Series, decimal: str, floats: bool) -> tuple[pl.Series, str | None] | None:
    """What a text column of numbers becomes (see ``detect_types``), with a warning or None; None
    when it stays as ``infer_series`` made it. ``floats``: that made decimal numbers of it (every
    value is a number like 12,5 or 1.000)."""
    column = original.alias("value")  # a fixed name, as in guess_decimal
    sample = _clean(column.drop_nulls().head(_SAMPLE)).drop_nulls()  # most columns end here, quickly
    if not sample.len():  # only spaces so far
        sample = _clean(column).drop_nulls()
    if not sample.str.contains("[0-9]").any():  # names and such: no pattern below fits them
        return None
    whole, plain, percent, amount = _patterns(decimal)
    if floats:
        if _fit(sample, whole):
            text = _clean(column)
            if text.drop_nulls().str.contains(whole).all():
                return _whole_numbers(original, text, decimal)
        return _decimal_numbers(original, column, decimal)
    # A pattern that a value of the sample doesn't fit fits not all values; one that every value of
    # the sample fits may still not fit a later value: all of them decide
    fitting = [pattern for pattern in (whole, plain, percent, amount) if _fit(sample, pattern)]
    if not fitting:
        return None
    text = _clean(column)
    values = text.drop_nulls()
    for pattern in fitting:
        if not values.str.contains(pattern).all():
            continue
        if pattern in (whole, plain) and values.str.contains(_CODE).any():
            return original, None  # codes like 0815 or 00,5
        if pattern == whole:
            return _whole_numbers(original, text, decimal)
        if pattern == amount and not _amounts(values):
            return None
        if _beyond_floats(values, decimal):
            return _exact(original, text, decimal, percent=pattern == percent)
        if pattern == percent:
            return _percentages(text, decimal), None
        parsed = dtypes.parse_numbers(text.str.replace_all(f"[{_CURRENCY}]", ""), decimal)  # also -$3.00
        return (parsed, None) if parsed.null_count() == text.null_count() else None
    return None


def _patterns(decimal: str) -> tuple[str, str, str, str]:
    """The patterns of whole numbers (1.000), numbers (1.234,5 and ,5), percentages (12,5 %) and
    amounts (1 234,50 €, -$3.00, 123,45-) with this decimal separator. Each one fits the values of
    the one before it, except percentages: they are tried in this order."""
    number, gap = _SPACED[decimal], f"{_SPACES}*"
    percent = f"^[+-]?{gap}{number}{gap}%$"
    amount = f"^{_SIGNED}{gap}{number}{gap}-?{gap}[{_CURRENCY}]?$"
    return _WHOLE[decimal], f"^[+-]?{_NUMBER[decimal]}$", percent, amount


def _whole_numbers(original: pl.Series, text: pl.Series, decimal: str) -> tuple[pl.Series, str | None] | None:
    """Whole numbers (cleaned texts, see ``_clean``): Int64, or beyond it exact decimals
    (Decimal(38, 0)), or beyond those text with a warning."""
    integers = dtypes.parse_integers(text, decimal)
    if integers.null_count() == text.null_count():
        return integers, None
    return _exact(original, text, decimal)


def _decimal_numbers(original: pl.Series, column: pl.Series, decimal: str) -> tuple[pl.Series, str | None] | None:
    """A column that ``infer_series`` made decimal numbers (``column``: its texts): codes with a
    leading zero (00,5) stay text, and numbers that a decimal number would change make exact
    decimals. Both are looked for in the texts as they are, without cleaning all of them first:
    most columns have neither."""
    if column.str.contains(r"^\s*[+-]?0[0-9]").any():
        return original, None
    if not _beyond_floats(column, decimal):  # the texts as they are: spaces around them don't change the numbers
        return None
    return _exact(original, _clean(column), decimal)


def _beyond_floats(values: pl.Series, decimal: str) -> bool:
    """Whether a decimal number (Float64) would change one of these numbers (texts that fit a
    pattern of numbers, also with spaces around them): one of more than 15 significant digits, or
    beyond the range of decimal numbers. The first long ones are looked at first: in a column of
    such numbers, one of them tells."""
    rows = (values.str.len_bytes() > _FLOAT_DIGITS).fill_null(False).arg_true()  # shorter ones have at most 15 digits
    for part in (rows.head(_SAMPLE), rows.slice(_SAMPLE)):
        long = values.gather(part)
        long = long.filter(long.str.count_matches("[0-9]") > _FLOAT_DIGITS)  # and so have those with fewer digits
        if long.len() and _plain(long, decimal).to_frame().select(_changed_by_floats(pl.col("value")).any()).item():
            return True
    return False


def _changed_by_floats(plain: pl.Expr) -> pl.Expr:
    """For numbers as plain text (-1234.50): whether a decimal number changes them. It keeps 15
    significant digits (zeros before the first digit and after the last one don't count), from
    about 1e-307 to 1e308."""
    counts = _counts(plain)
    number = plain.str.strip_chars_start("+-").str.strip_chars_start("0")  # .000123 for 0.000123
    zeros = number.str.len_bytes().cast(pl.Int64) - number.str.strip_chars_start(".0").str.len_bytes().cast(pl.Int64) - 1
    tiny = (counts["whole"] == 0) & (counts["significant"] > 0) & (zeros > 306)
    return (counts["significant"] > _FLOAT_DIGITS) | (counts["whole"] > 308) | tiny


def _counts(plain: pl.Expr) -> dict[str, pl.Expr]:
    """For numbers as plain text (-1234.50): the digits before the point (without zeros in front),
    the decimal places as written, the decimal places without zeros at the end, and the significant
    digits (zeros before the first digit and after the last one don't count: 1000 has one). With
    plain string operations: patterns take long for a million values."""
    number = plain.str.strip_chars_start("+-").str.strip_chars_start("0")  # 1234.50, .05 for 0.05, 1000
    length = number.str.len_bytes().cast(pl.Int64)
    point = number.str.find(".", literal=True).cast(pl.Int64)
    short = number.str.strip_chars_end("0").str.len_bytes().cast(pl.Int64)
    significant = number.str.replace(".", "", literal=True).str.strip_chars_start("0").str.strip_chars_end("0")
    return {
        "whole": point.fill_null(length),
        "places": pl.when(point.is_null()).then(0).otherwise(length - point - 1),
        "needed": pl.when(point.is_null()).then(0).otherwise((short - point - 1).clip(lower_bound=0)),
        "significant": significant.str.len_bytes().cast(pl.Int64),
    }


def _exact(original: pl.Series, text: pl.Series, decimal: str, percent: bool = False) -> tuple[pl.Series, str | None] | None:
    """Numbers (cleaned texts) as exact decimals (Decimal) with the decimal places they show, two
    more for percentages (divided by 100); with as few as they need, when those are too many. Beyond
    the 38 digits of an exact decimal the column stays text, with a warning. None when a value is no
    number after all (-3,00-)."""
    plain = _plain(text, decimal).to_frame()
    if percent:
        plain = plain.select(_hundredths(pl.col("value")))
    numbers = plain["value"]
    counts = plain.select(**_counts(pl.col("value")))
    whole, places = counts["whole"], counts["places"]
    written, most = places.max() or 0, whole.max() or 0
    scale = written if most + written <= _MOST_DIGITS else counts["needed"].max() or 0
    if most + scale > _MOST_DIGITS:
        return original, _too_long(original.name, text[(whole + places).arg_max()])
    exact = numbers.cast(pl.Decimal(_MOST_DIGITS, scale), strict=False)
    return (exact, None) if exact.null_count() == text.null_count() else None


def _hundredths(plain: pl.Expr) -> pl.Expr:
    """Numbers as plain text, divided by 100 exactly: the point two places to the left (12.5 is
    0.125, -5 is -0.05)."""
    sign = plain.str.extract(r"^([+-]?)", 1)
    unsigned = plain.str.strip_chars_start("+-")
    whole = unsigned.str.extract(r"^([0-9]*)", 1).str.pad_start(3, "0")
    places = unsigned.str.extract(r"\.([0-9]*)$", 1).fill_null("")
    return pl.concat_str(sign, whole.str.head(-2), pl.lit("."), whole.str.tail(2), places).alias("value")


def _too_long(name: str, example: str) -> str:
    return f"{name} stays text: its numbers are too long to be kept exactly, e.g. '{example}'"


def _plain(text: pl.Series, decimal: str) -> pl.Series:
    """Numbers, amounts and percentages (cleaned texts) as plain numbers with all their digits
    (-1234.50, see ``dtypes.plain_numbers``), named "value". Currency signs go first, also after a
    sign (-$3.00), and so does %."""
    return dtypes.plain_numbers(text.str.replace_all(f"[{_CURRENCY}%]", ""), decimal).alias("value")


def _fit(values: pl.Series, pattern: str) -> bool:
    return values.len() > 0 and values.str.contains(pattern).all()


def _amounts(values: pl.Series) -> bool:
    """Amounts with a currency sign or a minus at the end (else they would be numbers already),
    without leading zeros (codes like 0815 stay text)."""
    leading_zero = f"^{_SIGNED}{_SPACES}*0[0-9]"
    return values.str.contains(f"[{_CURRENCY}]|-$").any() and not values.str.contains(leading_zero).any()


def _percentages(text: pl.Series, decimal: str) -> pl.Series:
    """Percentages as numbers, read exactly: 1,1% is 0.011 (divided by 100 it would be
    0.011000000000000001)."""
    thousands = "." if decimal == "," else ","
    number = text.str.replace_all(_SPACES, "").str.strip_suffix("%").str.replace_all(thousands, "", literal=True)
    return (number.str.replace(decimal, ".", literal=True) + "e-2").cast(pl.Float64, strict=False)


def _clean(s: pl.Series) -> pl.Series:
    """Text without white space at the ends (spaces, tabs, line ends); empty values as null."""
    text = s.str.strip_chars()
    return pl.select(pl.when(pl.lit(text) == "").then(None).otherwise(pl.lit(text)).alias(s.name)).to_series()


def _bare(s: pl.Series) -> pl.Series:
    """The numbers in amounts and percentages, without spaces and currency signs (also after a
    sign: -$3.00), for counting decimal separators."""
    bare = _clean(s).drop_nulls().str.replace_all(_SPACES, "").str.replace_all(f"[{_CURRENCY}]", "")
    return bare.str.strip_chars("%").str.strip_suffix("-")

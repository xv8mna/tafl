"""Numbers and dates in text columns, as the CSV import detects them: the decimal separator the
values show, and the types the columns get (``dtypes.infer_series`` with the decimal separator of
each column, and also numbers with thousands separators only, amounts and percentages).

    decimal, own = detection.guess_decimal(df, default=".")
    typed = detection.detect_types(df, {name: own.get(name, decimal) for name in df.columns}, warn)
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

import polars as pl

from . import dtypes

# Numbers as text: 1.234,56 or 1234,56 (decimal comma) and 1,234.56 or 1234.56 (decimal point).
# Thousands separators come after 1 to 3 digits that don't start with 0: 0.500 is a decimal.
_NUMBER = {
    ",": r"([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)(,[0-9]+)?",
    ".": r"([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)(\.[0-9]+)?",
}
_WHOLE = {",": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)$", ".": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)$"}
_EITHER = r"^[+-]?([0-9]+|[1-9][0-9]{0,2}[.,][0-9]{3})$"  # 12, 1.250 and 1,250 fit both decimal separators
_SPACES = "[   ]"  # a space, a no-break space and a narrow no-break space
_CURRENCY = "€$£"
# Amounts and percentages can also have spaces between the thousands (1 234,50 €), only in groups
# of three digits: other digits with spaces between them (phone numbers, 2025 01 31) stay text.
_SPACED = {
    ",": rf"([0-9]+|[1-9][0-9]{{0,2}}(\.[0-9]{{3}})+|[1-9][0-9]{{0,2}}({_SPACES}[0-9]{{3}})+)(,[0-9]+)?",
    ".": rf"([0-9]+|[1-9][0-9]{{0,2}}(,[0-9]{{3}})+|[1-9][0-9]{{0,2}}({_SPACES}[0-9]{{3}})+)(\.[0-9]+)?",
}


def guess_decimal(df: pl.DataFrame, default: str) -> tuple[str, dict[str, str]]:
    """The decimal separator ("," or ".") of the text columns of ``df`` together, and of each
    column whose values show one. A value shows a decimal comma (1.234,56 / 12,5 / 0,500 /
    1.234.567) or a decimal point (1,234.56 / 12.5 / 0.500) when it fits only that one; values
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
    "," or "."), and also: numbers with thousands separators only (1.000) become whole numbers
    (exact, up to 19 digits); amounts (1.234,50 €, 1 234,50 €, 123,45-) and percentages (12,5% is
    0.125) become numbers when every value of the column is one. Digits with spaces between them
    are numbers only in amounts and percentages, as thousands (see ``_SPACED``): 1 234 and +49 30
    1234567 stay text. Numbers too long to be kept exactly stay text, and ``warn`` is told."""
    notes: dict[str, list[str]] = {}  # the warnings of infer_types, by column
    typed = data.with_columns(
        [dtypes.infer_series(data[name], decimals[name], notes.setdefault(name, []).append) for name in data.columns]
    )
    updates = []
    for name, dtype in data.schema.items():
        kind = typed[name].dtype
        if dtype != pl.String or kind not in (pl.Float64, pl.String):
            continue
        decimal = decimals[name]
        number, gap = _SPACED[decimal], f"{_SPACES}*"
        whole, percent = _WHOLE[decimal], f"^[+-]?{gap}{number}{gap}%$"
        amount = f"^[{_CURRENCY}]?{gap}[+-]?{gap}{number}{gap}-?{gap}[{_CURRENCY}]?$"
        column = data[name].alias("value")  # a fixed name, as in guess_decimal
        sample = _clean(column.drop_nulls().head(1000)).drop_nulls()  # most columns end here, quickly
        if not sample.str.contains("[0-9]").any():  # names and such: no pattern below fits them
            continue
        if kind == pl.Float64 or _fit(sample, whole):  # text: numbers too long for a decimal number
            pattern = whole
        else:
            pattern = percent if _fit(sample, percent) else amount
        if not _fit(sample, pattern) or (pattern == amount and not _amounts(sample)):
            continue
        text = _clean(column)
        values = text.drop_nulls()
        if not values.str.contains(pattern).all():
            continue
        if pattern == whole:
            if kind == pl.String and values.str.contains(r"^[+-]?0[0-9]").any():
                continue  # codes like 0815
            integers = dtypes.parse_integers(text, decimal)
            if integers.null_count() == text.null_count():  # else beyond Int64: the text stays
                updates.append(integers.alias(name))
                notes.pop(name, None)
            continue
        lost = dtypes.float_loses_digits(_bare(values), decimal)
        if lost.any():
            notes[name].append(f"{name} stays text: its numbers are too long to be kept exactly, e.g. '{values.filter(lost)[0]}'")
        elif pattern == percent:
            updates.append(_percentages(text, decimal).alias(name))
        elif _amounts(values):
            parsed = dtypes.parse_numbers(text, decimal)
            if parsed.null_count() == text.null_count():
                updates.append(parsed.alias(name))
    for messages in notes.values():
        for message in messages:
            warn(message)
    return typed.with_columns(updates)


def _fit(values: pl.Series, pattern: str) -> bool:
    return values.len() > 0 and values.str.contains(pattern).all()


def _amounts(values: pl.Series) -> bool:
    """Amounts with a currency sign or a minus at the end (else they would be numbers already),
    without leading zeros (codes like 0815 stay text)."""
    leading_zero = f"^[{_CURRENCY}]?{_SPACES}*[+-]?{_SPACES}*0[0-9]"
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
    """The numbers in amounts and percentages, without spaces, for counting decimal separators."""
    return _clean(s).drop_nulls().str.replace_all(_SPACES, "").str.strip_chars(_CURRENCY + "%").str.strip_suffix("-")

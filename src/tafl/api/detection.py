"""Numbers and dates in text columns, as the CSV import detects them: the decimal separator the
values show, and the types the columns get (``dtypes.infer_series`` with the decimal separator of
each column, and also numbers with thousands separators only, numbers without a digit before the
decimal separator, amounts and percentages, and exact decimals for numbers too long for a decimal
number).

    decimal, own = detection.guess_decimal(df, default=".")
    typed = detection.detect_types(df, {name: own.get(name, decimal) for name in df.columns}, warn)

For the files of a folder, each with its own formats, ``guess_decimals`` gives each file's decimal
separator and own columns, as ``guess_decimal`` gives them for one file, and ``detect_files`` the
types of each file's columns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import polars as pl

from . import dtypes
from .names import col, cols

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
_SHOWN = 2_000  # rows whose values show the decimal separator of a file
_PART = "\x1fpart"  # the number of a file among several: no column of a file has this name (see files.ONE_COLUMN)
_ISO_T = r"^[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}T[0-9]"  # a date-time like 2026-01-31T08:30:00, which has a letter


def guess_decimal(df: pl.DataFrame, default: str) -> tuple[str, dict[str, str]]:
    """The decimal separator ("," or ".") of the text columns of ``df`` together, and of each
    column whose values show one. A value shows a decimal comma (1.234,56 / 12,5 / ,5 / 0,500 /
    1.234.567) or a decimal point (1,234.56 / 12.5 / .5 / 0.500) when it fits only that one; values
    like 1.000 and 1,000 fit both. A column with a value that shows a point has a decimal point,
    else one with a value that shows a comma a decimal comma. For all columns together, the
    separator that more values of a sample show; without a difference, ``default``."""
    comma, point, columns = _shown(df)
    if comma != point:
        return ("," if comma > point else "."), columns
    return default, columns


def guess_decimals(frames: Sequence[pl.DataFrame], defaults: Sequence[str]) -> tuple[str, list[tuple[str, dict[str, str]]]]:
    """``guess_decimal`` for the files of a folder (``frames``), each with its own formats and
    ``default`` (that of its delimiter): the separator of all files together, and for each file
    its separator and that of its columns whose values show one, in the file or else in the other
    files. A column follows the values of its file that show a separator; else those of the same
    column in the other files (1.000 beside 0.500 there, as when all files are read together); else
    the other columns of its file; else the values of all files; without any, its default."""
    shown = _shown_files(frames)
    comma, point = sum(counts[0] for counts in shown), sum(counts[1] for counts in shown)
    every = None if comma == point else "," if comma > point else "."
    columns: dict[str, str] = {}  # a point in one file gives the column a point, as in one file
    for _commas, _points, own in shown:
        for name, decimal in own.items():
            if decimal == "." or name not in columns:
                columns[name] = decimal
    files = []
    for frame, (commas, points, own), default in zip(frames, shown, defaults, strict=True):
        decimal = ("," if commas > points else ".") if commas != points else every or default
        files.append((decimal, {name: own.get(name) or columns[name] for name in frame.columns if name in own or name in columns}))
    return every or (defaults[0] if defaults else "."), files


def _shown(df: pl.DataFrame) -> tuple[int, int, dict[str, str]]:
    """How many values of a sample of the text columns of ``df`` show a decimal comma and how many
    a decimal point, and the separator of each column whose values show one (see ``guess_decimal``)."""
    comma = point = 0
    columns: dict[str, str] = {}
    sample = df.head(_SHOWN)
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
    return comma, point, columns


def _shown_files(frames: Sequence[pl.DataFrame]) -> list[tuple[int, int, dict[str, str]]]:
    """``_shown`` of each of several frames. Those of up to 2,000 rows (all their values are the
    sample) are looked at in one go, for up to 100 columns at a time: one by one, many small files
    take long."""
    small = [i for i, frame in enumerate(frames) if 0 < frame.height <= _SHOWN] if len(frames) > 1 else []
    together = set(small)
    shown = [(0, 0, {}) if i in together else _shown(frame) for i, frame in enumerate(frames)]
    parts = [frames[i].select(pl.col(pl.String)).with_columns(pl.lit(n, pl.UInt32).alias(_PART)) for n, i in enumerate(small)]
    stacked = pl.concat(parts, how="diagonal") if parts else pl.DataFrame()
    texts = [name for name in stacked.columns if name != _PART]
    marked = stacked.select(pl.col(pl.String).str.contains("[.,]").any()).row(0) if texts else ()  # only these can show one
    names = [name for name, separators in zip(texts, marked, strict=True) if separators]
    for start in range(0, len(names), 100):
        chunk = names[start : start + 100]
        flags = [flag.alias(f"{kind}{k}") for k, name in enumerate(chunk) for kind, flag in zip("cp", _shows(col(name)), strict=True)]
        # the values first, then only sums by file: patterns inside the groups take much memory
        for n, *values in stacked.select(col(_PART), *flags).group_by(_PART).sum().iter_rows():
            commas, points, own = shown[small[n]]
            found = {name: "." if p else "," for name, c, p in zip(chunk, values[0::2], values[1::2], strict=True) if c or p}
            shown[small[n]] = (commas + sum(values[0::2]), points + sum(values[1::2]), own | found)
    return shown


def _shows(text: pl.Expr) -> tuple[pl.Expr, pl.Expr]:
    """Whether a value shows only a decimal comma, and whether only a decimal point: the values
    that ``_shown_decimals`` counts, as expressions."""
    stripped = text.str.strip_chars()
    bare = pl.when(stripped == "").then(None).otherwise(stripped).str.replace_all(_SPACES, "")
    bare = bare.str.replace_all(f"[{_CURRENCY}]", "").str.strip_chars("%").str.strip_suffix("-")
    number = ~bare.str.contains(r"^[+-]?[0-9]+$")
    comma, point = bare.str.contains(f"^[+-]?{_NUMBER[',']}$"), bare.str.contains(f"^[+-]?{_NUMBER['.']}$")
    return comma & ~point & number, point & ~comma & number


def _shown_decimals(s: pl.Series) -> tuple[int, int]:
    """How many values are numbers only with a decimal comma, and how many only with a decimal
    point (see ``guess_decimal``)."""
    values = _bare(s)
    values = values.filter(~values.str.contains(r"^[+-]?[0-9]+$"))
    fits_comma, fits_point = values.str.contains(f"^[+-]?{_NUMBER[',']}$"), values.str.contains(f"^[+-]?{_NUMBER['.']}$")
    return (fits_comma & ~fits_point).sum(), (fits_point & ~fits_comma).sum()


def detect_files(
    frames: Sequence[pl.DataFrame],
    guesses: Sequence[tuple[str, Mapping[str, str]]],
    warn: Callable[[str], None],
    between: Callable[[], None] | None = None,
) -> list[pl.DataFrame]:
    """``detect_types`` for each of the files of a folder (``frames``, with their ``guesses`` from
    ``guess_decimals``): each file's columns get the types that the file on its own gives them, with
    its own decimal separators and date formats. Files with the same separators are looked at
    together, which is as for each one on its own, only faster for many small files; a column that
    is text there is looked at in each of these files (one can have dates as 31.01.2026, another as
    2026-01-31). ``between`` runs between the groups of files, e.g. to stop when asked to."""
    typed = list(frames)
    groups: dict[tuple, list[int]] = {}
    for i, (decimal, own) in enumerate(guesses):
        groups.setdefault((decimal, tuple(sorted(own.items()))), []).append(i)
    for (decimal, own), members in groups.items():
        if between is not None:
            between()
        files, shown = [frames[i] for i in members], dict(own)
        together = files[0] if len(files) == 1 else pl.concat(files, how="diagonal")
        decimals = {name: shown.get(name, decimal) for name in together.columns}
        result = detect_types(together, decimals, warn)
        each = _each_file(result, together, files, decimals, warn) if len(files) > 1 else {}
        offset = 0
        for k, (i, frame) in enumerate(zip(members, files, strict=True)):
            part = result.slice(offset, frame.height)
            part = part if part.columns == frame.columns else part.select(cols(frame.columns))  # its own columns, in its order
            changes = [found[k] for name, found in each.items() if name in frame.columns]
            typed[i] = part.with_columns(changes) if changes else part
            offset += frame.height
    return typed


def _each_file(
    result: pl.DataFrame, together: pl.DataFrame, files: list[pl.DataFrame], decimals: Mapping[str, str], warn: Callable[[str], None]
) -> dict[str, list[pl.Series]]:
    """For ``detect_files``: the columns that stayed text in ``result`` (the files together), as
    each file gives them, where every file gives a type: column -> a column for each file. Each
    column is looked at in one file after the other, up to the first that keeps it text. A column
    with a word or a code like 0815 in its first values stays text at once: a file with one of
    them gives no type (true and false and date-times like 2026-01-31T08:30 have letters, but they
    can)."""
    before, after = together.schema, result.schema
    empty = dict(zip(together.columns, together.null_count().row(0), strict=True))  # a column without values has no type
    texts = {
        name: col(name).str.strip_chars()
        for name in together.columns
        if before[name] == pl.String and after[name] == pl.String and empty[name] < together.height
    }
    if not texts:
        return {}
    words = {name: text.str.contains("[A-Za-z]") & ~text.str.to_lowercase().is_in(["true", "false"]) & ~text.str.contains(_ISO_T) for name, text in texts.items()}
    seen = together.head(_SAMPLE).select([(words[name] | text.str.contains(r"^[+-]?0[0-9]+$")).any().alias(name) for name, text in texts.items()])
    each: dict[str, list[pl.Series]] = {}
    for name, stays in zip(texts, seen.row(0), strict=True):
        if stays:
            continue
        found, notes = [], []
        for frame in files:
            if name not in frame.columns:
                found.append(pl.Series(name, [], pl.String))  # not used: the file has no such column
                continue
            column = detect_types(frame.select(col(name)), {name: decimals[name]}, notes.append)[name]
            if column.dtype == pl.String and column.null_count() < column.len():
                break  # text in this file: text in all of them, as in one file
            found.append(column)
        else:
            each[name] = found
            for message in notes:
                warn(message)
    return each


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
        parsed = dtypes.parse_numbers(text, decimal)  # as Change type reads them, also -$3.00
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
    (-1234.50, see ``dtypes.plain_numbers``, which reads -$3.00 as Change type does), named
    "value". The % goes first."""
    return dtypes.plain_numbers(text.str.replace_all("%", "", literal=True), decimal).alias("value")


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

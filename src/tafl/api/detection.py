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
_FLOAT_DIGITS = 15  # a decimal number (Float64) keeps every number of up to 15 significant digits
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
    its own decimal separators and date formats. Files whose separators agree on the columns they
    share are looked at together, as one file: files of one format give what all their rows in one
    file give; a column that is text there is looked at in each of these files (one can have dates
    as 31.01.2026, another as 2026-01-31). Then the files are made to fit together as one file of
    all their values would: a column with only blanks in a file has no values there when another
    file gives the column a type, and a column of numbers in every file has the numbers that one
    file of all of them gives (see ``_numbers_of_files``). ``warn`` is told what ``detect_types``
    tells, but not that digits of date-times were cut in a column that ends as text, with all its
    digits. ``between`` runs between the groups of files, e.g. to stop when asked to."""
    typed = list(frames)
    notes: list[tuple[str, str, bool]] = []  # (column, warning, whether the column has a type there), in order
    mappings = [{name: own.get(name, decimal) for name in frame.columns} for frame, (decimal, own) in zip(frames, guesses, strict=True)]
    for decimals, members in _groups(mappings):
        if between is not None:
            between()
        files = [frames[i] for i in members]
        together = files[0] if len(files) == 1 else pl.concat(files, how="diagonal")
        result, found = _detect(together, decimals)
        schema = result.schema  # once: each call builds the types of all columns again
        notes += [(name, message, schema[name] != pl.String) for name, messages in found.items() for message in messages]
        each = _each_file(result, together, files, decimals, notes) if len(files) > 1 else {}
        offset = 0
        for k, (i, frame) in enumerate(zip(members, files, strict=True)):
            part = result.slice(offset, frame.height)
            part = part if part.columns == frame.columns else part.select(cols(frame.columns))  # its own columns, in its order
            changes = [found[k] for name, found in each.items() if name in frame.columns]
            typed[i] = part.with_columns(changes) if changes else part
            offset += frame.height
    if len(frames) > 1:
        _without_blanks(typed)
        _numbers_of_files(typed, frames, mappings, notes)
    typed_notes = {name for name, _message, has_type in notes if has_type}
    text = _text_at_last(typed, typed_notes) if len(frames) > 1 and typed_notes else set()
    for name, message, has_type in notes:
        if not (has_type and name in text):  # e.g. "the digits below a nanosecond were cut"
            warn(message)
    return typed


def _text_at_last(typed: list[pl.DataFrame], names: set[str]) -> set[str]:
    """Those of these columns that putting the files together makes text (``files.combine``, as
    Append does): their types in the files have no type in common that keeps every value."""
    schemas = [frame.schema for frame in typed]
    text = set()
    for name in names:
        columns = [frame[name] for frame, schema in zip(typed, schemas, strict=True) if name in schema]
        if len({column.dtype for column in columns}) > 1 and dtypes.common_type(columns).dtype == pl.String:
            text.add(name)
    return text


def _groups(mappings: list[dict[str, str]]) -> list[tuple[dict[str, str], list[int]]]:
    """The files (by their decimal separator of each column, ``mappings``) in groups whose separators
    agree on every column that two of them have: each group's separators and files, in file
    order. A file with more or fewer columns joins the files of its format."""
    groups: list[tuple[dict[str, str], list[int]]] = []
    for i, mapping in enumerate(mappings):
        for decimals, members in groups:
            if all(decimals.get(name, decimal) == decimal for name, decimal in mapping.items()):
                decimals.update(mapping)
                members.append(i)
                break
        else:
            groups.append((dict(mapping), [i]))
    return groups


def _kinds(schemas: list[pl.Schema]) -> dict[str, set[pl.DataType]]:
    """The types that each column has in these files."""
    kinds: dict[str, set[pl.DataType]] = {}
    for schema in schemas:
        for name, dtype in schema.items():
            kinds.setdefault(name, set()).add(dtype)
    return kinds


def _blank(s: pl.Series) -> bool:
    """Whether a text column has only blanks (spaces, "") or no values."""
    return not (s.alias("value").str.strip_chars().str.len_bytes().max() or 0)


def _without_blanks(typed: list[pl.DataFrame]) -> None:
    """For ``detect_files``: a column that is text in a file only because all its values are blank
    (spaces, "") has no values there, when another file gives the column a type: as in one file,
    where blanks beside numbers are empty. A column that is blank in every file stays text."""
    schemas = [frame.schema for frame in typed]
    for name, kinds in _kinds(schemas).items():
        if pl.String not in kinds or len(kinds) < 2:
            continue
        for i, schema in enumerate(schemas):
            if schema.get(name) == pl.String and _blank(typed[i][name]):
                typed[i] = typed[i].with_columns(pl.lit(None, pl.String).alias(name))


def _numbers_of_files(typed: list[pl.DataFrame], texts: Sequence[pl.DataFrame], mappings: list[dict[str, str]], notes: list[tuple[str, str, bool]]) -> None:
    """For ``detect_files``: a column of numbers in every file (whole numbers, decimal numbers, exact
    decimals) whose types Append can't put together without changing a value (a decimal number
    beside an exact decimal, a whole number beyond 2^53 beside decimal numbers) gets the numbers
    that one file of all its values gives (see ``_numbers_together``), and a warning about it goes to
    ``notes``."""
    schemas = [frame.schema for frame in typed]
    for name, kinds in _kinds(schemas).items():
        numbers = kinds - {pl.String}  # a column of a file can be text without values
        if len(numbers) < 2 or not all(kind.is_numeric() for kind in numbers):
            continue
        where = [i for i, schema in enumerate(schemas) if name in schema and typed[i][name].null_count() < typed[i].height]
        columns = [typed[i][name] for i in where]
        if any(column.dtype == pl.String for column in columns) or dtypes.common_type(columns).dtype != pl.String:
            continue  # text in a file (0815, n/a), as in one file; or Append keeps every value
        warn = lambda message, name=name: notes.append((name, message, False))  # noqa: E731
        found = _numbers_together(name, columns, [texts[i][name] for i in where], [mappings[i][name] for i in where], warn)
        for i, column in zip(where, found or [], strict=found is not None):
            typed[i] = typed[i].with_columns(column.alias(name))


def _numbers_together(
    name: str, columns: list[pl.Series], texts: list[pl.Series], decimals: list[str], warn: Callable[[str], None]
) -> list[pl.Series] | None:
    """The numbers of a column in several files (``columns``, from ``texts`` with the decimal
    separators ``decimals``) as one file of all of them gives them: exact decimals when a decimal
    number would change one of them (see ``_beyond_floats``), with the decimal places that the
    values show; beyond the 38 digits of an exact decimal the texts, and ``warn`` is told; else
    decimal numbers. Each file's texts are read with its own decimal separator. None when a value
    isn't read as the file read it."""
    cleaned = [_clean(text.alias("value")) for text in texts]
    percents = [_fit(text.drop_nulls(), _patterns(decimal)[2]) for text, decimal in zip(cleaned, decimals, strict=True)]
    parts = list(zip(columns, cleaned, decimals, percents, strict=True))
    if not any(_beyond_floats(text.drop_nulls(), decimal, percent) for _, text, decimal, percent in parts):
        found = [  # e.g. a whole number beyond 2^53 that a decimal number keeps, or 10^20
            column if column.dtype == pl.Float64
            else column.cast(pl.Float64) if column.dtype.is_integer()
            else _percentages(text, decimal) if percent
            else dtypes.parse_numbers(text, decimal)
            for column, text, decimal, percent in parts
        ]
    else:
        plains = [_plain(text, decimal).to_frame() for text, decimal in zip(cleaned, decimals, strict=True)]
        plain = pl.concat([frame.select(_hundredths(pl.col("value"))) if percent else frame for frame, percent in zip(plains, percents, strict=True)])
        exact, longest = _exact_numbers(plain["value"])
        if exact is None:
            warn(_too_long(name, pl.concat(cleaned)[longest]))
            return list(texts)
        found, offset = [], 0
        for text in texts:
            found.append(exact.slice(offset, text.len()))
            offset += text.len()
    return found if all(number.null_count() == text.null_count() for number, text in zip(found, cleaned, strict=True)) else None


def _each_file(
    result: pl.DataFrame,
    together: pl.DataFrame,
    files: list[pl.DataFrame],
    decimals: Mapping[str, str],
    notes: list[tuple[str, str, bool]],
) -> dict[str, list[pl.Series]]:
    """For ``detect_files``: the columns that stayed text in ``result`` (the files together), as
    each file gives them, where every file gives a type or has only blanks there (and one gives a
    type): column -> a column for each file. Each column is looked at in one file after the other,
    up to the first that keeps it text. A column
    with a word or a code like 0815 in its first values stays text at once: a file with one of
    them gives no type (true and false and date-times like 2026-01-31T08:30 have letters, but they
    can). The warnings about a column go to ``notes`` (see ``detect_files``) when it gets a type in
    every file."""
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
        found, said = [], []
        for frame in files:
            if name not in frame.columns:
                found.append(pl.Series(name, [], pl.String))  # not used: the file has no such column
                continue
            detected, messages = _detect(frame.select(col(name)), {name: decimals[name]})
            column = detected[name]
            if column.dtype == pl.String and not _blank(column):
                break  # text in this file: text in all of them, as in one file
            found.append(column)  # only blanks: no values (see _without_blanks)
            said += [(name, message, column.dtype != pl.String) for message in messages.get(name, [])]
        else:
            if any(column.dtype != pl.String for column in found):
                each[name] = found
                notes += said
    return each


def detect_types(data: pl.DataFrame, decimals: Mapping[str, str], warn: Callable[[str], None]) -> pl.DataFrame:
    """``dtypes.infer_types`` with the decimal separator of each column (``decimals``: column ->
    "," or "."), and also: numbers with thousands separators only (1.000) become whole numbers,
    numbers without a digit before the decimal separator (,5) numbers; amounts (1.234,50 €,
    1 234,50 €, 123,45-, -$3.00) and percentages (12,5% is 0.125) become numbers when every value of
    the column is one. Digits with spaces between them are numbers only in amounts and percentages,
    as thousands (see ``_SPACED``): 1 234 and +49 30 1234567 stay text, and so do numbers with a
    leading zero (0815, 00,5: codes). Numbers that a decimal number (Float64) would change keep all
    their digits: a column with a number that its decimal number gives back with other digits
    (12345678901234,5678; ``dtypes.float_loses_digits``), or with a whole number beyond Int64,
    becomes exact decimals (Decimal) with the decimal places its values show; beyond the 38 digits
    of an exact decimal it stays text, and ``warn`` is told. A decimal number as a program writes it
    (0.30000000000000004) stays one. All values of a column decide, not only the first ones."""
    typed, notes = _detect(data, decimals)
    for messages in notes.values():
        for message in messages:
            warn(message)
    return typed


def _detect(data: pl.DataFrame, decimals: Mapping[str, str]) -> tuple[pl.DataFrame, dict[str, list[str]]]:
    """``detect_types``, with its warnings by column instead of telling them."""
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
    return typed.with_columns(updates), notes


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
        if _beyond_floats(values, decimal, percent=pattern == percent):
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
    decimals. ``infer_series`` made decimal numbers only of numbers whose digits they keep
    (``dtypes.float_loses_digits``): only one too close to 0 is left (see ``_tiny``). Both are
    looked for in the texts as they are, without cleaning all of them first: most columns have
    neither."""
    if column.str.contains(r"^\s*[+-]?0[0-9]").any():
        return original, None
    if not _beyond_digits(column, decimal):  # the texts as they are: spaces around them don't change the numbers
        return None
    return _exact(original, _clean(column), decimal)


def _beyond_floats(values: pl.Series, decimal: str, percent: bool = False) -> bool:
    """Whether a decimal number (Float64) would change one of these numbers (texts that fit a
    pattern of numbers, also with spaces around them; with ``percent``, divided by 100): one that
    its decimal number gives back with other digits (``dtypes.float_loses_digits``:
    12345678901234.5678, not 0.30000000000000004 or 0.3333333333333333, which a program writes for
    its decimal numbers), or one of ``_beyond_digits``. Only numbers of more than 15 digits can
    be such a number; the first of them are looked at first: in a column of them, one tells."""
    rows = (values.str.len_bytes() > _FLOAT_DIGITS).fill_null(False).arg_true()  # shorter ones have at most 15 digits
    for part in (rows.head(_SAMPLE), rows.slice(_SAMPLE)):
        long = values.gather(part)
        long = long.filter(long.str.count_matches("[0-9]") > _FLOAT_DIGITS)  # and so have those with fewer digits
        if not long.len():
            continue
        plain = _plain(long, decimal).to_frame()
        plain = plain.select(_hundredths(pl.col("value"))) if percent else plain
        if dtypes.float_loses_digits(plain["value"], ".").any():
            return True
    return _beyond_digits(values, decimal, percent)


def _beyond_digits(values: pl.Series, decimal: str, percent: bool = False) -> bool:
    """Whether one of these numbers is beyond decimal numbers whatever its digits: so close to 0
    that its decimal number is 0 (below about 1e-307), or with a whole part beyond Int64
    (9,223,372,036,854,775,807) of more than 15 significant digits: a decimal number holds some of
    them, as 2^63, but an export writes them with 15 digits (9223372036854780000), while 1.5e20 is
    written as it is. ``dtypes.float_loses_digits`` looks at the digits only. Such a number takes
    more than 18 characters."""
    rows = (values.str.len_bytes() > 18).fill_null(False).arg_true()
    if not rows.len():
        return False
    plain = _plain(values.gather(rows), decimal).to_frame()
    plain = plain.select(_hundredths(pl.col("value"))) if percent else plain
    counts = _counts(pl.col("value"))
    number = pl.col("value").str.strip_chars_start("+-").str.strip_chars_start("0")  # .000123 for 0.000123
    zeros = number.str.len_bytes().cast(pl.Int64) - number.str.strip_chars_start(".0").str.len_bytes().cast(pl.Int64) - 1
    tiny = (counts["whole"] == 0) & (counts["significant"] > 0) & (zeros > 306)
    whole = number.str.head(counts["whole"])  # the digits before the point
    beyond = (counts["whole"] > 19) | ((counts["whole"] == 19) & (whole.cast(pl.UInt64, strict=False) > 2**63 - 1))
    big = beyond & (whole.str.strip_chars_end("0").str.len_bytes() > _FLOAT_DIGITS)
    return bool(plain.select((tiny | big).any()).item())


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
    exact, longest = _exact_numbers(plain["value"])
    if exact is None:
        return original, _too_long(original.name, text[longest])
    return (exact, None) if exact.null_count() == text.null_count() else None


def _exact_numbers(numbers: pl.Series) -> tuple[pl.Series | None, int]:
    """Numbers as plain text (-1234.50, named "value") as exact decimals with the decimal places
    they show, or with as few as they need when those are too many: the decimals, or None and the
    row of the longest number when they need more than the 38 digits of an exact decimal."""
    counts = numbers.to_frame().select(**_counts(pl.col("value")))
    whole, places = counts["whole"], counts["places"]
    written, most = places.max() or 0, whole.max() or 0
    scale = written if most + written <= _MOST_DIGITS else counts["needed"].max() or 0
    if most + scale > _MOST_DIGITS:
        return None, (whole + places).arg_max()
    return numbers.cast(pl.Decimal(_MOST_DIGITS, scale), strict=False), -1


def _hundredths(plain: pl.Expr) -> pl.Expr:
    """Numbers as plain text, divided by 100 exactly: the point two places to the left (12.5 is
    0.125, -5 is -0.05)."""
    sign = plain.str.extract(r"^([+-]?)", 1)
    unsigned = plain.str.strip_chars_start("+-")
    whole = unsigned.str.extract(r"^([0-9]*)", 1).str.pad_start(3, "0")
    places = unsigned.str.extract(r"\.([0-9]*)$", 1).fill_null("")
    return pl.concat_str(sign, whole.str.head(-2), pl.lit("."), whole.str.tail(2), places).alias("value")


def _too_long(name: str, example: str) -> str:
    shown = example if len(example) <= 40 else example[:39] + "…"  # a number can have thousands of digits
    return f"{name} stays text: its numbers are too long to be kept exactly, e.g. '{shown}'"


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

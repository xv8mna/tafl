"""CSV and text files: import and export."""

from __future__ import annotations

import codecs
import io
import math
import re
from collections.abc import Callable
from decimal import ROUND_DOWN, ROUND_HALF_UP, Context
from pathlib import Path

import polars as pl

from tafl.api import (
    ActionError,
    Endpoint,
    P,
    action,
    combine,
    dtypes,
    endpoints,
    file_endpoint,
    files,
    source_endpoints,
    source_files,
    source_params,
    source_summary,
    sources_fingerprint,
    unique_names,
)

_DELIMITERS = [(";", "Semicolon ;"), (",", "Comma ,"), ("tab", "Tab"), ("|", "Pipe |")]
_ENCODINGS = {"utf-8": "UTF-8", "cp1252": "Windows (ANSI)", "latin-1": "Latin-1", "utf-16": "UTF-16"}
_CHUNK = 1 << 20
_ONE_COLUMN = "\x1f"  # the delimiter for files that have none: text doesn't contain it

# Numbers as text: 1.234,56 or 1234,56 (decimal comma) and 1,234.56 or 1234.56 (decimal point).
# Thousands separators come after 1 to 3 digits that don't start with 0: 0.500 is a decimal.
_NUMBER = {
    ",": r"([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)(,[0-9]+)?",
    ".": r"([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)(\.[0-9]+)?",
}
_WHOLE = {",": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(\.[0-9]{3})+)$", ".": r"^[+-]?([0-9]+|[1-9][0-9]{0,2}(,[0-9]{3})+)$"}
_EITHER = r"^[+-]?([0-9]+|[1-9][0-9]{0,2}[.,][0-9]{3})$"  # 12, 1.250 and 1,250 fit both decimal separators
_SPACES = "[   ]"
_CURRENCY = "€$£"
# Amounts and percentages can also have spaces between the thousands (1 234,50 €), only in groups
# of three digits: other digits with spaces between them (phone numbers, 2025 01 31) stay text.
_SPACED = {
    ",": rf"([0-9]+|[1-9][0-9]{{0,2}}(\.[0-9]{{3}})+|[1-9][0-9]{{0,2}}({_SPACES}[0-9]{{3}})+)(,[0-9]+)?",
    ".": rf"([0-9]+|[1-9][0-9]{{0,2}}(,[0-9]{{3}})+|[1-9][0-9]{{0,2}}({_SPACES}[0-9]{{3}})+)(\.[0-9]+)?",
}
_FIFTEEN_DIGITS = Context(prec=15, rounding=ROUND_HALF_UP)  # the digits Excel shows and saves
_FIFTEEN_DIGITS_DOWN = Context(prec=15, rounding=ROUND_DOWN)


def _migrate_import(params: dict, from_version: int) -> dict:
    """Version 1 read one file; version 2 can also read all files of a folder; version 3 can
    go on without files in the folder."""
    if from_version < 2:
        params.setdefault("source", "file")
    if from_version < 3:
        params.setdefault("no_files", "error")
    return params


@action(
    "csv.import",
    "Import CSV / text files",
    category="Import",
    kind="source",
    params=[
        *source_params("Text files (*.csv *.txt *.tsv);;All files (*.*)", "*.csv;*.txt"),
        P.Choice("delimiter", "Delimiter", [("auto", "Detect automatically"), *_DELIMITERS]),
        P.Choice(
            "encoding",
            "Encoding",
            [
                ("auto", "Detect automatically"),
                ("utf-8", "UTF-8"),
                ("cp1252", "Windows (ANSI)"),
                ("latin-1", "Latin-1 (ISO-8859-1)"),
                ("utf-16", "UTF-16"),
            ],
        ),
        P.Bool("header", "First row has the column names", default=True),
        P.Int("skip_lines", "Skip lines at the top", min=0),
        P.Bool("detect_types", "Detect numbers and dates", default=True, help="Off: every column stays text."),
        P.Choice(
            "decimal",
            "Decimal separator",
            [("auto", "Detect automatically"), (",", "Comma: 1.234,56"), (".", "Point: 1,234.56")],
            visible_if={"detect_types": True},
        ),
    ],
    version=3,
    migrate=_migrate_import,
    summary=lambda p: f"Import {source_summary(p)}",
    fingerprint=sources_fingerprint,
    endpoints=source_endpoints,
)
def import_csv(df, p, ctx):
    """Reads a delimited text file (CSV, TXT, TSV), or all such files of a folder (and its
    subfolders) into one table: daily exports, for example.

    Delimiter, encoding and decimal separator are detected automatically unless chosen (for
    each file); a file whose first line has no delimiter has one column. Numbers and dates are
    recognized when every value of a column fits, in all files together: also amounts like
    1.234,50 € or 123,45- and percentages like 12,5% (read as 0.125). Columns with leading
    zeros (customer numbers, postal codes) stay text, and so do digits with spaces between them
    (phone numbers, card numbers): spaces between thousands count only in amounts and
    percentages (1 234,50 €). A value like 1.000 can be one thousand or one: it follows the
    other numbers of its column (like 0.500 or 2,5), else those of the file, and without any it
    is one thousand in files with semicolons. Files are matched by column names; a column
    missing in a file stays empty there.
    """
    paths, folder = source_files(p, ctx)
    parts, separators, encodings = [], [], []
    for path in paths:
        ctx.check_cancelled()
        frame, separator, encoding = _read_text_file(path, p, ctx.warn)
        parts.append((path, None, frame))
        separators.append(separator)
        encodings.append(encoding)
    decimals = []

    def detect_types(data: pl.DataFrame) -> pl.DataFrame:
        if not p.detect_types:
            return data
        decimal, columns = (p.decimal, {}) if p.decimal != "auto" else _guess_decimal(data, separators[0])
        decimals.append((decimal, [name for name, own in columns.items() if own != decimal]))
        return _detect_types(data, {name: columns.get(name, decimal) for name in data.columns}, ctx.warn)

    out = combine(parts, p, folder, prepare=detect_types)
    decimal = [_describe_decimals(*decimals[0])] if decimals else []
    if len(paths) == 1:
        ctx.log(", ".join([f"Encoding {encodings[0]}", f"delimiter {_describe_separator(separators[0])}", *decimal]))
    elif paths:
        kinds = ", ".join(sorted({_describe_separator(s) for s in separators}))
        ctx.log("; ".join([f"{len(paths)} files", f"encodings {', '.join(sorted(set(encodings)))}", f"delimiters {kinds}", *decimal]))
    return out


def _read_text_file(path: Path, p, warn: Callable[[str], None]) -> tuple[pl.DataFrame, str, str]:
    """One file as text columns named after its header: (data, delimiter, encoding)."""
    try:
        raw = path.read_bytes()
    except OSError as error:  # e.g. open in a program that doesn't let others read it
        raise files.read_error(error, path) from None
    data, encoding = _to_utf8(raw, p.encoding, path.name, warn)
    if b"\r\r\n" in data:  # line ends converted twice: one line end, not a \r at the end of the last value
        data = re.sub(rb"\r+\n", b"\r\n", data)
    sample = data[:65536].decode("utf-8", errors="ignore")
    separator = _detect_delimiter(sample, p.skip_lines) if p.delimiter == "auto" else _separator(p.delimiter)
    options = dict(separator=separator, has_header=False, skip_lines=p.skip_lines, raise_if_empty=False, encoding="utf8-lossy")
    try:
        try:
            out = pl.read_csv(io.BytesIO(data), infer_schema=False, **options)
        except pl.exceptions.ComputeError:  # rows longer than the first one ("found more fields")
            out, cut = _read_long_rows(data, options)
            if cut:
                rows = "1 row has" if cut == 1 else f"{cut:,} rows have" if cut > 1 else "some rows have"
                warn(f"In {path.name}, {rows} more values than the first row: the extra values were left out")
    except pl.exceptions.PolarsError as e:
        raise ActionError(f"Could not read {path.name}: {str(e).splitlines()[0]}") from None

    names: list[str] = []
    if p.header and out.height:
        names = ["" if v is None else str(v).strip() for v in out.row(0)]
        out = out.slice(1)
    names += [""] * (out.width - len(names))
    names = unique_names([name or f"Column {i}" for i, name in enumerate(names, 1)])
    return out.rename(dict(zip(out.columns, names))), separator, encoding


def _read_long_rows(data: bytes, options: dict) -> tuple[pl.DataFrame, int]:
    """A file with rows longer than its first row: the data cut to the first row's columns, and
    the number of rows that lost values (-1: unknown). Empty values at the end don't count."""
    width = pl.read_csv(io.BytesIO(data), infer_schema=False, n_rows=1, truncate_ragged_lines=True, **options).width
    schema = {f"column_{i}": pl.String for i in range(width + 1)}
    try:
        wide = pl.read_csv(io.BytesIO(data), schema=schema, missing_columns="insert", truncate_ragged_lines=True, **options)
    except (pl.exceptions.PolarsError, TypeError):  # a Polars without these options
        return pl.read_csv(io.BytesIO(data), infer_schema=False, truncate_ragged_lines=True, **options), -1
    return wide.drop(wide.columns[-1]), wide[:, -1].is_not_null().sum()


def _export_endpoints(p) -> list[Endpoint]:
    return [file_endpoint(p.file, endpoints.OUT, setting="file")] if p.file.strip() else []


def _migrate_export(params: dict, from_version: int) -> dict:
    """Version 1 always wrote the file, also for an empty table."""
    if from_version < 2:
        params.setdefault("skip_empty", False)
    return params


@action(
    "csv.export",
    "Export to CSV / text file",
    category="Export",
    kind="export",
    params=[
        P.File("file", "File", mode="save", filter="CSV files (*.csv);;Text files (*.txt);;All files (*.*)"),
        P.Bool(
            "skip_empty", "Don't export an empty table", default=False, help="When the table has no rows, the file is left as it is."
        ),
        P.Choice("delimiter", "Delimiter", _DELIMITERS),
        P.Choice(
            "encoding",
            "Encoding",
            [("utf-8-sig", "UTF-8 with BOM (Excel opens it correctly)"), ("utf-8", "UTF-8"), ("cp1252", "Windows (ANSI)")],
        ),
        P.Choice("decimal", "Decimal separator", [(",", "Comma: 1234,56"), (".", "Point: 1234.56")]),
        P.Text("date_format", "Date format", default="dd.mm.yyyy"),
        P.Text("datetime_format", "Date-time format", default="dd.mm.yyyy hh:mm:ss"),
        P.Bool("header", "Write column names", default=True),
        P.Bool("quote_all", "Put every value in quotes", default=False),
    ],
    version=2,
    migrate=_migrate_export,
    summary=lambda p: f"Export to {Path(p.file).name}",
    endpoints=_export_endpoints,
)
def export_csv(df, p, ctx):
    """Writes the table to a delimited text file. An existing file is replaced when the new
    one is complete: if the export fails, the old file stays as it was.

    Decimal numbers are written with at most 15 significant digits, as Excel shows and saves
    them: 0.37 * 3 is 1,11 (not 1,1099999999999999). Whole numbers and exact decimals keep all
    their digits. Binary values are written as hex text (0x0102FF); infinite and not-a-number
    values as empty, as in Excel. A table without columns gives an empty file.
    """
    path = ctx.resolve_path(p.file)
    separator = _separator(p.delimiter)
    if separator == "," and p.decimal == ",":
        raise ActionError("The delimiter and the decimal separator can't both be a comma")
    if p.skip_empty and df.height == 0:
        ctx.log("Not exported: the table is empty")
        return
    options = dict(
        separator=separator,
        include_header=p.header,
        line_terminator="\r\n",
        decimal_comma=p.decimal == ",",
        date_format=dtypes.to_strftime(p.date_format) or None,
        datetime_format=dtypes.to_strftime(p.datetime_format) or None,
        time_format="%H:%M:%S",
        quote_style="always" if p.quote_all else "necessary",
        float_scientific=False,
    )
    data = df.with_columns(
        *(_fifteen_digits(df[c]) for c, t in df.schema.items() if t.is_float()),
        *((pl.lit("0x") + pl.col(c).bin.encode("hex").str.to_uppercase()).alias(c) for c, t in df.schema.items() if t == pl.Binary),
    )
    if df.width == 0:  # rows without columns (Polars can't write them): an empty file, as for no rows
        data = pl.DataFrame()
    with files.replace_file(path, ctx.warn) as temporary:
        if p.encoding in ("utf-8", "utf-8-sig"):
            data.write_csv(temporary, include_bom=p.encoding == "utf-8-sig", **options)
        else:
            text = data.write_csv(None, **options)
            try:
                encoded = text.encode(p.encoding)
            except UnicodeEncodeError:
                encoded = text.encode(p.encoding, errors="replace")
                ctx.warn(f"Some characters don't exist in {p.encoding} and were written as '?'")
            temporary.write_bytes(encoded)
    if df.width == 0:
        ctx.log(f"Wrote an empty file to {path}: the table has no columns")
    else:
        ctx.log(f"Wrote {df.height:,} row{'s' if df.height != 1 else ''} to {path}")


def _fifteen_digits(s: pl.Series) -> pl.Series:
    """Decimal numbers for the export, rounded to 15 significant digits (half away from zero), as
    Excel shows and saves them: 0.37 * 3 is written as 1.11, not 1.1099999999999999, since Polars
    writes the shortest text that reads back as the number. That holds for whole decimal numbers
    too (0.37 * 3 * 1e16 is 11100000000000000, not 11099999999999998); whole numbers (Int) and
    exact decimals keep all their digits. A Float32 value keeps the digits it shows (0.1, not
    0.10000000149011612); infinite and not-a-number values become empty."""
    if s.dtype != pl.Float64:
        s = s.cast(pl.String).cast(pl.Float64)
    s = pl.select(pl.when(pl.lit(s).is_finite()).then(pl.lit(s)).alias(s.name)).to_series()
    long = s.cast(pl.String).str.len_bytes() > 16  # shorter texts have at most 15 digits: they stay as they are
    rows = long.fill_null(False).arg_true()
    if rows.len():
        s = s.scatter(rows, [_fifteen(v) for v in s.gather(rows).to_list()])
    return s


def _fifteen(value: float) -> float:
    rounded = float(_FIFTEEN_DIGITS.create_decimal_from_float(value))
    if math.isinf(rounded):  # rounded up beyond the largest decimal number: round towards zero
        rounded = float(_FIFTEEN_DIGITS_DOWN.create_decimal_from_float(value))
    return rounded


def _to_utf8(raw: bytes, encoding: str, name: str, warn: Callable[[str], None]) -> tuple[bytes, str]:
    """The file content as UTF-8 bytes, and the encoding it was read with (a byte order mark
    decides it). Characters that don't fit the encoding are shown as �, with a warning."""
    if raw.startswith(codecs.BOM_UTF8):
        raw = raw[len(codecs.BOM_UTF8) :]
        if not _is_utf8(raw):
            warn(f"{name}: {_are(_utf8_counts(raw)[1], 'character')} not UTF-8, shown as �")
        return raw, "utf-8-sig"
    if raw.startswith((codecs.BOM_UTF16_LE, codecs.BOM_UTF16_BE)):
        return _decoded(raw, "utf-16", name, warn), "utf-16"
    if encoding == "auto":
        if _is_utf8(raw):
            return raw, "utf-8"
        valid, invalid = _utf8_counts(raw)
        if valid > invalid:  # mostly UTF-8: a few wrong bytes don't make it a Windows file
            warn(f"{name}: {_are(invalid, 'character')} not UTF-8, shown as �")
            return raw, "utf-8"
        if valid:
            warn(f"{name} was read as Windows (ANSI), but it has UTF-8 characters too: they look wrong, e.g. Ã¼ for ü")
        return _decoded(raw, "cp1252", name, warn), "cp1252"
    if encoding == "utf-8":
        if not _is_utf8(raw):
            warn(
                f"{name} is not UTF-8: {_are(_utf8_counts(raw)[1], 'character')} shown as �. "
                "Choose another encoding (e.g. Windows (ANSI)) or Detect automatically"
            )
        return raw, encoding
    if encoding in ("cp1252", "latin-1") and not raw.isascii() and _is_utf8(raw):
        warn(
            f"{name} looks like UTF-8, not {_ENCODINGS[encoding]}: characters like ü look wrong (Ã¼). "
            "Choose UTF-8 or Detect automatically"
        )
    return _decoded(raw, encoding, name, warn), encoding


def _decoded(raw: bytes, codec: str, name: str, warn: Callable[[str], None]) -> bytes:
    text = raw.decode(codec, errors="replace")
    if replaced := text.count("�"):
        warn(f"{name}: {_are(replaced, 'character')} not {_ENCODINGS.get(codec, codec)}, shown as �")
    return text.encode("utf-8")


def _are(count: int, word: str) -> str:
    return f"1 {word} is" if count == 1 else f"{count:,} {word}s are"


def _is_utf8(raw: bytes) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    view = memoryview(raw)
    try:
        for start in range(0, len(raw), _CHUNK):
            decoder.decode(view[start : start + _CHUNK])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _utf8_counts(raw: bytes) -> tuple[int, int]:
    """Read as UTF-8: about how many characters are UTF-8 beyond ASCII (the bytes they take
    beyond one each: ü takes 2, € 3), and how many characters aren't UTF-8."""
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
    view = memoryview(raw)
    characters = replaced = 0
    for start in range(0, len(raw) + 1, _CHUNK):
        text = decoder.decode(view[start : start + _CHUNK], final=start + _CHUNK > len(raw))
        characters += len(text)
        replaced += text.count("�")
    return len(raw) - characters, replaced - raw.count("�".encode())  # a � in the file is a character like any other


def _detect_delimiter(sample: str, skip_lines: int) -> str:
    """The delimiter of the first line that splits most lines the same way. Without any in the
    first line, the file has one column (its values can still have commas, as in 12,50)."""
    lines = [line for line in sample.splitlines()[skip_lines : skip_lines + 50] if line.strip()][:20]
    candidates = (";", ",", "\t", "|")
    if lines and not any(_count_outside_quotes(lines[0], candidate) for candidate in candidates):
        return _ONE_COLUMN
    best, best_score = ",", -1.0
    for candidate in candidates:
        counts = [_count_outside_quotes(line, candidate) for line in lines]
        if not counts or counts[0] == 0:
            continue
        consistency = sum(c == counts[0] for c in counts) / len(counts)
        score = consistency * 1000 + counts[0]
        if score > best_score:
            best, best_score = candidate, score
    return best


def _count_outside_quotes(line: str, char: str) -> int:
    count, quoted = 0, False
    for ch in line:
        if ch == '"':
            quoted = not quoted
        elif ch == char and not quoted:
            count += 1
    return count


def _guess_decimal(df: pl.DataFrame, separator: str) -> tuple[str, dict[str, str]]:
    """The decimal separator of the file, and of the columns whose values show one. A value shows
    a decimal comma (1.234,56 / 12,5 / 0,500 / 1.234.567) or a decimal point (1,234.56 / 12.5 /
    0.500) when it fits only that one; values like 1.000 and 1,000 fit both. A column with a
    value that shows a point has a decimal point, else one with a value that shows a comma a
    decimal comma. For the file, the separator that more values of a sample show; without a
    difference, a comma in files with semicolons, else a point."""
    comma = point = 0
    columns: dict[str, str] = {}
    sample = df.head(2000)
    for name, dtype in df.schema.items():
        if dtype != pl.String:
            continue
        commas, points = _shown_decimals(sample[name])
        comma, point = comma + commas, point + points
        if not commas and not points and df.height > sample.height and sample[name].str.contains(_EITHER).all():
            rest = df[name].slice(sample.height)  # numbers like 12 or 1.250 so far: also 0.500 after them?
            commas, points = _shown_decimals(rest.filter(~rest.str.contains(_EITHER)))
        if commas or points:
            columns[name] = "." if points else ","
    if comma != point:
        return ("," if comma > point else "."), columns
    return ("," if separator == ";" else "."), columns


def _shown_decimals(s: pl.Series) -> tuple[int, int]:
    """How many values are numbers only with a decimal comma, and how many only with a decimal
    point (see ``_guess_decimal``)."""
    values = _bare(s)
    values = values.filter(~values.str.contains(r"^[+-]?[0-9]+$"))
    fits_comma, fits_point = values.str.contains(f"^[+-]?{_NUMBER[',']}$"), values.str.contains(f"^[+-]?{_NUMBER['.']}$")
    return (fits_comma & ~fits_point).sum(), (fits_point & ~fits_comma).sum()


def _detect_types(data: pl.DataFrame, decimals: dict[str, str], warn: Callable[[str], None]) -> pl.DataFrame:
    """``dtypes.infer_types`` with the decimal separator of each column, and also: numbers with
    thousands separators only (1.000) become whole numbers (exact, up to 19 digits); amounts
    (1.234,50 €, 1 234,50 €, 123,45-) and percentages (12,5% is 0.125) become numbers when every
    value of the column is one. Digits with spaces between them are numbers only in amounts and
    percentages, as thousands (see ``_SPACED``): 1 234 and +49 30 1234567 stay text."""
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
        sample = _clean(data[name].drop_nulls().head(1000)).drop_nulls()  # most columns end here, quickly
        if kind == pl.Float64 or _fit(sample, whole):  # text: numbers too long for a decimal number
            pattern = whole
        else:
            pattern = percent if _fit(sample, percent) else amount
        if not _fit(sample, pattern) or (pattern == amount and not _amounts(sample)):
            continue
        text = _clean(data[name])
        values = text.drop_nulls()
        if not values.str.contains(pattern).all():
            continue
        if pattern == whole:
            if kind == pl.String and values.str.contains(r"^[+-]?0[0-9]").any():
                continue  # codes like 0815
            integers = dtypes.parse_integers(text, decimal)
            if integers.null_count() == text.null_count():  # else beyond Int64: the text stays
                updates.append(integers)
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
                updates.append(parsed)
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


def _separator(choice: str) -> str:
    return "\t" if choice == "tab" else choice


def _describe_separator(separator: str) -> str:
    names = {"\t": "tab", ";": "semicolon", ",": "comma", "|": "pipe", _ONE_COLUMN: "none (one column)"}
    return names.get(separator, repr(separator))


def _describe_decimals(decimal: str, others: list[str]) -> str:
    """For the log: the decimal separator of the files, and the columns that have the other one."""
    text = f"decimal {'comma' if decimal == ',' else 'point'}"
    if not others:
        return text
    named = ", ".join(others[:5]) + (f" and {len(others) - 5} more" if len(others) > 5 else "")
    return f"{text} (decimal {'point' if decimal == ',' else 'comma'} in {named})"

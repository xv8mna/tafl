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
    col,
    combine,
    detection,
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
_SAMPLE = 65536  # bytes looked at for the delimiter
_ONE_COLUMN = files.ONE_COLUMN
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

    Delimiter, encoding and decimal separator are detected automatically unless chosen; a file
    whose first line has no delimiter has one column. Numbers and dates are recognized when
    every value of a column fits: also amounts like 1.234,50 €, -$3.00 or 123,45- and
    percentages like 12,5% (read as 0.125). Numbers keep all their digits: whole numbers of up
    to 19 digits are integers, longer ones and decimals that a decimal number would round
    (12345678901234,5678) exact decimals (up to 38 digits; longer ones stay text, with a
    warning). Columns with leading zeros
    (customer numbers, postal codes, 00,5) stay text, and so do digits with spaces between them
    (phone numbers, card numbers): spaces between thousands count only in amounts and
    percentages (1 234,50 €). A value like 1.000 can be one thousand or one: it follows the
    other numbers of its column (like 0.500 or 2,5, also in the other files of a folder), else
    those of its file, and without any it is one thousand in files with semicolons.

    In a folder, each file is read with its own delimiter, encoding, decimal separator and date
    format. Files are matched by column names; a column missing in a file stays empty there. A
    column with other types in other files gets a type that holds every value, as in Append,
    or becomes text with a warning; one that is text in a file stays text, as the files have it.
    """
    paths, folder = source_files(p, ctx)
    parts, separators, encodings = [], [], []
    for path in paths:
        ctx.check_cancelled()
        frame, separator, encoding = _read_text_file(path, p, ctx.warn)
        parts.append((path, None, frame))
        separators.append(separator)
        encodings.append(encoding)
    texts = [frame for _, _, frame in parts]
    overall, guesses = p.decimal, [(p.decimal, {})] * len(texts)
    if p.detect_types and p.decimal == "auto":  # without a difference, a decimal comma in files with semicolons, else a point
        overall, guesses = detection.guess_decimals(texts, ["," if s == ";" else "." for s in separators])
    said: set[str] = set()

    def warn_once(message: str) -> None:  # a warning about a column of several files once, with the first example
        if (key := message.split(", e.g.")[0]) not in said:
            said.add(key)
            ctx.warn(message)

    typed = detection.detect_files(texts, guesses, warn_once, ctx.check_cancelled) if p.detect_types else texts
    out = combine(parts, p, folder, warn=ctx.warn, prepare_each=(lambda i, _text: typed[i]) if p.detect_types else None)
    used: list[dict[str, str]] = []  # of each file: its numbers' columns whose values show a decimal separator, with it
    for frame, (_decimal, own) in zip(typed, guesses, strict=True) if p.detect_types else ():
        schema = frame.schema  # once: each call builds the types of all columns again
        used.append({name: own[name] for name in own if schema[name].is_numeric()})  # the others stayed text
    decimal = [_describe_decimals(overall, guesses, used, paths)] if used else []
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
    del raw  # after a byte order mark, ``data`` is a copy: the file would be in memory twice
    if b"\n" not in data and b"\r" in data:  # the line ends of old Macs: Polars would read it all as one line
        data = data.replace(b"\r", b"\n")
    if _converted_twice(data, p.skip_lines):  # one line end, not a \r at the end of the last value
        data = re.sub(rb"(?<!\r)\r+\n", b"\r\n", data)  # only from the start of a run: from each \r would take long
    sample = data[:_SAMPLE].decode("utf-8", errors="ignore")
    separator = files.detect_delimiter(sample, p.skip_lines) if p.delimiter == "auto" else _separator(p.delimiter)
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
        reason = _stray_quote(data, separator, p.skip_lines) or str(e).splitlines()[0]
        raise ActionError(f"Could not read {path.name}: {reason}") from None

    names: list[str] = []
    if p.header and out.height:
        names = ["" if v is None else str(v).strip() for v in out.row(0)]
        out = out.slice(1)
    names += [""] * (out.width - len(names))
    names = unique_names([name or f"Column {i}" for i, name in enumerate(names, 1)])
    return out.rename(dict(zip(out.columns, names))), separator, encoding


def _converted_twice(data: bytes, skip_lines: int) -> bool:
    """Whether line ends of the file were converted twice: a \\r\\r\\n outside quotes, anywhere in
    the file after the lines skipped (a header written on its own can have another line end than
    the rows). In a file with other line ends, such a line end in a quoted value is part of the
    value."""
    start = quotes = 0
    for _ in range(skip_lines):  # Polars skips them also in quotes: a quote there starts no value
        start = data.find(b"\n", start) + 1
        if not start:
            return False
    end = data.find(b"\r\r\n", start)
    while end >= 0:
        quotes += data.count(b'"', start, end)
        if quotes % 2 == 0:
            return True
        start, end = end, data.find(b"\r\r\n", end + 3)
    return False


def _stray_quote(data: bytes, separator: str, skip_lines: int) -> str | None:
    """Why Polars can't read the file, when a quote is out of place: in a value that doesn't start
    with one (24";Monitor: an odd number of them; an even number Polars reads as it is), after the
    closing quote of a value ("24"x), or one that is never closed ("Mouse;5 to the end of the
    file). The line and its start; None for other faults."""
    text = data.decode("utf-8", errors="replace")
    start = 0
    for _ in range(skip_lines):  # Polars skips them as they are, also in quotes
        start = text.find("\n", start) + 1
        if not start:
            return None
    s = re.escape(separator)
    # a lone \r is part of the value, as Polars reads it; "unrolled": one character class per step is
    # 2.5 times as fast as an alternation between it and the \r
    plain = rf'[^"{s}\r\n]*+(?:\r(?!\n)[^"{s}\r\n]*+)*+'
    field = rf'(?>"(?:[^"]|"")*+"|{plain}(?:"{plain}"{plain})*+)'  # quoted, or plain with pairs of quotes in it
    end = re.compile(rf"(?:{field}(?:{s}|\r?\n|\Z))*+").match(text, start).end()  # possessive: linear, little memory
    if end >= len(text):
        return None
    line = text.count("\n", 0, end) + 1
    shown = text[text.rfind("\n", 0, end) + 1 :].split("\n", 1)[0].rstrip("\r")
    shown = shown if len(shown) <= 60 else shown[:60] + "…"
    if text[end] == '"' and not _QUOTED.match(text, end):  # Polars stops at it, with the rest of the file in its text
        return (
            f"line {line} has a value that starts with a quote that is never closed: '{shown}'. In a CSV file, a value "
            'in quotes ends with a quote, and a quote in it is doubled ("24"" Monitor"). Correct the file, or export it '
            "again in that form."
        )
    if text[end] == '"':
        return (
            f"line {line} has a value that goes on after its closing quote: '{shown}'. In a CSV file, a quote in a "
            'quoted value is doubled ("24"" Monitor"). Correct the file, or export it again in that form.'
        )
    return (
        f"line {line} has a quote in a value that doesn't start with one: '{shown}'. In a CSV file, such a value is "
        'in quotes and its quotes are doubled ("24""" for 24"). Correct the file, or export it again in that form.'
    )


_QUOTED = re.compile(r'"(?:[^"]|"")*+"')  # a value in quotes, with its quotes doubled


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
    their digits. Times keep their fractions of a second (08:30:00.500). Binary values are
    written as hex text (0x0102FF), time spans as text that is the same on every PC (1d 2h 3m,
    as Pivot and Split column write them); infinite and not-a-number values as empty, as in
    Excel. A table without columns gives an empty file. A CSV file can't hold lists or records:
    such columns stop the export.
    """
    path = ctx.resolve_path(p.file)
    separator = _separator(p.delimiter)
    if separator == "," and p.decimal == ",":
        raise ActionError("The delimiter and the decimal separator can't both be a comma")
    if p.skip_empty and df.height == 0:
        ctx.log("Not exported: the table is empty")
        return
    schema = df.schema
    _check_flat(schema)
    options = dict(
        separator=separator,
        include_header=p.header,
        line_terminator="\r\n",
        decimal_comma=p.decimal == ",",
        date_format=dtypes.to_strftime(p.date_format) or None,
        datetime_format=dtypes.to_strftime(p.datetime_format) or None,
        time_format="%H:%M:%S%.f",  # fractions of a second only when there are some (08:30:00.500), as Pivot writes them
        quote_style="always" if p.quote_all else "necessary",
        float_scientific=False,
    )
    data = df.with_columns(
        *(_fifteen_digits(df[c]) for c, t in schema.items() if t.is_float()),
        *((pl.lit("0x") + col(c).bin.encode("hex").str.to_uppercase()).alias(c) for c, t in schema.items() if t == pl.Binary),
        *(dtypes.text_expr(col(c), t).alias(c) for c, t in schema.items() if t == pl.Duration),
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


def _check_flat(schema: pl.Schema) -> None:
    """A CSV file holds one text per value: lists, records and values of a plugin's own type stop
    the export, with the columns that have them (Polars' message names none)."""
    kinds = {name: _kind(dtype) for name, dtype in schema.items() if dtype.is_nested() or dtype.is_object()}
    if not kinds:
        return
    names, more = list(kinds), len(kinds) - 5
    shown = names[:5] + ([f"{more:,} more column{'s' if more > 1 else ''}"] if more > 0 else [])
    raise ActionError(
        f"A CSV file can't hold {_listed([k for k in _KINDS if k in kinds.values()], 'or')}: {_listed(shown, 'and')} "
        f"{'has' if len(names) == 1 else 'have'} them. Remove {'this column' if len(names) == 1 else 'these columns'} "
        "before this step (Keep, remove or reorder columns)."
    )


_KINDS = ("lists", "records", "values of a plugin's own type")


def _kind(dtype: pl.DataType) -> str:
    if dtype.is_object():
        return _KINDS[2]
    return _KINDS[1] if isinstance(dtype, pl.Struct) else _KINDS[0]


def _listed(words: list[str], last: str) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + f" {last} " + words[-1]


def _fifteen_digits(s: pl.Series) -> pl.Series:
    """Decimal numbers for the export, rounded to 15 significant digits (half away from zero), as
    Excel shows and saves them: 0.37 * 3 is written as 1.11, not 1.1099999999999999, since Polars
    writes the shortest text that reads back as the number. That holds for whole decimal numbers
    too (0.37 * 3 * 1e16 is 11100000000000000, not 11099999999999998); whole numbers (Int) and
    exact decimals keep all their digits. A Float32 value keeps the digits it shows (0.1, not
    0.10000000149011612); infinite and not-a-number values become empty."""
    name = s.name  # the work has a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    if s.dtype != pl.Float64:
        s = s.cast(pl.String).cast(pl.Float64)
    s = pl.select(pl.when(pl.lit(s).is_finite()).then(pl.lit(s)).alias("value")).to_series()
    long = s.cast(pl.String).str.len_bytes() > 16  # shorter texts have at most 15 digits: they stay as they are
    rows = long.fill_null(False).arg_true()
    if rows.len():
        s = s.scatter(rows, [_fifteen(v) for v in s.gather(rows).to_list()])
    return s.alias(name)


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


def _separator(choice: str) -> str:
    return "\t" if choice == "tab" else choice


def _describe_separator(separator: str) -> str:
    names = {"\t": "tab", ";": "semicolon", ",": "comma", "|": "pipe", _ONE_COLUMN: "none (one column)"}
    return names.get(separator, repr(separator))


def _describe_decimals(overall: str, guesses: list[tuple[str, dict[str, str]]], used: list[dict[str, str]], paths: list[Path]) -> str:
    """For the log: the decimal separator of the files, and the columns that have the other one.
    When files have another one in the same column, the separator of each file instead."""
    by_column: dict[str, set[str]] = {}
    for columns in used:
        for name, decimal in columns.items():
            by_column.setdefault(name, set()).add(decimal)
    if len(used) == 1:
        overall = guesses[0][0]
    elif any(len(decimals) > 1 for decimals in by_column.values()):
        files: dict[str, list[str]] = {}
        for path, (decimal, _own) in zip(paths, guesses, strict=True):
            files.setdefault(decimal, []).append(path.name)
        return ", ".join(
            f"decimal {_DECIMAL_WORDS[decimal]} in {names[0] if len(names) == 1 else f'{len(names):,} files'}"
            for decimal, names in sorted(files.items())
        )
    others = [name for name, decimals in by_column.items() if decimals != {overall}]
    text = f"decimal {_DECIMAL_WORDS[overall]}"
    if not others:
        return text
    named = ", ".join(others[:5]) + (f" and {len(others) - 5} more" if len(others) > 5 else "")
    return f"{text} (decimal {_DECIMAL_WORDS['.' if overall == ',' else ',']} in {named})"


_DECIMAL_WORDS = {",": "comma", ".": "point"}

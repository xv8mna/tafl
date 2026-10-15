"""CSV and text files: import and export."""

from __future__ import annotations

import codecs
import io
from pathlib import Path

import polars as pl

from etl_tool.api import (
    ActionError,
    P,
    action,
    combine,
    dtypes,
    source_files,
    source_params,
    source_summary,
    sources_fingerprint,
    unique_names,
)

_DELIMITERS = [(";", "Semicolon ;"), (",", "Comma ,"), ("tab", "Tab"), ("|", "Pipe |")]
_BOMS = ((codecs.BOM_UTF8, "utf-8-sig"), (codecs.BOM_UTF16_LE, "utf-16"), (codecs.BOM_UTF16_BE, "utf-16"))


def _migrate_import(params: dict, from_version: int) -> dict:
    """Version 1 read one file; version 2 can also read all files of a folder."""
    if from_version < 2:
        params.setdefault("source", "file")
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
    version=2,
    migrate=_migrate_import,
    summary=lambda p: f"Import {source_summary(p)}",
    fingerprint=sources_fingerprint,
)
def import_csv(df, p, ctx):
    """Reads a delimited text file (CSV, TXT, TSV), or all such files of a folder (and its
    subfolders) into one table: daily exports, for example.

    Delimiter, encoding and decimal separator are detected automatically unless chosen (for
    each file). Numbers and dates are recognized when every value of a column fits, in all
    files together; columns with leading zeros (customer numbers, postal codes) stay text.
    Files are matched by column names; a column missing in a file stays empty there.
    """
    files, folder = source_files(p, ctx)
    parts, separators, encodings = [], [], []
    for path in files:
        ctx.check_cancelled()
        frame, separator, encoding = _read_text_file(path, p)
        parts.append((path, None, frame))
        separators.append(separator)
        encodings.append(encoding)

    def detect_types(data: pl.DataFrame) -> pl.DataFrame:
        if not p.detect_types:
            return data
        decimal = p.decimal if p.decimal != "auto" else _guess_decimal(data, separators[0])
        return dtypes.infer_types(data, decimal)

    out = combine(parts, p, folder, prepare=detect_types)
    if len(files) == 1:
        ctx.log(f"Encoding {encodings[0]}, delimiter {_describe_separator(separators[0])}")
    else:
        kinds = ", ".join(sorted({_describe_separator(s) for s in separators}))
        ctx.log(f"{len(files)} files; encodings {', '.join(sorted(set(encodings)))}; delimiters {kinds}")
    return out


def _read_text_file(path: Path, p) -> tuple[pl.DataFrame, str, str]:
    """One file as text columns named after its header: (data, delimiter, encoding)."""
    raw = path.read_bytes()
    data, encoding = _to_utf8(raw, p.encoding)
    sample = data[:65536].decode("utf-8", errors="ignore")
    separator = _detect_delimiter(sample, p.skip_lines) if p.delimiter == "auto" else _separator(p.delimiter)
    try:
        out = pl.read_csv(
            io.BytesIO(data),
            separator=separator,
            has_header=False,
            infer_schema=False,
            skip_lines=p.skip_lines,
            truncate_ragged_lines=True,
            raise_if_empty=False,
            encoding="utf8-lossy",
        )
    except pl.exceptions.ComputeError as e:
        raise ActionError(f"Could not read {path.name}: {str(e).splitlines()[0]}") from None

    names: list[str] = []
    if p.header and out.height:
        names = ["" if v is None else str(v).strip() for v in out.row(0)]
        out = out.slice(1)
    names += [""] * (out.width - len(names))
    names = unique_names([name or f"Column {i}" for i, name in enumerate(names, 1)])
    return out.rename(dict(zip(out.columns, names))), separator, encoding


@action(
    "csv.export",
    "Export to CSV / text file",
    category="Export",
    kind="export",
    params=[
        P.File("file", "File", mode="save", filter="CSV files (*.csv);;Text files (*.txt);;All files (*.*)"),
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
    summary=lambda p: f"Export to {Path(p.file).name}",
)
def export_csv(df, p, ctx):
    """Writes the table to a delimited text file. Existing files are replaced."""
    path = ctx.resolve_path(p.file)
    separator = _separator(p.delimiter)
    if separator == "," and p.decimal == ",":
        raise ActionError("The delimiter and the decimal separator can't both be a comma")
    options = dict(
        separator=separator,
        include_header=p.header,
        line_terminator="\r\n",
        decimal_comma=p.decimal == ",",
        date_format=dtypes.to_strftime(p.date_format) or None,
        datetime_format=dtypes.to_strftime(p.datetime_format) or None,
        quote_style="always" if p.quote_all else "necessary",
        float_scientific=False,
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.encoding in ("utf-8", "utf-8-sig"):
            df.write_csv(path, include_bom=p.encoding == "utf-8-sig", **options)
        else:
            text = df.write_csv(None, **options)
            try:
                encoded = text.encode(p.encoding)
            except UnicodeEncodeError:
                encoded = text.encode(p.encoding, errors="replace")
                ctx.warn(f"Some characters don't exist in {p.encoding} and were written as '?'")
            path.write_bytes(encoded)
    except PermissionError:
        raise ActionError(f"Can't write {path.name}. Is it open in another program?") from None
    ctx.log(f"Wrote {df.height:,} rows to {path}")


def _to_utf8(raw: bytes, encoding: str) -> tuple[bytes, str]:
    """The file content as UTF-8 bytes, and the encoding it was read with."""
    for bom, name in _BOMS:
        if raw.startswith(bom):
            if name == "utf-8-sig":
                return raw[len(bom) :], name
            return raw.decode("utf-16").encode("utf-8"), name
    if encoding == "auto":
        encoding = "utf-8" if _is_utf8(raw) else "cp1252"
    if encoding == "utf-8":
        return raw, encoding
    return raw.decode(encoding, errors="replace").encode("utf-8"), encoding


def _is_utf8(raw: bytes) -> bool:
    decoder = codecs.getincrementaldecoder("utf-8")()
    view = memoryview(raw)
    try:
        for start in range(0, len(raw), 1 << 20):
            decoder.decode(view[start : start + (1 << 20)])
        decoder.decode(b"", final=True)
    except UnicodeDecodeError:
        return False
    return True


def _detect_delimiter(sample: str, skip_lines: int) -> str:
    lines = [line for line in sample.splitlines()[skip_lines : skip_lines + 50] if line.strip()][:20]
    best, best_score = ",", -1.0
    for candidate in (";", ",", "\t", "|"):
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


def _guess_decimal(df: pl.DataFrame, separator: str) -> str:
    """Count values that look like 1.234,56 / 12,5 versus 1,234.56 / 12.5 in a sample."""
    comma = point = 0
    sample = df.head(2000)
    for name, dtype in sample.schema.items():
        if dtype == pl.String:
            values = sample[name].str.strip_chars()
            comma += values.str.contains(r"^[+-]?([0-9]+|[0-9]{1,3}(\.[0-9]{3})+),[0-9]+$").sum()
            point += values.str.contains(r"^[+-]?([0-9]+|[0-9]{1,3}(,[0-9]{3})+)\.[0-9]+$").sum()
    if comma != point:
        return "," if comma > point else "."
    return "," if separator == ";" else "."


def _separator(choice: str) -> str:
    return "\t" if choice == "tab" else choice


def _describe_separator(separator: str) -> str:
    return {"\t": "tab", ";": "semicolon", ",": "comma", "|": "pipe"}.get(separator, repr(separator))

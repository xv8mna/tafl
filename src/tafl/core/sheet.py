"""The data sheet: rows and named columns of plain text, to bring data that is in no file into a
project. The data is pasted or typed, and goes into a file: an import step reads it back exactly.

Every column is one immutable Polars text column. An edit builds new columns from slices of the
old ones, which share their data, so an edit of a big sheet costs almost nothing and an undo step
is only a list of the columns as they were. The grid reads a block of a column at a time.
"""

from __future__ import annotations

import io
import os
import re
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from itertools import islice
from pathlib import Path
from typing import Any, NamedTuple
from xml.sax.saxutils import escape

import polars as pl
import xlsxwriter

from tafl.api import Action, ActionError, Cancelled, Context, files, unique_names

BLOCK = 256  # rows of a column read at a time for the grid
MAX_CELLS = 40_000_000  # the peak memory of a paste grows with the text: this protects the PC
MAX_COLUMNS = 16_384  # as a sheet of an Excel workbook
UNDO_STEPS = 100
UNDO_CELLS = 40_000_000  # cells rewritten by the undo steps kept
MAX_CHUNKS = 2048  # a column with more parts is put together; a big paste has about 600 parts
XLSX_SLOW_CELLS = 2_000_000  # above this, writing .xlsx takes long enough to say so
XLSX_CELLS_PER_SECOND = 175_000
XLSX_READ_BYTES = 140  # of memory for a cell while .xlsx is read: measured 2.3 GB for 18,000,000 cells of 10 characters

_CACHED_BLOCKS = 512
_SAMPLE = 65536  # characters looked at to find the separator, as the CSV import does
_QUOTED = r'[^"]*(?:""[^"]*)*"'  # from the first character of a quoted cell to its closing quote
_QUOTED_CELL = re.compile(rf'(?:^|(?<=[;,|]))"{_QUOTED}(?=[;,|]|$)', re.MULTILINE)  # of a table without tabs
# One word that is a number with a comma in it, with marks around it: 1,50 and 1.234,50€ and (2,5) and 12,-
# and 1,234.50. A whole number with groups only with a mark at it, $1,234,567: 100,200,300 is three values.
_NUMBER_WITH_COMMA = re.compile(
    r"(?<![^\s;|])(?:"
    r"[^\w\s,;|]*(?:\d+(?:[.']\d{3})*,(?:\d+|-+)|\d{1,3}(?:,\d{3})+\.\d+)[^\d\s,;|]*"
    r"|[^\w\s,;|]+\d{1,3}(?:,\d{3})+[^\d\s,;|]*"
    r"|\d{1,3}(?:,\d{3})+[^\w\s,;|]+"
    r")(?![^\s;|])"
)
_NUMBER_OR_DATE = re.compile(r"[+-]?\d[\d.,:/' -]*%?")
_RULER_CHARACTERS = " \t|+:=_-"  # of a line that only draws a line
_NUMBERED_NAME = re.compile(r"Column \d+")  # no name of its own
_SPARE_SEPARATORS = files.ONE_COLUMN + "\x1e\x1d\x1c\x1b\x1a\x19\x18"
_PROTECTED_PARTS = 1 << 17  # pieces of text kept apart at most: a Python string costs more than its text
_NAME_SPACES = str.maketrans("\r\n\t", "   ")

_XLSX_ROWS, _XLSX_COLUMNS, _XLSX_TEXT = 1_048_575, 16_384, 32_767  # below the names; characters in a cell
_XLSX_RICH_TEXT = 1 << 20  # what xlsxwriter may get for a cell: the XML of the longest cell fits
_XLSX_PART = 50_000  # cells written between two looks at the Stop button
_XLSX_ESCAPE = re.compile(r"_(?=x[0-9a-fA-F]{4}[_\x00-\x08\x0b-\x1f])")  # a control character gets a _ of its own
_XLSX_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f]")
_XLSX_ROWS_SHARE = 0.9  # of the time: the rest is for packing the file, and a Stop waits for its end
_XLSX_CELL_BYTES, _XLSX_ROW_BYTES = 50, 40  # in the sheet's XML, without the text
_XLSX_BIG = 3 << 29  # bytes of XML, 1.5 GB: the zip format needs its 64-bit extension from 2 GB on
_PARQUET_ROWS = 100_000  # in one part of the file: the default takes a third more memory to write

Ranges = list[tuple[int, int, int, int]]  # (top, bottom, left, right), inclusive, data rows


class SheetError(ValueError):
    """A problem the user can fix: the message is shown as it is."""


@dataclass(frozen=True)
class SheetFormat:
    suffix: str
    label: str
    action: str  # the import action
    settings: dict[str, Any]  # the settings that differ from the action's defaults


def _text_format(suffix: str, label: str, delimiter: str) -> SheetFormat:
    settings = {"delimiter": delimiter, "encoding": "auto", "header": True, "skip_lines": 0, "detect_types": False}
    return SheetFormat(suffix, label, "csv.import", settings)


# In the order of the choice in the window; the first is the default. The delimiter of the text
# files is fixed, not "auto": a file of one column with 1,50 in its first line would split at the comma.
FORMATS: dict[str, SheetFormat] = {
    ".xlsx": SheetFormat(".xlsx", "Excel workbook (.xlsx)", "excel.import", {}),
    ".csv": _text_format(".csv", "CSV file (.csv)", ";"),
    ".txt": _text_format(".txt", "Text file with tabs (.txt)", "tab"),
    ".parquet": SheetFormat(".parquet", "Parquet file (.parquet)", "parquet.import", {}),
}


# --- text from the clipboard ----------------------------------------------------------------------


@dataclass(frozen=True)
class Pasted:
    frame: pl.DataFrame  # text columns; empty cells are null; rows and columns at the end trimmed
    separator: str  # "\t", ";", ",", "|", or "" for one column
    few_tabs: bool = False  # split at tabs that only some lines have: they may be no separators
    framed: bool = False  # a list in a frame of |: the frame and its ruler lines are left out


def parse_text(text: str, *, one_column: bool = False) -> Pasted:
    """The plain text of the clipboard as a table: separated by tabs as spreadsheet programs write
    it, otherwise by the separator that makes the biggest table. Every value stays text, exactly
    as pasted. ``one_column`` doesn't split the lines at all."""
    if not text:
        raise SheetError("The clipboard holds no text. Copy some data, then press Ctrl+V here.")
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n").rstrip("\n")
    if not text:
        raise SheetError("The clipboard holds only empty cells.")
    separator, plain, hidden, few_tabs, framed = _separated(text, one_column)
    used = separator or _spare_separator(plain)
    rows = plain.count("\n") + 1
    _check_clipboard(rows, plain.partition("\n")[0].count(used) + 1)  # before the copies below
    data = plain.encode("utf-8", "replace")
    del text, plain
    columns = _widest(data, used) + 1
    _check_clipboard(rows, columns)
    frame = pl.read_csv(
        io.BytesIO(data),
        separator=used,
        has_header=False,
        quote_char=None,
        schema={f"c{i}": pl.String for i in range(columns)},
        missing_columns="insert",
        truncate_ragged_lines=True,
        raise_if_empty=False,
    )
    del data
    if hidden is not None:
        cells = pl.all().str.replace_all(hidden[0], used, literal=True).str.replace_all(hidden[1], "\n", literal=True)
        frame = frame.with_columns(cells)
    used_rows = _used_rows(frame.get_columns())
    if not used_rows:
        raise SheetError("The clipboard holds only empty cells.")
    last = next(i for i in range(frame.width - 1, -1, -1) if frame.to_series(i).null_count() < frame.height)
    table = frame.slice(0, used_rows).select(frame.columns[: last + 1])
    return Pasted(table, separator if columns > 1 else "", few_tabs, framed)


def _check_clipboard(rows: int, columns: int) -> None:
    if columns > MAX_COLUMNS:
        raise SheetError(
            f"The clipboard holds {columns:,} columns. A sheet holds at most {MAX_COLUMNS:,}: put the data in a file and import it."
        )
    if rows * columns > MAX_CELLS:
        raise SheetError(
            f"The clipboard holds {rows:,} rows × {columns:,} columns. A sheet holds at most {MAX_CELLS:,} cells: "
            "put the data in a file and import it."
        )


class _Split(NamedTuple):
    separator: str  # "" for one column
    plain: str  # the text with its quoted cells turned into plain cells (see ``_protect``)
    hidden: tuple[str, str] | None
    few_tabs: bool = False
    framed: bool = False


def _separated(text: str, one_column: bool) -> _Split:
    """How to split the text. Tabs come first: every spreadsheet program writes them, on every
    line. Tabs on only some lines give way to a separator that makes a bigger table. A tab only
    inside a quoted cell doesn't count. Text without any table goes by its first line."""
    if one_column:
        return _Split("", *_protect(text, _spare_separator(text), strict=True))
    plain, hidden = _protect(text, "\t", strict=True)
    looked_at = _whole_lines(plain)
    if plain is text:  # no cells of a table with tabs: they can be cells of another separator
        looked_at = _QUOTED_CELL.sub("", looked_at)
    tabs = "\t" in looked_at
    if tabs and _tabs_on_most_lines(looked_at):
        return _Split("\t", plain, hidden)
    sample = _whole_lines(text)
    found = _best_table(sample, "\t;|," if tabs else ";|,")
    if tabs and found in ("", "\t"):
        return _Split("\t", plain, hidden, few_tabs=True)
    del plain
    found = found or _first_line_separator(sample)
    if not found:
        return _Split("", *_protect(text, _spare_separator(text), strict=True))
    unframed = _without_frame(text) if found == "|" else text
    return _Split(found, *_protect(unframed, found, strict=False), framed=unframed is not text)


def _tabs_on_most_lines(sample: str) -> bool:
    lines = [line for line in sample.split("\n") if line]
    return sum("\t" in line for line in lines) * 2 > len(lines)


def _whole_lines(text: str) -> str:
    """The start of the text, to find the separator in: a line cut in two looks like another kind."""
    return text if len(text) <= _SAMPLE else text[:_SAMPLE].rpartition("\n")[0] or text[:_SAMPLE]


def _best_table(sample: str, separators: str) -> str:
    """The separator of the biggest table in the text, which can have other lines around it: a
    title, a greeting, the ruler of a list from an ERP system. "" for none."""
    return _biggest({separator: _table_size(sample, separator) for separator in separators}, sample)


def _biggest(sizes: dict[str, int], sample: str) -> str:
    """The separator with the biggest size, "" if all are 0. Of equals the first, or with quotes
    in the text the one of the CSV import: a separator in the quoted cells of another one can
    split just as many lines."""
    size = max(sizes.values())
    if not size:
        return ""
    equal = [separator for separator in sizes if sizes[separator] == size]
    rule = files.detect_delimiter(sample) if len(equal) > 1 and '"' in sample else ""
    return rule if rule in equal else equal[0]


def _table_size(sample: str, separator: str) -> int:
    """How many lines the separator splits, if they make a table: at least 2 of them split the
    same way, and at most 1 in 5 of the lines from the first to the last one doesn't split, or at
    least half of all lines split (two tables, a title per group). A list of single values can
    hold a separator now and then. Commas must not cut more numbers than they split lines."""
    rows = _table_lines(sample, separator)
    counts = [_splits(row, separator) for row in rows]
    lines = [index for index, count in enumerate(counts) if count]
    if len(lines) < 2 or Counter(filter(None, counts)).most_common(1)[0][1] < 2:
        return 0
    if separator == ",":
        cut = sum(not count and _NUMBER_WITH_COMMA.search(row) is not None for row, count in zip(rows, counts, strict=True))
        if cut >= len(lines):
            return 0
    close_together = len(lines) * 5 >= (lines[-1] - lines[0] + 1) * 4
    return len(lines) if close_together or len(lines) * 2 >= len(rows) else 0


def _first_line_separator(sample: str) -> str:
    """The separator of text without a table, such as a single row: the one that splits the first
    line most. One at the end of that line is no reason to split: Hallo Hans, or abc;. Without
    any, only a list such as Müller, Hans is split, by the commas of its first line and of most
    others. "" for one column."""
    lines = {separator: _table_lines(sample, separator) for separator in ";|,"}
    counts = {s: _splits(rows[0].rstrip().rstrip(s), s) if rows else 0 for s, rows in lines.items()}
    found = _biggest(counts, sample)
    with_comma = ["," in _without_number_commas(line) for line in lines[","]]
    if found or not with_comma or not with_comma[0]:
        return found
    return "," if sum(with_comma) * 2 > len(with_comma) else ""


def _table_lines(sample: str, separator: str) -> list[str]:
    """The lines that can hold cells of this separator, with its quoted cells as plain cells."""
    plain, _ = _protect(sample, separator, strict=False)
    return [line for line in plain.split("\n") if line.strip() and not _is_ruler(line)]


def _splits(line: str, separator: str) -> int:
    """How many cells the separator adds to the line. A comma in a number (1,50) or of a sentence
    (Müller, Hans, or Hallo Hans, at the end of a line) adds none."""
    if separator == ",":
        line = _without_number_commas(line).replace(", ", " ")
    return line.count(separator)


def _without_number_commas(line: str) -> str:
    return _NUMBER_WITH_COMMA.sub("0", line).rstrip().removesuffix(",")


def _is_ruler(line: str) -> bool:
    """Whether the line only draws a line: ----, ====, +---+---+, |---|---|."""
    return not line.strip(_RULER_CHARACTERS) and any(mark * 3 in line for mark in "-=_")


def _without_frame(text: str) -> str:
    """A list in a frame of |, as lists from ERP systems and Markdown tables are, without that frame: no | at
    the start and the end of the lines, and no ruler lines. A title above it stays. Text with a
    line that has a | but no such frame comes back as it is, the same object."""
    lines = []
    for line in text.split("\n"):
        if _is_ruler(line):
            continue
        cells = line.strip()
        if "|" not in cells:
            lines.append(line)
        elif len(cells) > 1 and cells[0] == cells[-1] == "|":
            lines.append(cells[1:-1])
        else:
            return text
    return "\n".join(lines) if lines else text


def _spare_separator(text: str) -> str:
    """A one-byte separator that the text doesn't hold: a text of one column doesn't split."""
    return next((c for c in _SPARE_SEPARATORS if c not in text), files.ONE_COLUMN)


def _protect(text: str, separator: str, strict: bool) -> tuple[str, tuple[str, str] | None]:
    """The text with its quoted cells turned into plain cells, and the two characters that stand
    for the separator and for line breaks in such cells (None if no cell needed them).

    A cell is quoted when it starts with a quote and its closing quote, with doubled quotes inside,
    comes right before a separator or a line end. Anything else is plain text: 5" screen, "Best"
    offer. With ``strict`` (what spreadsheet programs write), a cell loses its quotes only if it
    needs them, because it holds a tab, a line break, a quote or the separator: they write no
    other quotes, so "Best offer" is what the cell holds."""
    if '"' not in text:
        return text, None
    free = list(islice((c for c in map(chr, range(0xE000, 0xF900)) if c not in text), 2))
    if len(free) < 2:
        return text, None
    closing = re.compile(f"{_QUOTED}(?={re.escape(separator)}|\n|\\Z)")
    finished, parts, last, position, hidden = [], [], 0, 0, False
    for start in _quote_starts(text, separator):
        if start <= position:  # inside the cell before
            continue
        end = closing.match(text, start)
        if end is None:
            continue
        cell = text[start : end.end() - 1]
        if strict and "\t" not in cell and "\n" not in cell and '""' not in cell and separator not in cell:
            continue
        cell = cell.replace('""', '"')
        if separator in cell or "\n" in cell:
            cell = cell.replace(separator, free[0]).replace("\n", free[1])
            hidden = True
        parts += (text[last : start - 1], cell)
        last = position = end.end()
        if len(parts) >= _PROTECTED_PARTS:
            finished.append("".join(parts))
            parts.clear()
    if not last:
        return text, None
    parts.append(text[last:])
    finished.append("".join(parts))
    del parts
    return "".join(finished), (free[0], free[1]) if hidden else None


def _quote_starts(text: str, separator: str) -> Iterator[int]:
    """The places right after a quote at the start of a cell, in order. Each search goes on from
    the last result of its own: a text with many of one kind and none of the other stays fast."""
    if text.startswith('"'):
        yield 1
    after_separator, after_line = separator + '"', '\n"'
    one, other = text.find(after_separator), text.find(after_line)
    while one >= 0 or other >= 0:
        if other < 0 or 0 <= one < other:
            yield one + 2
            one = text.find(after_separator, one + 2)
        else:
            yield other + 2
            other = text.find(after_line, other + 2)


def _widest(data: bytes, separator: str) -> int:
    """How many separators the line with the most has. With only the separators and the line ends
    kept, that is the longest run of separators, which a search finds without any memory: a list
    of all lines would take more than the data, and small strings of Polars stay in its memory."""
    kept = (ord(separator), 10)
    marks = data.translate(None, bytes(code for code in range(256) if code not in kept))
    mark = separator.encode()
    found, step = 0, 1
    while mark * (found + step) in marks:
        found, step = found + step, step * 2
    while step > 1:
        step //= 2
        if mark * (found + step) in marks:
            found += step
    return found


def _used_rows(columns: Iterable[pl.Series]) -> int:
    """The number of rows up to the last row with a value. Column by column: a frame would put
    the parts of all columns together first."""
    used = 0
    for column in columns:
        if column.null_count() < column.len() and column.len() > used:
            used = max(used, int(column.is_not_null().arg_true()[-1]) + 1)
    return used


# --- the sheet --------------------------------------------------------------------------------------


class _State(NamedTuple):
    """The sheet at one moment. It holds only references to immutable columns."""

    columns: tuple[pl.Series, ...]
    names: tuple[str, ...]
    header: bool
    state_id: int
    cells: int  # the cells that the edit after this state rewrote


class Sheet:
    """Rows and named columns of text. All rows and columns of its methods are data rows and
    columns: the row of names, while it is on, doesn't count.

    Each edit is one undo step; an edit that changes nothing is no step. While the switch "first
    row has the column names" is on, row 0 of the columns holds the names: the switch moves no
    data, so it gives back exactly what was there in both directions."""

    def __init__(self, columns: int = 3, rows: int = 20) -> None:
        columns = max(columns, 1)
        self._columns: list[pl.Series] = [_blank(rows) for _ in range(columns)]
        self._names: list[str] = _free_names([], columns)  # while the switch is off
        self._header = False
        self._shown_names: list[str] | None = None
        self._cache: OrderedDict[tuple[int, int], list[str | None]] = OrderedDict()
        self._undo: list[_State] = []
        self._redo: list[_State] = []
        self._ids = self._state_id = self._saved_id = 0

    @classmethod
    def from_frame(cls, frame: pl.DataFrame) -> Sheet:
        """A sheet with the columns of ``frame`` as text, not changed yet."""
        sheet = cls(columns=1, rows=0)
        if frame.width:
            sheet._columns = [_as_text(column) for column in frame.get_columns()]
            sheet._names = list(frame.columns)
        return sheet

    # --- reading

    @property
    def row_count(self) -> int:
        return self._columns[0].len() - self._offset

    @property
    def column_count(self) -> int:
        return len(self._columns)

    @property
    def names(self) -> list[str]:
        """The names of the columns, as the table will have them. Don't change this list."""
        if self._shown_names is None:
            self._shown_names = self._names
            if self._header:
                names = [_column_name(column[0], i) for i, column in enumerate(self._columns, 1)]
                self._shown_names = unique_names(names)
        return self._shown_names

    @property
    def header(self) -> bool:
        return self._header

    @property
    def has_values(self) -> bool:
        return any(column.null_count() < column.len() for column in self._columns)

    @property
    def blank(self) -> bool:
        """No value, and no column with a name of its own: a paste has nothing to keep."""
        return not self.has_values and all(_NUMBERED_NAME.fullmatch(name) for name in self.names)

    @property
    def used_rows(self) -> int:
        """The data rows up to the last row with a value."""
        return max(_used_rows(self._columns) - self._offset, 0)

    @property
    def changed(self) -> bool:
        return self._state_id != self._saved_id

    @property
    def state_id(self) -> int:
        """Another number after every edit, undo and redo that changed something."""
        return self._state_id

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def text(self, row: int, column: int) -> str:
        """The whole text of a cell, "" for an empty cell."""
        block, place = divmod(row + self._offset, BLOCK)
        return self._block(column, block)[place] or ""

    def view(self) -> pl.DataFrame:
        """All data rows, for copying."""
        return self._named(self._offset, self.row_count)

    def frame(self) -> pl.DataFrame:
        """What is written to a file. A new sheet has spare rows and columns: rows at the end
        without a value are left out, and columns at the right without a value and without a
        name of their own. Rows and columns without a value in the middle stay."""
        first, rows, names = self._offset, self.used_rows, self.names
        width = self.column_count
        while (
            width > 1
            and _NUMBERED_NAME.fullmatch(names[width - 1])
            and self._columns[width - 1].slice(first, rows).null_count() == rows
        ):
            width -= 1
        return self._named(first, rows, width)

    def mark_saved(self) -> None:
        self._saved_id = self._state_id

    # --- editing

    def load(self, pasted: Pasted) -> None:
        """Replaces everything. The switch goes on when the first row looks like names: at least
        2 rows and 2 columns, and in the first row no empty cell and no cell that is only a number
        or a date. It is only where the switch starts."""
        frame = pasted.frame
        header = frame.height >= 2 and frame.width >= 2 and all(map(_may_be_a_name, frame.row(0)))
        self._apply(
            frame.get_columns(), names=_free_names([], frame.width), header=header, cells=frame.height * frame.width
        )

    def paste(self, pasted: Pasted, row: int, column: int) -> tuple[int, int, int, int]:
        """Writes the block with its top left corner at the cell. The sheet grows when the block
        doesn't fit. Returns the range of the block."""
        frame = pasted.frame
        top = row + self._offset
        height = max(self._height, top + frame.height)
        width = max(self.column_count, column + frame.width)
        _check_size(height, width)
        grown = height - self._height
        columns = [_splice(c, self._height, _blank(grown), 0) for c in self._columns] if grown else list(self._columns)
        added = width - self.column_count
        columns += [_blank(height) for _ in range(added)]
        names = [*self._names, *_free_names(self._names, added)]
        cells = 0
        for index, values in enumerate(frame.get_columns(), column):
            if not columns[index].slice(top, values.len()).equals(values):
                columns[index] = _splice(columns[index], top, values)
                cells += values.len()
        if grown or added or cells:
            self._apply(columns, names=names, cells=cells + grown * width + added * height)
        return row, row + frame.height - 1, column, column + frame.width - 1

    def set_text(self, row: int, column: int, text: str) -> None:
        value, top = _cell(text), row + self._offset
        if self._columns[column][top] != value:
            columns = list(self._columns)
            columns[column] = _splice(columns[column], top, _one(value))
            self._apply(columns, cells=1)

    def fill(self, ranges: Ranges, text: str) -> None:
        self._write(ranges, _cell(text))

    def clear(self, ranges: Ranges) -> None:
        self._write(ranges, None)

    def fill_down(self, ranges: Ranges) -> None:
        """The top row of each range goes to the rows below it. In a range of one row, the row
        above it is the source; in the first row nothing happens."""
        columns, cells = list(self._columns), 0
        for top, bottom, left, right in self._inside(ranges):
            source, first = (top - 1, top) if top == bottom else (top, top + 1)
            if source < self._offset:
                continue
            for index in range(left, right + 1):
                values = pl.repeat(columns[index][source], bottom - first + 1, dtype=pl.String, eager=True)
                cells += self._replace(columns, index, first, values)
        if cells:
            self._apply(columns, cells=cells)

    def fill_right(self, ranges: Ranges) -> None:
        """The left column of each range goes to the columns on its right. In a range of one
        column, the column on its left is the source; in the first column nothing happens."""
        columns, cells = list(self._columns), 0
        for top, bottom, left, right in self._inside(ranges):
            source, first = (left - 1, left) if left == right else (left, left + 1)
            if source < 0:
                continue
            values = columns[source].slice(top, bottom - top + 1)
            for index in range(first, right + 1):
                cells += self._replace(columns, index, top, values)
        if cells:
            self._apply(columns, cells=cells)

    def insert_rows(self, at: int, count: int = 1) -> None:
        if count < 1:
            return
        _check_size(self._height + count, self.column_count)
        at = min(max(at, 0), self.row_count) + self._offset
        empty = _blank(count)
        self._apply([_splice(column, at, empty, 0) for column in self._columns], cells=count * self.column_count)

    def delete_rows(self, spans: list[tuple[int, int]]) -> None:
        """Deletes rows: each span is (first, last), inclusive."""
        merged = _merged(spans, self.row_count)
        if not merged:
            return
        columns, empty = self._columns, _blank(0)
        for first, last in reversed(merged):
            columns = [_splice(column, first + self._offset, empty, last - first + 1) for column in columns]
        self._apply(columns, cells=sum(last - first + 1 for first, last in merged) * self.column_count)

    def insert_columns(self, at: int, count: int = 1) -> None:
        if count < 1:
            return
        _check_size(self._height, self.column_count + count)
        at = min(max(at, 0), self.column_count)
        added = [_blank(self._height) for _ in range(count)]
        names = [*self._names[:at], *_free_names(self._names, count), *self._names[at:]]
        self._apply([*self._columns[:at], *added, *self._columns[at:]], names=names, cells=count * self._height)

    def delete_columns(self, spans: list[tuple[int, int]]) -> None:
        """Deletes columns: each span is (first, last), inclusive. A sheet keeps one column."""
        merged = _merged(spans, self.column_count)
        if not merged:
            return
        gone = {index for first, last in merged for index in range(first, last + 1)}
        kept = [index for index in range(self.column_count) if index not in gone]
        columns = [self._columns[index] for index in kept] or [_blank(self._height)]
        names = [self._names[index] for index in kept] or _free_names([], 1)
        self._apply(columns, names=names, cells=len(gone) * self._height)

    def rename_column(self, column: int, name: str) -> None:
        """While the switch is on, the name is the cell in the row of names."""
        name = name.translate(_NAME_SPACES).strip()
        if not name:
            raise SheetError("A column needs a name")
        if name == self.names[column]:
            return
        if name in self.names:
            raise SheetError(f"A column named “{name}” already exists")
        if self._header:
            columns = list(self._columns)
            columns[column] = _splice(columns[column], 0, _one(name))
            self._apply(columns, cells=1)
        else:
            self._apply(self._columns, names=[*self._names[:column], name, *self._names[column + 1 :]], cells=0)

    def set_header(self, on: bool) -> None:
        if on == self._header:
            return
        columns = self._columns
        if on and not self._height:  # the row of names needs a row
            columns = [_blank(1) for _ in columns]
        self._apply(columns, header=on, cells=0)

    def undo(self) -> None:
        if self._undo:
            state = self._undo.pop()
            self._redo.append(self._state(state.cells))
            self._use(state)

    def redo(self) -> None:
        if self._redo:
            state = self._redo.pop()
            self._undo.append(self._state(state.cells))
            self._use(state)

    # --- inside

    @property
    def _offset(self) -> int:
        return 1 if self._header else 0

    @property
    def _height(self) -> int:
        return self._columns[0].len()

    def _named(self, start: int, rows: int, width: int | None = None) -> pl.DataFrame:
        columns = zip(self._columns[:width], self.names[:width], strict=True)
        return pl.DataFrame([column.slice(start, rows).alias(name) for column, name in columns])

    def _block(self, column: int, block: int) -> list[str | None]:
        key = (column, block)
        values = self._cache.get(key)
        if values is None:
            values = self._cache[key] = self._columns[column].slice(block * BLOCK, BLOCK).to_list()
            if len(self._cache) > _CACHED_BLOCKS:
                self._cache.popitem(last=False)
        else:
            self._cache.move_to_end(key)
        return values

    def _inside(self, ranges: Ranges) -> Ranges:
        """The parts of ``ranges`` inside the sheet, as rows of the columns."""
        rows, columns, offset = self.row_count, self.column_count, self._offset
        inside = []
        for top, bottom, left, right in ranges:
            top, bottom, left, right = max(top, 0), min(bottom, rows - 1), max(left, 0), min(right, columns - 1)
            if top <= bottom and left <= right:
                inside.append((top + offset, bottom + offset, left, right))
        return inside

    def _write(self, ranges: Ranges, value: str | None) -> None:
        columns, cells = list(self._columns), 0
        for top, bottom, left, right in self._inside(ranges):
            values = pl.repeat(value, bottom - top + 1, dtype=pl.String, eager=True)
            for index in range(left, right + 1):
                cells += self._replace(columns, index, top, values)
        if cells:
            self._apply(columns, cells=cells)

    @staticmethod
    def _replace(columns: list[pl.Series], index: int, top: int, values: pl.Series) -> int:
        """Puts ``values`` into the column from row ``top`` on; returns the cells rewritten."""
        if columns[index].slice(top, values.len()).equals(values):
            return 0
        columns[index] = _splice(columns[index], top, values)
        return values.len()

    def _state(self, cells: int) -> _State:
        return _State(tuple(self._columns), tuple(self._names), self._header, self._state_id, cells)

    def _apply(
        self, columns: Iterable[pl.Series], *, names: list[str] | None = None, header: bool | None = None, cells: int
    ) -> None:
        """The one way an edit gets into the sheet. It comes after the edit is worked out in full:
        a failed edit changes nothing."""
        self._undo.append(self._state(cells))
        self._redo.clear()
        while len(self._undo) > UNDO_STEPS or (
            len(self._undo) > 1 and sum(state.cells for state in self._undo) > UNDO_CELLS
        ):
            del self._undo[0]
        self._ids += 1
        self._use(
            _State(
                tuple(columns),
                tuple(self._names if names is None else names),
                self._header if header is None else header,
                self._ids,
                0,
            )
        )

    def _use(self, state: _State) -> None:
        self._columns, self._names = list(state.columns), list(state.names)
        self._header, self._state_id = state.header, state.state_id
        self._shown_names = None
        self._cache.clear()


def _blank(rows: int) -> pl.Series:
    return pl.repeat(None, rows, dtype=pl.String, eager=True)


def _one(value: str | None) -> pl.Series:
    return pl.Series([value], dtype=pl.String)


def _cell(text: str) -> str | None:
    """What a cell holds for a typed text, the same as for a pasted one."""
    return text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n") or None


def _as_text(column: pl.Series) -> pl.Series:
    """A column of a file: one changed outside the app may hold numbers or dates now."""
    if column.dtype == pl.String:
        return column
    try:
        return column.cast(pl.String)
    except pl.exceptions.PolarsError:
        return pl.Series([None if value is None else str(value) for value in column.to_list()], dtype=pl.String)


def _may_be_a_name(value: str | None) -> bool:
    return value is not None and not _NUMBER_OR_DATE.fullmatch(value.strip())


def _column_name(value: str | None, number: int) -> str:
    """The name for the text in the row of names, as the CSV and Excel imports read it back."""
    return (value or "").translate(_NAME_SPACES).strip() or f"Column {number}"


def _free_names(names: list[str], count: int) -> list[str]:
    """``count`` names Column N, with the first free numbers."""
    taken, free, number = set(names), [], 0
    while len(free) < count:
        number += 1
        if (name := f"Column {number}") not in taken:
            free.append(name)
    return free


def _merged(spans: Iterable[tuple[int, int]], count: int) -> list[tuple[int, int]]:
    """The spans within ``count`` places, sorted; those that touch are one span."""
    merged: list[tuple[int, int]] = []
    for first, last in sorted((max(first, 0), min(last, count - 1)) for first, last in spans):
        if first > last:
            continue
        if merged and first <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], last))
        else:
            merged.append((first, last))
    return merged


def _splice(column: pl.Series, top: int, values: pl.Series, removed: int | None = None) -> pl.Series:
    """The column with ``values`` in place of ``removed`` rows (by default as many as there are
    values) from row ``top`` on. The parts share their data."""
    removed = values.len() if removed is None else removed
    out = pl.concat([column.slice(0, top), values, column.slice(top + removed)], rechunk=False)
    return out.rechunk() if out.n_chunks() > MAX_CHUNKS else out


def _check_size(rows: int, columns: int) -> None:
    if columns > MAX_COLUMNS:
        raise SheetError(f"A sheet holds at most {MAX_COLUMNS:,} columns.")
    if rows * columns > MAX_CELLS:
        raise SheetError(f"A sheet holds at most {MAX_CELLS:,} cells.")


# --- files --------------------------------------------------------------------------------------


def format_problem(frame: pl.DataFrame, suffix: str) -> str | None:
    """Why the table doesn't fit a file of this format, as a message. Only .xlsx has limits."""
    if suffix != ".xlsx":
        return None
    if frame.height > _XLSX_ROWS:
        return (
            f"An Excel sheet holds at most {_XLSX_ROWS:,} rows below the column names, and this sheet has {frame.height:,}. "
            "Choose CSV, TXT or Parquet."
        )
    if frame.width > _XLSX_COLUMNS:
        return f"An Excel sheet holds at most {_XLSX_COLUMNS:,} columns, and this sheet has {frame.width:,}. Choose CSV, TXT or Parquet."
    long = sum(len(name) > _XLSX_TEXT for name in frame.columns)
    long += sum(int((frame[name].str.len_chars() > _XLSX_TEXT).sum()) for name in frame.columns)
    if long:
        cells = "1 cell has" if long == 1 else f"{long:,} cells have"
        return f"{cells} more than {_XLSX_TEXT:,} characters, the most an Excel cell holds. Choose CSV, TXT or Parquet."
    return None


def write_file(
    frame: pl.DataFrame,
    path: Path,
    suffix: str,
    *,
    warn: Callable[[str], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    progress: Callable[[float], None] | None = None,
) -> None:
    """Writes the table in the format of ``FORMATS[suffix]``. The old file stays as it was until
    the new one is complete. Problems are ActionErrors with a plain message. ``cancelled`` raises
    ``Cancelled`` while .xlsx is written and when any format is complete, before the old file
    goes. ``progress`` (0 to 1) works for .xlsx, the one format that takes long."""
    if problem := format_problem(frame, suffix):
        raise ActionError(problem)
    try:
        with files.replace_file(path, warn) as temporary:
            if suffix == ".xlsx":
                _write_xlsx(frame, temporary, cancelled, progress)
            elif suffix == ".parquet":
                frame.write_parquet(temporary, row_group_size=_PARQUET_ROWS)
            else:
                separator = ";" if suffix == ".csv" else "\t"
                frame.write_csv(
                    temporary, separator=separator, include_bom=True, line_terminator="\r\n", quote_style="necessary"
                )
            if cancelled is not None and cancelled():
                raise Cancelled()
    except pl.exceptions.PolarsError as error:
        raise ActionError(f"Can't write {path.name}: {str(error).splitlines()[0]}") from None


def _write_xlsx(
    frame: pl.DataFrame, path: Path, cancelled: Callable[[], bool] | None, progress: Callable[[float], None] | None
) -> None:
    """Row by row with xlsxwriter's ``constant_memory``, so that the memory stays flat and the
    Stop button works: ``DataFrame.write_excel`` can do neither. All cells are strings, also
    those that look like numbers, formulas, links or XML."""
    height, width = frame.height, frame.width
    with files._scratch_folder() as scratch:  # for xlsxwriter's files: also gone after a stop or an error
        book = xlsxwriter.Workbook(
            str(path),
            {
                "constant_memory": True,
                "tmpdir": str(scratch),
                "strings_to_numbers": False,
                "strings_to_formulas": False,
                "strings_to_urls": False,
            },
        )
        sheet = book.add_worksheet("Sheet1")
        sheet.xls_strmax = _XLSX_RICH_TEXT  # format_problem has looked at the length of the cells
        sheet._escape_control_characters = _xlsx_escaped
        try:
            if _big_xml(frame):
                book.use_zip64()  # only then: Excel may offer to repair such a file
            sheet.freeze_panes(1, 0)
            sheet.set_column(0, width - 1, 16)
            sheet.ignore_errors({"number_stored_as_text": f"A1:XFD{height + 1}"})  # no green corners
            bold = book.add_format({"bold": True})
            for column, name in enumerate(frame.columns):
                sheet.write_string(0, column, _xlsx_text(name), bold)
            part = max(_XLSX_PART // width, 1)
            for start in range(0, height, part):
                for row, values in enumerate(frame.slice(start, part).iter_rows(), start + 1):
                    for column, value in enumerate(values):
                        if value is not None:
                            sheet.write_string(row, column, _xlsx_text(value))
                if cancelled is not None and cancelled():
                    raise Cancelled()
                if progress is not None:
                    progress(min(start + part, height) / height * _XLSX_ROWS_SHARE)
            book.close()
        except Exception as error:
            if isinstance(error.__context__, OSError):  # xlsxwriter's FileCreateError
                raise error.__context__ from None
            raise
        finally:
            sheet.row_data_fh.close()
    if progress is not None:
        progress(1.0)


def _xlsx_text(value: str) -> str:
    """The text as xlsxwriter must get it. With ``constant_memory``, it writes a string from <r> to
    </r> into the file as it is, as its own XML of rich text. Such a text goes in as that XML: one
    run of plain text, with its characters escaped, which every program reads as the text."""
    return f"<r><t>{escape(value)}</t></r>" if value.startswith("<r>") and value.endswith("</r>") else value


def _xlsx_escaped(data: Any) -> Any:
    """As xlsxwriter's method, which misses the second of two escapes that share a _, as in
    _x1200_x1600_: a program that reads the file would turn it into another character."""
    if not isinstance(data, str):
        return data
    data = _XLSX_ESCAPE.sub("_x005F_", data)
    data = _XLSX_CONTROL.sub(lambda found: f"_x{ord(found[0]):04X}_", data)
    return data.replace("￾", "_xFFFE_").replace("￿", "_xFFFF_")


def _big_xml(frame: pl.DataFrame) -> bool:
    """Whether the sheet in the file may pass the size that a plain zip file holds. The tags of
    the cells count for more than the text."""
    text = sum(int(frame[name].str.len_bytes().sum() or 0) for name in frame.columns)
    cells = frame.height * frame.width - sum(frame.null_count().row(0))
    return text + cells * _XLSX_CELL_BYTES + frame.height * _XLSX_ROW_BYTES > _XLSX_BIG


def stamp_of(path: Path) -> str | None:
    """What says whether a file has changed: its time and size. None if it isn't there."""
    try:
        status = os.stat(path)
    except OSError:
        return None
    return f"{status.st_mtime_ns}-{status.st_size}"


def mark(path: Path) -> dict[str, str | None]:
    """The value of ``files.SHEET_KEY`` in the settings of the import step of a file a sheet wrote."""
    return {"file": path.name, "stamp": stamp_of(path)}


def source_format(action_id: str, params: Mapping[str, Any]) -> SheetFormat | None:
    """The format of the file of an import step, if a sheet wrote it and the step still reads it:
    after another file was chosen, it is None, and it comes back with Undo."""
    if not files.sheet_marked(params):
        return None
    found = FORMATS.get(Path(params["file"]).suffix.lower())
    return found if found is not None and found.action == action_id else None


def read_file(
    path: Path, sheet_format: SheetFormat, action: Action, warn: Callable[[str], None] | None = None
) -> pl.DataFrame:
    """The file as a sheet shows it, read with the fixed settings of its format, not those of a
    step: a step may have been changed (e.g. Detect numbers and dates on), and the sheet must show
    the file itself. ``warn`` gets what the import says about values it can't show, as Save
    can't bring them back. Problems are ActionErrors."""
    params = {**sheet_format.settings, "source": "file", "file": path.name, files.SHEET_KEY: {"file": path.name}}
    context = Context(project_dir=path.parent)
    frame = action(None, params, context)  # not the full path: a %TEMP% in it would be expanded
    if warn is not None:
        for level, message in context.messages:
            if level == "warning":
                warn(message)
    return frame

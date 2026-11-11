"""Showing a Polars DataFrame in a Qt table view, fast even with millions of rows.

Only the rows on screen are read, in blocks; numbers and dates are shown the way Windows'
regional settings write them (decimal comma, dd.mm.yyyy for German settings).
"""

from __future__ import annotations

import math
import re
import struct
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import QAbstractTableModel, QLocale, QModelIndex, QPersistentModelIndex, Qt

from tafl.api.dtypes import type_label

from .labels import LONG_TIP_LINES, tooltip_text

BLOCK = 256
_MAX_BLOCKS = 64
_MAX_TEXT = 500
_TIP_TEXT = 100 * LONG_TIP_LINES  # tooltip_text shows no more
_SMALL = 1e-4  # 10 decimal places show at least 7 digits of a bigger number

Index = QModelIndex | QPersistentModelIndex
_ROOT = QModelIndex()


class DisplayFormat:
    """Turns values into text for the grid."""

    def __init__(self, locale: QLocale | None = None) -> None:
        locale = locale or QLocale.system()
        self.decimal = locale.decimalPoint() or "."
        self.date = _strftime_date(locale.dateFormat(QLocale.FormatType.ShortFormat))

    def formatter(self, dtype: pl.DataType) -> Callable[[Any], str]:
        if dtype == pl.String:
            return lambda v: v if len(v) <= _MAX_TEXT else v[:_MAX_TEXT] + "…"
        if dtype == pl.Binary:  # as text a byte takes up to 4 characters
            return lambda v: str(v) if len(v) <= _MAX_TEXT else str(v[:_MAX_TEXT]) + "…"
        if dtype.is_nested():  # a list or a record can hold cells of any size
            return _start_of_text
        if dtype == pl.Float32:  # Python gets the value with the digits of a 64-bit number
            return lambda v: self.number(v, single=True)
        if dtype.is_float():
            return self.number
        if dtype == pl.Date:
            return lambda v: v.strftime(self.date)
        if dtype == pl.Datetime:
            return self.datetime
        if dtype == pl.Boolean:
            return lambda v: "true" if v else "false"
        if dtype == pl.Decimal:  # every digit, with the decimal separator of the PC
            return lambda v: format(v, "f").replace(".", self.decimal)
        if dtype == pl.Object:  # values of a plugin's own type
            return _text_of
        return str

    def number(self, value: float, single: bool = False) -> str:
        """With up to 10 decimal places. A number below 0.0001 that they would show as 0, or
        without some of its digits, is shown with an exponent and its digits, at most 15 of them,
        as Excel's General format does: 1E-12, 1.23456789012E-05. ``single``: the value of a
        32-bit number (Float32)."""
        if -_SMALL < value < _SMALL:  # (not NaN or ∞)
            text = _tiny(value, single) if value else "0"
        elif value != value:
            return "NaN"
        elif value == math.inf or value == -math.inf:
            return "∞" if value > 0 else "-∞"
        else:
            text = f"{value:.10f}".rstrip("0").rstrip(".")
        return text.replace(".", self.decimal) if self.decimal != "." else text

    def datetime(self, value: Any) -> str:
        text = value.strftime(self.date + " %H:%M:%S")
        if getattr(value, "microsecond", 0):
            text += f".{value.microsecond:06d}".rstrip("0").replace(".", self.decimal)
        return text


class FrameModel(QAbstractTableModel):
    def __init__(self, parent: Any = None) -> None:
        super().__init__(parent)
        self.display = DisplayFormat()
        self._df: pl.DataFrame | None = None
        self._blocks: OrderedDict[int, list[tuple[Any, ...]]] = OrderedDict()
        self._formatters: list[Callable[[Any], str]] = []
        self._numeric: list[bool] = []
        self._headers: list[str] = []
        self._tooltips: list[str] = []

    @property
    def frame(self) -> pl.DataFrame | None:
        return self._df

    def set_frame(self, df: pl.DataFrame | None) -> None:
        self.beginResetModel()
        self._df = df
        self._blocks.clear()
        schema = df.schema if df is not None else {}
        self._formatters = [self.display.formatter(t) for t in schema.values()]
        self._numeric = [t.is_numeric() for t in schema.values()]
        self._headers = [f"{name}\n{type_label(t)}" for name, t in schema.items()]
        self._tooltips = [f"{name}  ({t})" for name, t in schema.items()]
        self.endResetModel()

    def rowCount(self, parent: Index = _ROOT) -> int:
        return 0 if parent.isValid() or self._df is None else self._df.height

    def columnCount(self, parent: Index = _ROOT) -> int:
        return 0 if parent.isValid() or self._df is None else self._df.width

    def data(self, index: Index, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid() or self._df is None:
            return None
        if role in (Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ToolTipRole):
            value = self.value(index.row(), index.column())
            if value is None:
                return "" if role == Qt.ItemDataRole.DisplayRole else "(empty)"
            text = self._formatters[index.column()](value)
            if role == Qt.ItemDataRole.ToolTipRole:
                if isinstance(value, bytes):  # the part that the cell shows
                    tip = text
                elif isinstance(value, list | dict):  # the part that a tooltip shows
                    tip = _start_of_text(value, _TIP_TEXT)
                else:
                    tip = _text_of(value)
                return tooltip_text(tip, LONG_TIP_LINES) if len(tip) > 40 else None
            return text
        if role == Qt.ItemDataRole.TextAlignmentRole:
            horizontal = Qt.AlignmentFlag.AlignRight if self._numeric[index.column()] else Qt.AlignmentFlag.AlignLeft
            return horizontal | Qt.AlignmentFlag.AlignVCenter
        return None

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        horizontal = orientation == Qt.Orientation.Horizontal
        if role == Qt.ItemDataRole.DisplayRole:
            if horizontal:
                return self._headers[section] if section < len(self._headers) else None
            return str(section + 1)
        if role == Qt.ItemDataRole.ToolTipRole and horizontal and section < len(self._tooltips):
            return tooltip_text(self._tooltips[section])
        if role == Qt.ItemDataRole.TextAlignmentRole and not horizontal:
            return Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        return None

    def value(self, row: int, column: int) -> Any:
        number = row // BLOCK
        block = self._blocks.get(number)
        if block is None:
            block = self._df.slice(number * BLOCK, BLOCK).rows()
            self._blocks[number] = block
            if len(self._blocks) > _MAX_BLOCKS:
                self._blocks.popitem(last=False)
        else:
            self._blocks.move_to_end(number)
        return block[row - number * BLOCK][column]

    def text(self, row: int, column: int) -> str:
        value = self.value(row, column)
        return "" if value is None else self._formatters[column](value)

    def copier(self) -> Callable[[pl.Series], list[str]] | None:
        """A function that turns a column of the frame (or part of it) into the texts the grid
        shows, for copying cells: nothing is cut, and texts are in quotes as Excel writes them
        when they have tabs or line breaks. It can run in another thread."""
        if self._df is None:
            return None

        def whole(dtype: pl.DataType) -> Callable[[Any], str]:
            if dtype in (pl.String, pl.Categorical) or isinstance(dtype, pl.Enum):  # texts, also as categories
                return excel_text
            if dtype == pl.Object:  # a plugin's own values: their texts, in quotes as texts are
                return lambda value: excel_text(_text_of(value))
            return str if dtype == pl.Binary or dtype.is_nested() else self.display.formatter(dtype)

        full = {name: whole(dtype) for name, dtype in self._df.schema.items()}

        def texts(column: pl.Series) -> list[str]:
            show = full[column.name]
            return ["" if value is None else show(value) for value in column.to_list()]

        return texts


def _tiny(value: float, single: bool) -> str:
    """A number between -0.0001 and 0.0001, not 0: with 10 decimal places when they show all its
    digits, else with an exponent and its shortest digits. A digit that only comes from computing
    in binary is none: a 64-bit number has at most 15 digits, as in Excel (0.00003 can be
    2.9999999999999997e-05, 3e-13 can be 3.0000000000000004e-13)."""
    digits = _shortest(value, single)  # with an exponent: 1.25e-05
    mantissa, _, exponent = digits.partition("e")
    if not single and len(mantissa) - ("." in mantissa) - (mantissa[0] == "-") > 15:  # more than 15 digits
        digits = f"{value:.15g}"
        mantissa, _, exponent = digits.partition("e")
    places = len(mantissa) - mantissa.index(".") - 1 if "." in mantissa else 0  # (1.25e-05: 7)
    if exponent:
        places -= int(exponent)
    if places <= 10:
        return f"{value:.10f}".rstrip("0").rstrip(".")
    return digits.replace("e", "E")


def _shortest(value: float, single: bool) -> str:
    """The shortest text that is exactly ``value`` (Python's repr), or the 32-bit number
    ``value`` (a Float32 value has fewer digits: 1e-12, not 9.999999960041972e-13)."""
    if single:
        for digits in range(1, 10):
            text = f"{value:.{digits}g}"
            if _single(float(text)) == value:
                return text
    return repr(value)


def _single(number: float) -> float:
    """``number`` as a 32-bit number."""
    return struct.unpack("<f", struct.pack("<f", number))[0]


def _text_of(value: Any) -> str:
    """``str(value)``; "(no text)" for a value whose text fails (only plugins make such values)."""
    try:
        return str(value)
    except Exception:
        return "(no text)"


def _start_of_text(value: Any, most: int = _MAX_TEXT) -> str:
    text = str(_first_part(value, most))
    return text if len(text) <= most else text[:most] + "…"


def _first_part(value: Any, most: int) -> Any:
    """A part of ``value`` whose text starts with the first ``most`` characters of the text of all
    of it: the time it takes doesn't grow with the size of a list or of the values in it."""
    if isinstance(value, str | bytes):
        if len(value) <= most:
            return value
        part = value[:most]
        for quote in ("'", '"') if isinstance(value, str) else (b"'", b'"'):
            if quote in value and quote not in part:  # in a list, its quotes depend on them
                part += quote
        return part
    if isinstance(value, list):
        part, size = [], 0
        for item in value:
            if size > most:
                break
            part.append(_first_part(item, most))
            size += len(repr(part[-1])) + 2  # the text of a list shows the repr of its items
        return part
    if isinstance(value, dict):
        record, size = {}, 0
        for key, item in value.items():
            if size > most:
                break
            record[key] = _first_part(item, most)
            size += len(repr(key)) + len(repr(record[key])) + 4  # the text of a record: {'key': value, ...}
        return record
    return value


def excel_text(text: str) -> str:
    if "\t" in text or "\n" in text or "\r" in text or text.startswith('"'):
        return '"' + text.replace('"', '""') + '"'
    return text


def _strftime_date(qt_format: str) -> str:
    """Qt's short date format (e.g. dd.MM.yy) as strftime, always with a four-digit year."""
    tokens = re.findall(r"y+|M+|d+|[^yMd]+", qt_format)
    out = []
    for token in tokens:
        if token[0] == "y":
            out.append("%Y")
        elif token[0] == "M":
            if len(token) > 2:  # month names: keep it simple and use numbers
                return "%d.%m.%Y"
            out.append("%m")
        elif token[0] == "d":
            if len(token) > 2:  # weekday names
                continue
            out.append("%d")
        else:
            out.append(token.replace("%", "%%"))
    result = "".join(out).strip(" ,")
    return result if "%d" in result and "%m" in result and "%Y" in result else "%Y-%m-%d"

"""Showing a Polars DataFrame in a Qt table view, fast even with millions of rows.

Only the rows on screen are read, in blocks; numbers and dates are shown the way Windows'
regional settings write them (decimal comma, dd.mm.yyyy for German settings).
"""

from __future__ import annotations

import math
import re
from collections import OrderedDict
from collections.abc import Callable
from typing import Any

import polars as pl
from PySide6.QtCore import QAbstractTableModel, QLocale, QModelIndex, QPersistentModelIndex, Qt

from tafl.api.dtypes import type_label

BLOCK = 256
_MAX_BLOCKS = 64
_MAX_TEXT = 500

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
        return str

    def number(self, value: float) -> str:
        if math.isnan(value):
            return "NaN"
        if math.isinf(value):
            return "∞" if value > 0 else "-∞"
        text = f"{value:.10f}".rstrip("0").rstrip(".")
        if text in ("", "-0"):
            text = "0"
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
                return str(value) if len(str(value)) > 40 else None
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
            return self._tooltips[section]
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
        shows, for copying cells: texts in full, in quotes as Excel writes them when they have
        tabs or line breaks. It can run in another thread."""
        if self._df is None:
            return None
        full = {
            name: (excel_text if dtype == pl.String else self.display.formatter(dtype)) for name, dtype in self._df.schema.items()
        }

        def texts(column: pl.Series) -> list[str]:
            show = full[column.name]
            return ["" if value is None else show(value) for value in column.to_list()]

        return texts


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

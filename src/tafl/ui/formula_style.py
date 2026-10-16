"""Colors in a formula: [columns], functions, keywords, 'texts', numbers, comments and
{parameters}, an unknown [column] or {parameter} in red, and the bracket that matches the one
at the cursor."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

from PySide6.QtGui import QColor, QFont, QSyntaxHighlighter, QTextCharFormat, QTextCursor, QTextDocument
from PySide6.QtWidgets import QPlainTextEdit, QTextEdit

from tafl.api import ActionError, expressions
from tafl.api.parameters import fill_text, formula_places
from tafl.core.formula_preview import _KEYWORDS, _may_be_column

from . import theme

# Words that read as SQL keywords besides the reserved ones (unless they name a column): window
# functions, types (CAST(x AS DATE), DATE '2024-01-01') and units (INTERVAL 3 DAY).
_MORE_KEYWORDS = {
    "interval", "over", "partition", "by", "rows", "range", "preceding", "following", "unbounded", "current", "nulls",
    "date", "time", "timestamp", "integer", "int", "bigint", "double", "decimal", "varchar", "text", "boolean",
    "year", "years", "quarter", "month", "months", "week", "weeks", "day", "days", "hour", "hours", "minute",
    "minutes", "second", "seconds",
}  # fmt: skip


def _format(color: str, *, italic: bool = False, bold: bool = False) -> QTextCharFormat:
    style = QTextCharFormat()
    style.setForeground(QColor(color))
    if italic:
        style.setFontItalic(True)
    if bold:
        style.setFontWeight(QFont.Weight.Bold)
    return style


class FormulaHighlighter(QSyntaxHighlighter):
    """Colors the whole formula from its tokens (a /* comment */ or a text may span lines).
    ``set_columns`` gives the names a formula can use: other [names] turn red. ``set_parameters``
    gives the project's parameters: {Name} gets their color wherever it is filled in; another
    {name} turns red where it would be an error (as a value, not inside quotes or a column name),
    and [Sales {Year}] is the column with the value filled in."""

    def __init__(self, document: QTextDocument) -> None:
        super().__init__(document)
        colors = theme.FORMULA_COLORS
        self._formats = {
            "column": _format(colors["column"]),
            "unknown": _format(colors["unknown"]),
            "function": _format(colors["function"]),
            "keyword": _format(colors["keyword"], bold=True),
            "string": _format(colors["text"]),
            "number": _format(colors["number"]),
            "comment": _format(colors["comment"], italic=True),
            "parameter": _format(colors["parameter"]),
        }
        self._known: set[str] | None = None  # folded names; None: not known (no data yet)
        self._values: dict[str, Any] | None = None  # the parameters: name -> value; None: not known
        self._parameters: set[str] | None = None  # their names, stripped and in lowercase
        self._text: str | None = None  # the formula the spans are for
        self._spans: list[tuple[int, int, str]] = []  # (start, end, kind) in Qt positions

    def set_columns(self, names: Iterable[str] | None) -> None:
        known = None if names is None else {expressions._fold(n) for n in names}
        if known != self._known:
            self._known = known
            self._text = None
            self.rehighlight()

    def set_parameters(self, values: Mapping[str, Any] | None) -> None:
        values = None if values is None else dict(values)
        if values != self._values:
            self._values = values
            self._parameters = None if values is None else {n.strip().lower() for n in values}
            self._text = None
            self.rehighlight()

    def highlightBlock(self, text: str) -> None:
        formula = self.document().toPlainText()
        if formula != self._text:
            self._text, self._spans = formula, self._color(formula)
        start = self.currentBlock().position()
        end = start + self.currentBlock().length()
        open_at_end = -1
        for span_start, span_end, kind in self._spans:
            if span_end <= start or span_start >= end:
                continue
            first = max(span_start, start)
            self.setFormat(first - start, min(span_end, end) - first, self._formats[kind])
            if span_end > end - 1:  # goes on in the next line: when it changes, that line is colored again
                open_at_end = hash((span_start, span_end, kind)) & 0x3FFFFFFF
        self.setCurrentBlockState(open_at_end)

    def _color(self, formula: str) -> list[tuple[int, int, str]]:
        tokens = expressions.tokenize(formula)
        spans = []
        position = _Positions(formula)
        for index, token in enumerate(tokens):
            kind = _kind(formula, tokens, index, self._known, self._values)
            if kind:
                spans.append((position(token.start), position(token.end), kind))
        for start, end, name, place in formula_places(formula):  # after the tokens: colored over them
            known = self._parameters is None or name.strip().lower() in self._parameters
            if known or place == "formula":  # an unknown {name} in a text or a name stays as it is
                spans.append((position(start), position(end), "parameter" if known else "unknown"))
        return spans


def _kind(
    formula: str, tokens: list[expressions.Token], index: int, known: set[str] | None, values: Mapping[str, Any] | None
) -> str:
    token = tokens[index]
    if token.kind in ("column", "quoted"):
        if known is not None and token.closed and (name := _column(formula, token, values)) is not None and name not in known:
            return "unknown"
        return "column"
    if token.kind in ("string", "number", "comment"):
        return token.kind
    if token.kind == "word":
        word = expressions._fold(formula[token.start : token.end])
        if expressions._is_call(tokens, index):
            return "function"
        if known is not None and word in known and _may_be_column(formula, tokens, index):
            return "column"
        if word in _KEYWORDS or word in _MORE_KEYWORDS:
            return "keyword"
    return ""


def _column(formula: str, token: expressions.Token, values: Mapping[str, Any] | None) -> str | None:
    """The folded name of a [column] or "name", with the parameters filled in as the
    calculation does; None if a parameter in it has an error (its {Name} shows that)."""
    name = expressions._name(formula, token)
    if values and "{" in name:
        try:
            name = fill_text(name, values)
        except ActionError:
            return None
    return expressions._fold(name)


def bracket_selections(edit: QPlainTextEdit) -> list[QTextEdit.ExtraSelection]:
    """The parenthesis at the cursor (before or after it) and the one that matches it."""
    text = edit.toPlainText()
    if not text:
        return []
    position = _Positions(text)
    cursor = position.index(edit.textCursor().position())
    tokens = [t for t in expressions.tokenize(text) if t.kind in ("open", "close")]
    pairs: dict[int, int] = {}
    opened: list[int] = []
    for token in tokens:
        if token.kind == "open":
            opened.append(token.start)
        elif opened:
            start = opened.pop()
            pairs[start], pairs[token.start] = token.start, start
    for at in (cursor - 1, cursor):  # the one just typed (before the cursor) first
        if at in pairs:
            return [_selection(edit, position(i)) for i in (at, pairs[at])]
    return []


def _selection(edit: QPlainTextEdit, position: int) -> QTextEdit.ExtraSelection:
    selection = QTextEdit.ExtraSelection()
    selection.format.setBackground(QColor(theme.FORMULA_COLORS["bracket"]))
    cursor = QTextCursor(edit.document())
    cursor.setPosition(position)
    cursor.setPosition(position + 1, QTextCursor.MoveMode.KeepAnchor)
    selection.cursor = cursor
    return selection


class _Positions:
    """Python indexes ↔ Qt positions (Qt counts characters outside the BMP, e.g. emoji, twice)."""

    def __init__(self, text: str) -> None:
        self._text = text
        self._simple = len(text.encode("utf-16-le")) == 2 * len(text)

    def __call__(self, index: int) -> int:
        return index if self._simple else len(self._text[:index].encode("utf-16-le")) // 2

    def index(self, position: int) -> int:
        if self._simple:
            return position
        return len(self._text.encode("utf-16-le")[: position * 2].decode("utf-16-le", errors="ignore"))

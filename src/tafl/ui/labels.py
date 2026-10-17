"""Labels for texts of any length (a path, the summary of a step with thousands of values): they
never ask for more room than they need, so a long text can't make the window wider."""

from __future__ import annotations

import textwrap
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QLabel, QSizePolicy, QWidget

SHOWN = 1000  # characters a one-line label works with at most: more never fit on a screen
_TIP_WIDTH = 100  # characters per line of a tooltip
_TIP_LINES = 12


def shortened(text: str, limit: int) -> str:
    """``text`` with at most ``limit`` characters, ending with "…" when it was longer."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def tooltip_text(text: str) -> str:
    """A long text for a tooltip: lines of about 100 characters, at most a dozen of them."""
    text = shortened(text, _TIP_WIDTH * _TIP_LINES)
    lines: list[str] = []
    for paragraph in text.splitlines():
        lines += textwrap.wrap(paragraph, _TIP_WIDTH, break_on_hyphens=False) or [""]
    if len(lines) > _TIP_LINES:
        lines = [*lines[: _TIP_LINES - 1], lines[_TIP_LINES - 1].rstrip("…") + "…"]
    return "\n".join(lines)


class ElidedLabel(QLabel):
    """One line that ends with "…" when it gets less room than its text needs; the tooltip then
    has all of it. It asks for at most ``widest`` pixels (0: as many as the text needs) and
    keeps at least ``narrowest`` of them (when its text is that long). ``text()`` is the whole
    text."""

    def __init__(self, parent: QWidget | None = None, *, widest: int = 0, narrowest: int = 0) -> None:
        super().__init__(parent)
        self._full = ""
        self._line = ""  # the text as one line, at most SHOWN characters
        self._tip = ""  # a tooltip of its own (shown also when the text fits)
        self._widest = widest
        self._narrowest = narrowest
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Preferred)

    def setText(self, text: str) -> None:
        text = text or ""
        if text == self._full:
            return
        self._full = text
        line = text[: SHOWN + 1]
        self._line = " ".join(part.strip() for part in line.splitlines() if part.strip()) if "\n" in line else line
        self.updateGeometry()
        self._elide()

    def text(self) -> str:
        return self._full

    def shown_text(self) -> str:
        """What the label shows (ending with "…" when the text doesn't fit)."""
        return super().text()

    def set_tooltip(self, text: str) -> None:
        """A tooltip that is shown also when the whole text fits."""
        self._tip = text
        self._elide()

    def minimumSizeHint(self) -> Any:
        hint = super().minimumSizeHint()
        hint.setWidth(min(self._needed(), self._narrowest))
        return hint

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        hint.setWidth(min(self._needed(), self._widest) if self._widest else self._needed())
        return hint

    def _needed(self) -> int:
        """The width of the whole text (of its first SHOWN characters)."""
        margins = self.contentsMargins()
        return self.fontMetrics().horizontalAdvance(self._line) + margins.left() + margins.right() + 2 * self.margin()

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        width = self.contentsRect().width() - 2 * self.margin()
        shown = self.fontMetrics().elidedText(self._line, Qt.TextElideMode.ElideRight, max(width, 0))
        if shown != super().text():
            super().setText(shown)
        cut = shown != self._full
        self.setToolTip(self._tip or (tooltip_text(self._full) if cut else ""))


class WrappedLabel(QLabel):
    """A label that wraps its text into lines, and is never wider than ``narrowest`` pixels
    because of a long word (a path, a long name): that word is cut off at the edge instead."""

    def __init__(self, text: str = "", parent: QWidget | None = None, *, narrowest: int = 120) -> None:
        super().__init__(text, parent)
        self._narrowest = narrowest
        self.setWordWrap(True)

    def minimumSizeHint(self) -> Any:
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), self._narrowest))
        return hint

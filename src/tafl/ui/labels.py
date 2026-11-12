"""Labels for texts of any length (a path, the summary of a step with thousands of values): they
never ask for more room than they need, so a long text can't make the window wider.

Text from a project or a data file is plain text everywhere. Qt reads a label, a tooltip or a message
box with a tag in it as HTML, and then loads the images the tag names, also from another computer.
So a label or a box that takes such text is PlainText, and a tooltip, which can't be told so, gets
``plain_html``."""

from __future__ import annotations

import html
import textwrap
from typing import Any

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QPalette, QTextLayout, QTextOption
from PySide6.QtWidgets import QFrame, QLabel, QMessageBox, QPlainTextEdit, QSizePolicy, QVBoxLayout, QWidget

from . import theme

SHOWN = 1000  # characters a one-line label works with at most: more never fit on a screen
_TIP_WIDTH = 100  # characters per line of a tooltip
_TIP_LINES = 12
LONG_TIP_LINES = 40  # lines of a tooltip that is the place to read all of a text: a cell, an error


def shortened(text: str, limit: int) -> str:
    """``text`` with at most ``limit`` characters, ending with "…" when it was longer."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def plain_html(text: str) -> str:
    """``text`` for a tooltip, which looks for HTML by itself: as HTML that reads exactly as ``text``,
    with its spaces and line breaks; the lines stay as they are. The <p> makes sure that Qt takes it as
    HTML: an escaped text alone would show &amp;. No text stays no text (an empty paragraph would be an
    empty tooltip)."""
    if not text:
        return ""
    return f"<p style='white-space:pre'>{html.escape(text, quote=False)}</p>"


def tooltip_text(text: str, most_lines: int = _TIP_LINES) -> str:
    """Any text for a tooltip, as ``plain_html``: lines of about 100 characters, at most ``most_lines`` of
    them."""
    text = shortened(text, _TIP_WIDTH * most_lines)
    lines: list[str] = []
    for paragraph in text.splitlines():
        lines += textwrap.wrap(paragraph, _TIP_WIDTH, break_on_hyphens=False) or [""]
    if len(lines) > most_lines:
        lines = [*lines[: most_lines - 1], lines[most_lines - 1].rstrip("…") + "…"]
    return plain_html("\n".join(lines))


def message_box(parent: QWidget | None, title: str, text: str, question: bool = False) -> bool:
    """A warning with OK, or with ``question`` a question with Yes and No, that takes ``text`` as it is:
    QMessageBox.warning and QMessageBox.question can't be told so, and HTML in them would also be what
    Ctrl+C copies from the box. Returns whether the answer is Yes."""
    button = QMessageBox.StandardButton
    icon = QMessageBox.Icon.Question if question else QMessageBox.Icon.Warning
    box = QMessageBox(icon, title, "", button.Yes | button.No if question else button.Ok, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.setText(text)
    box.exec()
    answer = box.standardButton(box.clickedButton())
    box.deleteLater()
    return answer == button.Yes


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
        self.setToolTip(tooltip_text(self._tip or (self._full if cut else "")))


class WrappedLabel(QLabel):
    """A label that wraps its text into lines, and is never wider than ``narrowest`` pixels
    because of a long word (a path, a long name): a line that is still too wide ends with "…".
    With ``fill`` the label has a height of its own (e.g. a page of its own): when its text needs
    more lines than fit, the last line that fits ends with "…". The tooltip then has the text,
    and ``text()`` is all of it."""

    def __init__(
        self, text: str = "", parent: QWidget | None = None, *, narrowest: int = 120, fill: bool = False
    ) -> None:
        super().__init__(parent)
        self._narrowest = narrowest
        self._fill = fill
        self._full: str | None = None
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setWordWrap(True)
        self.setText(text)

    def setText(self, text: str) -> None:
        text = text or ""
        if text == self._full:
            return
        self._full = text
        self._fit()

    def text(self) -> str:
        return self._full or ""

    def shown_text(self) -> str:
        """What the label shows (lines that end with "…" where it is cut)."""
        return super().text()

    def minimumSizeHint(self) -> Any:
        hint = super().minimumSizeHint()
        hint.setWidth(min(hint.width(), self._narrowest))
        return hint

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        full = self._full or ""
        text = shortened(full, WRAPPED)
        width = self.contentsRect().width() - 2 * self.margin()
        lines = _lines(text, self, width) if width > 0 and text else []
        rows = len(lines)
        if self._fill and lines:  # the lines that fit into the label's height
            rows = max(1, (self.contentsRect().height() - 2 * self.margin()) // self.fontMetrics().lineSpacing())
        if any(wide > width for _, wide in lines) or rows < len(lines):
            metrics = self.fontMetrics()
            shown = [metrics.elidedText(line, Qt.TextElideMode.ElideRight, width) for line, _ in lines[:rows]]
            if rows < len(lines) and not shown[-1].endswith("…"):  # more lines below
                last = shown[-1]
                while last and metrics.horizontalAdvance(last + "…") > width:
                    last = last[:-1]
                shown[-1] = last.rstrip() + "…"
            text = "\n".join(shown)
        if text != super().text():
            super().setText(text)
        self.setToolTip(tooltip_text(full, LONG_TIP_LINES) if text != full else "")


WRAPPED = 10_000  # characters a wrapped label works with at most: more never fit on a screen


def _lines(text: str, label: QLabel, width: int) -> list[tuple[str, float]]:
    """The lines into which a label with word wrap breaks ``text`` at ``width`` pixels, each with
    its width: one that is wider has a word that doesn't fit."""
    option = QTextOption()
    option.setWrapMode(QTextOption.WrapMode.WordWrap)
    lines = []
    for paragraph in text.split("\n"):
        layout = QTextLayout(paragraph, label.font(), label)
        layout.setTextOption(option)
        layout.beginLayout()
        while (line := layout.createLine()).isValid():
            line.setLineWidth(width)
            start = line.textStart()
            lines.append((paragraph[start : start + line.textLength()].rstrip(" "), line.naturalTextWidth()))
        layout.endLayout()
    return lines


class MessageBox(QFrame):
    """A box for a message of any length (an error, a warning, what a step reports): its text
    wraps into lines, also a long word (a path) where it must; the box grows with the text up
    to ``lines`` lines, and a longer text scrolls. It never makes the window wider, and its text
    can be selected and copied. ``name`` (ErrorBox, WarningBox or InfoBox) gives its colors."""

    def __init__(self, name: str, parent: QWidget | None = None, *, lines: int = 8) -> None:
        super().__init__(parent)
        self.setObjectName(name)  # the style sheet draws the box
        self._most = lines
        self._full = ""
        self.view = _MessageText(self)
        palette = self.view.palette()  # the colors of the text, without a style sheet of its own:
        background, text = theme.BOXES[name][0], theme.BOXES[name][2]  # (else the scroll bar is black)
        for role, color in ((QPalette.ColorRole.Base, background), (QPalette.ColorRole.Text, text)):
            palette.setColor(role, QColor(color))
        self.view.setPalette(palette)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.view)
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    def setText(self, text: str) -> None:
        text = text or ""
        if text != self._full:
            self._full = text
            mode = _wrap_mode(text)
            if self.view.wordWrapMode() != mode:
                self.view.setPlainText("")  # the text before isn't laid out again the other way first
                self.view.setWordWrapMode(mode)
            self.view.setPlainText(text)
            self.fit()

    def text(self) -> str:
        return self._full

    def fit(self) -> None:
        """As high as the lines of the text at the box's width (whole lines), up to the most;
        more scroll."""
        view = self.view
        lines = _line_count(self._full, view, view.width(), self._most + 1)
        scrolls = lines > self._most
        policy = Qt.ScrollBarPolicy.ScrollBarAlwaysOn if scrolls else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        if view.verticalScrollBarPolicy() != policy:
            view.setVerticalScrollBarPolicy(policy)
        # One pixel more: QPlainTextEdit counts a line as shown only with a pixel below it
        height = min(lines, self._most) * view.fontMetrics().lineSpacing() + 1
        if view.height() != height:
            view.setFixedHeight(height)
        if round(view.document().documentLayout().documentSize().width()) != view.viewport().width():
            # QPlainTextEdit lays out its text again only at a resize event of the text's area: the
            # scroll bar that comes or goes in one (or while hidden) brings none
            view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
            view.setLineWrapMode(QPlainTextEdit.LineWrapMode.WidgetWidth)


def _wrap_mode(text: str) -> QTextOption.WrapMode:
    """How a MessageBox wraps ``text``: at spaces, and anywhere in a word that is too long for a
    line. A text of more than 20,000 characters wraps anywhere: Qt breaks a long word into lines
    in a time that grows with the square of its length (a word of a million characters took
    minutes), and a word can be longer than it looks: Qt doesn't break at a no-break space. In a
    shorter text no word takes long."""
    return QTextOption.WrapMode.WrapAnywhere if len(text) > _LONG_TEXT else QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere


_LONG_TEXT = 20_000  # characters


def _line_count(text: str, widget: QPlainTextEdit, width: int, most: int) -> int:
    """The lines of ``text`` in ``widget`` at ``width`` pixels, wrapped as ``widget`` wraps them;
    ``most`` at most (for a long text)."""
    option = QTextOption()
    option.setWrapMode(widget.wordWrapMode())
    count = 0
    for paragraph in text.split("\n"):
        layout = QTextLayout(paragraph, widget.font(), widget)
        layout.setTextOption(option)
        layout.beginLayout()
        while count < most and (line := layout.createLine()).isValid():
            line.setLineWidth(max(width, 1))
            count += 1
        layout.endLayout()
        if count >= most:
            break
    return max(count, 1)


class _MessageText(QPlainTextEdit):
    """The text of a MessageBox."""

    def __init__(self, box: MessageBox) -> None:
        super().__init__(box)
        self._box = box
        self._width = -1  # the width that the box fitted its height to
        self.setReadOnly(True)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.document().setDocumentMargin(0)  # whole lines, none cut in half
        self.setWordWrapMode(QTextOption.WrapMode.WrapAtWordBoundaryOrAnywhere)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)  # Tab doesn't stop here; a click can select and copy
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

    def minimumSizeHint(self) -> Any:
        return QSize(_NARROWEST_BOX, super().minimumSizeHint().height())

    def resizeEvent(self, event: Any) -> None:  # (of the viewport: also when the scroll bar comes or goes)
        super().resizeEvent(event)
        if self.width() != self._width:  # another width: other lines
            self._width = self.width()
            self._box.fit()


_NARROWEST_BOX = 40  # pixels of the text of a message box at least

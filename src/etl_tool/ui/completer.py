"""The suggestion list of a formula editor: it opens on [ and { and after two letters of a word
(or with Ctrl+Space), follows the typing, and Enter, Tab or a click writes the suggestion in."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QKeyEvent, QTextCursor
from PySide6.QtWidgets import QFrame, QLabel, QPlainTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget

from etl_tool.core.completion import Context, Suggestion, context_at, insertion, starts_with, suggestions

from . import theme

_SHOWN_ROWS = 10
_KEYS_ACCEPT = (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Tab)
_KEYS_MOVE = (Qt.Key.Key_Up, Qt.Key.Key_Down, Qt.Key.Key_PageUp, Qt.Key.Key_PageDown)


class _Popup(QFrame):
    """The list and a line about the selected entry. A child of the window, never focused:
    the typing goes on in the formula."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("Suggestions")
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setStyleSheet(
            f"QFrame#Suggestions {{ background: {theme.BASE}; border: 1px solid {theme.BORDER_LIGHT}; }}"
            f"QTreeWidget {{ border: none; }} QLabel {{ color: {theme.TEXT_DIM}; padding: 2px 4px; }}"
        )
        self.list = QTreeWidget(self)
        self.list.setColumnCount(2)
        self.list.setHeaderHidden(True)
        self.list.setRootIsDecorated(False)
        self.list.setUniformRowHeights(True)
        self.list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.list.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.help = QLabel(self)
        self.help.setWordWrap(True)
        self.help.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(1, 1, 1, 1)
        layout.setSpacing(0)
        layout.addWidget(self.list, 1)
        layout.addWidget(self.help)
        self.items: list[Suggestion] = []
        self.list.currentItemChanged.connect(lambda _current, _previous: self._show_help())

    def fill(self, items: list[Suggestion]) -> None:
        self.items = items
        self.list.clear()
        rows = []
        for index, item in enumerate(items):
            row = QTreeWidgetItem([item.name, item.detail])
            row.setData(0, Qt.ItemDataRole.UserRole, index)
            row.setForeground(1, QColor(theme.ACCENT if item.row is not None else theme.TEXT_DIM))
            rows.append(row)
        self.list.addTopLevelItems(rows)
        self.list.setCurrentItem(rows[0])
        self.list.resizeColumnToContents(0)

    @property
    def current(self) -> Suggestion | None:
        row = self.list.currentItem()
        return self.items[row.data(0, Qt.ItemDataRole.UserRole)] if row is not None else None

    def move_selection(self, key: int) -> None:
        count = self.list.topLevelItemCount()
        row = self.list.indexOfTopLevelItem(self.list.currentItem())
        step = {Qt.Key.Key_Up: -1, Qt.Key.Key_Down: 1, Qt.Key.Key_PageUp: -_SHOWN_ROWS, Qt.Key.Key_PageDown: _SHOWN_ROWS}[key]
        self.list.setCurrentItem(self.list.topLevelItem(max(0, min(count - 1, row + step))))

    def _show_help(self) -> None:
        item = self.current
        text = "" if item is None else item.help or (item.detail if item.kind != "column" else "")
        self.help.setText(text)
        self.help.setVisible(bool(text))


class FormulaCompleter(QObject):
    """Suggestions for ``edit``. ``columns()`` gives the columns the formula can use,
    ``parameters()`` the project's parameters."""

    def __init__(
        self,
        edit: QPlainTextEdit,
        columns: Callable[[], Sequence[Suggestion]],
        parameters: Callable[[], Sequence[Suggestion]] = list,
    ) -> None:
        super().__init__(edit)
        self.edit = edit
        self._columns = columns
        self._parameters = parameters
        self._popup: _Popup | None = None
        self._context: Context | None = None
        self._typed = False  # the next change of the text comes from a key
        self._forced = False  # opened with Ctrl+Space: also for one letter or none
        self._writing = False
        edit.installEventFilter(self)
        edit.textChanged.connect(self._text_changed)
        edit.cursorPositionChanged.connect(self._cursor_moved)
        self._follow = QTimer(self)  # keeps the list at the cursor, e.g. when the panel scrolls
        self._follow.setInterval(150)
        self._follow.timeout.connect(self._place)

    @property
    def visible(self) -> bool:
        return self._popup is not None and self._popup.isVisible()

    @property
    def shown(self) -> list[Suggestion]:
        return list(self._popup.items) if self.visible else []

    def open(self) -> None:
        """Show the suggestions for what is at the cursor, even for less than two letters."""
        self._forced = True
        self._update(opening=True)

    def refresh(self) -> None:
        """The columns may have changed (e.g. the first check of the formula came in)."""
        if self.visible:
            self._update(opening=False)

    def close(self) -> None:
        self._forced = False
        self._context = None
        self._follow.stop()
        if self._popup is not None:
            self._popup.hide()

    def accept(self, item: Suggestion | None = None) -> None:
        """Write the selected suggestion (or ``item``) in place of what is typed."""
        item = item or (self._popup.current if self._popup is not None else None)
        context = self._context
        self.close()
        if item is None or context is None:
            return
        text = self.edit.toPlainText()
        new, cursor = insertion(context, item, text)
        replacement = new[context.start : len(new) - (len(text) - context.end)]
        edit_cursor = self.edit.textCursor()
        edit_cursor.setPosition(_utf16(text, context.start))
        edit_cursor.setPosition(_utf16(text, context.end), QTextCursor.MoveMode.KeepAnchor)
        self._writing = True
        try:
            edit_cursor.insertText(replacement)
        finally:
            self._writing = False
        edit_cursor.setPosition(_utf16(new, cursor))
        self.edit.setTextCursor(edit_cursor)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if watched is not self.edit:
            return False
        kind = event.type()
        if kind == QEvent.Type.ShortcutOverride and isinstance(event, QKeyEvent) and self.visible:
            if event.key() in _KEYS_ACCEPT and not event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier):
                event.accept()  # Ctrl+Enter takes the suggestion, it doesn't apply a half-typed formula
                return True
        elif kind == QEvent.Type.KeyPress and isinstance(event, QKeyEvent):
            key, modifiers = event.key(), event.modifiers()
            if self.visible:
                if key in _KEYS_MOVE:
                    self._popup.move_selection(key)
                    return True
                if key in _KEYS_ACCEPT and not modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier):
                    self.accept()
                    return True
                if key == Qt.Key.Key_Escape:
                    self.close()
                    return True
            if key == Qt.Key.Key_Space and modifiers & Qt.KeyboardModifier.ControlModifier:
                self.open()
                return True
            self._typed = bool(event.text()) and event.text().isprintable()
        elif kind == QEvent.Type.InputMethod and event.commitString():  # e.g. a character composed with an IME
            self._typed = True
        elif kind in (QEvent.Type.FocusOut, QEvent.Type.Hide):
            self.close()
        return False

    def _text_changed(self) -> None:
        typed, self._typed = self._typed, False
        if self._writing:
            return
        if typed or self.visible:
            self._update(opening=typed)

    def _cursor_moved(self) -> None:
        if self.visible and not self._writing:
            self._update(opening=False)

    def _update(self, *, opening: bool) -> None:
        cursor = self.edit.textCursor()
        text = self.edit.toPlainText()
        context = None if cursor.hasSelection() else context_at(text, _python_index(text, cursor.position()), forced=self._forced)
        if context is None:
            items = []
        elif context.kind == "parameter":
            items = suggestions(context, (), self._parameters())
        else:
            items = suggestions(context, self._columns())
        if context is not None and context.kind == "word" and items and items[0].key.casefold() == context.prefix.casefold():
            if items[0].kind == "keyword" and len([i for i in items if i.key.casefold() == context.prefix.casefold()]) == 1:
                items = []  # the word is complete: Enter must apply the formula, not write it again
        if not self.visible and not self._forced and not any(starts_with(context, item) for item in items):
            items = []  # only names that contain the word somewhere: not worth opening the list for
        if not items or not (opening or self.visible):
            self.close()
            return
        self._context = context
        if self._popup is None or self._popup.parent() is not self.edit.window():
            self._popup = _Popup(self.edit.window())
            self._popup.list.itemClicked.connect(lambda _row, _column: self.accept())
            self.edit.destroyed.connect(self._popup.deleteLater)  # e.g. the rows of a list are rebuilt
        self._popup.fill(items)
        self._place()
        self._popup.show()
        self._popup.raise_()
        self._follow.start()

    def _place(self) -> None:
        popup, window = self._popup, self.edit.window()
        if popup is None or not self.edit.isVisible() or not self.edit.viewport().visibleRegion().contains(self.edit.cursorRect().center()):
            self.close()  # e.g. the panel was scrolled: the list must not float elsewhere
            return
        rows = min(len(popup.items), _SHOWN_ROWS)
        height = rows * (popup.list.sizeHintForRow(0) or popup.fontMetrics().height() + 4) + 6
        if popup.help.isVisible():
            height += popup.help.heightForWidth(420) + 4
        width = min(440, window.width() - 8)
        rect = self.edit.cursorRect()
        below = self.edit.viewport().mapTo(window, rect.bottomLeft()) + QPoint(0, 3)
        above = self.edit.viewport().mapTo(window, rect.topLeft()) - QPoint(0, height + 3)
        point = below if below.y() + height <= window.height() or above.y() < 0 else above
        x = max(4, min(point.x(), window.width() - width - 4))
        popup.setGeometry(x, point.y(), width, height)


def _utf16(text: str, index: int) -> int:
    """Qt counts characters outside the BMP (e.g. emoji) as two."""
    return len(text[:index].encode("utf-16-le")) // 2


def _python_index(text: str, position: int) -> int:
    """The index in ``text`` of a Qt cursor position."""
    return len(text.encode("utf-16-le")[: position * 2].decode("utf-16-le", errors="ignore"))

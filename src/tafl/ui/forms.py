"""Settings forms, built from the parameters an action (or connection type) declares.

Every parameter kind has an editor widget. Editors report a change once a value is complete
(Enter, leaving the field, picking from a list), not on every keystroke.
"""

from __future__ import annotations

import html
import os
import weakref
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import polars as pl
import shiboken6
from PySide6.QtCore import QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QSizePolicy,
    QSpinBox,
    QStyle,
    QStyleOptionComboBox,
    QStyleOptionSpinBox,
    QTextEdit,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tafl.api.dtypes import type_label
from tafl.api.expressions import is_panic
from tafl.api.params import List, Param, is_visible
from tafl.core.completion import Suggestion, column_suggestions, parameter_suggestions, warm_up
from tafl.core.formula_preview import NO_DATA, Preview, preview

from . import formula_window, functions_window, theme
from .checks import checks
from .completer import FormulaCompleter
from .formula_style import FormulaHighlighter, bracket_selections
from .formula_window import FormulaWindow
from .frame_model import DisplayFormat

_CHECK_DELAY_MS = 300  # a formula is checked this long after the last key


@dataclass
class FormContext:
    """What editors offer as choices."""

    columns: Callable[[str, dict[str, Any]], list[str] | None] = lambda source, values: None
    input_frame: Callable[[], pl.DataFrame | None] = lambda: None  # the data going into the step
    input_problem: Callable[[], str] = lambda: ""  # why there is none
    tables: list[tuple[str, str]] = field(default_factory=list)  # (id, name)
    connections: list[str] = field(default_factory=list)
    project_dir: Path | None = None
    edit_connections: Callable[[], None] | None = None
    parameters: Callable[[], dict[str, Any]] = dict  # name -> value of the project's parameters


class Editor(QWidget):
    """Base class: one parameter's input widget."""

    changed = Signal()

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.param = param
        self.ctx = ctx
        self.setToolTip(param.help)

    def value(self) -> Any:
        raise NotImplementedError

    def set_value(self, value: Any) -> None:
        raise NotImplementedError

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        """Update the choices offered (columns, tables, ...)."""
        self.ctx = ctx

    def set_placeholder(self, text: str) -> None:
        """Grey hint shown while the field is empty (used where there is no label)."""

    def allow_narrow(self) -> None:
        """In a row of a list, next to other fields: drop-down lists may get narrow (their texts
        are cut off), and number fields are as wide as their number, without arrows, so that the row
        fits a narrow settings panel."""
        for combo in self.findChildren(_Combo):
            combo.make_narrow()
        for spin in self.findChildren(_Spin):
            spin.make_narrow()

    def focus_input(self) -> QWidget:
        """Put the keyboard focus into the editor; returns the widget that has it (its focus proxy,
        else its first child that takes Tab focus, else itself). A line edit gets its text
        selected."""
        widget = self.focusProxy()
        if widget is None:
            widget = next(
                (
                    child
                    for child in self.findChildren(QWidget)
                    if child.focusPolicy() & Qt.FocusPolicy.TabFocus and child.isEnabled() and child.isVisibleTo(self)
                ),
                self,
            )
        widget.setFocus(Qt.FocusReason.OtherFocusReason)
        line = widget.lineEdit() if isinstance(widget, QComboBox) else widget
        if isinstance(line, QLineEdit):
            line.selectAll()
        return widget


class _Combo(QComboBox):
    """A drop-down list; ``make_narrow`` lets it be as wide as its text where there is room, and
    narrower where there isn't."""

    narrow = False

    def make_narrow(self) -> None:
        if not self.narrow:
            self.narrow = True
            self.currentTextChanged.connect(lambda _text: self.updateGeometry())
            self.updateGeometry()

    def _width(self, text: str) -> int:
        """The width for ``text``, with the arrow and the frame."""
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        metrics = self.fontMetrics()
        content = QSize(metrics.horizontalAdvance(text) + 4, metrics.height())
        return self.style().sizeFromContents(QStyle.ContentsType.CT_ComboBox, option, content, self).width()

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        if self.narrow:
            hint.setWidth(self._width(self.currentText() or "MMMM"))
        return hint

    def minimumSizeHint(self) -> Any:
        hint = super().minimumSizeHint()
        if self.narrow:
            hint.setWidth(min(hint.width(), self._width("MM")))
        return hint


class _Spin(QSpinBox):
    """A whole number field; ``make_narrow`` makes it as wide as the number it shows (at least two
    digits, also while a number is typed) and without arrows, instead of as wide as its largest
    possible number. The mouse wheel and the arrow keys still change the number."""

    narrow = False

    def make_narrow(self) -> None:
        if not self.narrow:
            self.narrow = True
            self.setButtonSymbols(QSpinBox.ButtonSymbols.NoButtons)
            self.lineEdit().textChanged.connect(lambda _text: self.updateGeometry())  # typing
            # A new value from the wheel, the arrow keys or the restore of an emptied field: Qt
            # writes its text with the line edit's signals blocked
            self.valueChanged.connect(lambda _value: self.updateGeometry())
            self.editingFinished.connect(self.updateGeometry)
            self.updateGeometry()

    def _width(self) -> int:
        """The width for the number shown (also the one being typed), with the frame."""
        option = QStyleOptionSpinBox()
        self.initStyleOption(option)
        metrics = self.fontMetrics()
        digits = max(self.text(), "00", key=len)
        content = QSize(metrics.horizontalAdvance(digits + " ") + 2, metrics.height())  # as Qt measures its range
        return self.style().sizeFromContents(QStyle.ContentsType.CT_SpinBox, option, content, self).width()

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        if self.narrow:
            hint.setWidth(min(hint.width(), self._width()))
        return hint

    def minimumSizeHint(self) -> Any:
        hint = super().minimumSizeHint()
        if self.narrow:
            hint.setWidth(min(hint.width(), self._width()))
        return hint


def _shown(param: Param, value: Any) -> Any:
    """What the editor shows: the default for a value of the wrong kind (e.g. a number for a
    table, from a project file written by hand), which the step's check reports."""
    return value if value is None or param.accepts(value) else param.initial()


def _row(*widgets: QWidget, stretch_first: bool = True) -> QHBoxLayout:
    layout = QHBoxLayout()
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(4)
    for i, widget in enumerate(widgets):
        layout.addWidget(widget, 1 if i == 0 and stretch_first else 0)
    return layout


class _PlainEdit(QPlainTextEdit):
    """Multi-line text that commits when focus leaves or on Ctrl+Enter."""

    committed = Signal()

    def focusOutEvent(self, event: Any) -> None:
        super().focusOutEvent(event)
        self.committed.emit()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.committed.emit()
            return
        super().keyPressEvent(event)


class TextEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._multiline = bool(getattr(param, "multiline", False))
        if self._multiline:
            self._edit: QLineEdit | _PlainEdit = _PlainEdit(self)
            self._edit.setFixedHeight(70)
            self._edit.committed.connect(self._commit)
        else:
            self._edit = QLineEdit(self)
            self._edit.setPlaceholderText(getattr(param, "placeholder", ""))
            self._edit.editingFinished.connect(self._commit)
        self._last = ""
        self.setLayout(_row(self._edit))

    def value(self) -> str:
        return self._edit.toPlainText() if self._multiline else self._edit.text()

    def set_value(self, value: Any) -> None:
        text = "" if value is None else str(value)
        if self._multiline:
            self._edit.setPlainText(text)
        else:
            self._edit.setText(text)
            self._edit.setCursorPosition(0)  # a narrow field shows the beginning of the text
        self._last = text

    def set_placeholder(self, text: str) -> None:
        if not self._edit.placeholderText():
            self._edit.setPlaceholderText(text)

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class CodeEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._edit = _PlainEdit(self)
        self._edit.setFont(theme.monospace_font())
        self._edit.setMinimumHeight(150)
        self._edit.setPlaceholderText("SELECT ...   (Ctrl+Enter to apply)")
        self._edit.committed.connect(self._commit)
        self._last = ""
        self.setLayout(_row(self._edit))

    def value(self) -> str:
        return self._edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._edit.setPlainText(self._last)

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class _FormulaEdit(_PlainEdit):
    """Like Excel's formula bar: Enter applies, Shift+Enter or Alt+Enter starts a new line."""

    focused = Signal()
    left = Signal()  # the focus went elsewhere
    expand = Signal()  # F2: the formula window

    def focusInEvent(self, event: Any) -> None:
        super().focusInEvent(event)
        self.focused.emit()

    def focusOutEvent(self, event: Any) -> None:
        if event.reason() == Qt.FocusReason.ActiveWindowFocusReason and isinstance(
            QApplication.activeWindow(), (functions_window.FunctionsWindow, formula_window.FormulaWindow)
        ):  # looking something up or editing in the big window: the formula isn't finished yet
            QPlainTextEdit.focusOutEvent(self, event)
        else:
            super().focusOutEvent(event)
        self.left.emit()

    def keyPressEvent(self, event: Any) -> None:
        if event.key() == Qt.Key.Key_F2:
            self.expand.emit()
            return
        plain_enter = event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and not (
            event.modifiers() & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.AltModifier)
        )
        if plain_enter:
            self.committed.emit()
            return
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.insertPlainText("\n")
            return
        super().keyPressEvent(event)


class _CheckLine(QLabel):
    """The line under a formula: what it gives, or what is wrong with it (two lines at most,
    all of it in the tooltip). Clicking it shows the mistake in the formula."""

    clicked = Signal()

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWordWrap(True)
        self.setMaximumHeight(2 * self.fontMetrics().lineSpacing() + 4)
        self.setTextFormat(Qt.TextFormat.RichText)
        self.setTextInteractionFlags(Qt.TextInteractionFlag.LinksAccessibleByMouse)

    def mousePressEvent(self, event: Any) -> None:
        super().mousePressEvent(event)
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()


class ExpressionEditor(Editor):
    """A formula (up to three visible lines) in colors, with suggestions while typing ([ and
    two letters of a word), and buttons for the column list, the functions and the formula
    window. The line under it shows what the formula gives on the first rows, or what is wrong."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._edit = _FormulaEdit(self)
        self._edit.setFont(theme.monospace_font())
        self._edit.setFixedHeight(3 * self._edit.fontMetrics().lineSpacing() + 12)
        self._edit.setTabChangesFocus(True)
        self._edit.setPlaceholderText(getattr(param, "placeholder", "") or "[Column] * 2")
        self._edit.committed.connect(self._commit)
        self._edit.focused.connect(lambda: functions_window.remember(self))
        self._insert = QToolButton(self)
        self._insert.setText("[ ]")
        self._insert.setToolTip("Insert a column name: the list opens (as when typing [)")
        self._insert.clicked.connect(self._open_columns)
        self._highlighter = FormulaHighlighter(self._edit.document())
        self._completer = FormulaCompleter(self._edit, self._column_suggestions, lambda: parameter_suggestions(self.ctx.parameters()))
        self._mistake: list[QTextEdit.ExtraSelection] = []  # the wavy underline
        self._edit.cursorPositionChanged.connect(self._update_selections)
        self._edit.focused.connect(self._update_selections)
        self._edit.left.connect(self._update_selections)
        self._edit.focused.connect(warm_up)
        self._functions = QToolButton(self)
        self._functions.setText("ƒx")
        self._functions.setToolTip("Functions: what they do and how they are written (F1)")
        self._functions.clicked.connect(self._show_functions)
        self._expand = QToolButton(self)
        self._expand.setText("⤢")
        self._expand.setToolTip("Formula window: a bigger editor with the columns, the functions and a preview (F2)")
        self._expand.clicked.connect(self.open_window)
        self._edit.expand.connect(self.open_window)
        # A click on the buttons or the line under the formula must leave the focus in it: if it
        # went to the panel, the formula would be applied half-typed. Qt gives the focus to the
        # nearest widget that takes it on a click: this editor, which passes it to the formula.
        for button in (self._insert, self._functions, self._expand):
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)  # the keys [, F1 and F2 do the same
        self.setFocusPolicy(Qt.FocusPolicy.ClickFocus)
        self.setFocusProxy(self._edit)
        self._columns: list[str] = []
        self._last = ""
        # In a list (Calculate columns): the (name, formula) of the rows above, and this row's name
        self.earlier: Callable[[], list[tuple[str, str]]] = list
        self.creates_name: Callable[[], str] = str
        self.creates = False  # the formula of a new column: the row needs a name
        self.window_open: FormulaWindow | None = None
        self.line = _CheckLine(self)
        self.line.hide()
        self.line.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.line.clicked.connect(self._show_mistake)
        self.line.linkActivated.connect(lambda _link: self._fix_mistake())
        self._check = checks().client(self._checked, self._check_failed)
        self.destroyed.connect(self._check.close)
        self._check_timer = QTimer(self)
        self._check_timer.setSingleShot(True)
        self._check_timer.setInterval(_CHECK_DELAY_MS)
        self._check_timer.timeout.connect(self.check)
        self._edit.textChanged.connect(self._typed)
        # The input of the last check: a weak reference to the frame (a field must not keep its
        # input data alive) or None for no data, and the rest
        self._checked_input: tuple[weakref.ref | None, tuple] | None = None
        self._result: Preview | None = None
        self._result_text = ""  # the formula the result is for
        self._line: tuple[str, str] = ("", "")  # (rich text, color) of the line for the result
        buttons = QVBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(2)
        buttons.addWidget(self._insert)
        buttons.addWidget(self._functions)
        buttons.addWidget(self._expand)
        buttons.addStretch(1)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(4)
        row.addWidget(self._edit, 1)
        row.addLayout(buttons)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addLayout(row)
        layout.addWidget(self.line)
        self.setLayout(layout)

    def insert_template(self, template: str) -> None:
        """Insert a function at the cursor (see functions_window.insert_into)."""
        functions_window.insert_into(self._edit, template)
        self._edit.window().activateWindow()
        self._edit.setFocus()

    def open_window(self) -> None:
        """Edit the formula in the formula window; Apply there applies it here."""
        if self.window_open is not None:
            self.window_open.raise_()
            return
        name = self.creates_name().strip()
        title = f"{self.param.label} for [{name}]" if name else self.param.label
        window = FormulaWindow(
            self.window(),
            self.value(),
            title=title,
            frame=lambda: self.ctx.input_frame(),
            no_data=lambda: self.ctx.input_problem(),
            earlier=self.earlier,
            condition=self._condition,
            name=name,
            creates=self.creates,
            parameters=lambda: self.ctx.parameters(),
        )
        window.applied.connect(self.apply_text)
        window.finished.connect(self._window_closed)
        self.window_open = window
        window.open()

    def apply_text(self, text: str) -> None:
        self._edit.setPlainText(text)
        self._commit()

    def _window_closed(self) -> None:
        self.window_open = None
        self._edit.setFocus()
        self._edit.moveCursor(self._edit.textCursor().MoveOperation.End)

    def apply(self) -> None:
        """Apply what is typed (as Enter does)."""
        self._commit()

    def _show_functions(self) -> None:
        functions_window.remember(self)
        functions_window.show_functions(self)

    def value(self) -> str:
        return self._edit.toPlainText()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._edit.setPlainText(self._last)
        self._edit.moveCursor(QTextCursor.MoveOperation.End)  # e.g. the [ ] button adds at the end

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._columns = ctx.columns("input", values) or []
        if self.window_open is not None:  # e.g. the data going into the step is calculated now
            self.window_open.inputs_changed()
        self.check()

    @property
    def checking(self) -> bool:
        return self._check_timer.isActive() or self._check.busy

    def check(self) -> None:
        """Check the formula on the first rows of the data going into the step, in the
        background; nothing happens if that was done already for the same formula and data."""
        self._check_timer.stop()
        if not self.isVisible():  # e.g. the settings on the map while the table view is shown
            return
        text = self.value()
        frame = self.ctx.input_frame()
        no_data = (self.ctx.input_problem() or NO_DATA) if frame is None else ""
        earlier, name, condition, creates = list(self.earlier()), self.creates_name(), self._condition, self.creates
        parameters = self.ctx.parameters()
        self._highlighter.set_parameters(parameters)
        rest = (text, tuple(earlier), name, no_data, tuple(parameters.items()))
        if self._checked_input is not None and self._checked_input[1] == rest:
            ref = self._checked_input[0]
            if (frame is None and ref is None) or (frame is not None and ref is not None and ref() is frame):
                return
        self._checked_input = (weakref.ref(frame) if frame is not None else None, rest)
        self._check.start(  # also for an empty formula: the columns it can use come with the result
            lambda cancel: preview(
                frame, text, earlier, condition=condition, name=name, creates=creates, cancel=cancel, no_data=no_data,
                parameters=parameters,
            )  # fmt: skip
        )

    @property
    def _condition(self) -> bool:
        return bool(getattr(self.param, "condition", False))

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.check()

    def _typed(self) -> None:
        self._render()  # greyed while it belongs to another text, as it was if the text is back
        self._check_timer.start()

    def _checked(self, result: Preview) -> None:
        self._result, self._result_text = result, self._checked_input[1][0] if self._checked_input else ""
        self._completer.refresh()  # the columns with their types (and the rows above) are known now
        if result.columns:  # the columns the formula can use: other [names] turn red
            self._highlighter.set_columns(c.name for c in result.columns)
        elif self._checked_input is not None and self._checked_input[0] is None:
            self._highlighter.set_columns(None)
        if result.ok and result.values is not None:
            plain, color = self._summary(result), theme.TEXT_DIM
        elif self._checked_input is not None and self._checked_input[0] is None:  # no data to check with
            plain, color = result.message, theme.TEXT_DIM
        else:
            plain, color = result.message, theme.STATUS_TEXT["error"]
        text = html.escape(plain, quote=False)
        if not result.ok and result.fix and result.mistake:
            text += f' · <a href="fix">Write {html.escape(result.fix, quote=False)}</a>'
        self._line = (text, color)
        self.line.setToolTip(plain + ("\n\nClick to show the mistake in the formula." if result.mistake else ""))
        self._render()

    def _check_failed(self, details: str) -> None:
        self._checked(Preview(False, "The check failed: " + details.strip().splitlines()[-1]))

    def _render(self) -> None:
        result = self._result
        if result is None or not self._result_text.strip() or (result.ok and result.values is None):  # nothing typed yet
            self.line.hide()
            self._underline(None)
            return
        current = self.value() == self._result_text
        text, color = self._line
        self.line.setStyleSheet(f"color: {color if current else theme.TEXT_DISABLED};")
        self.line.setText(text)
        self.line.show()
        self._underline(result.mistake if current else None)

    def _summary(self, result: Preview) -> str:
        """One line: what the formula gives, e.g. "→ number: 23.79, 119, 5.95, … · no errors in
        all 156 rows"."""
        values = result.values
        if self._condition:
            return "→ " + result.message[:1].lower() + result.message[1:]
        kind = type_label(values.dtype)
        if values.len() == 0:
            return f"→ {kind} · no rows to check"
        try:
            show = DisplayFormat().formatter(values.dtype)
            shown = []
            for value in values.head(3).to_list():
                if value is None:
                    shown.append("empty")
                elif values.dtype == pl.String:
                    shown.append("'" + (value if len(value) <= 24 else value[:24] + "…") + "'")
                else:
                    shown.append(show(value))
            first = ", ".join(shown) + (", …" if values.len() > 3 else "")
        except Exception:  # e.g. a date after the year 9999
            first = "(values out of range)"
        except BaseException as e:  # Polars panics on infinite dates
            if not is_panic(e):
                raise
            first = "(values out of range)"
        line = f"→ {kind}: {first} · no errors in {result.scope}"
        return line + (f" ({result.note})" if result.note else "")

    def _underline(self, mistake: tuple[int, int] | None) -> None:
        self._mistake = []
        if mistake is not None:
            selection = QTextEdit.ExtraSelection()
            selection.format.setUnderlineStyle(QTextCharFormat.UnderlineStyle.WaveUnderline)
            selection.format.setUnderlineColor(QColor(theme.STATUS_COLORS["error"]))
            selection.cursor = self._cursor(mistake)
            self._mistake = [selection]
        self._update_selections()

    def _update_selections(self) -> None:
        """The underlined mistake, and while typing the bracket matching the one at the cursor."""
        brackets = bracket_selections(self._edit) if self._edit.hasFocus() else []
        self._edit.setExtraSelections(self._mistake + brackets)

    def _column_suggestions(self) -> list[Suggestion]:
        result = self._result
        if result is not None and result.columns:  # with their types, and the rows above
            return column_suggestions([(c.label, c.type, c.row) for c in result.columns])
        frame = self.ctx.input_frame()  # before the first check: the input columns
        if frame is not None:
            return column_suggestions([(name, type_label(kind), None) for name, kind in frame.schema.items()])
        return column_suggestions([(name, "", None) for name in self._columns])

    def _open_columns(self) -> None:
        """The [ ] button: as when typing [ at the cursor."""
        self._edit.setFocus()
        self._edit.insertPlainText("[")
        self._completer.open()

    def _cursor(self, mistake: tuple[int, int]) -> QTextCursor:
        """A cursor that selects ``text[start:end]`` (Qt counts in UTF-16 units)."""
        text = self._result_text
        cursor = QTextCursor(self._edit.document())
        cursor.setPosition(_utf16(text, mistake[0]))
        cursor.setPosition(_utf16(text, mistake[1]), QTextCursor.MoveMode.KeepAnchor)
        return cursor

    def _show_mistake(self) -> None:
        result = self._result
        if result is None or result.mistake is None or self.value() != self._result_text:
            return
        self._edit.setTextCursor(self._cursor(result.mistake))
        self._edit.setFocus()

    def _fix_mistake(self) -> None:
        """Write the suggested name there instead; like typing, this doesn't apply the formula."""
        result = self._result
        if result is None or result.mistake is None or result.fix is None or self.value() != self._result_text:
            return
        cursor = self._cursor(result.mistake)
        cursor.insertText(result.fix)
        self._edit.setTextCursor(cursor)
        self._edit.setFocus()

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class IntEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._spin = _Spin(self)
        low, high = getattr(param, "min", None), getattr(param, "max", None)
        self._spin.setRange(-2_000_000_000 if low is None else int(low), 2_000_000_000 if high is None else int(high))
        self._spin.setKeyboardTracking(False)
        self._spin.valueChanged.connect(self.changed)
        self.setLayout(_row(self._spin, QWidget(self)))

    def value(self) -> int:
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        self._spin.blockSignals(True)
        try:
            self._spin.setValue(int(value or 0))
        except (TypeError, ValueError):
            self._spin.setValue(0)
        self._spin.blockSignals(False)


class FloatEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._spin = QDoubleSpinBox(self)
        self._spin.setDecimals(6)
        low, high = getattr(param, "min", None), getattr(param, "max", None)
        self._spin.setRange(-1e15 if low is None else float(low), 1e15 if high is None else float(high))
        self._spin.setKeyboardTracking(False)
        self._spin.valueChanged.connect(self.changed)
        self.setLayout(_row(self._spin, QWidget(self)))

    def value(self) -> float:
        return self._spin.value()

    def set_value(self, value: Any) -> None:
        self._spin.blockSignals(True)
        try:
            self._spin.setValue(float(value or 0))
        except (TypeError, ValueError):
            self._spin.setValue(0.0)
        self._spin.blockSignals(False)


class BoolEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._box = QCheckBox(param.label, self)
        self._box.toggled.connect(self.changed)
        self.setLayout(_row(self._box))

    def value(self) -> bool:
        return self._box.isChecked()

    def set_value(self, value: Any) -> None:
        self._box.blockSignals(True)
        self._box.setChecked(bool(value))
        self._box.blockSignals(False)


class ChoiceEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = _Combo(self)
        self._combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo.setMinimumContentsLength(8)
        for value, label in getattr(param, "options", ()):
            self._combo.addItem(label, value)
        self._combo.currentIndexChanged.connect(self.changed)
        self.setLayout(_row(self._combo))

    def value(self) -> Any:
        return self._combo.currentData()

    def set_value(self, value: Any) -> None:
        self._combo.blockSignals(True)
        index = self._combo.findData(value)
        if index < 0 and value is not None:
            self._combo.addItem(f"{value} (unknown)", value)
            index = self._combo.count() - 1
        self._combo.setCurrentIndex(max(index, 0))
        self._combo.blockSignals(False)
        self._combo.updateGeometry()  # a narrow one is as wide as its text


class ColumnEditor(Editor):
    """A column name: pick from the list or type it."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = _Combo(self)
        self._combo.setEditable(True)
        self._combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self._combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self._combo.setMinimumContentsLength(6)
        self._combo.lineEdit().setPlaceholderText("column")
        self._combo.activated.connect(lambda _: self._commit())
        self._combo.lineEdit().editingFinished.connect(self._commit)
        self._columns: list[str] | None = None
        self._last = ""
        self.setLayout(_row(self._combo))

    def value(self) -> str:
        return self._combo.currentText().strip()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._combo.blockSignals(True)
        self._combo.setCurrentText(self._last)
        self._combo.blockSignals(False)
        self._combo.updateGeometry()
        self._mark()

    def set_placeholder(self, text: str) -> None:
        self._combo.lineEdit().setPlaceholderText(text)

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        columns = ctx.columns(getattr(self.param, "source", "input"), values)
        if columns == self._columns:
            return
        self._columns = columns
        text = self._combo.currentText()
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItems(columns or [])
        self._combo.setCurrentText(text)
        self._combo.blockSignals(False)
        self._mark()

    def _commit(self) -> None:
        self._mark()
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()

    def _mark(self) -> None:
        missing = bool(self.value()) and self._columns is not None and self.value() not in self._columns
        _set_missing(self._combo, missing)
        self._combo.setToolTip("This column is not in the table" if missing else self.param.help)


class ColumnsEditor(Editor):
    """Several columns: tick them; drag to change the order."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._list = QListWidget(self)
        self._list.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self._list.setMaximumHeight(170)
        self._list.itemChanged.connect(lambda _: self.changed.emit())
        self._list.model().rowsMoved.connect(lambda *_: self.changed.emit())
        all_button, none_button = QPushButton("All", self), QPushButton("None", self)
        all_button.clicked.connect(lambda: self._check_all(True))
        none_button.clicked.connect(lambda: self._check_all(False))
        buttons = _row(all_button, none_button, stretch_first=False)
        buttons.addStretch(1)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        layout.addWidget(self._list)
        layout.addLayout(buttons)
        self._columns: list[str] | None = None

    def value(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def set_value(self, value: Any) -> None:
        self._rebuild(list(value or []))

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        columns = ctx.columns(getattr(self.param, "source", "input"), values)
        if columns != self._columns:
            self._columns = columns
            self._rebuild(self.value())

    def _rebuild(self, selected: list[str]) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        available = self._columns or []
        for name in [*selected, *(c for c in available if c not in selected)]:
            item = QListWidgetItem(name if self._columns is None or name in available else f"{name}  (missing)")
            item.setData(Qt.ItemDataRole.UserRole, name)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsDragEnabled)
            item.setCheckState(Qt.CheckState.Checked if name in selected else Qt.CheckState.Unchecked)
            if self._columns is not None and name not in available:
                item.setForeground(theme.status_text_color("error"))
            self._list.addItem(item)
        self._list.blockSignals(False)

    def _check_all(self, checked: bool) -> None:
        self._list.blockSignals(True)
        for i in range(self._list.count()):
            self._list.item(i).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
        self._list.blockSignals(False)
        self.changed.emit()


class PathEditor(Editor):
    """A file or folder; a path inside the project folder is stored relative to it."""

    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._edit = QLineEdit(self)
        self._edit.editingFinished.connect(self._commit)
        browse = QToolButton(self)
        browse.setText("…")
        browse.setToolTip("Browse")
        browse.clicked.connect(self._browse)
        self._last = ""
        self.setLayout(_row(self._edit, browse))

    def value(self) -> str:
        return self._edit.text().strip()

    def set_value(self, value: Any) -> None:
        self._last = "" if value is None else str(value)
        self._edit.setText(self._last)

    def _browse(self) -> None:
        start = self._absolute(self.value()) or str(self.ctx.project_dir or Path.home())
        if self.param.kind == "folder":
            chosen = QFileDialog.getExistingDirectory(self, self.param.label, start)
        elif getattr(self.param, "mode", "open") == "save":
            chosen, _ = QFileDialog.getSaveFileName(self, self.param.label, start, getattr(self.param, "filter", ""))
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, self.param.label, start, getattr(self.param, "filter", ""))
        if chosen:
            self._edit.setText(self._relative(chosen))
            self._commit()

    def _absolute(self, text: str) -> str:
        if not text:
            return ""
        path = Path(text)
        if not path.is_absolute() and self.ctx.project_dir is not None:
            path = self.ctx.project_dir / path
        return str(path)

    def _relative(self, chosen: str) -> str:
        if self.ctx.project_dir is None:
            return str(Path(chosen))
        try:
            relative = os.path.relpath(chosen, self.ctx.project_dir)
        except ValueError:
            return str(Path(chosen))
        return str(Path(chosen)) if relative.startswith("..") else Path(relative).as_posix()

    def _commit(self) -> None:
        if self.value() != self._last:
            self._last = self.value()
            self.changed.emit()


class TableEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = _Combo(self)
        self._combo.make_narrow()  # a long table name doesn't make the form wider than the panel
        self._combo.currentIndexChanged.connect(self.changed)
        self._value: str = ""
        self.setLayout(_row(self._combo))
        self._fill()

    def value(self) -> str:
        data = self._combo.currentData()
        return "" if data is None else str(data)

    def set_value(self, value: Any) -> None:
        self._value = "" if value is None else str(value)
        self._fill()

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._value = self.value()
        self._fill()

    def _fill(self) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("(choose a table)", "")
        for table_id, name in self.ctx.tables:
            self._combo.addItem(name, table_id)
        index = self._combo.findData(self._value)
        if index < 0 and self._value:
            self._combo.addItem(f"{self._value} (missing)", self._value)
            index = self._combo.count() - 1
        self._combo.setCurrentIndex(max(index, 0))
        self._combo.blockSignals(False)


class TablesEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._list = QListWidget(self)
        self._list.setMaximumHeight(140)
        self._list.itemChanged.connect(lambda _: self.changed.emit())
        self.setLayout(_row(self._list))
        self._selected: list[str] = []
        self._fill()

    def value(self) -> list[str]:
        return [
            self._list.item(i).data(Qt.ItemDataRole.UserRole)
            for i in range(self._list.count())
            if self._list.item(i).checkState() == Qt.CheckState.Checked
        ]

    def set_value(self, value: Any) -> None:
        self._selected = list(value or [])
        self._fill()

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._selected = self.value()
        self._fill()

    def _fill(self) -> None:
        self._list.blockSignals(True)
        self._list.clear()
        names = dict(self.ctx.tables)
        for table_id in [*self._selected, *(t for t in names if t not in self._selected)]:
            item = QListWidgetItem(names.get(table_id, f"{table_id} (missing)"))
            item.setData(Qt.ItemDataRole.UserRole, table_id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if table_id in self._selected else Qt.CheckState.Unchecked)
            self._list.addItem(item)
        self._list.blockSignals(False)


class ConnectionEditor(Editor):
    def __init__(self, param: Param, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._combo = _Combo(self)
        self._combo.make_narrow()
        self._combo.currentIndexChanged.connect(self.changed)
        edit = QToolButton(self)
        edit.setText("Edit…")
        edit.setToolTip("Define or change database connections")
        edit.clicked.connect(lambda: self.ctx.edit_connections and self.ctx.edit_connections())
        self._value = ""
        self.setLayout(_row(self._combo, edit))
        self._fill()

    def value(self) -> str:
        data = self._combo.currentData()
        return "" if data is None else str(data)

    def set_value(self, value: Any) -> None:
        self._value = "" if value is None else str(value)
        self._fill()

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._value = self.value()
        self._fill()

    def _fill(self) -> None:
        self._combo.blockSignals(True)
        self._combo.clear()
        self._combo.addItem("(choose a connection)", "")
        for name in self.ctx.connections:
            self._combo.addItem(name, name)
        index = self._combo.findData(self._value)
        if index < 0 and self._value:
            self._combo.addItem(f"{self._value} (missing)", self._value)
            index = self._combo.count() - 1
        self._combo.setCurrentIndex(max(index, 0))
        self._combo.blockSignals(False)


class ListEditor(Editor):
    """Rows of sub-settings (sort keys, renames, formulas, ...) with add / move / remove.

    Rows form a compact grid with one column per field. When a field holds a formula, each row
    becomes a small card instead: the short fields in one line, the formula below at full width.
    """

    def __init__(self, param: List, ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(param, ctx, parent)
        self._fields = list(param.fields)
        self._wide = [f for f in self._fields if f.kind in ("expression", "code")]
        self._rows: list[dict[str, Editor]] = []
        self._row_widgets: list[list[QWidget]] = []
        self._labels: dict[str, QLabel] = {}  # the header: field key -> label
        self._values: dict[str, Any] = {}
        self._grid = QGridLayout()
        self._grid.setContentsMargins(0, 0, 0, 0)
        self._grid.setHorizontalSpacing(4)
        self._grid.setVerticalSpacing(4)
        if not self._wide:  # a choice is as wide as its text, the other fields share the rest
            for column, f in enumerate(self._fields):
                self._grid.setColumnStretch(column, _stretch(f))
        add = QPushButton("+ Add", self)
        add.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
        add.clicked.connect(self._add_clicked)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        layout.addLayout(self._grid)
        layout.addWidget(add)
        if not self._wide:
            self._header()

    def value(self) -> list[dict[str, Any]]:
        return [{key: editor.value() for key, editor in row.items()} for row in self._rows]

    def set_value(self, value: Any) -> None:
        self._rebuild(list(value or []))

    def refresh(self, ctx: FormContext, values: dict[str, Any]) -> None:
        super().refresh(ctx, values)
        self._values = values
        for row in self._rows:
            for editor in row.values():
                editor.refresh(ctx, values)
        self._update_visibility()

    def _header(self) -> None:
        if len(self._fields) < 2:
            return
        for column, f in enumerate(self._fields):
            label = _ShortLabel(f.label, self)
            label.setObjectName("Dim")
            self._grid.addWidget(label, 0, column)
            self._labels[f.key] = label

    def _rebuild(self, rows: list[dict[str, Any]]) -> None:
        for widgets in self._row_widgets:
            for widget in widgets:
                self._grid.removeWidget(widget)
                widget.deleteLater()
        self._rows, self._row_widgets = [], []
        for values in rows:
            self._add_row(values)
        self._update_visibility()

    def _add_row(self, values: dict[str, Any]) -> None:
        number = len(self._rows)
        editors: dict[str, Editor] = {}
        for f in self._fields:
            editor = create_editor(f, self.ctx, self)
            editor.allow_narrow()
            editor.refresh(self.ctx, self._values)
            editor.set_value(_shown(f, values.get(f.key, f.initial())))
            editor.changed.connect(self._child_changed)
            editors[f.key] = editor
        for f in self._fields:  # a formula that creates a column: later rows can use it
            creates, editor = getattr(f, "creates", ""), editors[f.key]
            if creates in editors and isinstance(editor, ExpressionEditor):
                editor.earlier = lambda n=number, f=f: [(r[f.creates].value(), r[f.key].value()) for r in self._rows[:n]]
                editor.creates_name = editors[creates].value
                editor.creates = True
        tools = QWidget(self)
        buttons = _row(
            _tool(tools, "▲", "Move up", lambda: self._move(number, -1)),
            _tool(tools, "▼", "Move down", lambda: self._move(number, 1)),
            _tool(tools, "✕", "Remove", lambda: self._remove(number)),
            stretch_first=False,
        )
        buttons.setSpacing(0)
        tools.setLayout(buttons)
        if self._wide:
            card = QFrame(self)
            card.setObjectName("ListCard")
            grid = QGridLayout(card)
            grid.setContentsMargins(6, 6, 6, 6)
            grid.setSpacing(4)
            line = [f for f in self._fields if f not in self._wide]
            for column, f in enumerate(line):
                editors[f.key].set_placeholder(f.label)
                grid.addWidget(editors[f.key], 0, column)
                grid.setColumnStretch(column, 1)
            grid.addWidget(tools, 0, len(line))
            for offset, f in enumerate(self._wide, start=1):
                grid.addWidget(editors[f.key], offset, 0, 1, len(line) + 1)
            self._grid.addWidget(card, number, 0)
            widgets: list[QWidget] = [card]
        else:
            for column, f in enumerate(self._fields):
                self._grid.addWidget(editors[f.key], number + 1, column)
            self._grid.addWidget(tools, number + 1, len(self._fields))
            widgets = [*editors.values(), tools]
        self._rows.append(editors)
        self._row_widgets.append(widgets)

    def _add_clicked(self) -> None:
        rows = self.value()
        rows.append({f.key: f.initial() for f in self._fields})
        self._rebuild(rows)
        self.changed.emit()

    def _remove(self, number: int) -> None:
        rows = self.value()
        del rows[number]
        self._rebuild(rows)
        self.changed.emit()

    def _move(self, number: int, delta: int) -> None:
        rows = self.value()
        target = number + delta
        if 0 <= target < len(rows):
            rows.insert(target, rows.pop(number))
            self._rebuild(rows)
            self.changed.emit()

    def row_editor(self, row: int, key: str) -> Editor | None:
        """The editor of field ``key`` in row ``row`` (from 1)."""
        return self._rows[row - 1].get(key) if 1 <= row <= len(self._rows) else None

    def _child_changed(self) -> None:
        self._update_visibility()
        self.changed.emit()

    def _update_visibility(self) -> None:
        """Each row shows its fields; the header names those that a row shows (none without rows).
        The room of a field that no row shows stays where it is: the field before it gets it if
        that one grows with the row, else it stays empty. So the fields before it keep their place,
        and a choice that hides or shows a field stays under the mouse."""
        shown: set[str] = set()
        for row in self._rows:
            values = {key: editor.value() for key, editor in row.items()}
            for f in self._fields:
                visible = is_visible(f, values)
                row[f.key].setVisible(visible)
                if visible:
                    shown.add(f.key)
        for key, label in self._labels.items():
            label.setVisible(key in shown)
        if not self._wide:
            stretch = [_stretch(f) for f in self._fields]
            for column in range(len(stretch) - 1, 0, -1):
                if self._fields[column].key not in shown and stretch[column - 1]:
                    stretch[column - 1] += stretch[column]
                    stretch[column] = 0
            for column, factor in enumerate(stretch):
                self._grid.setColumnStretch(column, factor)


def _stretch(field_: Param) -> int:
    """How a field of a list row shares the room of the row: a choice is as wide as its text."""
    return 2 if field_.kind == "text" else 0 if field_.kind == "choice" else 1


class _ShortLabel(QLabel):
    """A label that can be narrower than its text: then it ends with "…", and the tooltip has
    all of it."""

    def __init__(self, text: str, parent: QWidget) -> None:
        super().__init__(text, parent)
        self._full = text
        self.setToolTip(text)
        self.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)

    def minimumSizeHint(self) -> Any:
        hint = super().minimumSizeHint()
        hint.setWidth(0)
        return hint

    def sizeHint(self) -> Any:
        hint = super().sizeHint()
        hint.setWidth(self.fontMetrics().horizontalAdvance(self._full) + 4)
        return hint

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        shown = self.fontMetrics().elidedText(self._full, Qt.TextElideMode.ElideRight, self.contentsRect().width())
        if shown != self.text():
            self.setText(shown)


def _tool(parent: QWidget, text: str, tip: str, slot: Callable[[], None]) -> QToolButton:
    button = QToolButton(parent)
    button.setText(text)
    button.setToolTip(tip)
    button.setAutoRaise(True)
    button.setFixedWidth(button.fontMetrics().horizontalAdvance(text) + 10)  # narrow: rows fit a narrow panel
    button.clicked.connect(slot)
    return button


def _set_missing(widget: QWidget, missing: bool) -> None:
    if widget.property("missing") != missing:
        widget.setProperty("missing", missing)
        widget.style().unpolish(widget)
        widget.style().polish(widget)


def flash(widgets: Iterable[QWidget], msecs: int = 1500) -> None:
    """Mark widgets for a moment (dynamic property "found", styled in theme.STYLE_SHEET)."""
    marked = [widget for widget in widgets if widget is not None]
    for widget in marked:
        _set_found(widget, True)

    def clear() -> None:
        for widget in marked:
            if shiboken6.isValid(widget):  # a form built again meanwhile deleted it
                _set_found(widget, False)

    QTimer.singleShot(msecs, clear)


def _set_found(widget: QWidget, found: bool) -> None:
    widget.setProperty("found", found)
    widget.style().unpolish(widget)
    widget.style().polish(widget)
    widget.update()


_EDITORS: dict[str, type[Editor]] = {
    "text": TextEditor,
    "code": CodeEditor,
    "expression": ExpressionEditor,
    "int": IntEditor,
    "float": FloatEditor,
    "bool": BoolEditor,
    "choice": ChoiceEditor,
    "column": ColumnEditor,
    "columns": ColumnsEditor,
    "file": PathEditor,
    "folder": PathEditor,
    "table": TableEditor,
    "tables": TablesEditor,
    "connection": ConnectionEditor,
    "list": ListEditor,
}


def create_editor(param: Param, ctx: FormContext, parent: QWidget | None = None) -> Editor:
    """The editor for a parameter; unknown kinds (from newer plugins) get a text field."""
    return _EDITORS.get(param.kind, TextEditor)(param, ctx, parent)


class ParamForm(QWidget):
    """All parameters of an action, label above field, in declaration order."""

    changed = Signal(dict)

    def __init__(self, spec: Sequence[Param], ctx: FormContext, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self._rows: list[tuple[Param, QLabel | None, Editor, QLabel | None]] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        for param in spec:
            label = None
            if param.kind != "bool":
                label = QLabel(param.label + (" *" if param.required else ""), self)
                label.setWordWrap(True)  # a long label doesn't make the form wider than the panel
                label.setToolTip(param.help)
                layout.addSpacing(4)
                layout.addWidget(label)
            editor = create_editor(param, ctx, self)
            editor.changed.connect(self._on_changed)
            layout.addWidget(editor)
            hint = None
            if param.help:
                hint = QLabel(param.help, self)
                hint.setObjectName("Dim")
                hint.setWordWrap(True)
                layout.addWidget(hint)
            self._rows.append((param, label, editor, hint))
        layout.addStretch(1)

    def set_values(self, values: dict[str, Any]) -> None:
        values = dict(values) | {param.key: _shown(param, values.get(param.key)) for param, *_ in self._rows}
        for param, _, editor, _ in self._rows:
            editor.refresh(self.ctx, values)
            editor.set_value(values.get(param.key))
        self._update_visibility(values)

    def values(self) -> dict[str, Any]:
        return {param.key: editor.value() for param, _, editor, _ in self._rows}

    def refresh(self, ctx: FormContext) -> None:
        self.ctx = ctx
        values = self.values()
        for _, _, editor, _ in self._rows:
            editor.refresh(ctx, values)

    def editor(self, key: str) -> Editor:
        return next(editor for param, _, editor, _ in self._rows if param.key == key)

    def focus(self, key: str, row: int = 0, field: str = "") -> QWidget | None:
        """Focus the editor of setting ``key`` (in a list: of ``field`` in ``row``) and flash it and
        its label. None when the setting isn't there or isn't shown."""
        found = next(((label, editor) for param, label, editor, _ in self._rows if param.key == key), None)
        if found is None or found[1].isHidden():
            return None
        label, editor = found
        target: Editor = editor
        if isinstance(editor, ListEditor):  # a field of a row; else the first field shown in the row
            row = max(row, 1)
            shown = editor.row_editor(row, field)
            if (shown is None or shown.isHidden()) and row <= len(editor._rows):
                shown = next((e for e in editor._rows[row - 1].values() if not e.isHidden()), None)
            target = shown or editor
        widget = target.focus_input()
        flash([widget] if label is None else [widget, label])
        return widget

    def _on_changed(self) -> None:
        values = self.values()
        self._update_visibility(values)
        for _, _, editor, _ in self._rows:
            editor.refresh(self.ctx, values)
        self.changed.emit(values)

    def _update_visibility(self, values: dict[str, Any]) -> None:
        for param, label, editor, hint in self._rows:
            visible = is_visible(param, values)
            for widget in (label, editor, hint):
                if widget is not None:
                    widget.setVisible(visible)


def _utf16(text: str, index: int) -> int:
    """The position of ``text[index]`` for Qt, which counts characters outside the BMP twice."""
    return len(text[:index].encode("utf-16-le")) // 2

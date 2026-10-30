"""The settings of the selected step, built from the action's parameters."""

from __future__ import annotations

from collections.abc import Callable

import shiboken6
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QCheckBox, QLabel, QLineEdit, QPushButton, QScrollArea, QVBoxLayout, QWidget

from tafl.api import Kind, files
from tafl.core import Status
from tafl.core.index import NOTE

from .forms import FormContext, ParamForm, flash
from .labels import ElidedLabel, WrappedLabel
from .session import Session, first_error, json_ready, missing_settings, waiting_text

_KIND_TEXT = {
    Kind.SOURCE: "first step of a table",
    Kind.TRANSFORM: "",
    Kind.EXPORT: "runs when the project is run",
    Kind.COMMAND: "runs when the project is run",
}


class SettingsPanel(QWidget):
    def __init__(self, session: Session, edit_connections: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self._edit_connections = edit_connections
        self._form: ParamForm | None = None
        self._form_key: tuple[str, str] | None = None
        self._form_params: dict = {}  # the step settings the form currently shows
        self._note_step: str | None = None  # the step whose note the note field shows

        heading = QLabel("Step settings", self)
        heading.setObjectName("PanelTitle")
        self.title = WrappedLabel(parent=self)
        self.title.setObjectName("Title")
        self.subtitle = ElidedLabel(self)
        self.subtitle.setObjectName("Dim")
        self.error = _box("ErrorBox", self)
        self.warning = _box("WarningBox", self)
        self.info = _box("InfoBox", self)
        self.enabled = QCheckBox("Step is on", self)
        self.enabled.toggled.connect(self._enabled_toggled)
        self.sheet_button = QPushButton("Edit in sheet", self)
        self.sheet_button.setToolTip(
            "Open the file of this import in a sheet: the data as it is in the file, not after later steps. "
            "Save writes the file back, and the table is calculated again."
        )
        self.sheet_button.clicked.connect(self._edit_in_sheet)
        self.sheet_button.hide()

        self._form_host = QVBoxLayout()
        self._form_host.setContentsMargins(0, 0, 0, 0)
        self.description = QLabel(self)
        self.description.setObjectName("Dim")
        self.description.setWordWrap(True)
        self.note = QLineEdit(self)
        self.note.setPlaceholderText("Note (shown in the step list)")
        self.note.editingFinished.connect(self._note_changed)

        content = QWidget(self)
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(0, 0, 8, 0)
        content_layout.setSpacing(6)
        content_layout.addWidget(self.enabled)
        content_layout.addWidget(self.sheet_button, 0, Qt.AlignmentFlag.AlignLeft)
        content_layout.addLayout(self._form_host)
        content_layout.addSpacing(8)
        content_layout.addWidget(self.note)
        content_layout.addWidget(self.description)
        content_layout.addStretch(1)
        scroll = QScrollArea(self)
        scroll.setWidgetResizable(True)
        scroll.setWidget(content)
        # Forms shrink to the panel's width; a panel narrower than a form can be scrolled.
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll = scroll

        layout = QVBoxLayout(self)
        layout.setContentsMargins(3, 0, 6, 6)
        layout.setSpacing(5)
        layout.addWidget(heading)
        layout.addWidget(self.title)
        layout.addWidget(self.subtitle)
        layout.addWidget(self.error)
        layout.addWidget(self.warning)
        layout.addWidget(self.info)
        layout.addWidget(scroll, 1)

        session.project_changed.connect(self._project_changed)  # emitted first: before the refresh
        session.selection_changed.connect(self.refresh)
        session.structure_changed.connect(self.refresh)
        session.step_changed.connect(lambda _: self.refresh())
        session.results_changed.connect(self.refresh)
        self.refresh()

    @property
    def form(self) -> ParamForm | None:
        return self._form

    def refresh(self) -> None:
        step = self.session.step
        action = self.session.action(step)
        if step is None:
            self._clear("Nothing selected", "Select a step to see its settings.")
            return
        if action is None:
            self._clear(f"Unknown action: {step.action}", "The plugin that provides this action is not installed. The step is kept as it is.")
            return
        key = (step.id, step.action)
        context = self._context()
        values = json_ready(action.resolve(step.params))
        typed = None  # what was typed into the form of another step and not applied yet
        if key != self._form_key:
            typed = self._drop_form()
            self._build_form(action.params, context, values)
            self._form_key = key
        else:
            self._form.refresh(context)
            if _settings(values) != _settings(self._form_params):  # changed elsewhere, e.g. by undo; never while typing
                self._form.set_values(values)
                self._form_params = values
        self.title.setText(action.name)
        details = [action.category, _KIND_TEXT[action.kind]]
        self.subtitle.setText("  ·  ".join(d for d in details if d))
        self.description.setText(_reflow(action.description))
        self.enabled.blockSignals(True)
        self.enabled.setChecked(step.enabled)
        self.enabled.setVisible(True)
        self.enabled.blockSignals(False)
        self.sheet_button.setVisible(self.session.sheet_format(step) is not None)
        # The note field shows the step's note, unless it holds typing that is not applied yet
        # ("modified": reset when it is applied). Also while it has the focus: after Undo took
        # back the typing just applied, it would else show that typing and apply it again.
        typed_note = None
        if step.id != self._note_step:  # another step: a note typed for the one before is its own
            if self.note.isModified() and self._note_step is not None:
                typed_note = self._note_step, self.note.text().strip()
            self._note_step = step.id
            self.note.setText(step.note)  # not modified now: leaving the field applies nothing
            if self.note.hasFocus():
                # The focus stays in the panel, as for the form's fields: typing on would else go
                # into the note of this step
                self.scroll.setFocus(Qt.FocusReason.OtherFocusReason)
        elif not self.note.isModified() and self.note.text() != step.note:
            # With the focus in it, the spaces typed at its ends stay (they are not part of the note)
            if not self.note.hasFocus() or self.note.text().strip() != step.note.strip():
                self.note.setText(step.note)
        self.note.setVisible(True)
        self._show_result()
        # Last: applying it changes that step, and the panel is refreshed again
        if typed is not None:
            self._apply_to(*typed)
        if typed_note is not None:
            self._note_to(*typed_note)

    def _project_changed(self) -> None:
        """Another project: what was typed for a step of the one before and not applied yet is
        dropped. Step ids repeat across projects: it would else go to a step of this one. (Undo
        and redo keep the project: they don't come here.)"""
        self._drop_form()
        self._form_key = None
        self._note_step = None

    def _drop_form(self) -> tuple[str, dict] | None:
        """Remove the form. What was typed into one of its fields and not applied yet (the field
        had the keyboard focus, e.g. the selection changed in code) is returned with its step's id:
        it belongs to that step, not to the step shown now. Only the settings typed in: the other
        fields may show settings that are out of date (an undo has just changed them)."""
        form, self._form = self._form, None
        if form is None:
            return None
        typed: list[dict] = []
        form.changed.disconnect(self._form_changed)
        form.changed.connect(typed.append)
        focus = QApplication.focusWidget()
        if focus is not None and form.isAncestorOf(focus):
            # The focus stays in the panel: Qt would move it on, e.g. to the tables, where typing
            # selects another table. Leaving the field applies what is typed in it now.
            self.scroll.setFocus(Qt.FocusReason.OtherFocusReason)
        # Hidden first: a form made a moment ago would else be shown as a window of its own (its
        # layout shows it later), and take the keyboard focus.
        form.hide()
        form.setParent(None)
        form.deleteLater()
        step_id = self._form_key[0] if self._form_key is not None else None
        if not typed or step_id is None:
            return None
        values = {key: value for key, value in typed[-1].items() if json_ready(value) != self._form_params.get(key)}
        return (step_id, values) if values else None

    def _apply_to(self, step_id: str, values: dict) -> None:
        """Settings typed for a step that is no longer shown (if it is still there)."""
        try:
            self.session.find_step(step_id)
        except KeyError:
            return
        self.session.set_params(step_id, values)

    def _note_to(self, step_id: str, note: str) -> None:
        try:
            self.session.find_step(step_id)
        except KeyError:
            return
        self.session.set_note(step_id, note)

    def _build_form(self, spec, context: FormContext, values: dict) -> None:
        self._form = ParamForm(spec, context, self)
        self._form.set_values(values)
        self._form_params = values
        self._form.changed.connect(self._form_changed)
        self._form_host.addWidget(self._form)

    def _show_result(self) -> None:
        result = self.session.step_result(self.session.step)
        error = warning = info = ""
        if result is not None:
            failed = first_error(self.session.result)
            waiting = self.session.waits_for(self.session.table_id) if failed is not None else None
            if result.status == Status.ERROR and (needed := missing_settings(result)):
                info = f"Not complete yet: fill in {', '.join(needed)}."
            elif result.status == Status.ERROR and waiting is not None:
                info = f"This step {waiting_text(waiting)} (fill in {', '.join(waiting[2])})."
            elif result.status == Status.ERROR:
                error = result.error or "Error"
            elif result.status == Status.BLOCKED and failed is not None and missing_settings(failed[1]):
                info = f"Step {failed[0]} is not complete yet: this step is calculated when it is."
            elif result.status == Status.BLOCKED and failed is not None and waiting is not None:
                info = f"Step {failed[0]} {waiting_text(waiting)}: this step is calculated when it is."
            elif result.status == Status.BLOCKED:
                error = "An earlier step has an error."
            elif result.status == Status.SKIPPED and result.skip_rest:
                info = f"This step is skipped: {result.skip_reason}."
            elif result.status == Status.SKIPPED:
                info = "This step runs when you run the project (F5). Nothing is written while you edit."
            elif result.skip_rest:
                info = f"The steps after this one are skipped: {result.skip_reason}."
            if not info and result.status == Status.OK:  # what the step reported, e.g. how many rows matched
                notes = [text for level, text in result.messages if level == "info" and text != result.skip_rest]
                info = "\n".join(notes[:5]) + ("\n…" if len(notes) > 5 else "")
            warning = "\n".join(text for level, text in result.messages if level == "warning")
        for box, text in ((self.error, error), (self.warning, warning), (self.info, info)):
            box.setText(text)
            box.setVisible(bool(text))

    def focus_setting(self, key: str, row: int = 0, field: str = "") -> bool:
        """Move the keyboard focus to a setting of the selected step and mark it for a moment (Enter in
        the Find panel). ``key``: a setting's key, or index.NOTE for the note; ``row`` (from 1) and
        ``field``: a field in a row of a list setting. False when the step has no such setting shown."""
        if key == NOTE:
            if self.note.isHidden():
                return False
            self.note.setFocus(Qt.FocusReason.OtherFocusReason)
            self.note.selectAll()
            flash([self.note])
            widget: QWidget = self.note
        elif key.startswith("#") or self._form is None:
            return False
        else:
            found = self._form.focus(key, row, field)
            if found is None:
                return False
            widget = found

        def show() -> None:  # after the layout has placed it
            if shiboken6.isValid(widget):
                self.scroll.ensureWidgetVisible(widget, 0, 40)

        QTimer.singleShot(0, self.scroll, show)
        return True

    def _context(self) -> FormContext:
        session = self.session
        step = session.step
        input_columns = session.input_columns(step)

        def columns(source: str, values: dict) -> list[str] | None:
            if source == "input":
                return input_columns
            table_id = values.get(source)
            return session.table_columns(table_id) if table_id else None

        return FormContext(
            columns=columns,
            input_frame=lambda: session.input_frame(step),
            input_problem=lambda: session.input_problem(step),
            tables=[(t.id, t.name) for t in session.project.tables if t.id != session.table_id],
            connections=[c.name for c in session.project.connections],
            project_dir=session.project.directory,
            edit_connections=self._edit_connections,
            parameters=session.parameter_values,
        )

    def _clear(self, title: str, text: str) -> None:
        typed = self._drop_form()
        self._form_key = None
        self.title.setText(title)
        self.subtitle.setText("")
        self.description.setText(text)
        self.enabled.setVisible(False)
        self.sheet_button.setVisible(False)
        self.note.setVisible(False)
        for box in (self.error, self.warning, self.info):
            box.hide()
        if typed is not None:
            self._apply_to(*typed)

    def _form_changed(self, values: dict) -> None:
        """The form's settings changed: they belong to the form's step (which is the selected
        step, unless the selection changed while a field was being typed in)."""
        if self._form_key is None:
            return
        try:
            _, step = self.session.find_step(self._form_key[0])
        except KeyError:
            return
        action = self.session.action(step)
        if action is not None:
            # Remember what the form shows first: set_params triggers refresh() right away.
            self._form_params = json_ready(action.resolve({**step.params, **values}))
            self.session.set_params(step.id, values)

    def _enabled_toggled(self, checked: bool) -> None:
        step = self.session.step
        if step is not None:
            self.session.set_enabled(step.id, checked)

    def _edit_in_sheet(self) -> None:
        step = self.session.step
        if step is not None:
            self.session.want_sheet_edit(step.id)

    def _note_changed(self) -> None:
        """Enter, or the note field was left: what is typed in it goes to its step."""
        if self._note_step is not None and self.note.isModified():
            # set_note refreshes the panel right away: the field is still "modified" then, so it
            # keeps what is typed (the field may be left only for a moment, e.g. by a save)
            self._note_to(self._note_step, self.note.text().strip())
            self.note.setModified(False)


def _settings(values: dict) -> dict:
    """What the form shows: not the mark of a data sheet, which changes when the file is saved."""
    return {key: value for key, value in values.items() if key != files.SHEET_KEY}


def _reflow(text: str) -> str:
    """Docstrings wrap at ~100 characters; join their lines so the label can wrap by itself."""
    paragraphs = [" ".join(line.strip() for line in p.splitlines()) for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def _box(name: str, parent: QWidget) -> QLabel:
    label = WrappedLabel(parent=parent)  # a long path in a message can't make the panel wider
    label.setObjectName(name)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.hide()
    return label

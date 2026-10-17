"""The settings of the selected step, built from the action's parameters."""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QCheckBox, QLabel, QLineEdit, QScrollArea, QVBoxLayout, QWidget

from tafl.api import Kind
from tafl.core import Status

from .forms import FormContext, ParamForm
from .session import Session, first_error, json_ready, missing_settings

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

        heading = QLabel("Step settings", self)
        heading.setObjectName("PanelTitle")
        self.title = QLabel(self)
        self.title.setObjectName("Title")
        self.title.setWordWrap(True)
        self.subtitle = QLabel(self)
        self.subtitle.setObjectName("Dim")
        self.error = _box("ErrorBox", self)
        self.warning = _box("WarningBox", self)
        self.info = _box("InfoBox", self)
        self.enabled = QCheckBox("Step is on", self)
        self.enabled.toggled.connect(self._enabled_toggled)

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
        if key != self._form_key:
            self._build_form(action.params, context, values)
            self._form_key = key
        else:
            self._form.refresh(context)
            if values != self._form_params:  # changed elsewhere, e.g. by undo; never while typing
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
        if self.note.text() != step.note and not self.note.hasFocus():
            self.note.setText(step.note)
        self.note.setVisible(True)
        self._show_result()

    def _build_form(self, spec, context: FormContext, values: dict) -> None:
        if self._form is not None:
            self._form.setParent(None)
            self._form.deleteLater()
        self._form = ParamForm(spec, context, self)
        self._form.set_values(values)
        self._form_params = values
        self._form.changed.connect(self._form_changed)
        self._form_host.addWidget(self._form)

    def _show_result(self) -> None:
        result = self.session.step_result(self.session.step)
        error = warning = info = ""
        if result is not None:
            if result.status == Status.ERROR and (needed := missing_settings(result)):
                info = f"Not complete yet: fill in {', '.join(needed)}."
            elif result.status == Status.ERROR:
                error = result.error or "Error"
            elif result.status == Status.BLOCKED and (failed := first_error(self.session.result)) and missing_settings(failed[1]):
                info = f"Step {failed[0]} is not complete yet: this step is calculated when it is."
            elif result.status == Status.BLOCKED:
                error = "An earlier step has an error."
            elif result.status == Status.SKIPPED and result.skip_rest:
                info = f"This step is skipped: {result.skip_reason}."
            elif result.status == Status.SKIPPED:
                info = "This step runs when you run the project (F5). Nothing is written while you edit."
            elif result.skip_rest:
                info = f"The steps after this one are skipped: {result.skip_reason}."
            warning = "\n".join(text for level, text in result.messages if level == "warning")
        for box, text in ((self.error, error), (self.warning, warning), (self.info, info)):
            box.setText(text)
            box.setVisible(bool(text))

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
        if self._form is not None:
            self._form.setParent(None)
            self._form.deleteLater()
            self._form = None
        self._form_key = None
        self.title.setText(title)
        self.subtitle.setText("")
        self.description.setText(text)
        self.enabled.setVisible(False)
        self.note.setVisible(False)
        for box in (self.error, self.warning, self.info):
            box.hide()

    def _form_changed(self, values: dict) -> None:
        step = self.session.step
        action = self.session.action(step)
        if step is not None and action is not None:
            # Remember what the form shows first: set_params triggers refresh() right away.
            self._form_params = json_ready(action.resolve({**step.params, **values}))
            self.session.set_params(step.id, values)

    def _enabled_toggled(self, checked: bool) -> None:
        step = self.session.step
        if step is not None:
            self.session.set_enabled(step.id, checked)

    def _note_changed(self) -> None:
        step = self.session.step
        if step is not None:
            self.session.set_note(step.id, self.note.text().strip())


def _reflow(text: str) -> str:
    """Docstrings wrap at ~100 characters; join their lines so the label can wrap by itself."""
    paragraphs = [" ".join(line.strip() for line in p.splitlines()) for p in text.split("\n\n")]
    return "\n\n".join(p for p in paragraphs if p)


def _box(name: str, parent: QWidget) -> QLabel:
    label = QLabel(parent)
    label.setObjectName(name)
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.hide()
    return label

"""The open project, the current selection and the latest results.

All edits go through the session: it records an undo step, marks the project as changed, tells
the panels to update and schedules a recalculation in the background.
"""

from __future__ import annotations

import copy
import os
import re
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import polars as pl
from PySide6.QtCore import QObject, QTimer, Signal

from tafl.api import Action, Mode
from tafl.api.parameters import PLACE
from tafl.api.params import List, Param
from tafl.core import (
    Connection,
    Engine,
    Group,
    Parameter,
    Project,
    Registry,
    Status,
    Step,
    StepResult,
    Table,
    TableResult,
    parameters,
)

from .theme import GROUP_COLORS
from .worker import Worker

_DEFAULT_TABLE_NAME = re.compile(r"Table \d+")
_UNDO_LIMIT = 100
_KEY_NAME = re.compile(r"(?i)(id|nr|no|number|key|code|nummer)$")
_REQUIRED = ": required"  # how the check of a setting says that it is empty (tafl.api.params)

# How one table can use another, as created from the project map: kind -> (action, preset).
LINK_KINDS: dict[str, tuple[str, dict[str, Any]]] = {
    "lookup": ("table.merge", {"how": "lookup"}),
    "left": ("table.merge", {"how": "left"}),
    "inner": ("table.merge", {"how": "inner"}),
    "full": ("table.merge", {"how": "full"}),
    "append": ("table.append", {}),
    "matching": ("table.merge", {"how": "matching"}),
    "non_matching": ("table.merge", {"how": "non_matching"}),
}


class Session(QObject):
    project_changed = Signal()  # another project was opened or created
    structure_changed = Signal()  # tables or steps were added, removed, renamed or moved
    step_changed = Signal(str)  # the settings (or on/off) of a step changed: step id
    selection_changed = Signal()
    results_changed = Signal()
    busy_changed = Signal(bool)
    run_busy_changed = Signal(bool)
    dirty_changed = Signal(bool)
    run_finished = Signal(object)  # {table id: TableResult}
    run_stopped = Signal()  # a run was stopped before it finished (no results)
    undo_changed = Signal()
    layout_changed = Signal()  # table positions on the project map changed
    filters_changed = Signal(str)  # the instant filters of a table changed: table id
    error = Signal(str)  # unexpected problems (tracebacks)

    def __init__(self, registry: Registry, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.registry = registry
        self.project = Project()
        self.engine = Engine(self.project, registry)
        self.results: dict[str, TableResult] = {}
        self.table_id: str | None = None
        self.step_id: str | None = None
        self.dirty = False
        self.overview = False  # True while the project map is shown: calculate every table
        self._undo: list[dict[str, Any]] = []
        self._redo: list[dict[str, Any]] = []
        self._stamp: tuple[int, int] | None = None  # the project file as it was opened or saved
        self._stopping = False
        self.run_seconds: float | None = None  # how long the last run took

        self._worker = Worker(self)
        self._worker.finished.connect(self._on_results)
        self._worker.failed.connect(self.error)
        self._worker.busy_changed.connect(self.busy_changed)
        self._runner = Worker(self)
        self._runner.finished.connect(self._on_run_finished)
        self._runner.failed.connect(self.error)
        self._runner.cancelled.connect(self.run_stopped)
        self._runner.busy_changed.connect(self.run_busy_changed)
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self.calculate_now)

    # --- files -------------------------------------------------------------------------------

    def new_project(self) -> None:
        self._set_project(Project())

    def open_project(self, path: str | Path) -> list[Step]:
        """Open a project; returns the steps that were upgraded to newer action versions."""
        project = Project.load(path)
        stamp = _stamp(project.path)
        self._set_project(project)
        self._stamp = stamp
        upgraded = self.engine.upgrade_steps()
        if upgraded:
            self._set_dirty(True)
        return upgraded

    def save_project(self, path: str | Path | None = None) -> Path:
        target = Path(path).resolve() if path is not None else self.project.path
        if target is None:
            raise ValueError("No file name")
        old_dir = self.project.directory
        if old_dir != target.parent:
            self._rebase_paths(old_dir, target.parent)
        saved = self.project.save(target)
        self._stamp = _stamp(saved)
        self._set_dirty(False)
        self.structure_changed.emit()
        return saved

    def changed_on_disk(self, path: str | Path | None = None) -> bool:
        """Whether the project file (or ``path``, if it is the project file) was changed since
        this window opened or saved it, e.g. by another window with the same project."""
        current = self.project.path
        if current is None or self._stamp is None or (path is not None and Path(path).resolve() != current):
            return False
        now = _stamp(current)
        return now is not None and now != self._stamp

    def _set_project(self, project: Project) -> None:
        self._worker.cancel()
        self._stamp = None
        self.project = project
        self.engine = Engine(project, self.registry, self.engine.cache)
        self.results = {}
        self._undo.clear()
        self._redo.clear()
        self.table_id = project.tables[0].id if project.tables else None
        self.step_id = None
        self._set_dirty(False)
        self.project_changed.emit()
        self.structure_changed.emit()
        self.selection_changed.emit()
        self.undo_changed.emit()
        self.calculate_now()

    # --- selection ---------------------------------------------------------------------------

    @property
    def table(self) -> Table | None:
        return self.project.find_table(self.table_id) if self.table_id else None

    @property
    def step(self) -> Step | None:
        """The selected step; without a selection, the last step of the table."""
        table = self.table
        if table is None or not table.steps:
            return None
        return next((s for s in table.steps if s.id == self.step_id), table.steps[-1])

    @property
    def step_index(self) -> int | None:
        table, step = self.table, self.step
        return None if step is None else table.steps.index(step)

    def select_table(self, table_id: str | None) -> None:
        if table_id == self.table_id:
            return
        self.table_id = table_id
        self.step_id = None
        self.selection_changed.emit()
        self.calculate_now()

    def select_step(self, step_id: str | None) -> None:
        if step_id != self.step_id:
            self.step_id = step_id
            self.selection_changed.emit()

    def action(self, step: Step | None) -> Action | None:
        return self.registry.actions.get(step.action) if step is not None else None

    # --- results -----------------------------------------------------------------------------

    @property
    def result(self) -> TableResult | None:
        return self.results.get(self.table_id) if self.table_id else None

    def step_result(self, step: Step | None) -> StepResult | None:
        result = self.result
        if step is None or result is None:
            return None
        return next((r for r in result.steps if r.step_id == step.id), None)

    def input_frame(self, step: Step | None) -> pl.DataFrame | None:
        """The table going into ``step``: the result of the last step before it that has one.
        None while the steps before it are not calculated as they are now (e.g. one was just
        moved), or when one of them has an error."""
        table, result = self.table, self.result
        if step is None or table is None or result is None or step not in table.steps:
            return None
        before = table.steps[: table.steps.index(step)]
        if [r.step_id for r in result.steps[: len(before)]] != [s.id for s in before]:
            return None  # results of another order of steps: wait for the new ones
        for outcome in reversed(result.steps[: len(before)]):
            if outcome.status in (Status.ERROR, Status.BLOCKED):
                return None
            if outcome.skip_rest is not None and outcome.df is not None and outcome.df.width == 0:
                return None  # an import that skipped the rest without data: its columns are unknown
            if outcome.df is not None:
                return outcome.df
        return None

    def input_problem(self, step: Step | None) -> str:
        """Why ``input_frame(step)`` has no data, in a sentence (for the formula window)."""
        table, result = self.table, self.result
        if table is None or step is None or step not in table.steps:
            return ""
        index = table.steps.index(step)
        if not table.enabled:
            return f"“{table.name}” is turned off, so it isn't calculated. Turn it on to see a preview."
        if result is not None and result.skipped:
            return f"“{table.name}” is not calculated: it {result.skipped}."
        if index == 0:
            return "This is the first step of the table: there is no data going into it."
        before = result.steps[:index] if result is not None else []
        for number, outcome in enumerate(before, start=1):
            if outcome.skip_rest is not None and outcome.status == Status.OK:
                return f"Step {number} skips the rest of the table ({outcome.skip_reason}): no data goes into this step."
        if result is not None and any(r.status in (Status.ERROR, Status.BLOCKED) for r in result.steps[:index]):
            failed = first_error(result)
            if failed is not None and (needed := missing_settings(failed[1])):
                return f"Step {failed[0]} is not complete yet (fill in {', '.join(needed)}): the preview follows when it is."
            return "A step before this one has an error: fix it to see a preview here."
        return "The steps before this one are still being calculated: the preview follows."

    def input_columns(self, step: Step | None) -> list[str] | None:
        """Columns of the table going into ``step`` (the last good result before it)."""
        frame = self.input_frame(step)
        return frame.columns if frame is not None else None

    def table_columns(self, table_id: str) -> list[str] | None:
        result = self.results.get(table_id)
        return result.df.columns if result is not None and result.df is not None else None

    def parameter_values(self) -> dict[str, Any]:
        """Name -> value of the project's parameters, as the calculation uses them (a Failed for
        a formula with an error), for the checks of formulas while typing. They are worked out
        again when the parameters change and on Reload data (see ``Engine.parameter_values``)."""
        return self.engine.parameter_values()

    def table_status(self, table_id: str) -> str:
        """The table's status: "ok", "error", "incomplete" (a step is not filled in yet),
        "disabled" (turned off), "skipped" (uses a table that is off) or "pending" (not
        calculated yet)."""
        table = self.project.find_table(table_id)
        if table is not None and not table.enabled:
            return "disabled"
        result = self.results.get(table_id)
        if result is None:
            return "pending"
        if result.skipped:
            return "skipped"
        if result.ok:
            return "ok"
        failed = first_error(result)
        return "incomplete" if result.error is None and failed is not None and missing_settings(failed[1]) else "error"

    def missing_settings(self, step: Step | None) -> list[str]:
        """The settings the selected table's ``step`` still needs, when that is all that is
        wrong with it (see ``missing_settings``)."""
        return missing_settings(self.step_result(step))

    def dependents(self, table_id: str) -> list[Table]:
        """Tables whose steps use ``table_id``."""
        return [t for t in self.project.tables if t.id != table_id and table_id in self.engine.dependencies(t.id)]

    # --- calculating -------------------------------------------------------------------------

    def recalculate(self) -> None:
        self._timer.start()

    def calculate_now(self) -> None:
        """Calculate the selected table; on the project map, all other tables afterwards."""
        self._timer.stop()
        table_id = self.table_id if self.table_id and self.project.find_table(self.table_id) else None
        if table_id is not None:
            self._calculate("table", table_id)
        elif self.overview and self.project.tables:
            self._calculate("all", None)

    def _calculate(self, scope: str, table_id: str | None) -> None:
        engine = self.engine.snapshot()
        if scope == "table":
            self._worker.start(lambda cancel: ("table", engine.evaluate_all(table_id, cancel=cancel)))
        else:
            self._worker.start(lambda cancel: ("all", engine.run(mode=Mode.PREVIEW, cancel=cancel)))

    def set_overview(self, enabled: bool) -> None:
        """While the project map is shown, every table is calculated (for its status)."""
        if enabled != self.overview:
            self.overview = enabled
            self.calculate_now()

    def reload_sources(self) -> None:
        self.engine.reload()
        self.calculate_now()

    def run_project(self, table_ids: list[str] | None = None) -> None:
        """Run tables (default: all) with exports and commands, in the background."""
        engine = self.engine.snapshot()

        def work(cancel):
            started = time.perf_counter()
            results = engine.run(table_ids, cancel=cancel)
            return results, engine.reload_token, time.perf_counter() - started

        self._stopping = False
        self._runner.start(work)

    def cancel_run(self) -> None:
        """Stop the run after the step that is running."""
        if self._runner.busy:
            self._stopping = True
        self._runner.cancel()

    @property
    def stopping(self) -> bool:
        """Stop was pressed, and the run is finishing the step it is in."""
        return self._stopping and self._runner.busy

    @property
    def busy(self) -> bool:
        return self._worker.busy

    @property
    def running(self) -> bool:
        return self._runner.busy

    def wait(self, msecs: int = 30_000) -> bool:
        """Wait for background work (tests, closing the app)."""
        return self._worker.wait(msecs) and self._runner.wait(msecs)

    def _on_results(self, outcome: tuple[str, dict[str, TableResult]]) -> None:
        scope, results = outcome
        self.results.update(results)
        self.results_changed.emit()
        if scope == "table" and self.overview:  # the selected table first, then everything else
            self._calculate("all", None)

    def _on_run_finished(self, outcome: tuple[dict[str, TableResult], int, float]) -> None:
        results, reload_token, self.run_seconds = outcome
        self.engine.reload_token = max(self.engine.reload_token, reload_token)  # previews reuse the run
        self.results.update(results)
        self.results_changed.emit()
        self.run_finished.emit(results)
        self.calculate_now()

    # --- editing tables ----------------------------------------------------------------------

    def add_table(
        self,
        source: Action,
        name: str | None = None,
        position: tuple[float, float] | None = None,
        group: str | None = None,
    ) -> Table:
        """A new table starting with ``source`` (an import); ``position`` places it on the map."""
        self._checkpoint()
        table = self.project.add_table(name or self._free_name("Table"))
        table.position = _rounded(position)
        table.group = group or ""
        self.project.add_step(table, source, _initial_params(source))
        self.table_id, self.step_id = table.id, None
        self._edited(structure=True)
        self.selection_changed.emit()
        return table

    def rename_table(self, table_id: str, name: str) -> None:
        name = name.strip()
        table = self.project.table(table_id)
        if not name:
            raise ValueError("A table needs a name")
        if name == table.name:
            return
        other = self.project.find_table(name)
        if other is not None and other is not table:
            raise ValueError(f"A table named '{name}' already exists")
        self._checkpoint()
        table.name = name
        self._edited(structure=True)

    def duplicate_table(self, table_id: str) -> Table:
        self._checkpoint()
        original = self.project.table(table_id)
        copy_ = self.project.add_table(self._free_name(f"{original.name} (copy)"))
        copy_.group = original.group
        copy_.filters = copy.deepcopy(original.filters)
        for step in original.steps:
            new = self.project.add_step(copy_, step.action, copy.deepcopy(step.params), version=step.version)
            new.enabled, new.note = step.enabled, step.note
        self.table_id, self.step_id = copy_.id, None
        self._edited(structure=True)
        self.selection_changed.emit()
        return copy_

    def delete_table(self, table_id: str) -> None:
        self.delete_tables([table_id])

    def delete_tables(self, table_ids: Iterable[str]) -> None:
        """Delete tables (one undo step)."""
        doomed = set(table_ids)
        if not doomed:
            return
        self._checkpoint()
        first = min(i for i, t in enumerate(self.project.tables) if t.id in doomed)
        self.project.tables = [t for t in self.project.tables if t.id not in doomed]
        for table_id in doomed:
            self.results.pop(table_id, None)
        if self.table_id in doomed:
            remaining = self.project.tables
            self.table_id = remaining[min(first, len(remaining) - 1)].id if remaining else None
            self.step_id = None
            self.selection_changed.emit()
        self._edited(structure=True)

    def set_positions(
        self,
        positions: dict[str, tuple[float, float]] | None,
        groups: dict[str, str | None] | None = None,
        group_positions: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        """Remember where the tables (and collapsed groups) sit on the project map; ``None``
        arranges everything automatically. ``groups`` (table id -> group id or None) changes
        memberships in the same undo step."""

        new = {t.id: _rounded((positions or {}).get(t.id)) for t in self.project.tables}
        new_groups = {
            g.id: _rounded((group_positions or {}).get(g.id, g.position if positions else None)) for g in self.project.groups
        }
        changes = {tid: gid or "" for tid, gid in (groups or {}).items() if self.project.table(tid).group != (gid or "")}
        same = all(t.position == new[t.id] for t in self.project.tables) and all(
            g.position == new_groups[g.id] for g in self.project.groups
        )
        if not changes and same:
            return
        self._checkpoint()
        for table in self.project.tables:
            table.position = new[table.id]
            table.group = changes.get(table.id, table.group)
        for group in self.project.groups:
            group.position = new_groups[group.id]
        self._set_dirty(True)
        if changes:
            self.structure_changed.emit()
        self.layout_changed.emit()

    def set_table_enabled(self, table_ids: Iterable[str], enabled: bool) -> None:
        """Turn tables on or off. A table that is off is not calculated or run, and tables that
        use it are skipped."""
        tables = [self.project.table(t) for t in table_ids]
        if all(t.enabled == enabled for t in tables):
            return
        self._checkpoint()
        for table in tables:
            table.enabled = enabled
        self._edited(structure=True)

    def set_groups_collapsed(
        self,
        group_ids: Iterable[str],
        collapsed: bool,
        positions: dict[str, tuple[float, float]] | None = None,
        group_positions: dict[str, tuple[float, float]] | None = None,
    ) -> None:
        """Show groups on the map as one box each (collapsed) or as their tables. ``positions``
        and ``group_positions`` move tables and collapsed groups in the same undo step (so the
        map can make room for expanded groups)."""
        groups = [g for g in map(self.project.group, group_ids) if g is not None and g.collapsed != collapsed]
        if not groups:
            return
        self._checkpoint()
        for group in groups:
            group.collapsed = collapsed
            if not collapsed:
                group.position = None  # only a collapsed group has a position of its own
        for table_id, position in (positions or {}).items():
            self.project.table(table_id).position = _rounded(position)
        for group_id, position in (group_positions or {}).items():
            group = self.project.group(group_id)
            if group is not None and group.collapsed:
                group.position = _rounded(position)
        self._edited(structure=True, calculate=False)

    # --- the project map ---------------------------------------------------------------------

    def add_link(self, source_id: str, target_id: str, kind: str) -> Step:
        """Let table ``target`` use table ``source``: a merge/join or append step (see
        ``LINK_KINDS``), placed before the target's exports and selected."""
        action_id, preset = LINK_KINDS[kind]
        action = self.registry.actions[action_id]
        table = self.project.table(target_id)
        params = {**_initial_params(action), **preset}
        if action_id == "table.append":
            params["tables"] = [source_id]
        else:
            key = self.guess_key(target_id, source_id)
            params["other"] = source_id
            params["match"] = [{"column": key, "other_column": key}]
        index = len(table.steps)
        while index > 1 and (previous := self.action(table.steps[index - 1])) is not None and previous.side_effects:
            index -= 1
        self._checkpoint()
        step = self.project.add_step(table, action, params, index=index)
        self.table_id, self.step_id = table.id, step.id
        self._edited(structure=True)
        self.selection_changed.emit()
        return step

    def guess_key(self, target_id: str, source_id: str) -> str:
        """A column both tables have, preferring key-like names (CustomerNo, ID, Code, ...)."""
        target, source = self.table_columns(target_id) or [], self.table_columns(source_id) or []
        common = [c for c in target if c in source]
        return next((c for c in common if _KEY_NAME.search(c)), common[0] if common else "")

    def add_derived_table(
        self, source_id: str, position: tuple[float, float] | None = None, group: str | None = None
    ) -> Table:
        """A new table that starts from the result of ``source``."""
        source = self.project.table(source_id)
        self._checkpoint()
        table = self.project.add_table(self._free_name(f"{source.name} (derived)"))
        self.project.add_step(table, self.registry.actions["table.derive"], {"table": source.id})
        table.position = _rounded(position)
        table.group = group or ""
        self.table_id, self.step_id = table.id, None
        self._edited(structure=True)
        self.selection_changed.emit()
        return table

    # --- groups ------------------------------------------------------------------------------

    def add_group(
        self,
        name: str,
        table_ids: Iterable[str] = (),
        color: str | None = None,
        positions: dict[str, tuple[float, float]] | None = None,
    ) -> Group:
        """A new group with these tables. ``positions`` moves tables on the map in the same undo
        step; without it, the map places the group's tables together."""
        used = {g.color for g in self.project.groups}
        colors = list(GROUP_COLORS)
        color = color or next((c for c in colors if c not in used), colors[len(self.project.groups) % len(colors)])
        self._checkpoint()
        group = self.project.add_group(name.strip() or "Group", color)
        for table_id in table_ids:
            table = self.project.table(table_id)
            table.group = group.id
            if positions is None:
                table.position = None  # the map puts the group's tables together
        for table_id, position in (positions or {}).items():
            self.project.table(table_id).position = _rounded(position)
        self._edited(structure=True, calculate=False)
        self.layout_changed.emit()
        return group

    def rename_group(self, group_id: str, name: str) -> None:
        group = self.project.group(group_id)
        if group is not None and name.strip() and name.strip() != group.name:
            self._checkpoint()
            group.name = name.strip()
            self._edited(structure=True, calculate=False)

    def set_group_color(self, group_id: str, color: str) -> None:
        group = self.project.group(group_id)
        if group is not None and color != group.color:
            self._checkpoint()
            group.color = color
            self._edited(structure=True, calculate=False)

    def delete_group(self, group_id: str) -> None:
        """Remove the group; its tables stay, without a group."""
        if self.project.group(group_id) is None:
            return
        self._checkpoint()
        self.project.groups = [g for g in self.project.groups if g.id != group_id]
        for table in self.project.members(group_id):
            table.group = ""
        self._edited(structure=True, calculate=False)

    def set_table_group(self, table_ids: Iterable[str], group_id: str | None) -> None:
        """Put tables into a group (on the map they move to its other tables) or out of their
        group (``None``)."""
        tables = [self.project.table(t) for t in table_ids]
        if all(t.group == (group_id or "") for t in tables):
            return
        self._checkpoint()
        for table in tables:
            _join(table, group_id)
        self._edited(structure=True, calculate=False)
        self.layout_changed.emit()

    def place_table(self, table_id: str, group_id: str | None, *, before: str | None = None, after: str | None = None) -> None:
        """Move a table in the list (tables view drag and drop): into ``group_id``, before or
        after another table, or else to the end of its group."""
        table = self.project.table(table_id)
        if table_id in (before, after):
            before = after = None
            if table.group == (group_id or ""):
                return
        self._checkpoint()
        _join(table, group_id)
        tables = self.project.tables
        tables.remove(table)
        if before:
            index = tables.index(self.project.table(before))
        elif after:
            index = tables.index(self.project.table(after)) + 1
        else:
            members = [i for i, t in enumerate(tables) if table.group and t.group == table.group]
            index = members[-1] + 1 if members else len(tables)
        tables.insert(index, table)
        self._edited(structure=True, calculate=False)

    # --- editing steps -----------------------------------------------------------------------

    def add_step(self, action: Action, index: int | None = None, params: dict[str, Any] | None = None) -> Step:
        """Add a step after the selected one (or at ``index``) and select it; ``params`` are
        settings that differ from the defaults."""
        table = self.table
        if table is None:
            raise ValueError("No table selected")
        if index is None:
            current = self.step_index
            index = len(table.steps) if current is None else current + 1
        self._checkpoint()
        step = self.project.add_step(table, action, {**_initial_params(action), **(params or {})}, index=index)
        self.step_id = step.id
        self._edited(structure=True)
        self.selection_changed.emit()
        return step

    def set_params(self, step_id: str, params: dict[str, Any]) -> None:
        table, step = self.find_step(step_id)
        merged = {**step.params, **params}
        if _json_equal(merged, step.params):
            return
        self._checkpoint()
        step.params = merged
        if table.steps[0] is step:
            self._name_table_after_file(table, step)
        self._edited(step_id=step_id)

    def set_enabled(self, step_id: str, enabled: bool) -> None:
        _, step = self.find_step(step_id)
        if step.enabled != enabled:
            self._checkpoint()
            step.enabled = enabled
            self._edited(step_id=step_id)

    def set_note(self, step_id: str, note: str) -> None:
        _, step = self.find_step(step_id)
        if step.note != note:
            self._checkpoint()
            step.note = note
            self._edited(step_id=step_id)

    def delete_step(self, step_id: str) -> None:
        table, step = self.find_step(step_id)
        self._checkpoint()
        index = table.steps.index(step)
        table.steps.remove(step)
        if self.step_id == step_id:
            self.step_id = table.steps[index - 1].id if index > 0 and table.steps else None
            self.selection_changed.emit()
        self._edited(structure=True)

    def move_step(self, step_id: str, new_index: int) -> None:
        table, step = self.find_step(step_id)
        index = table.steps.index(step)
        new_index = max(0, min(new_index, len(table.steps) - 1))
        if new_index == index:
            return
        self._checkpoint()
        table.steps.insert(new_index, table.steps.pop(index))
        self._edited(structure=True)

    def duplicate_step(self, step_id: str) -> Step:
        table, step = self.find_step(step_id)
        self._checkpoint()
        new = self.project.add_step(
            table, step.action, copy.deepcopy(step.params), version=step.version, index=table.steps.index(step) + 1
        )
        new.enabled, new.note = step.enabled, step.note
        self.step_id = new.id
        self._edited(structure=True)
        self.selection_changed.emit()
        return new

    # --- instant filters ---------------------------------------------------------------------

    def filters(self, table_id: str) -> list[dict[str, Any]]:
        table = self.project.find_table(table_id)
        return [dict(f) for f in table.filters] if table is not None else []

    def set_filters(self, table_id: str, filters: list[dict[str, Any]]) -> None:
        """Instant filters only change what the grid shows: they are saved with the project
        but are not an edit (no undo step, nothing is recalculated)."""
        table = self.project.table(table_id)
        filters = [dict(f) for f in filters]
        if table.filters == filters:
            return
        table.filters = filters
        self._set_dirty(True)
        self.filters_changed.emit(table_id)

    def set_connections(self, connections: list[Connection], renames: dict[str, str] | None = None) -> None:
        """Replace the project's connections; ``renames`` (old name -> new name) also updates
        the steps that use a renamed connection."""
        renames = {old: new for old, new in (renames or {}).items() if old != new}
        if not renames and [c.to_dict() for c in self.project.connections] == [c.to_dict() for c in connections]:
            return
        self._checkpoint()
        self.project.connections = connections
        for table in self.project.tables:
            for step in table.steps:
                action = self.registry.actions.get(step.action)
                for param in action.params if action else ():
                    value = step.params.get(param.key)
                    if param.kind == "connection" and isinstance(value, str) and value in renames:
                        step.params[param.key] = renames[value]
        self._edited(structure=True)

    def set_parameters(self, new: list[Parameter], renames: dict[str, str] | None = None) -> None:
        """Replace the project's parameters; ``renames`` (old name -> new name) also writes
        ``{New}`` for ``{Old}`` in the settings of the steps and the connections, and in the
        values of the parameters."""
        renames = {old: new_name for old, new_name in (renames or {}).items() if old != new_name}
        if not renames and [p.to_dict() for p in self.project.parameters] == [p.to_dict() for p in new]:
            return
        self._checkpoint()
        self.project.parameters = [Parameter(p.name, parameters.renamed(p.value, renames)) for p in new]
        if renames:
            for table in self.project.tables:
                for step in table.steps:
                    step.params = parameters.renamed(step.params, renames)
            for connection in self.project.connections:
                connection.settings = parameters.renamed(connection.settings, renames)
        self._edited(structure=True)

    # --- undo --------------------------------------------------------------------------------

    @property
    def can_undo(self) -> bool:
        return bool(self._undo)

    @property
    def can_redo(self) -> bool:
        return bool(self._redo)

    def undo(self) -> None:
        if self._undo:
            self._redo.append(self._state())
            self._restore(self._undo.pop())

    def redo(self) -> None:
        if self._redo:
            self._undo.append(self._state())
            self._restore(self._redo.pop())

    def _checkpoint(self) -> None:
        self._undo.append(self._state())
        del self._undo[:-_UNDO_LIMIT]
        self._redo.clear()
        self.undo_changed.emit()

    def _state(self) -> dict[str, Any]:
        return {"project": self.project.to_dict(), "table": self.table_id, "step": self.step_id}

    def _restore(self, state: dict[str, Any]) -> None:
        restored = Project.from_dict(state["project"], path=self.project.path)
        filters = {t.id: t.filters for t in self.project.tables}  # undo is for edits, not for filters
        for table in restored.tables:
            table.filters = filters.get(table.id, table.filters)
        self.project = restored
        self.engine.project = restored
        self.table_id = state["table"] if restored.find_table(state["table"] or "") else None
        self.step_id = state["step"]
        self._set_dirty(True)
        self.structure_changed.emit()
        self.selection_changed.emit()
        self.undo_changed.emit()
        self.recalculate()

    # --- helpers -----------------------------------------------------------------------------

    def _edited(self, *, structure: bool = False, step_id: str | None = None, calculate: bool = True) -> None:
        self._set_dirty(True)
        if structure:
            self.structure_changed.emit()
        if step_id is not None:
            self.step_changed.emit(step_id)
        if calculate:
            self.recalculate()

    def _set_dirty(self, dirty: bool) -> None:
        if dirty != self.dirty:
            self.dirty = dirty
            self.dirty_changed.emit(dirty)

    def find_step(self, step_id: str) -> tuple[Table, Step]:
        for table in self.project.tables:
            for step in table.steps:
                if step.id == step_id:
                    return table, step
        raise KeyError(step_id)

    def _free_name(self, base: str) -> str:
        if base == "Table" or self.project.find_table(base) is not None:
            number = 1
            while self.project.find_table(f"{base} {number}") is not None:
                number += 1
            return f"{base} {number}"
        return base

    def _name_table_after_file(self, table: Table, step: Step) -> None:
        """A new table called "Table 3" takes the name of the file it imports (or of the
        folder, when it imports all files of a folder)."""
        folder = step.params.get("source") == "folder"
        file = step.params.get("folder" if folder else "file")
        if not file or not _DEFAULT_TABLE_NAME.fullmatch(table.name):
            return
        path = Path(str(file))
        stem = (path.name if folder else path.stem).strip()
        if stem and self.project.find_table(stem) is None:
            table.name = stem
            self.structure_changed.emit()

    def _rebase_paths(self, old_dir: Path | None, new_dir: Path) -> None:
        """Keep file paths valid when the project is saved in another folder: relative paths are
        relative to the project file. A path that starts with a parameter ({Folder}\\orders.csv)
        stays as it is: the parameter may stand for a whole folder."""
        for table in self.project.tables:
            for step in table.steps:
                action = self.registry.actions.get(step.action)
                if action is not None:
                    _rebase(step.params, action.params, old_dir, new_dir)
        for connection in self.project.connections:
            connector = self.registry.connectors.get(connection.type)
            if connector is not None:
                _rebase(connection.settings, connector.params, old_dir, new_dir)


def _stamp(path: Path | None) -> tuple[int, int] | None:
    """When a file was last written, and its size (None if it isn't there)."""
    try:
        info = path.stat() if path is not None else None
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_size) if info is not None else None


def count(number: int, word: str) -> str:
    """"1 row", "2,500 rows"."""
    return f"{number:,} {word}{'' if number == 1 else 's'}"


def size_text(df: pl.DataFrame) -> str:
    """"1 row × 3 columns"."""
    return f"{count(df.height, 'row')} × {count(df.width, 'column')}"


def first_error(result: TableResult | None) -> tuple[int, StepResult] | None:
    """The number and the result of the table's first step with an error."""
    steps = result.steps if result is not None else []
    return next(((n, r) for n, r in enumerate(steps, start=1) if r.status == Status.ERROR), None)


def missing_settings(result: StepResult | None) -> list[str]:
    """The settings a step still needs (e.g. "File", "Columns, row 2 – Name") when that is all
    that is wrong with it: a step just added and not filled in yet. Empty for anything else."""
    if result is None or result.status != Status.ERROR or result.inherited or not result.error:
        return []
    lines = [line.strip() for line in result.error.splitlines() if line.strip()]
    if not lines or not all(line.endswith(_REQUIRED) for line in lines):
        return []
    return list(dict.fromkeys(line.removesuffix(_REQUIRED) for line in lines))


def _rounded(position: tuple[float, float] | None) -> tuple[float, float] | None:
    return (round(position[0]), round(position[1])) if position is not None else None


def _join(table: Table, group_id: str | None) -> None:
    """Change the group of a table. Joining a group, it forgets its place on the map: the map
    puts it below the group's other tables (a table leaving a group just leaves the frame)."""
    if table.group != (group_id or ""):
        table.group = group_id or ""
        if group_id:
            table.position = None


def _initial_params(action: Action) -> dict[str, Any]:
    """Parameters for a new step: the defaults, as plain JSON. Lists that need rows get them,
    so their fields are visible right away."""
    params = action.resolve({})
    for param in action.params:
        if isinstance(param, List) and not params[param.key]:
            params[param.key] = [param.new_row() for _ in range(param.min_items)]
    return json_ready(params)


def json_ready(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: json_ready(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(v) for v in value]
    return value


def _json_equal(a: Any, b: Any) -> bool:
    return json_ready(a) == json_ready(b)


def _rebase(values: dict[str, Any], spec: tuple[Param, ...], old_dir: Path | None, new_dir: Path) -> None:
    for param in spec:
        if param.kind not in ("file", "folder"):
            continue
        text = str(values.get(param.key) or "").strip()
        if not text or "%" in text or text.startswith("~") or PLACE.match(text):
            continue
        path = Path(text)
        if not path.is_absolute():
            if old_dir is None:
                continue
            path = old_dir / path
        try:
            relative = Path(os.path.relpath(path, new_dir)).as_posix()
        except ValueError:  # on another drive
            relative = ".."
        # Files inside the project folder are stored relative to it, all others with their full path.
        values[param.key] = str(path) if relative.startswith("..") else relative

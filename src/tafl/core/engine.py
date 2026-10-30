"""Calculates tables: runs step chains with caching, preview/run modes and table dependencies.

Caching: every step result is stored under a key that hashes everything it depends on — the
previous step's key, the action and its settings, the keys of referenced tables, and for sources a
fingerprint such as the file's modification time. Editing a step therefore changes the keys of that
step and everything after it (and of tables using it), so only those are recalculated.

Modes: in Preview, exports and commands are skipped (the table passes through unchanged). In Run
they execute, and sources are re-read.

An action that raises SkipRest ends its table there: the step is OK, the steps after it are
skipped, and the table's result is the data of that step. A step that uses a table which skipped
its rest and has no rows (e.g. an import on a day without files) ends its own table the same way,
without running: there is no data for it to work with.

The files that a folder import read (``Context.imported_files``) are kept with its result, also
in the cache, and the later steps of its table get them.
"""

from __future__ import annotations

import copy
import hashlib
import json
import threading
import time
import traceback
import uuid
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, NamedTuple

import polars as pl

from tafl import crash
from tafl.api import (
    Action,
    ActionError,
    Cancelled,
    Connector,
    Context,
    Database,
    Kind,
    Mode,
    P,
    Params,
    SkipRest,
    ValidationContext,
)
from tafl.api.expressions import is_panic
from tafl.api.parameters import fill_sql
from tafl.crash import installed_memory

from . import parameters
from .cancel import stopping_queries
from .project import Project, Step, Table
from .registry import Registry


class Status(StrEnum):
    OK = "ok"
    ERROR = "error"
    SKIPPED = "skipped"  # export or command in preview mode, or after a step that raised SkipRest
    DISABLED = "disabled"
    BLOCKED = "blocked"  # an earlier step of the table failed


class Imported(NamedTuple):
    """The files that an import read from a folder, and the folder (``Context.imported_files``)."""

    folder: Path
    files: tuple[Path, ...]


@dataclass
class StepResult:
    step_id: str
    status: Status
    df: pl.DataFrame | None = None
    error: str | None = None
    details: str | None = None  # traceback, for unexpected errors
    messages: list[tuple[str, str]] = field(default_factory=list)
    seconds: float = 0.0  # how long the action ran (also when it failed)
    cached: bool = False
    key: str = ""
    inherited: bool = False  # the error is in a table this step uses
    # The message of a SkipRest: on the step that raised it (OK) and on the steps after it (SKIPPED)
    skip_rest: str | None = None
    imported: Imported | None = None  # of an import of all files in a folder: the files it read

    @property
    def ok(self) -> bool:
        return self.status in (Status.OK, Status.SKIPPED, Status.DISABLED)

    @property
    def skip_reason(self) -> str:
        """``skip_rest`` to follow a colon, as in "skipped: the table is empty"."""
        text = (self.skip_rest or "").rstrip(".")
        if len(text) > 1 and text[0].isupper() and text[1].islower():  # "SQL …" and "“Orders” …" stay
            return text[0].lower() + text[1:]
        return text


@dataclass
class TableResult:
    table_id: str
    steps: list[StepResult] = field(default_factory=list)
    error: str | None = None  # problem with the table as a whole (no steps, circular reference)
    skipped: str | None = None  # why it wasn't calculated: "turned off", or a table it uses is off
    # The run was stopped while this table was calculated: ``steps`` are the steps that ran
    stopped: bool = False
    # Of a stopped table: the messages of the step that was running, e.g. how many files a
    # command had moved before the stop
    stopped_messages: list[tuple[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        if self.error is not None or self.skipped is not None or self.stopped:
            return False
        return bool(self.steps) and all(s.ok for s in self.steps)

    @property
    def failed(self) -> bool:
        """An error (a skipped or stopped table has none)."""
        return self.skipped is None and not self.stopped and not self.ok

    @property
    def df(self) -> pl.DataFrame | None:
        """The table's result, or None if it has an error."""
        return self.steps[-1].df if self.ok else None

    @property
    def key(self) -> str:
        return self.steps[-1].key if self.steps else ""

    @property
    def first_error(self) -> str | None:
        if self.error:
            return self.error
        return next((s.error for s in self.steps if s.status == Status.ERROR), None)

    @property
    def skipped_from(self) -> StepResult | None:
        """The step that skipped the rest of the table (SkipRest), if one did."""
        return next((s for s in self.steps if s.skip_rest is not None and s.status == Status.OK), None)


@dataclass(frozen=True)
class Link:
    """Step ``step`` (number ``index``) of table ``target`` uses table ``source``."""

    source: str
    target: str
    step: str
    index: int
    enabled: bool


class Cached(NamedTuple):
    """A cached step result: the table, the message of a SkipRest the step raised (None if it
    raised none), and the messages of the step itself (without the parameter warnings)."""

    df: pl.DataFrame
    skip_rest: str | None = None
    messages: tuple[tuple[str, str], ...] = ()


class ResultCache:
    """Step results by key; the least recently used are dropped beyond ``max_bytes`` (by default
    a quarter of the installed memory). Of each step only the results of its last two settings
    are kept: the current ones, and those an undo goes back to. A result keeps the step's
    messages and the message of a SkipRest, so that a cached step shows the same messages and
    skips the rest of its table too, and an import's result the files it read from a folder."""

    KEEP = 2  # results kept per step

    def __init__(self, max_bytes: int | None = None) -> None:
        self.max_bytes = max_bytes if max_bytes is not None else default_cache_bytes()
        self._items: OrderedDict[str, tuple[Cached, int, Imported | None]] = OrderedDict()
        self._bytes = 0
        self._steps: dict[str, list[str]] = {}  # step id -> the keys of its results, the latest last
        self._lock = threading.RLock()  # re-entrant: Ctrl+C right after it was taken leaves it held

    def get(self, key: str) -> pl.DataFrame | None:
        found = self.lookup(key)
        return found.df if found is not None else None

    def lookup(self, key: str, *, step: str | None = None) -> Cached | None:
        """The result with its messages. ``step``: the id of the step that uses it, which makes
        it that step's latest result."""
        found = self.find(key, step=step)
        return found[0] if found is not None else None

    def find(self, key: str, *, step: str | None = None) -> tuple[Cached, Imported | None] | None:
        """``lookup``, and the files an import read from a folder (``put``)."""
        with self._lock:
            item = self._items.get(key)
            if item is None:
                return None
            self._items.move_to_end(key)
            if step is not None:
                self._remember(step, key)
            return item[0], item[2]

    def put(
        self,
        key: str,
        df: pl.DataFrame,
        skip_rest: str | None = None,
        *,
        step: str | None = None,
        messages: Iterable[tuple[str, str]] | None = None,
        imported: Imported | None = None,
    ) -> None:
        """Keep a result; ``step``: the id of the step whose result it is, whose results of older
        settings are dropped; ``imported``: the files an import read from a folder. Without
        ``messages``, a result that is there already keeps its messages and files (a step that
        passed its table through unchanged)."""
        size = df.estimated_size()
        with self._lock:
            old = self._items.pop(key, None)
            if old is not None:
                self._bytes -= old[1]
            if messages is None:
                messages = old[0].messages if old is not None else ()
                imported = old[2] if old is not None else imported
            self._items[key] = (Cached(df, skip_rest, tuple(messages)), size, imported)
            self._bytes += size
            if step is not None:
                self._remember(step, key)
            while self._bytes > self.max_bytes and len(self._items) > 1:
                _, (_, dropped, _) = self._items.popitem(last=False)
                self._bytes -= dropped

    def clear(self) -> None:
        with self._lock:
            self._items.clear()
            self._steps.clear()
            self._bytes = 0

    def forget(self, steps: Iterable[str]) -> None:
        """Drop the results of these steps: their source has changed for good, so no calculation
        finds them again, and they would only hold on to memory."""
        with self._lock:
            for step in steps:
                for key in self._steps.pop(step, ()):
                    if key in self._items and not any(key in other for other in self._steps.values()):
                        self._bytes -= self._items.pop(key)[1]

    def __len__(self) -> int:
        return len(self._items)

    def _remember(self, step: str, key: str) -> None:
        keys = self._steps.setdefault(step, [])
        if key in keys:
            keys.remove(key)
        keys.append(key)
        while len(keys) > self.KEEP:
            old = keys.pop(0)
            if old in self._items and not any(old in other for other in self._steps.values()):
                self._bytes -= self._items.pop(old)[1]


def default_cache_bytes() -> int:
    """A quarter of the installed memory (2 GB if it can't be told), at least 512 MB."""
    installed = installed_memory()
    return max(installed // 4, 512 * 1024**2) if installed else 2 * 1024**3


# What plugin code can raise. A migrate, summary or fingerprint function that calls sys.exit() or
# raises KeyboardInterrupt fails like any other broken plugin code, without ending the program,
# unless the calculation is being stopped (see _stop_if).
_BROKEN = (Exception, SystemExit, KeyboardInterrupt)


def _stop_if(cancel: threading.Event | None) -> None:
    """Raise Cancelled if ``cancel`` is set. Ctrl+C on the command line sets it before it
    interrupts, and a stopped DuckDB query is reported as an error: what goes wrong while it is
    set is the stop, not a problem of the step."""
    if cancel is not None and cancel.is_set():
        raise Cancelled() from None


class Engine:
    def __init__(self, project: Project, registry: Registry, cache: ResultCache | None = None) -> None:
        self.project = project
        self.registry = registry
        self.cache = cache if cache is not None else ResultCache()
        # Stops the work when set (Ctrl+C on the command line); calculations without a cancel
        # event of their own use it. A KeyboardInterrupt while it isn't set is a plugin's error.
        self.cancel: threading.Event | None = None
        # Part of every source's cache key: raising it makes all sources read their data again.
        self.reload_token = 0
        # Values given for the project's parameters, e.g. by `tafl run --set Day=2026-10-01`
        self.parameter_overrides: dict[str, str] = {}
        # The parameters' values by what they were worked out from (shared with snapshots)
        self._parameter_values: dict[tuple, dict[str, Any]] = {}
        # Re-entrant: Ctrl+C can come right after a thread took it, and that thread then lists
        # the parameters that the stopped run used
        self._parameter_lock = threading.RLock()

    def reload(self) -> None:
        """Re-read all sources on the next calculation (databases can't tell us they changed)."""
        self.reload_token += 1

    def snapshot(self) -> Engine:
        """An engine on a copy of the project that shares this engine's cache, for calculating in
        a background thread while the project is being edited."""
        twin = Engine(copy.deepcopy(self.project), self.registry, self.cache)
        twin.reload_token = self.reload_token
        twin.cancel = self.cancel
        twin.parameter_overrides = dict(self.parameter_overrides)
        twin._parameter_values, twin._parameter_lock = self._parameter_values, self._parameter_lock
        return twin

    def parameter_values(self) -> dict[str, Any]:
        """Name -> value of the project's parameters (a Failed for one that can't be worked out).
        They are worked out again only when the parameters, the overrides or the reload token
        change (a run reloads): the calculations in between use the same values, so that =NOW()
        doesn't change with every edit."""
        key = (
            tuple((p.name, p.value) for p in self.project.parameters),
            tuple(sorted(self.parameter_overrides.items())),
            self.reload_token,
        )
        with self._parameter_lock:
            found = self._parameter_values.pop(key, None)
            if found is None:
                found = parameters.values(self.project, self.parameter_overrides)
            self._parameter_values[key] = found  # to the end: the values unused for longest are dropped first
            while len(self._parameter_values) > 8:  # a snapshot may still use an older key
                del self._parameter_values[next(iter(self._parameter_values))]
        return dict(found)

    def evaluate(
        self,
        table: str,
        *,
        until: int | None = None,
        mode: Mode | str = Mode.PREVIEW,
        cancel: threading.Event | None = None,
    ) -> TableResult:
        """Calculate one table (only up to step index ``until`` if given)."""
        evaluation = _Evaluation(self, Mode(mode), self.cancel if cancel is None else cancel)
        return evaluation.table(self.project.table(table).id, until)

    def evaluate_all(
        self, table: str, *, mode: Mode | str = Mode.PREVIEW, cancel: threading.Event | None = None
    ) -> dict[str, TableResult]:
        """Calculate a table; returns its result and the results of the tables it uses."""
        evaluation = _Evaluation(self, Mode(mode), self.cancel if cancel is None else cancel)
        table_id = self.project.table(table).id
        evaluation.table(table_id)
        return dict(evaluation.results)

    def run(
        self,
        tables: Iterable[str] | None = None,
        *,
        mode: Mode | str = Mode.RUN,
        cancel: threading.Event | None = None,
        done: Callable[[str, TableResult], None] | None = None,
        keep_results: bool = True,
    ) -> dict[str, TableResult]:
        """Calculate tables (default: all) and the tables they use, in dependency order.

        In Run mode (the default) all sources are read again and exports and commands execute.
        The results are cached, so a preview afterwards shows the data of the run.

        ``done`` gets each table's result as soon as the table is calculated. When the run is
        stopped (Cancelled), it also gets the table that was being calculated, with ``stopped``
        set, the steps that ran before the stop, and the messages of the step that was running
        (``stopped_messages``).

        Without ``keep_results`` the data of a table goes once ``done`` has had it and no table
        after it uses it, so that a run of many tables doesn't need the memory of all of them. The
        results have no ``df`` then, and of the cache only what tables read from outside stays,
        for the tables that read the same.
        """
        if Mode(mode) == Mode.RUN:
            self.reload()
        evaluation = _Evaluation(self, Mode(mode), self.cancel if cancel is None else cancel)
        wanted = None if tables is None else [self.project.table(t).id for t in tables]
        results = {}
        order = self.execution_order(wanted)
        users = Counter() if keep_results else Counter(used for table_id in order for used in self.dependencies(table_id))
        try:
            for table_id in order:
                results[table_id] = evaluation.table(table_id)
                if done is not None:
                    done(table_id, results[table_id])
                if not keep_results:
                    self._let_go(table_id, results, users)
        except (Cancelled, KeyboardInterrupt):
            stopped, evaluation.stopped = evaluation.stopped, None
            if done is not None and stopped is not None:
                stopped.stopped = True
                done(stopped.table_id, stopped)
            raise
        return results

    def _let_go(self, table_id: str, results: dict[str, TableResult], users: Counter[str]) -> None:
        """``table_id`` is done: drops its data, and that of the tables it used, if no later table
        needs them. ``users``: how many tables to come use each table."""
        used = self.dependencies(table_id)
        users.subtract(used)
        for finished in (table_id, *used):
            if finished in results and users[finished] <= 0:
                steps = results[finished].steps
                copies = any(link.index == 0 for link in self.links(finished))  # its first step is another table's data
                self.cache.forget(step.step_id for step in steps[0 if copies else 1 :])
                for step in steps:
                    step.df = None

    def links(self, table: str | None = None) -> list[Link]:
        """Which step uses which other table (of one table, or of all tables)."""
        tables = self.project.tables if table is None else [self.project.table(table)]
        result = []
        for target in tables:
            for index, step in enumerate(target.steps):
                action = self.registry.actions.get(step.action)
                if action is None:
                    continue
                try:
                    params = action.resolve(action.upgrade(step.params, step.version))
                except _BROKEN:
                    _stop_if(self.cancel)
                    continue
                for ref in action.table_refs(params):
                    if (source := self.project.find_table(ref)) is not None:
                        result.append(Link(source.id, target.id, step.id, index, step.enabled))
        return result

    def dependencies(self, table: str) -> list[str]:
        """Ids of the tables that a table's (enabled) steps use."""
        return list(dict.fromkeys(link.source for link in self.links(table) if link.enabled))

    def execution_order(self, table_ids: Iterable[str] | None = None) -> list[str]:
        """Table ids ordered so that every table comes after the tables it uses."""
        wanted = list(table_ids) if table_ids is not None else [t.id for t in self.project.tables]
        order: list[str] = []
        visiting: set[str] = set()
        done: set[str] = set()

        def visit(table_id: str) -> None:
            if table_id in done or table_id in visiting:  # cycles are reported when calculating
                return
            visiting.add(table_id)
            for dependency in self.dependencies(table_id):
                visit(dependency)
            visiting.discard(table_id)
            done.add(table_id)
            order.append(table_id)

        for table_id in wanted:
            visit(table_id)
        return order

    def upgrade_steps(self) -> list[Step]:
        """Convert steps saved by older versions of their actions, in place. Returns the steps
        that changed; the project should be saved afterwards."""
        changed = []
        for table in self.project.tables:
            for step in table.steps:
                action = self.registry.actions.get(step.action)
                if action is None or step.version >= action.version:
                    continue
                try:
                    step.params = action.upgrade(step.params, step.version)
                except _BROKEN:  # left as it is; calculating the table shows the error
                    _stop_if(self.cancel)
                    continue
                step.version = action.version
                changed.append(step)
        return changed

    def describe(self, step: Step) -> str:
        """Text for the step list, with table ids replaced by table names."""
        action = self.registry.actions.get(step.action)
        if action is None:
            return f"Unknown action '{step.action}'"
        try:
            params = action.resolve(action.upgrade(step.params, step.version))
        except _BROKEN as e:  # e.g. saved by a newer version; after Ctrl+C, only Ctrl+C itself stops here
            if isinstance(e, KeyboardInterrupt):
                _stop_if(self.cancel)
            return action.name
        shown = Params(params)
        for param in action.params:
            value = params.get(param.key)
            # A value of the wrong kind (e.g. a number from a project file written by hand) stays
            if not param.accepts(value) or not P.is_visible(param, params):
                continue
            if param.kind == "table" and value:
                shown[param.key] = self.table_name(value)
            elif param.kind == "tables" and value:
                shown[param.key] = [self.table_name(v) for v in value]
            elif isinstance(param, P.List):
                shown[param.key] = _unfilled_rows(param, value)
            else:
                shown[param.key] = _unfilled(param, value)
        try:
            return action.describe(shown)  # a summary that fails otherwise gives the action's name
        except KeyboardInterrupt:
            _stop_if(self.cancel)
            return action.name

    def table_name(self, ref: str) -> str:
        table = self.project.find_table(ref)
        return table.name if table else ref


class _Evaluation:
    """One calculation: remembers table results, detects circular references."""

    def __init__(self, engine: Engine, mode: Mode, cancel: threading.Event | None) -> None:
        self.engine = engine
        self.project = engine.project
        self.registry = engine.registry
        self.mode = mode
        self.cancel = cancel
        self.run_id = uuid.uuid4().hex if mode == Mode.RUN else ""
        self.results: dict[str, TableResult] = {}
        self.stack: list[str] = []
        self.stopped: TableResult | None = None  # the table being calculated when it was stopped
        self.stopped_messages: list[tuple[str, str]] = []  # those of the step that was running then
        # The same for every step, so that =TODAY() means the same day everywhere
        self.parameters = engine.parameter_values()

    def table(self, table_id: str, until: int | None = None) -> TableResult:
        full = until is None
        if full and table_id in self.results:
            return self.results[table_id]
        if table_id in self.stack:
            cycle = [*self.stack[self.stack.index(table_id) :], table_id]
            return TableResult(table_id, error="Circular reference: " + " → ".join(map(self.engine.table_name, cycle)))
        table = self.project.table(table_id)
        result = TableResult(table.id)
        result.skipped = self._skip_reason(table)
        if result.skipped is not None:
            if full:
                self.results[table_id] = result
            return result
        if not table.steps:
            result.error = "The table has no steps yet"
        self.stack.append(table.id)
        try:
            df: pl.DataFrame | None = None
            key = ""
            failed = False
            skip_rest: str | None = None
            imported: Imported | None = None  # the files the first step read from a folder
            for index, step in enumerate(table.steps if full else table.steps[: until + 1]):
                if failed:
                    result.steps.append(StepResult(step.id, Status.BLOCKED, error="An earlier step has an error"))
                    continue
                if skip_rest is not None:  # an earlier step raised SkipRest
                    if step.enabled:
                        skipped = StepResult(step.id, Status.SKIPPED, df=df, key=key, skip_rest=skip_rest)
                        skipped.messages.append(("info", skip_rest))
                    else:
                        skipped = StepResult(step.id, Status.DISABLED, df=df, key=key)
                    result.steps.append(skipped)
                    continue
                step_result = self._step(table, index, step, df, key, imported)
                result.steps.append(step_result)
                if step_result.status == Status.ERROR:
                    failed = True
                else:
                    df, key, skip_rest = step_result.df, step_result.key, step_result.skip_rest
                    if index == 0:
                        imported = step_result.imported
        except (Cancelled, KeyboardInterrupt):
            if self.stopped is None:  # the innermost table: the one whose step was running
                self.stopped = result
                result.stopped_messages, self.stopped_messages = self.stopped_messages, []
            raise
        finally:
            self.stack.pop()
        if full:
            self.results[table_id] = result
        return result

    def _skip_reason(self, table: Table) -> str | None:
        """A table that is off is not calculated, nor is any table that uses a skipped one."""
        if not table.enabled:
            return "turned off"
        if table.id in self.stack:  # circular reference: reported by the steps
            return None
        self.stack.append(table.id)
        try:
            for dependency in self.engine.dependencies(table.id):
                if self.table(dependency).skipped is not None:
                    name = self.engine.table_name(dependency)
                    off = not self.project.table(dependency).enabled
                    return f"uses “{name}”, which is {'turned off' if off else 'skipped'}"
        finally:
            self.stack.pop()
        return None

    def _step(
        self, table: Table, index: int, step: Step, df: pl.DataFrame | None, prev_key: str, imported: Imported | None = None
    ) -> StepResult:
        """``imported``: the files the first step of the table read from a folder."""
        _stop_if(self.cancel)
        action = self.registry.actions.get(step.action)
        if action is None:
            return _error(step, f"Unknown action '{step.action}'. Is the plugin that provides it installed?")
        if not step.enabled:
            if index == 0:
                return _error(step, "The first step is disabled, so the table has no data")
            return StepResult(step.id, Status.DISABLED, df=df, key=prev_key)
        if index == 0 and action.kind != Kind.SOURCE:
            return _error(step, f"A table starts with an import or 'Start from another table'; '{action.name}' needs an input table")
        if index > 0 and action.kind == Kind.SOURCE:
            return _error(step, f"'{action.name}' can only be the first step of a table")
        try:
            raw = action.upgrade(step.params, step.version)
            connector = self._connector(action, raw)
            unknown = []
            if self.parameters:
                spans = connector.sql_spans if connector is not None else None
                unknown = parameters.unknown_names(action.params, raw, self.parameters, spans)
            raw = parameters.fill(action.params, raw, self.parameters, connector.fill_sql if connector is not None else fill_sql)
            params = action.resolve(raw)
        except ActionError as e:
            _stop_if(self.cancel)
            return _error(step, str(e))
        except _BROKEN as e:  # e.g. a plugin's migrate function
            _stop_if(self.cancel)
            ended = not isinstance(e, Exception)  # sys.exit() or KeyboardInterrupt
            problem = f"{_owner(action)} tried to end the program ({_friendly(e)})" if ended else str(e)
            return _error(step, f"Could not read the step settings: {problem}", traceback.format_exc())
        warnings = [
            ("warning", f"{{{name}}} is not a parameter of the project: it was left as it is") for name in unknown
        ]

        # Tables this step uses are calculated first.
        deps: dict[str, TableResult] = {}
        for ref in action.table_refs(params):
            found = self.project.find_table(ref)
            if found is None:
                return _error(step, f"Table '{ref}' does not exist")
            dependency = self.table(found.id)
            if not dependency.ok:
                return _error(step, _dependency_error(found.name, dependency), inherited=True)
            deps[ref] = dependency

        ctx = _StepContext(self, deps)
        if imported is not None and index > 0:
            ctx.imported_folder, ctx.imported_files = imported
        if (empty := _empty_and_skipped(deps)) is not None:  # nothing to work with: this table ends here too
            message = f"The table “{self.engine.table_name(empty)}” has no data (its rest was skipped)"
            data = df if df is not None else deps[empty].df
            key = self._key(action, params, prev_key, deps, ctx)
            messages = [*warnings, ("info", message)]
            return StepResult(step.id, Status.OK, df=data, key=key, messages=messages, skip_rest=message)

        from_folder = (imported is not None) if index > 0 else None  # the table's import read files from a folder
        if action.side_effects and self.mode == Mode.PREVIEW:  # what would stop the step in a run, as a warning
            problems = [("warning", problem) for problem in self._own_problems(action, params, df, deps, from_folder)]
            messages = [*warnings, *problems, ("info", "Runs only when the project is run")]
            return StepResult(step.id, Status.SKIPPED, df=df, key=prev_key, messages=messages)

        key = self._key(action, params, prev_key, deps, ctx)
        # Exports and commands always execute. Everything else is cached; a run raises the reload
        # token first, so it never gets old data from the cache.
        use_cache = not action.side_effects
        if use_cache and (hit := self.engine.cache.find(key, step=step.id)) is not None:
            found, read = hit
            messages = [*warnings, *found.messages]
            if found.skip_rest is not None:
                messages.append(("info", found.skip_rest))
            return StepResult(
                step.id,
                Status.OK,
                df=found.df,
                key=key,
                cached=True,
                messages=messages,
                skip_rest=found.skip_rest,
                imported=read if index == 0 else None,
            )

        try:
            problems = action.check(params, self._validation_context(df, deps, from_folder))
        except _BROKEN as e:  # a plugin's checks
            _stop_if(self.cancel)
            return _error(step, f"Could not check the step settings: {_friendly(e)}", traceback.format_exc(), warnings)
        if problems:
            return _error(step, "\n".join(problems), messages=warnings)
        ctx.messages.extend(warnings)
        if (short := crash.short_of_memory()) is not None:
            return _error(step, short, messages=warnings)

        started = time.perf_counter()
        try:
            # Stop and Ctrl+C also stop a formula that runs
            with stopping_queries(self.cancel), crash.running(self.project.name, table.name, index + 1, action.name):
                out = action.run(df.clone() if df is not None else None, params, ctx)
        except Cancelled:
            self.stopped_messages = list(ctx.messages)
            raise
        except SkipRest as e:  # cached under this step's own key: the key of the data going in has no SkipRest
            seconds = time.perf_counter() - started
            data = e.df if e.df is not None else df if df is not None else pl.DataFrame()
            if not isinstance(data, pl.DataFrame):
                return _error(step, f"'{action.name}' did not return a table", messages=ctx.messages, seconds=seconds)
            message = str(e.message).strip() or f"Step {index + 1} skips the rest of the table"
            read = _imported(ctx) if index == 0 else None
            if use_cache:
                self.engine.cache.put(key, data, message, step=step.id, messages=ctx.messages[len(warnings) :], imported=read)
            ctx.messages.append(("info", message))
            return StepResult(
                step.id, Status.OK, df=data, key=key, messages=ctx.messages, seconds=seconds, skip_rest=message, imported=read
            )
        except ActionError as e:  # also a DuckDB query that Ctrl+C stopped ("Query interrupted")
            self._stop_if_stopping(ctx)
            return _error(step, str(e), messages=ctx.messages, seconds=time.perf_counter() - started)
        except Exception as e:
            self._stop_if_stopping(ctx)
            seconds = time.perf_counter() - started
            return _error(step, _friendly(e), traceback.format_exc(), ctx.messages, seconds=seconds)
        except BaseException as e:
            # Not an Exception: a crash inside Polars (Rust), and an action that tries to end the
            # program (sys.exit, KeyboardInterrupt). Ctrl+C on the command line sets the cancel
            # event before it interrupts: then the calculation stops.
            self._stop_if_stopping(ctx)
            if not (is_panic(e) or isinstance(e, SystemExit | KeyboardInterrupt)):
                raise
            message = _friendly(e) if is_panic(e) else f"{_owner(action)} tried to end the program ({_friendly(e)})"
            return _error(step, message, traceback.format_exc(), ctx.messages, seconds=time.perf_counter() - started)
        seconds = time.perf_counter() - started

        if out is None and action.side_effects:
            out = df
        if not isinstance(out, pl.DataFrame):
            return _error(step, f"'{action.name}' did not return a table", messages=ctx.messages, seconds=seconds)
        own = ctx.messages[len(warnings) :]
        if out is df:  # passed the table through unchanged: later steps keep their cache keys
            key, own = prev_key, None  # the result stays the previous step's, with its messages
        read = _imported(ctx) if index == 0 else None
        if use_cache:
            self.engine.cache.put(key, out, step=step.id, messages=own, imported=read)
        return StepResult(step.id, Status.OK, df=out, key=key, messages=ctx.messages, seconds=seconds, imported=read)

    def _stop_if_stopping(self, ctx: Context) -> None:
        """``_stop_if``; the messages of the step that was running go to its table then
        (``TableResult.stopped_messages``)."""
        if self.cancel is not None and self.cancel.is_set():
            self.stopped_messages = list(ctx.messages)
            raise Cancelled() from None

    def _connector(self, action: Action, raw: Mapping[str, Any]) -> Connector | None:
        """The database type of the step's connection: it fills the parameters into the SQL."""
        for param in action.params:
            if isinstance(param, P.ConnectionRef):
                definition = self.project.connection(str(raw.get(param.key) or ""))
                connector = self.registry.connectors.get(definition.type) if definition else None
                if connector is not None:
                    return connector
        return None

    def _key(self, action: Action, params: Params, prev_key: str, deps: dict[str, TableResult], ctx: Context) -> str:
        parts = [prev_key, action.id, str(action.version), json.dumps(params, sort_keys=True, default=str)]
        parts += [f"{ref}={deps[ref].key}" for ref in sorted(deps)]
        if action.kind == Kind.SOURCE:
            parts += [f"dir={self.project.directory}", f"reload={self.engine.reload_token}"]
            if action.fingerprint is not None:
                try:
                    parts.append(repr(action.fingerprint(params, ctx)))
                except _BROKEN:
                    _stop_if(self.cancel)
                    # can't tell: read again (not by the clock: on some systems it gives the same
                    # time for calculations a few milliseconds apart)
                    parts.append(f"no-fingerprint={uuid.uuid4().hex}")
        if action.side_effects:
            parts.append(f"run={self.run_id}")
        for param in action.params:  # changed connection settings: read the data again
            if isinstance(param, P.ConnectionRef):
                parts.append(f"connection={self._connection_part(str(params.get(param.key) or ''))}")
        return hashlib.sha1("\x1f".join(parts).encode("utf-8")).hexdigest()

    def _connection_part(self, name: str) -> str:
        definition = self.project.connection(name)
        connector = self.registry.connectors.get(definition.type) if definition else None
        if definition is None or connector is None:
            return "none"
        try:
            settings = self.connection_settings(definition.settings, connector)
        except ActionError as e:
            return f"error={e}"
        return json.dumps([definition.type, settings], sort_keys=True, default=str)

    def connection_settings(self, settings: Mapping[str, Any], connector: Connector) -> dict[str, Any]:
        """A connection's settings with the parameters filled in (as texts)."""
        return parameters.fill(list(connector.params), settings, self.parameters)

    def _own_problems(
        self, action: Action, params: Params, df: pl.DataFrame | None, deps: dict[str, TableResult], from_folder: bool | None
    ) -> list[str]:
        """The problems that the action's own ``checks`` finds; none when it fails."""
        try:
            return action.own_problems(params, self._validation_context(df, deps, from_folder))
        except _BROKEN:
            _stop_if(self.cancel)
            return []

    def _validation_context(
        self, df: pl.DataFrame | None, deps: dict[str, TableResult], imported: bool | None = None
    ) -> ValidationContext:
        def table_columns(ref: str) -> list[str] | None:
            result = deps.get(ref)
            return result.df.columns if result is not None and result.df is not None else None

        tables = {t.id: t.name for t in self.project.tables} | {t.name: t.name for t in self.project.tables}
        return ValidationContext(
            columns=df.columns if df is not None else None,
            table_columns=table_columns,
            tables=tables,
            connections=[c.name for c in self.project.connections],
            imported=imported,
        )


class _StepContext(Context):
    def __init__(self, evaluation: _Evaluation, deps: dict[str, TableResult]) -> None:
        super().__init__(mode=evaluation.mode, project_dir=evaluation.project.directory, cancel=evaluation.cancel)
        self._evaluation = evaluation
        self._deps = deps

    def table(self, table_id: str) -> pl.DataFrame:
        result = self._deps.get(table_id)
        if result is None or result.df is None:
            raise ActionError(f"Table '{table_id}' has to be chosen in this step's settings to be used")
        return result.df.clone()

    def table_name(self, table_id: str) -> str:
        return self._evaluation.engine.table_name(table_id)

    def database(self, connection: str, *, create: bool = False) -> Database:
        definition = self._evaluation.project.connection(connection)
        if definition is None:
            raise ActionError(f"Connection '{connection}' is not defined in this project")
        connector = self._evaluation.registry.connectors.get(definition.type)
        if connector is None:
            raise ActionError(f"Unknown connection type '{definition.type}'. Is its plugin installed?")
        return connector.open(self._evaluation.connection_settings(definition.settings, connector), self, create=create)


def _error(
    step: Step,
    message: str,
    details: str | None = None,
    messages: list[tuple[str, str]] | None = None,
    *,
    inherited: bool = False,
    seconds: float = 0.0,
) -> StepResult:
    return StepResult(
        step.id,
        Status.ERROR,
        error=message,
        details=details,
        messages=list(messages or []),
        seconds=seconds,
        inherited=inherited,
    )


def _unfilled(param: P.Param, value: Any) -> Any:
    """"?" for a setting that must be filled in and is still empty (for the step's summary)."""
    if not (param.required and param.is_empty(value)):
        return value
    return ["?"] if param.kind in ("columns", "tables") else "?"


def _unfilled_rows(param: P.List, rows: list) -> list:
    """The rows of a list setting with "?" for their empty fields that must be filled in; a
    list that needs rows and has none gets one."""
    if not rows and (param.min_items or param.required):
        rows = [param.new_row()]
    shown = []
    for row in rows:
        row = Params(row) if isinstance(row, Mapping) else row
        if isinstance(row, Params):
            for field_ in param.fields:
                if P.is_visible(field_, row) and field_.accepts(row.get(field_.key)):
                    row[field_.key] = _unfilled(field_, row.get(field_.key))
        shown.append(row)
    return shown


def _owner(action: Action) -> str:
    return f"The plugin '{action.plugin}'" if action.plugin else f"'{action.name}'"


def _imported(ctx: Context) -> Imported | None:
    """The files an import read from a folder, as its context has them (``source_files``)."""
    if ctx.imported_files is None or ctx.imported_folder is None:
        return None
    try:
        return Imported(Path(ctx.imported_folder), tuple(Path(path) for path in ctx.imported_files))
    except TypeError:  # set by a plugin to something else
        return None


def _empty_and_skipped(deps: Mapping[str, TableResult]) -> str | None:
    """The first used table that skipped its rest and has no rows (a used table whose data has
    rows is used as usual)."""
    for ref, dependency in deps.items():
        data = dependency.df
        if dependency.skipped_from is not None and data is not None and data.height == 0:
            return ref
    return None


def _dependency_error(name: str, dependency: TableResult) -> str:
    """Name the table where the problem really is, instead of nesting "has an error" messages."""
    if dependency.error:
        return dependency.error if dependency.error.startswith("Circular") else f"Table '{name}': {dependency.error}"
    failed = next(s for s in dependency.steps if s.status == Status.ERROR)
    return failed.error if failed.inherited else f"Table '{name}' has an error: {failed.error}"


def _friendly(error: BaseException) -> str:
    """First paragraph of an unexpected error; Polars/DuckDB append long query plans."""
    text = str(error).strip()
    for marker in ("\n\nResolved plan", "\n\nThis error occurred", "\n\nLINE "):
        text = text.split(marker)[0]
    text = text.split("\n\n")[0].strip()
    return f"{type(error).__name__}: {text}" if text else type(error).__name__

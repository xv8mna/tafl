"""A random check of the table statuses that the window shows.

It builds small random projects (3 to 8 tables that import files, some through a parameter, or
start from other tables, merge and append each other and skip their rest when a table is empty,
some of them off, some in a circular reference), edits them through a real session as the window
does (offscreen: nothing is shown), and each time the session is quiet after an operation,
compares the status of every table that has one, its data or its message, with what a new engine
calculates for the same project. Run it before a release (a seed takes about 3 seconds):

    .venv\\Scripts\\python tools\\check_statuses.py                          (seeds 1 to 100, at most 30 minutes)
    .venv\\Scripts\\python tools\\check_statuses.py --seeds 500 --start 1000 --minutes 60
    .venv\\Scripts\\python tools\\check_statuses.py --seeds 1 --start 1234 --verbose   (one seed, each operation)
    .venv\\Scripts\\python tools\\check_statuses.py --seeds 1 --start 1234 --shrink    (and a shorter failing list)

The operations are those of the window: click a table, add, change, turn off, move and delete
steps, add, duplicate, link, rename, turn off and delete tables, Undo and Redo, the project map on
and off (on it, Del also deletes the steps of arrows into other tables), Calculate all, Run this
table, Run and Stop, Reload data, another value of the parameter, and a file that changes before
Reload data. Some follow the one before at once or while its calculation runs.

A seed gives the same project and the same operations, but the moments at which the calculations
in the background end can differ: a failure that depends on them may not come again each time.
A failure prints the seed, the operations up to it and the tables whose status is wrong. Exit
code 0: no failure; 1: a failure.
"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tempfile
import threading
import time
import traceback
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QSettings
from PySide6.QtWidgets import QApplication

from tafl.api import Mode, P, action
from tafl.core import Engine, Parameter, Project, Registry, Table
from tafl.ui.collector import Collector
from tafl.ui.session import LINK_KINDS, Session

FILES = {"a.csv": "Id;Name\n1;Anna\n2;Ben\n", "b.csv": "Id;Name\n2;Carla\n3;Dieter\n4;Emil\n"}
MISSING = "missing.csv"  # a file that isn't there: the import has an error
# What b.csv can become before Reload data: other rows, no Id column (the merges fail), no rows
CHANGED_B = ("Id;Name\n2;Carla\n3;Dieter\n4;Emil\n", "Id;Name\n5;Fritz\n", "Key;Name\n1;X\n", "Id;Name\n")
QUIET_SECONDS = 60  # longer than that busy is a failure
GAPS = (("then", 55), ("at once", 20), ("while it calculates", 10), ("a moment later", 15))
MAX_TABLES, MAX_STEPS = 10, 8


@action("check.pause", "Pause", params=[P.Int("ms", "Milliseconds", default=30)])
def pause(df, p, ctx):
    """Passes the table on after a moment, in the background calculations of the session, so that
    the next operation can come while one runs. The check's own calculation runs on the main
    thread and doesn't wait."""
    if threading.current_thread() is not threading.main_thread():
        time.sleep(p.ms / 1000)
    return df


@dataclass
class Operation:
    """What the window does, with tables by name; ``then``: how the next operation follows."""

    kind: str
    args: tuple = ()
    then: str = "then"
    seconds: float = 0.0  # for "a moment later"

    def __str__(self) -> str:
        text = self.text()
        if self.then == "a moment later":
            return f"{text}; {self.seconds * 1000:.0f} ms later:"
        return f"{text}; {self.then}:" if self.then != "then" else text

    def text(self) -> str:
        kind, args = self.kind, [_shown(a) for a in self.args]
        on = "on" if self.args and self.args[-1] is True else "off"
        texts = {
            "select": lambda: f"select {args[0]}",
            "rename": lambda: f"rename {args[0]} to {args[1]}",
            "settings": lambda: f"settings of step {args[1]} of {args[0]}: {args[2]}",
            "step on/off": lambda: f"turn step {args[1]} of {args[0]} on/off",
            "delete step": lambda: f"delete step {args[1]} of {args[0]}",
            "move step": lambda: f"move step {args[1]} of {args[0]} to {args[2]}",
            "add step": lambda: f"add step to {args[0]}: {self.args[1]} {args[2]}",
            "new table": lambda: f"new table, import of {args[0]}",
            "derived table": lambda: f"new table starting from {args[0]}",
            "duplicate": lambda: f"duplicate {args[0]}",
            "link": lambda: f"link {args[0]} → {args[1]} ({self.args[2]})",
            "delete tables": lambda: f"delete tables {args[0]}",
            "turn tables": lambda: f"turn tables {args[0]} {on}",
            "map": lambda: f"map {on}",
            "calculate all": lambda: f"calculate all {on}",
            "run": lambda: f"run {args[0]}" if args else "run the project",
            "parameter": lambda: f"parameter F = {args[0]}",
            "file and reload": lambda: f"b.csv becomes {_shown(CHANGED_B[self.args[0]])}, then Reload data",
        }
        return texts[kind]() if kind in texts else kind


@dataclass
class Failure:
    seed: int
    operations: list[Operation]
    problems: list[str]
    project: Path | None = None
    tries: list[bool] = field(default_factory=list)


def _shown(value: Any) -> str:
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_shown(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(map(_shown, value)) + "]"
    return f"“{value}”" if isinstance(value, str) else str(value)


# --- the project ---------------------------------------------------------------------------------


def random_project(rng: random.Random, registry: Registry) -> Project:
    """3 to 8 tables T1, T2, ...: each imports a file (b.csv also as {F}, a parameter) or starts
    from another table (any table, so circular references happen), then up to 3 more steps; some
    tables are off."""
    actions = registry.actions
    project = Project(name="Check")
    project.parameters = [Parameter("F", "b.csv")]
    tables = [project.add_table(f"T{number}") for number in range(1, rng.randint(3, 8) + 1)]
    names = [t.name for t in tables]
    for table in tables:
        if rng.random() < 0.45:
            file = _file(rng)
            project.add_step(table, actions["csv.import"], {"file": "{F}" if file == "b.csv" and rng.random() < 0.5 else file})
        else:
            project.add_step(table, actions["table.derive"], {"table": rng.choice([n for n in names if n != table.name])})
        for _ in range(rng.randint(0, 3)):
            action_id, params = random_step(rng, names, table.name)
            project.add_step(table, actions[action_id], params)
    for table in tables:
        for step in table.steps:  # the app stores ids; a project file written by hand may hold names
            for key in ("table", "other"):
                if key in step.params and rng.random() < 0.9:
                    step.params[key] = project.table(step.params[key]).id
            if "tables" in step.params:
                step.params["tables"] = [project.table(n).id if rng.random() < 0.9 else n for n in step.params["tables"]]
        table.enabled = rng.random() > 0.15
    return project


def _file(rng: random.Random) -> str:
    return rng.choice(["a.csv", "a.csv", "b.csv", "b.csv", MISSING])


def random_step(rng: random.Random, names: list[str], own: str) -> tuple[str, dict[str, Any]]:
    """An action and its settings, with the tables it uses by name (``with_ids`` makes them ids)."""
    others = [n for n in names if n != own] or [own]
    if rng.random() < 0.3:  # a step that skips the rest of the table: of this table, or when another one is empty
        kind = rng.choice(["skip", "skip other", "no rows"])
        if kind == "skip":
            return "flow.skip_if_empty", {"which": "this"}
        if kind == "skip other":
            return "flow.skip_if_empty", {"which": "other", "table": rng.choice(others)}
        return "rows.filter_formula", {"condition": "[Id] > 100"}  # no rows: the next skip of this table skips
    kind = rng.choice(["filter", "filter", "calculate", "merge", "merge", "append", "pause", "incomplete"])
    if kind == "filter":
        return "rows.filter_formula", {"condition": rng.choice(["[Id] > 1", "[Id] < 4", "[Missing] > 1"])}
    if kind == "calculate":
        return "calc.columns", {"columns": [{"name": "Twice", "formula": rng.choice(["[Id] * 2", "[Missing] * 2"])}]}
    if kind == "merge":
        match = [{"column": "Id", "other_column": "Id"}]
        return "table.merge", {"other": rng.choice(others), "how": rng.choice(["lookup", "inner", "matching"]), "match": match}
    if kind == "append":
        return "table.append", {"tables": rng.sample(others, rng.randint(1, min(2, len(others))))}
    if kind == "pause":
        return "check.pause", {"ms": rng.choice([10, 30, 80])}
    return "table.merge", {}  # not filled in yet: incomplete, and the tables that use it wait


def with_ids(project: Project, params: dict[str, Any]) -> dict[str, Any] | None:
    """The settings with table ids instead of names; None if a table is gone."""
    found = dict(params)
    for key in ("table", "other"):
        if key in found:
            table = _named(project, found[key])
            if table is None:
                return None
            found[key] = table.id
    if "tables" in found:
        tables = [_named(project, name) for name in found["tables"]]
        if None in tables:
            return None
        found["tables"] = [t.id for t in tables]
    return found


def _named(project: Project, name: str) -> Table | None:
    return next((t for t in project.tables if t.name == name), None)


# --- the operations ------------------------------------------------------------------------------


def random_operation(rng: random.Random, session: Session) -> Operation:
    """One operation of the window on the open project."""
    project = session.project
    names = [t.name for t in project.tables]
    selected = session.table
    if rng.random() < 0.06:  # another value of the parameter, or a file that changes before Reload data
        kind = rng.choice(["parameter", "file and reload"])
        then = rng.choices([g for g, _ in GAPS], [w for _, w in GAPS])[0]
        return Operation(kind, _arguments(rng, session, kind), then, rng.uniform(0, 0.2))
    choices: list[tuple[str, int]] = [("undo", 10), ("redo", 6), ("map", 6), ("reload", 2), ("run", 3), ("stop", 1)]
    if names:
        choices += [("select", 14), ("turn tables", 6), ("rename", 3), ("duplicate", 2), ("derived table", 3)]
        choices += [("link", 5), ("delete tables", 3), ("calculate all", 2)]
    if len(names) < MAX_TABLES:
        choices.append(("new table", 2))
    if selected is not None:
        choices += [("settings", 10), ("step on/off", 5), ("delete step", 6), ("move step", 3)]
        if len(selected.steps) < MAX_STEPS:
            choices.append(("add step", 8))
    kind = rng.choices([c for c, _ in choices], [w for _, w in choices])[0]
    then = rng.choices([g for g, _ in GAPS], [w for _, w in GAPS])[0]
    operation = Operation(kind, _arguments(rng, session, kind), then, rng.uniform(0, 0.2))
    return operation


def _arguments(rng: random.Random, session: Session, kind: str) -> tuple:
    project, selected = session.project, session.table
    names = [t.name for t in project.tables]
    if kind == "select":
        return (rng.choice(names),)
    if kind == "turn tables":
        chosen = rng.sample(names, rng.randint(1, min(2, len(names))))
        return chosen, not all(_named(project, n).enabled for n in chosen)
    if kind == "rename":
        number = 1
        while f"R{number}" in names:
            number += 1
        return rng.choice(names), f"R{number}"
    if kind in ("duplicate", "derived table"):
        return (rng.choice(names),)
    if kind == "link":
        source = rng.choice(names)
        return source, rng.choice([n for n in names if n != source] or names), rng.choice(sorted(LINK_KINDS))
    if kind == "delete tables":
        return (rng.sample(names, rng.randint(1, min(2, len(names) - 1))) if len(names) > 1 else [],)
    if kind == "calculate all":
        return (rng.random() < 0.6,)
    if kind == "new table":
        return (_file(rng),)
    if kind == "map":
        return (not session.overview,)
    if kind == "run":
        return (rng.choice(names),) if names and rng.random() < 0.7 else ()
    if kind == "parameter":
        return (rng.choice(["a.csv", "b.csv", MISSING]),)
    if kind == "file and reload":
        return (rng.randrange(len(CHANGED_B)),)
    if kind == "add step":
        return selected.name, *random_step(rng, names, selected.name)
    if kind in ("settings", "step on/off", "move step") or kind == "delete step":
        table = selected
        if kind == "delete step" and session.overview and rng.random() < 0.5:  # Del on arrows on the map
            table = rng.choice(project.tables)
        if not table.steps:
            return (table.name, 0)
        number = rng.randint(1, len(table.steps))
        if kind == "settings":
            return table.name, number, _new_settings(rng, table.steps[number - 1], names, table.name)
        if kind == "move step":
            return table.name, number, rng.randint(1, len(table.steps))
        return table.name, number
    return ()


def _new_settings(rng: random.Random, step, names: list[str], own: str) -> dict[str, Any]:
    """Other settings for the step, of the same action."""
    others = [n for n in names if n != own] or [own]
    if step.action == "csv.import":
        return {"file": _file(rng)}
    if step.action == "table.derive":
        return {"table": rng.choice(others)}
    if step.action == "table.merge":
        if rng.random() < 0.2:
            return {"how": rng.choice(["lookup", "inner", "matching"])}
        return {"other": rng.choice(others), "match": [{"column": "Id", "other_column": "Id"}]}
    if step.action not in ("table.append", "rows.filter_formula", "calc.columns", "check.pause", "flow.skip_if_empty"):
        return {}
    while True:  # another step of the same action
        action_id, params = random_step(rng, names, own)
        if action_id == step.action:
            return params


def apply(operation: Operation, session: Session, registry: Registry) -> bool:
    """Do the operation; False if it can't be done now (a table it names is gone)."""
    project, kind, args = session.project, operation.kind, operation.args
    if kind == "undo":
        session.undo()
    elif kind == "redo":
        session.redo()
    elif kind == "map":
        session.set_overview(args[0])
    elif kind == "reload":
        session.reload_sources()
    elif kind == "parameter":
        session.set_parameters([Parameter("F", args[0])])
    elif kind == "file and reload":
        (project.directory / "b.csv").write_text(CHANGED_B[args[0]], encoding="utf-8")
        session.reload_sources()
    elif kind == "calculate all":
        session.calculate_all(args[0])
    elif kind == "stop":
        session.cancel_run()
    elif kind == "new table":
        table = session.add_table(registry.actions["csv.import"])
        session.set_params(table.steps[0].id, {"file": args[0]})
    elif kind == "run":
        tables = [_named(project, args[0])] if args else None
        if tables == [None]:
            return False
        session.run_project(None if tables is None else [t.id for t in tables])
    elif kind in ("turn tables", "delete tables"):
        tables = [_named(project, name) for name in args[0]]
        if None in tables or not tables:
            return False
        if kind == "turn tables":
            session.set_table_enabled([t.id for t in tables], args[1])
        else:
            session.delete_tables([t.id for t in tables])
    elif kind == "link":
        source, target = _named(project, args[0]), _named(project, args[1])
        if source is None or target is None:
            return False
        session.add_link(source.id, target.id, args[2])
    else:
        table = _named(project, args[0])
        if table is None:
            return False
        if kind == "select":
            session.select_table(table.id)
        elif kind == "rename":
            if project.find_table(args[1]) is not None:
                return False
            session.rename_table(table.id, args[1])
        elif kind == "duplicate":
            session.duplicate_table(table.id)
        elif kind == "derived table":
            session.add_derived_table(table.id)
        elif kind == "add step":
            params = with_ids(project, args[2])
            if params is None or table is not session.table:
                return False
            session.add_step(registry.actions[args[1]], params=params)
        else:
            return _apply_to_step(operation, session, table)
    return True


def _apply_to_step(operation: Operation, session: Session, table: Table) -> bool:
    kind, args = operation.kind, operation.args
    number = args[1]
    if not 1 <= number <= len(table.steps):
        return False
    step = table.steps[number - 1]
    if kind == "delete step":
        if table is not session.table and not session.overview:
            return False  # only Del on the map's arrows deletes a step of another table
        session.delete_step(step.id)
        return True
    if table is not session.table:
        return False
    if kind == "settings":
        params = with_ids(session.project, args[2])
        if params is None:
            return False
        session.set_params(step.id, params)
    elif kind == "step on/off":
        session.set_enabled(step.id, not step.enabled)
    elif kind == "move step":
        session.move_step(step.id, args[2] - 1)
    return True


# --- running and comparing -----------------------------------------------------------------------


class Checker:
    def __init__(self, app: QApplication, registry: Registry, folder: Path, verbose: bool = False) -> None:
        self.app = app
        self.registry = registry
        self.folder = folder
        self.verbose = verbose
        self.session = Session(registry)
        self.errors: list[str] = []  # unexpected problems that the session reports (tracebacks)
        self.session.error.connect(self.errors.append)
        self.checks = 0
        self.operations = 0

    def run_seed(self, seed: int, count: int, replay: list[Operation] | None = None) -> Failure | None:
        """The seed's project and ``count`` random operations (or those of ``replay``)."""
        rng = random.Random(seed)
        folder = self.folder / f"seed{seed}"
        shutil.rmtree(folder, ignore_errors=True)
        folder.mkdir(parents=True)
        for name, text in FILES.items():
            (folder / name).write_text(text, encoding="utf-8")
        project = random_project(rng, self.registry)
        self.errors.clear()
        self.session.open_project(project.save(folder / "check.tafl"), trusted=True)
        done: list[Operation] = []
        problems = self.quiet_problems()
        planned = replay if replay is not None else [None] * count
        for planned_operation in planned:
            if problems:
                break
            operation = planned_operation or random_operation(rng, self.session)
            try:
                if not apply(operation, self.session, self.registry):
                    continue
            except Exception as error:  # noqa: BLE001 - the session must not fail on an operation
                problems = [f"{operation}: {type(error).__name__}: {error}"]
                done.append(operation)
                break
            done.append(operation)
            self.operations += 1
            if self.verbose:
                print(f"  {len(done)}. {operation}", flush=True)
            problems = self.follow(operation)
        if not problems:
            problems = self.quiet_problems()
        if not problems:
            return None
        saved = folder / "failure.tafl"
        Project.from_dict(self.session.project.to_dict()).save(saved)
        return Failure(seed, done, problems, saved)

    def follow(self, operation: Operation) -> list[str]:
        """Waits as the operation says; after "then", for a quiet session, and compares."""
        if operation.then == "at once":
            return []
        if operation.then == "while it calculates":
            self.wait(lambda: self.session.busy and not self.session._timer.isActive(), 1.0)
            return []
        if operation.then == "a moment later":
            self.wait(lambda: False, operation.seconds)
            return []
        return self.quiet_problems()

    def wait(self, done, seconds: float) -> bool:
        end = time.monotonic() + seconds
        while True:
            self.app.processEvents()
            if done():
                return True
            if time.monotonic() > end:
                return False
            time.sleep(0.003)

    def quiet(self) -> bool:
        session = self.session
        return not (session.busy or session.running or session._timer.isActive())

    def quiet_problems(self) -> list[str]:
        if not self.wait(self.quiet, QUIET_SECONDS):
            return [f"the session is still busy after {QUIET_SECONDS} s"]
        self.checks += 1
        problems = [f"the session reported a problem: {text.strip().splitlines()[-1]}" for text in self.errors]
        return problems + wrong_statuses(self.session, self.registry)


def wrong_statuses(session: Session, registry: Registry) -> list[str]:
    """The tables whose status is not what a new engine calculates, and those whose data (ok), or
    whose message (skipped, error, incomplete, waiting) is another one."""
    project = Project.from_dict(session.project.to_dict(), path=session.project.path)
    fresh = Session(registry)  # its statuses come from the new results; it calculates nothing itself
    fresh.project, fresh.engine = project, Engine(project, registry)
    fresh.results = fresh.engine.run(mode=Mode.PREVIEW)
    problems = []
    try:
        for table in session.project.tables:
            shown, right = session.table_status(table.id), fresh.table_status(table.id)
            mine, new = session.results.get(table.id), fresh.results[table.id]
            if shown == "pending":
                if session.overview and table.enabled:
                    problems.append(f"{table.name}: not calculated on the map, right: {right}")
            elif shown != right:
                problems.append(f"{table.name}: {shown}, right: {right}")
            elif shown == "ok" and not mine.df.equals(new.df):
                problems.append(f"{table.name}: ok, but {_size(mine)} instead of {_size(new)}, or other values")
            elif shown == "skipped" and mine.skipped != new.skipped:
                problems.append(f"{table.name}: skipped, “{mine.skipped}”, right: “{new.skipped}”")
            elif shown in ("error", "incomplete", "waiting") and mine.first_error != new.first_error:
                problems.append(f"{table.name}: {shown}, “{mine.first_error}”, right: “{new.first_error}”")
            elif shown == "waiting" and session.waits_for(table.id) != fresh.waits_for(table.id):
                problems.append(f"{table.name}: waits for {session.waits_for(table.id)}, right: {fresh.waits_for(table.id)}")
    finally:
        fresh.deleteLater()
        QApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # no event loop runs here to do it
    return problems


def _size(result) -> str:
    rows, columns = result.df.shape
    return f"{rows} × {columns}"


def shrink(checker: Checker, failure: Failure, tries: int = 3, passes: int = 3) -> Failure:
    """Leaves out one operation after another, from the last, where the rest still fails (in one
    try), in up to ``passes`` rounds; then runs the shortest list ``tries`` times."""
    operations = list(failure.operations)
    for _ in range(passes):
        before = len(operations)
        index = len(operations) - 1
        while index >= 0 and len(operations) > 1:
            found = checker.run_seed(failure.seed, 0, operations[:index] + operations[index + 1 :])
            if found is not None:
                operations, failure = found.operations, found
            index = min(index, len(operations)) - 1
        if len(operations) == before:
            break
    failure.tries = [checker.run_seed(failure.seed, 0, operations) is not None for _ in range(tries)]
    return failure


def report(failure: Failure, title: str = "FAILURE") -> None:
    print(f"\n{title}: seed {failure.seed}, {len(failure.operations)} operation(s)", flush=True)
    for number, operation in enumerate(failure.operations, start=1):
        print(f"  {number}. {operation}")
    for problem in failure.problems:
        print(f"  wrong: {problem}")
    if failure.tries:
        print(f"  fails again in {sum(failure.tries)} of {len(failure.tries)} tries")
    if failure.project is not None:
        print(f"  the project at the end: {failure.project}")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", type=int, default=100, help="how many seeds (default 100)")
    parser.add_argument("--start", type=int, default=1, help="the first seed (default 1)")
    parser.add_argument("--minutes", type=float, default=30, help="no new seed after this time (default 30)")
    parser.add_argument("--operations", type=int, default=30, help="operations per seed (default 30)")
    parser.add_argument("--shrink", action="store_true", help="look for a shorter list of operations that fails")
    parser.add_argument("--verbose", action="store_true", help="print each operation")
    parser.add_argument("--folder", type=Path, help="for the projects (default: a temporary folder, deleted at the end)")
    args = parser.parse_args()

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    settings = tempfile.mkdtemp(prefix="tafl-check-settings-")
    QSettings.setDefaultFormat(QSettings.Format.IniFormat)
    QSettings.setPath(QSettings.Format.IniFormat, QSettings.Scope.UserScope, settings)
    app = QApplication([])
    app.setOrganizationName("Tafl Check")
    collector = Collector()  # as in the app: reference cycles are collected on this thread only
    registry = Registry()
    registry.load_builtin()
    module = types.ModuleType("check_pauses")
    module.pause = pause
    registry.add_module(module)
    folder = args.folder or Path(tempfile.mkdtemp(prefix="tafl-check-"))
    checker = Checker(app, registry, folder, args.verbose)
    printed = sys.excepthook

    def unexpected(kind, error, trace) -> None:
        """An error in a slot of the session (a signal handler) is printed, not raised: it counts too."""
        checker.errors.append("".join(traceback.format_exception(kind, error, trace)))
        printed(kind, error, trace)

    sys.excepthook = unexpected
    started = time.monotonic()
    failures: list[Failure] = []
    seeds = 0
    try:
        for seed in range(args.start, args.start + args.seeds):
            if time.monotonic() - started > args.minutes * 60:
                print(f"time limit reached after {seeds} seed(s)")
                break
            if args.verbose:
                print(f"seed {seed}", flush=True)
            failure = checker.run_seed(seed, args.operations)
            seeds += 1
            if failure is not None:
                report(failure)
                if args.shrink:
                    report(shrink(checker, failure), "SHORTER")
                failures.append(failure)
            if seeds % 10 == 0:
                print(f"{seeds} seed(s), {checker.operations} operations, {len(failures)} failure(s), "
                      f"{time.monotonic() - started:.0f} s", flush=True)
    finally:
        sys.excepthook = printed
        checker.session.wait()
        collector.stop()
        shutil.rmtree(settings, ignore_errors=True)
        if args.folder is None and not failures:
            shutil.rmtree(folder, ignore_errors=True)
    print(
        f"checked {seeds} seed(s) from {args.start}: {checker.operations} operations, {checker.checks} checks, "
        f"{len(failures)} failure(s) {[f.seed for f in failures]}, {time.monotonic() - started:.0f} s"
    )
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

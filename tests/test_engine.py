import os
import threading
import time
import types

import duckdb
import polars as pl
import pytest

from tafl.api import ActionError, Cancelled, P, action, expressions
from tafl.core import Engine, Project, Registry, ResultCache, Status
from tafl.core.cancel import CancelEvent

CSV = "Id;Customer;Amount;Date\n1;A;10,5;01.01.2025\n2;B;20;02.01.2025\n3;A;5;03.01.2025\n"


@pytest.fixture
def project(tmp_path) -> Project:
    (tmp_path / "orders.csv").write_text(CSV, encoding="utf-8")
    project = Project(name="Test")
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    project.add_step(orders, "rows.filter_formula", {"condition": "[Amount] > 6"})
    project.add_step(orders, "calc.columns", {"columns": [{"name": "Gross", "formula": "ROUND([Amount] * 1.19, 2)"}]})
    project.add_step(orders, "csv.export", {"file": "out/orders.csv"})
    return project


def statuses(result):
    return [s.status for s in result.steps]


def test_preview_calculates_and_skips_exports(project, registry, tmp_path):
    result = Engine(project, registry).evaluate("Orders")
    assert result.ok
    assert statuses(result) == [Status.OK, Status.OK, Status.OK, Status.SKIPPED]
    assert result.df.to_dicts() == [
        {"Id": 1, "Customer": "A", "Amount": 10.5, "Date": pl.Series(["2025-01-01"]).str.to_date()[0], "Gross": 12.5},
        {"Id": 2, "Customer": "B", "Amount": 20.0, "Date": pl.Series(["2025-01-02"]).str.to_date()[0], "Gross": 23.8},
    ]
    assert not (tmp_path / "out").exists()


def test_run_executes_exports(project, registry, tmp_path):
    engine = Engine(project, registry)
    engine.evaluate("Orders")
    results = engine.run()
    assert results["t1"].ok
    assert not any(s.cached for s in results["t1"].steps)  # a run reads everything again
    preview = engine.evaluate("Orders")  # ... and the preview afterwards reuses the run's results
    assert [s.cached for s in preview.steps[:3]] == [True, True, True]
    written = (tmp_path / "out" / "orders.csv").read_text(encoding="utf-8-sig")
    assert written.splitlines()[0] == "Id;Customer;Amount;Date;Gross"
    assert written.splitlines()[1] == "1;A;10,5;01.01.2025;12,5"


def test_only_changed_steps_are_recalculated(project, registry):
    engine = Engine(project, registry)
    engine.evaluate("Orders")
    again = engine.evaluate("Orders")
    assert [s.cached for s in again.steps[:3]] == [True, True, True]

    project.tables[0].steps[2].params["columns"][0]["formula"] = "[Amount] * 2"
    changed = engine.evaluate("Orders")
    assert [s.cached for s in changed.steps[:3]] == [True, True, False]
    assert changed.df["Gross"].to_list() == [21.0, 40.0]


def test_a_cached_step_keeps_its_messages(project):
    module = types.ModuleType("talking")

    @action("demo.talk", "Talk")
    def talk(df, p, ctx):
        ctx.warn("Something to know")
        return df.with_columns(pl.lit(1).alias("one"))

    @action("demo.check", "Check")
    def check(df, p, ctx):
        ctx.log("Looked at it")
        return df  # passed through: the result stays the one of the step before

    module.talk, module.check = talk, check
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step("Orders", "demo.talk", {}, index=1)
    project.add_step("Orders", "demo.check", {}, index=2)
    engine = Engine(project, registry)
    first = engine.evaluate("Orders")
    again = engine.evaluate("Orders")
    assert again.steps[1].cached and again.steps[1].messages == first.steps[1].messages == [("warning", "Something to know")]
    assert again.steps[2].messages == [("info", "Looked at it")]
    assert again.steps[0].cached and again.steps[0].messages == first.steps[0].messages  # the import's own message
    assert first.steps[0].messages


def test_summaries_show_what_is_still_to_fill_in(project, registry):
    engine = Engine(project, registry)

    def summary(action_id, params):
        return engine.describe(project.add_step("Orders", registry.actions[action_id], params))

    assert summary("rows.filter_formula", {"condition": ""}) == "Keep rows where ?"
    assert summary("calc.columns", {"columns": [{"name": "", "formula": ""}]}) == "Calculate ?"
    assert summary("calc.columns", {"columns": [{"name": "Net", "formula": ""}]}) == "Calculate Net"
    assert summary("table.merge", {"how": "lookup"}) == "Lookup ? on ?"
    assert summary("table.merge", {"how": "lookup", "other": "t1", "match": [{"column": "Id"}]}) == "Lookup Orders on Id"
    assert summary("columns.select", {"columns": []}) == "Keep ?"
    assert summary("csv.export", {"file": ""}) == "Export to ?"
    assert summary("rows.sort", {"by": [{"column": "Id", "order": "desc"}]}) == "Sort by Id ↓"


def test_source_file_change_is_picked_up(project, registry, tmp_path):
    engine = Engine(project, registry)
    assert engine.evaluate("Orders").df.height == 2
    (tmp_path / "orders.csv").write_text(CSV + "4;C;99;04.01.2025\n", encoding="utf-8")
    result = engine.evaluate("Orders")
    assert not result.steps[0].cached
    assert result.df["Id"].to_list() == [1, 2, 4]


def test_reload_rereads_sources(project, registry):
    engine = Engine(project, registry)
    engine.evaluate("Orders")
    engine.reload()
    assert not engine.evaluate("Orders").steps[0].cached


def test_error_blocks_later_steps(project, registry):
    project.tables[0].steps[1].params["condition"] = "[Amout] > 6"
    result = Engine(project, registry).evaluate("Orders")
    assert not result.ok and result.df is None
    assert statuses(result) == [Status.OK, Status.ERROR, Status.BLOCKED, Status.BLOCKED]
    assert "column [Amout] not found (did you mean [Amount]?)" in result.first_error


def test_settings_are_checked_before_running(project, registry):
    project.add_step("Orders", "rows.sort", {"by": [{"column": "amount"}]}, index=2)
    result = Engine(project, registry).evaluate("Orders")
    assert result.steps[2].error == "Sort by, row 1 – Column: column 'amount' not found (did you mean 'Amount'?)"


def test_settings_of_the_wrong_kind_are_step_errors(project, registry):
    """E.g. from a project file written by hand: a number where a table belongs."""
    derived = project.add_table("Derived")
    step = project.add_step(derived, "table.derive", {"table": 5})
    match = [{"column": "Id", "other_column": "Id"}]
    project.add_step(derived, "table.merge", {"other": ["t1"], "match": match, "columns": 3})
    engine = Engine(project, registry)
    assert engine.links(derived.id) == [] and engine.describe(step) == "Start from 5"
    assert engine.evaluate("Derived").steps[0].error == "Table: 5 is not a table"
    step.params["table"] = "t1"
    errors = engine.evaluate("Derived").steps[1].error.splitlines()
    assert errors == ['Other table: ["t1"] is not a table', "Columns to add (empty = all): 3 is not a list of columns"]


def test_unknown_action_shows_error_and_survives_saving(project, registry, tmp_path):
    project.add_step("Orders", "fancy.plugin", {"keep": "me"})
    result = Engine(project, registry).evaluate("Orders")
    assert "Unknown action 'fancy.plugin'" in result.steps[-1].error
    project.save()
    reloaded = Project.load(project.path)
    assert reloaded.tables[0].steps[-1].params == {"keep": "me"}


def test_step_position_rules(project, registry):
    project.add_step("Orders", "csv.import", {"file": "orders.csv"})
    assert "can only be the first step" in Engine(project, registry).evaluate("Orders").steps[-1].error
    lonely = project.add_table("Lonely")
    project.add_step(lonely, "rows.sort", {"by": [{"column": "x"}]})
    assert "needs an input table" in Engine(project, registry).evaluate("Lonely").steps[0].error
    empty = project.add_table("Empty")
    assert Engine(project, registry).evaluate(empty.id).error == "The table has no steps yet"


def test_disabled_step_passes_data_through(project, registry):
    project.tables[0].steps[1].enabled = False
    result = Engine(project, registry).evaluate("Orders")
    assert result.steps[1].status == Status.DISABLED
    assert result.df.height == 3


def test_until_calculates_only_the_first_steps(project, registry):
    result = Engine(project, registry).evaluate("Orders", until=0)
    assert len(result.steps) == 1 and result.df.height == 3


def test_dependencies_and_order(project, registry):
    customers = project.add_table("Customers")
    project.add_step(customers, "table.derive", {"table": "t1"})
    project.add_step(customers, "calc.aggregate", {"group_by": ["Customer"], "calculations": [{"function": "sum", "column": "Amount"}]})
    report = project.add_table("Report")
    project.add_step(report, "table.derive", {"table": customers.id})
    project.tables.reverse()  # order in the project doesn't matter
    engine = Engine(project, registry)
    assert engine.dependencies(report.id) == [customers.id]
    assert engine.execution_order() == ["t1", customers.id, report.id]
    results = engine.run(mode="preview")
    assert results[report.id].df.to_dicts() == [{"Customer": "A", "Sum of Amount": 10.5}, {"Customer": "B", "Sum of Amount": 20.0}]
    assert engine.describe(customers.steps[0]) == "Start from Orders"


def test_referenced_table_errors_are_reported(project, registry):
    project.tables[0].steps[1].params["condition"] = "nonsense ("
    other = project.add_table("Other")
    project.add_step(other, "table.derive", {"table": "t1"})
    result = Engine(project, registry).evaluate("Other")
    assert result.steps[0].error.startswith("Table 'Orders' has an error: Formula error")
    # one level further: still points at Orders, no nested messages
    third = project.add_table("Third")
    project.add_step(third, "table.derive", {"table": other.id})
    assert Engine(project, registry).evaluate("Third").first_error == result.steps[0].error


def test_circular_references_are_detected(project, registry):
    a, b = project.add_table("A"), project.add_table("B")
    project.add_step(a, "table.derive", {"table": b.id})
    project.add_step(b, "table.derive", {"table": a.id})
    result = Engine(project, registry).evaluate("A")
    assert result.first_error == "Circular reference: A → B → A"


def test_old_step_settings_are_migrated(project):
    module = types.ModuleType("versioned")

    def migrate(params, from_version):
        if from_version < 2:
            params["factor"] = params.pop("multiplier")
        return params

    @action("demo.scale", "Scale", params=[P.Int("factor", "Factor", default=1)], version=2, migrate=migrate)
    def scale(df, p, ctx):
        return df.with_columns(pl.col("Amount") * p.factor)

    module.scale = scale
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step("Orders", "demo.scale", {"multiplier": 10}, version=1)
    assert Engine(project, registry).evaluate("Orders").df["Amount"].to_list() == [105.0, 200.0]
    project.tables[0].steps[-1].version = 3
    assert "saved by a newer version" in Engine(project, registry).evaluate("Orders").steps[-1].error


def test_old_merge_steps_keep_their_behavior(project, registry, tmp_path):
    """A merge saved by version 1 with "all matches" must still be a left join after upgrading."""
    (tmp_path / "customers.csv").write_text("Customer;Region\nA;North\nA;South\n", encoding="utf-8")
    customers = project.add_table("Customers")
    project.add_step(customers, "csv.import", {"file": "customers.csv"})
    project.add_step(
        "Orders",
        "table.merge",
        {"other": customers.id, "match": [{"column": "Customer", "other_column": "Customer"}], "multiple": "all"},
        version=1,
        index=3,
    )
    engine = Engine(project, registry)
    before = engine.evaluate("Orders").df
    assert before["Region"].to_list() == ["North", "South", None]

    changed = engine.upgrade_steps()  # the imports (added without a version) are upgraded too
    step = project.tables[0].steps[3]
    assert [s for s in changed if s.action == "table.merge"] == [step]
    assert step.version == 2 and step.params["how"] == "left"
    assert engine.evaluate("Orders").df.equals(before)
    assert engine.upgrade_steps() == []


def test_add_step_records_the_action_version(project, registry):
    step = project.add_step("Orders", registry.actions["table.merge"], {})
    assert step.action == "table.merge" and step.version == 2


def test_snapshot_shares_the_cache_but_not_the_project(project, registry):
    engine = Engine(project, registry)
    engine.evaluate("Orders")
    twin = engine.snapshot()
    assert all(s.cached for s in twin.evaluate("Orders").steps[:3])
    twin.project.tables[0].name = "Changed"
    assert project.tables[0].name == "Orders"


def test_evaluate_all_returns_the_tables_used(project, registry):
    report = project.add_table("Report")
    project.add_step(report, "table.derive", {"table": "t1"})
    results = Engine(project, registry).evaluate_all("Report")
    assert set(results) == {"t1", report.id}
    assert results["t1"].df.height == 2


def test_cancel(project, registry):
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(Cancelled):
        Engine(project, registry).evaluate("Orders", cancel=cancel)


def test_relative_paths_follow_the_project_file(project, registry, tmp_path):
    moved = tmp_path / "moved"
    moved.mkdir()
    (moved / "orders.csv").write_text("Id;Customer;Amount;Date\n9;Z;100;01.01.2025\n", encoding="utf-8")
    project.save(moved / "test.tafl")
    assert Engine(project, registry).evaluate("Orders").df["Id"].to_list() == [9]
    assert os.path.exists(moved / "test.tafl")


def test_a_table_that_is_off_is_skipped_with_the_tables_using_it(project, registry, tmp_path):
    report = project.add_table("Report")
    project.add_step(report, "table.derive", {"table": "t1"})
    project.add_step(report, "csv.export", {"file": "out/report.csv"})
    summary = project.add_table("Summary")
    project.add_step(summary, "table.derive", {"table": report.id})
    project.tables[0].enabled = False
    results = Engine(project, registry).run()
    orders = results["t1"]
    assert orders.skipped == "turned off" and orders.steps == []
    assert not orders.ok and not orders.failed  # skipped is not an error
    assert results[report.id].skipped == "uses “Orders”, which is turned off"
    assert results[summary.id].skipped == "uses “Report”, which is skipped"
    assert not (tmp_path / "out").exists()  # no exports ran
    assert Engine(project, registry).evaluate(summary.id).skipped is not None  # the preview too

    data = project.to_dict()
    assert data["tables"][0]["enabled"] is False and "enabled" not in data["tables"][1]
    assert Project.from_dict(data).tables[0].enabled is False

    project.tables[0].enabled = True
    results = Engine(project, registry).run()
    assert all(r.ok for r in results.values()) and (tmp_path / "out" / "report.csv").exists()


def test_a_time_span_on_an_empty_table_is_a_step_error(project, registry):
    """It used to crash inside Polars and stop the calculation."""
    project.tables[0].steps[1].params["condition"] = "[Amount] > 1000"  # keeps no rows
    project.tables[0].steps[2].params["columns"] = [{"name": "Age", "formula": "GETDATE() - [Date]"}]
    result = Engine(project, registry).evaluate("Orders")
    assert result.steps[2].status == Status.ERROR and "time span" in result.steps[2].error


def test_a_crash_inside_polars_becomes_a_step_error(project):
    """Polars reports crashes as PanicException, which is not an Exception."""

    class PanicException(BaseException):  # recognized by its name, like the one from Polars
        pass

    module = types.ModuleType("crashing")

    @action("demo.crash", "Crash")
    def crash(df, p, ctx):
        raise PanicException("called `Result::unwrap()` on an `Err` value")

    module.crash = crash
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step("Orders", "demo.crash", {})
    result = Engine(project, registry).evaluate("Orders")
    assert result.steps[-1].status == Status.ERROR and "PanicException" in result.steps[-1].error


@pytest.fixture
def quitting_registry() -> Registry:
    """The built-in actions plus ones that try to end the program, and a slow one that fails."""
    module = types.ModuleType("quitting")

    @action("demo.exit", "Exit")
    def exit_(df, p, ctx):
        raise SystemExit("bye")

    @action("demo.interrupt", "Interrupt")
    def interrupt(df, p, ctx):
        raise KeyboardInterrupt

    @action("demo.slow_failure", "Slow failure", params=[P.Bool("unexpected", "Unexpected")])
    def slow_failure(df, p, ctx):
        time.sleep(0.2)
        if p.unexpected:
            raise RuntimeError("the connection timed out")
        raise ActionError("The server did not answer")

    module.exit_, module.interrupt, module.slow_failure = exit_, interrupt, slow_failure
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    return registry


def test_a_step_that_tries_to_end_the_program_is_a_step_error(project, quitting_registry):
    """sys.exit() or KeyboardInterrupt in a plugin's step used to end the app."""
    project.add_step("Orders", "demo.exit", {})
    result = Engine(project, quitting_registry).evaluate("Orders")
    assert result.steps[-1].status == Status.ERROR
    assert result.steps[-1].error == "The plugin 'quitting' tried to end the program (SystemExit: bye)"
    assert "SystemExit" in result.steps[-1].details

    project.tables[0].steps[-1].action = "demo.interrupt"
    result = Engine(project, quitting_registry).evaluate("Orders")
    assert result.steps[-1].error == "The plugin 'quitting' tried to end the program (KeyboardInterrupt)"


def _leaving_registry(error: BaseException) -> Registry:
    """The built-in actions plus ones whose migrate, summary and fingerprint raise ``error``."""

    def leave(*_):
        raise error

    module = types.ModuleType("leaving")
    module.old = action("demo.old", "Old", version=2, migrate=leave, summary=leave)(lambda df, p, ctx: df)
    module.source = action("demo.source", "Source", kind="source", fingerprint=leave)(lambda df, p, ctx: pl.DataFrame({"a": [1]}))
    module.titled = action("demo.titled", "Titled", summary=leave)(lambda df, p, ctx: df)
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    return registry


@pytest.mark.parametrize(
    ("error", "shown"), [(SystemExit("bye"), "SystemExit: bye"), (KeyboardInterrupt(), "KeyboardInterrupt")]
)
def test_plugin_code_besides_run_that_tries_to_end_the_program(project, error, shown):
    """A migrate, summary or fingerprint function that calls sys.exit() or raises
    KeyboardInterrupt fails like other broken code."""
    project.add_step("Orders", "demo.old", {}, version=1)
    project.add_step("Orders", "demo.titled", {})
    engine = Engine(project, _leaving_registry(error))
    assert engine.describe(project.tables[0].steps[-2]) == "Old"
    assert engine.describe(project.tables[0].steps[-1]) == "Titled"
    assert project.tables[0].steps[-2] not in engine.upgrade_steps() and engine.links() == []
    failed = engine.evaluate("Orders").steps[-2]
    assert failed.status == Status.ERROR
    assert failed.error == f"Could not read the step settings: The plugin 'leaving' tried to end the program ({shown})"

    project.add_step(project.add_table("Other"), "demo.source", {})
    assert engine.evaluate("Other").df.to_dicts() == [{"a": 1}]  # without a fingerprint: read every time
    assert not engine.evaluate("Other").steps[0].cached


def test_a_keyboard_interrupt_from_plugin_code_after_cancelling_stops(project):
    """Ctrl+C on the command line sets the cancel event before it interrupts: then a
    KeyboardInterrupt in a migrate, summary or fingerprint function stops the work."""
    project.add_step("Orders", "demo.old", {}, version=1)
    project.add_step("Orders", "demo.titled", {})
    engine = Engine(project, _leaving_registry(KeyboardInterrupt()))
    engine.cancel = threading.Event()
    engine.cancel.set()
    for work in (
        lambda: engine.describe(project.tables[0].steps[-2]),
        lambda: engine.describe(project.tables[0].steps[-1]),
        engine.upgrade_steps,
        engine.links,
    ):
        with pytest.raises(Cancelled):
            work()

    # The step settings and the fingerprint, while calculating
    cancel = threading.Event()

    def ctrl_c(*_):
        cancel.set()
        raise KeyboardInterrupt

    module = types.ModuleType("stopping")
    module.old = action("demo.old2", "Old", version=2, migrate=ctrl_c)(lambda df, p, ctx: df)
    module.source = action("demo.source2", "Source", kind="source", fingerprint=ctrl_c)(lambda df, p, ctx: pl.DataFrame())
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step(project.add_table("Old"), "csv.import", {"file": "orders.csv"})
    project.add_step("Old", "demo.old2", {}, version=1)
    project.add_step(project.add_table("New"), "demo.source2", {})
    for table in ("Old", "New"):
        cancel.clear()
        with pytest.raises(Cancelled):
            Engine(project, registry).evaluate(table, cancel=cancel)


def test_an_interruption_after_cancelling_stops_the_calculation(project):
    """Ctrl+C on the command line sets the cancel event first, then interrupts the step."""
    cancel = threading.Event()
    module = types.ModuleType("interrupted")

    @action("demo.ctrl_c", "Ctrl+C")
    def ctrl_c(df, p, ctx):
        cancel.set()
        raise KeyboardInterrupt

    module.ctrl_c = ctrl_c
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step("Orders", "demo.ctrl_c", {})
    with pytest.raises(Cancelled):
        Engine(project, registry).evaluate("Orders", cancel=cancel)


@pytest.mark.parametrize(
    "error", [ActionError("Formula error: Query interrupted"), duckdb.InterruptException("Query interrupted")]
)
def test_an_error_after_cancelling_stops_the_calculation(project, error):
    """Ctrl+C stops a DuckDB query, which reports it as an error: with the cancel event set,
    that is the stop, also in the last step."""
    cancel = threading.Event()
    module = types.ModuleType("interrupted")

    @action("demo.stopped_query", "Stopped query")
    def stopped_query(df, p, ctx):
        cancel.set()
        raise error

    module.stopped_query = stopped_query
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step("Orders", "demo.stopped_query", {})
    with pytest.raises(Cancelled):
        Engine(project, registry).evaluate("Orders", cancel=cancel)
    assert Engine(project, registry).evaluate("Orders").steps[-1].status == Status.ERROR  # not stopping: an error


SLOW = "(SELECT COUNT(*) FROM range(3000000000) t(i) WHERE i % 7 = 8) = 0"  # many seconds


def test_a_formula_that_is_interrupted_stops_the_calculation(project):
    """The cancel event is set while a formula runs, and its DuckDB query is interrupted."""
    cancel = threading.Event()
    module = types.ModuleType("interrupting")

    @action("demo.interrupted_formula", "Interrupted formula")
    def interrupted_formula(df, p, ctx):
        con = expressions._connection()  # this thread's: the one the formula uses
        lock = threading.Lock()
        running = [True]

        def ctrl_c():
            cancel.set()
            while True:  # DuckDB forgets an interrupt that comes before the query starts
                with lock:  # never after the formula: it would stop the next query on this thread
                    if not running[0]:
                        return
                    con.interrupt()
                time.sleep(0.02)

        thread = threading.Thread(target=ctrl_c, daemon=True)
        thread.start()
        try:
            return expressions.filter_rows(df, SLOW)
        finally:
            with lock:
                running[0] = False
            thread.join()

    module.interrupted_formula = interrupted_formula
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    project.add_step("Orders", "demo.interrupted_formula", {})
    started = time.perf_counter()
    with pytest.raises(Cancelled):
        Engine(project, registry).evaluate("Orders", cancel=cancel)
    assert time.perf_counter() - started < 5


def test_stopping_a_calculation_stops_the_formula_that_runs(project, registry):
    """A CancelEvent (the window's Stop, Ctrl+C on the command line) interrupts DuckDB itself:
    the step doesn't have to look at it."""
    project.add_step("Orders", "rows.filter_formula", {"condition": SLOW})
    cancel = CancelEvent()
    threading.Timer(0.3, cancel.set).start()
    started = time.perf_counter()
    with pytest.raises(Cancelled):
        Engine(project, registry).evaluate("Orders", cancel=cancel)
    assert time.perf_counter() - started < 5
    after = Engine(project, registry).evaluate("Orders", until=1)  # the next query on this thread runs
    assert after.ok


def test_a_run_reports_each_table_and_the_one_it_stopped_in(project, tmp_path):
    cancel = threading.Event()
    module = types.ModuleType("interrupted")

    @action("demo.ctrl_c", "Ctrl+C")
    def ctrl_c(df, p, ctx):
        cancel.set()
        raise KeyboardInterrupt

    module.ctrl_c = ctrl_c
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    second = project.add_table("Second")
    project.add_step(second, "table.derive", {"table": "t1"})
    project.add_step(second, "rows.sort", {"by": [{"column": "Id"}]})
    project.add_step(second, "demo.ctrl_c", {})
    project.add_step(second, "csv.export", {"file": "out/second.csv"})
    reported = []
    with pytest.raises(Cancelled):
        Engine(project, registry).run(cancel=cancel, done=lambda table_id, result: reported.append((table_id, result)))
    assert [table_id for table_id, _ in reported] == ["t1", second.id]
    orders, stopped = reported[0][1], reported[1][1]
    assert orders.ok and not orders.stopped and (tmp_path / "out" / "orders.csv").exists()
    assert stopped.stopped and statuses(stopped) == [Status.OK, Status.OK]  # the steps before the stop
    assert not stopped.ok and not stopped.failed and stopped.df is None

    reported.clear()
    results = Engine(project, registry).run(["t1"], done=lambda table_id, result: reported.append((table_id, result)))
    assert reported == list(results.items()) and results["t1"].ok


def test_a_failed_step_has_the_time_it_ran(project, quitting_registry):
    project.add_step("Orders", "demo.slow_failure", {})
    for unexpected in (False, True):
        project.tables[0].steps[-1].params["unexpected"] = unexpected
        failed = Engine(project, quitting_registry).evaluate("Orders").steps[-1]
        assert failed.status == Status.ERROR and failed.seconds >= 0.2
    project.add_step("Orders", "rows.sort", {"by": [{"column": "amount"}]}, index=1)  # doesn't run
    assert Engine(project, quitting_registry).evaluate("Orders").steps[1].seconds == 0


def test_the_cache_keeps_the_last_two_settings_of_a_step(project, registry):
    engine = Engine(project, registry, ResultCache())
    calculate = project.tables[0].steps[2]
    keys = []
    for factor in (2, 3, 4):
        calculate.params["columns"][0]["formula"] = f"[Amount] * {factor}"
        keys.append(engine.evaluate("Orders").steps[2].key)
    assert engine.cache.get(keys[0]) is None  # two edits ago
    assert engine.cache.get(keys[1]) is not None and engine.cache.get(keys[2]) is not None

    calculate.params["columns"][0]["formula"] = "[Amount] * 3"  # undo
    assert engine.evaluate("Orders").steps[2].cached
    calculate.params["columns"][0]["formula"] = "[Amount] * 5"  # a new edit: the one undone is dropped
    engine.evaluate("Orders")
    assert engine.cache.get(keys[2]) is None and engine.cache.get(keys[1]) is not None
    assert engine.evaluate("Orders").steps[0].cached  # the steps before it keep theirs


def test_a_result_that_another_step_still_has_stays():
    cache = ResultCache()
    df = pl.DataFrame({"a": [1]})
    cache.put("shared", df, step="s1")
    cache.put("shared", df, step="s2")
    cache.put("k2", df, step="s2")
    cache.put("k3", df, step="s2")
    assert cache.get("shared") is df  # still the result of s1
    cache.put("k4", df, step="s1")
    cache.put("k5", df, step="s1")
    assert cache.get("shared") is None and len(cache) == 4


def test_the_cache_limit_follows_the_installed_memory(monkeypatch):
    from tafl.core import engine

    assert engine.installed_memory() > 1024**3  # this computer
    monkeypatch.setattr(engine, "installed_memory", lambda: 16 * 1024**3)
    assert ResultCache().max_bytes == 4 * 1024**3
    monkeypatch.setattr(engine, "installed_memory", lambda: 1024**3)
    assert ResultCache().max_bytes == 512 * 1024**2
    monkeypatch.setattr(engine, "installed_memory", lambda: None)
    assert ResultCache().max_bytes == 2 * 1024**3
    assert ResultCache(100).max_bytes == 100

import os
import threading
import types

import polars as pl
import pytest

from tafl.api import Cancelled, P, action
from tafl.core import Engine, Project, Registry, Status

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

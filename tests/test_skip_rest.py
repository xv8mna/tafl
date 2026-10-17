"""SkipRest: an action ends its table at a step without an error."""

import types

import polars as pl
import pytest

from tafl.api import Mode, P, SkipRest, action
from tafl.cli import main
from tafl.core import Engine, Project, Registry, ResultCache, Status, StepResult

CSV = "Id;Amount\n1;5\n2;7\n"


@pytest.fixture
def project(tmp_path) -> Project:
    """Orders: import, filter (no rows are left), skip the rest if empty, calculate, export."""
    (tmp_path / "orders.csv").write_text(CSV, encoding="utf-8")
    project = Project(name="Test")
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    project.add_step(orders, "rows.filter_formula", {"condition": "[Amount] > 100"})
    project.add_step(orders, "flow.skip_if_empty", {})
    project.add_step(orders, "calc.columns", {"columns": [{"name": "Double", "formula": "[Amount] * 2"}]})
    project.add_step(orders, "csv.export", {"file": "out/orders.csv"})
    return project


@pytest.fixture
def demo_registry() -> Registry:
    """The built-in actions plus sources and a command that raise SkipRest."""
    module = types.ModuleType("skipping")

    @action("demo.nothing", "Nothing to read", kind="source", params=[P.Bool("with_data", "With data")])
    def nothing(df, p, ctx):
        ctx.log("Looked for files")
        raise SkipRest("No files today", pl.DataFrame({"File": ["none"]}) if p.with_data else None)

    @action("demo.silent", "Skip without a message")
    def silent(df, p, ctx):
        raise SkipRest("  ")

    @action("demo.wrong", "Skip with something that isn't a table")
    def wrong(df, p, ctx):
        raise SkipRest("Nothing", df={"a": [1]})

    @action("demo.stop", "Stop the table", kind="command")
    def stop(df, p, ctx):
        raise SkipRest("Stopped by the command")

    module.nothing, module.silent, module.wrong, module.stop = nothing, silent, wrong, stop
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    return registry


def statuses(result):
    return [s.status for s in result.steps]


def test_the_rest_of_the_table_is_skipped(project, registry):
    result = Engine(project, registry).evaluate("Orders")
    assert statuses(result) == [Status.OK, Status.OK, Status.OK, Status.SKIPPED, Status.SKIPPED]
    ended = result.steps[2]
    assert ended.skip_rest == "The table is empty" and ("info", "The table is empty") in ended.messages
    assert result.skipped_from is ended and result.steps[0].skip_rest is None
    for later in result.steps[3:]:
        assert later.skip_rest == "The table is empty" and later.messages == [("info", "The table is empty")]
        assert later.df is ended.df and later.key == ended.key
    assert result.ok and not result.failed
    assert result.df.columns == ["Id", "Amount"] and result.df.height == 0  # the data at the step
    assert result.key == ended.key


def test_a_run_skips_the_exports_and_succeeds(project, registry, tmp_path):
    results = Engine(project, registry).run()
    assert statuses(results["t1"]) == [Status.OK, Status.OK, Status.OK, Status.SKIPPED, Status.SKIPPED]
    assert results["t1"].ok
    assert not (tmp_path / "out").exists()


def test_with_rows_nothing_is_skipped(project, registry, tmp_path):
    project.tables[0].steps[1].params["condition"] = "[Amount] > 6"
    results = Engine(project, registry).run()
    assert statuses(results["t1"]) == [Status.OK] * 5
    assert results["t1"].skipped_from is None and results["t1"].df["Double"].to_list() == [14]
    assert (tmp_path / "out" / "orders.csv").exists()


def test_a_cached_result_skips_the_same_steps(project, registry):
    engine = Engine(project, registry)
    first = engine.evaluate("Orders")
    again = engine.evaluate("Orders")
    assert [s.cached for s in again.steps[:3]] == [True, True, True]
    assert statuses(again) == statuses(first)
    assert [(s.skip_rest, s.messages) for s in again.steps[2:]] == [(s.skip_rest, s.messages) for s in first.steps[2:]]

    engine.run()  # a preview after a run shows the run's results, skipped steps included
    preview = engine.evaluate("Orders")
    assert all(s.cached for s in preview.steps[:3])
    assert statuses(preview) == [Status.OK, Status.OK, Status.OK, Status.SKIPPED, Status.SKIPPED]

    project.tables[0].steps[1].params["condition"] = "[Amount] > 6"  # rows again: nothing is skipped
    assert statuses(engine.evaluate("Orders")) == [Status.OK, Status.OK, Status.OK, Status.OK, Status.SKIPPED]


def test_the_data_going_in_keeps_its_own_cache_entry(project, registry):
    engine = Engine(project, registry)
    result = engine.evaluate("Orders")
    assert engine.cache.lookup(result.steps[1].key)[1] is None  # the filter itself skips nothing
    assert engine.cache.lookup(result.steps[2].key)[1] == "The table is empty"
    project.tables[0].steps[2].enabled = False
    assert statuses(engine.evaluate("Orders")) == [Status.OK, Status.OK, Status.DISABLED, Status.OK, Status.SKIPPED]


def test_steps_that_are_off_stay_off(project, registry):
    project.add_step("Orders", "rows.sort", {"by": [{"column": "Id"}]}, index=1)
    project.tables[0].steps[1].enabled = False  # before the skip
    project.tables[0].steps[4].enabled = False  # after it
    result = Engine(project, registry).evaluate("Orders")
    assert statuses(result) == [Status.OK, Status.DISABLED, Status.OK, Status.OK, Status.DISABLED, Status.SKIPPED]
    assert result.steps[4].skip_rest is None and result.steps[4].df is result.steps[3].df


def test_only_up_to_a_step(project, registry):
    engine = Engine(project, registry)
    assert statuses(engine.evaluate("Orders", until=1)) == [Status.OK, Status.OK]
    assert statuses(engine.evaluate("Orders", until=3)) == [Status.OK, Status.OK, Status.OK, Status.SKIPPED]


def test_another_table_and_tables_that_use_a_skipped_one(project, registry):
    orders = project.tables[0]
    report = project.add_table("Report")
    project.add_step(report, "table.derive", {"table": orders.id})
    project.add_step(report, "calc.columns", {"columns": [{"name": "Half", "formula": "[Amount] / 2"}]})
    check = project.add_table("Check")
    project.add_step(check, "csv.import", {"file": "orders.csv"})
    project.add_step(check, "flow.skip_if_empty", {"which": "other", "table": orders.id})
    project.add_step(check, "csv.export", {"file": "out/check.csv"})
    uses_check = project.add_table("Uses check")
    project.add_step(uses_check, "table.derive", {"table": check.id})
    project.add_step(uses_check, "calc.columns", {"columns": [{"name": "Half", "formula": "[Amount] / 2"}]})
    engine = Engine(project, registry)
    assert engine.dependencies(check.id) == [orders.id]

    results = engine.run()
    # Orders has no rows and skipped its rest: there is nothing for Report to work with
    assert statuses(results[report.id]) == [Status.OK, Status.SKIPPED] and results[report.id].ok
    no_data = "The table “Orders” has no data (its rest was skipped)"
    assert results[report.id].steps[0].skip_rest == no_data
    assert results[report.id].df.columns == ["Id", "Amount"] and results[report.id].df.height == 0
    assert statuses(results[check.id]) == [Status.OK, Status.OK, Status.SKIPPED]
    assert results[check.id].steps[1].skip_rest == no_data
    assert results[check.id].df.height == 2  # its own data, unchanged
    # Check skipped its rest too, but it has rows: they are used as usual
    assert statuses(results[uses_check.id]) == [Status.OK, Status.OK]
    assert results[uses_check.id].df["Half"].to_list() == [2.5, 3.5]

    orders.steps[1].params["condition"] = "[Amount] > 6"
    results = engine.run()
    assert statuses(results[check.id]) == [Status.OK, Status.OK, Status.OK]
    assert statuses(results[report.id]) == [Status.OK, Status.OK] and results[report.id].df["Half"].to_list() == [3.5]


@pytest.fixture
def daily(tmp_path) -> Project:
    """Inbox: the files of a folder (none: skip the rest), with exports. Total starts from
    Inbox, Report and Customers merge with it, History appends it, Summary starts from Total."""
    (tmp_path / "inbox").mkdir()
    (tmp_path / "customers.csv").write_text("Id;Name\n1;Anna\n2;Ben\n", encoding="utf-8")
    project = Project(name="Daily")
    project.path = tmp_path / "daily.tafl"
    inbox = project.add_table("Inbox")
    project.add_step(inbox, "csv.import", {"source": "folder", "folder": "inbox", "pattern": "*.csv", "no_files": "skip"})
    project.add_step(inbox, "calc.columns", {"columns": [{"name": "Gross", "formula": "ROUND([Net] * 1.19, 2)"}]})
    project.add_step(inbox, "csv.export", {"file": "out/inbox.csv"})
    total = project.add_table("Total")
    project.add_step(total, "table.derive", {"table": inbox.id})
    project.add_step(total, "calc.aggregate", {"calculations": [{"function": "sum", "column": "Gross"}]})
    project.add_step(total, "csv.export", {"file": "out/total.csv"})
    for name, how in (("Report", "lookup"), ("Customers", "inner")):
        table = project.add_table(name)
        project.add_step(table, "csv.import", {"file": "customers.csv"})
        match = [{"column": "Id", "other_column": "Id"}]
        project.add_step(table, "table.merge", {"other": inbox.id, "match": match, "how": how, "columns": ["Gross"]})
        project.add_step(table, "rows.filter_formula", {"condition": "[Gross] > 0"})
        project.add_step(table, "csv.export", {"file": f"out/{name.lower()}.csv"})
    history = project.add_table("History")
    project.add_step(history, "csv.import", {"file": "customers.csv"})
    project.add_step(history, "table.append", {"tables": [inbox.id]})
    project.add_step(history, "csv.export", {"file": "out/history.csv"})
    summary = project.add_table("Summary")
    project.add_step(summary, "table.derive", {"table": total.id})
    project.add_step(summary, "csv.export", {"file": "out/summary.csv"})
    return project


def test_a_day_without_data_skips_the_tables_that_use_the_import(daily, registry, tmp_path):
    results = {daily.table(t).name: r for t, r in Engine(daily, registry).run().items()}
    assert all(r.ok for r in results.values())
    no_data = "The table “Inbox” has no data (its rest was skipped)"
    assert statuses(results["Total"]) == [Status.OK, Status.SKIPPED, Status.SKIPPED]
    assert results["Total"].steps[0].skip_rest == no_data and results["Total"].steps[0].messages == [("info", no_data)]
    assert results["Total"].df.columns == ["File"] and results["Total"].df.height == 0  # the data of Inbox
    for name in ("Report", "Customers", "History"):  # the step that uses Inbox doesn't run
        assert statuses(results[name]) == [Status.OK, Status.OK] + [Status.SKIPPED] * (len(results[name].steps) - 2)
        assert results[name].steps[1].skip_rest == no_data
        assert results[name].df.columns == ["Id", "Name"] and results[name].df.height == 2  # the data going in
    # Total skipped its rest and has no rows, too
    assert statuses(results["Summary"]) == [Status.OK, Status.SKIPPED]
    assert results["Summary"].steps[0].skip_rest == "The table “Total” has no data (its rest was skipped)"
    assert not (tmp_path / "out").exists()

    (tmp_path / "inbox" / "a.csv").write_text("Id;Net\n1;10\n", encoding="utf-8")
    results = {daily.table(t).name: r for t, r in Engine(daily, registry).run().items()}
    assert all(r.ok and r.skipped_from is None for r in results.values())
    assert results["Total"].df.to_dicts() == [{"Sum of Gross": 11.9}]
    assert results["Report"].df["Name"].to_list() == ["Anna"] and results["History"].df.height == 3
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == [
        "customers.csv", "history.csv", "inbox.csv", "report.csv", "summary.csv", "total.csv",
    ]  # fmt: skip


def test_a_day_without_data_on_the_command_line(daily, capsys):
    daily.save()
    assert main(["run", str(daily.path)]) == 0
    output = capsys.readouterr().out
    assert "1. Start from Inbox   0 rows × 1 column" in output
    assert "i The table “Inbox” has no data (its rest was skipped)" in output
    assert "2. Aggregate: Sum of Gross   (skipped: the table “Inbox” has no data (its rest was skipped))" in output
    assert "(skipped: the table “Total” has no data (its rest was skipped))" in output
    assert "✗" not in output and "Done in" in output


@pytest.mark.parametrize("mode", [Mode.PREVIEW, Mode.RUN])
def test_a_source_that_skips_the_rest(demo_registry, mode):
    project = Project()
    table = project.add_table("Files")
    project.add_step(table, "demo.nothing", {})
    project.add_step(table, "calc.columns", {"columns": [{"name": "X", "formula": "1"}]})
    engine = Engine(project, demo_registry)
    result = engine.run(mode=mode)[table.id]
    assert statuses(result) == [Status.OK, Status.SKIPPED] and result.ok
    assert result.df.shape == (0, 0)  # an empty table
    assert result.steps[0].messages == [("info", "Looked for files"), ("info", "No files today")]
    assert result.steps[1].messages == [("info", "No files today")]

    project.tables[0].steps[0].params["with_data"] = True
    result = engine.evaluate("Files", mode=mode)
    assert result.df.to_dicts() == [{"File": "none"}] and result.steps[1].df is result.df

    again = engine.evaluate("Files", mode=mode)
    assert again.steps[0].cached and again.steps[0].skip_rest == "No files today"
    assert again.steps[0].messages == [("info", "Looked for files"), ("info", "No files today")]  # kept


def test_a_command_that_skips_the_rest_runs_only_in_a_run(project, demo_registry, tmp_path):
    project.tables[0].steps[1].params["condition"] = "[Amount] > 6"
    project.add_step("Orders", "demo.stop", {}, index=3)
    engine = Engine(project, demo_registry)
    preview = engine.evaluate("Orders")
    assert statuses(preview) == [Status.OK, Status.OK, Status.OK, Status.SKIPPED, Status.OK, Status.SKIPPED]
    assert preview.steps[3].skip_rest is None and preview.df.columns == ["Id", "Amount", "Double"]

    results = engine.run()
    assert statuses(results["t1"]) == [Status.OK, Status.OK, Status.OK, Status.OK, Status.SKIPPED, Status.SKIPPED]
    assert results["t1"].steps[3].skip_rest == "Stopped by the command" and results["t1"].ok
    assert not (tmp_path / "out").exists()


def test_messages_and_data_that_skip_rest_needs(project, demo_registry):
    project.add_step("Orders", "demo.silent", {}, index=1)
    engine = Engine(project, demo_registry)
    result = engine.evaluate("Orders")
    assert result.steps[1].skip_rest == "Step 2 skips the rest of the table"
    assert statuses(result)[2:] == [Status.SKIPPED] * 4

    project.tables[0].steps[1].action = "demo.wrong"
    result = engine.evaluate("Orders")
    assert result.steps[1].error == "'Skip with something that isn't a table' did not return a table"
    assert statuses(result)[2:] == [Status.BLOCKED] * 4


def test_the_reason_follows_a_colon():
    def reason(message):
        return StepResult("s", Status.SKIPPED, skip_rest=message).skip_reason

    assert reason("The table is empty") == "the table is empty"
    assert reason("No files named *.csv in C:\\inbox.") == "no files named *.csv in C:\\inbox"
    assert reason("SQL Server has no new rows") == "SQL Server has no new rows"
    assert reason("“Orders” is empty") == "“Orders” is empty"
    assert StepResult("s", Status.SKIPPED).skip_reason == ""


def test_the_cache_keeps_the_message():
    cache = ResultCache()
    df = pl.DataFrame({"a": [1]})
    cache.put("k1", df)
    cache.put("k2", df, "The table is empty")
    assert cache.get("k2") is df and cache.lookup("k2") == (df, "The table is empty", ())
    assert cache.lookup("k1") == (df, None, ()) and cache.lookup("k3") is None


def test_command_line(project, capsys):
    project.save()
    assert main(["run", str(project.path)]) == 0
    output = capsys.readouterr().out
    assert "3. Skip the rest if the table is empty   0 rows × 2 columns" in output
    assert "i The table is empty" in output
    assert "4. Calculate Double   (skipped: the table is empty)" in output
    assert "5. Export to orders.csv   (skipped: the table is empty)" in output
    assert "Done in" in output

    assert main(["run", str(project.path), "--preview"]) == 0
    assert "(runs only when the project is run)" not in capsys.readouterr().out

    assert main(["show", str(project.path), "--table", "Orders"]) == 0
    output = capsys.readouterr().out
    assert "(skipped: the table is empty)" in output and "0 rows × 2 columns" in output

"""Check data: stop the table, or only warn, when the number of rows is not as expected."""

import time
from datetime import date

import polars as pl
import pytest

from tafl.api import ActionError, Params, ValidationContext
from tafl.api.testing import StubContext
from tafl.cli import main
from tafl.core import Engine, Project, Status

NO_ROWS = "The table has no rows; it must have at least 1 row"


def table(rows: int) -> pl.DataFrame:
    """A table with ``rows`` rows; without rows it still has its two columns."""
    return pl.DataFrame({"Id": pl.int_range(rows, eager=True), "Name": pl.repeat("x", rows, eager=True)})


def checks(*rows) -> list[dict]:
    """("at_least", 10) or ("between", 10, 20) -> rows of the setting "Checks"."""
    return [{"check": "rows", **dict(zip(("compare", "number", "up_to"), row))} for row in rows]


def check(act, df, *rows, on_fail="stop"):
    """The result and the messages of the step."""
    ctx = StubContext()
    out = act("check.data", df, {"checks": checks(*rows), "on_fail": on_fail}, ctx)
    return out, ctx.messages


# --- the action -----------------------------------------------------------------------------------


def test_settings(registry):
    action = registry.actions["check.data"]
    assert (action.name, action.category, action.version, action.migrate) == ("Check data", "Rows", 1, None)
    assert [a.name for a in registry.by_category()["Rows"]][0] == "Check data"
    assert action.resolve({}) == {
        "checks": [{"check": "rows", "compare": "at_least", "number": 1, "up_to": 100}],
        "on_fail": "stop",
    }
    assert action.check(action.resolve({}), ValidationContext(columns=["Id"])) == []
    assert action.description.startswith(
        "Checks the data at this step: when a check fails, it stops the table or only warns. The data\nitself stays as it is."
    )
    assert "Number of rows: at least, at most, exactly or between two numbers" in action.description
    on_fail = action.params[1]
    assert on_fail.options == (("stop", "Stop the table"), ("warn", "Only warn"))
    assert on_fail.help.startswith("Stop the table: the step shows an error")
    fields = {f.key: f for f in action.params[0].fields}
    assert [f.label for f in fields.values()] == ["Check", "Must be", "Number", "and"]
    assert fields["check"].options == (("rows", "Number of rows"),)
    assert [label for _, label in fields["compare"].options] == ["at least", "at most", "exactly", "between"]


@pytest.mark.parametrize(
    ("rows", "on_fail", "summary"),
    [
        ([("at_least", 1)], "stop", "Check: at least 1 row, else stop"),
        ([("at_most", 0)], "warn", "Check: no rows, else warn"),
        ([("between", 10, 100)], "stop", "Check: between 10 and 100 rows, else stop"),
        ([("at_least", 1), ("at_most", 1000)], "stop", "Check: at least 1 row; at most 1,000 rows, else stop"),
        ([("exactly", 1)], "warn", "Check: exactly 1 row, else warn"),
        ([("exactly", 0)], "stop", "Check: no rows, else stop"),
        ([("at_least", 0)], "stop", "Check: at least 0 rows, else stop"),
        ([("between", 5, 5)], "stop", "Check: exactly 5 rows, else stop"),
        ([("between", 0, 0)], "stop", "Check: no rows, else stop"),
        ([("between", 1500, 2_000_000)], "warn", "Check: between 1,500 and 2,000,000 rows, else warn"),
    ],
)
def test_summaries(registry, rows, on_fail, summary):
    action = registry.actions["check.data"]
    assert action.describe(action.resolve({"checks": checks(*rows), "on_fail": on_fail})) == summary


def test_summaries_of_lists_without_rows_or_unknown_kinds(registry):
    action = registry.actions["check.data"]
    assert action.describe(Params(checks=[], on_fail="stop")) == "Check: ?, else stop"
    assert action.describe(action.resolve({"checks": "rows"})) == "Check data"  # not a list
    assert action.describe(action.resolve({"checks": [{"number": "many"}]})) == "Check data"
    later = action.resolve({"checks": [*checks(("at_least", 1)), {"check": "unique", "columns": ["Id"]}]})
    assert action.describe(later) == "Check: at least 1 row; ?, else stop"


def test_problems_with_the_settings(registry):
    action = registry.actions["check.data"]
    vc = ValidationContext(columns=["Id"])

    def problems(values):
        return action.check(action.resolve(values), vc)

    assert problems({"checks": checks(("between", 10, 5))}) == [
        "Checks, row 1 – between 10 and 5: the first number must not be bigger than the second"
    ]
    assert problems({"checks": checks(("at_least", 1), ("between", 2000, 1000), ("between", 3, 1))}) == [
        "Checks, row 2 – between 2,000 and 1,000: the first number must not be bigger than the second",
        "Checks, row 3 – between 3 and 1: the first number must not be bigger than the second",
    ]
    assert problems({"checks": checks(("between", 5, 5), ("at_least", 200, 100))}) == []  # "and" counts only for between
    assert problems({"checks": checks(("at_least", -1))}) == ["Checks, row 1 – Number: must be at least 0"]
    assert problems({"checks": checks(("between", 1, -1))}) == [
        "Checks, row 1 – and: must be at least 0",
        "Checks, row 1 – between 1 and -1: the first number must not be bigger than the second",
    ]
    assert problems({"checks": checks(("between", "ten", 5))}) == ["Checks, row 1 – Number: must be a whole number"]
    assert problems({"checks": []}) == ["Checks: add at least 1 row"]
    assert problems({"checks": checks(("more", 1))}) == [
        "Checks, row 1 – Must be: 'more' is not one of: at_least, at_most, exactly, between"
    ]
    assert problems({"on_fail": "ignore"}) == ["When a check fails: 'ignore' is not one of: stop, warn"]


@pytest.mark.parametrize(
    ("row", "rows", "passed", "text"),
    [
        (("at_least", 10), 9, False, "The table has 9 rows; it must have at least 10 rows"),
        (("at_least", 10), 10, True, "The check passed: the table has 10 rows"),
        (("at_least", 10), 11, True, "The check passed: the table has 11 rows"),
        (("at_most", 10), 9, True, "The check passed: the table has 9 rows"),
        (("at_most", 10), 10, True, "The check passed: the table has 10 rows"),
        (("at_most", 10), 11, False, "The table has 11 rows; it must have at most 10 rows"),
        (("exactly", 10), 9, False, "The table has 9 rows; it must have exactly 10 rows"),
        (("exactly", 10), 10, True, "The check passed: the table has 10 rows"),
        (("exactly", 10), 11, False, "The table has 11 rows; it must have exactly 10 rows"),
        (("between", 10, 20), 9, False, "The table has 9 rows; it must have between 10 and 20 rows"),
        (("between", 10, 20), 10, True, "The check passed: the table has 10 rows"),
        (("between", 10, 20), 20, True, "The check passed: the table has 20 rows"),
        (("between", 10, 20), 21, False, "The table has 21 rows; it must have between 10 and 20 rows"),
        # no rows and one row
        (("at_least", 1), 0, False, NO_ROWS),
        (("at_least", 1), 1, True, "The check passed: the table has 1 row"),
        (("at_least", 2), 1, False, "The table has 1 row; it must have at least 2 rows"),
        (("at_least", 0), 0, True, "The check passed: the table has no rows"),
        (("at_most", 0), 0, True, "The check passed: the table has no rows"),
        (("at_most", 0), 12, False, "The table has 12 rows; it must have no rows"),
        (("at_most", 1), 2, False, "The table has 2 rows; it must have at most 1 row"),
        (("exactly", 0), 1, False, "The table has 1 row; it must have no rows"),
        (("exactly", 1), 1, True, "The check passed: the table has 1 row"),
        (("exactly", 1), 2, False, "The table has 2 rows; it must have exactly 1 row"),
        (("between", 0, 0), 0, True, "The check passed: the table has no rows"),
        (("between", 0, 0), 3, False, "The table has 3 rows; it must have no rows"),
        (("between", 1, 1), 0, False, "The table has no rows; it must have exactly 1 row"),
        (("between", 5, 5), 4, False, "The table has 4 rows; it must have exactly 5 rows"),
        (("between", 0, 1), 0, True, "The check passed: the table has no rows"),
        # many rows
        (("at_most", 1000), 1500, False, "The table has 1,500 rows; it must have at most 1,000 rows"),
        (("at_least", 1000), 1500, True, "The check passed: the table has 1,500 rows"),
        (("between", 1000, 2000), 2500, False, "The table has 2,500 rows; it must have between 1,000 and 2,000 rows"),
        (("exactly", 1_000_000), 999_999, False, "The table has 999,999 rows; it must have exactly 1,000,000 rows"),
    ],
)
def test_each_check_at_its_bounds(act, row, rows, passed, text):
    df = table(rows)
    if passed:
        for on_fail in ("stop", "warn"):
            out, messages = check(act, df, row, on_fail=on_fail)
            assert out is df and messages == [("info", text)]
    else:
        with pytest.raises(ActionError) as raised:
            check(act, df, row)
        assert str(raised.value) == text
        out, messages = check(act, df, row, on_fail="warn")
        assert out is df and messages == [("warning", text)]


def test_all_failing_checks_are_reported_in_their_order(act):
    """In one line, so that Run results, the step list and Find show all of them."""
    df = table(5)
    rows = [("at_least", 10), ("between", 1, 9), ("at_most", 1), ("exactly", 3)]
    failure = "The table has 5 rows; it must have at least 10 rows, at most 1 row and exactly 3 rows"
    with pytest.raises(ActionError) as raised:
        check(act, df, *rows)
    assert str(raised.value) == failure  # the check that passed is not mentioned

    out, messages = check(act, df, *rows, on_fail="warn")
    assert out is df and out.equals(table(5))
    assert messages == [("warning", failure)]
    two = "The table has 37 rows; it must have at least 100 rows and between 50 and 500 rows"  # as in the guide
    assert check(act, table(37), ("at_least", 100), ("between", 50, 500), on_fail="warn")[1] == [("warning", two)]


def test_a_step_without_checks(registry, act):
    """The list can lose its last row (✕): the step then has an error, and its summary doesn't
    look like a real check."""
    action = registry.actions["check.data"]
    assert action.check(action.resolve({"checks": []}), ValidationContext(columns=["Id"])) == ["Checks: add at least 1 row"]
    with pytest.raises(ActionError, match="^Checks: add at least 1 row$"):
        act("check.data", table(1), {"checks": []})
    project = Project(name="Test")
    orders = project.add_table("Orders")
    project.add_step(orders, registry.actions["csv.import"], {"file": "orders.csv"})
    step = project.add_step(orders, action, {"checks": [], "on_fail": "warn"})
    engine = Engine(project, registry)
    assert engine.describe(step) == "Check: ?, else warn"
    new = project.add_step(orders, action, {})
    assert engine.describe(new) == "Check: at least 1 row, else stop"


def test_all_checks_pass(act):
    out, messages = check(act, table(5), ("at_least", 1), ("at_most", 5), ("between", 0, 1000))
    assert out.equals(table(5)) and messages == [("info", "All 3 checks passed: the table has 5 rows")]


def test_empty_tables(act):
    for df in (table(0), pl.DataFrame(), pl.DataFrame({"Id": []})):
        with pytest.raises(ActionError, match=f"^{NO_ROWS}$"):
            act("check.data", df, {})
        assert check(act, df, ("at_most", 0))[1] == [("info", "The check passed: the table has no rows")]
        out, messages = check(act, df, ("at_least", 1), on_fail="warn")
        assert out is df and messages == [("warning", NO_ROWS)]


def test_the_data_stays_as_it_is(act):
    """Rows with nothing but empty values count; columns of any type pass through."""
    df = pl.DataFrame(
        {
            "Id": [1, None, None],
            "Day": [date(2026, 10, 25), None, None],
            "Tags": [["a", "b"], None, []],
            "Point": [{"x": 1.5, "y": None}, None, {"x": None, "y": 2.0}],
            "Empty": [None, None, None],
        }
    )
    before = df.clone()
    out, messages = check(act, df, ("exactly", 3))
    assert out is df and out.equals(before) and out.schema == before.schema
    assert messages == [("info", "The check passed: the table has 3 rows")]


def test_settings_of_later_kinds_of_checks(registry, act):
    """Rows saved without a field get its default; fields of other kinds (unknown here) stay
    and change nothing; a kind this app doesn't know gives a clear problem."""
    action = registry.actions["check.data"]
    vc = ValidationContext(columns=["Id"])
    stored = action.resolve({"checks": [{"check": "rows", "compare": "between", "number": 3}]})
    assert stored.checks[0].up_to == 100 and action.check(stored, vc) == []
    with_more = {"check": "rows", "compare": "at_least", "number": 2, "columns": ["Id"], "condition": "[Id] > 0"}
    resolved = action.resolve({"checks": [with_more]})
    assert resolved.checks[0]["columns"] == ["Id"] and resolved.checks[0]["condition"] == "[Id] > 0"
    assert action.check(resolved, vc) == []
    ctx = StubContext()
    assert act("check.data", table(2), {"checks": [with_more]}, ctx).height == 2
    assert ctx.messages == [("info", "The check passed: the table has 2 rows")]
    assert action.upgrade({"checks": [with_more]}, 1) == {"checks": [with_more]}

    later = action.resolve({"checks": [*checks(("at_least", 1)), {"check": "unique", "number": -1, "columns": ["Id"]}]})
    assert action.check(later, vc) == ["Checks, row 2 – Check: 'unique' is not one of: rows"]  # its own fields only
    with pytest.raises(ActionError, match="^Checks, row 2 – Check: 'unique' is not one of: rows$"):
        act("check.data", table(1), later)  # without the engine, which reports it before the step runs


def _best(run, rounds=3) -> float:
    times = []
    for _ in range(rounds):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return min(times)


def test_a_million_rows_take_no_time(act):
    rows = 1_000_000
    df = pl.select(Id=pl.int_range(rows), Name=pl.int_range(rows).cast(pl.String), Amount=pl.int_range(rows) / 7)
    passing = {"checks": checks(("at_least", 1), ("between", 1000, 2_000_000))}
    failing = {"checks": checks(("at_most", 10), ("exactly", 5)), "on_fail": "warn"}
    assert _best(lambda: act("check.data", df, passing)) < 0.05
    assert _best(lambda: act("check.data", df, failing)) < 0.05


# --- in a project -----------------------------------------------------------------------------------

CSV = "Id;Amount\n1;5\n2;7\n"
TOO_FEW = "The table has 2 rows; it must have at least 10 rows"


@pytest.fixture
def project(tmp_path) -> Project:
    """Orders: import (2 rows), check (at least 10 rows), export. Report starts from Orders."""
    (tmp_path / "orders.csv").write_text(CSV, encoding="utf-8")
    project = Project(name="Test")
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    project.add_step(orders, "check.data", {"checks": checks(("at_least", 10))})
    project.add_step(orders, "csv.export", {"file": "out/orders.csv"})
    report = project.add_table("Report")
    project.add_step(report, "table.derive", {"table": orders.id})
    project.add_step(report, "calc.columns", {"columns": [{"name": "Double", "formula": "[Amount] * 2"}]})
    project.add_step(report, "csv.export", {"file": "out/report.csv"})
    return project


def statuses(result):
    return [s.status for s in result.steps]


def test_stop_ends_the_table_and_the_tables_that_use_it(project, registry, tmp_path):
    engine = Engine(project, registry)
    assert engine.describe(project.tables[0].steps[1]) == "Check: at least 10 rows, else stop"
    results = engine.run()
    orders, report = results["t1"], results[project.tables[1].id]
    assert statuses(orders) == [Status.OK, Status.ERROR, Status.BLOCKED]
    assert orders.failed and orders.first_error == TOO_FEW and orders.steps[1].messages == []
    assert statuses(report) == [Status.ERROR, Status.BLOCKED, Status.BLOCKED]
    assert report.steps[0].inherited and report.steps[0].error == f"Table 'Orders' has an error: {TOO_FEW}"
    assert not (tmp_path / "out").exists()  # no export ran

    preview = engine.evaluate("Orders")  # while the project is edited too
    assert statuses(preview) == [Status.OK, Status.ERROR, Status.BLOCKED] and preview.steps[1].error == TOO_FEW


def test_stop_fails_a_run_on_the_command_line(project, tmp_path, capsys):
    path = project.save(tmp_path / "test.tafl")
    assert main(["run", str(path)]) == 1
    output = capsys.readouterr().out
    assert TOO_FEW in output and f"Table 'Orders' has an error: {TOO_FEW}" in output
    assert not (tmp_path / "out").exists()


def test_warn_lets_the_data_go_on(project, registry, tmp_path, capsys):
    project.tables[0].steps[1].params["on_fail"] = "warn"
    engine = Engine(project, registry)
    assert engine.describe(project.tables[0].steps[1]) == "Check: at least 10 rows, else warn"
    results = engine.run()
    orders, report = results["t1"], results[project.tables[1].id]
    assert statuses(orders) == [Status.OK] * 3 and statuses(report) == [Status.OK] * 3
    assert orders.steps[1].messages == [("warning", TOO_FEW)] and orders.df.height == 2
    assert (tmp_path / "out" / "orders.csv").exists() and report.df["Double"].to_list() == [10, 14]

    first = engine.evaluate("Orders", until=1)
    again = engine.evaluate("Orders", until=1)
    assert again.steps[1].cached and again.steps[1].messages == [("warning", TOO_FEW)] == first.steps[1].messages

    (tmp_path / "out" / "orders.csv").unlink()
    path = project.save(tmp_path / "test.tafl")
    assert main(["run", str(path)]) == 0
    assert f"! {TOO_FEW}" in capsys.readouterr().out
    assert (tmp_path / "out" / "orders.csv").exists()


def test_with_enough_rows_the_exports_run(project, registry, tmp_path):
    project.tables[0].steps[1].params["checks"] = checks(("between", 1, 2))
    orders = Engine(project, registry).run()["t1"]
    assert statuses(orders) == [Status.OK] * 3
    assert orders.steps[1].messages == [("info", "The check passed: the table has 2 rows")]
    assert (tmp_path / "out" / "orders.csv").read_text(encoding="utf-8-sig").splitlines() == ["Id;Amount", "1;5", "2;7"]


def test_a_kind_of_check_this_app_does_not_know(project, registry):
    project.tables[0].steps[1].params["checks"] = [{"check": "unique", "columns": ["Id"]}]
    result = Engine(project, registry).evaluate("Orders")
    assert statuses(result)[:2] == [Status.OK, Status.ERROR]
    assert result.steps[1].error == "Checks, row 1 – Check: 'unique' is not one of: rows"


def test_a_step_whose_last_check_was_removed(project, registry):
    project.tables[0].steps[1].params["checks"] = []
    engine = Engine(project, registry)
    result = engine.evaluate("Orders")
    assert statuses(result) == [Status.OK, Status.ERROR, Status.BLOCKED]
    assert result.steps[1].error == "Checks: add at least 1 row"
    assert engine.describe(project.tables[0].steps[1]) == "Check: ?, else stop"

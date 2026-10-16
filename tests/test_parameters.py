"""Project parameters: their values, and filling {Name} into texts, formulas and SQL."""

import datetime as dt
import sqlite3

import polars as pl
import pytest

from etl_tool.api import ActionError, P, expressions
from etl_tool.api.parameters import (
    Failed,
    as_text,
    fill_formula,
    fill_sql,
    fill_text,
    formula_literal,
    formula_places,
    left_in_formula,
    sql_literal,
    typed,
)
from etl_tool.core import Connection, Engine, Parameter, Project, Status, parameters

DAY = dt.date(2026, 10, 15)


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("500", 500),
        ("-3", -3),
        ("01067", "01067"),  # a postcode, not a number
        ("2.5", 2.5),
        ("2026-10-15", DAY),
        ("2026-10-15 08:30", dt.datetime(2026, 10, 15, 8, 30)),
        ("2026-10-15T08:30:15", dt.datetime(2026, 10, 15, 8, 30, 15)),
        (r"D:\Data", r"D:\Data"),
        ("", ""),
        ("Report 2026", "Report 2026"),
    ],
)
def test_fixed_values_get_a_type(text, value):
    assert typed(text) == value and type(typed(text)) is type(value)


def test_values_written_as_text_in_formulas_and_in_sql():
    assert [as_text(v) for v in (DAY, dt.datetime(2026, 10, 15, 8, 30), 2.0, 2.5, True, None)] == [
        "2026-10-15", "2026-10-15 08:30:00", "2", "2.5", "true", "",
    ]  # fmt: skip
    assert formula_literal(DAY) == "DATE '2026-10-15'"
    assert formula_literal(dt.datetime(2026, 10, 15, 8, 30)) == "TIMESTAMP '2026-10-15 08:30:00'"
    assert formula_literal("O'Brien") == "'O''Brien'"
    assert [formula_literal(v) for v in (5, 2.5, True, None)] == ["5", "2.5", "TRUE", "NULL"]
    assert sql_literal("O'Brien") == "'O''Brien'"
    assert [sql_literal(v) for v in (DAY, 5, 2.5, False, None)] == ["'2026-10-15'", "5", "2.5", "0", "NULL"]


def test_texts_and_paths_get_the_value_as_text():
    values = {"Day": DAY, "Folder": r"D:\Data"}
    assert fill_text(r"{Folder}\orders_{day}.csv", values) == r"D:\Data\orders_2026-10-15.csv"
    assert fill_text(r"\d{3} {Other} {Day", values) == r"\d{3} {Other} {Day"  # other braces stay


def test_formulas_get_typed_values_and_texts_inside_quotes():
    values = {"Day": DAY, "Name": "O'Brien", "Limit": 500}
    formula = """[Date] >= {Day} AND [Net] > {Limit} AND [Customer] = {Name} || ' {Name} on {Day}'"""
    assert fill_formula(formula, values) == (
        "[Date] >= DATE '2026-10-15' AND [Net] > 500 AND [Customer] = 'O''Brien' || ' O''Brien on 2026-10-15'"
    )
    untouched = """[{Day}] + "{Day}" + $${Day}$$ -- {Day}"""
    assert fill_formula(untouched, values) == untouched
    assert expressions.evaluate(pl.DataFrame({"x": [1]}), fill_formula("{Name} || ' ' || {Day}", values))[0] == "O'Brien 2026-10-15"


def test_sql_gets_values_in_the_databases_way():
    values = {"Day": DAY, "Name": "x'; DROP TABLE Orders; --"}
    sql = """SELECT * FROM "{Day}" WHERE Day = {Day} AND Name = {Name} AND Note LIKE '%{Day}%' -- {Day}\n/* {Name} */ AND [{Name}] = 1"""
    assert fill_sql(sql, values) == (
        """SELECT * FROM "{Day}" WHERE Day = '2026-10-15' AND Name = 'x''; DROP TABLE Orders; --'"""
        """ AND Note LIKE '%2026-10-15%' -- {Day}\n/* {Name} */ AND [{Name}] = 1"""
    )
    assert fill_sql("WHERE Day = {Day}", values, literal=lambda v: f"<{v}>") == "WHERE Day = <2026-10-15>"
    assert fill_sql("'it''s {Day}' = {Day}", values) == "'it''s 2026-10-15' = '2026-10-15'"  # doubled quotes


def test_a_parameter_with_an_error_is_an_error_where_it_is_used():
    values = {"Day": Failed("Division by zero")}
    for fill in (fill_text, fill_formula, fill_sql):
        with pytest.raises(ActionError, match=r"The parameter \{Day\} has an error: Division by zero"):
            fill("x {day}", values)
    assert fill_text("x {Other}", values) == "x {Other}"


def test_left_over_names_in_formulas():
    assert left_in_formula("[A] + {Dya}") == "Dya"
    assert left_in_formula("'{Dya}' || [{Dya}] -- {Dya}") is None
    assert formula_places("{A} || '{B}' || [{C}]") == [(0, 3, "A", False), (8, 11, "B", True)]
    with pytest.raises(ActionError, match=r"there is no parameter \{Dya\}"):
        expressions.evaluate(pl.DataFrame({"A": [1]}), "[A] + {Dya}")


def _project(*pairs) -> Project:
    return Project(parameters=[Parameter(name, value) for name, value in pairs])


def test_values_in_order_formulas_use_the_ones_above():
    project = _project(("Day", "=DATE '2026-10-15' - 1"), ("Month", "=strftime({Day}, '%Y-%m')"), ("Limit", "500"))
    assert parameters.values(project) == {"Day": dt.date(2026, 10, 14), "Month": "2026-10", "Limit": 500}
    given = parameters.values(project, {"day": "2026-01-31", "LIMIT": "=1 + 1"})
    assert given == {"Day": dt.date(2026, 1, 31), "Month": "2026-01", "Limit": 2}
    assert parameters.values(_project(("Today", "=TODAY()")))["Today"] == dt.date.today()


def test_a_formula_with_an_error():
    found = parameters.values(_project(("Bad", "=1 +"), ("Later", "={Bad} + 1"), ("Below", "={Later2}")))
    assert isinstance(found["Bad"], Failed) and found["Bad"].message.startswith("the formula ends too early")
    assert found["Later"] == Failed("The parameter {Bad} has an error: " + found["Bad"].message)
    assert found["Below"].message.startswith("there is no parameter {Later2}")


def test_problems_with_names_and_overrides():
    project = _project(("Day", "1"), ("day", "2"), ("2nd", "3"), ("Run date", "4"))
    assert parameters.problems(project, {"Month": "x", "RUN DATE": "5"}) == [
        "There are two parameters named 'day'",
        "The parameter name '2nd' can't be used: letters, digits, _ and spaces, starting with a letter",
        "The project has no parameter 'Month' (its parameters: Day, day, 2nd, Run date)",
    ]


def test_filling_the_settings_of_a_step():
    params = [
        P.Text("file", "File"),
        P.Expression("condition", "Condition"),
        P.Code("query", "SQL", language="sql"),
        P.Choice("mode", "Mode", [("{Day}", "x")]),
        P.List("columns", "Columns", [P.Text("name", "Name"), P.Expression("formula", "Formula")]),
        P.Columns("keep", "Keep"),
    ]
    raw = {
        "file": "orders_{Day}.csv",
        "condition": "[Date] = {Day}",
        "query": "WHERE d = {Day}",
        "mode": "{Day}",
        "columns": [{"name": "Col {Day}", "formula": "{Day}"}],
        "keep": ["A", "B {Day}"],
        "other": "{Day}",
    }
    filled = parameters.fill(params, raw, {"Day": DAY})
    assert filled == {
        "file": "orders_2026-10-15.csv",
        "condition": "[Date] = DATE '2026-10-15'",
        "query": "WHERE d = '2026-10-15'",
        "mode": "{Day}",  # a choice is not typed
        "columns": [{"name": "Col 2026-10-15", "formula": "DATE '2026-10-15'"}],
        "keep": ["A", "B 2026-10-15"],
        "other": "{Day}",  # no setting of the action
    }
    assert raw["file"] == "orders_{Day}.csv"  # the step itself keeps {Day}
    assert parameters.fill(params, raw, {}) == raw


def test_renaming_writes_the_new_name_everywhere():
    settings = {"file": "a_{day}_{Month}.csv", "columns": [{"formula": "{DAY} + 1"}], "n": 3}
    assert parameters.renamed(settings, {"Day": "Run Day", "Month": "Day"}) == {
        "file": "a_{Run Day}_{Day}.csv", "columns": [{"formula": "{Run Day} + 1"}], "n": 3,
    }  # fmt: skip


def test_parameters_are_saved_with_the_project(tmp_path):
    project = _project(("Day", "=TODAY() - 1"), ("Folder", r"D:\Data"))
    loaded = Project.load(project.save(tmp_path / "p.tafl"))
    assert [(p.name, p.value) for p in loaded.parameters] == [("Day", "=TODAY() - 1"), ("Folder", r"D:\Data")]
    assert "parameters" not in Project().to_dict()


# --- in a calculation -------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path) -> Project:
    for day in ("2026-10-01", "2026-10-02"):
        (tmp_path / f"orders_{day}.csv").write_text(f"Id;Day\n1;{day}\n2;{day}\n3;2026-01-01\n", encoding="utf-8")
    project = Project(name="Test", parameters=[Parameter("Day", "2026-10-01"), Parameter("Min", "2")])
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders_{Day}.csv"})
    project.add_step(orders, "rows.filter_formula", {"condition": "[Day] = {Day} AND [Id] >= {Min}"})
    project.add_step(orders, "calc.columns", {"columns": [{"name": "Label {Min}", "formula": "'Day ' || {Day}"}]})
    return project


def test_a_calculation_fills_in_the_values(project, registry):
    result = Engine(project, registry).evaluate("Orders")
    assert result.ok, [s.error for s in result.steps]
    assert result.df.to_dicts() == [{"Id": 2, "Day": dt.date(2026, 10, 1), "Label 2": "Day 2026-10-01"}]


def test_other_values_calculate_again(project, registry):
    engine = Engine(project, registry)
    engine.evaluate("Orders")
    project.parameters[0].value = "2026-10-02"
    result = engine.evaluate("Orders")
    assert [s.cached for s in result.steps] == [False, False, False]
    assert result.df["Day"].to_list() == [dt.date(2026, 10, 2)]
    engine.parameter_overrides = {"day": "2026-10-01", "Min": "1"}
    assert engine.evaluate("Orders").df["Id"].to_list() == [1, 2]
    project.parameters[0].value = "2026-10-02"
    assert engine.evaluate("Orders").steps[0].cached  # the same values: from the cache


def test_a_parameter_with_an_error_stops_the_steps_that_use_it(project, registry):
    project.parameters[1].value = "=1 / 0 +"
    result = Engine(project, registry).evaluate("Orders")
    assert [s.status for s in result.steps] == [Status.OK, Status.ERROR, Status.BLOCKED]
    assert result.steps[1].error.startswith("The parameter {Min} has an error: the formula ends too early")


def test_an_unknown_name_in_a_formula(project, registry):
    project.tables[0].steps[1].params["condition"] = "[Id] > {Mni}"
    result = Engine(project, registry).evaluate("Orders")
    assert "there is no parameter {Mni} (Project → Parameters lists them)" in result.steps[1].error


def test_sql_and_connections_use_parameters(tmp_path, registry):
    with sqlite3.connect(tmp_path / "shop.db") as con:
        con.execute("CREATE TABLE Orders (Id INTEGER, Customer TEXT, Day TEXT)")
        con.executemany("INSERT INTO Orders VALUES (?, ?, ?)", [(1, "O'Brien", "2026-10-01"), (2, "Smith", "2026-10-02")])
    con.close()
    project = Project(
        name="Test",
        parameters=[Parameter("Db", "shop"), Parameter("Customer", "O'Brien"), Parameter("Day", "2026-10-01")],
        connections=[Connection("shop", "sqlite", {"file": "{Db}.db"})],
    )
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    query = "SELECT * FROM Orders WHERE Customer = {Customer} AND Day = {Day} -- {Day}"
    project.add_step(orders, "db.import", {"connection": "shop", "mode": "query", "query": query})
    engine = Engine(project, registry)
    result = engine.evaluate("Orders")
    assert result.ok, [s.error for s in result.steps]
    assert result.df["Id"].to_list() == [1]
    engine.parameter_overrides = {"Customer": "x' OR '1'='1", "Day": "2026-10-02"}
    assert engine.evaluate("Orders").df.height == 0  # a value, never SQL
    engine.parameter_overrides = {"Db": "other"}  # another database: read again
    assert "other.db" in engine.evaluate("Orders").steps[0].error


def test_a_changed_connection_reads_again(tmp_path, registry):
    for name, rows in (("a", 1), ("b", 2)):
        with sqlite3.connect(tmp_path / f"{name}.db") as con:
            con.execute("CREATE TABLE T (x INTEGER)")
            con.executemany("INSERT INTO T VALUES (?)", [(i,) for i in range(rows)])
        con.close()
    project = Project(name="Test", connections=[Connection("db", "sqlite", {"file": "a.db"})])
    project.path = tmp_path / "test.tafl"
    table = project.add_table("T")
    project.add_step(table, "db.import", {"connection": "db", "mode": "table", "table": "T"})
    engine = Engine(project, registry)
    assert engine.evaluate("T").df.height == 1
    project.connections[0].settings["file"] = "b.db"
    assert engine.evaluate("T").df.height == 2

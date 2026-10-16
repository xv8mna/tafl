"""The preview of a formula on the first rows (formula window)."""

import threading
import time

import polars as pl
import pytest

from tafl.api import Cancelled, expressions
from tafl.core.cancel import CancelEvent
from tafl.core.formula_preview import preview, used_columns

DATA = pl.DataFrame({"Net": [10.0, 20.0, None], "Name": ["a", "b", "c"], "Amount": [1, 2, 3]})


def test_result_and_used_columns():
    result = preview(DATA, "ROUND([Net] * 1.19, 2)", name="Gross")
    assert result.ok and result.message == "Result: number · no errors in all 3 rows"
    assert result.rows.columns == ["Net", "Gross"] and result.rows["Gross"].to_list() == [11.9, 23.8, None]
    assert [(c.name, c.type) for c in result.columns] == [("Net", "number"), ("Name", "text"), ("Amount", "integer")]
    assert preview(DATA, "[Net] + 1", name="Net").rows.columns == ["Net", "Net (new)"]  # replaces the column
    assert preview(DATA, "GETDATE()").rows.columns == ["Row", "Result"]  # no column used: numbered rows
    assert preview(DATA, "42", name="Row").rows.columns == ["Row", "Row (new)"]
    clash = pl.DataFrame({"Total": [1, 2], "Total (new)": [5, 6]})
    assert preview(clash, "[Total] + [Total (new)]", name="Total").rows.columns == ["Total", "Total (new)", "Total (new) (new)"]


def test_rows_above_come_first():
    result = preview(DATA, "[Gross] * 2", [("Gross ", "[Net] * 2")])  # the step ignores spaces around names
    assert result.ok and result.rows["Result"].to_list() == [40.0, 80.0, None]
    assert (result.columns[-1].name, result.columns[-1].row) == ("Gross", 1)
    replaced = preview(DATA, "[Net]", [("Net", "[Net] * 2")])  # replaced in place, as the step does
    assert [c.name for c in replaced.columns] == ["Name", "Amount", "Net"] and replaced.rows["Result"][0] == 20.0


def test_what_the_step_rejects_in_the_rows_above():
    broken = preview(DATA, "[Net] + 1", [("G", "[Nett] * 2")])
    assert not broken.ok and broken.message == "Row 1 ([G]) has an error: Column [Nett] not found (did you mean [Net]?)"
    assert broken.rows is not None  # this formula is fine: its values are shown
    assert preview(DATA, "[Gross] + 1", [("Gross", "")]).message == "Row 1 ([Gross]) has no formula yet."
    assert preview(DATA, "[Net] + 1", [("", "[Net] * 2")]).message == "Row 1 has no column name yet: the step needs one."
    assert preview(DATA, "[Net] + 1", [("", " ")]).message.startswith("Row 1 is still empty")
    twice = "Two new columns are named “G” (rows 1 and 2): give each one its own name."
    assert preview(DATA, "[G] + 1", [("G", "[Net] * 2"), ("G", "[Net] * 3")]).message == twice
    assert preview(DATA, "[Net] + 1", [("G", "[Net] * 2")], name=" G").message == twice  # this row's name too
    assert not preview(DATA, "", [("G", "")]).ok


def test_conditions_errors_and_no_data():
    counts = preview(DATA, "[Net] > 15", condition=True)
    assert counts.message == "True for 1, false for 1, empty for 1 (counts as false) of all 3 rows"
    assert not preview(DATA, "[Amount] + 1", condition=True).ok
    error = preview(DATA, "[Amont] * 2")
    assert not error.ok and error.message == "Column [Amont] not found (did you mean [Amount]?)"
    assert not preview(None, "[Net]").ok
    assert preview(None, "[Net]", no_data="The table is turned off.").message == "The table is turned off."
    assert preview(DATA, "  ").ok and preview(DATA, "  ").rows is None
    assert preview(DATA, "-- just a note").rows is None


def test_names_that_differ_only_in_case_give_the_steps_error():
    for formula in ("[NET] * 2", "net * 2"):
        result = preview(pl.DataFrame({"Net": [1.0], "NET": [10.0], "x": [0]}), formula)
        assert not result.ok and "differ only in upper and lower case" in result.message


def test_star_and_columns_use_all_columns():
    df = pl.DataFrame({"a1": [1, 2], "a2": [10, 20], "b": [100, 200]})
    assert preview(df, "GREATEST(*COLUMNS(*))").rows["Result"].to_list() == [100, 200]
    assert preview(df, "LIST_SUM(LIST_VALUE(*COLUMNS('a.*')))").rows["Result"].to_list() == [11, 22]
    assert preview(df, "COUNT(*) OVER ()").rows["Result"].to_list() == [2, 2]


def test_only_the_used_columns_are_evaluated():
    """Registering hundreds of columns costs more than the formula: they are left out."""
    wide = pl.DataFrame({f"c{i}": list(range(1500)) for i in range(300)})
    result = preview(wide, "[c1] + C299 + LENGTH('x')")
    assert result.message == "Result: integer · no errors in the first 1,000 rows"
    assert result.rows.columns == ["c1", "c299", "Result"] and result.rows["Result"][2] == 2 + 2 + 1
    assert used_columns("[Net] + amount + ROUND(x) + net", DATA.columns) == ["Net", "Amount"]
    assert preview(DATA, "(SELECT 1) + [Amount]").ok  # falls back to all columns when needed
    rows_above = [(f"n{i}", f"[c{i}] + 1") for i in range(10)]
    started = time.perf_counter()
    for _ in range(5):
        assert preview(wide, "[n9] * 2", rows_above).ok
    assert time.perf_counter() - started < 2  # the rows above are evaluated once, on their columns


def test_used_columns_are_what_the_formula_reads():
    columns = ["Date", "Day", "End", "Year", "x", 'x"y', 'x""y', "p]q", "p]]q"]
    assert used_columns("CAST([x] AS DATE) + INTERVAL 3 DAY", columns) == ["x"]
    assert used_columns("CASE WHEN [x] > '' THEN 1 END", columns) == ["x"]
    assert used_columns("DATE '2020-01-01' + INTERVAL 1 YEAR + x::date", columns) == ["x"]
    assert used_columns("Date + 1", columns) == ["Date"]
    assert used_columns('[x""y] + "p]]q"', columns) == ['x""y', "p]]q"]  # each kind of name has its own escape


def test_the_window_function_note():
    big = pl.DataFrame({"Overtime": list(range(1500))})
    assert "window functions" not in preview(big, "[Overtime] * 2").message
    condition = preview(big, "[Overtime] > AVG([Overtime]) OVER ()", condition=True)
    assert condition.message.endswith("(window functions see only these rows here)")


def test_a_cancelled_preview_stops_its_query():
    cancel = CancelEvent()
    slow = "(SELECT SUM(range % 7) FROM range(5000000000)) + [Amount]"
    threading.Timer(0.3, cancel.set).start()
    started = time.perf_counter()
    with pytest.raises(Cancelled):
        preview(DATA, slow, cancel=cancel)
    assert time.perf_counter() - started < 5
    assert preview(DATA, "[Amount] + 1").ok  # the connection works on


def test_where_the_mistake_is_and_what_to_write_instead():
    def mistake(formula):
        result = preview(DATA, formula)
        return formula[slice(*result.mistake)] if result.mistake else None, result.fix

    assert mistake("[Amont] * 2") == ("[Amont]", "[Amount]")
    assert mistake("amont * 2") == ("amont", "[Amount]")
    assert mistake("UPPPER([Name])") == ("UPPPER", "UPPER")
    assert mistake("DATEDIFF(day, [Net], [Net])") == ("day", "'day'")
    assert mistake("today + 1") == ("today", "TODAY()")
    assert mistake("[Net") == ("[Net", None)
    assert mistake("'abc") == ("'abc", None)
    assert mistake("(([Net] + 1)") == ("(", None)
    assert mistake("[Net] + 1)") == (")", None)
    assert mistake("ROUND([Net] 2)") == ("2", None)
    assert mistake("[Net] > 1 AND") == ("AND", None)
    assert mistake("[Net] + [Amount]") == (None, None)
    result = preview(DATA, "[Net] * 2")
    assert result.values.to_list() == [20.0, 40.0, None] and result.scope == "all 3 rows"


def test_calculate_rows_build_on_each_other():
    """Each row starts from the sample of the row above: ten rows cost ten evaluations, not 55."""
    frame = pl.DataFrame({"x": list(range(1500))})
    rows = [(f"n{i}", f"[{'x' if i == 0 else f'n{i - 1}'}] + 1") for i in range(10)]
    for number in range(10):
        assert preview(frame, "[x]", rows[:number]).ok
    calls = []
    original = expressions.evaluate
    try:
        expressions.evaluate = lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs)
        assert preview(frame, "[n9] * 2", rows).rows["Result"][0] == 20
    finally:
        expressions.evaluate = original
    assert len(calls) == 2  # the last row above and this formula: the other rows were kept


def test_a_new_column_needs_a_name_and_a_formula_more_than_comments():
    nameless = preview(DATA, "[Net] * 2", creates=True)
    assert not nameless.ok and nameless.message == "This row has no column name yet: the step needs one."
    assert nameless.values is not None  # the values are shown all the same
    assert preview(DATA, "[Net] * 2", name="Double", creates=True).ok
    assert preview(DATA, "[Net] * 2").ok  # not a new column (e.g. a condition): no name needed
    assert preview(DATA, "-- later").message == "The formula is empty: it has only comments."


def test_the_kept_sample_lets_go_of_its_input():
    from tafl.core import formula_preview

    frame = pl.DataFrame({"x": [1, 2]})
    preview(frame, "[x]")
    assert formula_preview._cache_frame is frame
    preview(None, "[x]")  # the input is gone (e.g. an earlier step fails)
    assert formula_preview._cache_frame is None and not formula_preview._cache


def test_parameters_are_filled_in_first():
    from tafl.api.parameters import Failed

    values = {"Rate": 1.5, "Bad": Failed("oops")}
    result = preview(DATA, "[Amount] * {rate}", parameters=values)
    assert result.ok and result.values.to_list() == [1.5, 3.0, 4.5] and result.rows.columns == ["Amount", "Result"]
    assert preview(DATA, "[Amount] * {Rate}").message.startswith("There is no parameter {Rate}")
    missing = preview(DATA, "[Amount] * {Rat}", parameters=values)
    assert missing.message.startswith("There is no parameter {Rat}") and missing.mistake == (11, 16)
    bad = preview(DATA, "[Amount] * {bad}", parameters=values)
    assert bad.message == "The parameter {Bad} has an error: oops" and bad.mistake == (11, 16)
    assert bad.columns  # the columns are still offered
    above = preview(DATA, "[X]", [("X", "{Bad}")], creates=True, name="Y", parameters=values)
    assert above.message == "Row 1 ([X]) has an error: The parameter {Bad} has an error: oops"
    unknown = preview(DATA, "{Rate} + [Nett]", parameters=values)
    assert unknown.mistake == (9, 15) and unknown.fix == "[Net]"  # found in the formula as typed


def test_parameters_in_the_names_of_new_columns(tmp_path, registry):
    """Calculate columns fills {Name} into the names of the new columns and into the formulas
    ([Sales {Year}]): the preview does the same, so it agrees with the calculation."""
    from tafl.api.parameters import Failed
    from tafl.core import Engine, Parameter, Project

    (tmp_path / "data.csv").write_text("Amount\n1\n2\n3\n", encoding="utf-8")
    project = Project(name="Test", parameters=[Parameter("Year", "2026")])
    project.path = tmp_path / "test.tafl"
    table = project.add_table("T")
    project.add_step(table, "csv.import", {"file": "data.csv"})
    rows = [("Sales {Year}", "[Amount] * 10"), ("Double {Year}", "[Sales {Year}] * 2")]
    project.add_step(table, "calc.columns", {"columns": [{"name": name, "formula": formula} for name, formula in rows]})
    engine = Engine(project, registry)
    calculated = engine.evaluate("T")
    assert calculated.ok, calculated.first_error
    assert calculated.df.columns == ["Amount", "Sales 2026", "Double 2026"]

    values = engine.parameter_values()
    result = preview(DATA, rows[1][1], rows[:1], creates=True, name=rows[1][0], parameters=values)
    assert result.ok, result.message
    assert result.rows.columns == ["Sales 2026", "Double 2026"]
    assert result.values.to_list() == calculated.df["Double 2026"].to_list() == [20, 40, 60]
    made = [(c.name, c.label) for c in result.columns if c.row is not None]
    assert made == [("Sales 2026", "Sales {Year}")]  # offered as written: it follows the parameter
    same = preview(DATA, "1", [("Sales {Year}", "1")], creates=True, name="Sales 2026", parameters=values)
    assert same.message.startswith("Two new columns are named “Sales 2026”")  # as the step sees them

    values["Bad"] = Failed("oops")
    above = preview(DATA, "[Amount]", [("Sales {Bad}", "1")], creates=True, name="X", parameters=values)
    assert above.message == "Row 1 ([Sales {Bad}]) has an error: The parameter {Bad} has an error: oops"
    named = preview(DATA, "[Amount]", creates=True, name="X {Bad}", parameters=values)
    assert not named.ok and named.message == "The parameter {Bad} has an error: oops" and named.columns

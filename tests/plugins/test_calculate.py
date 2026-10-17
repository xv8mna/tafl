import datetime as dt

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.params import ValidationContext


def test_calculate_columns(act, people):
    out = act(
        "calc.columns",
        people,
        {
            "columns": [
                {"name": "Label", "formula": "CONCAT(ISNULL([Name], '?'), ' (', [Age], ')')"},
                {"name": "Older", "formula": "[Age] + 10"},
                {"name": "Band", "formula": "CASE WHEN [Older] >= 50 THEN 'senior' ELSE 'junior' END"},
            ]
        },
    )
    assert out["Label"].to_list()[:2] == ["Anna (34)", "ben (27)"]
    assert out["Label"][3] == "? (19)"
    assert out["Band"].to_list() == ["junior", "junior", "senior", "junior", "senior"]


def test_two_new_columns_need_different_names(act, people):
    with pytest.raises(ActionError, match="Two new columns are named “test” \\(rows 2 and 3\\)"):
        act("calc.columns", people, {"columns": [
            {"name": "X", "formula": "1"}, {"name": "test", "formula": "1"}, {"name": " test", "formula": "2"},
        ]})
    replaced = act("calc.columns", people, {"columns": [{"name": "Age", "formula": "[Age] + 1"}]})
    assert replaced.columns == people.columns  # a column from before is replaced in place
    both = act("calc.columns", people, {"columns": [{"name": "test", "formula": "1"}, {"name": "Test", "formula": "2"}]})
    assert both.columns[-2:] == ["test", "Test"]


def test_number_rows_within_groups_in_an_order(act):
    df = pl.DataFrame({"Customer": ["A", "B", "A", "A", "B"], "Amount": [5, 1, 9, 7, 3]})
    out = act(
        "calc.enumerate",
        df,
        {"name": "Rank", "group_by": ["Customer"], "order_by": [{"column": "Amount", "order": "desc"}]},
    )
    assert out["Amount"].to_list() == [5, 1, 9, 7, 3]  # row order unchanged
    assert out["Rank"].to_list() == [3, 2, 1, 2, 1]
    plain = act("calc.enumerate", df, {"start": 10, "step": 10})
    assert plain["No"].to_list() == [10, 20, 30, 40, 50]


def test_number_groups(act):
    df = pl.DataFrame({"Customer": ["B", "A", "B", None, "A"]})
    out = act("calc.enumerate", df, {"name": "Group", "mode": "groups", "group_by": ["Customer"]})
    assert out["Group"].to_list() == [1, 2, 1, 3, 2]
    ordered = act(
        "calc.enumerate", df, {"mode": "groups", "group_by": ["Customer"], "order_by": [{"column": "Customer"}]}
    )
    assert ordered["No"].to_list() == [2, 1, 2, 3, 1]
    with pytest.raises(ActionError, match="form the groups"):
        act("calc.enumerate", df, {"mode": "groups"})


def test_aggregate(act, people):
    out = act(
        "calc.aggregate",
        people,
        {
            "group_by": ["City"],
            "calculations": [
                {"function": "sum", "column": "Age"},
                {"function": "count_rows"},
                {"function": "count", "column": "Score"},
                {"function": "join", "column": "Name", "name": "Names"},
            ],
        },
    )
    assert out.to_dicts() == [
        {"City": "Berlin", "Sum of Age": 75, "Count": 2, "Count values of Score": 2, "Names": "Anna, Carla"},
        {"City": "Hamburg", "Sum of Age": 27, "Count": 1, "Count values of Score": 0, "Names": "ben"},
        {"City": "Köln", "Sum of Age": 19, "Count": 1, "Count values of Score": 1, "Names": ""},
        {"City": None, "Sum of Age": 58, "Count": 1, "Count values of Score": 1, "Names": "Dieter"},
    ]
    assert out.schema["Count"] == pl.Int64 and out.schema["Count values of Score"] == pl.Int64


def test_a_new_aggregate_step_is_incomplete(registry):
    action = registry.actions["calc.aggregate"]
    calculations = action.params[1]
    new = action.resolve({"calculations": [calculations.new_row()]})  # a new step's first row
    assert new.calculations[0].function == "sum" and new.calculations[0].column == ""
    assert action.check(new, ValidationContext()) == ["Calculate, row 1 – Column: required"]  # not an error of the run
    assert action.describe(new) == "Aggregate: Sum of ?"
    rows = action.resolve({"calculations": [{"function": "count_rows"}]})  # no column to choose
    assert action.check(rows, ValidationContext()) == [] and action.describe(rows) == "Aggregate: Count"


def test_aggregate_totals_and_name_clashes(act, people):
    total = act("calc.aggregate", people, {"calculations": [{"function": "mean", "column": "Age"}]})
    assert total.to_dicts() == [{"Average of Age": 35.8}]
    with pytest.raises(ActionError, match="used twice"):
        act("calc.aggregate", people, {"group_by": ["City"], "calculations": [{"function": "min", "column": "Age", "name": "City"}]})


def test_errors_name_the_new_column(act, people):
    with pytest.raises(ActionError, match=r"Formula error in \[Gross\]: column \[Agee\] not found"):
        act("calc.columns", people, {"columns": [{"name": "Double", "formula": "[Age] * 2"}, {"name": "Gross", "formula": "[Agee] * 2"}]})


def test_aggregate_names_columns_of_the_wrong_type(act, people):
    with pytest.raises(ActionError, match="^Sum needs a number column; City is text$"):
        act("calc.aggregate", people, {"calculations": [{"function": "sum", "column": "City"}]})
    days = pl.DataFrame({"Day": [dt.date(2025, 1, 1), dt.date(2025, 1, 3)]})
    with pytest.raises(ActionError, match="^Sum needs a number column; Day is a date column$"):
        act("calc.aggregate", days, {"calculations": [{"function": "sum", "column": "Day"}]})
    with pytest.raises(ActionError, match="^Median needs a number or date column; City is text$"):
        act("calc.aggregate", people, {"calculations": [{"function": "median", "column": "City"}]})
    average = act("calc.aggregate", days, {"calculations": [{"function": "mean", "column": "Day"}]})
    assert average["Average of Day"].to_list() == [dt.datetime(2025, 1, 2)]
    assert act("calc.aggregate", people, {"calculations": [{"function": "max", "column": "City"}]}).item() == "Köln"


def test_number_rows_in_text_order_like_sort(act):
    df = pl.DataFrame({"Name": ["Zebra", "Äpfel", "apfel", "Peter"]})
    out = act("calc.enumerate", df, {"order_by": [{"column": "Name"}]})
    assert out["No"].to_list() == [4, 1, 2, 3]

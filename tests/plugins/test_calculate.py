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


def test_aggregate_sums_beyond_the_whole_numbers_stop_the_step(act):
    """Polars lets a sum of Int64 overflow (-446744073709551616 here): a wrong number would be
    worse than a message."""
    big = 9_000_000_000_000_000_000
    df = pl.DataFrame({"K": ["a", "a", "b", "b"], "V": [big, big, big, -big + 7]})
    sums = {"calculations": [{"function": "sum", "column": "V"}, {"function": "count_rows"}]}
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["K"], **sums})
    assert str(raised.value) == (
        "“Sum of V” is 18,000,000,000,000,000,000 in a group: too big for a whole number "
        "(at most 9,223,372,036,854,775,807). Change “V” to a decimal number before this step."
    )
    with pytest.raises(ActionError, match="^“Total” is 18,000,000,000,000,000,007: too big"):
        act("calc.aggregate", df, {"calculations": [{"function": "sum", "column": "V", "name": "Total"}]})
    fits = act("calc.aggregate", df.filter(pl.col("K") == "b"), {"group_by": ["K"], **sums})
    assert fits.rows() == [("b", 7, 2)] and fits.schema == pl.Schema({"K": pl.String, "Sum of V": pl.Int64, "Count": pl.Int64})
    small = act("calc.aggregate", pl.DataFrame({"V": [1, 2, None]}), sums)  # as before
    assert small.rows() == [(3, 3)] and small.schema["Sum of V"] == pl.Int64


def test_aggregate_minimum_and_maximum_of_lists_stop_the_step(act):
    """Polars can't compare lists or records: it gave empty values (as Pivot does now)."""
    df = pl.DataFrame({"K": ["a", "a", "b"], "L": [[3, 1], [2], [5]], "S": [{"a": 1}, {"a": 2}, {"a": 3}]})
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["K"], "calculations": [{"function": "min", "column": "L"}]})
    assert str(raised.value) == "“L” holds list(int64) values, which Minimum can't compare"
    with pytest.raises(ActionError, match="^“S” holds struct.* values, which Maximum can't compare$"):
        act("calc.aggregate", df, {"calculations": [{"function": "max", "column": "S"}]})
    first = act("calc.aggregate", df, {"group_by": ["K"], "calculations": [{"function": "first", "column": "L"}]})
    assert first.rows() == [("a", [3, 1]), ("b", [5])]


def test_aggregate_sums_of_durations_beyond_292_years_stop_the_step(act):
    """A duration in nanoseconds (as Parquet files often have them) holds up to 292 years; Polars
    lets a longer sum overflow, like one of whole numbers."""
    days = pl.Series([dt.timedelta(days=d) for d in (60_000, 60_000, -60_000, -1)], dtype=pl.Duration("ns"))
    df = pl.DataFrame({"Site": ["A", "A", "B", "B"], "Runtime": days})
    sums = {"calculations": [{"function": "sum", "column": "Runtime"}]}
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["Site"], **sums})
    assert str(raised.value) == (
        "“Sum of Runtime” is 120,000 days in a group: too long for a duration (at most 106,751 days). "
        "Change “Runtime” to hours before this step, with the formula EPOCH([Runtime]) / 3600."
    )
    total = act("calc.aggregate", df, sums)  # beyond only on the way
    assert total.rows() == [(dt.timedelta(days=59_999),)] and total.schema["Sum of Runtime"] == pl.Duration("ns")
    micro = act("calc.aggregate", df.with_columns(pl.col("Runtime").cast(pl.Duration("us"))), {"group_by": ["Site"], **sums})
    assert micro.rows() == [("A", dt.timedelta(days=120_000)), ("B", dt.timedelta(days=-60_001))]


def test_number_rows_in_text_order_like_sort(act):
    df = pl.DataFrame({"Name": ["Zebra", "Äpfel", "apfel", "Peter"]})
    out = act("calc.enumerate", df, {"order_by": [{"column": "Name"}]})
    assert out["No"].to_list() == [4, 1, 2, 3]

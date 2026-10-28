"""Pivot: the values of one column become columns."""

import datetime as dt
import time
from decimal import Decimal as D

import polars as pl
import pytest

from tafl.api import ActionError, Params
from tafl.api.params import ValidationContext
from tafl.api.testing import StubContext
from tafl.core import Engine, Project
from tafl.plugins import reshape

SALES = pl.DataFrame(
    {
        "Region": ["Süd", "Nord", "Süd", "Nord", "West", "Nord"],
        "Month": ["2025-02", "2025-01", "2025-01", "2025-02", "2025-03", "2025-01"],
        "Amount": [20, 10, 5, 7, 3, 1],
    }
)


def pivot(act, df, ctx=None, **params):
    return act("reshape.pivot", df, params, ctx)


def test_the_settings(registry):
    action = registry.actions["reshape.pivot"]
    assert (action.name, action.category, action.kind, action.version) == ("Pivot", "Columns", "transform", 1)
    assert action.resolve({}) == {"rows": [], "column": "", "value": "", "function": "sum", "order": "sorted"}
    assert [label for _, label in action.params[3].options] == [
        "Sum", "Count values (not empty)", "Average", "Minimum", "Maximum", "First value", "Last value",
    ]
    assert action.description.startswith("Turns the values of one column into columns, like an Excel pivot table")
    assert "1,000 new columns, and at most 50,000,000 cells (rows × new columns)." in action.description
    assert "(text with nothing but spaces counts here, as in Aggregate)" in action.description
    assert action.check(action.resolve({}), ValidationContext()) == ["Column names from: required", "Values from: required"]


def test_the_summary(registry):
    action = registry.actions["reshape.pivot"]
    summary = action.describe(Params(rows=["Region"], column="Month", value="Amount", function="sum"))
    assert summary == "Pivot Month into columns: Sum of Amount by Region"
    summary = action.describe(Params(rows=["Customer", "City"], column="Status", value="OrderNo", function="count"))
    assert summary == "Pivot Status into columns: Count values of OrderNo by Customer, City"
    total = action.describe(Params(rows=[], column="Year", value="Price", function="mean"))
    assert total == "Pivot Year into columns: Average of Price"
    project = Project(name="Test")
    table = project.add_table("Orders")
    step = project.add_step(table, "reshape.pivot", {})
    assert Engine(project, registry).describe(step) == "Pivot ? into columns: Sum of ?"


def test_row_keys_that_are_also_the_names_or_the_values(registry):
    action = registry.actions["reshape.pivot"]
    both = action.resolve({"rows": ["Region", "Month", "Amount"], "column": "Month", "value": "Amount"})
    assert action.check(both, ValidationContext(columns=SALES.columns)) == [
        "Row keys: “Month” gives the column names, so it can't be a row key too",
        "Row keys: “Amount” holds the values, so it can't be a row key too",
    ]
    same = action.resolve({"rows": ["Month"], "column": "Month", "value": "Month"})
    assert action.check(same, ValidationContext()) == [
        "Row keys: “Month” gives the column names, so it can't be a row key too"
    ]
    counted = action.resolve({"rows": ["Region"], "column": "Month", "value": "Month", "function": "count"})
    assert action.check(counted, ValidationContext(columns=SALES.columns)) == []  # names and values from one column
    assert action.check(action.resolve({"rows": 5, "column": "Month", "value": "Amount"}), ValidationContext()) == [
        "Row keys (empty = one total row): 5 is not a list of columns"
    ]


def test_sums_by_region_and_month(act):
    out = pivot(act, SALES, rows=["Region"], column="Month", value="Amount")
    assert out.columns == ["Region", "2025-01", "2025-02", "2025-03"]
    assert out.rows() == [("Süd", 5, 20, None), ("Nord", 11, 7, None), ("West", None, None, 3)]  # as the keys first appear
    assert out.schema["2025-01"] == pl.Int64
    counted = pivot(act, SALES, rows=["Region"], column="Month", value="Month", function="count")  # one column for both
    assert counted.rows() == [("Süd", 1, 1, None), ("Nord", 2, 1, None), ("West", None, None, 1)]


def test_the_order_of_the_new_columns(act):
    first = pivot(act, SALES, rows=["Region"], column="Month", value="Amount", order="first")
    assert first.columns == ["Region", "2025-02", "2025-01", "2025-03"]
    numbers = pl.DataFrame({"K": [1] * 4, "N": [10, 2, 1, 2], "V": [1, 2, 3, 4]})
    assert pivot(act, numbers, rows=["K"], column="N", value="V").columns == ["K", "1", "2", "10"]  # not as text
    assert pivot(act, numbers, rows=["K"], column="N", value="V", order="first").columns == ["K", "10", "2", "1"]


def test_cells_without_rows_and_cells_of_only_empty_values(act):
    df = pl.DataFrame({"K": ["a", "a", "b"], "O": ["x", "y", "x"], "V": [None, 1, 2]}, schema_overrides={"V": pl.Int64})
    assert pivot(act, df, rows=["K"], column="O", value="V").rows() == [("a", None, 1), ("b", 2, None)]
    assert pivot(act, df, rows=["K"], column="O", value="V", function="count").rows() == [("a", 0, 1), ("b", 1, None)]
    for function in ("mean", "min", "max", "first", "last"):
        out = pivot(act, df, rows=["K"], column="O", value="V", function=function)
        assert out.rows() == [("a", None, 1), ("b", 2, None)]


FUNCTIONS = pl.DataFrame(
    {
        "K": ["a", "a", "a", "b"],
        "O": ["x", "x", "x", "x"],
        "Int": [3, None, 1, 4],
        "Float": [1.5, 2.25, None, 0.5],
        "Decimal": pl.Series([D("1.10"), D("2.25"), None, D("3.00")], dtype=pl.Decimal(10, 2)),
        "Date": [dt.date(2025, 1, 1), dt.date(2025, 1, 4), None, dt.date(2025, 2, 1)],
        "Text": ["b", "A", None, "c"],
        "Bool": [True, True, False, None],
    }
)


@pytest.mark.parametrize(
    ("column", "function", "expected", "dtype"),
    [
        ("Int", "sum", [4, 4], pl.Int64),
        ("Int", "count", [2, 1], pl.Int64),
        ("Int", "mean", [2.0, 4.0], pl.Float64),
        ("Int", "min", [1, 4], pl.Int64),
        ("Int", "max", [3, 4], pl.Int64),
        ("Int", "first", [3, 4], pl.Int64),
        ("Int", "last", [1, 4], pl.Int64),
        ("Float", "sum", [3.75, 0.5], pl.Float64),
        ("Float", "mean", [1.875, 0.5], pl.Float64),
        ("Float", "max", [2.25, 0.5], pl.Float64),
        ("Decimal", "sum", [D("3.35"), D("3.00")], pl.Decimal(38, 2)),
        ("Decimal", "mean", [1.675, 3.0], pl.Float64),
        ("Decimal", "min", [D("1.10"), D("3.00")], pl.Decimal(10, 2)),
        ("Date", "mean", [dt.datetime(2025, 1, 2, 12), dt.datetime(2025, 2, 1)], pl.Datetime("us")),
        ("Date", "min", [dt.date(2025, 1, 1), dt.date(2025, 2, 1)], pl.Date),
        ("Date", "last", [dt.date(2025, 1, 4), dt.date(2025, 2, 1)], pl.Date),
        ("Date", "count", [2, 1], pl.Int64),
        ("Text", "min", ["A", "c"], pl.String),  # by character codes, as Aggregate
        ("Text", "max", ["b", "c"], pl.String),
        ("Text", "first", ["b", "c"], pl.String),
        ("Text", "last", ["A", "c"], pl.String),
        ("Text", "count", [2, 1], pl.Int64),
        ("Bool", "sum", [2, None], pl.Int64),
        ("Bool", "mean", [2 / 3, None], pl.Float64),
        ("Bool", "count", [3, 0], pl.Int64),
        ("Bool", "first", [True, None], pl.Boolean),
    ],
)
def test_every_function_on_every_type(act, column, function, expected, dtype):
    out = pivot(act, FUNCTIONS, rows=["K"], column="O", value=column, function=function)
    assert out["x"].to_list() == expected
    assert out.schema["x"] == dtype


def test_sums_of_narrow_whole_numbers_dont_overflow(act):
    df = pl.DataFrame({"O": ["x", "x"], "V": pl.Series([2_147_483_647, 5], dtype=pl.Int32)})
    out = pivot(act, df, column="O", value="V")
    assert out.rows() == [(2_147_483_652,)] and out.schema["x"] == pl.Int64
    small = pl.DataFrame({"O": ["x", "x"], "V": pl.Series([200, 100], dtype=pl.UInt8)})
    assert pivot(act, small, column="O", value="V").rows() == [(300,)]
    durations = pl.DataFrame({"O": ["x", "x"], "V": [dt.timedelta(hours=1), dt.timedelta(hours=2)]})
    assert pivot(act, durations, column="O", value="V").rows() == [(dt.timedelta(hours=3),)]


def test_sums_beyond_the_whole_numbers_stop_the_step(act):
    """Polars lets a sum of Int64 overflow: such sums are added up as Int128, and one that doesn't
    fit back stops the step (a wrong number would be worse)."""
    big = 9_000_000_000_000_000_000
    df = pl.DataFrame({"K": [1, 1, 2, 2], "O": ["x", "x", "x", "y"], "V": [big, big, 5, None]})
    with pytest.raises(ActionError) as raised:
        pivot(act, df, rows=["K"], column="O", value="V")
    assert str(raised.value) == (
        "The sum of “V” in “x” is 18,000,000,000,000,000,000: too big for a whole number "
        "(at most 9,223,372,036,854,775,807). Change “V” to a decimal number before this step."
    )
    negative = df.with_columns(-pl.col("V"))
    with pytest.raises(ActionError, match="is -18,000,000,000,000,000,000: too small for a whole number"):
        pivot(act, negative, rows=["K"], column="O", value="V")
    fits = pl.DataFrame({"K": [1, 1, 2, 2], "O": ["x", "y", "x", "x"], "V": [big, big, big, -big + 7]})
    out = pivot(act, fits, rows=["K"], column="O", value="V")
    assert out.rows() == [(1, big, big), (2, 7, None)] and out.schema == pl.Schema({"K": pl.Int64, "x": pl.Int64, "y": pl.Int64})
    unsigned = pl.DataFrame({"O": ["x", "x"], "V": pl.Series([2**63, 2**62], dtype=pl.UInt64)})
    out = pivot(act, unsigned, column="O", value="V")
    assert out.rows() == [(2**63 + 2**62,)] and out.schema["x"] == pl.UInt64
    with pytest.raises(ActionError, match="too big for a whole number \\(at most 18,446,744,073,709,551,615\\)"):
        pivot(act, unsigned.with_columns(pl.lit(2**63, dtype=pl.UInt64).alias("V")), column="O", value="V")
    counted = pivot(act, df, rows=["K"], column="O", value="V", function="count")
    assert counted.rows() == [(1, 2, None), (2, 1, 0)]  # the other functions are as they were
    on_the_way = pl.DataFrame({"O": ["x"] * 4, "V": [big, big, -big, -big + 7]})  # beyond Int64 only on the way
    assert pivot(act, on_the_way, column="O", value="V").rows() == [(7,)]


def test_sums_of_durations_beyond_292_years_stop_the_step(act):
    """A duration in nanoseconds (as Parquet files often have them) holds up to 292 years; Polars
    lets a longer sum overflow, like one of whole numbers."""
    days = pl.Series([dt.timedelta(days=60_000)] * 4 + [dt.timedelta(days=1)], dtype=pl.Duration("ns"))
    df = pl.DataFrame({"Site": ["A", "A", "B", "B", "B"], "O": ["x", "x", "x", "y", "y"], "Runtime": days})
    with pytest.raises(ActionError) as raised:
        pivot(act, df, rows=["Site"], column="O", value="Runtime")
    assert str(raised.value) == (
        "The sum of “Runtime” in “x” is 120,000 days: too long for a duration (at most 106,751 days). "
        "Change “Runtime” to hours before this step, with the formula EPOCH([Runtime]) / 3600."
    )
    micro = pivot(act, df.with_columns(pl.col("Runtime").cast(pl.Duration("us"))), rows=["Site"], column="O", value="Runtime")
    assert micro.rows() == [("A", dt.timedelta(days=120_000), None), ("B", dt.timedelta(days=60_000), dt.timedelta(days=60_001))]
    assert micro.schema["x"] == pl.Duration("us")
    back = df.with_columns(pl.when(pl.col("Site") == "B").then(-pl.col("Runtime")).otherwise(pl.col("Runtime")))
    fits = pivot(act, back, column="O", value="Runtime")  # 120,000 - 60,000 days: beyond only on the way
    assert fits.rows() == [(dt.timedelta(days=60_000), dt.timedelta(days=-60_001))] and fits.schema["x"] == pl.Duration("ns")


def test_sums_are_checked_without_a_wider_result(act, monkeypatch):
    """Sums that might be beyond Int64 are checked cell by cell first: the pivot itself is in the
    type of the values, as a result of Int128 would need twice the memory."""
    pivoted = []
    pivot_of_polars = pl.DataFrame.pivot

    def recorded(self, *args, **kwargs):
        pivoted.append(self.schema)
        return pivot_of_polars(self, *args, **kwargs)

    monkeypatch.setattr(pl.DataFrame, "pivot", recorded)
    big = 9_000_000_000_000_000_000
    df = pl.DataFrame({"K": [1, 1, 2], "O": ["x", "y", "x"], "V": [big, big, 5]})
    assert pivot(act, df, rows=["K"], column="O", value="V").rows() == [(1, big, big), (2, 5, None)]
    assert pl.Int128 not in pivoted[0].values()


def test_minimum_and_maximum_of_lists_stop_the_step(act):
    """Polars can't compare lists or records: it would give empty cells."""
    df = pl.DataFrame({"K": ["a", "a", "b"], "O": ["x"] * 3, "L": [[3, 1], [2], [5]], "S": [{"a": 1}, {"a": 2}, {"a": 3}]})
    with pytest.raises(ActionError) as raised:
        pivot(act, df, rows=["K"], column="O", value="L", function="min")
    assert str(raised.value) == "“L” holds list(int64) values, which Minimum can't compare"
    with pytest.raises(ActionError, match="^“S” holds struct.* values, which Maximum can't compare$"):
        pivot(act, df, rows=["K"], column="O", value="S", function="max")
    assert pivot(act, df, rows=["K"], column="O", value="L", function="first").rows() == [("a", [3, 1]), ("b", [5])]
    assert pivot(act, df, rows=["K"], column="O", value="L", function="count").rows() == [("a", 2), ("b", 1)]


@pytest.mark.parametrize(
    ("column", "function", "message"),
    [
        ("Text", "sum", "Sum needs a number column; “Text” is text"),
        ("Date", "sum", "Sum needs a number column; “Date” is a date column"),
        ("Text", "mean", "Average needs a number or date column; “Text” is text"),
    ],
)
def test_functions_that_need_numbers(act, column, function, message):
    with pytest.raises(ActionError) as raised:
        pivot(act, FUNCTIONS, rows=["K"], column="O", value=column, function=function)
    assert str(raised.value) == message


def names(act, values, **params) -> list[str]:
    df = pl.DataFrame({"K": [1] * len(values), "O": values, "V": list(range(len(values)))}, strict=False)
    return pivot(act, df, rows=["K"], column="O", value="V", function="count", **params).columns[1:]


def test_names_from_numbers_dates_and_true_false(act):
    assert names(act, [10, 2, -5, 1234]) == ["-5", "2", "10", "1234"]
    assert names(act, [2.0, 1.5, 0.1, -0.0, 1e20, 1e-11, float("nan")]) == [
        "0", "0.00000000001", "0.1", "1.5", "2", "100000000000000000000", "NaN",
    ]
    assert names(act, [1e16, 1e-7, 0.1 + 0.2, 2.0**53]) == [
        "0.0000001", "0.30000000000000004", "9007199254740992", "10000000000000000"
    ]
    assert names(act, [0.0, -0.0, 123456789012345.0]) == ["0", "123456789012345"]  # 0 and -0 are one value
    assert names(act, [D("5.00"), D("-1.25")]) == ["-1.25", "5.00"]
    assert names(act, [dt.date(2025, 12, 31), dt.date(2025, 1, 2)]) == ["2025-01-02", "2025-12-31"]
    moments = [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 8, 30, 0, 500_000)]
    assert names(act, moments) == ["2025-12-31 08:30:00", "2025-12-31 08:30:00.500"]
    assert names(act, [dt.time(8, 30, 0, 500_000), dt.time(8, 30)]) == ["08:30:00", "08:30:00.500"]
    assert names(act, [dt.timedelta(days=1, hours=2, minutes=3), dt.timedelta(seconds=1.5)]) == ["1s 500ms", "1d 2h 3m"]
    assert names(act, [True, False]) == ["false", "true"]
    assert names(act, ["b", "a"], order="first") == ["b", "a"]


def test_text_names_are_sorted_as_sort_does(act):
    texts = ["nord", "Nord", "Ärger", "apfel", "Zoo", " Nord"]
    assert names(act, texts) == [" Nord", "apfel", "Ärger", "nord", "Nord", "Zoo"]
    categories = pl.DataFrame({"O": pl.Series(["b", "A", "c"], dtype=pl.Categorical), "V": [1, 2, 3]})
    assert pivot(act, categories, column="O", value="V").columns == ["A", "b", "c"]


def test_empty_values_go_into_one_column(act):
    df = pl.DataFrame({"K": ["a", "a", "b", "b", "a"], "O": ["", None, "x", "  ", "x"], "V": [1, 2, 3, 4, 5]})
    out = pivot(act, df, rows=["K"], column="O", value="V")
    assert out.columns == ["K", "x", "(empty)"] and out.rows() == [("a", 5, 3), ("b", 3, 4)]
    first = pivot(act, df, rows=["K"], column="O", value="V", order="first")
    assert first.columns == ["K", "(empty)", "x"] and first.rows() == [("a", 3, 5), ("b", 4, 3)]
    assert names(act, [None, 1, None]) == ["1", "(empty)"]
    assert names(act, [None, None]) == ["(empty)"]  # a column of type Null


def test_names_that_are_taken(act):
    ctx = StubContext()
    df = pl.DataFrame({"Region": ["a", "a", "b"], "O": ["(empty)", None, "Region"], "V": [1, 2, 3]})
    out = pivot(act, df, ctx, rows=["Region"], column="O", value="V")
    assert out.columns == ["Region", "(empty)", "Region (2)", "(empty) (2)"]
    assert out.rows() == [("a", 1, None, 2), ("b", None, 3, None)]
    assert ctx.messages == [
        ("info", "Two columns would be named “Region”: the new one is named “Region (2)”"),
        ("info", "Two columns would be named “(empty)”: the new one is named “(empty) (2)”"),
    ]
    internal = pl.DataFrame({"__names__": ["a", "b"], "O": ["__names__", "__values__"], "__values__": [1, 2]})
    out = pivot(act, internal, rows=["__names__"], column="O", value="__values__")  # the names the step works with
    assert out.columns == ["__names__", "__names__ (2)", "__values__"]
    assert out.rows() == [("a", 1, None), ("b", None, 2)]
    digits = pl.DataFrame({"0": ["a", "b"], "row 0": ["c", "d"], "O": [1, 0], "V": [1, 2]})  # as the step's working names
    out = pivot(act, digits, rows=["0", "row 0"], column="O", value="V")
    assert out.columns == ["0", "row 0", "0 (2)", "1"] and out.rows() == [("a", "c", None, 1), ("b", "d", 2, None)]


def test_a_number_never_takes_the_name_of_another_value(act):
    """Otherwise a column would hold another value on a day without the value that took the number."""
    ctx = StubContext()
    numbered = pl.DataFrame({"K": [1, 1, 1], "O": ["K", "K (2)", "x"], "V": [1, 2, 3]})
    out = pivot(act, numbered, ctx, rows=["K"], column="O", value="V")
    assert out.columns == ["K", "K (3)", "K (2)", "x"] and out.rows() == [(1, 1, 2, 3)]
    assert ctx.messages == [("info", "Two columns would be named “K”: the new one is named “K (3)”")]
    other_day = pivot(act, numbered.filter(pl.col("O") != "K"), rows=["K"], column="O", value="V")
    assert other_day.columns == ["K", "K (2)", "x"] and other_day.rows() == [(1, 2, 3)]
    keys = pl.DataFrame({"A": [1] * 3, "A (2)": [1] * 3, "O": ["A", "A (2)", "A (3)"], "V": [1, 2, 3]})
    assert pivot(act, keys, rows=["A", "A (2)"], column="O", value="V").columns == ["A", "A (2)", "A (4)", "A (2) (2)", "A (3)"]
    empty_first = pl.DataFrame({"O": [None, "(empty)", "(empty) (2)"], "V": [1, 2, 3]})
    out = pivot(act, empty_first, column="O", value="V", order="first")  # (empty) isn't a value
    assert out.columns == ["(empty) (3)", "(empty)", "(empty) (2)"] and out.rows() == [(1, 2, 3)]


def test_names_that_differ_in_case_are_two_columns(act):
    assert names(act, ["Nord", "nord"], order="first") == ["Nord", "nord"]


def test_names_that_differ_only_in_spaces_at_the_ends_are_two_columns_with_a_message(act):
    ctx = StubContext()
    df = pl.DataFrame({"O": ["Köln", "Köln ", "Bonn", " Bonn", "Kiel", "Bonn  "], "V": [1, 2, 3, 4, 5, 6]})
    out = pivot(act, df, ctx, column="O", value="V", order="first")
    assert out.columns == ["Köln", "Köln ", "Bonn", " Bonn", "Kiel", "Bonn  "] and out.rows() == [(1, 2, 3, 4, 5, 6)]
    assert ctx.messages == [
        (
            "info",
            "The new columns “Köln” and “Köln ” differ only in spaces at the start or end (and 2 more pairs): "
            "Clean text before this step makes them one column",
        )
    ]
    ctx = StubContext()
    pivot(act, df.head(2), ctx, column="O", value="V")
    assert ctx.messages[0][1].startswith("The new columns “Köln” and “Köln ” differ only in spaces at the start or end: ")
    ctx = StubContext()
    pivot(act, pl.DataFrame({"O": ["a", "b"], "V": [1, 2]}), ctx, column="O", value="V")
    assert ctx.messages == []


def test_one_total_row_and_empty_row_keys(act):
    assert pivot(act, SALES, column="Month", value="Amount").rows() == [(16, 27, 3)]
    df = pl.DataFrame({"K": [None, "a", None], "L": [1, 1, 1], "O": ["x", "x", "y"], "V": [1, 2, 3]})
    out = pivot(act, df, rows=["K", "L", "K"], column="O", value="V")
    assert out.columns == ["K", "L", "x", "y"] and out.rows() == [(None, 1, 1, 3), ("a", 1, 2, None)]


def test_an_empty_table(act):
    ctx = StubContext()
    out = pivot(act, SALES.clear(), ctx, rows=["Region"], column="Month", value="Amount")
    assert out.schema == pl.Schema({"Region": pl.String}) and out.height == 0
    assert ctx.messages == [("info", "The table has no rows, so there are no new columns")]
    assert pivot(act, SALES.clear(), column="Month", value="Amount").shape == (0, 0)
    with pytest.raises(ActionError, match="Sum needs a number column"):
        pivot(act, SALES.clear(), column="Amount", value="Month")


def test_values_that_cant_become_names(act):
    df = pl.DataFrame({"O": [[1], [2]], "V": [1, 2]})
    with pytest.raises(ActionError) as raised:
        pivot(act, df, column="O", value="V")
    assert str(raised.value) == "The values of “O” (list(int64)) can't become column names"
    with pytest.raises(ActionError, match="can't become column names"):
        pivot(act, pl.DataFrame({"O": [b"\x01"], "V": [1]}), column="O", value="V")


def test_the_limits(act, monkeypatch):
    many = pl.DataFrame({"K": [i % 3 for i in range(1_001)], "O": list(range(1_001)), "V": [1] * 1_001})
    with pytest.raises(ActionError) as raised:
        pivot(act, many, rows=["K"], column="O", value="V")
    assert str(raised.value) == (
        "“O” has 1,001 distinct values: too many new columns (at most 1,000). Choose a column with fewer "
        "values, or group them first, e.g. dates by month with a formula."
    )
    assert pivot(act, many.filter(pl.col("O") > 0), rows=["K"], column="O", value="V").shape == (3, 1_001)
    monkeypatch.setattr(reshape, "_MAX_COLUMNS", 3)
    empties = pl.DataFrame({"O": ["a", None, "", " ", "b"], "V": [1] * 5})  # 5 distinct values, 3 new columns
    assert pivot(act, empties, column="O", value="V").columns == ["a", "b", "(empty)"]
    monkeypatch.setattr(reshape, "_MAX_COLUMNS", 2)
    with pytest.raises(ActionError, match="^“O” has 3 distinct values: too many new columns \\(at most 2\\)"):
        pivot(act, empties, column="O", value="V")
    monkeypatch.setattr(reshape, "_MAX_COLUMNS", 1_000)
    monkeypatch.setattr(reshape, "_MAX_CELLS", 10)
    six = pl.DataFrame({"K": [0, 1, 2, 0, 1, 2], "C": ["x", "y", "z", "x", "y", "z"], "O": [0, 1, 2, 3, 4, 5], "V": [1] * 6})
    assert pivot(act, six, rows=["K"], column="C", value="V").shape == (3, 4)  # 9 cells: 6 rows x 3 columns would be 18
    with pytest.raises(ActionError) as raised:
        pivot(act, six, rows=["O"], column="C", value="V")
    assert str(raised.value) == (
        "The result would have 6 rows and 3 new columns: 18 cells (at most 10). Choose fewer row keys, "
        "or a column with fewer distinct values."
    )
    assert pivot(act, six, column="O", value="V").shape == (1, 6)  # one total row: 6 cells


def test_unpivot_then_pivot_gives_the_table_back(act):
    df = pl.DataFrame(
        {"Customer": ["B", "A", "C"], "City": ["X", None, "Y"], "Mar": [1, None, 3], "Jan": [4, 5, None], "Feb": [7, 8, 9]}
    )
    long = act("reshape.unpivot", df, {"columns": ["Mar", "Jan", "Feb"], "name": "Month", "value": "Amount"})
    back = pivot(act, long, rows=["Customer", "City"], column="Month", value="Amount", function="first", order="first")
    assert back.equals(df)


def test_in_a_project(registry, tmp_path):
    (tmp_path / "sales.csv").write_text("Region;Month;Amount\nNord;Jan;5\nSüd;Feb;7\nNord;Feb;1\n", encoding="utf-8")
    project = Project(name="Test")
    project.path = tmp_path / "test.tafl"
    table = project.add_table("Sales")
    project.add_step(table, "csv.import", {"file": "sales.csv"})
    params = {"rows": ["Region"], "column": "Month", "value": "Amount", "order": "first"}
    step = project.add_step(table, "reshape.pivot", params)
    result = Engine(project, registry).run(mode="preview")[table.id]
    assert result.ok and result.df.rows() == [("Nord", 5, 1), ("Süd", None, 7)]
    assert Engine(project, registry).describe(step) == "Pivot Month into columns: Sum of Amount by Region"
    step.params["rows"] = ["Region", "Month"]
    result = Engine(project, registry).run(mode="preview")[table.id]
    assert result.steps[1].error == "Row keys: “Month” gives the column names, so it can't be a row key too"


def _best(run, rounds=3) -> float:
    times = []
    for _ in range(rounds):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return min(times)


def test_a_million_rows_stay_fast(act):
    """10,000 customers x 12 months. The step stays close to Polars' own pivot (the bound is generous:
    measured 32 ms against 22 ms)."""
    rows = 1_000_000
    df = pl.select(
        Customer=(pl.int_range(rows).hash(1) % 10_000).cast(pl.String).str.zfill(5),
        Month=(pl.int_range(rows).hash(2) % 12 + 1).cast(pl.String).str.zfill(2),
        Amount=(pl.int_range(rows).hash(3) % 1000).cast(pl.Int64),
    )
    plain = _best(lambda: df.pivot(on="Month", index="Customer", values="Amount", aggregate_function="sum"))
    params = {"rows": ["Customer"], "column": "Month", "value": "Amount"}
    step = _best(lambda: act("reshape.pivot", df, params))
    out = act("reshape.pivot", df, params)
    assert out.shape == (10_000, 13) and out.columns[1:] == [f"{m:02d}" for m in range(1, 13)]
    assert out.select(pl.sum_horizontal(pl.exclude("Customer")).sum()).item() == df["Amount"].sum()
    assert step < 3 * plain + 0.2, (step, plain)


def test_long_names_stay_fast(act):
    """1,000 keys x 999 date-times (names of 19 characters): Polars pivots slowly on long names,
    the step on the place of each name (measured 0.17 s against 0.16 s; 1.3 s on the names)."""
    rows = 1_000_000
    df = pl.select(
        K=pl.int_range(rows).hash(1) % 1_000,
        O=pl.datetime(2020, 1, 1) + pl.duration(minutes=pl.int_range(rows).hash(2) % 999),
        V=(pl.int_range(rows).hash(3) % 1000).cast(pl.Int64),
    )
    plain = _best(lambda: df.pivot(on="O", index="K", values="V", aggregate_function="sum"))
    params = {"rows": ["K"], "column": "O", "value": "V"}
    step = _best(lambda: act("reshape.pivot", df, params))
    out = act("reshape.pivot", df, params)
    assert out.shape == (1_000, 1_000) and out.columns[1:3] == ["2020-01-01 00:00:00", "2020-01-01 00:01:00"]
    assert out.select(pl.sum_horizontal(pl.exclude("K")).sum()).item() == df["V"].sum()
    assert step < 3 * plain + 0.2, (step, plain)

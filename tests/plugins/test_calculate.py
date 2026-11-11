import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.params import ValidationContext
from tafl.api.testing import StubContext


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


def test_formulas_on_exact_decimals_say_which_values_lost_digits(act):
    """A formula's result with decimal places is a decimal number (about 15 digits), as in 0.6.4:
    12345678901234.5678 and …5677 both became 12345678901234.568, without a warning. Each new
    column whose values lost digits says so; CAST(... AS VARCHAR), which the warning names, keeps
    all of them as text."""
    amounts = [D("12345678901234.5678"), D("12345678901234.5677"), D("1.5"), None]
    df = pl.DataFrame({"Amount": pl.Series(amounts, dtype=pl.Decimal(18, 4)), "Rate": pl.Series([D("1.1")] * 4, dtype=pl.Decimal(38, 18))})
    ctx = StubContext()
    columns = [
        {"name": "Net", "formula": "[Amount] + 0"},
        {"name": "Rated", "formula": "[Rate] * [Net]"},  # decimal numbers: nothing to say
        {"name": "Twice", "formula": "[Rate] * 2"},
        {"name": "Exact", "formula": "CAST([Amount] AS VARCHAR)"},
    ]
    out = act("calc.columns", df, {"columns": columns}, ctx)
    assert out.schema["Net"] == pl.Float64 and out["Net"].to_list() == [12345678901234.568, 12345678901234.568, 1.5, None]
    assert out["Exact"].to_list() == ["12345678901234.5678", "12345678901234.5677", "1.5000", None]
    assert ctx.warnings == [
        "[Net]: 2 value(s) were rounded, e.g. 12345678901234.5678 to 12345678901234.568: a formula's result with decimal "
        "places is a decimal number, which keeps about 15 digits (CAST(... AS VARCHAR) keeps them all, as text)"
    ]


def test_formulas_on_amounts_of_18_digits_are_calculated_in_38_when_they_need_it(act):
    """DuckDB multiplies exact decimals of up to 18 digits in 18 digits: [Amount] * 1.5 on a
    decimal(18, 4) amount of 70,000,000,000,000 stopped with "Overflow in multiplication of
    DECIMAL(18) … add an explicit cast", also after Fill empty values (0.6.4's Fill made the column a
    decimal(38, 4)), and so did the function list's Change in % on a change of 100 billion. Such a
    formula is calculated again with the amounts in 38 digits, as after a cast error."""
    df = pl.DataFrame({"Amount": pl.Series([D("70000000000000.0000"), None, D("1.5")], dtype=pl.Decimal(18, 4))})
    filled = act("rows.fill", df, {"columns": ["Amount"], "method": "value", "value": "0"})
    out = act("calc.columns", filled, {"columns": [{"name": "Gross", "formula": "[Amount] * 1.5"}]})
    assert out["Gross"].to_list() == [105000000000000.0, 0.0, 2.25]
    change = "100.0 * ([Amount] - LAG([Amount]) OVER (ORDER BY [Date])) / NULLIF(LAG([Amount]) OVER (ORDER BY [Date]), 0)"
    days = pl.DataFrame(
        {"Date": [dt.date(2025, 1, 1), dt.date(2025, 1, 2)], "Amount": pl.Series([D("120000000000.0000"), D("10.0000")], dtype=pl.Decimal(18, 4))}
    )
    changed = act("calc.columns", days, {"columns": [{"name": "Change", "formula": change}]})["Change"].to_list()
    assert changed[0] is None and changed[1] == pytest.approx(-99.99999999166667)


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


def test_number_the_groups_into_a_group_column(act):
    """The numbers replace that column, as with "Number the rows". Before, the step stopped with
    'ColumnNotFoundError: "Region" not found'."""
    df = pl.DataFrame({"Region": ["N", "S", "N", None], "Amount": [5, 1, 9, 7]})
    out = act("calc.enumerate", df, {"name": "Region", "mode": "groups", "group_by": ["Region"]})
    assert out.columns == ["Region", "Amount"] and out["Region"].to_list() == [1, 2, 1, 3]
    both = df.with_columns(Year=pl.Series([2025, 2025, 2026, 2025]))
    out = act("calc.enumerate", both, {"name": "Year", "mode": "groups", "group_by": ["Region", "Year"], "start": 10})
    assert out.rows() == [("N", 5, 10), ("S", 1, 11), ("N", 9, 12), (None, 7, 13)]
    ordered = act("calc.enumerate", df, {"name": "Region", "mode": "groups", "group_by": ["Region"], "order_by": [{"column": "Amount"}]})
    assert ordered["Region"].to_list() == [2, 1, 2, 3]  # S has the smallest amount, then N (5 and 9), then the empty region
    rows = act("calc.enumerate", df, {"name": "Region", "mode": "rows", "group_by": ["Region"]})
    assert rows.columns == ["Region", "Amount"] and rows["Region"].to_list() == [1, 1, 2, 1]
    with_no = df.select("Region", pl.lit(0).alias("No"), "Amount")  # another column of that name: replaced at the end, as before
    other = act("calc.enumerate", with_no, {"name": "No", "mode": "groups", "group_by": ["Region"]})
    assert other.columns == ["Region", "Amount", "No"] and other["No"].to_list() == [1, 2, 1, 3]


def test_join_texts_of_time_spans_and_binary_values(act):
    """They have no cast to text: a raw error of Polars stopped the step."""
    df = pl.DataFrame(
        {
            "G": ["a", "a", "b"],
            "Span": [dt.timedelta(hours=1), dt.timedelta(days=1, minutes=2), None],
            "Blob": [b"\xff\x00", b"abc", None],
        }
    )
    out = act(
        "calc.aggregate",
        df,
        {
            "group_by": ["G"],
            "calculations": [{"function": "join", "column": "Span", "name": "S"}, {"function": "join", "column": "Blob", "name": "B"}],
            "separator": "; ",
        },
    )
    assert out.to_dicts() == [{"G": "a", "S": "1h; 1d 2m", "B": "0xFF00; 0x616263"}, {"G": "b", "S": "", "B": ""}]
    text = df.with_columns(pl.Series("Blob", [b"abc", "ä".encode(), None]))  # bytes that are all text stay text
    out = act("calc.aggregate", text, {"group_by": [], "calculations": [{"function": "join", "column": "Blob"}], "separator": ", "})
    assert out.row(0) == ("abc, ä",)


@pytest.mark.parametrize("name", ["V", "*", "^a.*$"])
@pytest.mark.parametrize(
    ("values", "joined"),
    [
        pytest.param(pl.Series([2.0, 1e16, -0.0, 0.1 + 0.2, None]), "2|10000000000000000|0|0.30000000000000004", id="decimal numbers"),
        pytest.param(pl.Series([0.1, 2.0], dtype=pl.Float32), "0.1|2", id="Float32"),
        pytest.param(pl.Series([dt.time(8, 30, 0, 500000), dt.time(8, 30)]), "08:30:00.500|08:30:00", id="times"),
        pytest.param(
            pl.Series([dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 8, 30, 0, 250)]),
            "2025-12-31 08:30:00|2025-12-31 08:30:00.000250",
            id="date-times",
        ),
        pytest.param(
            pl.Series([dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 7, 1, 9, 30)]).dt.replace_time_zone("Europe/Berlin"),
            "2025-12-31 08:30:00|2025-07-01 09:30:00",
            id="time zone",
        ),
        pytest.param(pl.Series([dt.date(2025, 12, 31), None]), "2025-12-31", id="dates"),
        pytest.param(pl.Series([D("2.50"), D("-0.10")], dtype=pl.Decimal(10, 2)), "2.50|-0.10", id="exact decimals"),
        pytest.param(pl.Series([True, False]), "true|false", id="true/false"),
        pytest.param(pl.Series(["b", "a"], dtype=pl.Categorical), "b|a", id="categories"),
    ],
)
def test_join_texts_writes_the_values_the_same_on_every_pc(act, values, joined, name):
    """As Pivot and Split column write them: 2, not 2.0; a time keeps its fractions of a second
    (two times gave one text, 08:30:00); a date-time has them only when there are some, and a time
    zone's local time has no offset. Before, Polars' text: 2.0, 1e+16, -0.0, 08:30:00,
    2025-12-31 08:30:00.000000+01:00."""
    df = pl.DataFrame({"G": ["a"] * values.len(), name: values, "abc": [1] * values.len()})  # abc: what ^a.*$ would find
    params = {"group_by": ["G"], "calculations": [{"function": "join", "column": name, "name": "J"}], "separator": "|"}
    assert act("calc.aggregate", df, params).rows() == [("a", joined)]


@pytest.mark.parametrize(
    ("values", "label"),
    [([[1, 2], [3]], "list"), ([{"x": 1}, {"x": 2}], "record"), (pl.Series([object(), object()], dtype=pl.Object), "object")],
)
def test_join_texts_of_lists_and_records_stops_the_step(act, values, label):
    """Polars stopped with "cannot cast List type", and wrote records as {1}, without the names of
    their fields."""
    df = pl.DataFrame({"G": ["a", "b"], "Tags": values})
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["G"], "calculations": [{"function": "join", "column": "Tags"}]})
    assert str(raised.value) == f"“Tags” holds {label} values, which Join texts can't join"


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


def test_the_type_of_a_column_has_its_article(act):
    """Before: "O is a object column" (and "Sales is a empty column", which is no error now)."""
    df = pl.DataFrame({"O": pl.Series([object(), object()], dtype=pl.Object), "When": [dt.time(8), None]})
    with pytest.raises(ActionError, match="^Sum needs a number column; O is an object column$"):
        act("calc.aggregate", df, {"calculations": [{"function": "sum", "column": "O"}]})
    with pytest.raises(ActionError, match="^Average needs a number or date column; O is an object column$"):
        act("calc.aggregate", df, {"calculations": [{"function": "mean", "column": "O"}]})
    with pytest.raises(ActionError, match="^Sum needs a number column; When is a time column$"):
        act("calc.aggregate", df, {"calculations": [{"function": "sum", "column": "When"}]})


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


def test_aggregate_sums_beyond_the_digits_of_exact_decimals_stop_the_step(act):
    """Polars adds up exact decimals in their own 38 digits: beyond them it gave a wrong number in a
    group (-1.40E+38), and its own error for the total row, also when only a part of the sum was
    beyond."""
    big, minus = D("9" * 36 + ".25"), D("-" + "9" * 36 + ".25")  # 38 digits (- would round them to 28)
    back = D("-" + "9" * 36 + ".18")  # 0.07 - big
    df = pl.DataFrame({"K": ["a", "a", "b", "b"], "V": pl.Series([big, big, big, back], dtype=pl.Decimal(38, 2))})
    sums = {"calculations": [{"function": "sum", "column": "V"}, {"function": "count_rows"}]}
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["K"], **sums})
    assert str(raised.value) == (
        "“Sum of V” is 1,999,999,999,999,999,999,999,999,999,999,999,998.50 in a group: too many digits for an exact "
        "decimal (at most 38). Change “V” to a decimal number before this step."
    )
    with pytest.raises(ActionError, match=r"^“Total” is -1,999,.*,998\.50: too many digits for an exact decimal"):
        act("calc.aggregate", df.head(2).select(-pl.col("V")), {"calculations": [{"function": "sum", "column": "V", "name": "Total"}]})
    fits = act("calc.aggregate", df.filter(pl.col("K") == "b"), {"group_by": ["K"], **sums})
    assert fits.rows() == [("b", D("0.07"), 2)]
    assert fits.schema == pl.Schema({"K": pl.String, "Sum of V": pl.Decimal(38, 2), "Count": pl.Int64})
    on_the_way = act("calc.aggregate", df.with_columns(pl.col("V").shift(-1).fill_null(minus)), sums)  # big, big, back, minus
    assert on_the_way.rows() == [(D("0.07"), 4)] and on_the_way.schema["Sum of V"] == pl.Decimal(38, 2)
    small = act("calc.aggregate", pl.DataFrame({"V": pl.Series([D("1.5"), None], dtype=pl.Decimal(10, 1))}), sums)  # as before
    assert small.rows() == [(D("1.5"), 2)] and small.schema["Sum of V"] == pl.Decimal(38, 1)


def test_a_sum_of_only_empty_values_is_empty(act):
    """As in SQL and in Pivot, also for a table without rows. Before, Aggregate gave 0, and a column
    of the type "empty" stopped Sum with "Sales is a empty column"."""
    df = pl.DataFrame(
        {
            "G": ["a", "a", "b"],
            "Int": pl.Series([None, None, 5], dtype=pl.Int64),
            "Float": [None, None, 1.5],
            "Exact": pl.Series([None, None, D("2.50")], dtype=pl.Decimal(10, 2)),
            "Span": pl.Series([None, None, dt.timedelta(hours=1)], dtype=pl.Duration("us")),
            "Flag": pl.Series([None, None, True], dtype=pl.Boolean),
        }
    )
    columns = ["Int", "Float", "Exact", "Span", "Flag"]
    sums = {"calculations": [*({"function": "sum", "column": c, "name": c} for c in columns), {"function": "count", "column": "Int"}]}
    out = act("calc.aggregate", df, {"group_by": ["G"], **sums})
    assert out.rows() == [("a", None, None, None, None, None, 0), ("b", 5, 1.5, D("2.50"), dt.timedelta(hours=1), 1, 1)]
    assert act("calc.aggregate", df.head(2), sums).rows() == [(None, None, None, None, None, 0)]
    assert act("calc.aggregate", df.clear(), sums).rows() == [(None, None, None, None, None, 0)]  # no rows
    assert act("calc.aggregate", df.clear(), {"group_by": ["G"], **sums}).height == 0

    big = 2**62  # sums that are added up wider, then checked
    wide = pl.DataFrame({"G": ["a", "a", "b", "b"], "V": [big, -big + 5, None, None]})
    assert act("calc.aggregate", wide, {"group_by": ["G"], **sums_of("V")}).rows() == [("a", 5), ("b", None)]
    exact = pl.Decimal(38, 2)
    long = pl.Series([D("9" * 36 + ".25"), D("-" + "9" * 36 + ".18"), None, None], dtype=exact)
    out = act("calc.aggregate", wide.with_columns(V=long), {"group_by": ["G"], **sums_of("V")})
    assert out.rows() == [("a", D("0.07")), ("b", None)] and out.schema["V"] == exact

    empty = pl.DataFrame({"Sales": pl.Series([None, None], dtype=pl.Null)})
    calculations = [{"function": f, "column": "Sales", "name": f} for f in ("sum", "mean", "median")]
    out = act("calc.aggregate", empty, {"calculations": calculations})
    assert out.rows() == [(None, None, None)]
    assert out.schema == pl.Schema({"sum": pl.Int64, "mean": pl.Float64, "median": pl.Float64})


def sums_of(column: str) -> dict:
    return {"calculations": [{"function": "sum", "column": column, "name": column}]}


def test_aggregate_minimum_and_maximum_of_lists_stop_the_step(act):
    """Polars can't compare lists or records: it gave empty values (as Pivot does now)."""
    df = pl.DataFrame({"K": ["a", "a", "b"], "L": [[3, 1], [2], [5]], "S": [{"a": 1}, {"a": 2}, {"a": 3}]})
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["K"], "calculations": [{"function": "min", "column": "L"}]})
    assert str(raised.value) == "“L” holds list values, which Minimum can't compare"
    with pytest.raises(ActionError, match="^“S” holds record values, which Maximum can't compare$"):
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


def test_aggregate_sums_beyond_whole_numbers_of_128_bits_stop_the_step(act):
    """Only a plugin makes such columns. Before, the sums wrapped around without a message."""
    big = 2**127 - 1
    df = pl.DataFrame({"K": ["a", "a", "b", "b", "c"], "V": pl.Series([big, 5, big, -5, None], dtype=pl.Int128)})
    with pytest.raises(ActionError) as raised:
        act("calc.aggregate", df, {"group_by": ["K"], **sums_of("V")})
    assert str(raised.value) == (
        f"“V” is {big + 5:,} in a group: too big for a whole number (at most {big:,}). "
        "Change “V” to a decimal number before this step."
    )
    fits = act("calc.aggregate", df.filter(pl.col("K") != "a"), {"group_by": ["K"], **sums_of("V")})
    assert fits.rows() == [("b", big - 5), ("c", None)] and fits.schema["V"] == pl.Int128
    small = pl.DataFrame({"V": pl.Series([-big - 1, -5], dtype=pl.Int128)})
    with pytest.raises(ActionError, match=f"^“V” is {-big - 6:,}: too small for a whole number \\(at least {-big - 1:,}\\)"):
        act("calc.aggregate", small, sums_of("V"))
    unsigned = pl.DataFrame({"V": pl.Series([2**128 - 1, 2, None], dtype=pl.UInt128)})
    with pytest.raises(ActionError, match=f"^“V” is {2**128 + 1:,}: too big for a whole number \\(at most {2**128 - 1:,}\\)"):
        act("calc.aggregate", unsigned, sums_of("V"))
    out = act("calc.aggregate", unsigned.with_columns(V=pl.Series([2**128 - 3, 2, None], dtype=pl.UInt128)), sums_of("V"))
    assert out.rows() == [(2**128 - 1,)] and out.schema["V"] == pl.UInt128


def test_number_rows_in_text_order_like_sort(act):
    df = pl.DataFrame({"Name": ["Zebra", "Äpfel", "apfel", "Peter"]})
    out = act("calc.enumerate", df, {"order_by": [{"column": "Name"}]})
    assert out["No"].to_list() == [4, 1, 2, 3]

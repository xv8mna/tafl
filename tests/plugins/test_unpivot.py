"""Unpivot: columns become rows."""

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

WIDE = pl.DataFrame({"Customer": ["A", "B"], "Jan": [1, 4], "City": ["X", "Y"], "Feb": [2, None], "Mar": [3, 6]})


def unpivot(act, df, ctx=None, **params):
    return act("reshape.unpivot", df, params, ctx)


def test_the_settings(registry):
    action = registry.actions["reshape.unpivot"]
    assert (action.name, action.category, action.kind, action.version) == ("Unpivot", "Columns", "transform", 1)
    assert action.resolve({}) == {"mode": "these", "columns": [], "name": "Column", "value": "Value", "drop_empty": False}
    assert action.description.startswith("Turns columns into rows, the opposite of Pivot")
    help_text = " ".join(action.description.split())
    assert "whole numbers and exact decimals exact decimals, dates and date-times date-times," in help_text
    assert "Any other mix, also decimal numbers with exact decimals, becomes text" in help_text
    assert "At most 250,000,000 cells in the result (rows × columns)." in help_text
    assert action.check(action.resolve({}), ValidationContext()) == ["Columns: required"]


def test_the_summary(registry):
    action = registry.actions["reshape.unpivot"]
    summary = action.describe(Params(mode="these", columns=["Jan", "Feb", "Mar"], name="Month", value="Amount"))
    assert summary == "Unpivot Jan, Feb, Mar → Month, Amount"
    summary = action.describe(Params(mode="others", columns=["Customer"], name="Column", value="Value"))
    assert summary == "Unpivot all but Customer → Column, Value"
    project = Project(name="Test")
    step = project.add_step(project.add_table("Orders"), "reshape.unpivot", {})
    assert Engine(project, registry).describe(step) == "Unpivot ? → Column, Value"
    blank = project.add_step(project.tables[0], "reshape.unpivot", {"mode": "others", "columns": ["Customer"], "name": "  "})
    assert Engine(project, registry).describe(blank) == "Unpivot all but Customer → ?, Value"


def test_the_names_of_the_new_columns(registry):
    action = registry.actions["reshape.unpivot"]

    def problems(**values):
        return action.check(action.resolve({"columns": ["Jan"], **values}), ValidationContext())

    assert problems(name=" Month ", value="Month") == ["The two new columns need different names"]
    assert problems(name="  ", value=" ") == ["New column for the names: required", "New column for the values: required"]
    assert problems(name="", value="Value") == ["New column for the names: required"]
    assert problems(name="Month", value="month") == []


def test_rows_come_row_by_row(act):
    out = unpivot(act, WIDE, columns=["Jan", "Feb", "Mar"])
    assert out.columns == ["Customer", "City", "Column", "Value"]  # the other columns in table order, then the new ones
    assert out.rows() == [
        ("A", "X", "Jan", 1), ("A", "X", "Feb", 2), ("A", "X", "Mar", 3),
        ("B", "Y", "Jan", 4), ("B", "Y", "Feb", None), ("B", "Y", "Mar", 6),
    ]
    assert out.schema["Value"] == pl.Int64 and out.schema["Column"] == pl.String


def test_the_order_of_the_setting_and_all_columns_except_some(act):
    out = unpivot(act, WIDE, columns=["Mar", "Jan", "Mar"], name=" Month ", value="Amount")
    assert out.columns == ["Customer", "City", "Feb", "Month", "Amount"]
    assert out["Month"].to_list() == ["Mar", "Jan", "Mar", "Jan"]
    others = unpivot(act, WIDE, mode="others", columns=["City", "Customer"])
    assert others.columns == ["Customer", "City", "Column", "Value"]
    assert others["Column"].to_list() == ["Jan", "Feb", "Mar"] * 2  # in table order
    single = unpivot(act, WIDE, columns=["Feb"])
    assert single["Value"].to_list() == [2, None]


def test_the_errors(act):
    with pytest.raises(ActionError) as raised:
        unpivot(act, WIDE, columns=["Jan", "Feb"], name="City")
    assert str(raised.value) == "A column named “City” stays in the table: choose another name for the new column"
    same = unpivot(act, WIDE, columns=["Jan", "Feb"], name="Jan", value="Feb")  # the turned columns go away
    assert same.columns == ["Customer", "City", "Mar", "Jan", "Feb"] and same["Jan"].to_list() == ["Jan", "Feb"] * 2
    assert same["Feb"].to_list() == [1, 2, 4, None]
    with pytest.raises(ActionError) as raised:
        unpivot(act, WIDE, mode="others", columns=WIDE.columns)
    assert str(raised.value) == "All columns stay in the table: no column is left to turn into rows"
    nested = pl.DataFrame({"K": [1], "L": [[1, 2]], "N": [3]})
    with pytest.raises(ActionError) as raised:
        unpivot(act, nested, columns=["N", "L"])
    assert str(raised.value) == "“L” holds list(int64) values, which can't be turned into rows"


def values(act, ctx=None, **columns):
    df = pl.DataFrame(columns, strict=False)
    out = unpivot(act, df, ctx, columns=list(columns))
    return out["Value"]


@pytest.mark.parametrize(
    "column",
    [
        pl.Series([dt.date(2025, 1, 31), None]),
        pl.Series([D("1.25"), D("-3.50")], dtype=pl.Decimal(10, 2)),
        pl.Series([1, 2], dtype=pl.Int32),
        pl.Series([2**64 - 1, 1], dtype=pl.UInt64),
        pl.Series([True, None]),
        pl.Series([dt.datetime(2025, 1, 1, 8)]).dt.replace_time_zone("Europe/Berlin"),
    ],
)
def test_columns_of_one_type_keep_it(act, column):
    ctx = StubContext()
    out = values(act, ctx, a=column, b=column)
    assert out.dtype == column.dtype and out.to_list() == [v for pair in zip(column, column, strict=True) for v in pair]
    assert ctx.messages == []


def test_common_types_without_lost_digits(act):
    ctx = StubContext()
    whole = values(act, ctx, a=pl.Series([1], dtype=pl.Int32), b=[2**40], c=pl.Series([3], dtype=pl.UInt8))
    assert whole.dtype == pl.Int64 and whole.to_list() == [1, 2**40, 3]
    mixed = values(act, ctx, a=[1, 2**53], b=[1.5, None])
    assert mixed.dtype == pl.Float64 and mixed.to_list() == [1.0, 1.5, 2.0**53, None]
    tenths = pl.Series([D("1.5"), None], dtype=pl.Decimal(10, 1))
    exact = values(act, ctx, a=[10**12, 1], b=tenths, c=pl.Series([D("0.25"), D("1")], dtype=pl.Decimal(5, 2)))
    assert exact.dtype == pl.Decimal(38, 2)
    assert exact.to_list() == [D("1000000000000.00"), D("1.50"), D("0.25"), D("1.00"), None, D("1.00")]
    times = values(act, ctx, a=[dt.date(2025, 1, 31)], b=[dt.datetime(2025, 2, 1, 8, 30)])
    assert times.dtype == pl.Datetime("us")
    assert times.to_list() == [dt.datetime(2025, 1, 31), dt.datetime(2025, 2, 1, 8, 30)]
    zoned = pl.Series([dt.datetime(2025, 7, 1, 8)]).dt.replace_time_zone("Europe/Berlin")
    local = values(act, ctx, a=[dt.date(2025, 1, 31)], b=zoned)
    assert local.to_list()[0] == dt.datetime(2025, 1, 31, tzinfo=zoned[0].tzinfo)  # midnight there, not in UTC
    empty = values(act, ctx, a=[None, None], b=[1, 2])
    assert empty.dtype == pl.Int64 and empty.to_list() == [None, 1, None, 2]  # a column with nothing in it doesn't count
    assert ctx.messages == []


def test_date_times_and_durations_of_different_precision(act):
    """E.g. microseconds from a CSV file and nanoseconds from a Parquet file: date-times in the
    finest unit, or in a coarser one when a value is beyond the nanoseconds (after 2262)."""
    ctx = StubContext()
    micro = pl.Series([dt.datetime(2026, 1, 1, 8, 30), dt.datetime(2026, 1, 1, 8, 30, 0, 500_000)])
    nano = micro.cast(pl.Datetime("ns")).dt.offset_by("1ns")
    out = values(act, ctx, a=micro, b=nano, c=[dt.date(2026, 1, 2), None])
    assert out.dtype == pl.Datetime("ns") and out.dt.offset_by("-1ns")[1] == micro[0]
    assert out.cast(pl.Datetime("us")).to_list()[::3] == [micro[0], micro[1]]
    zoned = values(act, ctx, a=micro.dt.replace_time_zone("UTC"), b=nano.dt.replace_time_zone("UTC"))
    assert zoned.dtype == pl.Datetime("ns", "UTC")
    late = values(act, ctx, a=[dt.datetime(9999, 12, 31), dt.datetime(2026, 1, 1)], b=micro.cast(pl.Datetime("ns")))
    assert late.dtype == pl.Datetime("us") and late.to_list() == [dt.datetime(9999, 12, 31), micro[0], dt.datetime(2026, 1, 1), micro[1]]
    durations = values(act, ctx, a=pl.Series([dt.timedelta(seconds=1)], dtype=pl.Duration("ms")), b=[dt.timedelta(microseconds=5)])
    assert durations.dtype == pl.Duration("us") and durations.to_list() == [dt.timedelta(seconds=1), dt.timedelta(microseconds=5)]
    assert ctx.messages == []
    both = values(act, ctx, a=[dt.datetime(9999, 12, 31)], b=nano.head(1))  # neither unit holds both
    assert both.dtype == pl.String and both.to_list() == ["9999-12-31 00:00:00", "2026-01-01 08:30:00.000000001"]
    assert ctx.messages == [("info", "“Value” is text, so that no value of “a” changes")]


def test_a_coarser_unit_than_the_columns_have(act):
    """A date after 2262 next to nanosecond date-times: microseconds hold both, also when no
    column has them."""
    ctx = StubContext()
    nano = pl.Series([dt.datetime(2026, 1, 5, 8, 30), None]).cast(pl.Datetime("ns"))
    out = values(act, ctx, ValidTo=[dt.date(9999, 12, 31), dt.date(2026, 1, 1)], Changed=nano)
    assert out.dtype == pl.Datetime("us")
    assert out.to_list() == [dt.datetime(9999, 12, 31), dt.datetime(2026, 1, 5, 8, 30), dt.datetime(2026, 1, 1), None]
    long = pl.Series([dt.timedelta(days=365 * 300)], dtype=pl.Duration("ms"))
    durations = values(act, ctx, a=long, b=pl.Series([dt.timedelta(microseconds=5)], dtype=pl.Duration("ns")))
    assert durations.dtype == pl.Duration("us") and durations.to_list() == [dt.timedelta(days=365 * 300), dt.timedelta(microseconds=5)]
    assert ctx.messages == []


LOST = "“Value” is text, so that no value of “{}” changes"
MIXED = "“Value” is text: the columns have different types ({})"


@pytest.mark.parametrize(
    ("columns", "expected", "message"),
    [
        ({"a": [2**53 + 1], "b": [1.5]}, ["9007199254740993", "1.5"], LOST.format("a")),
        ({"a": [1], "b": pl.Series([2**63], dtype=pl.UInt64)}, ["1", "9223372036854775808"], LOST.format("b")),
        (
            {"a": [10**12], "b": pl.Series([D("0.1")], dtype=pl.Decimal(38, 30))},
            ["1000000000000", "0.100000000000000000000000000000"],
            LOST.format("a"),
        ),
        ({"a": [1], "b": ["x"]}, ["1", "x"], MIXED.format("integer: a; text: b")),
        ({"a": [True], "b": [1]}, ["true", "1"], MIXED.format("true/false: a; integer: b")),
        ({"a": [dt.date(2025, 12, 31)], "b": [7]}, ["2025-12-31", "7"], MIXED.format("date: a; integer: b")),
        (
            {"a": [2.0], "b": pl.Series([D("1.50")], dtype=pl.Decimal(5, 2))},
            ["2", "1.50"],
            MIXED.format("number: a; exact decimal: b"),
        ),
    ],
)
def test_other_mixes_become_text(act, columns, expected, message):
    ctx = StubContext()
    out = values(act, ctx, **columns)
    assert out.dtype == pl.String and out.to_list() == expected
    assert ctx.messages == [("info", message)]


def test_text_forms(act):
    ctx = StubContext()
    out = values(
        act,
        ctx,
        Text=["x", " y ", None],
        Float=[1.5, 2.0, -0.0],
        Datetime=[dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 8, 30, 0, 500_000), None],
        Time=[dt.time(8, 30), dt.time(8, 30, 0, 500_000), None],
        Duration=[dt.timedelta(days=1, hours=2, minutes=3), None, None],
        Binary=[b"\x01\xff", None, None],
    )
    assert out.to_list() == [
        "x", "1.5", "2025-12-31 08:30:00", "08:30:00", "1d 2h 3m", "0x01FF",
        " y ", "2", "2025-12-31 08:30:00.500", "08:30:00.500", None, None,
        None, "0", None, None, None, None,
    ]
    many = values(act, ctx, **{f"M{i}": [i] for i in range(1, 6)}, Note=["n"])
    assert many.to_list() == ["1", "2", "3", "4", "5", "n"]
    assert ctx.messages[-1] == ("info", MIXED.format("integer: M1, M2, M3 and 2 more; text: Note"))


def test_leaving_out_empty_values(act):
    df = pl.DataFrame({"K": ["a", "b"], "X": ["x", "  "], "Y": [None, "y"]})
    out = unpivot(act, df, columns=["X", "Y"], drop_empty=True)
    assert out.rows() == [("a", "X", "x"), ("b", "Y", "y")]
    numbers = unpivot(act, WIDE, columns=["Jan", "Feb", "Mar"], drop_empty=True)
    assert numbers["Value"].to_list() == [1, 2, 3, 4, 6]
    assert unpivot(act, WIDE, columns=["Jan", "Feb", "Mar"]).height == 6  # off: an empty cell gives a row too
    for dtype in (pl.Categorical, pl.Enum(["x", "y", "  "])):  # text of other types too (e.g. from a plugin)
        out = unpivot(act, df.with_columns(pl.col("X", "Y").cast(dtype)), columns=["X", "Y"], drop_empty=True)
        assert out.rows() == [("a", "X", "x"), ("b", "Y", "y")] and out.schema["Value"] == dtype


def test_the_rows_are_put_in_order_a_piece_at_a_time(act, monkeypatch):
    """Not with a sort of the whole result (it needs twice the memory): the same rows for any size
    of the pieces, also with empty values left out."""
    df = pl.DataFrame({"K": list("abcdefg"), "X": [1, None, 3, 4, None, 6, 7], "Y": [None, 2, 3, None, 5, 6, None], "Z": range(7)})
    expected = [(k, c, v) for k, *rest in df.rows() for c, v in zip("XYZ", rest, strict=True)]
    for piece in (1, 2, 3, 4, 5, 6, 20, 2**18):
        monkeypatch.setattr(reshape, "_PIECE", piece)
        out = unpivot(act, df, columns=["X", "Y", "Z"])
        assert out.rows() == expected, piece
        assert unpivot(act, df, columns=["X", "Y", "Z"], drop_empty=True).rows() == [r for r in expected if r[2] is not None]
        assert unpivot(act, df, columns=["Y"]).rows() == [(k, x, z, "Y", y) for k, x, y, z in df.rows()]
    assert unpivot(act, df.clear(), columns=["X", "Y"]).schema["Value"] == pl.Int64


def test_a_result_too_big(act, monkeypatch):
    monkeypatch.setattr(reshape, "_MAX_LONG_CELLS", 20)
    with pytest.raises(ActionError) as raised:
        unpivot(act, WIDE, columns=["Jan", "Feb", "Mar"])
    assert str(raised.value) == (
        "The result would have 6 rows and 4 columns: 24 cells (at most 20). Turn fewer columns into rows, "
        "or remove columns or rows before this step."
    )
    assert unpivot(act, WIDE, columns=["Jan", "Feb", "Mar"], drop_empty=True).height == 5  # 5 rows: 20 cells
    assert unpivot(act, WIDE.select("Jan", "Feb", "Mar"), columns=["Jan", "Feb", "Mar"]).shape == (6, 2)


def test_an_empty_table(act):
    ctx = StubContext()
    out = unpivot(act, WIDE.clear(), ctx, columns=["Jan", "Feb"])
    assert out.height == 0
    expected = {"Customer": pl.String, "City": pl.String, "Mar": pl.Int64, "Column": pl.String, "Value": pl.Int64}
    assert out.schema == pl.Schema(expected)
    mixed = unpivot(act, WIDE.clear(), columns=["Jan", "City"])
    assert mixed.schema["Value"] == pl.String
    assert unpivot(act, pl.DataFrame({"a": [None]}), columns=["a"]).schema["Value"] == pl.Null


def test_in_a_project(registry, tmp_path):
    (tmp_path / "wide.csv").write_text("Customer;Jan;Feb\nA;1;2\nB;3;\n", encoding="utf-8")
    project = Project(name="Test")
    project.path = tmp_path / "test.tafl"
    table = project.add_table("Wide")
    project.add_step(table, "csv.import", {"file": "wide.csv"})
    params = {"mode": "others", "columns": ["Customer"], "name": "Month", "value": "Amount", "drop_empty": True}
    project.add_step(table, "reshape.unpivot", params)
    result = Engine(project, registry).run(mode="preview")[table.id]
    assert result.ok and result.df.rows() == [("A", "Jan", 1), ("A", "Feb", 2), ("B", "Jan", 3)]
    table.steps[1].params["name"] = "Amount"
    result = Engine(project, registry).run(mode="preview")[table.id]
    assert result.steps[1].error == "The two new columns need different names"


def _best(run, rounds=3) -> float:
    times = []
    for _ in range(rounds):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return min(times)


def test_a_million_rows_stay_fast(act):
    """1M rows x 12 columns give 12M rows, row by row: close to Polars' own unpivot and a sort (the
    bound is generous: measured 0.15 s against 0.15 s)."""
    rows = 1_000_000
    months = [f"M{m:02d}" for m in range(1, 13)]
    df = pl.select(
        Id=pl.int_range(rows),
        Name=pl.int_range(rows).cast(pl.String),
        **{m: (pl.int_range(rows).hash(i) % 1000).cast(pl.Int64) for i, m in enumerate(months)},
    )

    def plain():
        out = df.with_row_index("row").unpivot(on=months, index=["row", "Id", "Name"])
        return out.sort("row", maintain_order=True).drop("row")

    baseline = _best(plain)
    step = _best(lambda: act("reshape.unpivot", df, {"columns": months}))
    out = act("reshape.unpivot", df, {"columns": months})
    assert out.shape == (12_000_000, 4) and out["Column"].head(13).to_list() == [*months, "M01"]
    assert out["Value"].head(12).to_list() == list(df.row(0)[2:])
    assert step < 3 * baseline + 0.2, (step, baseline)

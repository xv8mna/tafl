import datetime as dt
import subprocess
import sys
from decimal import Decimal as D
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.testing import StubContext

CUSTOMERS = pl.DataFrame(
    {
        "CustNo": ["1", "2", "2", "4"],
        "Name": ["Anna", "Ben", "Ben B.", "Dora"],
        "City": ["Berlin", "Hamburg", "Hamburg", "Köln"],
    }
)
ORDERS = pl.DataFrame({"Order": [10, 11, 12, 13], "Customer": [1, 2, 3, None], "City": ["X", "Y", "Z", "W"]})


@pytest.fixture
def ctx():
    return StubContext(tables={"cust": CUSTOMERS})


def merge(act, ctx, **options):
    params = {"other": "cust", "match": [{"column": "Customer", "other_column": "CustNo"}], **options}
    return act("table.merge", ORDERS, params, ctx)


def test_lookup_keeps_rows_and_takes_first_match(act, ctx):
    out = merge(act, ctx, columns=["Name"])
    assert out.columns == ["Order", "Customer", "City", "Name"]
    assert out["Name"].to_list() == ["Anna", "Ben", None, None]
    assert out.schema["Customer"] == pl.Int64  # compared as text, but the column keeps its type
    assert ctx.warnings == ["'Customer' (integer) and 'CustNo' (text) are compared as text"]


def test_left_join_uses_all_matches(act, ctx):
    assert merge(act, ctx, how="left", columns=["Name"])["Name"].to_list() == ["Anna", "Ben", "Ben B.", None, None]
    first_only = merge(act, ctx, how="left", columns=["Name"], multiple="first")
    assert first_only["Name"].to_list() == ["Anna", "Ben", None, None]


def test_lookup_always_uses_the_first_match(act, ctx):
    assert merge(act, ctx, how="lookup", columns=["Name"], multiple="all")["Name"].to_list() == ["Anna", "Ben", None, None]


def test_inner_and_full_join(act, ctx):
    assert merge(act, ctx, how="inner", columns=["Name"])["Order"].to_list() == [10, 11, 11]
    assert merge(act, ctx, how="inner", columns=["Name"], multiple="first")["Order"].to_list() == [10, 11]
    full = merge(act, ctx, how="full", columns=["Name"])
    assert full["Order"].to_list() == [10, 11, 11, 12, 13, None]
    assert full["Customer"].to_list() == [1, 2, 2, 3, None, 4]  # "4" of the other table fits
    assert full["Name"].to_list() == ["Anna", "Ben", "Ben B.", None, None, "Dora"]


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ({}, {"how": "lookup", "multiple": "first"}),
        ({"how": "lookup", "multiple": "all"}, {"how": "left", "multiple": "all"}),
        ({"how": "inner"}, {"how": "inner", "multiple": "first"}),  # version 1 default was "first"
        ({"how": "full", "multiple": "all"}, {"how": "full", "multiple": "all"}),
        ({"how": "matching"}, {"how": "matching", "multiple": "first"}),
    ],
)
def test_version_1_settings_are_upgraded(registry, old, new):
    action = registry.actions["table.merge"]
    assert action.version == 2
    assert action.upgrade({"other": "cust", **old}, 1) == {"other": "cust", **new}


def test_matching_and_non_matching_rows(act, ctx):
    assert merge(act, ctx, how="matching")["Order"].to_list() == [10, 11]
    assert merge(act, ctx, how="non_matching")["Order"].to_list() == [12, 13]


def test_existing_column_names_get_a_suffix(act, ctx):
    assert merge(act, ctx)["City (2)"].to_list() == ["Berlin", "Hamburg", None, None]


def test_a_second_merge_counts_the_suffix_up(act, ctx):
    once = merge(act, ctx, columns=["City"])
    twice = act("table.merge", once, {"other": "cust", "match": [{"column": "Customer", "other_column": "CustNo"}]}, ctx)
    assert twice.columns == ["Order", "Customer", "City", "City (2)", "Name", "City (3)"]
    custom = merge(act, ctx, columns=["City"], suffix="_x")
    again = act("table.merge", custom, {"other": "cust", "match": [{"column": "Customer", "other_column": "CustNo"}], "columns": ["City"], "suffix": "_x"}, ctx)
    assert again.columns[-1] == "City_x2"


@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full"])
def test_a_new_name_never_takes_the_name_of_a_key_column(act, how):
    """The other table's key column "City (2)" is still there while the columns are renamed (before,
    Polars stopped: "column 'City (2)' is duplicate"). So is the copy of a key compared as text."""
    other = pl.DataFrame({"City": ["Berlin"], "City (2)": ["B1"]})
    this = pl.DataFrame({"City": ["Bonn"], "Branch": ["B1"]})
    params = {"other": "other", "match": [{"column": "Branch", "other_column": "City (2)"}], "how": how}
    out = act("table.merge", this, params, StubContext(tables={"other": other}))
    assert out.columns == ["City", "Branch", "City (3)"] and out.rows() == [("Bonn", "B1", "Berlin")]
    other = pl.DataFrame({"K": [1], "__key0": ["x"]})
    this = pl.DataFrame({"K": ["1"], "__key0": ["y"]})
    params = {"other": "other", "match": [{"column": "K", "other_column": "K"}], "how": how, "suffix": "__"}
    out = act("table.merge", this, params, StubContext(tables={"other": other}))
    assert out.columns == ["K", "__key0", "__key0__2"] and out.rows() == [("1", "y", "x")]


THIS = pl.DataFrame({"Region": ["N", "S"], "Code": [1, 2], "Net": [10, 20]})
OTHER = pl.DataFrame({"Region": ["N", "W"], "Code": [1, 3], "Tax": [100, 300]})
BOTH_KEYS = [{"column": "Region", "other_column": "Region"}, {"column": "Code", "other_column": "Code"}]


@pytest.mark.parametrize("suffix", ["", " (2)", "_x", "   "])
def test_a_full_join_on_two_keys_with_any_suffix(act, suffix):
    """Polars stopped for an emptied suffix: "column with name 'Region' has more than one occurrence"."""
    params = {"other": "other", "match": BOTH_KEYS, "how": "full", "suffix": suffix}
    out = act("table.merge", THIS, params, StubContext(tables={"other": OTHER}))
    assert out.columns == ["Region", "Code", "Net", "Tax"]
    assert out.rows() == [("N", 1, 10, 100), ("S", 2, 20, None), ("W", 3, None, 300)]


@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full"])
def test_an_empty_suffix_still_gives_a_free_name(act, how):
    params = {"other": "other", "match": BOTH_KEYS, "how": how, "suffix": ""}
    other = OTHER.with_columns(Net=pl.Series([1, 3]))
    out = act("table.merge", THIS, params, StubContext(tables={"other": other}))
    assert out.columns == ["Region", "Code", "Net", "Tax", "Net2"]
    assert out.row(0) == ("N", 1, 10, 100, 1)


@pytest.mark.parametrize(
    ("column", "suffix", "table"),
    [
        ("Region (2)", " (2)", "this"),
        ("Code (2)", " (2)", "this"),
        ("Region_x", "_x", "this"),
        ("Region__other__", "", "this"),
        ("Region__other__", "", "other"),
        ("Region__other__", " (2)", "other"),
    ],
)
def test_a_full_join_next_to_a_column_named_like_a_key_with_the_suffix(act, column, suffix, table):
    """The same fault with a suffix that is not empty: the key column of the other table with the
    suffix was a name of one of the tables for a moment."""
    this, other =(THIS.rename({"Net": column}), OTHER) if table == "this" else (THIS, OTHER.rename({"Tax": column}))
    params = {"other": "other", "match": BOTH_KEYS, "how": "full", "suffix": suffix}
    out = act("table.merge", this, params, StubContext(tables={"other": other}))
    assert out.columns == ["Region", "Code", this.columns[2], other.columns[2]]
    assert out.rows() == [("N", 1, 10, 100), ("S", 2, 20, None), ("W", 3, None, 300)]


@pytest.mark.parametrize("suffix", ["", " (2)"])
def test_a_full_join_when_keys_of_the_other_table_have_names_of_this_table(act, suffix):
    this = pl.DataFrame({"K": ["N", "S"], "L": [1, 2], "Region": ["r", "s"], "Code": [7, 8]})
    match = [{"column": "K", "other_column": "Region"}, {"column": "L", "other_column": "Code"}]
    params = {"other": "other", "match": match, "how": "full", "suffix": suffix}
    out = act("table.merge", this, params, StubContext(tables={"other": OTHER}))
    assert out.columns == ["K", "L", "Region", "Code", "Tax"]
    assert out.rows() == [("N", 1, "r", 7, 100), ("S", 2, "s", 8, None), ("W", 3, None, None, 300)]


def test_a_full_join_on_keys_of_two_types_with_an_empty_suffix(act):
    this = THIS.with_columns(pl.col("Code").cast(pl.String))
    params = {"other": "other", "match": BOTH_KEYS, "how": "full", "suffix": ""}
    out = act("table.merge", this, params, StubContext(tables={"other": OTHER}))
    assert out.schema["Code"] == pl.String
    assert out.rows() == [("N", "1", 10, 100), ("S", "2", 20, None), ("W", "3", None, 300)]


def test_the_spare_suffix_gives_no_name_of_either_table():
    from tafl.plugins.combine import _spare_suffix

    assert _spare_suffix(["a", "b"], ["a"]) == "__other__"
    assert _spare_suffix(["a", "a__other__", "a__other___"], ["a"]) == "__other____"
    assert _spare_suffix(["a"], ["a", "a__other__"]) == "__other___"


def test_append(act):
    ctx = StubContext(tables={"b": pl.DataFrame({"x": ["3"], "y": [True]})})
    a = pl.DataFrame({"x": [1, 2]})
    out = act("table.append", a, {"tables": ["b"]}, ctx)
    assert out.to_dicts() == [{"x": "1", "y": None}, {"x": "2", "y": None}, {"x": "3", "y": True}]
    assert ctx.warnings == ["“x” is text: it has different types in the tables (integer, text)"]
    assert act("table.append", a, {"tables": ["b"], "columns": "this"}, ctx).columns == ["x"]


UTC = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("this", "other", "expected", "dtype"),
    [
        ([7], [1.5], [7.0, 1.5], pl.Float64),
        ([7], pl.Series([D("2.50")], dtype=pl.Decimal(38, 2)), [D("7.00"), D("2.50")], pl.Decimal(38, 2)),
        ([dt.date(2025, 1, 2)], [dt.datetime(2025, 1, 3, 8, 30)], [dt.datetime(2025, 1, 2), dt.datetime(2025, 1, 3, 8, 30)], pl.Datetime("us")),
        (
            [dt.date(2025, 1, 2)],
            pl.Series([dt.datetime(2025, 1, 3, 8, 30)], dtype=pl.Datetime("us", "UTC")),
            [dt.datetime(2025, 1, 2, tzinfo=UTC), dt.datetime(2025, 1, 3, 8, 30, tzinfo=UTC)],
            pl.Datetime("us", "UTC"),
        ),
        (pl.Series([dt.timedelta(seconds=1)], dtype=pl.Duration("ms")), [dt.timedelta(microseconds=5)], [dt.timedelta(seconds=1), dt.timedelta(microseconds=5)], pl.Duration("us")),
        ([7], pl.Series([None], dtype=pl.String), [7, None], pl.Int64),  # a column with only empty values doesn't count
        (pl.Series([None], dtype=pl.List(pl.String)), [dt.date(2025, 1, 2)], [None, dt.date(2025, 1, 2)], pl.Date),
        ([["a", "b"]], pl.Series([None], dtype=pl.Null), [["a", "b"], None], pl.List(pl.String)),
        (  # a date after 2262 beside nanoseconds with a time zone: microseconds hold both (it stopped)
            [dt.date(9999, 12, 31)],
            pl.Series([dt.datetime(2025, 3, 1, 12)], dtype=pl.Datetime("ns")).dt.replace_time_zone("UTC"),
            [dt.datetime(9999, 12, 31, tzinfo=UTC), dt.datetime(2025, 3, 1, 12, tzinfo=UTC)],
            pl.Datetime("us", "UTC"),
        ),
    ],
)
def test_append_keeps_a_type_that_holds_every_value(act, this, other, expected, dtype):
    """As Unpivot does. Before, Polars found a type of its own, or stopped with a technical error."""
    ctx = StubContext(tables={"o": pl.DataFrame({"x": other})})
    out = act("table.append", pl.DataFrame({"x": this}), {"tables": ["o"]}, ctx)
    assert out.schema["x"] == dtype and out["x"].to_list() == expected
    assert ctx.messages == []


@pytest.mark.parametrize(
    ("this", "other", "expected", "message"),
    [
        ([dt.date(2025, 1, 2)], [7], ["2025-01-02", "7"], "date, integer"),  # was 20090 and 7
        ([True], [7], ["true", "7"], "true/false, integer"),  # was 1 and 7
        ([dt.date(2025, 1, 2)], [True], ["2025-01-02", "true"], "date, true/false"),  # stopped: no supertype
        ([b"\x01\xff"], ["x"], ["0x01FF", "x"], "binary, text"),  # was binary, the text too
        ([dt.timedelta(days=1, hours=2)], ["x"], ["1d 2h", "x"], "duration, text"),  # stopped
        ([1.5], pl.Series([D("2.50")], dtype=pl.Decimal(38, 2)), ["1.5", "2.50"], "number, exact decimal"),  # was 1.5, 2.5
        ([dt.datetime(2025, 1, 2, 8, 30)], ["x"], ["2025-01-02 08:30:00", "x"], "date-time, text"),  # 08:30:00.000000
        ([2.0], [dt.time(8, 30, 0, 500_000)], ["2", "08:30:00.500"], "number, time"),  # was a number of nanoseconds
        (
            [dt.datetime(2025, 1, 2, 8, 30)],
            pl.Series([dt.datetime(2025, 1, 2, 8, 30)], dtype=pl.Datetime("us", "UTC")),
            ["2025-01-02 08:30:00", "2025-01-02 08:30:00"],
            "date-time, date-time (UTC)",
        ),
    ],
)
def test_append_makes_other_mixes_text_and_says_so(act, this, other, expected, message):
    ctx = StubContext(tables={"o": pl.DataFrame({"x": other})})
    out = act("table.append", pl.DataFrame({"x": this}), {"tables": ["o"]}, ctx)
    assert out.schema["x"] == pl.String and out["x"].to_list() == expected
    assert ctx.warnings == [f"“x” is text: it has different types in the tables ({message})"]


def test_append_says_which_columns_became_text(act):
    df = pl.DataFrame({"*": [1], "^a.*$": [dt.date(2025, 1, 2)], "abc": [2**53 + 1], "d": [True], "e": [1], "f": [1]})
    other = pl.DataFrame({"*": [0.5], "^a.*$": [3], "abc": [0.5], "d": ["x"], "e": ["x"], "f": ["x"]})
    ctx = StubContext(tables={"o": other, "p": other.select("f", "abc")})
    out = act("table.append", df, {"tables": ["o", "p"]}, ctx)
    assert out.rows() == [
        (1.0, "2025-01-02", "9007199254740993", "true", "1", "1"),
        (0.5, "3", "0.5", "x", "x", "x"),
        (None, None, "0.5", None, None, "x"),
    ]
    assert ctx.warnings == [
        "“^a.*$” is text: it has different types in the tables (date, integer)",
        "“abc” is text, so that none of its values changes (integer, number)",
        "“d” is text: it has different types in the tables (true/false, text)",
        "2 more columns are text, as their types differ between the tables: “e”, “f”",
    ]
    ctx = StubContext(tables={"o": other.drop("f")})  # one more: in the singular (it said "1 more columns are")
    act("table.append", df.drop("f"), {"tables": ["o"]}, ctx)
    assert ctx.warnings[3:] == ["1 more column is text, as its types differ between the tables: “e”"]


def test_append_keeps_the_digits_of_whole_numbers_beyond_int64(act):
    """As exact decimals, as whole numbers beyond 9,223,372,036,854,775,807 do elsewhere. Before,
    such values beside whole numbers of another type became 128-bit numbers (only a plugin knows them)."""
    ctx = StubContext(tables={"o": pl.DataFrame({"x": pl.Series([2**64 - 1], dtype=pl.UInt64)})})
    out = act("table.append", pl.DataFrame({"x": [-1]}), {"tables": ["o"]}, ctx)
    assert out.schema["x"] == pl.Decimal(38, 0) and out["x"].to_list() == [D(-1), D(2**64 - 1)] and ctx.messages == []
    small = act("table.append", pl.DataFrame({"x": [-1]}), {"tables": ["s"]}, StubContext(tables={"s": pl.DataFrame({"x": pl.Series([7], dtype=pl.Int32)})}))
    assert small.schema["x"] == pl.Int64 and small["x"].to_list() == [-1, 7]  # the common case
    ctx = StubContext(tables={"o": pl.DataFrame({"x": pl.Series([2**127 - 1], dtype=pl.Int128)})})  # 39 digits
    out = act("table.append", pl.DataFrame({"x": [-1]}), {"tables": ["o"]}, ctx)
    assert out["x"].to_list() == ["-1", str(2**127 - 1)]
    assert ctx.warnings == ["“x” is text, so that none of its values changes (integer)"]


def test_append_writes_binary_values_that_are_text_as_that_text(act):
    """As Clean text, Join texts and Merge do; one value that is no text makes all of them hex digits.
    Before, they were always hex digits (0x616263)."""
    ctx = StubContext(tables={"t": pl.DataFrame({"v": ["x"]})})
    out = act("table.append", pl.DataFrame({"v": [b"abc", "ä".encode(), None]}), {"tables": ["t"]}, ctx)
    assert out["v"].to_list() == ["abc", "ä", None, "x"]
    assert ctx.warnings == ["“v” is text: it has different types in the tables (binary, text)"]
    out = act("table.append", pl.DataFrame({"v": [b"abc", b"\xff\x00"]}), {"tables": ["t"]}, StubContext(tables={"t": pl.DataFrame({"v": ["x"]})}))
    assert out["v"].to_list() == ["0x616263", "0xFF00", "x"]
    tables = {"y": pl.DataFrame({"v": [b"y", b"\xff"]}), "t": pl.DataFrame({"v": ["t"]})}  # decided once for the column
    out = act("table.append", pl.DataFrame({"v": [b"x"]}), {"tables": ["y", "t"]}, StubContext(tables=tables))
    assert out["v"].to_list() == ["0x78", "0x79", "0xFF", "t"]  # it gave "x" beside "0x79"


def test_append_of_categories_beside_text_says_nothing(act):
    """Categories (Parquet's dictionary columns, a plugin's Enum) are text: their values stay. Before,
    the step warned "“Region” is text: it has different types in the tables (text)"."""
    cats = pl.DataFrame({"Region": pl.Series(["North", "South"], dtype=pl.Categorical), "Size": pl.Series(["S"], dtype=pl.Enum(["S"])).extend_constant(None, 1)})
    ctx = StubContext(tables={"t": pl.DataFrame({"Region": ["West"], "Size": pl.Series(["L"], dtype=pl.Enum(["L"]))})})
    out = act("table.append", cats, {"tables": ["t"]}, ctx)
    assert out.schema == pl.Schema({"Region": pl.String, "Size": pl.String})
    assert out.rows() == [("North", "S"), ("South", None), ("West", "L")] and ctx.messages == []
    ctx = StubContext(tables={"t": pl.DataFrame({"Region": [7]})})  # beside a number, text with a warning
    assert act("table.append", cats.select("Region"), {"tables": ["t"]}, ctx)["Region"].to_list() == ["North", "South", "7"]
    assert len(ctx.warnings) == 1 and ctx.warnings[0].startswith("“Region” is text: it has different types in the tables (")


@pytest.mark.parametrize("first", ["Order", "*"])
def test_append_only_the_columns_of_this_table_keeps_every_row(act, first):
    """Also the rows of a table that has none of this table's columns, with empty values, as in
    0.6.3. Before, they were left out without a message."""
    orders = pl.DataFrame({first: [1, 2], "Amount": [10.0, 20.0]})
    march = pl.DataFrame({"order": [3, 4, 5], "amount": [30.0, 40.0, 50.0]})  # the names in lower case
    ctx = StubContext(tables={"march": march, "april": march.head(1).with_columns(Amount=pl.lit(5.0))})
    out = act("table.append", orders, {"tables": ["march", "april"], "columns": "this"}, ctx)
    assert out.rows() == [(1, 10.0), (2, 20.0), (None, None), (None, None), (None, None), (None, 5.0)]
    assert out.schema == orders.schema and ctx.messages == []


def test_append_of_lists_beside_other_values_stops_with_a_message(act):
    ctx = StubContext(tables={"o": pl.DataFrame({"Tags": ["x"], "Note": [[1]]})})
    with pytest.raises(ActionError) as raised:
        act("table.append", pl.DataFrame({"Tags": [["a", "b"]]}), {"tables": ["o"]}, ctx)
    assert str(raised.value).startswith("“Tags” has different types in the tables (")
    assert str(raised.value).endswith(
        "lists and records can't be in one column with other values. Remove the column from one of the tables before this step."
    )
    out = act("table.append", pl.DataFrame({"Tags": ["a"]}), {"tables": ["o"], "columns": "this"}, ctx)
    assert out.rows() == [("a",), ("x",)]  # Note isn't in this table: its type doesn't matter


_APPEND_WIDE = """
import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
df = pl.DataFrame({f"M{i}": [i] for i in range(4001)})
other = pl.DataFrame({f"M{i}": [i + 0.5] if i % 2 else [str(i)] for i in range(4001)})
ctx = StubContext(tables={"o": other})
out = registry.actions["table.append"](df, {"tables": ["o"]}, ctx)
assert out.shape == (2, 4001) and out.schema["M1"] == pl.Float64 and out.schema["M2"] == pl.String, out.schema
assert out["M1"].to_list() == [1.0, 1.5] and out["M2"].to_list() == ["2", "2"]
assert len(ctx.warnings) == 4 and ctx.warnings[3].startswith("1,998 more columns are text"), ctx.warnings
"""


def test_append_of_4000_columns_of_other_types():
    """Each column is converted on its own. In a process of its own, so that a deep Polars
    expression that overflows the stack shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _APPEND_WIDE], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-1500:]}"


def test_derive(act):
    assert act("table.derive", None, {"table": "c"}, StubContext(tables={"c": CUSTOMERS})).equals(CUSTOMERS)


def test_keys_of_different_types_keep_their_type(act):
    numbers = pl.DataFrame({"Nr": [1.0, 2.0], "Status": ["open", "done"]})
    ctx = StubContext(tables={"other": numbers})
    df = pl.DataFrame({"Nr": [1, 2, 3]})
    out = act("table.merge", df, {"other": "other", "match": [{"column": "Nr", "other_column": "Nr"}], "columns": ["Status"]}, ctx)
    assert out.schema == {"Nr": pl.Int64, "Status": pl.String}
    assert out.to_dicts() == [{"Nr": 1, "Status": "open"}, {"Nr": 2, "Status": "done"}, {"Nr": 3, "Status": None}]
    assert not ctx.warnings
    matching = act("table.merge", df, {"other": "other", "match": [{"column": "Nr", "other_column": "Nr"}], "how": "matching"}, ctx)
    assert matching.schema == {"Nr": pl.Int64} and matching["Nr"].to_list() == [1, 2]


def test_full_join_takes_the_other_type_when_its_keys_dont_fit(act):
    ctx = StubContext(tables={"cust": pl.DataFrame({"CustNo": ["1", "04"], "Name": ["Anna", "Olga"]})})
    df = pl.DataFrame({"Customer": [1, 2]})
    params = {"other": "cust", "match": [{"column": "Customer", "other_column": "CustNo"}], "how": "full"}
    out = act("table.merge", df, params, ctx)
    assert out.to_dicts() == [
        {"Customer": "1", "Name": "Anna"},
        {"Customer": "2", "Name": None},
        {"Customer": "04", "Name": "Olga"},  # as a number it would be 4
    ]


def _ids(values, dtype):
    return pl.Series([None if v is None else D(v) for v in values], dtype=dtype)


ID_A, ID_B = "12345678901234567890123", "12345678901234567890124"  # equal as decimal numbers (Float64)


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        ("lookup", [(D(ID_A), "first"), (D(ID_B), "second"), (D("7"), None)]),
        ("left", [(D(ID_A), "first"), (D(ID_B), "second"), (D("7"), None)]),
        ("inner", [(D(ID_A), "first"), (D(ID_B), "second")]),
        ("full", [(D(ID_A), "first"), (D(ID_B), "second"), (D("7"), None), (D("5"), "five")]),
    ],
)
def test_exact_decimal_keys_of_different_precision_match_with_all_digits(act, how, expected):
    other = pl.DataFrame({"ID38": _ids([ID_B, ID_A, "5"], pl.Decimal(38, 0)), "Label": ["second", "first", "five"]})
    ctx = StubContext(tables={"other": other})
    df = pl.DataFrame({"ID": _ids([ID_A, ID_B, "7"], pl.Decimal(30, 0))})
    params = {"other": "other", "match": [{"column": "ID", "other_column": "ID38"}], "how": how, "multiple": "all"}
    out = act("table.merge", df, params, ctx)
    assert out.schema == {"ID": pl.Decimal(30, 0), "Label": pl.String}
    assert out.rows() == expected
    assert not ctx.warnings
    matching = act("table.merge", df, {**params, "how": "matching"}, ctx)
    assert matching["ID"].to_list() == [D(ID_A), D(ID_B)]
    assert act("table.merge", df, {**params, "how": "non_matching"}, ctx)["ID"].to_list() == [D("7")]


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        ("lookup", [(2**53, None), (2**53 + 1, "x"), (None, None)]),
        ("left", [(2**53, None), (2**53 + 1, "x"), (None, None)]),
        ("inner", [(2**53 + 1, "x")]),
    ],
)
def test_whole_numbers_and_exact_decimals_match_with_all_digits(act, how, expected):
    other = pl.DataFrame({"Nr": _ids([str(2**53 + 1), "9" * 20, None], pl.Decimal(20, 0)), "L": ["x", "y", "z"]})
    ctx = StubContext(tables={"other": other})
    df = pl.DataFrame({"Nr": pl.Series([2**53, 2**53 + 1, None], dtype=pl.Int64)})  # 2**53 + 1 as Float64 is 2**53
    out = act("table.merge", df, {"other": "other", "match": [{"column": "Nr", "other_column": "Nr"}], "how": how}, ctx)
    assert out.schema == {"Nr": pl.Int64, "L": pl.String}
    assert out.rows() == expected


@pytest.mark.parametrize(
    "dtype", [pl.Decimal(18, 0), pl.UInt64, pl.Int32], ids=["decimal(18,0)", "UInt64", "Int32"]
)
def test_other_whole_number_types_match_with_all_digits(act, dtype):
    values = [2**31 - 1, 2**31 - 2] if dtype == pl.Int32 else [2**53 + 1, 2**53]
    other = pl.DataFrame({"Nr": pl.Series(values).cast(dtype), "L": ["x", "y"]})
    ctx = StubContext(tables={"other": other})
    df = pl.DataFrame({"Nr": pl.Series([values[1], values[0], 7], dtype=pl.Int64)})
    params = {"other": "other", "match": [{"column": "Nr", "other_column": "Nr"}]}
    assert act("table.merge", df, params, ctx).rows() == [(values[1], "y"), (values[0], "x"), (7, None)]
    full = act("table.merge", pl.DataFrame({"Nr": [7]}), {**params, "how": "full"}, ctx)
    assert full.schema["Nr"] == pl.Int64 and full["Nr"].to_list() == [7, *values]


def test_full_join_keeps_long_keys_of_the_other_table(act):
    other = pl.DataFrame({"Nr": _ids([str(2**53 + 1), "9" * 20], pl.Decimal(20, 0)), "L": ["x", "y"]})
    ctx = StubContext(tables={"other": other})
    params = {"other": "other", "match": [{"column": "Nr", "other_column": "Nr"}], "how": "full"}
    out = act("table.merge", pl.DataFrame({"Nr": [1, 2**53]}), params, ctx)
    assert out.schema == {"Nr": pl.Decimal(38, 0), "L": pl.String}  # 9...9 doesn't fit Int64
    assert out.rows() == [(D(1), None), (D(2**53), None), (D(2**53 + 1), "x"), (D("9" * 20), "y")]
    fits = act("table.merge", pl.DataFrame({"Nr": [1]}), params, StubContext(tables={"other": other.head(1)}))
    assert fits.schema["Nr"] == pl.Int64 and fits.rows() == [(1, None), (2**53 + 1, "x")]
    unsigned = StubContext(tables={"other": pl.DataFrame({"Nr": pl.Series([2**64 - 1], dtype=pl.UInt64), "L": ["u"]})})
    assert act("table.merge", pl.DataFrame({"Nr": [1]}), params, unsigned).rows() == [(D(1), None), (D(2**64 - 1), "u")]


def test_exact_decimals_with_decimal_places_match_with_all_digits(act):
    amounts = ["1234567890.1234567891", "1234567890.1234567892", "2.5"]  # equal as Float64, except 2.5
    other = pl.DataFrame({"Amount": _ids(amounts, pl.Decimal(25, 12)), "L": ["one", "two", "half"]})
    ctx = StubContext(tables={"other": other})
    df = pl.DataFrame({"Amount": _ids([amounts[1], amounts[0], "2.50", "3"], pl.Decimal(20, 10))})
    params = {"other": "other", "match": [{"column": "Amount", "other_column": "Amount"}]}
    assert act("table.merge", df, params, ctx)["L"].to_list() == ["two", "one", "half", None]
    whole = act("table.merge", pl.DataFrame({"Amount": [2, 3]}), params, ctx)  # Int64 against decimals
    assert whole["L"].to_list() == [None, None]
    full = act("table.merge", pl.DataFrame({"Amount": [3]}), {**params, "how": "full"}, ctx)
    assert full.schema["Amount"] == pl.Decimal(38, 12)  # 2.5 doesn't fit whole numbers
    assert full["Amount"].to_list() == [D(3), D(amounts[0]), D(amounts[1]), D("2.5")]


def test_keys_too_long_for_one_column_match_nothing(act):
    other = pl.DataFrame({"ID": _ids(["9" * 38, "1"], pl.Decimal(38, 0)), "L": ["long", "one"]})
    ctx = StubContext(tables={"other": other})
    df = pl.DataFrame({"Amount": _ids(["1", "0.5"], pl.Decimal(38, 10))})  # holds 28 whole digits
    params = {"other": "other", "match": [{"column": "Amount", "other_column": "ID"}]}
    assert act("table.merge", df, {**params, "how": "left"}, ctx)["L"].to_list() == ["one", None]
    assert act("table.merge", df, {**params, "how": "inner"}, ctx)["L"].to_list() == ["one"]
    with pytest.raises(ActionError, match=r"^Full join: the keys 'Amount' \(number\) and 'ID' \(number\) don't fit in one"):
        act("table.merge", df, {**params, "how": "full"}, ctx)
    swapped = StubContext(tables={"other": df.rename({"Amount": "ID"}).with_columns(L=pl.lit("x"))})
    long = pl.DataFrame({"Amount": _ids(["9" * 38, "1"], pl.Decimal(38, 0))})
    assert act("table.merge", long, {**params, "how": "left"}, swapped)["L"].to_list() == [None, "x"]
    with pytest.raises(ActionError, match="don't fit in one column"):  # 0.5 needs a decimal place, 9...9 all 38 digits
        act("table.merge", long, {**params, "how": "full"}, swapped)


def test_decimal_numbers_are_compared_as_decimal_numbers(act):
    other = pl.DataFrame({"Nr": [1.5, 2.0], "L": ["x", "y"]})
    df = pl.DataFrame({"Nr": _ids(["1.50", "2", "3"], pl.Decimal(10, 2))})
    params = {"other": "other", "match": [{"column": "Nr", "other_column": "Nr"}]}
    out = act("table.merge", df, params, StubContext(tables={"other": other}))
    assert out.schema["Nr"] == pl.Decimal(10, 2) and out["L"].to_list() == ["x", "y", None]


@pytest.mark.parametrize(
    ("dtype", "keys", "big"),
    [(pl.Decimal(10, 2), ["1.50", "2.00"], 1e9), (pl.Decimal(20, 3), ["1.5", "2"], 2.0**63), (pl.Decimal(10, 2), ["1.5", "2"], 99999999.996)],
)
def test_full_join_with_decimal_number_keys_too_big_for_the_exact_decimals(act, dtype, keys, big):
    """Rows only in the other table keep their key: 1e9 doesn't fit a Decimal(10, 2), so the keys
    become decimal numbers (Float64). Before, Polars stopped with "can't fit values with 12
    digits"."""
    other = pl.DataFrame({"K2": [1.5, big], "R": ["x", "y"]})
    params = {"other": "other", "match": [{"column": "K", "other_column": "K2"}], "how": "full", "multiple": "all"}
    out = act("table.merge", pl.DataFrame({"K": _ids(keys, dtype), "L": ["a", "b"]}), params, StubContext(tables={"other": other}))
    assert out.schema == {"K": pl.Float64, "L": pl.String, "R": pl.String}
    assert out.rows() == [(1.5, "a", "x"), (2.0, "b", None), (big, None, "y")]
    fits = StubContext(tables={"other": other.head(1).vstack(pl.DataFrame({"K2": [7.25], "R": ["z"]}))})
    out = act("table.merge", pl.DataFrame({"K": _ids(keys, dtype)}), params, fits)  # 7.25 fits: the type stays
    assert out.schema == {"K": dtype, "R": pl.String} and out["K"].to_list() == [D("1.5"), D("2"), D("7.25")]


BERLIN = pl.Datetime("us", "Europe/Berlin")


@pytest.mark.parametrize(
    ("dtype", "this", "texts", "values"),
    [
        (pl.Date, dt.date(2024, 1, 31), ["2024-01-31", "2024-02-01"], [dt.date(2024, 1, 31), dt.date(2024, 2, 1)]),
        (
            pl.Datetime("us"),
            dt.datetime(2024, 1, 31, 8),
            ["2024-01-31 08:00:00", "2024-02-01 09:30:00.250"],  # the same text on every PC
            [dt.datetime(2024, 1, 31, 8), dt.datetime(2024, 2, 1, 9, 30, 0, 250000)],
        ),
        (
            BERLIN,
            dt.datetime(2024, 1, 31, 8, tzinfo=ZoneInfo("Europe/Berlin")),
            ["2024-01-31 08:00:00", "2024-07-01 09:30:00"],  # the local time, without the offset
            [dt.datetime(2024, 1, 31, 8, tzinfo=ZoneInfo("Europe/Berlin")), dt.datetime(2024, 7, 1, 9, 30, tzinfo=ZoneInfo("Europe/Berlin"))],
        ),
        (pl.Time, dt.time(8), ["08:00:00", "09:30:00"], [dt.time(8), dt.time(9, 30)]),
        (
            pl.Time,
            dt.time(8, 0, 0, 500000),
            ["08:00:00.500", "09:30:00.000250"],
            [dt.time(8, 0, 0, 500000), dt.time(9, 30, 0, 250)],
        ),
        (pl.Boolean, True, ["true", "false"], [True, False]),
    ],
)
def test_full_join_of_dates_times_and_true_false_with_text_keys(act, dtype, this, texts, values):
    """Compared as text like in a left join. The rows only in the other table get their key in
    this table's type when it has one; else the key column becomes text. Before, Polars stopped:
    "casting from string to date is not supported"."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": "full"}
    df = pl.DataFrame({"A": pl.Series([this], dtype=dtype)})
    ctx = StubContext(tables={"other": pl.DataFrame({"K": texts, "N": ["x", "y"]})})
    out = act("table.merge", df, params, ctx)
    assert out.schema == {"A": dtype, "N": pl.String} and out.rows() == [(values[0], "x"), (values[1], "y")]
    word = {pl.Date: "date", pl.Time: "time", pl.Boolean: "true/false"}.get(dtype, "date-time")
    assert ctx.warnings == [f"'A' ({word}) and 'K' (text) are compared as text"]
    assert ("info", "The row matched; 1 row of the other table without a match was added") in ctx.messages
    other = StubContext(tables={"other": pl.DataFrame({"K": [texts[0], "x"], "N": ["x", "y"]})})
    out = act("table.merge", df, params, other)  # "x" is no value of this type: the keys become text
    assert out.schema == {"A": pl.String, "N": pl.String} and out.rows() == [(texts[0], "x"), ("x", "y")]


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        ("lookup", [(b"abc", None), (b"\x00\xff", "y"), (None, None)]),
        ("left", [(b"abc", None), (b"\x00\xff", "y"), (None, None)]),
        ("inner", [(b"\x00\xff", "y")]),
        ("full", [("0x616263", None), ("0x00FF", "y"), (None, None), ("abc", "x"), ("zz", "z")]),  # "abc" is no hex
        ("matching", [(b"\x00\xff",)]),
        ("non_matching", [(b"abc",), (None,)]),
    ],
)
def test_binary_keys_with_bytes_that_are_no_text_are_compared_with_text_as_hex_digits(act, how, expected):
    """As Join texts writes such a column (0x00FF). Before, Polars stopped with "invalid utf8"."""
    params = {"other": "other", "match": [{"column": "V", "other_column": "K"}], "how": how}
    ctx = StubContext(tables={"other": pl.DataFrame({"K": ["abc", "0x00FF", "zz"], "N": ["x", "y", "z"]})})
    assert act("table.merge", pl.DataFrame({"V": [b"abc", b"\x00\xff", None]}), params, ctx).rows() == expected
    assert ctx.warnings == ["'V' (binary) and 'K' (text) are compared as text (the binary values as hex digits, 0xFF00)"]
    swapped = {**params, "match": [{"column": "K", "other_column": "V"}], "how": "lookup"}
    other = StubContext(tables={"other": pl.DataFrame({"V": [b"abc", b"\x00\xff"], "N": ["a", "b"]})})
    assert act("table.merge", ctx.tables["other"], swapped, other)["N (2)"].to_list() == [None, "b", None]
    text = StubContext(tables={"other": pl.DataFrame({"V": [b"abc", "ä".encode()], "N": ["a", "b"]})})  # all text: as text
    assert act("table.merge", ctx.tables["other"], swapped, text)["N (2)"].to_list() == ["a", None, None]


@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full", "matching", "non_matching"])
def test_keys_that_cant_be_compared_stop_with_a_message(act, how):
    """Polars can't cast a duration to text, or a time to a date-time: a message instead of its error."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    ctx = StubContext(tables={"other": pl.DataFrame({"K": ["1 day"], "T": [dt.date(2024, 1, 31)]})})
    durations = pl.DataFrame({"A": [dt.timedelta(days=1)]})
    message = r"can't be compared: choose other key columns, or first make one of them text with a formula such as CAST\(\[A\] AS VARCHAR\)$"
    with pytest.raises(ActionError, match=r"^'A' \(duration\) and 'K' \(text\) " + message):  # in plain words
        act("table.merge", durations, params, ctx)
    assert ctx.warnings == []  # not "compared as text"
    times = pl.DataFrame({"A": [dt.time(8)]})
    with pytest.raises(ActionError, match=r"^'A' \(time\) and 'T' \(date\) can't be compared"):
        act("table.merge", times, {**params, "match": [{"column": "A", "other_column": "T"}]}, ctx)


@pytest.mark.parametrize("how", ["lookup", "full"])
def test_times_are_compared_with_text_with_their_fractions_of_a_second(act, how):
    """Polars' cast to text leaves out the fractions: 08:00:00.5 would match "08:00:00", and a full
    join that makes the key column text would drop them."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    this = pl.DataFrame({"A": [dt.time(8, 0, 0, 500000), dt.time(9)]})
    ctx = StubContext(tables={"other": pl.DataFrame({"K": ["08:00:00", "09:00:00", "x"], "N": ["a", "b", "c"]})})
    out = act("table.merge", this, params, ctx)
    if how == "lookup":
        assert out.rows() == [(dt.time(8, 0, 0, 500000), None), (dt.time(9), "b")]
    else:  # "x" is no time: the key column becomes text, with the fractions
        assert out.schema["A"] == pl.String
        assert out.rows() == [("08:00:00.500", None), ("09:00:00", "b"), ("08:00:00", "a"), ("x", "c")]
    exact = StubContext(tables={"other": pl.DataFrame({"K": ["08:00:00.500"], "N": ["a"]})})
    assert act("table.merge", this, params, exact).rows()[0] == (dt.time(8, 0, 0, 500000), "a")


DECIMALS = [17.0, 2.5, 1e16, -0.0, None]
AS_TEXT = ["17", "2.5", "10000000000000000", "0"]  # the same on every PC, as Pivot and Split column write them


@pytest.mark.parametrize("name", ["A", "*", "^a.*$"])
@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full", "matching", "non_matching"])
def test_decimal_number_keys_find_the_same_text_on_every_pc(act, how, name):
    """17.0 finds the text "17", 1e16 "10000000000000000" and -0.0 "0", as Pivot and Split column
    write them. Before, Polars' text was compared: "17.0", "1e+16" and "-0.0"."""
    params = {"other": "other", "match": [{"column": name, "other_column": "K"}], "how": how}
    this = pl.DataFrame({name: DECIMALS, "abc": [1, 2, 3, 4, 5]})  # abc: the column ^a.*$ would find
    other = pl.DataFrame({"K": AS_TEXT, "N": ["a", "b", "c", "d"]})
    ctx = StubContext(tables={"other": other})
    out = act("table.merge", this, params, ctx)
    assert ctx.warnings == [f"'{name}' (number) and 'K' (text) are compared as text"]
    expected = {
        "lookup": [(17.0, 1, "a"), (2.5, 2, "b"), (1e16, 3, "c"), (-0.0, 4, "d"), (None, 5, None)],
        "inner": [(17.0, 1, "a"), (2.5, 2, "b"), (1e16, 3, "c"), (-0.0, 4, "d")],
        "matching": [(17.0, 1), (2.5, 2), (1e16, 3), (-0.0, 4)],
        "non_matching": [(None, 5)],
    }
    expected["left"] = expected["full"] = expected["lookup"]  # every text matches: the key column stays as it is
    assert out.rows() == expected[how] and out.schema[name] == pl.Float64
    swapped = {"other": "other", "match": [{"column": "K", "other_column": name}]}  # this table's keys are the texts
    back = act("table.merge", other, swapped, StubContext(tables={"other": this.with_columns(L=pl.lit("x"))}))
    assert back["L"].to_list() == ["x"] * 4


def test_a_full_join_keeps_decimal_number_keys_only_for_texts_written_like_them(act):
    """A text of the other table without a match becomes a number only when it is that number's
    text: "17.0" and "1e16" stay text, and so does the key column (as "04" for whole numbers)."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": "full"}
    this = pl.DataFrame({"A": DECIMALS})
    fits = StubContext(tables={"other": pl.DataFrame({"K": ["17", "3.25", "20000000000000000"], "N": ["a", "b", "c"]})})
    out = act("table.merge", this, params, fits)
    assert out.schema["A"] == pl.Float64
    assert out.rows() == [(17.0, "a"), (2.5, None), (1e16, None), (-0.0, None), (None, None), (3.25, "b"), (2e16, "c")]
    for text in ("17.0", "1e16", "-0"):
        other = StubContext(tables={"other": pl.DataFrame({"K": [text], "N": ["a"]})})
        out = act("table.merge", this, params, other)
        assert out.schema["A"] == pl.String
        assert out.rows() == [*((shown, None) for shown in AS_TEXT), (None, None), (text, "a")]


@pytest.mark.parametrize(
    ("values", "found", "not_found"),
    [
        (pl.Series([dt.datetime(2025, 12, 31, 8, 30)], dtype=pl.Datetime("ns")), ["2025-12-31 08:30:00"], ["2025-12-31 08:30:00.000000000"]),
        (pl.Series([dt.datetime(2025, 12, 31, 8, 30, 0, 250000)], dtype=pl.Datetime("ms")), ["2025-12-31 08:30:00.250"], ["2025-12-31 08:30:00.25"]),
        (pl.Series([dt.datetime(2025, 12, 31, 8, 30, 0, 500)]), ["2025-12-31 08:30:00.000500"], ["2025-12-31 08:30:00"]),
        (pl.Series([dt.datetime(2025, 12, 31, 8, 30)]).dt.replace_time_zone("UTC"), ["2025-12-31 08:30:00"], ["2025-12-31 08:30:00.000000+00:00"]),
        (pl.Series([0.1, 17.0], dtype=pl.Float32), ["0.1", "17"], ["17.0", "0.10000000149011612"]),
    ],
    ids=["nanoseconds", "milliseconds", "microseconds", "UTC", "Float32"],
)
def test_date_time_keys_find_the_same_text_on_every_pc(act, values, found, not_found):
    """As Pivot and Split column write them: without fractions of a second when there are none,
    and a time zone's local time without the offset. Before, Polars' text with all six digits (or
    three, or nine) was compared, and the offset: 2025-12-31 08:30:00.000000+01:00."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}]}
    other = pl.DataFrame({"K": [*not_found, *found], "N": ["not"] * len(not_found) + [f"found {i}" for i in range(len(found))]})
    out = act("table.merge", values.alias("A").to_frame(), params, StubContext(tables={"other": other}))
    assert out.schema["A"] == values.dtype and out["N"].to_list() == [f"found {i}" for i in range(len(found))]


def test_a_full_join_reads_the_local_time_of_a_time_zone(act):
    """The rows only in the other table get a date-time with the column's time zone when their text
    is a local time there. The clocks turned back (02:30 twice) or forward (no 02:30) on those
    days: such a text is no single moment, and the key column becomes text."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": "full"}
    this = pl.DataFrame({"A": [dt.datetime(2025, 7, 1, 9, 30, tzinfo=ZoneInfo("Europe/Berlin"))]})
    fits = StubContext(tables={"other": pl.DataFrame({"K": ["2025-07-01 09:30:00", "2025-12-01 10:00:00.500"], "N": ["a", "b"]})})
    out = act("table.merge", this, params, fits)
    assert out.schema["A"] == BERLIN
    assert out.rows() == [
        (dt.datetime(2025, 7, 1, 9, 30, tzinfo=ZoneInfo("Europe/Berlin")), "a"),
        (dt.datetime(2025, 12, 1, 10, 0, 0, 500000, tzinfo=ZoneInfo("Europe/Berlin")), "b"),
    ]
    for text in ("2025-10-26 02:30:00", "2025-03-30 02:30:00"):
        other = StubContext(tables={"other": pl.DataFrame({"K": [text], "N": ["a"]})})
        out = act("table.merge", this, params, other)
        assert out.schema["A"] == pl.String and out.rows() == [("2025-07-01 09:30:00", None), (text, "a")]


@pytest.mark.parametrize("how", ["lookup", "full", "matching"])
@pytest.mark.parametrize(
    ("this", "other", "message"),
    [
        ([[1, 2]], ["[1, 2]"], r"^'A' holds list values, which can't be compared with 'K': choose other key columns$"),
        ([{"x": 1}], [{"y": 1}], r"^'A' holds record values, which can't be compared with 'K'"),  # Polars' text: {1}, {1}
        ([1], [[1]], r"^'K' holds list values, which can't be compared with 'A'"),
        ([[1]], [["1"]], r"^'A' holds list values, which can't be compared with 'K'"),
        ([None], [[1]], r"^'K' holds list values, which can't be compared with 'A'"),
    ],
    ids=["list and text", "records of other fields", "number and list", "lists of other types", "empty and list"],
)
def test_lists_and_records_match_only_keys_of_their_own_type(act, how, this, other, message):
    """They have no text to compare with: a list stopped with Polars' message, and a record was
    compared as Polars' text, without the names of its fields. Lists and records of one type match
    value by value, as before."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    ctx = StubContext(tables={"other": pl.DataFrame({"K": other, "N": ["x"]})})
    with pytest.raises(ActionError, match=message):
        act("table.merge", pl.DataFrame({"A": this}), params, ctx)
    assert ctx.warnings == []
    same = StubContext(tables={"other": pl.DataFrame({"K": [[1, 2], [3]], "N": ["x", "y"]})})
    out = act("table.merge", pl.DataFrame({"A": [[3], [4]]}), params, same)
    assert out.rows() == {"lookup": [([3], "y"), ([4], None)], "full": [([3], "y"), ([4], None), ([1, 2], "x")], "matching": [([3],)]}[how]


@pytest.mark.parametrize("how", ["lookup", "full"])
def test_objects_of_a_plugin_cant_be_keys(act, how):
    """Polars can't compare them: "not yet implemented: Hash Left Join between object and object"."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    objects = pl.Series([object()], dtype=pl.Object)
    for other in (pl.DataFrame({"K": objects}), pl.DataFrame({"K": ["x"]})):
        with pytest.raises(ActionError, match=r"^'A' holds object values, which can't be compared: choose other key columns$"):
            act("table.merge", pl.DataFrame({"A": objects}), params, StubContext(tables={"other": other}))
    with pytest.raises(ActionError, match=r"^'K' holds object values"):
        act("table.merge", pl.DataFrame({"A": ["x"]}), params, StubContext(tables={"other": pl.DataFrame({"K": objects})}))


@pytest.mark.parametrize("how", ["lookup", "full"])
def test_categories_and_bytes_that_are_text_are_compared_as_their_text(act, how):
    """As before: a category is its text, and bytes that are all text are that text. The type of
    categories is called text, so the step doesn't warn about them (before: "'A' (Categorical) and
    'K' (String) are compared as text")."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    other = StubContext(tables={"other": pl.DataFrame({"K": ["b", "c"], "N": ["x", "y"]})})
    for dtype in (pl.Categorical, pl.Enum(["a", "b", "c"])):
        categories = pl.DataFrame({"A": pl.Series(["a", "b"], dtype=dtype)})
        out = act("table.merge", categories, params, other)
        assert out.schema["A"] == dtype and out.rows() == [("a", None), ("b", "x"), *([("c", "y")] if how == "full" else [])]
    assert other.warnings == []
    out = act("table.merge", pl.DataFrame({"A": [b"a", b"b"]}), params, other)
    assert out.schema["A"] == pl.Binary and out.rows() == [(b"a", None), (b"b", "x"), *([(b"c", "y")] if how == "full" else [])]
    assert other.warnings == ["'A' (binary) and 'K' (text) are compared as text"]


@pytest.mark.parametrize("how", ["lookup", "full"])
def test_time_spans_of_other_units_are_compared(act, how):
    """A duration in milliseconds and one in microseconds: compared in the finer unit."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    this = pl.DataFrame({"A": pl.Series([dt.timedelta(seconds=1), dt.timedelta(minutes=2)], dtype=pl.Duration("ms"))})
    other = pl.DataFrame({"K": pl.Series([dt.timedelta(minutes=2), dt.timedelta(microseconds=1500)], dtype=pl.Duration("us")), "N": ["a", "b"]})
    ctx = StubContext(tables={"other": other})
    out = act("table.merge", this, params, ctx)
    assert ctx.warnings == []
    if how == "lookup":
        assert out.schema["A"] == pl.Duration("ms") and out.rows() == [(dt.timedelta(seconds=1), None), (dt.timedelta(minutes=2), "a")]
    else:  # 1.5 ms doesn't fit milliseconds: the key column gets microseconds
        assert out.schema["A"] == pl.Duration("us")
        assert out.rows() == [(dt.timedelta(seconds=1), None), (dt.timedelta(minutes=2), "a"), (dt.timedelta(microseconds=1500), "b")]


def test_a_full_join_of_time_spans_too_long_for_nanoseconds_stops(act):
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": "full"}
    this = pl.DataFrame({"A": pl.Series([dt.timedelta(seconds=1)], dtype=pl.Duration("ns"))})
    other = pl.DataFrame({"K": pl.Series([dt.timedelta(days=400 * 365)], dtype=pl.Duration("ms"))})
    with pytest.raises(ActionError, match=r"^Full join: .* don't fit in one column: a time span in nanoseconds holds up to 292 years$"):
        act("table.merge", this, params, StubContext(tables={"other": other}))


def test_full_join_with_the_first_match_adds_every_row_of_the_other_table_without_a_match(act):
    """Only the rows that match are reduced to the first one: rows with an empty key never match,
    and like the rows whose key isn't in this table, a full join adds all of them."""
    other = pl.DataFrame({"K": [1, 1, None, None, 5, 5], "N": ["a", "b", "c", "d", "e", "f"]})
    params = {"other": "other", "match": [{"column": "K", "other_column": "K"}], "how": "full", "multiple": "first"}
    ctx = StubContext(tables={"other": other})
    out = act("table.merge", pl.DataFrame({"K": [1, 2]}), params, ctx)
    assert out.rows() == [(1, "a"), (2, None), (None, "c"), (None, "d"), (5, "e"), (5, "f")]
    assert ctx.messages == [("info", "1 of 2 rows matched, 1 without a match: 2; 4 rows of the other table without a match were added")]
    every = act("table.merge", pl.DataFrame({"K": [1, 2]}), {**params, "multiple": "all"}, StubContext(tables={"other": other}))
    assert every.rows() == [(1, "a"), (1, "b"), *out.rows()[1:]]
    text = act("table.merge", pl.DataFrame({"K": ["1"]}), params, StubContext(tables={"other": other}))  # compared as text
    assert text.rows() == [("1", "a"), (None, "c"), (None, "d"), ("5", "e"), ("5", "f")]


# One column as the key for two columns of the other table, or the other way round

SHIPMENTS = pl.DataFrame(
    {
        "ShipTo": ["17", "23", "42", "42", None, "42", "5"],
        "BillTo": ["17", "99", "42", "17", "42", "42", "5"],
        "Amount": [10, 20, 30, 40, 50, 60, 70],
    }
)
CLIENTS = pl.DataFrame(  # 42 is there twice, so a join repeats rows; an empty key matches nothing
    {
        "CustomerNo": ["17", "42", "42", "777", "23", None, "42"],
        "Name": ["Anna", "Ben", "Ben B.", "Cleo", "Dora", "Emil", "Fritz"],
        "AltNo": ["17", "42", "42", "777", "99", None, "43"],
        "City": ["Berlin", "Wien", "Graz", "Bern", "Rom", "Oslo", "Linz"],
    }
)
SHAPES = {
    "other column for two": [("ShipTo", "CustomerNo"), ("BillTo", "CustomerNo")],
    "this column for two": [("ShipTo", "CustomerNo"), ("ShipTo", "AltNo")],
    "a row given twice": [("ShipTo", "CustomerNo"), ("BillTo", "CustomerNo"), ("ShipTo", "CustomerNo")],
    "both": [("BillTo", "AltNo"), ("ShipTo", "CustomerNo"), ("BillTo", "CustomerNo")],
}
COLUMN_ORDERS = [CLIENTS.columns, CLIENTS.columns[::-1], ["Name", "City", "AltNo", "CustomerNo"]]


def _merged_row_by_row(this, other, pairs, how, multiple, columns):
    """What a merge gives, worked out one row at a time: the columns, the rows, and how many rows
    of this table matched."""
    keys = [other_column for _, other_column in pairs]
    added = [c for c in (columns or other.columns) if c not in keys]
    rows = other.rows(named=True)
    if how == "lookup" or multiple == "first":  # of the rows that match, the first per key (a full join adds all others)
        seen, first = set(), []
        for r in rows:
            match = any(all(row[c] is not None and row[c] == r[o] for c, o in pairs) for row in this.rows(named=True))
            key = tuple(r[c] for c in keys)
            if (how == "full" and not match) or key not in seen:
                first.append(r)
                seen.add(key)
        rows = first
    out, used, matched = [], set(), 0
    for row in this.rows(named=True):
        found = [i for i, r in enumerate(rows) if all(row[c] is not None and row[c] == r[o] for c, o in pairs)]
        matched += bool(found)
        if how in ("matching", "non_matching"):
            if bool(found) == (how == "matching"):
                out.append(row)
            continue
        found = found[:1] if how == "lookup" else found
        used.update(found)
        out += [{**row, **{c: rows[i][c] for c in added}} for i in found]
        if not found and how != "inner":
            out.append({**row, **dict.fromkeys(added)})
    if how == "full":  # the other table's rows without a match, with their keys in this table's key columns
        for i, r in enumerate(rows):
            if i not in used:
                own = dict.fromkeys(this.columns)
                for column, other_column in reversed(pairs):  # the first row for a column gives its value
                    own[column] = r[other_column]
                out.append({**own, **{c: r[c] for c in added}})
    return [*this.columns, *([] if how in ("matching", "non_matching") else added)], out, matched


@pytest.mark.parametrize("shape", SHAPES)
@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full", "matching", "non_matching"])
@pytest.mark.parametrize("multiple", ["all", "first"])
def test_a_column_that_is_the_key_for_two(act, shape, how, multiple):
    """Every merge type, with all columns of the other table in any order or a few of them: no
    added column gets lost (a full join lost one when a column of the other table was the key for
    two columns of this table)."""
    pairs = SHAPES[shape]
    match = [{"column": column, "other_column": other_column} for column, other_column in pairs]
    for order in COLUMN_ORDERS:
        for columns in ([], ["City", "Name"]):
            other = CLIENTS.select(order)
            params = {"other": "cust", "match": match, "how": how, "multiple": multiple, "columns": columns}
            ctx = StubContext(tables={"cust": other})
            out = act("table.merge", SHIPMENTS, params, ctx)
            names, rows, matched = _merged_row_by_row(SHIPMENTS, other, pairs, how, multiple, columns)
            assert out.columns == names, (order, columns)
            assert out.rows(named=True) == rows, (order, columns)
            [(level, text)] = ctx.messages
            count = f"{7 - matched} of 7 rows without a match" if how == "non_matching" else f"{matched} of 7 rows matched"
            assert level == "info" and text.startswith(count), text


def test_a_full_join_on_two_columns_with_one_column_of_the_other_table_keeps_all_columns(act):
    orders = pl.DataFrame({"ShipTo": ["00017", "00023", "00042"], "BillTo": ["00017", "00099", "00042"], "Amount": [10, 20, 30]})
    customers = pl.DataFrame({"CustomerNo": ["00017", "00042", "00777"], "Name": ["Anna", "Ben", "Cleo"], "City": ["Berlin", "Wien", "Bern"]})
    match = [{"column": "ShipTo", "other_column": "CustomerNo"}, {"column": "BillTo", "other_column": "CustomerNo"}]
    params = {"other": "cust", "match": match, "how": "full", "columns": ["Name", "City"]}
    out = act("table.merge", orders, params, StubContext(tables={"cust": customers}))
    assert out.rows() == [
        ("00017", "00017", 10, "Anna", "Berlin"),
        ("00023", "00099", 20, None, None),
        ("00042", "00042", 30, "Ben", "Wien"),
        ("00777", "00777", None, "Cleo", "Bern"),
    ]
    numbers = pl.DataFrame({"ShipTo": [17, 23, 42], "BillTo": ["17", "99", "42"]})  # one key compared as text, one not
    unpadded = StubContext(tables={"cust": customers.with_columns(pl.col("CustomerNo").str.strip_chars_start("0"))})
    out = act("table.merge", numbers, params, unpadded)
    assert out.schema == {"ShipTo": pl.Int64, "BillTo": pl.String, "Name": pl.String, "City": pl.String}
    assert out.rows() == [(17, "17", "Anna", "Berlin"), (23, "99", None, None), (42, "42", "Ben", "Wien"), (777, "777", "Cleo", "Bern")]


@pytest.mark.parametrize(
    ("this", "other", "expected"),
    [
        pytest.param(
            [dt.datetime(2025, 1, 2, 8, 30), None],
            {"K": [True, None], "X": [0, 3]},
            ["2025-01-02 08:30:00", None, "true", "3"],
            id="date-time",
        ),
        pytest.param(
            [dt.datetime(2025, 1, 2, 8, 30)],
            {"K": [True, None], "X": ["x", "2025-01-03 09:00:00"]},
            ["2025-01-02 08:30:00", "true", "2025-01-03 09:00:00"],
            id="date-time and a text that fits it",
        ),
        pytest.param([17.0, None], {"K": [True, None], "X": ["x", "Berlin"]}, ["17", None, "true", "Berlin"], id="decimal number"),
    ],
)
def test_a_full_join_on_one_column_that_is_the_key_for_two_of_the_other_table(act, this, other, expected):
    """A row only in the other table with an empty first key gets its second key. The first key
    had made the column text: the step then stopped with a technical error ("`to_string` operation
    not supported", "conversion from `str` to `i64` failed in column 'exponent'"), or wrote the
    second key as 2025-01-03 09:00:00.000000."""
    match = [{"column": "A", "other_column": "K"}, {"column": "A", "other_column": "X"}]
    params = {"other": "other", "match": match, "how": "full", "multiple": "all"}
    out = act("table.merge", pl.DataFrame({"A": this}), params, StubContext(tables={"other": pl.DataFrame(other)}))
    assert out.schema["A"] == pl.String and out["A"].to_list() == expected
    texts = {"K": ["2025-01-02 08:30:00", "2025-01-05 10:00:00"], "X": ["2025-01-02 08:30:00", "2025-01-05 10:00:00"]}
    fits = act("table.merge", pl.DataFrame({"A": [dt.datetime(2025, 1, 2, 8, 30)]}), params, StubContext(tables={"other": pl.DataFrame(texts)}))
    assert fits["A"].to_list() == [dt.datetime(2025, 1, 2, 8, 30), dt.datetime(2025, 1, 5, 10)]  # both keys fit: a date-time


BERLIN_DAY = pl.Series([dt.datetime(2025, 1, 3, 9), dt.datetime(2025, 1, 4, 9)]).dt.replace_time_zone("Europe/Berlin")


@pytest.mark.parametrize(
    ("this", "second", "added"),
    [
        pytest.param([dt.datetime(2025, 1, 2, 8, 30)], [dt.date(2025, 1, 3), dt.date(2025, 1, 4)], "2025-01-04", id="date-times, a date"),
        pytest.param([dt.date(2025, 1, 2)], [dt.datetime(2025, 1, 3, 9), dt.datetime(2025, 1, 4, 9)], "2025-01-04 09:00:00", id="dates, a date-time"),
        pytest.param([dt.datetime(2025, 1, 2, 8, 30)], BERLIN_DAY, "2025-01-04 09:00:00", id="date-times, a time zone"),
        pytest.param([1], [5.0, 0.0], "0", id="whole numbers, 0.0"),
        pytest.param([1], [5.0, 1e16], "10000000000000000", id="whole numbers, 1e16"),
        pytest.param([1], [5.0, 2.5], "2.5", id="whole numbers, 2.5"),
        pytest.param([1], pl.Series([5.0, 0.1], dtype=pl.Float32), "0.1", id="whole numbers, Float32"),
        pytest.param([1], pl.Series([D("5.00"), D("17.00")], dtype=pl.Decimal(10, 2)), "17.00", id="whole numbers, exact decimals"),
        pytest.param([1], pl.Series([5, 2**64 - 1], dtype=pl.UInt64), "18446744073709551615", id="whole numbers, UInt64"),
    ],
)
def test_a_full_join_writes_a_later_key_as_text_when_an_earlier_one_made_the_column_text(act, this, second, added):
    """A = K and A = X: K's text "x" doesn't fit A, so A becomes text; X is compared with A as
    date-times or numbers. The row only in the other table with an empty K gets X's value, as text
    in X's own type. Before, the step stopped ("casting from string to datetime is not
    supported"), or wrote 0.0, 1e+16 and 0.10000000149011612."""
    match = [{"column": "A", "other_column": "K"}, {"column": "A", "other_column": "X"}]
    params = {"other": "other", "match": match, "how": "full", "multiple": "all"}
    other = pl.DataFrame({"K": ["x", None], "X": second})
    out = act("table.merge", pl.DataFrame({"A": this}), params, StubContext(tables={"other": other}))
    shown = {int: "1", dt.date: "2025-01-02"}.get(type(this[0]), "2025-01-02 08:30:00")
    assert out.schema["A"] == pl.String and out["A"].to_list() == [shown, "x", added]


@pytest.mark.parametrize("how", ["lookup", "full"])
@pytest.mark.parametrize(
    ("this", "other", "words"),
    [
        pytest.param(pl.Series([None], dtype=pl.Time), pl.Series([dt.datetime(2025, 1, 2, 8)]), "(time) and 'K' (date-time)", id="time, date-time"),
        pytest.param(pl.Series([None], dtype=pl.Time), pl.Series([dt.date(2025, 1, 2)]), "(time) and 'K' (date)", id="time, date"),
        pytest.param(pl.Series([None], dtype=pl.Duration("us")), pl.Series(["1h"]), "(duration) and 'K' (text)", id="time span, text"),
        pytest.param(pl.Series([None], dtype=pl.Duration("us")), pl.Series([dt.date(2025, 1, 2)]), "(duration) and 'K' (date)", id="time span, date"),
        pytest.param(pl.Series([None], dtype=pl.Duration("us")), pl.Series([5]), "(duration) and 'K' (integer)", id="time span, number"),
    ],
)
def test_keys_that_cant_be_compared_stop_also_without_values(act, how, this, other, words):
    """The types decide: Polars' cast stops only for values, so a key column without any passed,
    and a full join then stopped with a technical error ("casting from Time to Datetime not
    supported")."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    ctx = StubContext(tables={"other": other.alias("K").to_frame().with_columns(N=pl.lit("n"))})
    with pytest.raises(ActionError) as raised:
        act("table.merge", this.alias("A").to_frame(), params, ctx)
    assert str(raised.value).startswith(f"'A' {words} can't be compared: choose other key columns")


@pytest.mark.parametrize(
    ("this", "second", "added"),
    [
        pytest.param([1.5], pl.Series([5, 2**64 - 1, 2**64 - 1025], dtype=pl.UInt64), ["18446744073709551615", "18446744073709550591"], id="UInt64"),
        pytest.param([1.5], pl.Series([5, 2**63 - 1, 2**53 + 1], dtype=pl.Int64), ["9223372036854775807", "9007199254740993"], id="Int64"),
        pytest.param([1.5], pl.Series([5, 2**127 - 1, -(2**127)], dtype=pl.Int128), [str(2**127 - 1), str(-(2**127))], id="Int128"),
        pytest.param([1.5], pl.Series([D(5), D("9" * 38), D(1)], dtype=pl.Decimal(38, 0)), ["9" * 38, "1"], id="38 nines"),
        pytest.param(
            [1.5],
            pl.Series([D(5), D("1234567890.1234567891"), D("0.1000000001")], dtype=pl.Decimal(38, 10)),
            ["1234567890.1234567891", "0.1000000001"],
            id="exact decimals",
        ),
        pytest.param(
            [dt.date(2025, 1, 2)],
            pl.Series(["2025-01-03 00:00:00", "2025-01-04 08:30:00.000000001", "2025-01-05 09:00:00"]).str.to_datetime(time_unit="ns"),
            ["2025-01-04 08:30:00.000000001", "2025-01-05 09:00:00"],
            id="nanoseconds",
        ),
    ],
)
def test_a_full_join_writes_a_later_key_as_text_with_all_its_digits(act, this, second, added):
    """The second pair compares the keys as decimal numbers (or in microseconds), which keep about
    15 digits: the key of a row only in the other table is written from its own value. Before, it
    was cast back from the compared one: 2^64 - 1 got lost (an empty key) and 1234567890.1234567891
    became 1234567890.1234567168."""
    match = [{"column": "A", "other_column": "K"}, {"column": "A", "other_column": "X"}]
    params = {"other": "other", "match": match, "how": "full", "multiple": "all"}
    other = pl.DataFrame({"K": ["x", None, None], "X": second})
    out = act("table.merge", pl.DataFrame({"A": this}), params, StubContext(tables={"other": other}))
    assert out.schema["A"] == pl.String and out["A"].to_list()[2:] == added
    assert out.columns == ["A"]  # no copies of the keys are left


def test_the_hint_of_keys_that_cant_be_compared_names_a_column_that_is_not_text(act):
    """Before: "… make one of them text with a formula such as CAST([A] AS VARCHAR)" for a column A
    that is text already."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}]}
    other = StubContext(tables={"other": pl.DataFrame({"K": [dt.timedelta(hours=1)]})})
    with pytest.raises(ActionError) as raised:
        act("table.merge", pl.DataFrame({"A": ["1h"]}), params, other)
    assert str(raised.value) == (
        "'A' (text) and 'K' (duration) can't be compared: choose other key columns, or first make one of them text "
        "with a formula such as CAST([K] AS VARCHAR)"
    )


SPAN = [dt.timedelta(hours=1), dt.timedelta(0)]


@pytest.mark.parametrize(
    ("this", "other", "named"),
    [
        pytest.param([1, 2], SPAN, "K", id="integer, time span"),
        pytest.param([1.5, 2.0], SPAN, "K", id="number, time span"),
        pytest.param([dt.date(2025, 1, 2), None], SPAN, "K", id="date, time span"),
        pytest.param([dt.datetime(2025, 1, 2, 8, 30), None], SPAN, "K", id="date-time, time span"),
        pytest.param([dt.time(8, 30), None], SPAN, "K", id="time, time span"),
        pytest.param([True, False], SPAN, "K", id="true/false, time span"),
        pytest.param([b"a", b"\xff"], SPAN, "K", id="binary, time span"),
        pytest.param(pl.Series([None, None], dtype=pl.Null), SPAN, "K", id="empty, time span"),
        pytest.param(["1h", "x"], SPAN, "K", id="text, time span"),
        pytest.param(SPAN, [1, 2], "A", id="time span, integer"),
        pytest.param(SPAN, ["1h", "x"], "A", id="time span, text"),
        pytest.param([dt.time(8, 30), None], [dt.datetime(2025, 1, 2, 8, 30), None], "A", id="time, date-time"),
    ],
)
def test_the_hint_of_keys_that_cant_be_compared_works_when_followed(act, this, other, named):
    """The hint names a column that, made text with its formula, can be compared: a time span,
    which nothing else can be compared with. Before, it named this table's column against a time
    span, and the next run asked for the other one."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}]}
    left, right = pl.DataFrame({"A": this}), pl.DataFrame({"K": other})
    with pytest.raises(ActionError) as raised:
        act("table.merge", left, params, StubContext(tables={"other": right}))
    assert str(raised.value).endswith(f"such as CAST([{named}] AS VARCHAR)")
    as_text = {"columns": [{"name": named, "formula": f"CAST([{named}] AS VARCHAR)"}]}
    if named == "A":
        left = act("calc.columns", left, as_text)
    else:
        right = act("calc.columns", right, as_text)
    assert act("table.merge", left, params, StubContext(tables={"other": right})).height == 2


def test_the_hint_doubles_a_bracket_in_a_column_name(act):
    """As a formula needs it: [a]]b] is the column a]b."""
    params = {"other": "other", "match": [{"column": "a]b", "other_column": "K"}]}
    with pytest.raises(ActionError) as raised:
        act("table.merge", pl.DataFrame({"a]b": SPAN}), params, StubContext(tables={"other": pl.DataFrame({"K": [1, 2]})}))
    assert str(raised.value).endswith("such as CAST([a]]b] AS VARCHAR)")
    out = act("calc.columns", pl.DataFrame({"a]b": SPAN}), {"columns": [{"name": "a]b", "formula": "CAST([a]]b] AS VARCHAR)"}]})
    assert out.schema["a]b"] == pl.String

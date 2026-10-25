import datetime as dt
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
    assert ctx.warnings == ["'Customer' (Int64) and 'CustNo' (String) are compared as text"]


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


def test_append(act):
    ctx = StubContext(tables={"b": pl.DataFrame({"x": ["3"], "y": [True]})})
    a = pl.DataFrame({"x": [1, 2]})
    out = act("table.append", a, {"tables": ["b"]}, ctx)
    assert out.to_dicts() == [{"x": "1", "y": None}, {"x": "2", "y": None}, {"x": "3", "y": True}]
    assert act("table.append", a, {"tables": ["b"], "columns": "this"}, ctx).columns == ["x"]


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
    with pytest.raises(ActionError, match=r"^Full join: the keys 'Amount' \(.*\) and 'ID' \(.*\) don't fit in one"):
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
            ["2024-01-31 08:00:00.000000", "2024-02-01 09:30:00.250000"],
            [dt.datetime(2024, 1, 31, 8), dt.datetime(2024, 2, 1, 9, 30, 0, 250000)],
        ),
        (
            BERLIN,
            dt.datetime(2024, 1, 31, 8, tzinfo=ZoneInfo("Europe/Berlin")),
            ["2024-01-31 08:00:00.000000+01:00", "2024-07-01 09:30:00.000000+02:00"],
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
    assert ctx.warnings == [f"'A' ({dtype}) and 'K' (String) are compared as text"]
    assert ("info", "The row matched; 1 row of the other table without a match was added") in ctx.messages
    other = StubContext(tables={"other": pl.DataFrame({"K": [texts[0], "x"], "N": ["x", "y"]})})
    out = act("table.merge", df, params, other)  # "x" is no value of this type: the keys become text
    assert out.schema == {"A": pl.String, "N": pl.String} and out.rows() == [(texts[0], "x"), ("x", "y")]


@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full", "matching", "non_matching"])
def test_keys_that_cant_be_compared_stop_with_a_message(act, how):
    """Polars can't cast a duration to text, or a time to a date-time: a message instead of its error."""
    params = {"other": "other", "match": [{"column": "A", "other_column": "K"}], "how": how}
    ctx = StubContext(tables={"other": pl.DataFrame({"K": ["1 day"], "T": [dt.date(2024, 1, 31)]})})
    durations = pl.DataFrame({"A": [dt.timedelta(days=1)]})
    message = r"can't be compared: choose other key columns, or first make one of them text with a formula such as CAST\(\[A\] AS VARCHAR\)$"
    with pytest.raises(ActionError, match=r"^'A' \(Duration\(time_unit='us'\)\) and 'K' \(String\) " + message):
        act("table.merge", durations, params, ctx)
    assert ctx.warnings == []  # not "compared as text"
    times = pl.DataFrame({"A": [dt.time(8)]})
    with pytest.raises(ActionError, match=r"^'A' \(Time\) and 'T' \(Date\) can't be compared"):
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

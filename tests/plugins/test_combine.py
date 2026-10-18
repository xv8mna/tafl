from decimal import Decimal as D

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

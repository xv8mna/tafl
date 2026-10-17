import polars as pl
import pytest

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

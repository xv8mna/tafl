"""The merge step says how many rows matched, with a few key values of the rows without a match."""

import time
from datetime import date, datetime
from decimal import Decimal as D

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.testing import StubContext
from tafl.core import Engine, Project

THIS = pl.DataFrame({"Key": [f"{i:05d}" for i in range(200)], "Amount": list(range(200))})
EXTRA = [f"9{i:04d}" for i in range(12)]  # keys only in the other table
OTHERS = {  # the other table: all keys of this table, the first 180, or none
    "all": [f"{i:05d}" for i in range(200)],
    "some": [f"{i:05d}" for i in range(180)] + EXTRA,
    "none": EXTRA,
}
SOME = "00180, 00181, 00182 and 17 more"
NONE = "00000, 00001, 00002 and 197 more"
ADDED = "; 12 rows of the other table without a match were added"


def merge(act, df, other, how, *, multiple="all", match=None, **options):
    """Run a merge with the other table "Customers"; returns the result and the messages."""
    ctx = StubContext(tables={"Customers": other})
    match = match or [{"column": "Key", "other_column": "Key"}]
    params = {"other": "Customers", "match": match, "how": how, "multiple": multiple, **options}
    return act("table.merge", df, params, ctx), ctx.messages


def customers(keys):
    return pl.DataFrame({"Key": keys, "Name": [f"Name {key}" for key in keys]}, schema={"Key": pl.String, "Name": pl.String})


def info(act, df, other, how, **options) -> str:
    _out, messages = merge(act, df, other, how, **options)
    infos = [text for level, text in messages if level == "info"]
    assert len(infos) == 1, messages
    return infos[0]


@pytest.mark.parametrize(
    ("how", "covered", "expected"),
    [
        ("lookup", "all", "All 200 rows matched"),
        ("lookup", "some", f"180 of 200 rows matched, 20 without a match: {SOME}"),
        ("lookup", "none", f"None of the 200 rows matched: {NONE}"),
        ("left", "all", "All 200 rows matched"),
        ("left", "some", f"180 of 200 rows matched, 20 without a match: {SOME}"),
        ("left", "none", f"None of the 200 rows matched: {NONE}"),
        ("inner", "all", "All 200 rows matched"),
        ("inner", "some", f"180 of 200 rows matched; 20 without a match were left out: {SOME}"),
        ("inner", "none", f"None of the 200 rows matched, so the table is empty: {NONE}"),
        ("full", "all", "All 200 rows matched"),
        ("full", "some", f"180 of 200 rows matched, 20 without a match: {SOME}{ADDED}"),
        ("full", "none", f"None of the 200 rows matched: {NONE}{ADDED}"),
        ("matching", "all", "All 200 rows matched and were kept"),
        ("matching", "some", f"180 of 200 rows matched and were kept; 20 without a match were left out: {SOME}"),
        ("matching", "none", f"None of the 200 rows matched, so the table is empty: {NONE}"),
        ("non_matching", "all", "All 200 rows matched, so the table is empty"),
        ("non_matching", "some", "20 of 200 rows without a match were kept; 180 matched and were left out"),
        ("non_matching", "none", "None of the 200 rows matched: all were kept"),
    ],
)
@pytest.mark.parametrize("multiple", ["all", "first"])
def test_every_merge_type_says_how_many_rows_matched(act, how, covered, expected, multiple):
    assert info(act, THIS, customers(OTHERS[covered]), how, multiple=multiple) == expected


@pytest.mark.parametrize(
    ("how", "rows"),
    [("lookup", 200), ("left", 380), ("inner", 360), ("full", 404), ("matching", 180), ("non_matching", 20)],
)
def test_rows_that_repeat_count_once(act, how, rows):
    """Each key twice in the other table: a join repeats rows, the message counts this table's rows."""
    other = customers(OTHERS["some"] * 2)
    out, messages = merge(act, THIS, other, how)
    assert out.height == rows
    expected = {
        "inner": f"180 of 200 rows matched; 20 without a match were left out: {SOME}",
        "matching": f"180 of 200 rows matched and were kept; 20 without a match were left out: {SOME}",
        "non_matching": "20 of 200 rows without a match were kept; 180 matched and were left out",
    }.get(how, f"180 of 200 rows matched, 20 without a match: {SOME}")
    added = "; 24 rows of the other table without a match were added" if how == "full" else ""
    assert messages == [("info", expected + added)]
    # One row of each key that matches; a full join adds all 24 rows without a match
    _out, first = merge(act, THIS, other, how, multiple="first")
    assert first == [("info", expected + added)]


@pytest.mark.parametrize(
    ("how", "matched", "unmatched"),
    [
        ("lookup", "The row matched", "The row has no match: 00007"),
        ("left", "The row matched", "The row has no match: 00007"),
        ("inner", "The row matched", "The row has no match, so the table is empty: 00007"),
        ("full", "The row matched" + ADDED, "The row has no match: 00007" + ADDED),
        ("matching", "The row matched", "The row has no match, so the table is empty: 00007"),
        ("non_matching", "The row matched, so the table is empty", "The row has no match: it was kept"),
    ],
)
def test_one_row(act, how, matched, unmatched):
    other = customers(EXTRA)
    one = pl.DataFrame({"Key": ["00007"]})
    assert info(act, one, other.vstack(customers(["00007"])), how) == matched
    assert info(act, one, other, how) == unmatched


@pytest.mark.parametrize(
    ("how", "few_missing", "few_matching"),
    [
        ("lookup", "199 of 200 rows matched, 1 without a match: 00199", "1 of 200 rows matched, 199 without a match: "),
        ("inner", "199 of 200 rows matched; 1 without a match was left out: 00199", "1 of 200 rows matched; 199 without"),
        (
            "matching",
            "199 of 200 rows matched and were kept; 1 without a match was left out: 00199",
            "1 of 200 rows matched and was kept; 199 without a match were left out: ",
        ),
        (
            "non_matching",
            "1 of 200 rows without a match was kept; 199 matched and were left out",
            "199 of 200 rows without a match were kept; 1 matched and was left out",
        ),
    ],
)
def test_counts_of_one(act, how, few_missing, few_matching):
    assert info(act, THIS, customers(OTHERS["all"][:199]), how) == few_missing
    assert info(act, THIS, customers(["00000"]), how).startswith(few_matching)


def test_one_row_of_the_other_table_added(act):
    other = customers([*OTHERS["all"], "99999"])
    assert info(act, THIS, other, "full") == "All 200 rows matched; 1 row of the other table without a match was added"


@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full", "matching"])
def test_empty_keys_never_match(act, how):
    df = pl.DataFrame({"Key": [None, "00001", None, "", "00009", "00001"]})
    other = customers(["00001", None, ""])  # empty text matches empty text, an empty key nothing
    text = info(act, df, other, how)
    assert text.startswith("3 of 6 rows matched")
    assert text.split(": ")[-1].split(";")[0] == "(empty) and 00009"


def test_empty_text_is_shown(act):
    df = pl.DataFrame({"Key": ["", "x", None]})
    assert info(act, df, customers(["y"]), "lookup") == "None of the 3 rows matched: (empty text), x and (empty)"


def test_several_key_columns(act):
    df = pl.DataFrame({"Country": ["DE", "DE", "FR", "FR", "IT", "DE"], "No": [1, 2, 1, None, 5, 2]})
    other = pl.DataFrame({"Land": ["DE", "FR"], "Nr": [1, 2], "Name": ["a", "b"]})
    match = [{"column": "Country", "other_column": "Land"}, {"column": "No", "other_column": "Nr"}]
    expected = "1 of 6 rows matched, 5 without a match: (DE, 2), (FR, 1), (FR, empty) and 1 more"
    assert info(act, df, other, "lookup", match=match) == expected
    same_column_twice = [{"column": "No", "other_column": "Nr"}, {"column": "No", "other_column": "Land"}]
    text = info(act, df, other.with_columns(Land=pl.col("Nr")), "lookup", match=same_column_twice)
    assert text == "4 of 6 rows matched, 2 without a match: (empty) and 5"  # one key column, shown once


def test_and_more_counts_all_rows_without_a_match(act):
    """Up to three values in row order, also when the first 10,000 rows without a match have only
    one or two of them; the count is of all of them."""
    many = pl.DataFrame({"Key": pl.int_range(25_000, eager=True) % 12_000})
    other = pl.DataFrame({"Key": [-1], "Name": ["x"]})
    assert info(act, many, other, "lookup") == "None of the 25,000 rows matched: 0, 1, 2 and 11,997 more"
    assert info(act, many.vstack(pl.DataFrame({"Key": [-1]})), other, "left").endswith(": 0, 1, 2 and 11,997 more")
    late = pl.DataFrame({"Key": [5] * 10_000 + [6, 7, 5, 1]})  # the first 10,000 rows have one value
    assert info(act, late, other, "inner") == "None of the 10,004 rows matched, so the table is empty: 5, 6, 7 and 1 more"
    one = pl.DataFrame({"Key": [1], "Name": ["x"]})
    expected = "1 of 10,004 rows matched; 10,003 without a match were left out: 5, 6 and 7"
    assert info(act, late, one, "inner") == expected
    assert info(act, late, one, "matching") == expected.replace("matched;", "matched and was kept;")
    texts = pl.DataFrame({"Key": ["09999"] * 10_000 + ["00017", "01234", "05555"]})  # sorted, as big tables often are
    known = pl.DataFrame({"Key": ["00017", "00023"], "Name": ["a", "b"]})
    assert info(act, texts, known, "lookup") == "1 of 10,003 rows matched, 10,002 without a match: 09999, 01234 and 05555"
    halves = pl.DataFrame({"Key": [None, "1"] * 6_000 + [None, "2", "3"]})
    assert info(act, halves, other.cast({"Key": pl.String}), "lookup").endswith(": (empty), 1, 2 and 1 more")


def test_keys_compared_as_text_warn_and_inform(act):
    df = pl.DataFrame({"Order": [10, 11, 12, 13], "Customer": [1, 2, 3, None]})
    other = pl.DataFrame({"CustNo": ["1", "2", "2", "04"], "Name": ["Anna", "Ben", "Ben B.", "Olga"]})
    out, messages = merge(act, df, other, "left", match=[{"column": "Customer", "other_column": "CustNo"}])
    assert out["Name"].to_list() == ["Anna", "Ben", "Ben B.", None, None]
    assert messages == [
        ("warning", "'Customer' (Int64) and 'CustNo' (String) are compared as text"),
        ("info", "2 of 4 rows matched, 2 without a match: 3 and (empty)"),
    ]


@pytest.mark.parametrize(
    ("this", "other", "shown"),
    [
        (pl.Series([1, 3], dtype=pl.Int64), pl.Series([1.0, 2.0]), "3"),  # compared as decimal numbers
        (pl.Series([D("1.50"), D("3.25")], dtype=pl.Decimal(10, 2)), pl.Series([1.5]), "3.25"),
        (pl.Series([D(1), D(0)], dtype=pl.Decimal(20, 10)), pl.Series([1]), "0.0000000000"),  # not 0E-10
        (pl.Series([date(2026, 10, 1), date(2026, 10, 23)]), pl.Series([datetime(2026, 10, 1)]), "2026-10-23"),
        (pl.Series([datetime(2026, 10, 1), datetime(2026, 10, 23, 8, 30)]), pl.Series([date(2026, 10, 1)]), "2026-10-23 08:30:00"),
        (pl.Series(["1", "x" * 40]), pl.Series([1]), "x" * 29 + "…"),  # cut to 30 characters
        (pl.Series(["1", "two\nlines"]), pl.Series([1]), "two lines"),
    ],
)
def test_key_values_of_other_types(act, this, other, shown):
    """The values are this table's own key values, not the compared copies."""
    df, right = pl.DataFrame({"Key": this}), pl.DataFrame({"Key": other, "Name": ["x"] * other.len()})
    assert info(act, df, right, "lookup") == f"1 of 2 rows matched, 1 without a match: {shown}"
    assert info(act, df, right, "matching").endswith(f"1 without a match was left out: {shown}")


@pytest.mark.parametrize(
    ("how", "expected"),
    [
        ("lookup", "None of the 200 rows matched: the other table has no rows"),
        ("left", "None of the 200 rows matched: the other table has no rows"),
        ("full", "None of the 200 rows matched: the other table has no rows"),
        ("inner", "None of the 200 rows matched, so the table is empty: the other table has no rows"),
        ("matching", "None of the 200 rows matched, so the table is empty: the other table has no rows"),
        ("non_matching", "None of the 200 rows matched: the other table has no rows, so all were kept"),
    ],
)
def test_an_empty_other_table(act, how, expected):
    assert info(act, THIS, customers([]), how) == expected


def test_one_row_and_an_empty_other_table(act):
    one = pl.DataFrame({"Key": ["00007"]})
    assert info(act, one, customers([]), "lookup") == "The row has no match: the other table has no rows"
    assert info(act, one, customers([]), "non_matching") == "The row has no match: the other table has no rows, so it was kept"


@pytest.mark.parametrize("how", ["lookup", "left", "inner", "full", "matching", "non_matching"])
def test_no_message_for_a_table_without_rows(act, how):
    out, messages = merge(act, THIS.clear(), customers(OTHERS["some"]), how)
    if how == "full":  # a full join adds the other table's rows: it says so
        assert messages == [("info", "This table has no rows: the 192 rows of the other table were added")]
    else:
        assert messages == []
    assert out.height == (12 + 180 if how == "full" else 0)
    _, messages = merge(act, THIS.clear(), customers([]), "full")  # nothing added: nothing to say
    assert messages == []
    _, messages = merge(act, THIS.clear(), customers(["00001"]), "full")
    assert messages == [("info", "This table has no rows: the 1 row of the other table was added")]


def test_a_key_given_twice_counts_once(act):
    df = pl.DataFrame({"CustomerNo": ["1", "2", "3"], "Region": ["N", "S", "N"]})
    other = pl.DataFrame({"CustomerNo": ["1", "2"], "Name": ["Anna", "Ben"], "Region": ["N", "N"]})
    twice = [{"column": "CustomerNo", "other_column": "CustomerNo"}] * 2
    for how in ("lookup", "left", "inner", "full", "matching", "non_matching"):
        out, messages = merge(act, df, other, how, match=twice)
        once, _ = merge(act, df, other, how, match=twice[:1])
        assert out.equals(once), how
        assert len(messages) == 1, how
    # One column of the other table for two of this table
    both = [{"column": "CustomerNo", "other_column": "CustomerNo"}, {"column": "Region", "other_column": "CustomerNo"}]
    out, messages = merge(act, df.with_columns(Region=pl.col("CustomerNo")), other, "lookup", match=both)
    assert out["Name"].to_list() == ["Anna", "Ben", None]
    assert messages == [("info", "2 of 3 rows matched, 1 without a match: (3, 3)")]


ORDERS = pl.DataFrame({"Order": [10, 11, 12, 13], "Customer": ["1", "2", "3", None], "City": ["X", "Y", "Z", "W"]})
CUSTOMERS = pl.DataFrame({"CustNo": ["1", "2", "2", "4"], "Name": ["Anna", "Ben", "Ben B.", "Dora"], "City": ["B", "H", "H", "K"]})


@pytest.mark.parametrize(
    ("how", "multiple", "expected", "text"),
    [
        (
            "lookup",
            "all",
            {"Order": [10, 11, 12, 13], "Customer": ["1", "2", "3", None], "City": ["X", "Y", "Z", "W"], "Name": ["Anna", "Ben", None, None], "City (2)": ["B", "H", None, None]},
            "2 of 4 rows matched, 2 without a match: 3 and (empty)",
        ),
        (
            "left",
            "all",
            {"Order": [10, 11, 11, 12, 13], "Customer": ["1", "2", "2", "3", None], "City": ["X", "Y", "Y", "Z", "W"], "Name": ["Anna", "Ben", "Ben B.", None, None], "City (2)": ["B", "H", "H", None, None]},
            "2 of 4 rows matched, 2 without a match: 3 and (empty)",
        ),
        (
            "inner",
            "all",
            {"Order": [10, 11, 11], "Customer": ["1", "2", "2"], "City": ["X", "Y", "Y"], "Name": ["Anna", "Ben", "Ben B."], "City (2)": ["B", "H", "H"]},
            "2 of 4 rows matched; 2 without a match were left out: 3 and (empty)",
        ),
        (
            "inner",
            "first",
            {"Order": [10, 11], "Customer": ["1", "2"], "City": ["X", "Y"], "Name": ["Anna", "Ben"], "City (2)": ["B", "H"]},
            "2 of 4 rows matched; 2 without a match were left out: 3 and (empty)",
        ),
        (
            "full",
            "all",
            {"Order": [10, 11, 11, 12, 13, None], "Customer": ["1", "2", "2", "3", None, "4"], "City": ["X", "Y", "Y", "Z", "W", None], "Name": ["Anna", "Ben", "Ben B.", None, None, "Dora"], "City (2)": ["B", "H", "H", None, None, "K"]},
            "2 of 4 rows matched, 2 without a match: 3 and (empty); 1 row of the other table without a match was added",
        ),
        (
            "matching",
            "all",
            {"Order": [10, 11], "Customer": ["1", "2"], "City": ["X", "Y"]},
            "2 of 4 rows matched and were kept; 2 without a match were left out: 3 and (empty)",
        ),
        (
            "non_matching",
            "all",
            {"Order": [12, 13], "Customer": ["3", None], "City": ["Z", "W"]},
            "2 of 4 rows without a match were kept; 2 matched and were left out",
        ),
    ],
)
def test_the_merged_table_stays_as_it_was(act, how, multiple, expected, text):
    out, messages = merge(act, ORDERS, CUSTOMERS, how, multiple=multiple, match=[{"column": "Customer", "other_column": "CustNo"}])
    expected = pl.DataFrame(expected, schema_overrides={"Order": pl.Int64})
    assert out.schema == expected.schema and out.equals(expected), out
    assert messages == [("info", text)]


@pytest.mark.parametrize("how", ["lookup", "left", "full"])
def test_columns_named_like_the_markers_stay(act, how):
    df = pl.DataFrame({"Key": ["1", "2"], "__other_row__": [True, None], "__this_row__": ["a", "b"]})
    other = pl.DataFrame({"Key": ["1", "3"], "__other_row__2": [7, 8], "__this_row__": ["x", "y"]})
    out, messages = merge(act, df, other, how)
    assert out.columns == ["Key", "__other_row__", "__this_row__", "__other_row__2", "__this_row__ (2)"]
    assert out["__other_row__"].to_list()[:2] == [True, None] and out["__this_row__"].to_list()[:2] == ["a", "b"]
    assert messages[-1][1].startswith("1 of 2 rows matched, 1 without a match: 2")


def test_a_full_join_that_stops_says_nothing(act):
    """The keys don't fit in one column (see test_combine): an error, no message."""
    long = pl.Series([D("9" * 38), D(1)], dtype=pl.Decimal(38, 0))
    other = pl.DataFrame({"ID": pl.Series([D(1), D("0.5")], dtype=pl.Decimal(38, 10)), "L": ["one", "half"]})
    ctx = StubContext(tables={"other": other})
    params = {"other": "other", "match": [{"column": "Amount", "other_column": "ID"}], "how": "full"}
    with pytest.raises(ActionError, match="don't fit in one column"):  # after the join
        act("table.merge", pl.DataFrame({"Amount": long}), params, ctx)
    swapped = StubContext(tables={"other": pl.DataFrame({"ID": long, "L": ["long", "one"]})})
    with pytest.raises(ActionError, match="don't fit in one column"):  # before the join
        act("table.merge", other.select(Amount="ID"), params, swapped)
    assert ctx.messages == swapped.messages == []


def test_the_message_names_the_other_table(registry, tmp_path):
    """In a project the message has the other table's name, and the step result keeps it."""
    (tmp_path / "customers.csv").write_text("No;Name\n1;Anna\n2;Ben\n3;Cleo\n", encoding="utf-8")
    (tmp_path / "orders.csv").write_text("Order;Customer\n10;1\n11;7\n12;2\n", encoding="utf-8")
    project = Project(name="Shop")
    project.path = tmp_path / "shop.tafl"
    customers_table = project.add_table("Customers")
    project.add_step(customers_table, "csv.import", {"file": "customers.csv"})
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    match = [{"column": "Customer", "other_column": "No"}]
    project.add_step(orders, "table.merge", {"other": customers_table.id, "match": match, "how": "full"})
    result = Engine(project, registry).run(mode="preview")[orders.id]
    assert result.df["Name"].to_list() == ["Anna", None, "Ben", "Cleo"]
    expected = "2 of 3 rows matched, 1 without a match: 7; 1 row of the other table without a match was added"
    assert result.steps[1].messages == [("info", expected)]


def _best(run, rounds=3) -> float:
    times = []
    for _ in range(rounds):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return min(times)


@pytest.mark.parametrize("how", ["lookup", "inner", "full", "matching"])
def test_a_million_rows_without_a_match_stay_fast(act, how):
    """The worst case: text keys, none matches, so all key values are counted. The time of the
    step stays close to the time of the join itself (the bound is generous, measured times are in
    the release notes of the change)."""
    rows = 1_000_000
    df = pl.select(Key=(pl.int_range(rows).hash(3) % rows).cast(pl.String).str.zfill(7), Amount=pl.int_range(rows))
    other = pl.DataFrame({"Key": [f"x{i}" for i in range(100_000)], "Name": ["n"] * 100_000})
    ctx = StubContext(tables={"other": other})
    params = {"other": "other", "match": [{"column": "Key", "other_column": "Key"}], "how": how}
    join = {"lookup": "left", "matching": "semi"}.get(how, how)
    plain = _best(lambda: df.join(other, on="Key", how=join, coalesce=True, maintain_order="left"))
    merged = _best(lambda: act("table.merge", df, params, ctx))
    distinct = df["Key"].n_unique()
    assert ctx.messages[-1][1].endswith(f" and {distinct - 3:,} more" + ("; 100,000 rows of the other table without a match were added" if how == "full" else ""))
    assert merged < 3 * plain + 0.2, (merged, plain)

import datetime as dt

import polars as pl
import pytest

from etl_tool.api import ActionError


def names(df):
    return df["Name"].to_list()


def flt(act, df, *conditions, **options):
    keys = ("column", "operator", "value")
    return act("rows.filter", df, {"conditions": [dict(zip(keys, c)) for c in conditions], **options})


def test_numbers_accept_a_decimal_comma(act, people):
    assert names(flt(act, people, ("Score", ">=", "1,5"))) == ["Anna", "Carla", None]


def test_text_is_case_insensitive_by_default_and_any_works(act, people):
    out = flt(act, people, ("Name", "=", "BEN"), ("City", "starts", "ber"), match="any")
    assert names(out) == ["Anna", "ben", "Carla"]
    assert names(flt(act, people, ("Name", "=", "BEN"), case_sensitive=True)) == []


def test_not_equal_and_not_contains_include_empty_cells(act, people):
    assert names(flt(act, people, ("City", "!=", "Berlin"))) == ["ben", None, "Dieter"]
    assert names(flt(act, people, ("City", "not_contains", "er"))) == ["ben", None, "Dieter"]


def test_empty_lists_and_removing(act, people):
    assert names(flt(act, people, ("City", "empty", ""))) == ["Dieter"]
    assert names(flt(act, people, ("Age", "in", "19; 58"))) == [None, "Dieter"]
    assert names(flt(act, people, ("Age", ">", "30"), keep="remove")) == ["ben", None]


def test_dates_including_today(act):
    today = dt.date.today()
    df = pl.DataFrame({"Name": ["a", "b", "c"], "Day": [today, today - dt.timedelta(days=1), None]})
    assert names(flt(act, df, ("Day", "=", "today"))) == ["a"]
    assert names(flt(act, df, ("Day", "<", today.strftime("%d.%m.%Y")))) == ["b"]


def test_bad_values_are_reported(act, people):
    with pytest.raises(ActionError, match="'abc' is not a number"):
        flt(act, people, ("Age", "=", "abc"))


def test_filter_by_formula(act, people):
    out = act("rows.filter_formula", people, {"condition": "[Age] BETWEEN 20 AND 40 OR [Name] IS NULL"})
    assert names(out) == ["Anna", "ben", None]


def test_sort_by_several_columns_with_empty_last(act, people):
    out = act("rows.sort", people, {"by": [{"column": "City"}, {"column": "Age", "order": "desc"}]})
    assert names(out) == ["Carla", "Anna", "ben", None, "Dieter"]


def test_remove_duplicates(act):
    df = pl.DataFrame({"k": [1, 1, 2, 3, 3], "v": ["a", "b", "c", "d", "e"]})
    assert act("rows.dedupe", df, {"columns": ["k"]})["v"].to_list() == ["a", "c", "d"]
    assert act("rows.dedupe", df, {"columns": ["k"], "keep": "last"})["v"].to_list() == ["b", "c", "e"]
    assert act("rows.dedupe", df, {"columns": ["k"], "keep": "none"})["v"].to_list() == ["c"]
    assert act("rows.dedupe", df, {})["v"].to_list() == ["a", "b", "c", "d", "e"]


def test_fill_empty_values(act):
    df = pl.DataFrame({"Group": ["A", None, "  ", "B", None], "n": [1, None, 3, None, 5]})
    assert act("rows.fill", df, {"columns": ["Group"]})["Group"].to_list() == ["A", "A", "A", "B", "B"]
    assert act("rows.fill", df, {"columns": ["n"], "method": "up"})["n"].to_list() == [1, 3, 3, 5, 5]
    assert act("rows.fill", df, {"columns": ["n"], "method": "value", "value": "0"})["n"].to_list() == [1, 0, 3, 0, 5]

import datetime as dt
from decimal import Decimal as D

import polars as pl
import pytest

from tafl.api import ActionError


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


def test_sort_text_like_excel(act):
    df = pl.DataFrame({"Name": ["Zebra", "apfel", "Äpfel", "Apfel", "Özdemir", "zebra", "Peter", None, "Straße", "Strasse"]})
    ascending = act("rows.sort", df, {"by": [{"column": "Name"}]})
    assert names(ascending) == ["apfel", "Äpfel", "Apfel", "Özdemir", "Peter", "Straße", "Strasse", "Zebra", "zebra", None]
    descending = act("rows.sort", df, {"by": [{"column": "Name", "order": "desc"}], "empty_last": False})
    assert names(descending) == [None, "Zebra", "zebra", "Straße", "Strasse", "Peter", "Özdemir", "apfel", "Äpfel", "Apfel"]


def test_two_digit_years_in_typed_values(act):
    df = pl.DataFrame({"Name": ["a", "b", "c"], "Day": [dt.date(2025, 12, 31), dt.date(1975, 1, 1), None]})
    assert names(flt(act, df, ("Day", "=", "31.12.25"))) == ["a"]
    assert names(flt(act, df, ("Day", "in", "01.01.75; 1.1.2000"))) == ["b"]
    filled = act("rows.fill", df, {"columns": ["Day"], "method": "value", "value": "01.02.26"})
    assert filled["Day"][2] == dt.date(2026, 2, 1)


def test_long_whole_numbers_in_typed_values(act):
    df = pl.DataFrame({"Name": ["a", "b"], "Code": [1234567890123456789, 1234567890123456768]})
    assert names(flt(act, df, ("Code", "=", "1234567890123456789"))) == ["a"]
    assert names(flt(act, df, ("Code", "<", "1234567890123456789"))) == ["b"]


def test_typed_values_for_exact_decimals(act):
    ids = [D("12345678901234567890123"), D("12345678901234567890124"), D("5"), None]
    amounts = [D("12345678901234.5678"), D("12345678901234.5679"), D("1.10"), None]
    df = pl.DataFrame(
        {
            "Name": ["a", "b", "c", "d"],
            "ID": pl.Series(ids, dtype=pl.Decimal(30, 0)),
            "Amount": pl.Series(amounts, dtype=pl.Decimal(18, 4)),
        }
    )
    assert names(flt(act, df, ("ID", "=", "12345678901234567890124"))) == ["b"]
    assert names(flt(act, df, ("ID", "in", "12345678901234567890123; 5"))) == ["a", "c"]
    assert names(flt(act, df, ("Amount", "=", "12.345.678.901.234,5679"))) == ["b"]
    assert names(flt(act, df, ("Amount", "in", "1,1; 2"))) == ["c"]
    assert names(flt(act, df, ("Amount", "not_in", "1.1"))) == ["a", "b", "d"]
    assert names(flt(act, df, ("Amount", "<", "1e14"))) == ["a", "b", "c"]
    filled = act("rows.fill", df, {"columns": ["ID", "Amount"], "method": "value", "value": "0,005"})
    assert isinstance(filled.schema["Amount"], pl.Decimal) and filled["Amount"][3] == D("0.005")
    assert filled["Amount"][0] == D("12345678901234.5678")


FORTY = "1234567890123456789012345678901234567890"  # more digits than an exact decimal holds
TINY = "0," + "0" * 39 + "1"  # 40 decimal places


@pytest.fixture
def decimals():
    ids = [D("12345678901234567890123"), D("12345678901234567890124"), D(-5), None]
    return pl.DataFrame(
        {
            "Name": ["a", "b", "c", "d"],
            "ID": pl.Series(ids, dtype=pl.Decimal(30, 0)),
            "Small": pl.Series([D("1.50"), D("0.00"), D("-2.25"), None], dtype=pl.Decimal(10, 2)),
            "Max": pl.Series([D("9" * 38), D("0"), D("-" + "9" * 38), None], dtype=pl.Decimal(38, 0)),
        }
    )


@pytest.mark.parametrize(
    ("column", "value", "expected"),
    [
        ("ID", FORTY, {"=": [], "!=": "abcd", ">": [], ">=": [], "<": "abc", "<=": "abc"}),
        ("ID", "-" + FORTY, {"=": [], "!=": "abcd", ">": "abc", ">=": "abc", "<": [], "<=": []}),
        ("Small", TINY, {"=": [], "!=": "abcd", ">": "a", ">=": "a", "<": "bc", "<=": "bc"}),
        ("Small", "-" + TINY, {"=": [], "!=": "abcd", ">": "ab", ">=": "ab", "<": "c", "<=": "c"}),
        ("Small", "1,505", {"=": [], "!=": "abcd", ">": [], ">=": [], "<": "abc", "<=": "abc"}),
        ("Small", "1,5" + "0" * 40, {"=": "a", "!=": "bcd", ">": [], ">=": "a", "<": "bc", "<=": "abc"}),
        ("Max", "9" * 38 + ",5", {"=": [], "!=": "abcd", ">": [], ">=": [], "<": "abc", "<=": "abc"}),
        ("Max", "-" + "9" * 38 + ",5", {"=": [], "!=": "abcd", ">": "abc", ">=": "abc", "<": [], "<=": []}),
        ("Max", "9" * 38, {"=": "a", "!=": "bcd", ">": [], ">=": "a", "<": "bc", "<=": "abc"}),
        ("ID", "12345678901234567890123", {"=": "a", "!=": "bcd", ">": "b", ">=": "ab", "<": "c", "<=": "ac"}),
    ],
)
def test_typed_values_with_more_digits_than_exact_decimals_hold(act, decimals, column, value, expected):
    for operator, rows in expected.items():
        assert names(flt(act, decimals, (column, operator, value))) == list(rows), operator


def test_lists_with_more_digits_than_exact_decimals_hold(act, decimals):
    assert names(flt(act, decimals, ("ID", "in", f"{FORTY}; -5"))) == ["c"]
    assert names(flt(act, decimals, ("ID", "not_in", f"{FORTY}; -5"))) == ["a", "b", "d"]
    assert names(flt(act, decimals, ("Small", "in", f"{TINY}; 1,505; 1,5{'0' * 40}"))) == ["a"]
    assert names(flt(act, decimals, ("Small", "in", TINY))) == []
    assert names(flt(act, decimals, ("Small", "not_in", TINY))) == ["a", "b", "c", "d"]
    assert names(flt(act, decimals, ("Max", "in", f"{'9' * 38}; {'9' * 39}"))) == ["a"]


@pytest.mark.parametrize(("column", "value"), [("ID", FORTY), ("Small", TINY), ("Small", "1" * 37)])
def test_fill_with_more_digits_than_exact_decimals_hold(act, decimals, column, value):
    message = rf"^'{value}' has too many digits for column '{column}': an exact decimal holds 38, decimal places"
    with pytest.raises(ActionError, match=message + " included$"):
        act("rows.fill", decimals, {"columns": [column], "method": "value", "value": value})


def test_fill_with_more_decimal_places_than_the_values_of_the_column_allow(act):
    """The column gets the decimal places of the value: 123.45 with 37 of them would have 40
    digits. Before, Polars stopped with "conversion from decimal[10,2] to decimal[38,37] failed"."""
    df = pl.DataFrame({"d": pl.Series([D("1.5"), None, D("-123.45")], dtype=pl.Decimal(10, 2))})
    for places in (37, 36):
        value = "0," + "1" * places
        message = rf"^'{value}' has too many decimal places for the values of column 'd' \(e\.g\. -123\.45\): an exact"
        with pytest.raises(ActionError, match=message + " decimal holds 38 digits, decimal places included$"):
            act("rows.fill", df, {"columns": ["d"], "method": "value", "value": value})
    filled = act("rows.fill", df, {"columns": ["d"], "method": "value", "value": "0," + "1" * 35})  # 3 + 35 digits fit
    assert filled["d"].to_list() == [D("1.5"), D("0." + "1" * 35), D("-123.45")]
    empty = pl.DataFrame({"d": pl.Series([None], dtype=pl.Decimal(10, 2))})
    assert act("rows.fill", empty, {"columns": ["d"], "method": "value", "value": "0," + "1" * 37})["d"][0] == D("0." + "1" * 37)


def test_fill_exact_decimals_with_long_values_that_fit(act, decimals):
    filled = act("rows.fill", decimals, {"columns": ["ID", "Small"], "method": "value", "value": "1" * 36})
    assert filled["ID"].to_list()[3] == D("1" * 36) and filled["Small"].to_list()[3] == D("1" * 36)
    places = act("rows.fill", decimals, {"columns": ["Small"], "method": "value", "value": "0," + "0" * 35 + "1"})
    assert places["Small"].to_list() == [D("1.5"), D("0"), D("-2.25"), D("1e-36")]


def test_typed_date_times(act):
    stamps = [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(1945, 1, 1, 8, 30), None]
    df = pl.DataFrame({"Name": ["a", "b", "c"], "At": stamps})
    assert names(flt(act, df, ("At", "=", "31.12.25 08:30"))) == ["a"]
    assert names(flt(act, df, ("At", "<", "01.01.46 00:00:00"))) == ["b"]
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": "1.2.26 9:15"})
    assert filled["At"][2] == dt.datetime(2026, 2, 1, 9, 15)
    with pytest.raises(ActionError, match=r"^'soon' is not a date-time \(column 'At'\); use e\.g\. 31\.12\.2025 08:30"):
        flt(act, df, ("At", "=", "soon"))


def test_sort_many_texts_like_excel(act):
    words = ["Zebra", "apfel", "Äpfel", "Apfel", "Özdemir", "Straße", "Strasse", "élan", "日本", None]
    df = pl.DataFrame({"Name": words * 5_000, "Nr": range(50_000)})  # many repeated values
    out = act("rows.sort", df, {"by": [{"column": "Name"}]})
    first = ["apfel", "Äpfel", "Apfel", "élan", "Özdemir", "Straße", "Strasse", "Zebra", "日本", None]
    assert out.unique("Name", maintain_order=True)["Name"].to_list() == first
    assert out["Nr"][:6].to_list() == [1, 2, 3, 11, 12, 13]  # equal values keep their order

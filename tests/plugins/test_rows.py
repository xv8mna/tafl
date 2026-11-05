import datetime as dt
import re
import subprocess
import sys
from decimal import Decimal as D
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import polars as pl
import pytest

from tafl.api import ActionError, dtypes
from tafl.api.parameters import DateTimeText
from tafl.api.testing import StubContext

KIRITIMATI = "Pacific/Kiritimati"  # 14 hours ahead of UTC: another zone than this PC's


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


@pytest.mark.parametrize(
    ("operator", "value", "kind"),
    [(">", "-16", "number"), ("<", "31.03.2026", "date"), (">=", " 1.234,5 ", "number"), ("<=", "2026-03-31 08:30", "date")],
)
def test_text_compared_in_order_with_a_number_or_a_date_says_that_it_compares_text(act, operator, value, kind):
    """As the guide says, a text column compares text: "-14,2" is not greater than "-16", and
    "2026-05-01" is less than "31.03.2026". The rows stay as they were; the step now says so."""
    texts = ["-14,2", "-20,5", "2026-01-15", "2026-05-01", "abc", None]
    df = pl.DataFrame({"Name": list("abcdef"), "Due": texts})
    ctx = StubContext()
    out = act("rows.filter", df, {"conditions": [{"column": "Due", "operator": operator, "value": value}]}, ctx)
    keep = {">": "__gt__", ">=": "__ge__", "<": "__lt__", "<=": "__le__"}[operator]
    assert names(out) == [n for n, t in zip("abcdef", texts) if t is not None and getattr(t.lower(), keep)(value.lower())]
    label = {">": "is greater than", ">=": "is greater than or equal to", "<": "is less than", "<=": "is less than or equal to"}
    assert ctx.warnings == [
        f"Column 'Due' holds text, so the condition {label[operator]} '{value.strip()}' compares the texts character by "
        f"character, not as {kind}s: to compare {kind}s, change the column's type before this step (Change type / format)"
    ]


@pytest.mark.parametrize(("operator", "value"), [("<", "08:30"), (">=", " 8:30:15 "), (">", "17:00:00.5")])
def test_text_compared_in_order_with_a_time_says_that_it_compares_text_and_which_formula_compares_times(act, operator, value):
    """Text order puts "8:15" after "08:30" and "10:00" before "9:00". Change type makes no times,
    so the step names a formula that compares them, and that formula does."""
    texts = ["8:15", "08:45", "10:00", "9:00", "17:00", "x", None]
    df = pl.DataFrame({"Name": list("abcdefg"), "Start]": texts})
    ctx = StubContext()
    out = act("rows.filter", df, {"conditions": [{"column": "Start]", "operator": operator, "value": value}]}, ctx)
    keep = {">": "__gt__", ">=": "__ge__", "<": "__lt__", "<=": "__le__"}[operator]
    assert names(out) == [n for n, t in zip("abcdefg", texts) if t is not None and getattr(t, keep)(value)]  # as before
    label = {">": "is greater than", ">=": "is greater than or equal to", "<": "is less than"}[operator]
    formula = f"TRY_CAST([Start]]] AS TIME) {operator} TIME '{value.strip()}'"
    assert ctx.warnings == [
        f"Column 'Start]' holds text, so the condition {label} '{value.strip()}' compares the texts character by "
        f"character, not as times: to compare times, use Filter rows by formula, e.g. {formula}"
    ]
    moments = [None if t in ("x", None) else dt.time.fromisoformat(t.zfill(5)) for t in texts]
    typed = dt.time.fromisoformat(value.strip().zfill(5) if value.strip().count(":") == 1 else value.strip().zfill(8))
    in_time_order = [n for n, t in zip("abcdefg", moments) if t is not None and getattr(t, keep)(typed)]
    assert names(act("rows.filter_formula", df, {"condition": formula})) == in_time_order


@pytest.mark.parametrize(
    ("operator", "value"),
    [
        ("=", "-16"),
        ("!=", "31.03.2026"),
        (">", "Berlin"),
        ("in", "-16"),
        ("contains", "1"),
        ("<", ""),
        ("=", "08:30"),
        ("<", "25:00"),
        # words that Polars reads as numbers: they warned "not as numbers"
        (">", "Nan"),
        (">", "Inf"),
        ("<=", "infinity"),
        ("<", "-INF"),
    ],
)
def test_text_compared_otherwise_says_nothing(act, operator, value):
    ctx = StubContext()
    act("rows.filter", pl.DataFrame({"Due": ["-16", "x"]}), {"conditions": [{"column": "Due", "operator": operator, "value": value}]}, ctx)
    assert ctx.warnings == []


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


def test_a_typed_number_that_is_no_whole_number_of_the_column_keeps_its_digits(act):
    """It was compared as a decimal number, whose last digits are lost: 2**63 was equal to the
    highest Int64 and the one below it."""
    top = 2**63 - 1
    df = pl.DataFrame({"Name": ["a", "b", "c"], "Code": [top, top - 1, 1]})
    beyond = str(2**63)
    assert names(flt(act, df, ("Code", "=", beyond))) == []
    assert names(flt(act, df, ("Code", "!=", beyond))) == ["a", "b", "c"]
    assert names(flt(act, df, ("Code", "<", beyond))) == ["a", "b", "c"]
    assert names(flt(act, df, ("Code", ">", beyond))) == []
    assert names(flt(act, df, ("Code", ">", "-" + beyond))) == ["a", "b", "c"]
    assert names(flt(act, df, ("Code", ">=", f"{top - 1}.5"))) == ["a"]  # decimal places beyond a float's
    assert names(flt(act, df, ("Code", "<", "1e400"))) == ["a", "b", "c"]
    unsigned = df.with_columns(pl.Series("Code", [2**64 - 1, 2**64 - 2, 1], dtype=pl.UInt64))
    assert names(flt(act, unsigned, ("Code", "=", str(2**64)))) == []
    assert names(flt(act, unsigned, ("Code", "<", str(2**64)))) == ["a", "b", "c"]
    wide = 150000000000000000000000000000000000000  # beyond the 38 digits of an exact decimal
    huge = df.with_columns(pl.Series("Code", [wide + 1, wide, -wide], dtype=pl.Int128))
    assert names(flt(act, huge, ("Code", "<", "2.5"))) == ["c"]
    assert names(flt(act, huge, ("Code", ">", f"{wide}.5"))) == ["a"]
    assert names(flt(act, huge, ("Code", "=", f"{wide}.5"))) == []
    assert names(flt(act, huge, ("Code", "<=", f"-{wide}.5"))) == []


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


def test_typed_times(act):
    """Before, a time stayed text: Polars stopped with "cannot compare 'date/datetime/time' to a
    string value"."""
    df = pl.DataFrame({"Name": ["a", "b", "c", "d"], "Start": [dt.time(8), dt.time(8, 0, 0, 500_000), dt.time(17, 30), None]})
    assert names(flt(act, df, ("Start", "=", "08:00:00"))) == ["a"]
    assert names(flt(act, df, ("Start", "=", "8:00"))) == ["a"]
    assert names(flt(act, df, ("Start", "=", " 08:00:00.5 "))) == ["b"]
    assert names(flt(act, df, ("Start", "!=", "08:00"))) == ["b", "c", "d"]
    assert names(flt(act, df, ("Start", ">", "08:00"))) == ["b", "c"]
    assert names(flt(act, df, ("Start", "<=", "08:00:00.500"))) == ["a", "b"]
    assert names(flt(act, df, ("Start", "in", "17:30; 08:00:00"))) == ["a", "c"]
    assert names(flt(act, df, ("Start", "not_in", "17:30; 08:00:00"))) == ["b", "d"]
    assert act("rows.fill", df, {"columns": ["Start"], "method": "value", "value": "6:45"})["Start"][3] == dt.time(6, 45)


def test_typed_times_keep_their_nanoseconds(act):
    """A time with nanoseconds (from a Parquet file or a formula) and a typed one: Python's time
    has microseconds, so 08:00:00.000000001 was 08:00:00."""
    eight = 8 * 3600 * 10**9  # in nanoseconds
    df = pl.DataFrame({"Name": list("abcd"), "Start": pl.Series([eight, eight + 1, eight + 1000, None]).cast(pl.Time)})
    assert names(flt(act, df, ("Start", "=", "08:00:00.000000001"))) == ["b"]
    assert names(flt(act, df, ("Start", "!=", "08:00:00.000000001"))) == ["a", "c", "d"]
    assert names(flt(act, df, ("Start", ">", "08:00:00.000000001"))) == ["c"]
    assert names(flt(act, df, ("Start", "<=", "08:00:00.000000999"))) == ["a", "b"]
    assert names(flt(act, df, ("Start", "in", "08:00:00.000000001; 08:00:00.000001"))) == ["b", "c"]
    assert names(flt(act, df, ("Start", "not_in", "08:00:00.000000001"))) == ["a", "c", "d"]
    filled = act("rows.fill", df, {"columns": ["Start"], "method": "value", "value": "08:00:00.000000002"})
    assert filled.schema["Start"] == pl.Time and filled["Start"].to_physical().to_list() == [eight, eight + 1, eight + 1000, eight + 2]


def test_typed_date_times_keep_their_nanoseconds(act):
    """A date-time with nanoseconds (Parquet files of other programs often have them) and a typed
    one: Polars compared it with Python's date-time in microseconds, so 08:30:00 found
    08:30:00.000000001 too, and a fill made the whole column microseconds."""
    start = dt.datetime(2025, 1, 1, 8, 30)
    stamps = pl.Series([start] * 3 + [None], dtype=pl.Datetime("ns")) + pl.Series([0, 1, 1000, 0], dtype=pl.Duration("ns"))
    df = pl.DataFrame({"Name": list("abcd"), "At": stamps})
    assert names(flt(act, df, ("At", "=", "2025-01-01 08:30:00"))) == ["a"]
    assert names(flt(act, df, ("At", "=", "01.01.2025 08:30"))) == ["a"]
    assert names(flt(act, df, ("At", "=", "2025-01-01 08:30:00.000000001"))) == ["b"]
    assert names(flt(act, df, ("At", "!=", "2025-01-01 08:30:00"))) == ["b", "c", "d"]
    assert names(flt(act, df, ("At", ">", "2025-01-01 08:30"))) == ["b", "c"]
    assert names(flt(act, df, ("At", "<=", "2025-01-01 08:30:00.000000999"))) == ["a", "b"]
    assert names(flt(act, df, ("At", "in", "2025-01-01 08:30:00.000000001; 2025-01-01 08:30:00.000001"))) == ["b", "c"]
    assert names(flt(act, df, ("At", "not_in", "2025-01-01 08:30:00"))) == ["b", "c", "d"]
    assert names(flt(act, df, ("At", "<", "today"))) == ["a", "b", "c"]
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": "2025-01-01 08:30:00.000000002"})
    first = stamps.to_physical()[0]
    assert filled.schema["At"] == pl.Datetime("ns") and filled["At"].to_physical().to_list() == [first, first + 1, first + 1000, first + 2]
    now = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": "now"})["At"]
    assert now.dtype == pl.Datetime("ns") and now.to_physical().to_list()[:3] == [first, first + 1, first + 1000]


def test_typed_date_times_with_a_time_zone_are_its_local_time(act):
    """As the grid shows them (Parquet files can have such columns). Before, Polars stopped with
    "could not evaluate comparison between series 'At' of dtype: Datetime('ns', 'Europe/Berlin') and ..."."""
    utc = pl.Series([dt.datetime(2025, 1, 1, 7, 30)] * 3 + [None], dtype=pl.Datetime("ns")).dt.replace_time_zone("UTC")
    utc = utc + pl.Series([0, 1, 3600 * 10**9, 0], dtype=pl.Duration("ns"))  # 07:30, a nanosecond later, 08:30
    df = pl.DataFrame({"Name": list("abcd"), "Berlin": utc.dt.convert_time_zone("Europe/Berlin"), "UTC": utc.dt.cast_time_unit("us")})
    assert names(flt(act, df, ("Berlin", "=", "2025-01-01 08:30"))) == ["a"]  # 07:30 in UTC
    assert names(flt(act, df, ("UTC", "=", "01.01.2025 08:30"))) == ["c"]
    assert names(flt(act, df, ("Berlin", "=", "2025-01-01 08:30:00.000000001"))) == ["b"]
    assert names(flt(act, df, ("Berlin", "!=", "2025-01-01 08:30"))) == ["b", "c", "d"]
    assert names(flt(act, df, ("Berlin", ">", "2025-01-01 08:30"))) == ["b", "c"]
    assert names(flt(act, df, ("Berlin", "<", "2025-01-01 09:30"))) == ["a", "b"]
    assert names(flt(act, df, ("Berlin", "in", "2025-01-01 09:30; 2025-01-01 08:30"))) == ["a", "c"]
    assert names(flt(act, df, ("Berlin", "not_in", "2025-01-01 09:30"))) == ["a", "b", "d"]
    assert names(flt(act, df, ("UTC", "in", "2025-03-30 02:30; 2025-10-26 02:30"))) == []  # no clock changes in UTC
    filled = act("rows.fill", df, {"columns": ["Berlin", "UTC"], "method": "value", "value": "2025-07-01 08:30:00.000000002"})
    # the column of microseconds holds nanoseconds for the 2 below them, which it lost before
    assert filled.schema["Berlin"] == pl.Datetime("ns", "Europe/Berlin") and filled.schema["UTC"] == pl.Datetime("ns", "UTC")
    summer = int(dt.datetime(2025, 7, 1, 6, 30, tzinfo=dt.UTC).timestamp()) * 10**9  # 08:30 in Berlin
    assert filled["Berlin"].to_physical()[3] == summer + 2 and filled["UTC"].to_physical()[3] == summer + 2 * 3600 * 10**9 + 2
    assert filled["UTC"].to_physical()[:3].to_list() == [v * 1000 for v in df["UTC"].to_physical()[:3]]


@pytest.mark.parametrize("operator", ["=", ">=", "in", "not_in", "fill"])
@pytest.mark.parametrize(
    ("value", "happens"),
    [("2025-03-30 02:30", "doesn't happen"), ("2025-03-30 02:59:59.999999999", "doesn't happen"), ("26.10.2025 02:00", "happens twice")],
)
def test_a_typed_time_that_a_clock_change_skips_or_repeats_says_so(act, operator, value, happens):
    df = pl.DataFrame({"At": pl.Series([dt.datetime(2025, 1, 1)], dtype=pl.Datetime("ns")).dt.replace_time_zone("Europe/Berlin")})
    change = "skips" if happens == "doesn't happen" else "repeats"
    message = rf"^'{value}' {happens} in the time zone of column 'At' \(Europe/Berlin\): a clock change {change} it$"
    with pytest.raises(ActionError, match=message):
        if operator == "fill":
            act("rows.fill", df, {"columns": ["At"], "method": "value", "value": value})
        else:
            flt(act, df, ("At", operator, f"2025-01-01; {value}" if "in" in operator else value))


@pytest.mark.parametrize("zone", [None, "UTC", "Europe/Berlin"])
def test_typed_date_times_beyond_nanoseconds_stay_what_they_are(act, zone):
    """Nanoseconds reach only from 1677 to 2262: 31.12.9999 23:59:59 became 1816-03-30 05:56:07
    without a word, and 31.12.9999 stopped with "conversion from `date` to `datetime[ns]` failed".
    Such a value stays in microseconds, in which Polars then compares, as in 0.6.2."""
    stamps = pl.Series([dt.datetime(2024, 1, 1), None, dt.datetime(2025, 6, 30, 12)], dtype=pl.Datetime("ns"))
    df = pl.DataFrame({"Name": list("abc"), "At": stamps.dt.replace_time_zone(zone)})
    for value in ("31.12.9999 23:59:59", "31.12.9999", "2300-01-01", "2262-04-12 00:00", "2262-04-11 23:47:16.8547759"):
        assert names(flt(act, df, ("At", "<", value))) == ["a", "c"], value
        assert names(flt(act, df, ("At", ">=", value))) == [], value
        assert names(flt(act, df, ("At", "!=", value))) == ["a", "b", "c"], value
    for value in ("01.01.1600 00:00", "0001-01-01", "1677-09-21 00:00"):
        assert names(flt(act, df, ("At", ">", value))) == ["a", "c"], value
        assert names(flt(act, df, ("At", "<=", value))) == [], value
    assert names(flt(act, df, ("At", "in", "31.12.9999 23:59:59; 01.01.2024 00:00"))) == ["a"]
    assert names(flt(act, df, ("At", "not_in", "01.01.2024; 01.01.1600"))) == ["b", "c"]
    assert names(flt(act, df, ("At", "in", "31.12.9999; 01.01.1600"))) == []
    for value, cell in [("31.12.9999 23:59:59", dt.datetime(9999, 12, 31, 23, 59, 59)), ("01.01.1600", dt.datetime(1600, 1, 1))]:
        filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": value})["At"]
        assert filled.dtype == pl.Datetime("us", zone)  # as in 0.6.2
        assert filled.dt.replace_time_zone(None).to_list() == [dt.datetime(2024, 1, 1), cell, dt.datetime(2025, 6, 30, 12)]
    millis = act("rows.fill", df.with_columns(pl.col("At").dt.cast_time_unit("ms")), {"columns": ["At"], "method": "value", "value": "31.12.9999"})
    assert millis.schema["At"] == pl.Datetime("ms", zone) and millis["At"].dt.replace_time_zone(None)[1] == dt.datetime(9999, 12, 31)


@pytest.mark.parametrize("zone", [None, "UTC", "America/New_York"])  # in UTC, 31.12.9999 23:59:59 in New York is in the year 10000
def test_a_date_time_beyond_nanoseconds_is_compared_without_converting_the_column(act, zone):
    """Such a value was a Series of one value: Polars made all values of the nanosecond column
    microseconds to compare them with it, and Filter rows took up to 8 times as long as in 0.6.2.
    Polars now plans the condition as for 0.6.2's Python date-time: no value of the column reaches it."""
    from tafl.plugins.rows import _condition

    stamps = pl.Series([dt.datetime(2024, 1, 1), None, dt.datetime(2025, 6, 30, 12)], dtype=pl.Datetime("ns"))
    df = pl.DataFrame({"Name": list("abc"), "At": stamps.dt.replace_time_zone(zone)})
    far = SimpleNamespace(column="At", operator="<", value="31.12.9999 23:59:59")
    mask = pl.all_horizontal([_condition(df, far, False)]).fill_null(False)  # as Filter rows makes it
    python = pl.all_horizontal([pl.col("At") < dt.datetime(9999, 12, 31, 23, 59, 59)]).fill_null(False)
    naive = df.with_columns(pl.col("At").dt.replace_time_zone(None))
    assert df.lazy().filter(mask).explain() == naive.lazy().filter(python).explain()
    assert names(flt(act, df, ("At", "<", "31.12.9999 23:59:59"))) == ["a", "c"]
    assert names(flt(act, df, ("At", "<=", "31.12.9999 23:59:59"))) == ["a", "c"]
    assert names(flt(act, df, ("At", "=", "31.12.9999 23:59:59"))) == []
    assert names(flt(act, df, ("At", "!=", "31.12.9999 23:59:59"))) == ["a", "b", "c"]
    assert names(flt(act, df, ("At", ">", "31.12.9999 23:59:59"))) == []
    assert names(flt(act, df, ("At", "<", "31.12.9999 23:59:59"), ("At", ">", "01.01.1600"), keep="remove")) == ["b"]


@pytest.mark.parametrize("zone", ["Europe/Berlin", "America/New_York"])
def test_filling_with_a_date_time_beyond_nanoseconds_keeps_the_typed_time(act, zone):
    """In the column's time zone, as Polars and the grid show it: Python's rules have summer time
    in 2300 there, Polars' don't, so 12:00 would become 13:00 through a Python date-time. And
    31.12.9999 23:59:59 in New York is in the year 10000 in UTC, which a Python date-time can't be."""
    stamps = pl.Series([dt.datetime(2025, 1, 1), None], dtype=pl.Datetime("ns")).dt.replace_time_zone(zone)
    for value, cell in [("01.07.2300 12:00", dt.datetime(2300, 7, 1, 12)), ("31.12.9999 23:59:59", dt.datetime(9999, 12, 31, 23, 59, 59))]:
        filled = act("rows.fill", pl.DataFrame({"At": stamps}), {"columns": ["At"], "method": "value", "value": value})["At"]
        assert filled.dtype == pl.Datetime("us", zone)
        assert filled.dt.replace_time_zone(None).to_list() == [dt.datetime(2025, 1, 1), cell], value


@pytest.mark.parametrize("zone", [None, "Europe/Berlin"])
def test_filling_nanoseconds_with_a_date_time_beyond_them_says_which_would_change(act, zone):
    """With such a value the column holds microseconds, which cut its nanoseconds."""
    stamps = pl.Series([dt.datetime(2025, 6, 30, 12), None], dtype=pl.Datetime("ns")) + pl.Series([1, 0], dtype=pl.Duration("ns"))
    df = pl.DataFrame({"At": stamps.dt.replace_time_zone(zone)})
    message = r"^'31\.12\.9999' doesn't fit column 'At': nanoseconds reach only from 1677 to 2262, so with it the column holds"
    cut = r"\(e\.g\. 2025-06-30 12:00:00\.000000001 would become 2025-06-30 12:00:00\)$"
    with pytest.raises(ActionError, match=message + " microseconds, but it has values with nanoseconds " + cut):
        act("rows.fill", df, {"columns": ["At"], "method": "value", "value": "31.12.9999"})
    near = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": "31.12.2261"})["At"]  # the nanoseconds stay
    assert near.dtype == pl.Datetime("ns", zone) and near.to_physical()[0] == df["At"].to_physical()[0]


LOWEST, HIGHEST = -(2**63), 2**63 - 1  # nanoseconds hold 1677-09-21 00:12:43.145224192 to 2262-04-11 23:47:16.854775807 (UTC)


def local_text(moment: int, zone: str | None) -> str:
    """The local time of a moment (nanoseconds since 1970 in UTC) in a time zone, typed with all
    its digits, worked out with Python's zoneinfo and whole numbers, not with Polars."""
    micros, below = divmod(moment, 1000)
    local = dt.datetime(1970, 1, 1, tzinfo=dt.UTC) + dt.timedelta(microseconds=micros)
    if zone is not None:
        local = local.astimezone(ZoneInfo(zone))
    return f"{local:%Y-%m-%d %H:%M:%S}.{local.microsecond:06d}{below:03d}"


def nanosecond_stamps(moments: list[int | None], zone: str | None) -> pl.Series:
    """Date-times in nanoseconds of these moments, in a time zone, or without one (the time in UTC)."""
    utc = pl.Series(moments, dtype=pl.Int64).cast(pl.Datetime("ns", "UTC"))
    return utc.dt.replace_time_zone(None) if zone is None else utc.dt.convert_time_zone(zone)


@pytest.mark.parametrize(
    ("zone", "moment", "text"),
    [
        # Python has the standard time of Lord Howe there, Polars (and so Tafl) its summer time
        ("Australia/Lord_Howe", HIGHEST - 35_236_854_775_000, "2262-04-12 01:00:00.000000807"),
        ("Asia/Tokyo", HIGHEST - 10**12, None),  # zones without summer time: Python agrees with Polars
        ("Pacific/Kiritimati", HIGHEST - 3_600 * 10**9 - 1, None),
        ("Australia/Brisbane", HIGHEST - 10**12 + 999, None),
        ("America/New_York", LOWEST + 10**12 + 5, None),  # in 1677 its local mean time: 1677-09-20 19:33:21.145224197
        ("America/Los_Angeles", LOWEST + 60 * 10**9 + 7, None),
        ("Asia/Tokyo", LOWEST + 10**12 + 5, None),  # the moment and its local time are both inside: as before
        ("Europe/Berlin", LOWEST + 10**12 + 999, None),
    ],
)
def test_a_typed_date_time_near_the_ends_of_nanoseconds_is_the_moment_of_its_local_time(act, zone, moment, text):
    """In a zone east of UTC the local time can be after 2262-04-11 23:47:16.854775807 while the
    moment is before it, in a zone west of UTC before 1677-09-21 00:12:43.145224192. Polars read
    such a text in nanoseconds as a time of the other end, in the zone's old local mean time: the
    moment was 23 min 40 s off in Lord Howe, 3 min 58 s in New York, without a word."""
    df = pl.DataFrame({"Name": list("abcd"), "At": nanosecond_stamps([moment, moment - 1, moment + 1, None], zone)})
    text = text or local_text(moment, zone)
    assert names(flt(act, df, ("At", "=", text))) == ["a"]
    assert names(flt(act, df, ("At", "<", text))) == ["b"]
    assert names(flt(act, df, ("At", ">=", text))) == ["a", "c"]
    assert names(flt(act, df, ("At", "in", f"2025-01-01; {text}"))) == ["a"]
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": text})["At"]
    assert filled.dtype == pl.Datetime("ns", zone) and filled.to_physical().to_list() == [moment, moment - 1, moment + 1, moment]


@pytest.mark.parametrize("zone", [None, "UTC", "Asia/Tokyo"])
@pytest.mark.parametrize("moment", [LOWEST, LOWEST + 1, LOWEST + 808, LOWEST + 999, HIGHEST, HIGHEST - 807, HIGHEST - 999])
def test_a_typed_date_time_less_than_a_microsecond_inside_nanoseconds_is_in_nanoseconds(act, zone, moment):
    """The microsecond at each end of nanoseconds counted as beyond them: Fill empty values made
    the column microseconds, or stopped when one of its values had digits below a microsecond
    ("doesn't fit column 'At': nanoseconds reach only from 1677 to 2262"), and Filter rows compared
    in microseconds, so that 1677-09-21 00:12:43.145225 equaled 00:12:43.145225001 too."""
    other = moment + 1 if moment < HIGHEST else moment - 1
    df = pl.DataFrame({"Name": list("abc"), "At": nanosecond_stamps([moment, other, None], zone)})
    for text in {local_text(moment, zone), local_text(moment, zone).rstrip("0")}:  # 1677-09-21 00:12:43.145225 too
        assert names(flt(act, df, ("At", "=", text))) == ["a"], text
        assert names(flt(act, df, ("At", "!=", text))) == ["b", "c"], text
        assert names(flt(act, df, ("At", "<", text))) == ([] if other > moment else ["b"]), text
        assert names(flt(act, df, ("At", ">", text))) == (["b"] if other > moment else []), text
        assert names(flt(act, df, ("At", "in", text))) == ["a"], text
        filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": text})["At"]
        assert filled.dtype == pl.Datetime("ns", zone) and filled.to_physical().to_list() == [moment, other, moment], text


def test_a_typed_date_time_just_beyond_nanoseconds_is_beyond_all_of_them(act):
    """2262-04-11 23:47:16.854775808 is in the microsecond of the last nanosecond: Polars compared
    in microseconds, so that it equaled the values of that microsecond, and a fill cut it to
    2262-04-11 23:47:16.854775 without a word."""
    df = pl.DataFrame({"Name": list("abcde"), "At": nanosecond_stamps([HIGHEST, HIGHEST - 807, LOWEST, LOWEST + 808, None], None)})
    for value, lower in [("2262-04-11 23:47:16.854775808", "abcd"), ("2262-04-11 23:47:16.8547759", "abcd"), ("1677-09-21 00:12:43.145224191", "")]:
        assert names(flt(act, df, ("At", "=", value))) == [], value
        assert names(flt(act, df, ("At", "!=", value))) == ["a", "b", "c", "d", "e"], value
        assert names(flt(act, df, ("At", "<", value))) == list(lower), value
        assert names(flt(act, df, ("At", ">=", value))) == [c for c in "abcd" if c not in lower], value
        assert names(flt(act, df, ("At", "in", value))) == [], value
        with pytest.raises(ActionError, match=r"its digits below a microsecond need nanoseconds, which reach only from 1677 to 2262$"):
            act("rows.fill", df.filter(pl.col("Name") == "e"), {"columns": ["At"], "method": "value", "value": value})


def nanoseconds_of(local: dt.datetime, zone: str | None) -> int:
    """The moment of a local time in nanoseconds since 1970 in UTC, with Python's zoneinfo and
    whole numbers (a time without a zone: as if it were UTC, as Polars keeps it)."""
    if local.tzinfo is None:
        local = local.replace(tzinfo=dt.UTC if zone is None else ZoneInfo(zone))
    delta = local - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)
    return (delta.days * 86_400 + delta.seconds) * 10**9 + delta.microseconds * 1000


def stamps_in(moments: list[int | None], unit: str, zone: str | None) -> pl.Series:
    """Date-times of these moments (nanoseconds since 1970 in UTC) in ``unit``, which holds them."""
    size = {"ms": 10**6, "us": 10**3, "ns": 1}[unit]
    numbers = pl.Series([None if m is None else m // size for m in moments], dtype=pl.Int64)
    return numbers.cast(pl.Datetime(unit)) if zone is None else numbers.cast(pl.Datetime(unit, "UTC")).dt.convert_time_zone(zone)


def matching(cells: list[int | None], operator: str, value: int) -> list[str]:
    """The names of the rows (a, b, c, ...) whose cell an operator keeps, worked out with Python."""
    keep = {"=": "__eq__", "!=": "__ne__", "<": "__lt__", "<=": "__le__", ">": "__gt__", ">=": "__ge__"}[operator]
    return [chr(97 + i) for i, cell in enumerate(cells) if (cell is None and operator == "!=") or (cell is not None and getattr(cell, keep)(value))]


@pytest.mark.parametrize("zone", [None, "Europe/Berlin"])
@pytest.mark.parametrize("day", [dt.datetime(2025, 1, 1, 8, 30), dt.datetime(1960, 7, 1, 8, 30)], ids=["2025", "1960"])
@pytest.mark.parametrize(("unit", "fraction"), [("ms", "0005"), ("ms", "000000001"), ("ms", "9999999"), ("us", "0000005"), ("us", "000000999")])
def test_a_typed_date_time_with_digits_below_the_unit_of_its_column_is_compared_exactly(act, zone, day, unit, fraction):
    """Polars left out the digits that the column's unit doesn't hold: in a column of milliseconds
    08:30:00.0005 equaled 08:30:00 and was neither greater nor less. It is between two values of
    the column, and Fill empty values makes the unit finer, so that it keeps its digits."""
    size = {"ms": 10**6, "us": 10**3}[unit]
    typed = nanoseconds_of(day, zone) + int(fraction.ljust(9, "0"))
    below = typed // size * size  # the value of the column below it
    cells = [below - size, below, below + size, None]
    df = pl.DataFrame({"Name": list("abcd"), "At": stamps_in(cells, unit, zone)})
    text = f"{day:%Y-%m-%d %H:%M:%S}.{fraction}"
    for operator in ("=", "!=", "<", "<=", ">", ">="):
        assert names(flt(act, df, ("At", operator, text))) == matching(cells, operator, typed), operator
    assert names(flt(act, df, ("At", "in", f"{text}; {day:%Y-%m-%d %H:%M}"))) == matching(cells, "=", nanoseconds_of(day, zone))
    assert names(flt(act, df, ("At", "not_in", text))) == ["a", "b", "c", "d"]
    finer = "ns" if typed % 1000 else "us"
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": text})["At"]
    assert filled.dtype == pl.Datetime(finer, zone)  # Polars wrote the value below it
    assert filled.to_physical().to_list() == [m // {"us": 1000, "ns": 1}[finer] for m in cells[:3] + [typed]]


def test_a_typed_time_or_date_time_with_digits_below_a_nanosecond_is_compared_exactly(act):
    """No column holds them: Polars left them out, so 08:00:00.0000000005 equaled 08:00:00."""
    eight = 8 * 3600 * 10**9
    times = pl.DataFrame({"Name": list("abc"), "Start": pl.Series([eight, eight + 1, None]).cast(pl.Time)})
    stamps = pl.DataFrame({"Name": list("abc"), "At": stamps_in([eight, eight + 1, None], "ns", "Europe/Berlin")})
    for df, column, whole in [(times, "Start", "08:00:00"), (stamps, "At", "1970-01-01 09:00:00")]:  # 08:00 in UTC
        value = whole + ".0000000005"
        assert names(flt(act, df, (column, "=", value))) == []
        assert names(flt(act, df, (column, "!=", value))) == ["a", "b", "c"]
        assert names(flt(act, df, (column, "<", value))) == names(flt(act, df, (column, "<=", value))) == ["a"]
        assert names(flt(act, df, (column, ">", value))) == names(flt(act, df, (column, ">=", value))) == ["b"]
        assert names(flt(act, df, (column, "in", f"{value}; {whole}.000000001"))) == ["b"]
        assert names(flt(act, df, (column, "=", whole + ".0000000000"))) == ["a"]  # zeros below a nanosecond: as before
        kind = "times" if column == "Start" else "date-times"
        message = rf"^'{re.escape(value)}' doesn't fit column '{column}': {kind} hold no digits below a nanosecond$"
        with pytest.raises(ActionError, match=message):
            act("rows.fill", df, {"columns": [column], "method": "value", "value": value})


@pytest.mark.parametrize("unit", ["ms", "us"])
def test_a_date_time_parameter_with_digits_below_the_unit_of_its_column(act, unit):
    """A moment of this PC's clock, such as {Now}, has microseconds: in a column of milliseconds
    with a time zone Polars left them out, as for a typed date-time."""
    local = dt.datetime(2026, 7, 1, 10, 0, 0, 123_457)  # on this PC's clock
    moment = nanoseconds_of(local.astimezone(dt.UTC), None)
    value = DateTimeText(local.isoformat(sep=" "), local)
    size = {"ms": 10**6, "us": 10**3}[unit]
    below = moment // size * size
    cells = [below - size, below, below + size, None]
    df = pl.DataFrame({"Name": list("abcd"), "At": stamps_in(cells, unit, KIRITIMATI)})
    for operator in ("=", "!=", "<", ">="):
        assert names(flt(act, df, ("At", operator, value))) == matching(cells, operator, moment), operator
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": value})["At"]
    assert filled.dtype == pl.Datetime("us", KIRITIMATI) and filled.to_physical()[3] == moment // 1000


@pytest.mark.parametrize(
    ("cells", "unit", "value", "message"),
    [
        (  # its digits below a microsecond can't be after 2262
            [nanoseconds_of(dt.datetime(2025, 1, 1), None), None],
            "us",
            "9999-01-01 00:00:00.000000001",
            r"its digits below a microsecond need nanoseconds, which reach only from 1677 to 2262$",
        ),
        (  # the column of microseconds can't become nanoseconds
            [nanoseconds_of(dt.datetime(9999, 12, 31), None), None],
            "us",
            "2025-01-01 08:30:00.000000001",
            r"with it the column holds nanoseconds, which reach only from 1677 to 2262, but it has values beyond them \(e\.g\. 9999-12-31 00:00:00\)$",
        ),
        (  # nor milliseconds of a year far beyond 9999 microseconds
            [2**62 * 10**6, None],
            "ms",
            "2025-01-01 08:30:00.0005",
            r"with it the column holds microseconds, but it has values beyond them",
        ),
    ],
)
def test_filling_a_date_time_that_a_finer_unit_of_its_column_cant_take_says_so(act, cells, unit, value, message):
    df = pl.DataFrame({"At": stamps_in(cells, unit, None)})
    with pytest.raises(ActionError, match=rf"^'{re.escape(value)}' doesn't fit column 'At': " + message):
        act("rows.fill", df, {"columns": ["At"], "method": "value", "value": value})


@pytest.mark.parametrize("unit", ["ns", "us", "ms"])
def test_a_date_time_parameter_on_a_column_with_a_time_zone_is_a_moment_of_this_pcs_clock(act, unit):
    """As in formulas, where {Now} is this moment: it was the local time of the column's zone
    there, wrong by the difference to this PC's zone, without a word. A typed date-time stays
    the local time of the column's zone, and other columns get the text as before."""
    local = dt.datetime(2026, 7, 1, 10, 0)  # on this PC's clock
    utc = local.astimezone(dt.UTC).replace(tzinfo=None)
    stamps = pl.Series([utc - dt.timedelta(minutes=30), utc, utc + dt.timedelta(minutes=30), None], dtype=pl.Datetime(unit))
    df = pl.DataFrame({"Name": list("abcd"), "At": stamps.dt.replace_time_zone("UTC").dt.convert_time_zone(KIRITIMATI)})
    value = DateTimeText(" 2026-07-01 10:00:00 ", local)  # the setting " {Cut} "
    assert names(flt(act, df, ("At", "<", value))) == ["a"]
    assert names(act("rows.filter_formula", df, {"condition": "[At] < TIMESTAMP '2026-07-01 10:00:00'"})) == ["a"]
    assert names(flt(act, df, ("At", "=", value))) == ["b"]
    assert names(flt(act, df, ("At", ">=", value))) == ["b", "c"]
    assert names(flt(act, df, ("At", "in", value))) == ["b"]
    assert names(flt(act, df, ("At", "not_in", value))) == ["a", "c", "d"]
    assert names(flt(act, df, ("At", "<", "2026-07-01 10:00:00"))) == []  # typed: 10:00 in Kiritimati
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": value})["At"]
    assert filled.dtype == pl.Datetime(unit, KIRITIMATI) and filled[3] == utc.replace(tzinfo=dt.UTC)
    others = pl.DataFrame({"At": [local, None], "Day": [local.date(), None], "Text": ["x", None]})
    filled = act("rows.fill", others, {"columns": ["At", "Day", "Text"], "method": "value", "value": value})
    assert filled.row(1) == (local, local.date(), " 2026-07-01 10:00:00 ")


def test_a_date_time_parameter_beyond_nanoseconds_on_a_column_with_a_time_zone(act):
    """The range rule of typed values, and a moment that is in the year 10000 in the column's zone
    can't be a value of it."""
    end = DateTimeText("9999-12-31 23:59:59", dt.datetime(9999, 12, 31, 23, 59, 59))
    stamps = pl.Series([dt.datetime(2024, 1, 1), None], dtype=pl.Datetime("ns")).dt.replace_time_zone("UTC")
    df = pl.DataFrame({"Name": ["a", "b"], "At": stamps})
    assert names(flt(act, df, ("At", "<", end))) == ["a"]
    assert names(flt(act, df.with_columns(pl.col("At").dt.convert_time_zone(KIRITIMATI)), ("At", "<", end))) == ["a"]
    assert names(flt(act, df, ("At", "in", end))) == []
    later = DateTimeText("2999-12-31 00:00:00", dt.datetime(2999, 12, 31))
    filled = act("rows.fill", df, {"columns": ["At"], "method": "value", "value": later})["At"]
    assert filled.dtype == pl.Datetime("us", "UTC") and filled[1] == dt.datetime(2999, 12, 31).astimezone(dt.UTC)
    message = r"^'9999-12-31 23:59:59' doesn't fit column 'At': in its time zone \(Pacific/Kiritimati\) this moment is in"
    with pytest.raises(ActionError, match=message + r" the year 10000, and date-times reach from the year 1 to 9999$"):
        act("rows.fill", df.with_columns(pl.col("At").dt.convert_time_zone(KIRITIMATI)), {"columns": ["At"], "method": "value", "value": end})


@pytest.mark.parametrize("zone", ["Pacific/Kiritimati", "Pacific/Pago_Pago"])  # 25 hours apart: one has another date than this PC
def test_today_and_now_are_the_local_date_and_time_of_a_columns_time_zone(act, zone):
    now = dt.datetime.now(dt.UTC)
    start = dt.datetime.combine(now.astimezone(ZoneInfo(zone)).date(), dt.time(), tzinfo=ZoneInfo(zone)).astimezone(dt.UTC)
    hour, tick = dt.timedelta(hours=1), dt.timedelta(microseconds=1)
    moments = [start - tick, start, start + dt.timedelta(days=1) - tick, now - hour, now + hour]
    utc = pl.Series([t.replace(tzinfo=None) for t in moments], dtype=pl.Datetime("ns")).dt.replace_time_zone("UTC")
    df = pl.DataFrame({"Name": list("abcde"), "At": utc.dt.convert_time_zone(zone)})
    assert names(flt(act, df.head(3), ("At", "=", "today"))) == ["b"]
    assert names(flt(act, df.head(3), ("At", "<", "today"))) == ["a"]
    assert names(flt(act, df.tail(2), ("At", "<", "now"))) == ["d"]
    assert names(flt(act, df.tail(2), ("At", "in", "today; now"))) == []
    empty = df.with_columns(pl.lit(None, dtype=pl.Datetime("ns", zone)).alias("At"))
    filled = act("rows.fill", empty, {"columns": ["At"], "method": "value", "value": "now"})["At"]
    assert filled.dtype == pl.Datetime("ns", zone) and abs(filled[0] - dt.datetime.now(dt.UTC)) < hour


@pytest.mark.parametrize("operator", ["=", "in", "not_in"])
@pytest.mark.parametrize("value", ["soon", "25:00", "24:00:00", "0800"])
def test_a_typed_value_that_is_no_time(act, operator, value):
    df = pl.DataFrame({"Start": [dt.time(8)]})
    with pytest.raises(ActionError, match=rf"^'{value}' is not a time \(column 'Start'\); use e\.g\. 08:30 or 08:30:15$"):
        flt(act, df, ("Start", operator, value))


@pytest.mark.parametrize("dtype", [pl.Int8, pl.Int64, pl.UInt32, pl.UInt64, pl.Int128])
def test_lists_with_values_that_are_no_whole_numbers(act, dtype):
    """A decimal number can't be in a whole-number column. Before, Polars stopped with "'is_in'
    cannot check for Int64 values in List(Float64) data"."""
    df = pl.DataFrame({"Name": ["a", "b", "c", "d"], "Qty": pl.Series([1, 2, 100, None], dtype=dtype)})
    assert names(flt(act, df, ("Qty", "in", "1,5; 2"))) == ["b"]
    assert names(flt(act, df, ("Qty", "in", "1,5"))) == []
    assert names(flt(act, df, ("Qty", "not_in", "1,5; 2"))) == ["a", "c", "d"]
    assert names(flt(act, df, ("Qty", "not_in", "1,5"))) == ["a", "b", "c", "d"]
    assert names(flt(act, df, ("Qty", "in", "2,0; 1e2"))) == ["b", "c"]  # whole numbers in another form
    assert names(flt(act, df, ("Qty", "in", "2; inf; nan; 1e30; 1e300"))) == ["b"]
    assert names(flt(act, df, ("Qty", "in", "1e300; 1e30; nan; inf; 2"))) == ["b"]


def test_lists_of_long_whole_numbers(act):
    df = pl.DataFrame({"Name": ["a", "b"], "Code": [1234567890123456789, 100_000_000_000_000_000]})
    assert names(flt(act, df, ("Code", "in", "1234567890123456789; 1,5"))) == ["a"]
    assert names(flt(act, df, ("Code", "in", "1e17"))) == ["b"]
    wide = pl.DataFrame({"Name": ["a", "b"], "Code": pl.Series([2, 2**70], dtype=pl.Int128)})
    assert names(flt(act, wide, ("Code", "in", f"2; {2**70}"))) == ["a", "b"]  # a list Polars can't build by itself


LONG = [12345678901234567890, 12345678901234567891, 18446744073709551615, 7, None]  # beyond an Int64


@pytest.mark.parametrize("dtype", [pl.UInt64, pl.Int128, pl.UInt128])
def test_whole_numbers_beyond_int64_keep_all_their_digits(act, dtype):
    """Before, they became decimal numbers: lists lost them without a word, and = found the row next to it too."""
    df = pl.DataFrame({"Name": list("abcde"), "V": pl.Series(LONG, dtype=dtype)})
    assert names(flt(act, df, ("V", "in", "12345678901234567890"))) == ["a"]
    assert names(flt(act, df, ("V", "not_in", "12345678901234567890"))) == ["b", "c", "d", "e"]
    assert names(flt(act, df, ("V", "in", "18446744073709551615; 7"))) == ["c", "d"]
    assert names(flt(act, df, ("V", "not_in", "18446744073709551615; 7"))) == ["a", "b", "e"]
    assert names(flt(act, df, ("V", "=", "12345678901234567890"))) == ["a"]
    assert names(flt(act, df, ("V", "!=", "12345678901234567890"))) == ["b", "c", "d", "e"]
    assert names(flt(act, df, ("V", ">", "12345678901234567890"))) == ["b", "c"]
    assert names(flt(act, df, ("V", "<=", "12345678901234567890"))) == ["a", "d"]
    filled = act("rows.fill", df, {"columns": ["V"], "method": "value", "value": "12345678901234567892"})
    assert filled.schema["V"] == dtype and filled["V"][4] == 12345678901234567892


def test_the_limits_of_wide_whole_numbers(act):
    wide = pl.DataFrame({"Name": ["low", "high", "b"], "V": pl.Series([-(2**127), 2**127 - 1, 7], dtype=pl.Int128)})
    assert names(flt(act, wide, ("V", "in", f"{-(2**127)}; {2**127 - 1}"))) == ["low", "high"]
    assert names(flt(act, wide, ("V", "=", str(2**127 - 1)))) == ["high"]
    assert names(flt(act, wide, ("V", "in", f"{2**127}; {-(2**127) - 1}"))) == []
    highest = pl.DataFrame({"Name": ["high", "b", "c"], "V": pl.Series([2**128 - 1, 2**127 + 5, None], dtype=pl.UInt128)})
    assert names(flt(act, highest, ("V", "in", str(2**128 - 1)))) == ["high"]
    assert names(flt(act, highest, ("V", "=", str(2**127 + 5)))) == ["b"]
    assert names(flt(act, highest, ("V", "<", str(2**128 - 1)))) == ["b"]
    assert act("rows.fill", highest, {"columns": ["V"], "method": "value", "value": str(2**127 + 6)})["V"][2] == 2**127 + 6


@pytest.mark.parametrize("value", ["-1", "-9223372036854775808", "7-"])
def test_a_number_below_0_is_below_all_values_of_an_unsigned_column(act, value):
    """Polars compares a UInt128 column with -1 as Int128 numbers: > and >= left out its values
    from 2^127 up."""
    df = pl.DataFrame({"Name": list("abcd"), "V": pl.Series([2**128 - 1, 2**127, 5, None], dtype=pl.UInt128)})
    for operator, expected in {">": "abc", ">=": "abc", "<": "", "<=": "", "=": "", "!=": "abcd"}.items():
        assert names(flt(act, df, ("V", operator, value))) == list(expected), operator


@pytest.mark.parametrize("dtype", [pl.Int8, pl.Int64, pl.UInt64, pl.Int128, pl.UInt128])
@pytest.mark.parametrize("value", ["1e39", "1e300", "-1e39", str(2**128), str(2**200)])
def test_whole_numbers_beyond_every_column(act, dtype, value):
    """Polars takes no such Python number: they stay decimal numbers, and are beyond all values."""
    df = pl.DataFrame({"Name": ["a", "b"], "V": pl.Series([1, None], dtype=dtype)})
    assert names(flt(act, df, ("V", "=", value))) == []
    assert names(flt(act, df, ("V", "!=", value))) == ["a", "b"]
    assert names(flt(act, df, ("V", "<", value))) == ([] if value.startswith("-") else ["a"])
    assert names(flt(act, df, ("V", ">", value))) == (["a"] if value.startswith("-") else [])
    assert names(flt(act, df, ("V", "in", value))) == []
    assert names(flt(act, df, ("V", "not_in", value))) == ["a", "b"]


def test_a_decimal_number_in_many_digits_is_no_whole_number(act):
    """A Float64 has no room for its decimal places: it can't be the value before or after it."""
    cells = [9007199254740993, 9007199254740994, 12345678901234567168, 12345678901234567890, 5]
    df = pl.DataFrame({"Name": list("abcde"), "V": pl.Series(cells, dtype=pl.UInt64)})
    assert names(flt(act, df, ("V", "in", "9007199254740993,5"))) == []
    assert names(flt(act, df, ("V", "in", "12345678901234567890.5"))) == []
    assert names(flt(act, df, ("V", "not_in", "12345678901234567890.5"))) == list("abcde")
    assert names(flt(act, df, ("V", "in", "5,0; 0." + "0" * 60 + "1"))) == ["e"]


def test_filling_a_whole_number_column(act):
    """What the column can hold keeps its type, other values widen it as they always did."""
    df = pl.DataFrame({"V": pl.Series([1, None], dtype=pl.Int8)})
    for value, dtype, cell in [("7", pl.Int8, 7), ("300", pl.Int16, 300), ("1,5", pl.Float64, 1.5), ("1e30", pl.Float64, 1e30), (str(2**64), pl.Float64, 2.0**64)]:
        filled = act("rows.fill", df, {"columns": ["V"], "method": "value", "value": value})
        assert filled.schema["V"] == dtype and filled["V"][1] == cell


@pytest.mark.parametrize("dtype", [pl.UInt64, pl.UInt128])
def test_filling_unsigned_whole_numbers_with_a_number_below_0_says_what_does_not_fit(act, dtype):
    """With a number below 0 the column gets a type with a sign, which doesn't hold its highest
    values. Before, Polars stopped with "conversion from `u64` to `i64` failed"."""
    top, high = (2**64 - 1, 2**63 - 1) if dtype == pl.UInt64 else (2**128 - 1, 2**127 - 1)
    df = pl.DataFrame({"V": pl.Series([top, None], dtype=dtype)})
    message = rf"^'-1' doesn't fit column 'V': with numbers below 0 the column holds whole numbers up to {high}, but"
    with pytest.raises(ActionError, match=message + rf" it has bigger ones \(e\.g\. {top}\)$"):
        act("rows.fill", df, {"columns": ["V"], "method": "value", "value": "-1"})
    fits = pl.DataFrame({"V": pl.Series([high, None], dtype=dtype)})  # as before: the type with a sign holds it
    assert act("rows.fill", fits, {"columns": ["V"], "method": "value", "value": "-1"})["V"].to_list() == [high, -1]


@pytest.mark.parametrize("dtype", [pl.Int64, pl.UInt64, pl.Int128, pl.UInt128])
@pytest.mark.parametrize("value", ["1,5", str(2**200)], ids=["decimal places", "beyond the type"])
def test_filling_whole_numbers_with_a_decimal_number_says_which_would_change(act, dtype, value):
    """The column becomes decimal numbers, which keep about 15 digits: its long whole numbers
    changed without a word (9223372036854775807 became 9223372036854775808)."""
    top = pl.select(dtype.max()).item()
    df = pl.DataFrame({"V": pl.Series([2**60, top, None], dtype=dtype)})
    message = rf"^'{value}' doesn't fit column 'V': with it the column holds decimal numbers, which keep about 15 digits,"
    with pytest.raises(ActionError, match=message + rf" but it has longer whole numbers \(e\.g\. {top} would become {int(float(top))}\)$"):
        act("rows.fill", df, {"columns": ["V"], "method": "value", "value": value})
    fits = pl.DataFrame({"V": pl.Series([2**60, None], dtype=dtype)})  # as before: a decimal number keeps it
    filled = act("rows.fill", fits, {"columns": ["V"], "method": "value", "value": value})
    assert filled.schema["V"] == pl.Float64 and filled["V"][0] == 2**60


@pytest.mark.parametrize(
    ("dtype", "value", "becomes"),
    [
        (pl.Int8, "12345678901234567891", 12345678901234567168),
        (pl.Int64, "-12.345.678.901.234.567.891", -12345678901234567168),
        (pl.Int64, "9007199254740993,5", 9007199254740994),
        (pl.UInt64, "123456789012345678901", 123456789012345683968),
        (pl.Int128, str(2**127 + 1), 2**127),
        (pl.UInt128, str(2**128 + 1), 2**128),
        (pl.Int64, str(2**200 + 1), 2**200),
    ],
)
def test_filling_whole_numbers_with_a_typed_number_that_decimal_numbers_round_says_so(act, dtype, value, becomes):
    """A typed number that the column's type can't hold makes it decimal numbers, which keep about
    15 digits: the number changed without a word (12345678901234567891 became 12345678901234567168)."""
    df = pl.DataFrame({"V": pl.Series([1, None], dtype=dtype)})
    message = rf"^'{re.escape(value)}' doesn't fit column 'V': with it the column holds decimal numbers, which keep about 15"
    with pytest.raises(ActionError, match=message + rf" digits, so it would become {becomes}$"):
        act("rows.fill", df, {"columns": ["V"], "method": "value", "value": value})
    kept = act("rows.fill", df, {"columns": ["V"], "method": "value", "value": str(becomes)})  # as before: it stays
    assert int(kept["V"][1]) == becomes


@pytest.mark.parametrize(
    ("value", "becomes"),
    [
        ("1234567890123456.78", "1234567890123456.8"),
        ("123456789,123456789", "123456789.12345679"),
        ("-0.1234567890123456789", "-0.12345678901234568"),
        ("0.0000123456789012345678", "0.000012345678901234568"),
    ],
)
def test_filling_whole_numbers_with_decimal_places_that_decimal_numbers_round_says_so(act, value, becomes):
    """Only the whole part was checked: 1234567890123456.78 became 1234567890123456.8 without a word."""
    df = pl.DataFrame({"V": pl.Series([1, None], dtype=pl.Int64)})
    message = rf"^'{re.escape(value)}' doesn't fit column 'V': with it the column holds decimal numbers, which keep about 15"
    with pytest.raises(ActionError, match=message + rf" digits, so it would become {re.escape(becomes)}$"):
        act("rows.fill", df, {"columns": ["V"], "method": "value", "value": value})


@pytest.mark.parametrize(
    ("value", "cell"),
    [
        ("0.1", 0.1),
        ("1,5", 1.5),
        ("2.50", 2.5),
        ("1.234,56", 1234.56),
        ("-0.5", -0.5),
        ("1,234,567.25", 1234567.25),
        ("1.234.567,25 €", 1234567.25),
        ("1e-5", 0.00001),
        ("1" + "0" * 37, 1e37),  # stopped: the cell shows the typed number
    ],
)
def test_filling_whole_numbers_with_a_number_that_decimal_numbers_keep(act, value, cell):
    df = pl.DataFrame({"V": pl.Series([1, None], dtype=pl.Int64)})
    filled = act("rows.fill", df, {"columns": ["V"], "method": "value", "value": value})
    assert filled.schema["V"] == pl.Float64 and filled["V"].to_list() == [1.0, cell]


def test_a_list_with_a_value_the_column_can_not_hold(act):
    df = pl.DataFrame({"Name": ["a", "b"], "Qty": pl.Series([1, 2], dtype=pl.Int8)})
    assert names(flt(act, df, ("Qty", "in", "1; 300; -1"))) == ["a"]
    assert names(flt(act, df.with_columns(pl.col("Qty").cast(pl.UInt8)), ("Qty", "in", "2; 300; -1"))) == ["b"]


def test_lists_of_decimal_numbers_keep_their_decimals(act):
    df = pl.DataFrame({"Name": ["a", "b"], "Net": [1.5, 2.0]})
    assert names(flt(act, df, ("Net", "in", "1,5"))) == ["a"]
    assert names(flt(act, df, ("Net", "not_in", "1,5; 7"))) == ["b"]


def test_sort_many_texts_like_excel(act):
    words = ["Zebra", "apfel", "Äpfel", "Apfel", "Özdemir", "Straße", "Strasse", "élan", "日本", None]
    df = pl.DataFrame({"Name": words * 5_000, "Nr": range(50_000)})  # many repeated values
    out = act("rows.sort", df, {"by": [{"column": "Name"}]})
    first = ["apfel", "Äpfel", "Apfel", "élan", "Özdemir", "Straße", "Strasse", "Zebra", "日本", None]
    assert out.unique("Name", maintain_order=True)["Name"].to_list() == first
    assert out["Nr"][:6].to_list() == [1, 2, 3, 11, 12, 13]  # equal values keep their order


@pytest.mark.parametrize("operator", ["=", "!=", ">", ">=", "<", "<=", "in", "not_in"])
def test_a_column_of_time_spans_says_that_a_typed_value_cant_be_used(act, operator):
    conditions = [{"column": "V", "operator": operator, "value": "1"}]
    with pytest.raises(ActionError, match="Column 'V' holds time spans: a typed value can't be used with it"):
        act("rows.filter", pl.DataFrame({"V": [dt.timedelta(hours=1)]}), {"conditions": conditions})


MOMENTS = [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 9, 15, 0, 500_000), None]
TEXTS = pl.DataFrame(
    {
        "Name": ["a", "b", "c"],
        "When": MOMENTS,
        "Berlin": pl.Series(MOMENTS).dt.replace_time_zone("Europe/Berlin"),
        "Nanos": pl.Series(MOMENTS, dtype=pl.Datetime("ns")) + pl.Series([0, 1, 0], dtype=pl.Duration("ns")),
        "Start": [dt.time(8, 30), dt.time(8, 30, 0, 500_000), None],
        "Net": [17.0, 2.5, None],
        "Big": [1e16, -0.0, None],
        "Span": [dt.timedelta(days=1, hours=2, minutes=3), dt.timedelta(seconds=1.5), None],
        "Exact": pl.Series([D("5.00"), D("1.25"), None], dtype=pl.Decimal(10, 2)),
        "Day": [dt.date(2025, 12, 31), dt.date(2026, 1, 1), None],
        "Paid": [True, False, None],
    }
)


@pytest.mark.parametrize(
    ("column", "operator", "value", "expected"),
    [
        ("When", "ends", "08:30:00", "a"),  # Polars wrote 2025-12-31 08:30:00.000000
        ("When", "ends", "09:15:00.500", "b"),
        ("When", "contains", "000000", ""),
        ("Berlin", "ends", "08:30:00", "a"),  # the local time, without +01:00
        ("Berlin", "contains", "+01", ""),
        ("Nanos", "ends", ":00.500000001", "b"),
        ("Nanos", "ends", "08:30:00", "a"),
        ("Start", "ends", "08:30:00", "a"),  # Polars wrote 08:30:00 for 08:30:00.5 too
        ("Start", "contains", "00.5", "b"),
        ("Net", "ends", "17", "a"),  # not 17.0
        ("Net", "contains", ".0", ""),
        ("Big", "starts", "10000000000000000", "a"),  # not 1e16
        ("Big", "contains", "e", ""),
        ("Big", "not_contains", "-", "abc"),  # 0, not -0.0
        ("Span", "contains", "2h", "a"),  # it stopped: a typed value couldn't be used with time spans
        ("Span", "starts", "1D", "a"),
        ("Span", "ends", "500ms", "b"),
        ("Span", "not_contains", "d", "bc"),
        ("Exact", "ends", ".00", "a"),  # as before
        ("Day", "starts", "2025-12", "a"),
        ("Paid", "starts", "TR", "a"),
    ],
)
def test_contains_starts_and_ends_look_at_the_text_of_values_that_are_no_text(act, column, operator, value, expected):
    """As Split column and Pivot write them (``dtypes.text_expr``): Polars' own text has other
    digits, the offset of a time zone, and no fraction of a second for a time."""
    assert names(flt(act, TEXTS, (column, operator, value))) == list(expected)


class Thing:
    """A value of a plugin's own type."""

    def __str__(self):
        return "thing"


NESTED = pl.DataFrame(
    {
        "Name": ["a", "b", "c"],
        "Tags": [["x", "y"], [], None],
        "Sizes": pl.Series([[1, 2], [3, 4], None], dtype=pl.Array(pl.Int64, 2)),
        "Pair": [{"x": 1, "y": "a"}, {"x": 2, "y": None}, None],
        "Own": pl.Series([Thing(), Thing(), None], dtype=pl.Object),
    }
)


@pytest.mark.parametrize("operator", ["=", "!=", ">", ">=", "<", "<=", "contains", "not_contains", "starts", "ends", "in", "not_in"])
@pytest.mark.parametrize(
    ("column", "message"),
    [
        ("Tags", r"^Column 'Tags' holds lists, which Filter rows can't compare .*LIST_CONTAINS\(\[Tags\], 'a'\)$"),
        ("Sizes", r"^Column 'Sizes' holds lists, which Filter rows can't compare .*LIST_CONTAINS\(\[Sizes\], 1\)$"),
        ("Pair", r"^Column 'Pair' holds records, which Filter rows can't compare .*STRUCT_EXTRACT\(\[Pair\], 'x'\) = 1$"),
        ("Own", r"^Column 'Own' holds values of a plugin's own type, which Filter rows can't compare with a value$"),
    ],
)
def test_lists_and_records_say_that_filter_rows_cant_compare_them(act, operator, column, message):
    """Before, Polars stopped with "cannot cast List type", "'is_in' cannot check for Struct…" or
    "cannot cast 'Object' type", and a record was compared as text without a word."""
    with pytest.raises(ActionError, match=message):
        flt(act, NESTED, (column, operator, "a"))


def test_lists_and_records_can_be_empty_or_not(act):
    for column in ("Tags", "Sizes", "Pair", "Own"):
        assert names(flt(act, NESTED, (column, "empty", ""))) == ["c"], column
        assert names(flt(act, NESTED, (column, "not_empty", ""))) == ["a", "b"], column


def test_the_formula_of_the_message_for_lists_and_records_works(act):
    """What the message proposes, with the name of the column written for a formula."""
    df = NESTED.rename({"Tags": "Tags]", "Pair": "It's"}).with_columns(pl.Series("It's", [{"it's": 1}, {"it's": 2}, None]))
    with pytest.raises(ActionError, match=r"LIST_CONTAINS\(\[Tags\]\]\], 'a'\)") as raised:
        flt(act, df, ("Tags]", "contains", "x"))
    formula = re.search(r"LIST_CONTAINS\(.*\)", str(raised.value)).group().replace("'a'", "'x'")
    assert names(act("rows.filter_formula", df, {"condition": formula})) == ["a"]
    with pytest.raises(ActionError, match=r"STRUCT_EXTRACT\(\[It's\], 'it''s'\) = 1") as raised:
        flt(act, df, ("It's", "=", "1"))
    formula = re.search(r"STRUCT_EXTRACT\(.*", str(raised.value)).group()
    assert names(act("rows.filter_formula", df, {"condition": formula})) == ["a"]


MORNING = dt.datetime(2025, 12, 31, 8, 30)
ITEMS = {  # the first row holds the value of the example, if there is one; None: any list or record
    "Texts": pl.Series([["a", "b"], ["c"], None]),
    "Ids": pl.Series([[1, 2], [3], None]),
    "Rates": pl.Series([[1.5], [2.5], None]),
    "Amounts": pl.Series([[D("1.50")], [D("2.00")], None], dtype=pl.List(pl.Decimal(10, 2))),
    "Flags": pl.Series([[True], [False], None]),
    "Days": pl.Series([[dt.date(2025, 12, 31)], [dt.date(2026, 1, 1)], None]),
    "Millis": pl.Series([[MORNING], [MORNING.replace(hour=9)], None], dtype=pl.List(pl.Datetime("ms"))),
    "Stamps": pl.Series([[MORNING], [MORNING.replace(hour=9)], None], dtype=pl.List(pl.Datetime("us"))),
    "Nanos": pl.Series([[MORNING], [MORNING.replace(hour=9)], None], dtype=pl.List(pl.Datetime("ns"))),
    "Zoned": pl.Series([[MORNING], [MORNING], None], dtype=pl.List(pl.Datetime("ns", "Europe/Berlin"))),
    "Starts": pl.Series([[dt.time(8, 30)], [dt.time(9)], None]),
    "Spans": pl.Series([[dt.timedelta(hours=1)], [dt.timedelta(hours=2)], None]),
    "Nanospans": pl.Series([[dt.timedelta(hours=1)], [dt.timedelta(hours=2)], None], dtype=pl.List(pl.Duration("ns"))),
    "Bytes": pl.Series([[b"a"], [b"b"], None]),
    "Nested": pl.Series([[["a"]], [["b"]], None]),
    "Deeper": pl.Series([[[1]], [[2]], None]),
    "Pairs": pl.Series([[{"x": 1, "y": "a"}], [{"x": 2, "y": "b"}], None]),
    "Empties": pl.Series([[None], [None], None], dtype=pl.List(pl.Null)),
    "Sizes": pl.Series([[1, 2], [3, 4], None], dtype=pl.Array(pl.Int64, 2)),
    "Person": pl.Series([{"name": "a", "age": 30}, {"name": "Bo", "age": 40}, None]),
    "Seen": pl.Series([{"at": MORNING}, {"at": MORNING.replace(hour=9)}, None]),
    "Hour": pl.Series([{"t": dt.time(8, 30)}, {"t": dt.time(9)}, None]),
    "Inner": pl.Series([{"l": ["a"]}, {"l": ["b"]}, None]),
    "Quoted": pl.Series([{"it's": 1}, {"it's": 2}, None]),
    "Lasting": pl.Series([{"d": dt.timedelta(hours=1)}, {"d": dt.timedelta(hours=2)}, None]),
    "Blank": pl.Series([{"n": None}, {"n": None}, None], dtype=pl.Struct({"n": pl.Null})),
}


@pytest.mark.parametrize("column", list(ITEMS))
def test_the_formula_of_the_message_runs_for_lists_and_records_of_every_type(act, column):
    """It always proposed LIST_CONTAINS(…, 'a') and STRUCT_EXTRACT(…) = 1: "Could not convert string
    'a' to INT64" for lists of numbers, and the same for lists of lists and records of texts. The
    example's value now has the type of the items (or of the record's first field)."""
    df = pl.DataFrame({"Name": list("abc"), column: ITEMS[column]})
    with pytest.raises(ActionError, match=rf"^Column '{column}' holds (lists|records), which Filter rows") as raised:
        flt(act, df, (column, "contains", "a"))
    formula = re.search(r"e\.g\. (.*)$", str(raised.value)).group(1)
    kept = names(act("rows.filter_formula", df, {"condition": formula}))
    if column == "Zoned":  # the example's time is one of DuckDB's time zone: it runs
        assert kept in ([], ["a"], ["a", "b"]), formula
    elif column == "Empties":  # no value of the type: any list that has items
        assert formula == "LEN([Empties]) > 0" and kept == ["a", "b"]
    elif column == "Blank":  # a record whose first field is never filled
        assert formula == "STRUCT_EXTRACT([Blank], 'n') IS NOT NULL" and kept == []
    else:
        assert kept == ["a"], formula


@pytest.mark.parametrize(
    ("column", "held"), [("Tags", "lists"), ("Sizes", "lists"), ("Pair", "records"), ("Own", "values of a plugin's own type")]
)
def test_filling_lists_and_records_with_a_fixed_value_says_that_a_typed_value_cant_be_used(act, column, held):
    """Before, Polars stopped with "cannot cast List type" or "conversion from `struct[2]` to `str`
    failed", or made every record text ({1,"a"}) without a word."""
    message = rf"^Column '{column}' holds {held}: a typed value can't be used with it$"
    with pytest.raises(ActionError, match=message):
        act("rows.fill", NESTED, {"columns": [column], "method": "value", "value": "x"})
    full = pl.DataFrame({column: NESTED[column].head(1).append(NESTED[column].tail(1))})  # no empty field in a record
    with pytest.raises(ActionError, match=message):
        act("rows.fill", full, {"columns": [column], "method": "value", "value": "x"})


def test_filling_lists_and_records_down_and_up_works(act):
    for column in ("Tags", "Sizes", "Pair", "Own"):
        first, second, _ = NESTED[column].to_list()
        filled = act("rows.fill", NESTED, {"columns": [column], "method": "down"})
        assert filled.schema[column] == NESTED.schema[column] and filled[column].to_list() == [first, second, second]
        assert act("rows.fill", NESTED, {"columns": [column], "method": "up"})[column].to_list() == [first, second, None]


def test_filling_a_column_of_time_spans_says_that_a_typed_value_cant_be_used(act):
    df = pl.DataFrame({"V": [dt.timedelta(hours=1), None]})
    with pytest.raises(ActionError, match="Column 'V' holds time spans: a typed value can't be used with it"):
        act("rows.fill", df, {"columns": ["V"], "method": "value", "value": "1"})


BINARY = pl.DataFrame({"Name": list("abcde"), "V": [b"abc", b"\x00\xff", None, b"", b"0x41"]})


@pytest.mark.parametrize(
    "operator, value, expected",
    [
        ("=", "abc", ["a"]),
        ("=", "", ["d"]),
        ("=", "0x41", ["e"]),  # the text itself, not the bytes it would be as hex digits
        ("!=", "abc", ["b", "c", "d", "e"]),
        (">", "a", ["a"]),
        (">=", "abc", ["a"]),
        ("<", "a", ["b", "d", "e"]),
        ("<=", "", ["d"]),
        ("in", "abc; 0x41", ["a", "e"]),
        ("not_in", "abc; 0x41", ["b", "c", "d"]),
        ("!=", "\ud800", ["a", "b", "c", "d", "e"]),  # half a character: a project file can hold it
    ],
)
def test_binary_data_is_compared_with_the_bytes_of_a_typed_value(act, operator, value, expected):
    assert names(flt(act, BINARY, ("V", operator, value))) == expected


def test_binary_data_can_be_filled_with_the_bytes_of_a_typed_value(act):
    filled = act("rows.fill", BINARY, {"columns": ["V"], "method": "value", "value": "x"})
    assert filled.schema["V"] == pl.Binary
    assert filled["V"].to_list() == [b"abc", b"\x00\xff", b"x", b"", b"0x41"]


@pytest.mark.parametrize(
    "operator, value, expected",
    [
        ("contains", "ff", ["b"]),
        ("contains", "abc", []),  # b"abc" is 0x616263
        ("contains", "0x41", []),  # the text 0x41 is 0x30783431
        ("not_contains", "FF", ["a", "c", "d", "e"]),
        ("starts", "0X00", ["b"]),
        ("ends", "0x", ["d"]),  # no bytes
    ],
)
def test_binary_data_with_bytes_that_are_no_text_is_searched_in_its_hex_digits(act, operator, value, expected):
    """As Join texts writes such a column (0x00FF). Before, Polars stopped with "invalid utf8"."""
    assert names(flt(act, BINARY, ("V", operator, value))) == expected
    assert names(flt(act, BINARY, ("V", "contains", "ff"), case_sensitive=True)) == []
    text = pl.DataFrame({"Name": ["a", "b", "c"], "V": [b"abc", "ä".encode(), None]})  # bytes that are all text stay text
    assert names(flt(act, text, ("V", "contains", "Ä"))) == ["b"]


def test_the_text_of_a_decimal_number_column_is_written_once(act, monkeypatch):
    """For contains, starts with and ends with: inside a filter, Polars ran the Python that writes
    it once for each thread and condition, so that 500 columns took 3 s (0.6.3: 0.2 s), and a
    column of two conditions took twice the time. Float32 values keep their own digits (0.1)."""
    calls = []
    written = dtypes._float_texts
    monkeypatch.setattr(dtypes, "_float_texts", lambda s: calls.append(s.len()) or written(s))
    df = pl.DataFrame(
        {
            "Name": list("abcd"),
            "Net": [3.0, 2.5, 1e16, None],
            "Rate": [0.5, 1.0, 17.25, -0.0],
            "Small": pl.Series([0.1, 3.0, None, 2.0], dtype=pl.Float32),
        }
    )
    conditions = [("Net", "contains", "17"), ("Net", "ends", "5"), ("Rate", "starts", "17"), ("Small", "ends", "0.1")]
    assert names(flt(act, df, *conditions, match="any")) == ["a", "b", "c"]
    assert 1 <= len(calls) <= 3  # at most once for each of the 3 columns
    calls.clear()
    assert names(flt(act, df, ("Rate", "=", "0"), ("Rate", "contains", "-"), match="any")) == ["d"]  # -0.0 is 0
    assert len(calls) == 1


_WIDE = """
import datetime as dt
import time

import polars as pl

from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
fill, keep = registry.actions["rows.fill"], registry.actions["rows.filter"]
texts = pl.DataFrame({f"t{i}": ["a", None, " "] for i in range(4001)})
numbers = pl.DataFrame({f"n{i}": [1.5, None, 2.0] for i in range(4001)})
day = dt.datetime(2025, 1, 1, 8, 30)
stamps = pl.DataFrame({f"d{i}": pl.Series([day, None, day + dt.timedelta(milliseconds=1)], dtype=pl.Datetime("ms")) for i in range(4001)})
later = dt.datetime(2025, 1, 1, 8, 30, 0, 500)
decimals = pl.DataFrame({f"f{i}": [17.0, 2.5, 1e16, None] for i in range(4001)})
steps = [
    # the text of decimal numbers: 17, 2.5, 10000000000000000 (Python wrote it in each thread of Polars)
    ("filter decimal texts", keep, decimals, {"conditions": [{"column": c, "operator": "contains", "value": "17"}
     for c in decimals.columns]}, [(17.0,)]),
    ("fill texts", fill, texts, {"columns": texts.columns, "method": "value", "value": "x"}, [("a",), ("x",), ("x",)]),
    ("fill numbers", fill, numbers, {"columns": numbers.columns, "method": "value", "value": "0"}, [(1.5,), (0.0,), (2.0,)]),
    ("fill down", fill, texts, {"columns": texts.columns, "method": "down"}, [("a",), ("a",), ("a",)]),
    ("filter", keep, numbers, {"conditions": [{"column": c, "operator": ">", "value": "1,5"} for c in numbers.columns]}, [(2.0,)]),
    # milliseconds become microseconds for the typed value
    ("fill date-times", fill, stamps, {"columns": stamps.columns, "method": "value", "value": "2025-01-01 08:30:00.0005"},
     [(day,), (later,), (day + dt.timedelta(milliseconds=1),)]),
    ("filter date-times", keep, stamps, {"conditions": [{"column": c, "operator": ">", "value": "2025-01-01 08:30:00.0005"}
     for c in stamps.columns]}, [(day + dt.timedelta(milliseconds=1),)]),
]
for label, action, df, params, rows in steps:
    start = time.perf_counter()
    out = action(df, params, StubContext())
    seconds = time.perf_counter() - start
    assert out.width == 4001 and out.rows() == [row * 4001 for row in rows], (label, out.shape)
    assert seconds < 5, f"{label} took {seconds:.1f} s"  # 30 s and more when every column made the table's schema anew
"""


def test_fill_and_filter_many_columns_quickly():
    """Fill empty values with a fixed value took 10 s for 2,000 text columns and 40 s for 4,000
    (0.6.2: 7 and 29 s), 72 s for 4,000 date-time columns (0.6.2: 55 s), and Filter rows with a
    condition for each of 4,000 columns 10 s, 23 s with a date-time (0.6.2 too): every column made
    the schema of the whole table anew, and read the typed value again. In a process of its own,
    so that a stack overflow of so many columns shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _WIDE], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"

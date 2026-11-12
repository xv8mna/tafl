import datetime as dt
import math
import subprocess
import sys
from decimal import Decimal as D

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.testing import StubContext


def test_rename(act, people):
    out = act("columns.rename", people, {"columns": [{"column": "Name", "new_name": "Customer"}]})
    assert out.columns == ["Customer", "City", "Age", "Score"]
    with pytest.raises(ActionError, match="exist twice: City"):
        act("columns.rename", people, {"columns": [{"column": "Name", "new_name": "City"}]})


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("keep", ["Age", "Name"]), ("remove", ["City", "Score"]), ("front", ["Age", "Name", "City", "Score"])],
)
def test_select_columns(act, people, mode, expected):
    assert act("columns.select", people, {"mode": mode, "columns": ["Age", "Name"]}).columns == expected


def test_removing_missing_and_repeated_columns(act, people):
    with pytest.raises(pl.exceptions.ColumnNotFoundError, match='"Region" not found'):
        act("columns.select", people, {"mode": "remove", "columns": ["Age", "Region"]})
    assert act("columns.select", people, {"mode": "remove", "columns": ["Age", "Age"]}).columns == ["Name", "City", "Score"]


_REMOVE_WIDE = """
import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
df = pl.DataFrame({f"Day {i}": [i, i + 1] for i in range(4001)})
out = registry.actions["columns.select"](df, {"mode": "remove", "columns": df.columns[1:]}, StubContext())
assert out.rows() == [(0,), (1,)] and out.columns == ["Day 0"], out
"""


def test_remove_columns_of_a_wide_table():
    """4,000 columns at once: Polars nests a list of columns given to drop, and so many ended the
    process (a stack overflow), also the app with unsaved edits. In a process of its own, so that
    such an end shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _REMOVE_WIDE], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-500:]}"


def change(act, df, column, type_, fmt="", ctx=None, **options):
    return act(
        "columns.change_type", df, {"columns": [{"column": column, "type": type_, "format": fmt}], **options}, ctx
    )[column]


def test_text_to_numbers_and_dates(act):
    df = pl.DataFrame({"n": ["1.234,5", "7", "x"], "d": ["31.12.2025", "1.1.2025", "soon"], "b": ["ja", "nein", "?"]})
    ctx = StubContext()
    assert change(act, df, "n", "number", ctx=ctx).to_list() == [1234.5, 7.0, None]
    assert ctx.warnings == ["n: 1 value(s) could not be converted to Decimal number, e.g. 'x' (now empty)"]
    assert change(act, df, "n", "integer").to_list() == [1235, 7, None]  # rounds like Excel
    assert change(act, df, "d", "date").to_list() == [dt.date(2025, 12, 31), dt.date(2025, 1, 1), None]
    assert change(act, df, "d", "date", "d.m.yyyy").to_list()[1] == dt.date(2025, 1, 1)
    assert change(act, df, "b", "boolean").to_list() == [True, False, None]


def test_conversion_errors_can_stop(act):
    with pytest.raises(ActionError, match="could not be converted"):
        change(act, pl.DataFrame({"n": ["x"]}), "n", "number", on_error="fail")


def test_numbers_and_dates_to_text(act):
    df = pl.DataFrame({"f": [1234.567, None], "zip": [1067, 80331], "d": [dt.date(2025, 3, 9), None]})
    assert change(act, df, "f", "text", "#,##0.00").to_list() == ["1.234,57", None]
    assert change(act, df, "f", "text", "0.0", decimal=".").to_list() == ["1234.6", None]
    assert change(act, df, "zip", "text", "00000").to_list() == ["01067", "80331"]
    assert change(act, df, "d", "text").to_list() == ["09.03.2025", None]
    assert change(act, df, "d", "text", "yyyy-mm").to_list() == ["2025-03", None]


def test_same_column_converted_twice(act):
    df = pl.DataFrame({"n": ["1234,5"]})
    params = {"columns": [{"column": "n", "type": "number"}, {"column": "n", "type": "text", "format": "#,##0.00"}]}
    assert act("columns.change_type", df, params)["n"].to_list() == ["1.234,50"]


def test_excel_day_numbers_to_dates(act):
    df = pl.DataFrame({"serial": [45658, 45659]})
    assert change(act, df, "serial", "date").to_list() == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]


def test_numbers_to_dates_with_a_format_of_digits(act):
    """20260131 with yyyymmdd: its digits are read with the format, as the text "20260131" is.
    Before, every number was an Excel day number: 20260131 became a day in the year 57370, without
    a message."""
    df = pl.DataFrame(
        {
            "Day": [20260131, 20251231, None],
            "Excel": [20260131.0, 20251231.0, None],  # as an Excel import gives them
            "Exact": pl.Series([D("20260131.00"), D("20251231"), None], dtype=pl.Decimal(38, 2)),
            "Lost": [1012026, 31122025, None],  # ddmmyyyy: the number lost the leading zero of 01012026
            "When": [20260131083015, 20251231235959, None],
        }
    )
    days = [dt.date(2026, 1, 31), dt.date(2025, 12, 31), None]
    ctx = StubContext()
    assert change(act, df, "Day", "date", "yyyymmdd", ctx=ctx).to_list() == days
    assert change(act, df, "Excel", "date", "yyyymmdd", ctx=ctx).to_list() == days
    assert change(act, df, "Exact", "date", "YYYYMMDD", ctx=ctx).to_list() == days
    assert change(act, df, "Lost", "date", "ddmmyyyy", ctx=ctx).to_list() == [dt.date(2026, 1, 1), dt.date(2025, 12, 31), None]
    assert change(act, df, "Day", "datetime", "yyyymmdd", ctx=ctx).to_list() == [dt.datetime(2026, 1, 31), dt.datetime(2025, 12, 31), None]
    moments = [dt.datetime(2026, 1, 31, 8, 30, 15), dt.datetime(2025, 12, 31, 23, 59, 59), None]
    assert change(act, df, "When", "datetime", "yyyymmddhhmmss", ctx=ctx).to_list() == moments
    assert ctx.warnings == []

    ctx = StubContext()  # decimal places don't fit a format; a four-digit year first can't have lost a zero
    odd = pl.DataFrame({"Day": [20260131.5, 990131.0, 20260131.0]})
    assert change(act, odd, "Day", "date", "yyyymmdd", ctx=ctx).to_list() == [None, None, dt.date(2026, 1, 31)]
    assert ctx.warnings == ["Day: 2 value(s) could not be converted to Date, e.g. '20260131.5', '990131.0' (now empty)"]
    serials = pl.DataFrame({"Day": [45658, 45659]})  # a format with separators: Excel day numbers, as before
    assert change(act, serials, "Day", "date", "dd.mm.yyyy").to_list() == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]


def test_texts_need_all_the_digits_of_a_format_of_digits(act):
    """A text can't have lost leading zeros: one with fewer or more digits than such a format is no
    date of it. Before, "1012026" with ddmmyyyy became 10.12.0026 and "10126" with ddmmyy
    10.12.2006, without a warning."""
    df = pl.DataFrame(
        {
            "Day": ["1012026", "01012026", " 31122025 ", "010120260", None],
            "Iso": ["20260131", "2026131", "202601311", "20261231", None],
            "Short": ["311225", "10126", "0101260", "010126", None],
            "When": ["20260131083015", "2026013183015", "202601310830150", "20251231235959", None],
        }
    )
    for column, fmt, expected, wrong in (
        ("Day", "ddmmyyyy", [None, dt.date(2026, 1, 1), dt.date(2025, 12, 31), None, None], "'1012026', '010120260'"),
        ("Iso", "yyyymmdd", [dt.date(2026, 1, 31), None, None, dt.date(2026, 12, 31), None], "'2026131', '202601311'"),
        ("Short", "ddmmyy", [dt.date(2025, 12, 31), None, None, dt.date(2026, 1, 1), None], "'10126', '0101260'"),
    ):
        ctx = StubContext()
        assert change(act, df, column, "date", fmt, ctx=ctx).to_list() == expected
        assert ctx.warnings == [f"{column}: 2 value(s) could not be converted to Date, e.g. {wrong} (now empty)"]
    ctx = StubContext()
    moments = [dt.datetime(2026, 1, 31, 8, 30, 15), None, None, dt.datetime(2025, 12, 31, 23, 59, 59), None]
    assert change(act, df, "When", "datetime", "yyyymmddhhmmss", ctx=ctx).to_list() == moments
    assert ctx.warnings == ["When: 2 value(s) could not be converted to Date and time, e.g. '2026013183015', '202601310830150' (now empty)"]
    separators = pl.DataFrame({"d": ["1.1.2026", "31.12.2025"]})  # a format with separators: as before
    assert change(act, separators, "d", "date", "dd.mm.yyyy").to_list() == [dt.date(2026, 1, 1), dt.date(2025, 12, 31)]


@pytest.mark.parametrize("fmt", ["dmyyyy", "dmyy", "yyyymd", "yyyymmddhmm"])
@pytest.mark.parametrize("value", [1012026, "1012026"])
def test_a_format_of_digits_needs_dd_and_mm(act, fmt, value):
    """Without separators, a day or month of one or two digits can't be told from the next part.
    Before, 1012026 with dmyyyy became 10.12.0026 without a message, a number and a text alike."""
    with pytest.raises(ActionError) as raised:
        change(act, pl.DataFrame({"v": [value]}), "v", "datetime" if "h" in fmt else "date", fmt)
    assert str(raised.value).startswith(f"The date format “{fmt}” has no separators") and "use dd and mm" in str(raised.value)


def test_d_and_m_with_separators_stay(act):
    assert change(act, pl.DataFrame({"v": ["1.1.2026", "31.12.2025"]}), "v", "date", "d.m.yyyy").to_list() == [
        dt.date(2026, 1, 1),
        dt.date(2025, 12, 31),
    ]


def test_numbers_beyond_excel_days_become_empty_with_a_warning(act):
    """Before, 20250101 became a day in the year 57342 without a warning (an export to Excel then
    stopped), and very big numbers, NaN and infinity stopped with "conversion from f64 to i64
    failed", also with "become empty"."""
    df = pl.DataFrame({"Day": [45658, 20250101, 2958465, None], "Value": [45658.5, float("nan"), float("inf"), 1e300]})
    ctx = StubContext()
    assert change(act, df, "Day", "date", ctx=ctx).to_list() == [dt.date(2025, 1, 1), None, dt.date(9999, 12, 31), None]
    hint = (
        ". Numbers are Excel day numbers, up to 2,958,465 (31.12.9999): for a number such as 20260131, give the format "
        "yyyymmdd"
    )
    assert ctx.warnings == [f"Day: 1 value(s) could not be converted to Date, e.g. '20250101' (now empty){hint}"]
    ctx = StubContext()
    assert change(act, df, "Value", "datetime", ctx=ctx).to_list() == [dt.datetime(2025, 1, 1, 12), None, None, None]
    assert ctx.warnings == [f"Value: 3 value(s) could not be converted to Date and time, e.g. 'nan', 'inf', '1e+300' (now empty){hint}"]
    ctx = StubContext()  # NaN and infinity alone are no dates written as digits
    assert change(act, df.head(3), "Value", "date", ctx=ctx).to_list() == [dt.date(2025, 1, 1), None, None]
    assert ctx.warnings == ["Value: 2 value(s) could not be converted to Date, e.g. 'nan', 'inf' (now empty)"]
    with pytest.raises(ActionError) as raised:
        change(act, df, "Day", "date", on_error="fail")
    assert str(raised.value) == f"Day: 1 value(s) could not be converted to Date, e.g. '20250101'{hint}"


def test_columns_that_cant_become_numbers_or_dates_are_named_in_plain_words(act):
    """Before: "W: a Datetime(time_unit='us', time_zone=None) column can't become a number"."""
    df = pl.DataFrame({"W": [dt.datetime(2025, 1, 2, 8, 30)], "T": [dt.time(8, 30)], "O": pl.Series([object()], dtype=pl.Object)})
    for column, target, message in (
        ("W", "integer", "W: a date-time column can't become a number"),
        ("W", "number", "W: a date-time column can't become a number"),
        ("T", "date", "T: a time column can't become a date"),
        ("O", "number", "O: an object column can't become a number"),
        ("T", "boolean", "T: a time column can't become true/false"),
    ):
        with pytest.raises(ActionError) as raised:
            change(act, df, column, target)
        assert str(raised.value) == message


def test_long_numbers_keep_all_their_digits(act):
    df = pl.DataFrame(
        {
            "Code": ["1234567890123456789", "1111111111111111111"],
            "Big": ["9223372036854775807", "-9223372036854775808"],
            "ID": ["98765432109876543210", "7"],
        }
    )
    assert change(act, df, "Code", "integer").to_list() == [1234567890123456789, 1111111111111111111]
    assert change(act, df, "Big", "integer").to_list() == [2**63 - 1, -(2**63)]
    ctx = StubContext()
    assert change(act, df, "ID", "integer", ctx=ctx).to_list() == [None, 7]
    assert ctx.warnings == [
        "ID: 1 value(s) could not be converted to Whole number, e.g. '98765432109876543210' (now empty). "
        "Whole numbers go up to 9,223,372,036,854,775,807: keep longer IDs as text"
    ]
    with pytest.raises(ActionError, match="keep longer IDs as text"):
        change(act, df, "ID", "integer", on_error="fail")
    numbers = pl.DataFrame({"n": [2**63 - 1, 2**53 + 1], "f": [2.5, -2.5]})
    assert change(act, numbers, "n", "integer").to_list() == [2**63 - 1, 2**53 + 1]
    assert change(act, numbers, "f", "integer").to_list() == [3, -3]
    assert change(act, numbers, "n", "text", "0").to_list() == ["9223372036854775807", "9007199254740993"]


def test_warnings_give_different_examples(act):
    """Before, a value could be an example twice: "e.g. '1.5', '1.5', '2.25'"."""
    ctx = StubContext()
    change(act, pl.DataFrame({"v": [1.5, 1.5, 2.25]}), "v", "integer", ctx=ctx)
    assert ctx.warnings == ["v: 3 value(s) were rounded, e.g. '1.5', '2.25': a whole number has no decimal places (Decimal number and Text keep them)"]
    ctx = StubContext()
    change(act, pl.DataFrame({"v": ["x", "x", "y", "x", "z", "w"]}), "v", "number", ctx=ctx)
    assert ctx.warnings == ["v: 6 value(s) could not be converted to Decimal number, e.g. 'x', 'y', 'z' (now empty)"]
    ctx = StubContext()
    change(act, pl.DataFrame({"v": ["12345678901234567890", "12345678901234567890", "1"]}), "v", "number", ctx=ctx)
    assert ctx.warnings == [
        "v: 2 value(s) were rounded, e.g. '12345678901234567890': a decimal number keeps about 15 digits (Text keeps them all)"
    ]


def test_whole_numbers_from_text_read_each_text_once(act, monkeypatch):
    """Reading the number of a text is the slow part: the rounding warning read every text a
    second time (1.5 to 1.7 times as slow as 0.6.3 for a million texts)."""
    from tafl.api import dtypes

    readings = []
    reading = dtypes._number_text
    monkeypatch.setattr(dtypes, "_number_text", lambda *args: readings.append(args) or reading(*args))
    ctx = StubContext()
    assert change(act, pl.DataFrame({"n": ["1.000,5", "7", "12.345"]}), "n", "integer", ctx=ctx).to_list() == [1001, 7, 12345]
    assert len(readings) == 1 and ctx.warnings == [
        "n: 1 value(s) were rounded, e.g. '1.000,5': a whole number has no decimal places (Decimal number and Text keep them)"
    ]


def test_whole_numbers_say_which_values_were_rounded(act):
    """Values with decimal places are rounded half away from zero, as in Excel, and now the step
    says so, as Decimal number does for the digits it can't keep. Before, there was no warning."""
    df = pl.DataFrame(
        {
            "Text": ["1.000,5", "7", "2,0", "-2,5", "x", None, "1,5e0"],
            "Float": [1000.5, 7.0, 2.0, -2.5, float("nan"), None, 0.25],
            "Exact": pl.Series([D("1000.50"), D("7"), D("2.00"), D("-2.50"), None, None, D("0.25")], dtype=pl.Decimal(10, 2)),
            "Whole": pl.Series([1, 2, 3, 4, 5, None, 7], dtype=pl.Int32),
        }
    )
    why = ": a whole number has no decimal places (Decimal number and Text keep them)"
    ctx = StubContext()
    assert change(act, df, "Text", "integer", ctx=ctx).to_list() == [1001, 7, 2, -3, None, None, 2]
    assert ctx.warnings == [
        "Text: 1 value(s) could not be converted to Whole number, e.g. 'x' (now empty)",
        f"Text: 3 value(s) were rounded, e.g. '1.000,5', '-2,5', '1,5e0'{why}",
    ]
    ctx = StubContext()
    assert change(act, df, "Float", "integer", ctx=ctx).to_list() == [1001, 7, 2, -3, None, None, 0]
    assert ctx.warnings[1:] == [f"Float: 3 value(s) were rounded, e.g. '1000.5', '-2.5', '0.25'{why}"]  # after the one of NaN
    ctx = StubContext()
    assert change(act, df, "Exact", "integer", ctx=ctx).to_list() == [1001, 7, 2, -3, None, None, 0]
    assert ctx.warnings == [f"Exact: 3 value(s) were rounded, e.g. '1000.50', '-2.50', '0.25'{why}"]
    ctx = StubContext()
    assert change(act, df, "Whole", "integer", ctx=ctx).to_list() == [1, 2, 3, 4, 5, None, 7]
    assert change(act, pl.DataFrame({"Text": ["1,234.5", "2.0"]}), "Text", "integer", ctx=ctx, decimal=".").to_list() == [1235, 2]
    assert ctx.warnings == [f"Text: 1 value(s) were rounded, e.g. '1,234.5'{why}"]


def test_long_numbers_to_decimal_numbers_warn(act):
    df = pl.DataFrame({"Code": ["1234567890123456789", "12,5"]})
    ctx = StubContext()
    assert change(act, df, "Code", "number", ctx=ctx).to_list() == [1.2345678901234568e18, 12.5]
    assert ctx.warnings == [
        "Code: 1 value(s) were rounded, e.g. '1234567890123456789': a decimal number keeps about 15 digits "
        "(Whole number and Text keep them all)"
    ]


@pytest.mark.parametrize(
    ("values", "dtype", "keep"),
    [
        (["9223372036854775807", "-9223372036854775808", "9007199254740993"], pl.String, "Whole number and Text keep them all"),
        (["12345678901234567890", "9007199254740993"], pl.String, "Text keeps them all"),
        (["1" + "0" * 38, "7"], pl.String, "Text keeps them all"),  # 39 digits
        (["-9223372036854775809", "7"], pl.String, "Text keeps them all"),
        ([2**53 + 1, 2**63 - 1], pl.Int64, "Whole number and Text keep them all"),
        ([2**64 - 1, 7], pl.UInt64, "Text keeps them all"),
        ([D(2**53 + 1), D(7)], pl.Decimal(38, 0), "Whole number and Text keep them all"),
        ([D(10**20 + 1), D(7)], pl.Decimal(38, 0), "exact decimals and Text keep them all"),
    ],
)
def test_the_rounding_warning_names_whole_number_only_for_values_it_holds(act, values, dtype, keep):
    """Whole number goes up to 9,223,372,036,854,775,807 and makes a longer value empty, as text,
    as an unsigned number or as an exact decimal. The warning of Decimal number said that Whole
    number keeps all their digits."""
    df = pl.DataFrame({"v": pl.Series(values, dtype=dtype)})
    ctx = StubContext()
    change(act, df, "v", "number", ctx=ctx)
    assert ctx.warnings and ctx.warnings[0].endswith(f": a decimal number keeps about 15 digits ({keep})"), ctx.warnings
    ctx = StubContext()
    holds = change(act, df, "v", "integer", ctx=ctx).null_count() == 0  # what Whole number does with them
    assert holds == keep.startswith("Whole number"), ctx.warnings


def test_numbers_with_an_exponent_keep_their_digits_or_say_so(act):
    """An exponent moves the digits: 4503599627370497e1 became 45035996273704968 and 1e-400 0
    without a warning (a text of up to 15 characters wasn't looked at), and 1e400 became ∞. Now the
    rounded ones say so, and one beyond decimal numbers can't be converted. The text inf stays ∞."""
    tiny = "1e-" + "9" * 20  # an exponent beyond those of exact decimals too
    df = pl.DataFrame({"T": ["4503599627370497e1", "1,5e3", "1e400", "-1e400", "1e-400", tiny, "inf", None]})
    ctx = StubContext()
    assert change(act, df, "T", "number", ctx=ctx).to_list() == [45035996273704968.0, 1500.0, None, None, 0.0, 0.0, math.inf, None]
    assert ctx.warnings == [
        "T: 2 value(s) could not be converted to Decimal number, e.g. '1e400', '-1e400' (now empty)",
        f"T: 3 value(s) were rounded, e.g. '4503599627370497e1', '1e-400', '{tiny}': a decimal number keeps about 15 "
        "digits (Text keeps them all)",
    ]
    with pytest.raises(ActionError, match=r"^T: 2 value\(s\) could not be converted to Decimal number, e\.g\. '1e400', '-1e400'$"):
        change(act, df, "T", "number", on_error="fail")
    ctx = StubContext()  # Whole number reads a text with an exponent only up to 2^53: it can't keep this one
    change(act, pl.DataFrame({"T": ["4503599627370497e1", "1e15"]}), "T", "number", ctx=ctx)
    assert ctx.warnings == [
        "T: 1 value(s) were rounded, e.g. '4503599627370497e1': a decimal number keeps about 15 digits (Text keeps them all)"
    ]
    ctx = StubContext()  # a whole number of 1e-400 is 0: it was rounded
    whole = pl.DataFrame({"T": ["1e-400", "1,5e3", "1,2345e2", tiny, "-1,5e-1", "0,0e-5", "4500e-2", "0,05e1", "1,50e1"]})
    assert change(act, whole, "T", "integer", ctx=ctx).to_list() == [0, 1500, 123, 0, 0, 0, 45, 1, 15]
    assert ctx.warnings == [
        f"T: 5 value(s) were rounded, e.g. '1e-400', '1,2345e2', '{tiny}': a whole number has no decimal places "
        "(Decimal number and Text keep them)"
    ]


def test_decimals_with_more_than_15_digits_to_decimal_numbers_warn(act):
    """Only the whole part of a text counted: 12345678901234,5678 and …5677 both became
    12345678901234.568 without a warning. A whole number keeps no decimal places, so only Text is
    named for them. 16 digits that a decimal number keeps give no warning."""
    df = pl.DataFrame({"Amount": ["12345678901234,5678", "12345678901234,5677", "1,5", "0,1234567890123456", None]})
    ctx = StubContext()
    assert change(act, df, "Amount", "number", ctx=ctx).to_list() == [12345678901234.568, 12345678901234.568, 1.5, 0.1234567890123456, None]
    assert ctx.warnings == [
        "Amount: 2 value(s) were rounded, e.g. '12345678901234,5678', '12345678901234,5677': a decimal number keeps "
        "about 15 digits (Text keeps them all)"
    ]
    ctx = StubContext()
    change(act, pl.DataFrame({"Amount": ["12,345,678,901,234.5678", "1,000"]}), "Amount", "number", ctx=ctx, decimal=".")
    assert ctx.warnings == [
        "Amount: 1 value(s) were rounded, e.g. '12,345,678,901,234.5678': a decimal number keeps about 15 digits (Text keeps them all)"
    ]


def test_exact_decimals_with_many_decimal_places_to_decimal_numbers_warn_only_for_lost_digits(act):
    """1.1 of a decimal(38, 18) counted as rounded: the decimal number 1.1, made exact again, is
    1.100000000000000128. Values near the 38 digits don't stop the step."""
    rates = [D("1.1"), D("0.123456789012345678"), D("0.25"), None]
    df = pl.DataFrame({"Rate": pl.Series(rates, dtype=pl.Decimal(38, 18))})
    ctx = StubContext()
    assert change(act, df, "Rate", "number", ctx=ctx).to_list() == [1.1, 0.12345678901234568, 0.25, None]
    assert ctx.warnings == [
        "Rate: 1 value(s) were rounded, e.g. '0.123456789012345678': a decimal number keeps about 15 digits "
        "(exact decimals and Text keep them all)"
    ]
    ctx = StubContext()
    change(act, pl.DataFrame({"Rate": pl.Series([D("1.1"), D("0.5")], dtype=pl.Decimal(38, 18))}), "Rate", "number", ctx=ctx)
    assert ctx.warnings == []
    big = pl.DataFrame({"P": pl.Series([D("-9999999999999999999999999999.9999999999"), D("2")], dtype=pl.Decimal(38, 10))})
    ctx = StubContext()
    assert change(act, big, "P", "number", ctx=ctx).to_list() == [-1e28, 2.0]
    assert ctx.warnings == [
        "P: 1 value(s) were rounded, e.g. '-9999999999999999999999999999.9999999999': a decimal number keeps about "
        "15 digits (exact decimals and Text keep them all)"
    ]


def test_two_digit_years_without_a_format(act):
    df = pl.DataFrame({"Short": ["31.12.25", "01.01.26", "29.02.24", "01.01.30"]})
    expected = [dt.date(2025, 12, 31), dt.date(2026, 1, 1), dt.date(2024, 2, 29), dt.date(1930, 1, 1)]
    assert change(act, df, "Short", "date").to_list() == expected
    assert change(act, df, "Short", "date", "dd.mm.yy").to_list() == expected
    assert change(act, df, "Short", "datetime").to_list()[0] == dt.datetime(2025, 12, 31)


def test_tiny_numbers_to_text_without_a_format_keep_their_digits(act):
    """Without a format, 1e-12 became the text 0, and 6e-11 the text 0,0000000001."""
    df = pl.DataFrame({"v": [1e-12, 6e-11, 1234.5, 2 / 3, None]})
    assert change(act, df, "v", "text").to_list() == ["0,000000000001", "0,00000000006", "1234,5", "0,6666666667", None]
    assert change(act, df, "v", "text", decimal=".").to_list()[:2] == ["0.000000000001", "0.00000000006"]


_TEXT_WIDE = """
import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
df = pl.DataFrame({f"v{i}": [1e-12, 1.5] for i in range(4001)})
out = registry.actions["columns.change_type"](df, {"columns": [{"column": c, "type": "text", "format": ""} for c in df.columns]}, StubContext())
assert out.row(0) == ("0,000000000001",) * 4001 and out.row(1) == ("1,5",) * 4001, out
"""


def test_tiny_numbers_of_a_wide_table_to_text():
    """4,001 decimal-number columns to text, each on its own (in a process of its own, so that an
    end of the process shows as a failed test)."""
    done = subprocess.run([sys.executable, "-c", _TEXT_WIDE], capture_output=True, encoding="utf-8", errors="replace", timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


def test_numbers_to_text_with_percent_and_text(act):
    df = pl.DataFrame({"Rate": [0.05, 0.1], "Price": [1234.5, -2.0]})
    assert change(act, df, "Rate", "text", "0%").to_list() == ["5%", "10%"]
    assert change(act, df, "Price", "text", "#,##0.00 €").to_list() == ["1.234,50 €", "-2,00 €"]
    assert change(act, df, "Price", "text", '"EUR "0.00', decimal=".").to_list() == ["EUR 1234.50", "-EUR 2.00"]
    with pytest.raises(ActionError, match="exponent"):
        change(act, df, "Price", "text", "0.0E+0")


def test_dates_to_text_with_month_names(act):
    df = pl.DataFrame({"d": [dt.date(2025, 12, 31)]})
    assert change(act, df, "d", "text", "d. mmmm yyyy (dddd)").to_list() == ["31. December 2025 (Wednesday)"]


@pytest.mark.parametrize("name", ["v", "*", "^a.*$"])
def test_time_spans_and_times_to_text_the_same_on_every_pc(act, name):
    """As Pivot and Split column write them. A time span stopped with "casting from Duration to
    String not supported"; a time lost its fractions of a second (08:30:00.500 became 08:30:00,
    like 08:30:00)."""
    df = pl.DataFrame({name: [dt.timedelta(days=1, hours=2, minutes=3), dt.timedelta(seconds=1.5), None], "abc": [1, 2, 3]})
    assert change(act, df, name, "text").to_list() == ["1d 2h 3m", "1s 500ms", None]
    times = pl.DataFrame({name: [dt.time(8, 30, 0, 500000), dt.time(8, 30), None], "abc": [1, 2, 3]})
    assert change(act, times, name, "text").to_list() == ["08:30:00.500", "08:30:00", None]
    assert change(act, df.with_columns(pl.Series(name, [True, False, None])), name, "text").to_list() == ["true", "false", None]


@pytest.mark.parametrize(
    ("values", "label"),
    [([[1, 2], None], "list"), ([{"x": 1, "y": "a"}, None], "record"), (pl.Series([object(), None], dtype=pl.Object), "object")],
)
def test_lists_and_records_cant_become_text(act, values, label):
    """Before: "cannot cast List type", a record as {1,"a"} without the names of its fields, and
    "cannot cast 'Object' type"."""
    with pytest.raises(ActionError) as raised:
        change(act, pl.DataFrame({"Tags": values}), "Tags", "text")
    assert str(raised.value) == f"Tags: the column holds {label} values, which can't become text"


@pytest.mark.parametrize("dtype", [pl.Date, pl.Datetime("us"), pl.Datetime("ns", "Europe/Berlin")], ids=["date", "date-time", "zone"])
@pytest.mark.parametrize("fmt", ["0.00", "#,##0.00", "0%", "0.000", "#,##0.000", "0.000000", ".000", "00000", "#,##0", '"day"', "General"])
def test_dates_to_text_with_a_number_format_stops_the_step(act, dtype, fmt):
    """Every number format stops in the same way. Before, Polars stopped with "cannot format
    timezone-naive Datetime with format '0%.2f'"; 0.000 wrote the milliseconds (0.500), and a format
    without codes gave its own text for every value (00000 for each date). Also when the column
    has no values."""
    days = pl.Series("When", [dt.datetime(2025, 12, 31, 8, 30, 0, 500000), None]).cast(pl.Datetime(dtype.time_unit) if dtype != pl.Date else pl.Date)
    if dtype != pl.Date and dtype.time_zone:
        days = days.dt.replace_time_zone(dtype.time_zone)
    message = f"When: the format '{fmt}' writes no part of a date. Use codes such as dd.mm.yyyy hh:mm:ss"
    for values in (days, days.clear()):
        with pytest.raises(ActionError) as raised:
            change(act, values.to_frame(), "When", "text", fmt)
        assert str(raised.value) == message


@pytest.mark.parametrize("dtype", [pl.Date, pl.Datetime("us")], ids=["date", "date-time"])
def test_dates_to_text_with_a_format_that_polars_cant_write_stops_the_step(act, dtype):
    """Fractions of a second have 3, 6 or 9 digits (.000): ss.00 is a date format that can't be
    written."""
    days = pl.DataFrame({"When": pl.Series([dt.datetime(2025, 12, 31, 8, 30)]).cast(dtype)})
    kind = "dates" if dtype == pl.Date else "date-times"
    with pytest.raises(ActionError) as raised:
        change(act, days, "When", "text", "hh:mm:ss.00")
    assert str(raised.value) == (
        f"When: {kind} can't be written with the format 'hh:mm:ss.00'. Use codes such as dd.mm.yyyy hh:mm:ss, "
        "and .000, .000000 or .000000000 for fractions of a second"
    )
    written = "31.12.2025 00:00:00.000" if dtype == pl.Date else "31.12.2025 08:30:00.000"  # dates at midnight
    assert change(act, days, "When", "text", "dd.mm.yyyy hh:mm:ss.000").to_list() == [written]


def test_date_times_to_text_without_a_format_keep_their_fractions_of_a_second(act):
    """With 3, 6 or 9 digits, as Pivot and Split column write them. Before, the default
    dd.mm.yyyy hh:mm:ss left them out, so that 08:30:00.500 and 08:30:00 gave one text. A format
    that is typed stays as it is."""
    texts = ["2025-12-31 08:30:00", "2025-12-31 08:30:00.5", "2025-12-31 08:30:00.000001", "2025-12-31 08:30:00.000000001", None]
    moments = pl.Series("When", texts).str.to_datetime("%Y-%m-%d %H:%M:%S%.f", time_unit="ns").to_frame()
    expected = ["31.12.2025 08:30:00", "31.12.2025 08:30:00.500", "31.12.2025 08:30:00.000001", "31.12.2025 08:30:00.000000001", None]
    assert change(act, moments, "When", "text").to_list() == expected
    micro = moments.with_columns(pl.col("When").cast(pl.Datetime("us")).dt.replace_time_zone("Europe/Berlin"))
    assert change(act, micro, "When", "text").to_list() == [*expected[:3], expected[0], None]  # 1 ns is cut by the cast to µs
    typed = change(act, moments, "When", "text", "dd.mm.yyyy hh:mm:ss")
    assert typed.to_list() == ["31.12.2025 08:30:00"] * 4 + [None]


def test_dates_to_text_with_a_time_are_written_at_midnight(act):
    """As Excel writes them. Polars stopped with "cannot format Date with format '%d.%m.%Y %H:%M'".
    A format with nothing but a time writes no part of a date: 00:00 for each one."""
    days = pl.DataFrame({"When": [dt.date(2025, 12, 31), None]})
    assert change(act, days, "When", "text", "dd.mm.yyyy hh:mm").to_list() == ["31.12.2025 00:00", None]
    assert change(act, days, "When", "text", "yyyy-mm-dd hh:mm:ss.000 am/pm").to_list() == ["2025-12-31 12:00:00.000 AM", None]
    assert change(act, days.clear(), "When", "text", "dd.mm.yyyy hh:mm").to_list() == []
    for fmt in ("hh:mm", "ss.000", "am/pm"):
        with pytest.raises(ActionError, match=rf"^When: the format '{fmt}' writes no part of a date\. Use codes such as"):
            change(act, days, "When", "text", fmt)
    moments = pl.DataFrame({"When": [dt.datetime(2025, 12, 31, 17, 5, 9, 250000)]})
    assert change(act, moments, "When", "text", "dd.mm.yyyy hh:mm am/pm").to_list() == ["31.12.2025 05:05 PM"]
    assert change(act, moments, "When", "text", "hh:mm:ss.000").to_list() == ["17:05:09.250"]
    zoned = moments.with_columns(pl.col("When").dt.replace_time_zone("Europe/Berlin"))
    assert change(act, zoned, "When", "text", "%Y-%m-%d %H:%M %Z").to_list() == ["2025-12-31 17:05 CET"]
    assert change(act, days, "When", "text", "dddd").to_list() == ["Wednesday", None]  # one part of a date is enough


def test_exact_decimals(act):
    amounts = [D("12345678901234.5678"), D("2.5000"), D("-2.5000"), None]
    rates = [D("0.123456789012345678"), D("1.100000000000000000"), None, None]
    df = pl.DataFrame(
        {"Amount": pl.Series(amounts, dtype=pl.Decimal(18, 4)), "Rate": pl.Series(rates, dtype=pl.Decimal(38, 18))}
    )
    ctx = StubContext()
    assert change(act, df, "Amount", "number", ctx=ctx).to_list() == [12345678901234.568, 2.5, -2.5, None]
    assert ctx.warnings == [
        "Amount: 1 value(s) were rounded, e.g. '12345678901234.5678': a decimal number keeps about 15 digits "
        "(exact decimals and Text keep them all)"
    ]
    assert change(act, df, "Amount", "integer").to_list() == [12345678901235, 3, -3, None]
    assert change(act, df, "Amount", "text").to_list() == ["12345678901234,5678", "2,5", "-2,5", None]
    assert change(act, df, "Rate", "text", decimal=".").to_list() == ["0.123456789012345678", "1.1", None, None]
    assert change(act, df, "Amount", "text", "#,##0.00").to_list() == ["12.345.678.901.234,57", "2,50", "-2,50", None]


def test_exact_decimals_near_their_digits_to_whole_numbers(act):
    """Polars rounded them with their decimal places, where a result can need more than 38 digits:
    -9999999999999999999999999999.9999999999 of a decimal(38, 10), and also -0.5 of a
    decimal(38, 38) or 999999999999999999.5 of a decimal(38, 20), whose whole numbers fit. The step
    stopped with "ComputeError: overflow in decimal rounding", with both choices of Values that
    can't be converted. Now the values that fit are rounded, and the others can't be converted. A
    decimal(6, 2) gave 10000 for 9999.99 before too."""
    values = [D("-9999999999999999999999999999.9999999999"), D("1.5"), D("-2.5"), D("9223372036854775807.4"), D("0.4999999999"), None]
    df = pl.DataFrame({"P": pl.Series(values, dtype=pl.Decimal(38, 10))})
    ctx = StubContext()
    assert change(act, df, "P", "integer", ctx=ctx).to_list() == [None, 2, -3, 2**63 - 1, 0, None]
    assert ctx.warnings == [
        "P: 1 value(s) could not be converted to Whole number, e.g. '-9999999999999999999999999999.9999999999' (now empty). "
        "Whole numbers go up to 9,223,372,036,854,775,807: keep longer IDs as text",
        "P: 4 value(s) were rounded, e.g. '1.5000000000', '-2.5000000000', '9223372036854775807.4000000000': a whole number "
        "has no decimal places (Decimal number and Text keep them)",
    ]
    with pytest.raises(ActionError, match=r"^P: 1 value\(s\) could not be converted to Whole number, e\.g\. '-9{28}\.9{10}'\. Whole"):
        change(act, df, "P", "integer", on_error="fail")
    small = pl.DataFrame({"S": pl.Series([D("9999.99"), D("-9999.50"), D("0.49"), D("-0.50")], dtype=pl.Decimal(6, 2))})
    assert change(act, small, "S", "integer").to_list() == [10000, -10000, 0, -1]
    fine = pl.DataFrame({"F": pl.Series([D("0.5E-37"), D("-0.5"), D("0.9" + "9" * 37)], dtype=pl.Decimal(38, 38))})
    assert change(act, fine, "F", "integer").to_list() == [0, -1, 1]
    long = pl.DataFrame({"L": pl.Series([D("999999999999999999.5"), D("-0.5")], dtype=pl.Decimal(38, 20))})
    assert change(act, long, "L", "integer").to_list() == [10**18, -1]


_CHANGE_WIDE = """
from decimal import Decimal

import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
change = registry.actions["columns.change_type"]
big = Decimal("-9999999999999999999999999999.9999999999")
df = pl.DataFrame({f"P{i}": pl.Series([big, Decimal("1.5")], dtype=pl.Decimal(38, 10)) for i in range(4001)})
kinds = ["integer" if i % 2 else "number" for i in range(4001)]
ctx = StubContext()
out = change(df, {"columns": [{"column": c, "type": kind, "format": ""} for c, kind in zip(df.columns, kinds)]}, ctx)
assert out.width == 4001 and out.row(1) == tuple(2 if kind == "integer" else 1.5 for kind in kinds), out
assert out.row(0) == tuple(None if kind == "integer" else -1e28 for kind in kinds), out
assert len(ctx.warnings) == 2 * 2000 + 2001, ctx.warnings[:3]
"""


def test_exact_decimals_of_a_wide_table_to_numbers():
    """4,001 columns of exact decimals near their 38 digits to whole numbers (every second one)
    and to decimal numbers: each one is rounded and checked on its own. In a process of its own,
    so that a stack overflow of a deep Polars expression shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _CHANGE_WIDE], capture_output=True, encoding="utf-8", errors="replace", timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


def test_exact_decimals_with_38_digits_to_text_with_a_format(act):
    df = pl.DataFrame(
        {
            "P": pl.Series([D("1234567890123456789012345678.0123456789"), None], dtype=pl.Decimal(38, 10)),
            "I": pl.Series([D("99999999999999999999999999999"), D("-" + "1" * 38)], dtype=pl.Decimal(38, 0)),
        }
    )
    assert change(act, df, "P", "text", "0.0000").to_list() == ["1234567890123456789012345678,0123", None]
    assert change(act, df, "I", "text", "#,##0.00").to_list() == [
        "99.999.999.999.999.999.999.999.999.999,00",
        "-11.111.111.111.111.111.111.111.111.111.111.111.111,00",
    ]
    assert change(act, df, "I", "text", "0;(0)").to_list() == ["99999999999999999999999999999", "(" + "1" * 38 + ")"]
    assert change(act, df, "I", "text").to_list() == ["99999999999999999999999999999", "-" + "1" * 38]


def test_date_times_with_two_digit_years_without_a_format(act):
    df = pl.DataFrame({"T": ["31.12.25 08:30", "01.01.45 17:05:09", ""]})
    ctx = StubContext()
    expected = [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(1945, 1, 1, 17, 5, 9), None]
    assert change(act, df, "T", "datetime", ctx=ctx).to_list() == expected
    assert change(act, df, "T", "date", ctx=ctx).to_list() == [dt.date(2025, 12, 31), dt.date(1945, 1, 1), None]
    assert not ctx.warnings


def test_binary_to_text_as_hex(act):
    df = pl.DataFrame({"raw": [b"\x01\x02\xff", b"", None]})
    assert change(act, df, "raw", "text").to_list() == ["0x0102FF", "0x", None]

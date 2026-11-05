import datetime as dt
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


def test_long_numbers_to_decimal_numbers_warn(act):
    df = pl.DataFrame({"Code": ["1234567890123456789", "12,5"]})
    ctx = StubContext()
    assert change(act, df, "Code", "number", ctx=ctx).to_list() == [1.2345678901234568e18, 12.5]
    assert ctx.warnings == [
        "Code: 1 value(s) were rounded, e.g. '1234567890123456789': a decimal number keeps about 15 digits "
        "(Whole number and Text keep them all)"
    ]


def test_two_digit_years_without_a_format(act):
    df = pl.DataFrame({"Short": ["31.12.25", "01.01.26", "29.02.24", "01.01.30"]})
    expected = [dt.date(2025, 12, 31), dt.date(2026, 1, 1), dt.date(2024, 2, 29), dt.date(1930, 1, 1)]
    assert change(act, df, "Short", "date").to_list() == expected
    assert change(act, df, "Short", "date", "dd.mm.yy").to_list() == expected
    assert change(act, df, "Short", "datetime").to_list()[0] == dt.datetime(2025, 12, 31)


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
def test_time_spans_and_times_to_text_as_the_grid_shows_them(act, name):
    """A time span stopped with "casting from Duration to String not supported"; a time lost its
    fractions of a second (08:30:00.500 became 08:30:00, like 08:30:00)."""
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
@pytest.mark.parametrize(
    ("fmt", "message"),
    [
        ("0.00", r"^When: date(-time)?s can't be written with the format '0\.00'\. Use codes such as dd\.mm\.yyyy hh:mm:ss"),
        ("#,##0.00", r"can't be written with the format '#,##0\.00'"),
        ("0%", r"can't be written with the format '0%'"),
        ("hh:mm:ss.00", r"can't be written with the format 'hh:mm:ss\.00'.*\.000, \.000000 or \.000000000 for fractions"),
        ("00000", r"^When: the format '00000' writes no part of a date\. Use codes such as dd\.mm\.yyyy hh:mm:ss$"),
        ("#,##0", r"^When: the format '#,##0' writes no part of a date"),
        ('"day"', r"writes no part of a date"),
    ],
    ids=["0.00", "#,##0.00", "0%", "ss.00", "00000", "#,##0", "quoted text"],
)
def test_dates_to_text_with_a_format_without_date_codes_stops_the_step(act, dtype, fmt, message):
    """A number format for dates: Polars stopped with "cannot format timezone-naive Datetime with
    format '0%.2f'", and a format without codes gave its own text for every value (00000 for each
    date). Also when the column has no values."""
    days = pl.Series("When", [dt.datetime(2025, 12, 31, 8, 30), None]).cast(pl.Datetime(dtype.time_unit) if dtype != pl.Date else pl.Date)
    if dtype != pl.Date and dtype.time_zone:
        days = days.dt.replace_time_zone(dtype.time_zone)
    for values in (days, days.clear()):
        with pytest.raises(ActionError, match=message):
            change(act, values.to_frame(), "When", "text", fmt)


def test_date_times_to_text_without_a_format_keep_their_fractions_of_a_second(act):
    """With as many digits as the grid shows. Before, the default dd.mm.yyyy hh:mm:ss left them
    out, so that 08:30:00.500 and 08:30:00 gave one text. A format that is typed stays as it is."""
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
        "(Whole number and Text keep them all)"
    ]
    assert change(act, df, "Amount", "integer").to_list() == [12345678901235, 3, -3, None]
    assert change(act, df, "Amount", "text").to_list() == ["12345678901234,5678", "2,5", "-2,5", None]
    assert change(act, df, "Rate", "text", decimal=".").to_list() == ["0.123456789012345678", "1.1", None, None]
    assert change(act, df, "Amount", "text", "#,##0.00").to_list() == ["12.345.678.901.234,57", "2,50", "-2,50", None]


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

import datetime as dt
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

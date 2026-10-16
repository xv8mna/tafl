import datetime as dt

import polars as pl
import pytest

from tafl.api import dtypes


@pytest.mark.parametrize(
    ("excel", "strftime"),
    [
        ("dd.mm.yyyy", "%d.%m.%Y"),
        ("yyyy-mm-dd hh:mm:ss", "%Y-%m-%d %H:%M:%S"),
        ("dd.mm.yy hh:mm", "%d.%m.%y %H:%M"),
        ("d.m.yyyy", "%-d.%-m.%Y"),
        ("mmmm yyyy", "%B %Y"),
        ("hh:mm \"Uhr\"", "%H:%M Uhr"),
        ("hh:mm:ss.000", "%H:%M:%S%.3f"),
        ("%d/%m/%Y", "%d/%m/%Y"),
    ],
)
def test_to_strftime(excel, strftime):
    assert dtypes.to_strftime(excel) == strftime


def test_infer_types_german_values():
    df = pl.DataFrame(
        {
            "id": ["1", "2", "3"],
            "zip": ["01067", "10115", "80331"],
            "amount": ["1.234,50", "12,5", "-3"],
            "date": ["31.12.2025", "1.2.2025", None],
            "stamp": ["31.12.2025 23:59", "01.01.2026 00:00", ""],
            "flag": ["true", "FALSE", "true"],
            "text": ["a", "1", "b"],
        }
    )
    out = dtypes.infer_types(df, decimal=",")
    assert out.schema == {
        "id": pl.Int64,
        "zip": pl.String,  # leading zero: stays text
        "amount": pl.Float64,
        "date": pl.Date,
        "stamp": pl.Datetime("us"),
        "flag": pl.Boolean,
        "text": pl.String,
    }
    assert out["amount"].to_list() == [1234.5, 12.5, -3.0]
    assert out["date"].to_list() == [dt.date(2025, 12, 31), dt.date(2025, 2, 1), None]
    assert out["stamp"][2] is None


def test_infer_types_point_decimal_and_iso_dates():
    df = pl.DataFrame({"n": ["1,234.5", "2.25"], "d": ["2025-01-31", "2025-02-01"]})
    out = dtypes.infer_types(df, decimal=".")
    assert out["n"].to_list() == [1234.5, 2.25]
    assert out.schema["d"] == pl.Date


def test_two_digit_year_is_not_read_as_year_25():
    out = dtypes.infer_types(pl.DataFrame({"d": ["01.02.25", "31.12.24"]}))
    assert out["d"].to_list() == [dt.date(2025, 2, 1), dt.date(2024, 12, 31)]


def test_parse_numbers_handles_currency_spaces_and_trailing_minus():
    s = pl.Series(["1.234,56 €", "12,5-", " 7 ", "abc", None])
    assert dtypes.parse_numbers(s, ",").to_list() == [1234.56, -12.5, 7.0, None, None]


@pytest.mark.parametrize(
    ("fmt", "decimal", "expected"),
    [
        ("0.00", ",", ["1234,57", "-0,50", "2,68"]),
        ("#,##0.00", ",", ["1.234,57", "-0,50", "2,68"]),
        ("#,##0.00", ".", ["1,234.57", "-0.50", "2.68"]),
        ("0", ",", ["1235", "-1", "3"]),
        ("", ",", ["1234,567", "-0,5", "2,675"]),
    ],
)
def test_format_numbers(fmt, decimal, expected):
    s = pl.Series([1234.567, -0.5, 2.675])  # 2.675 rounds half up like Excel
    assert dtypes.format_numbers(s, fmt, decimal).to_list() == expected


def test_format_numbers_zero_padding():
    assert dtypes.format_numbers(pl.Series([1067, 80331]), "00000").to_list() == ["01067", "80331"]


def test_excel_serial_numbers():
    out = dtypes.excel_serial_to_datetime(pl.Series([45658.0, 45658.5]))
    assert out.to_list() == [dt.datetime(2025, 1, 1), dt.datetime(2025, 1, 1, 12)]


def test_parse_booleans():
    s = pl.Series(["Ja", "nein", "X", "0", "maybe", None])
    assert dtypes.parse_booleans(s).to_list() == [True, False, True, False, None, None]


def test_normalize_types():
    assert dtypes.normalize(pl.Series([1, 2], dtype=pl.Int32)).dtype == pl.Int64
    assert dtypes.normalize(pl.Series([None, None])).dtype == pl.String
    assert dtypes.normalize(pl.Series(["1.50"]).cast(pl.Decimal(10, 2))).dtype == pl.Float64


def test_whole_numbers_and_midnight_dates():
    df = pl.DataFrame(
        {
            "a": [1.0, 2.0, None],
            "b": [1.0, 2.5, None],
            "c": [dt.datetime(2025, 1, 1), dt.datetime(2025, 1, 2), None],
            "d": [dt.datetime(2025, 1, 1, 8), dt.datetime(2025, 1, 2), None],
        }
    )
    out = dtypes.midnight_to_date(dtypes.whole_numbers_to_int(df))
    assert out.schema == {"a": pl.Int64, "b": pl.Float64, "c": pl.Date, "d": pl.Datetime("us")}

import datetime as dt

import polars as pl
import pytest

from tafl.api import ActionError, dtypes


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


def test_long_whole_numbers_keep_all_digits():
    df = pl.DataFrame(
        {
            "ID": ["98765432109876543210", "98765432109876543211"],
            "Code": ["1234567890123456789", "1111111111111111111"],
            "Big": ["9223372036854775807", "-9223372036854775808"],
            "Over": ["9223372036854775808", "1"],
            "Amount": ["1234567890123456789,5", "1"],
            "Short": ["1234567890123,5", "1"],
        }
    )
    warnings = []
    out = dtypes.infer_types(df, ",", warnings.append)
    assert out.schema == {
        "ID": pl.String,  # a decimal number would make both IDs 98765432109876540000
        "Code": pl.Int64,
        "Big": pl.Int64,
        "Over": pl.String,  # one more than Int64 holds
        "Amount": pl.String,
        "Short": pl.Float64,
    }
    assert out["Code"].to_list() == [1234567890123456789, 1111111111111111111]
    assert out["Big"].to_list() == [2**63 - 1, -(2**63)]
    assert warnings == [
        "ID stays text: its numbers are too long to be kept exactly, e.g. '98765432109876543210'",
        "Over stays text: its numbers are too long to be kept exactly, e.g. '9223372036854775808'",
        "Amount stays text: its numbers are too long to be kept exactly, e.g. '1234567890123456789,5'",
    ]
    assert dtypes.infer_types(df).schema == out.schema  # without anyone to warn


def test_parse_integers_is_exact_and_rounds_like_excel():
    s = pl.Series(
        ["1234567890123456789", "-9223372036854775808", "9223372036854775808", "1.234,5", "2,5", "-2,5", "0,49",
         "12,5-", " 1 000 € ", ",5", "1e5", "7\r", "x", "", None]  # fmt: skip
    )
    assert dtypes.parse_integers(s, ",").to_list() == [
        1234567890123456789, -(2**63), None, 1235, 3, -3, 0, -13, 1000, 1, 100000, 7, None, None, None
    ]  # fmt: skip
    assert dtypes.parse_integers(pl.Series(["1,234,567,890,123,456,789", "2.5"]), ".").to_list() == [1234567890123456789, 3]


def test_float_loses_digits():
    text = pl.Series(["1234567890123456789", "9007199254740993", "9007199254740992", "123456789012345,678", "x", None])
    assert dtypes.float_loses_digits(text, ",").to_list() == [True, True, False, False, False, False]
    numbers = pl.Series([2**53, 2**53 + 1, -5, None])
    assert dtypes.float_loses_digits(numbers).to_list() == [False, True, False, False]
    assert not dtypes.float_loses_digits(pl.Series([1.5])).any()


def test_two_digit_years_like_excel():
    s = pl.Series(["31.12.25", "01.01.26", "29.02.24", "1.1.29", "1.1.30", "31.12.99", "01.01.00"])
    expected = [
        dt.date(2025, 12, 31), dt.date(2026, 1, 1), dt.date(2024, 2, 29), dt.date(2029, 1, 1), dt.date(1930, 1, 1),
        dt.date(1999, 12, 31), dt.date(2000, 1, 1),
    ]  # fmt: skip
    assert dtypes.parse_dates(s).to_list() == expected
    assert dtypes.parse_dates(s, "dd.mm.yy").to_list() == expected
    assert dtypes.parse_datetimes(s).to_list() == [dt.datetime.combine(d, dt.time()) for d in expected]
    assert dtypes.infer_types(pl.DataFrame({"d": s}))["d"].to_list() == expected
    assert dtypes.parse_datetimes(pl.Series(["31.12.45 08:15"]), "dd.mm.yy hh:mm").to_list() == [dt.datetime(1945, 12, 31, 8, 15)]


def test_parse_dates_reads_each_value_in_its_own_format():
    s = pl.Series(["31.12.2025", "2025-12-30", "29.12.25", "2025/12/28", "2025-12-27 10:30", "31.02.2025", "soon", None])
    assert dtypes.parse_dates(s).to_list() == [
        dt.date(2025, 12, 31), dt.date(2025, 12, 30), dt.date(2025, 12, 29), dt.date(2025, 12, 28),
        dt.date(2025, 12, 27), None, None, None,
    ]  # fmt: skip
    assert dtypes.parse_datetimes(s)[4] == dt.datetime(2025, 12, 27, 10, 30)


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        ("0%", ["5%", "13%", "-123450%", "0%"]),
        ("0.0 %", ["5,0 %", "12,5 %", "-123450,0 %", "0,0 %"]),
        ("#,##0.00 €", ["0,05 €", "0,13 €", "-1.234,50 €", "0,00 €"]),
        ('"No. "000', ["No. 000", "No. 000", "-No. 1235", "No. 000"]),
        ("[$€-407] #,##0.00", ["€ 0,05", "€ 0,13", "-€ 1.234,50", "€ 0,00"]),
        ("#,##0.00 €;-#,##0.00 €", ["0,05 €", "0,13 €", "-1.234,50 €", "0,00 €"]),
        ("#,##0.00;[Red](#,##0.00);\"-\"", ["0,05", "0,13", "(1.234,50)", "-"]),
        ("_-* #,##0 €_-", [" 0 € ", " 0 € ", "- 1.235 € ", " 0 € "]),
        ("#,##0.0,", ["0,0", "0,0", "-1,2", "0,0"]),
        ("0 \\k\\g", ["0 kg", "0 kg", "-1235 kg", "0 kg"]),
    ],
)
def test_format_numbers_with_percent_and_text(fmt, expected):
    s = pl.Series([0.05, 0.125, -1234.5, 0.0])
    assert dtypes.format_numbers(s, fmt, ",").to_list() == expected


def test_format_numbers_keeps_whole_numbers_exact():
    s = pl.Series([2**63 - 1, None])
    assert dtypes.format_numbers(s, "#,##0", ".").to_list() == ["9,223,372,036,854,775,807", None]
    assert dtypes.format_numbers(pl.Series([float("inf"), float("nan")]), "0.00").to_list() == ["inf", "nan"]


@pytest.mark.parametrize(
    ("fmt", "message"),
    [
        ("0.00E+00", "exponent"),
        ("abc", "has no digits"),
        ("000-000", "text between its digits"),
        ("#.##0,00", "a comma for thousands and a point for decimals"),
        ("[>100]0", r"\[>100\] is not supported"),
    ],
)
def test_format_numbers_refuses_what_it_cant_write(fmt, message):
    with pytest.raises(ActionError, match=message):
        dtypes.format_numbers(pl.Series([1.5]), fmt)


def test_text_sort_key_sorts_like_excel():
    words = ["Zebra", "apfel", "Äpfel", "Apfel", "Özdemir", "zebra", "Peter", "Straße", "Strasse", "Stras", "été", "Ætna", "ẞ"]
    expected = ["Ætna", "apfel", "Äpfel", "Apfel", "été", "Özdemir", "Peter", "ẞ", "Stras", "Straße", "Strasse", "Zebra", "zebra"]
    df = pl.DataFrame({"w": words})
    assert df.sort(dtypes.text_sort_key(pl.col("w")), maintain_order=True)["w"].to_list() == expected
    assert sorted(words, key=dtypes.text_key) == expected
    keys = df.select(dtypes.text_sort_key(pl.col("w")))["w"].to_list()
    assert keys == [dtypes.text_key(w) for w in words]
    assert dtypes.text_key("é") == dtypes.text_key("é") == "e"  # also with a separate accent

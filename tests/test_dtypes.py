import datetime as dt
import math
import random
import struct
import time
from decimal import Decimal

import polars as pl
import pytest
from polars.testing import assert_series_equal

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


def test_the_common_type_of_columns():
    """The rule of Unpivot and Append for columns of different types (see their tests)."""
    whole, decimal = pl.Series("a", [1, None]), pl.Series("b", [1.5])
    assert dtypes.common_type([whole, decimal]) == dtypes.CommonType(pl.Float64, (True, True))
    empty = pl.Series("^a.*$", [None, None], dtype=pl.String)  # nothing in it: it doesn't count
    assert dtypes.common_type([whole, empty]) == dtypes.CommonType(pl.Int64, (True, False))
    assert dtypes.common_type([decimal, pl.Series("*", [2**53 + 1])]) == dtypes.CommonType(pl.String, (True, True), lost=1)
    assert dtypes.common_type([whole, pl.Series("d", [[1]])]).dtype is None  # a list beside a number has no text
    no_rows = [whole.clear(), pl.Series("e", [], dtype=pl.Date), pl.Series("f", [], dtype=pl.Null)]
    assert dtypes.common_type(no_rows) == dtypes.CommonType(pl.String, (True, True, False))  # the types decide
    assert dtypes.common_type([pl.Series("g", [None])]) == dtypes.CommonType(pl.Null, (False,))
    two, seven = dtypes.to_common_type(pl.lit(2.0), pl.Float64, pl.String), dtypes.to_common_type(pl.lit(7), pl.Int64, pl.Float64)
    assert pl.select(two.alias("a"), seven.alias("b")).row(0) == ("2", 7.0)
    assert dtypes.type_word(pl.Decimal(38, 2)) == "exact decimal" and dtypes.type_word(pl.Datetime("us", "UTC")) == "date-time (UTC)"


def test_parse_booleans():
    s = pl.Series(["Ja", "nein", "X", "0", "maybe", None])
    assert dtypes.parse_booleans(s).to_list() == [True, False, True, False, None, None]


def test_normalize_types():
    assert dtypes.normalize(pl.Series([1, 2], dtype=pl.Int32)).dtype == pl.Int64
    assert dtypes.normalize(pl.Series([None, None])).dtype == pl.String
    assert dtypes.normalize(pl.Series(["1.50"]).cast(pl.Decimal(10, 2))).dtype == pl.Float64


def test_normalize_can_keep_every_digit_of_decimals():
    ids = pl.Series([Decimal("12345678901234567890123"), Decimal("12345678901234567890124")], dtype=pl.Decimal(30, 0))
    amounts = pl.Series([Decimal("12345678901234.5678"), Decimal("1.1000"), None], dtype=pl.Decimal(18, 4))
    counts = pl.Series([Decimal("9223372036854775807"), None], dtype=pl.Decimal(20, 0))
    assert dtypes.normalize(ids, exact=True).equals(ids)  # a decimal number would make both IDs equal
    assert dtypes.normalize(amounts, exact=True).equals(amounts)
    assert dtypes.normalize(counts, exact=True).dtype == pl.Int64
    assert dtypes.normalize(counts, exact=True).to_list() == [2**63 - 1, None]
    assert dtypes.normalize(pl.Series([1, 2], dtype=pl.UInt8), exact=True).dtype == pl.Int64
    assert dtypes.normalize(ids).equals(ids, check_dtypes=True)  # formulas too: whole numbers keep their digits
    assert dtypes.normalize(amounts).dtype == pl.Float64  # formulas: the others as before


@pytest.mark.parametrize("exact", [True, False])
def test_normalize_keeps_every_digit_of_whole_numbers_of_128_bits(exact):
    """In a Parquet file from Polars: beyond Int64 the import stopped with "conversion from `i128`
    to `i64` failed". They follow the rules of decimals: exact decimals, or text beyond 38 digits."""
    nines = 10**38 - 1  # the most an exact decimal holds
    for values, dtype in [([2**64, -5, None], pl.Int128), ([2**64, 5, None], pl.UInt128), ([nines, -nines], pl.Int128)]:
        out = dtypes.normalize(pl.Series("Id", values, dtype=dtype), exact=exact)
        assert out.dtype == pl.Decimal(38, 0) and out.to_list() == [None if v is None else Decimal(v) for v in values]
    for values, dtype in [([2**127 - 1, -(2**127), None], pl.Int128), ([2**128 - 1, 5], pl.UInt128), ([10**38, 5], pl.Int128)]:
        out = dtypes.normalize(pl.Series("Id", values, dtype=dtype), exact=exact)
        assert out.to_list() == [None if v is None else str(v) for v in values]
    assert dtypes.normalize(pl.Series([2**63 - 1, None], dtype=pl.UInt128), exact=exact).dtype == pl.Int64  # as before


def test_decimals_that_lose_digits_as_decimal_numbers():
    s = pl.Series(
        [Decimal("12345678901234.5678"), Decimal("1.1000"), Decimal("2.6750"), Decimal("-0.0001"), None],
        dtype=pl.Decimal(18, 4),
    )
    assert dtypes.float_loses_digits(s).to_list() == [True, False, False, False, False]
    big = pl.Series([Decimal("9" * 38), Decimal("10" + "0" * 20)], dtype=pl.Decimal(38, 0))
    assert dtypes.float_loses_digits(big).to_list() == [True, False]


def test_decimals_to_text_keep_their_digits():
    values = [Decimal("0.123456789012345678"), Decimal("-1.100000000000000000"), Decimal("0")]
    s = pl.Series(values, dtype=pl.Decimal(38, 18))
    assert dtypes.format_numbers(s).to_list() == ["0,123456789012345678", "-1,1", "0"]
    ids = pl.Series([Decimal("12345678901234567890123"), Decimal("100")], dtype=pl.Decimal(30, 0))
    assert dtypes.format_numbers(ids, "", ".").to_list() == ["12345678901234567890123", "100"]
    assert dtypes.format_numbers(ids, "#,##0", ".").to_list() == ["12,345,678,901,234,567,890,123", "100"]


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        (
            "#,##0.00",
            ["99,999,999,999,999,999,999,999,999,999.00", "-12,345,678,901,234,567,890,123,456,789,012,345,678.00"],
        ),
        ("0;(0)", ["99999999999999999999999999999", "(12345678901234567890123456789012345678)"]),
        ("0%", ["9999999999999999999999999999900%", "-1234567890123456789012345678901234567800%"]),
        ("#,##0,", ["100,000,000,000,000,000,000,000,000", "-12,345,678,901,234,567,890,123,456,789,012,346"]),
    ],
)
def test_format_numbers_keeps_all_38_digits_of_decimals(fmt, expected):
    ids = pl.Series([Decimal("9" * 29), Decimal("-12345678901234567890123456789012345678")], dtype=pl.Decimal(38, 0))
    assert dtypes.format_numbers(ids, fmt, ".").to_list() == expected


def test_format_numbers_keeps_all_decimal_places_of_long_decimals():
    s = pl.Series([Decimal("1234567890123456789012345678.0123456789"), Decimal("-0.5")], dtype=pl.Decimal(38, 10))
    assert dtypes.format_numbers(s, "0.0000", ",").to_list() == ["1234567890123456789012345678,0123", "-0,5000"]
    assert dtypes.format_numbers(s, "0.00000000;-0.00000000", ".").to_list() == [
        "1234567890123456789012345678.01234568",
        "-0.50000000",
    ]


@pytest.mark.parametrize(
    ("text", "decimal", "expected"),
    [
        ("12345678901234,5678", ",", Decimal("12345678901234.5678")),
        ("1.234.567.890.123.456.789.012", ",", Decimal("1234567890123456789012")),
        (" 1,10 € ", ",", Decimal("1.10")),
        ("12,5-", ",", Decimal("-12.5")),
        ("1e5", ".", Decimal("100000")),
        ("x", ",", None),
        ("inf", ".", None),
        ("", ",", None),
    ],
)
def test_parse_decimal_is_exact(text, decimal, expected):
    parsed = dtypes.parse_decimal(text, decimal)
    assert parsed == expected and str(parsed) == str(expected)


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
    # Rounded beyond the type: 2^127 - 1 counted as kept, and a UInt128 column stopped with
    # "conversion from `u128` to `i128` failed"
    wide = pl.Series([2**127 - 1, 2**127 - 2**74, -(2**127), None], dtype=pl.Int128)
    assert dtypes.float_loses_digits(wide).to_list() == [True, False, False, False]
    top = pl.Series([2**128 - 1, 2**127, 2**64 - 1, None], dtype=pl.UInt128)
    assert dtypes.float_loses_digits(top).to_list() == [True, False, True, False]


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


def test_date_times_with_nanoseconds():
    s = pl.Series(["2025-12-31 08:30:00.000000001", "2025-12-31T08:30:00.5", "31.12.25 08:30", "2025-12-31", None])
    stamps = dtypes.parse_datetimes(s, time_unit="ns")
    assert stamps.dtype == pl.Datetime("ns")
    assert stamps.dt.to_string("%Y-%m-%d %H:%M:%S%.f").to_list() == [
        "2025-12-31 08:30:00.000000001", "2025-12-31 08:30:00.500", "2025-12-31 08:30:00", "2025-12-31 00:00:00", None
    ]  # fmt: skip
    assert dtypes.parse_datetimes(s).to_list()[:2] == [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 8, 30, 0, 500000)]
    assert dtypes.parse_datetimes(pl.Series(["31.12.2025 08:30"]), "dd.mm.yyyy hh:mm", time_unit="ns").dtype == pl.Datetime("ns")


def test_date_times_with_two_digit_years():
    s = pl.Series(["31.12.25 08:30", "1.1.45 23:59:30", "29.02.24 0:00", "31.12.25 25:00", None])
    expected = [
        dt.datetime(2025, 12, 31, 8, 30), dt.datetime(1945, 1, 1, 23, 59, 30), dt.datetime(2024, 2, 29), None, None
    ]  # fmt: skip
    assert dtypes.parse_datetimes(s).to_list() == expected
    assert dtypes.parse_dates(s).to_list() == [d and d.date() for d in expected]
    stamps = pl.DataFrame(
        {"T": ["31.12.25 08:30", "01.01.30 17:05", ""], "S": ["31.12.99 08:30:15", "1.1.00 0:00:00", None]}
    )
    assert dtypes.infer_types(stamps).to_dict(as_series=False) == {
        "T": [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(1930, 1, 1, 17, 5), None],
        "S": [dt.datetime(1999, 12, 31, 8, 30, 15), dt.datetime(2000, 1, 1), None],
    }


def test_parse_times():
    s = pl.Series(["08:30", "8:30", "08:30:15", " 08:30:15.250 ", "23:59:59.999999", "24:00", "8", "abc", "", None])
    expected = [dt.time(8, 30), dt.time(8, 30), dt.time(8, 30, 15), dt.time(8, 30, 15, 250_000), dt.time(23, 59, 59, 999_999)]
    assert dtypes.parse_times(s).to_list() == [*expected, None, None, None, None, None]


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


def _unicode_keys(s: pl.Series) -> pl.Series:
    """The sort key worked out the plain way, for every text."""
    folded = s.str.normalize("NFKD").str.to_lowercase().str.replace_all(r"\p{M}+", "")
    fold = {"ß": "ss", "æ": "ae", "œ": "oe", "ø": "o", "đ": "d", "ð": "d", "ł": "l", "þ": "th", "ı": "i"}
    return folded.str.replace_many(list(fold), list(fold.values()))


def test_text_sort_key_takes_shortcuts_with_the_same_result():
    chars = pl.Series([chr(c) for c in range(1, 0x30000) if not 0xD800 <= c < 0xE000])  # every character alone
    assert dtypes.text_sort_key(chars).equals(_unicode_keys(chars))
    rng = random.Random(7)
    pool = "aZ 9-Äöüßé́̈ŁłØİıǅﬁ²ΣσςΟΔΟΣ日本😀 "
    texts = ["".join(rng.choice(pool) for _ in range(rng.randint(0, 8))) for _ in range(5000)]
    s = pl.Series([*texts, None, "ΟΔΟΣ", "Ἀθῆναι"])
    assert dtypes.text_sort_key(s).equals(_unicode_keys(s))
    repeated = pl.Series(texts[:100] * 300)  # many repeated values: each one is worked out once
    assert dtypes.text_sort_key(repeated).equals(_unicode_keys(repeated))
    assert pl.DataFrame({"t": repeated}).select(dtypes.text_sort_key(pl.col("t")))["t"].equals(_unicode_keys(repeated))
    assert dtypes.text_sort_key(pl.Series([], dtype=pl.String)).len() == 0


def test_text_sort_key_decomposes_only_what_needs_it(monkeypatch):
    seen = []
    plain = dtypes._unicode_keys
    dtypes._latin_keys()  # the table, worked out once
    monkeypatch.setattr(dtypes, "_unicode_keys", lambda s: seen.append(s.to_list()) or plain(s))
    words = ("Äpfel", "Straße", "élan", "Zebra", "Ölkanne")
    names = pl.Series([f"Kunde {i} {w}" for i in range(30_000) for w in words])  # all different
    repeated = pl.Series(["Äpfel", "Straße", "日本", "Zebra"] * 25_000)
    assert dtypes.text_sort_key(names)[:3].to_list() == ["kunde 0 apfel", "kunde 0 strasse", "kunde 0 elan"]
    assert dtypes.text_sort_key(repeated)[:3].to_list() == ["apfel", "strasse", "日本"]
    assert seen == [["日本"]]  # once, for all its rows


def test_text_sort_key_sorts_like_excel():
    words = ["Zebra", "apfel", "Äpfel", "Apfel", "Özdemir", "zebra", "Peter", "Straße", "Strasse", "Stras", "été", "Ætna", "ẞ"]
    expected = ["Ætna", "apfel", "Äpfel", "Apfel", "été", "Özdemir", "Peter", "ẞ", "Stras", "Straße", "Strasse", "Zebra", "zebra"]
    df = pl.DataFrame({"w": words})
    assert df.sort(dtypes.text_sort_key(pl.col("w")), maintain_order=True)["w"].to_list() == expected
    assert sorted(words, key=dtypes.text_key) == expected
    keys = df.select(dtypes.text_sort_key(pl.col("w")))["w"].to_list()
    assert keys == [dtypes.text_key(w) for w in words]
    assert dtypes.text_sort_key(df["w"]).to_list() == keys
    assert dtypes.text_key("é") == dtypes.text_key("é") == "e"  # also with a separate accent


def _texts(values, dtype=None) -> list:
    s = pl.Series("v", values, dtype=dtype, strict=False)
    return s.to_frame().select(dtypes.text_expr(pl.col("v"), s.dtype))["v"].to_list()


def test_text_expr_writes_values_the_same_on_every_pc():
    assert _texts(["x", " y ", None]) == ["x", " y ", None]
    assert _texts([1234, -5, None]) == ["1234", "-5", None]
    assert _texts([Decimal("5.00"), Decimal("-0.50")], pl.Decimal(10, 2)) == ["5.00", "-0.50"]
    assert _texts([dt.date(2025, 12, 31)]) == ["2025-12-31"]
    moments = [dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 8, 30, 0, 500_000)]
    assert _texts(moments) == ["2025-12-31 08:30:00", "2025-12-31 08:30:00.500"]
    zoned = pl.Series(moments).dt.replace_time_zone("Europe/Berlin")
    assert _texts(zoned.to_list(), zoned.dtype) == ["2025-12-31 08:30:00", "2025-12-31 08:30:00.500"]  # no offset
    assert _texts([dt.time(8, 30), dt.time(8, 30, 0, 500_000)]) == ["08:30:00", "08:30:00.500"]
    assert _texts([dt.timedelta(days=1, hours=2, minutes=3)]) == ["1d 2h 3m"]
    assert _texts([True, False]) == ["true", "false"]
    assert _texts([b"\x01\xff"]) == ["0x01FF"]


def test_bytes_are_text():
    assert dtypes.bytes_are_text(pl.Series([b"abc", "ä".encode(), b"", b"\x00", None]))
    assert dtypes.bytes_are_text(pl.Series([None], dtype=pl.Binary))
    assert not dtypes.bytes_are_text(pl.Series([b"abc", b"\x00\xff"]))
    assert not dtypes.bytes_are_text(pl.Series(["ä".encode()[:1]]))  # half of a character


@pytest.mark.parametrize(
    ("values", "dtype", "label"),
    [
        (["a"], pl.String, "text"),
        ([1], pl.Int64, "integer"),
        ([1.5], pl.Float64, "number"),
        ([dt.datetime(2025, 1, 2)], pl.Datetime("us", "UTC"), "date-time"),
        ([dt.timedelta(1)], pl.Duration("ms"), "duration"),
        ([None], pl.Null, "empty"),
        ([["a", "b"]], pl.List(pl.String), "list"),  # Polars' own words were list(string), ...
        ([[[1]]], pl.List(pl.List(pl.Int64)), "list"),
        ([[1, 2]], pl.Array(pl.Int64, 2), "list"),
        ([{"x": 1, "y": "a"}], pl.Struct({"x": pl.Int64, "y": pl.String}), "record"),
        ([object()], pl.Object, "object"),
    ],
)
def test_type_label_says_list_record_and_object_in_plain_words(values, dtype, label):
    """The word under a column's name in the grid, and in messages such as "“Tags” holds list
    values, which can't be split"."""
    assert dtypes.type_label(pl.Series(values, dtype=dtype).dtype) == label


@pytest.mark.parametrize("dtype", [pl.Categorical, pl.Enum(["a", "b"])], ids=["Categorical", "Enum"])
def test_type_label_calls_categories_text(dtype):
    """The grid shows categories (from Parquet files or plugins) as text. Before: categorical and
    enum(categories=['a', 'b'])."""
    assert dtypes.type_label(pl.Series(["a", None], dtype=dtype).dtype) == "text"


def test_text_expr_writes_decimal_numbers_with_all_their_digits_and_no_exponent():
    values = [2.0, 1.5, -0.0, 0.1 + 0.2, 1 / 3, 1e16, -1e20, 1e-7, 2.5e-8, 123456789012345.0, 2.0**53, 2.0**53 + 2]
    assert _texts(values) == [
        "2", "1.5", "0", "0.30000000000000004", "0.3333333333333333", "10000000000000000", "-100000000000000000000",
        "0.0000001", "0.000000025", "123456789012345", "9007199254740992", "9007199254740994",
    ]
    assert _texts([float("nan"), float("inf"), float("-inf"), None]) == ["NaN", "inf", "-inf", None]
    assert _texts([0.1, 2.0, 1e16], pl.Float32) == ["0.1", "2", "10000000000000000"]  # the digits it shows
    rng = random.Random(7)
    numbers = [rng.gauss(0, 1e6) for _ in range(3000)] + [rng.random() for _ in range(3000)]
    numbers += [10.0 ** rng.uniform(-30, 30) for _ in range(3000)]
    assert [float(text) for text in _texts(numbers)] == numbers  # every value reads back as it was


def test_text_expr_writes_the_digits_of_an_exponent_without_it():
    """The same digits as Polars shows with the exponent (1.5e-7 is 0.00000015), also at the ends
    of the range and for Float32 values."""
    rng = random.Random(3)
    doubles = [struct.unpack("<d", rng.getrandbits(64).to_bytes(8, "little"))[0] for _ in range(20_000)]
    singles = [struct.unpack("<f", rng.getrandbits(32).to_bytes(4, "little"))[0] for _ in range(5_000)]
    for dtype, values in (
        (pl.Float64, [5e-324, -2.2250738585072014e-308, 1.7976931348623157e308, -1.2345678901234568e17, 1e16, *doubles]),
        (pl.Float32, [3.4028234663852886e38, 1e-45, 1.1754943508222875e-38, -2.5e-8, *singles]),
    ):
        numbers = pl.Series([value for value in values if math.isfinite(value)], dtype=dtype)
        with_exponent = numbers.filter(numbers.cast(pl.String).str.contains("e"))
        assert with_exponent.len() > numbers.len() / 2
        expected = [format(Decimal(text), "f") for text in with_exponent.cast(pl.String)]
        assert _texts(with_exponent.to_list(), dtype) == expected


def test_text_expr_of_decimal_numbers_is_quick():
    rng = random.Random(1)
    numbers = pl.Series("v", [rng.random() for _ in range(1_000_000)])
    start = time.perf_counter()
    numbers.to_frame().select(dtypes.text_expr(pl.col("v"), pl.Float64))
    assert time.perf_counter() - start < 1.5  # Polars itself: about 0.1 s


def test_text_expr_of_decimal_numbers_with_an_exponent_is_quick():
    """Below 1e-4 and from 1e16 on, Polars writes most numbers with an exponent."""
    rng = random.Random(2)
    numbers = pl.Series("v", [rng.random() * 1e-5 for _ in range(500_000)] + [rng.uniform(1e16, 1e20) for _ in range(500_000)])
    frame = numbers.to_frame()
    assert numbers.cast(pl.String).str.contains("e").sum() > 990_000

    def best(run) -> float:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            run()
            times.append(time.perf_counter() - start)
        return min(times)

    plain = best(lambda: frame.select(pl.col("v").cast(pl.String)))
    step = best(lambda: frame.select(dtypes.text_expr(pl.col("v"), pl.Float64)))
    texts = frame.select(dtypes.text_expr(pl.col("v"), pl.Float64))["v"]
    assert not texts.str.contains("e").any() and texts.cast(pl.Float64).equals(numbers)
    assert step < 10 * plain + 0.4, (step, plain)


_OF_A_SERIES = [
    pytest.param(dtypes.parse_numbers, ["1.234,5", None, "x"], id="parse_numbers"),
    pytest.param(dtypes.parse_integers, ["1.234", None, "12345678901234567890"], id="parse_integers"),
    pytest.param(dtypes.parse_dates, [" 31.12.2025", None, "x"], id="parse_dates"),
    pytest.param(lambda s: dtypes.parse_datetimes(s, "dd.mm.yyyy hh:mm"), ["31.12.2025 08:30", None], id="parse_datetimes"),
    pytest.param(dtypes.parse_times, ["08:30", None, "x"], id="parse_times"),
    pytest.param(dtypes.parse_booleans, ["ja", "No", None, "x"], id="parse_booleans"),
    pytest.param(dtypes.excel_serial_to_datetime, [45658, None, 45658.5], id="excel_serial_to_datetime"),
    pytest.param(lambda s: dtypes.numbers_to_datetimes(s, "ddmmyyyy"), [31012026.0, None, 1012026.5], id="numbers_to_datetimes"),
    pytest.param(lambda s: dtypes.format_dates(s, "dd.mm.yyyy"), [dt.date(2025, 1, 2), None], id="format_dates"),
    pytest.param(lambda s: dtypes.format_numbers(s, "#,##0.00"), [1234.5, None], id="format_numbers"),
    pytest.param(dtypes.float_loses_digits, ["12345678901234567890", "1", None], id="float_loses_digits of texts"),
    pytest.param(dtypes.float_loses_digits, [2**60 + 1, 1, None], id="float_loses_digits of whole numbers"),
    pytest.param(dtypes.infer_series, ["1", " 2", None], id="infer_series of whole numbers"),
    pytest.param(dtypes.infer_series, ["31.12.2025", None], id="infer_series of dates"),
    pytest.param(dtypes.infer_series, ["true", "False", None], id="infer_series of true/false"),
    pytest.param(dtypes.infer_series, ["0815", None], id="infer_series of codes"),
    pytest.param(lambda s: dtypes.whole_numbers_to_int(s.to_frame()).to_series(), [1.0, None, 3.0], id="whole_numbers_to_int"),
    pytest.param(lambda s: dtypes.midnight_to_date(s.to_frame()).to_series(), [dt.datetime(2025, 1, 2), None], id="midnight_to_date"),
    pytest.param(dtypes.text_sort_key, ["B", "é", None, "a"], id="text_sort_key"),
    pytest.param(dtypes.text_sort_key, ["B", "é", None, "a"] * 6_000, id="text_sort_key of many repeated values"),
]


@pytest.mark.parametrize("name", ["*", "^a.*$", "^x$"])
@pytest.mark.parametrize(("function", "values"), _OF_A_SERIES)
def test_the_functions_of_a_series_work_for_a_name_like_a_pattern(function, values, name):
    """Polars' own Series methods read a name like ^a.*$ as a pattern (one that finds nothing:
    they stop with an IndexError). The result keeps the Series' own name."""
    s = pl.Series("v", values, strict=False)
    assert_series_equal(function(s.alias(name)), function(s).alias(name))


def test_day_numbers_beyond_the_years_1_to_9999_become_empty():
    """Before, 20250101 became a day in the year 57342 (which no export can write), and very big
    numbers, NaN and infinity stopped with "conversion from f64 to i64 failed"."""
    days = [2958465.5, 2958466, -693593, -693593.5, 20250101, 1e300, math.nan, math.inf, -math.inf, None, 1.5]
    out = dtypes.excel_serial_to_datetime(pl.Series("Amount", days))
    assert out.name == "Amount" and out.dtype == pl.Datetime("us")
    first, last = dt.datetime(1, 1, 1), dt.datetime(9999, 12, 31, 12)
    assert out.to_list() == [last, None, first, None, None, None, None, None, None, None, dt.datetime(1899, 12, 31, 12)]
    whole = dtypes.excel_serial_to_datetime(pl.Series([2**63 - 1, 2958465, None], dtype=pl.Int64))
    assert whole.to_list() == [None, dt.datetime(9999, 12, 31), None]

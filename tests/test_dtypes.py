import datetime as dt
import random
from decimal import Decimal

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


def test_normalize_can_keep_every_digit_of_decimals():
    ids = pl.Series([Decimal("12345678901234567890123"), Decimal("12345678901234567890124")], dtype=pl.Decimal(30, 0))
    amounts = pl.Series([Decimal("12345678901234.5678"), Decimal("1.1000"), None], dtype=pl.Decimal(18, 4))
    counts = pl.Series([Decimal("9223372036854775807"), None], dtype=pl.Decimal(20, 0))
    assert dtypes.normalize(ids, exact=True).equals(ids)  # a decimal number would make both IDs equal
    assert dtypes.normalize(amounts, exact=True).equals(amounts)
    assert dtypes.normalize(counts, exact=True).dtype == pl.Int64
    assert dtypes.normalize(counts, exact=True).to_list() == [2**63 - 1, None]
    assert dtypes.normalize(pl.Series([1, 2], dtype=pl.UInt8), exact=True).dtype == pl.Int64
    assert dtypes.normalize(ids).dtype == pl.Float64  # formulas: as before


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

import datetime as dt
import re
import subprocess
import sys
import time
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from tafl.api import ActionError, dtypes
from tafl.api.testing import StubContext

THOUSANDS = "Artikel;Menge;Gewicht;Preis\r\nA;1.000;2.500;3,50\r\nB;12.000;1.250;4,25\r\nC;500;10.000;1,99\r\n"
ONLY_THOUSANDS = "Artikel;Menge\r\nA;1.234\r\nB;2.500\r\nC;750\r\n"
GERMAN = (
    "Kunde;PLZ;Betrag;Datum;Notiz\n"
    'Müller GmbH;01067;1.234,56;31.12.2025;"Text; mit Semikolon"\n'
    "Schröder;80331;-5,5;01.02.2025;\n"
)
PHONES = (
    "Name;Tel;Karte;BLZ;Datum;Bereich;Amt\n"
    "A;+49 30 1234567;4111 1111 1111 1111;370 400 44;2025 01 31;10 20;1 234\n"
    "B;+49 89 7654321;5500 0000 0000 0004;100 500 00;2024 12 24;5 7;2 500\n"
)


def test_import_german_ansi_file(act, tmp_path):
    (tmp_path / "kunden.csv").write_bytes(GERMAN.encode("cp1252"))
    ctx = StubContext(project_dir=tmp_path)
    df = act("csv.import", None, {"file": "kunden.csv"}, ctx)
    assert df.schema == {"Kunde": pl.String, "PLZ": pl.String, "Betrag": pl.Float64, "Datum": pl.Date, "Notiz": pl.String}
    assert df["Kunde"].to_list() == ["Müller GmbH", "Schröder"]
    assert df["PLZ"].to_list() == ["01067", "80331"]
    assert df["Betrag"].to_list() == [1234.56, -5.5]
    assert df["Datum"].to_list() == [dt.date(2025, 12, 31), dt.date(2025, 2, 1)]
    assert df["Notiz"].to_list() == ["Text; mit Semikolon", None]
    assert ctx.messages == [("info", "Encoding cp1252, delimiter semicolon, decimal comma")]


def test_import_utf8_bom_with_comma_delimiter(act, tmp_path):
    (tmp_path / "prices.csv").write_text("id,price,when\n1,2.50,2025-01-31\n2,10,2025-02-01\n", encoding="utf-8-sig")
    df = act("csv.import", None, {"file": str(tmp_path / "prices.csv")})
    assert df.columns == ["id", "price", "when"]
    assert df["price"].to_list() == [2.5, 10.0]
    assert df.schema["when"] == pl.Date


def test_import_without_header_skipping_lines(act, tmp_path):
    (tmp_path / "raw.txt").write_text("Report 2025\nexported by me\n1;a\n2;b\n", encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "raw.txt"), "header": False, "skip_lines": 2})
    assert df.columns == ["Column 1", "Column 2"]
    assert df["Column 1"].to_list() == [1, 2]


def test_import_everything_as_text(act, tmp_path):
    (tmp_path / "t.csv").write_text("a;b\n1;2,5\n", encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "t.csv"), "detect_types": False})
    assert df.schema == {"a": pl.String, "b": pl.String}


def test_blank_and_duplicate_header_names(act, tmp_path):
    (tmp_path / "t.csv").write_text("a;a;;b\n1;2;3;4\n", encoding="utf-8")
    assert act("csv.import", None, {"file": str(tmp_path / "t.csv")}).columns == ["a", "a (2)", "Column 3", "b"]


def test_missing_file(act, tmp_path):
    with pytest.raises(ActionError, match="File not found"):
        act("csv.import", None, {"file": str(tmp_path / "nope.csv")})


def test_export_and_import_round_trip(act, tmp_path):
    df = pl.DataFrame(
        {
            "Name": ["Ä; quoted", None],
            "Amount": [1234.5, -0.25],
            "Day": [dt.date(2025, 12, 31), None],
            "At": [dt.datetime(2025, 1, 2, 3, 4, 5), None],
        }
    )
    path = tmp_path / "sub" / "out.csv"
    act("csv.export", df, {"file": str(path)})
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["Name;Amount;Day;At", '"Ä; quoted";1234,5;31.12.2025;02.01.2025 03:04:05', ";-0,25;;"]
    assert path.read_bytes().startswith(b"\xef\xbb\xbf")
    back = act("csv.import", None, {"file": str(path)})
    assert back["Amount"].to_list() == [1234.5, -0.25]
    assert back["Day"].to_list() == [dt.date(2025, 12, 31), None]
    assert back.schema["At"] == pl.Datetime("us")


def test_export_ansi_warns_about_missing_characters(act, tmp_path):
    ctx = StubContext()
    act("csv.export", pl.DataFrame({"x": ["5 €", "smile 😀"]}), {"file": str(tmp_path / "a.csv"), "encoding": "cp1252"}, ctx)
    assert (tmp_path / "a.csv").read_bytes().decode("cp1252").splitlines() == ["x", "5 €", "smile ?"]
    assert ctx.warnings


def test_export_rejects_comma_for_both_separators(act, tmp_path):
    with pytest.raises(ActionError, match="both be a comma"):
        act("csv.export", pl.DataFrame({"x": [1.5]}), {"file": str(tmp_path / "a.csv"), "delimiter": ","})


def test_thousands_separators_are_not_decimal_points(act, tmp_path):
    (tmp_path / "t.csv").write_bytes(THOUSANDS.encode("cp1252"))
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "t.csv")}, ctx)
    assert df.to_dict(as_series=False) == {
        "Artikel": ["A", "B", "C"],
        "Menge": [1000, 12000, 500],
        "Gewicht": [2500, 1250, 10000],
        "Preis": [3.5, 4.25, 1.99],
    }
    assert df.schema["Menge"] == pl.Int64
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal comma")]
    (tmp_path / "only.csv").write_bytes(ONLY_THOUSANDS.encode("cp1252"))
    assert act("csv.import", None, {"file": str(tmp_path / "only.csv")})["Menge"].to_list() == [1234, 2500, 750]


def test_values_like_1_000_follow_the_other_numbers(act, tmp_path):
    (tmp_path / "en.csv").write_text('item,qty,price,count\nA,1.000,2.5,"1,000"\nB,12.250,10,"12,000"\n', encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "en.csv")}, ctx)
    assert df["qty"].to_list() == [1.0, 12.25] and df["price"].to_list() == [2.5, 10.0]
    assert df["count"].to_list() == [1000, 12000]
    assert ctx.messages == [("info", "Encoding utf-8, delimiter comma, decimal point")]
    (tmp_path / "points.csv").write_text("Nr;Wert;Preis\n1;1.000;2.5\n2;3.125;4\n", encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "points.csv")})
    assert df["Wert"].to_list() == [1.0, 3.125] and df["Preis"].to_list() == [2.5, 4.0]


@pytest.mark.parametrize(
    ("values", "numbers"),
    [
        (["0.500", "1.250"], [0.5, 1.25]),
        (["-0.250", "0.125", "12.500"], [-0.25, 0.125, 12.5]),
        (["0.500", "1.250", "2.000"], [0.5, 1.25, 2.0]),
        (["1.250", "+0.750", "-1.000"], [1.25, 0.75, -1.0]),
    ],
)
def test_decimals_with_a_leading_zero_are_not_thousands(act, tmp_path, values, numbers):
    (tmp_path / "w.csv").write_text("Artikel;Gewicht\r\n" + "".join(f"A;{v}\r\n" for v in values), encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "w.csv")}, ctx)
    assert df["Gewicht"].to_list() == numbers and df.schema["Gewicht"] == pl.Float64
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal point")]


def test_a_chosen_decimal_comma_doesnt_read_0_500_as_thousands(act, tmp_path):
    (tmp_path / "w.csv").write_text("Artikel;Gewicht;Menge\nA;0.500;1.000\nB;1.250;-12.000\n", encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "w.csv"), "decimal": ","})
    assert df["Gewicht"].to_list() == ["0.500", "1.250"]  # not 500 and 1250: 0.500 can't be thousands
    assert df["Menge"].to_list() == [1000, -12000]


def test_a_column_follows_its_own_decimal_separator(act, tmp_path):
    rows = ["A;1.000;2.500;3,50;0.500", "B;12.000;1.250;4,25;1.250", "C;500;10.000;1,99;2.000"]
    (tmp_path / "t.csv").write_text("Artikel;Menge;Gewicht;Preis;Netto\n" + "\n".join(rows) + "\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "t.csv")}, ctx)
    assert df.drop("Artikel").to_dict(as_series=False) == {
        "Menge": [1000, 12000, 500],
        "Gewicht": [2500, 1250, 10000],
        "Preis": [3.5, 4.25, 1.99],
        "Netto": [0.5, 1.25, 2.0],
    }
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal comma (decimal point in Netto)")]
    (tmp_path / "en.csv").write_text('item,qty,price\nA,"0,500",2.5\nB,"1,250",10\n', encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "en.csv")})
    assert df["qty"].to_list() == [0.5, 1.25] and df["price"].to_list() == [2.5, 10.0]
    # a value that shows the separator after many that don't (beyond the sample)
    (tmp_path / "long.csv").write_text("Nr;Gewicht;Menge\n" + "1;1.250;5\n" * 2500 + "2;0.500;0.250\n", encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "long.csv")})
    assert df["Gewicht"].unique().sort().to_list() == [0.5, 1.25]
    assert df["Menge"].unique().sort().to_list() == [0.25, 5.0]


def test_the_log_names_the_columns_with_the_other_decimal_separator(act, tmp_path):
    folder = tmp_path / "in"
    folder.mkdir()
    header = "Nr;" + ";".join(f"P{n}" for n in range(1, 8)) + ";Betrag\n"
    (folder / "a.csv").write_text(header + "1;" + "0.500;" * 7 + "2,5\n", encoding="utf-8")
    (folder / "b.csv").write_text(header + "2;" + "1.250;" * 7 + "3,75\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"source": "folder", "folder": str(folder), "pattern": "*.csv"}, ctx)
    assert df["P7"].to_list() == [0.5, 1.25] and df["Betrag"].to_list() == [2.5, 3.75]
    assert ctx.messages == [("info", "2 files; encodings utf-8; delimiters semicolon; decimal point (decimal comma in Betrag)")]
    (tmp_path / "c.csv").write_text(header + "3;" + "0,5;" * 7 + "1.5\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "c.csv")}, ctx)
    assert df.row(0) == (3, *[0.5] * 7, 1.5)
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal comma (decimal point in Betrag)")]
    # more than five columns: the first five are named
    rows = ["1;" + "0.5;" * 7 + "2,5", *(f"{n};" + "1;" * 7 + "2,5" for n in range(2, 12))]
    (tmp_path / "d.csv").write_text(header + "\n".join(rows) + "\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "d.csv")}, ctx)
    assert df.row(0) == (1, *[0.5] * 7, 2.5)
    assert ctx.messages == [
        ("info", "Encoding utf-8, delimiter semicolon, decimal comma (decimal point in P1, P2, P3, P4, P5 and 2 more)")
    ]
    ctx = StubContext()
    act("csv.import", None, {"file": str(tmp_path / "c.csv"), "decimal": ","}, ctx)  # chosen: for all columns
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal comma")]


def test_the_log_names_only_columns_that_became_numbers(act, tmp_path):
    (tmp_path / "t.csv").write_text("Preis;Netto;Code\n1,5;0.500;0.250\n2,5;1.250;abc\n3,5;2.000;1.000\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "t.csv")}, ctx)
    assert df["Netto"].to_list() == [0.5, 1.25, 2.0] and df["Code"].to_list() == ["0.250", "abc", "1.000"]
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal comma (decimal point in Netto)")]
    (tmp_path / "text.csv").write_text("Preis;Code\n1,5;0.500\n2,5;abc\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "text.csv")}, ctx)
    assert df["Code"].to_list() == ["0.500", "abc"]
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal comma")]


def test_line_ends_converted_twice(act, tmp_path):
    (tmp_path / "p.csv").write_bytes(b"Artikel;Preis\r\r\nA;1.5\r\r\nB;2.25\r\r\n")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "p.csv")}, ctx)
    assert df.to_dict(as_series=False) == {"Artikel": ["A", "B"], "Preis": [1.5, 2.25]}
    assert ctx.messages == [("info", "Encoding utf-8, delimiter semicolon, decimal point")]
    (tmp_path / "n.csv").write_bytes(b'Nr;Notiz\r\r\n1;"x; y"\r\r\n2;z\r\r\n3;\r\r\n')
    df = act("csv.import", None, {"file": str(tmp_path / "n.csv")})
    assert df["Notiz"].to_list() == ["x; y", "z", None]
    (tmp_path / "t.csv").write_text("Artikel;Gewicht\nA;0.500\t\nB;1.250\t\n", encoding="utf-8")  # tabs at the end
    assert act("csv.import", None, {"file": str(tmp_path / "t.csv")})["Gewicht"].to_list() == [0.5, 1.25]
    (tmp_path / "many.csv").write_bytes(b'a;b\r\r\r\r\n1;"x\ry"\r\n2;z\r\r\n')  # more than two; one inside a value stays
    assert act("csv.import", None, {"file": str(tmp_path / "many.csv")}).to_dict(as_series=False) == {"a": [1, 2], "b": ["x\ry", "z"]}


def test_a_line_end_converted_twice_in_a_value_stays(act, tmp_path):
    """In a file with other line ends, as Tafl writes them, it is part of the value. The first line
    end outside quotes tells how the line ends of the file are."""
    (tmp_path / "v.csv").write_bytes(b'A;B\r\n"x\r\r\ny";1\r\nz;2\r\n')
    assert act("csv.import", None, {"file": str(tmp_path / "v.csv")})["A"].to_list() == ["x\r\r\ny", "z"]
    (tmp_path / "h.csv").write_bytes(b'"Net\r\namount";B\r\r\n1;"x\r\r\ny"\r\r\n')  # converted twice; a name with a line break
    out = act("csv.import", None, {"file": str(tmp_path / "h.csv"), "delimiter": ";"})
    assert out.to_dict(as_series=False) == {"Net\r\namount": [1], "B": ["x\r\ny"]}


def test_line_ends_converted_twice_after_the_first_line(act, tmp_path):
    """Python's csv.writer on Windows ends rows with \\r\\r\\n in a file opened without newline="", and a
    header written on its own ends with \\n: the first line end decided, and the last value of each
    row kept a \\r (Smith\\r matched no customer). Such a line end outside quotes counts anywhere in
    the file, also after the first 64 KB; then also those in quoted values are converted."""
    for header in (b"Order;Customer\r\n", b"Order;Customer\n"):
        (tmp_path / "a.csv").write_bytes(header + b"1;Smith\r\r\n2;Doe\r\r\n")
        out = act("csv.import", None, {"file": str(tmp_path / "a.csv")})
        assert out.to_dict(as_series=False) == {"Order": [1, 2], "Customer": ["Smith", "Doe"]}, header
    rows = b"".join(b"%d;Filler\r\n" % n for n in range(6000))  # more than 64 KB
    (tmp_path / "late.csv").write_bytes(b"Order;Customer\r\n" + rows + b"9001;Smith\r\r\n")
    assert act("csv.import", None, {"file": str(tmp_path / "late.csv")})["Customer"][-1] == "Smith"
    (tmp_path / "q.csv").write_bytes(b'Order;Customer\r\n1;"x\r\r\ny"\r\n2;Doe\r\r\n')
    assert act("csv.import", None, {"file": str(tmp_path / "q.csv")})["Customer"].to_list() == ["x\r\ny", "Doe"]
    (tmp_path / "s.csv").write_bytes(b'Report "2025\r\nOrder;Customer\r\r\n1;Smith\r\r\n')  # a quote in a line skipped
    assert act("csv.import", None, {"file": str(tmp_path / "s.csv"), "skip_lines": 1})["Customer"].to_list() == ["Smith"]
    (tmp_path / "many.csv").write_bytes(b"Order;Customer\r\n1;Smith\r\r\n")
    start = time.perf_counter()
    assert act("csv.import", None, {"file": str(tmp_path / "many.csv"), "skip_lines": 10**8}).height == 0  # more than there are
    assert time.perf_counter() - start < 2


def test_a_line_end_converted_twice_in_a_value_of_a_wide_file_stays(act, tmp_path):
    """The first line end of a wide table can be beyond the 64 KB looked at for the delimiter: the
    value lost a \\r."""
    (tmp_path / "w.csv").write_bytes(b"A;" + b"N" * 70_000 + b'\r\n"x\r\r\ny";1\r\n')
    assert act("csv.import", None, {"file": str(tmp_path / "w.csv")})["A"].to_list() == ["x\r\r\ny"]


def test_carriage_returns_in_values_come_back_from_an_export(act, tmp_path):
    values = ["x\r\r\ny", "Smith\r", "a\r\nb", "q\r\r", "z"]
    for quote_all in (False, True):
        act("csv.export", pl.DataFrame({"a": values, "b": values}), {"file": str(tmp_path / "e.csv"), "quote_all": quote_all})
        assert act("csv.import", None, {"file": str(tmp_path / "e.csv")}).to_dict(as_series=False) == {"a": values, "b": values}


@pytest.mark.parametrize("delimiter", [";", ",", "\t", "|"])
def test_the_delimiter_is_detected_beside_column_names_of_two_lines(act, tmp_path, delimiter):
    """A line break in quotes is part of its line. The first line was "Net without a delimiter:
    the file had one column, and Polars couldn't read it."""
    d = delimiter
    (tmp_path / "h.csv").write_bytes(f'"Net\r\namount"{d}"A\nB"{d}C\r\n1{d}"x\ny"{d}z\r\n2{d}w{d}v\r\n'.encode())
    out = act("csv.import", None, {"file": str(tmp_path / "h.csv")})
    assert out.to_dict(as_series=False) == {"Net\r\namount": [1, 2], "A\nB": ["x\ny", "w"], "C": ["z", "v"]}
    (tmp_path / "s.csv").write_bytes(f'"Report of\n"Net\namount"{d}B\n1{d}x\n'.encode())  # Polars skips lines also in quotes
    out = act("csv.import", None, {"file": str(tmp_path / "s.csv"), "skip_lines": 1})
    assert out.to_dict(as_series=False) == {"Net\namount": [1], "B": ["x"]}


def test_a_long_run_of_carriage_returns_takes_no_long_time(act, tmp_path):
    """Each carriage return of the run started the search again, with the Python lock held: the
    time grew with the square of the run, and 160 KB took 16 s without any way to stop it."""
    (tmp_path / "r.csv").write_bytes(b"a;b\r\r\n1;2\r\r\n" + b"\r" * 120_000 + b"x")
    start = time.perf_counter()
    out = act("csv.import", None, {"file": str(tmp_path / "r.csv")})
    assert time.perf_counter() - start < 2
    assert out.row(0) == ("1", 2)  # the carriage returns and the x are one more value of the first column


def test_a_file_with_one_column(act, tmp_path):
    (tmp_path / "b.csv").write_text("Betrag\r\n1.234,50\r\n12,00\r\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "b.csv")}, ctx)
    assert df.to_dict(as_series=False) == {"Betrag": [1234.5, 12.0]}
    assert ctx.messages == [("info", "Encoding utf-8, delimiter none (one column), decimal comma")]
    (tmp_path / "n.csv").write_text('"Name, Vorname"\nMüller, Hans\n"Schmidt; Eva"\n', encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "n.csv")})
    assert df.to_dict(as_series=False) == {"Name, Vorname": ["Müller, Hans", "Schmidt; Eva"]}
    (tmp_path / "s.csv").write_text("Bericht\nA;1\n", encoding="utf-8")  # a title line: still one column, as before
    assert act("csv.import", None, {"file": str(tmp_path / "s.csv")}).to_dict(as_series=False) == {"Bericht": ["A;1"]}
    df = act("csv.import", None, {"file": str(tmp_path / "s.csv"), "skip_lines": 1, "header": False})
    assert df.to_dict(as_series=False) == {"Column 1": ["A"], "Column 2": [1]}


def test_long_numbers_with_thousands_separators_keep_all_digits(act, tmp_path):
    (tmp_path / "l.csv").write_text(
        "Id;Code;Mehr;Betrag\n"
        "1.234.567.890.123.456.789;0815;98.765.432.109.876.543.210;12.345.678.901.234.567,89 €\n"
        "9.007.199.254.740.993;0042;1;5 €\n",
        encoding="utf-8",
    )
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "l.csv")}, ctx)
    assert df["Id"].to_list() == [1234567890123456789, 9007199254740993] and df.schema["Id"] == pl.Int64
    assert df.schema["Code"] == pl.String and df.schema["Mehr"] == pl.String and df.schema["Betrag"] == pl.String
    assert ctx.warnings == [
        "Mehr stays text: its numbers are too long to be kept exactly, e.g. '98.765.432.109.876.543.210'",
        "Betrag stays text: its numbers are too long to be kept exactly, e.g. '12.345.678.901.234.567,89 €'",
    ]


def test_amounts_percentages_and_a_minus_at_the_end(act, tmp_path):
    (tmp_path / "a.csv").write_text(
        "Betrag;Rabatt;Saldo;Kunde;Notiz\n"
        "1.234,50 €;12,5%;123,45-;0815;5 €\n"
        "€ 5;3 %;7,00;0042;viel €\n"
        "-2 000,00 €;100%;1.000,00-;0007;\n",
        encoding="utf-8",
    )
    df = act("csv.import", None, {"file": str(tmp_path / "a.csv")})
    assert df["Betrag"].to_list() == [1234.5, 5.0, -2000.0]
    assert df["Rabatt"].to_list() == [0.125, 0.03, 1.0]
    assert df["Saldo"].to_list() == [-123.45, 7.0, -1000.0]
    assert df.schema["Kunde"] == pl.String and df.schema["Notiz"] == pl.String


@pytest.mark.parametrize("decimal", ["auto", ",", "."])
def test_digits_with_spaces_between_them_stay_text(act, tmp_path, decimal):
    (tmp_path / "p.csv").write_text(PHONES, encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "p.csv"), "decimal": decimal}, ctx)
    assert df.schema == dict.fromkeys(df.columns, pl.String)
    assert df.row(0) == ("A", "+49 30 1234567", "4111 1111 1111 1111", "370 400 44", "2025 01 31", "10 20", "1 234")
    assert df.row(1) == ("B", "+49 89 7654321", "5500 0000 0000 0004", "100 500 00", "2024 12 24", "5 7", "2 500")
    assert not ctx.warnings
    (tmp_path / "c.csv").write_text(  # with commas, no-break spaces and empty values
        "Gruppen,Paare,Telefon,Menge\n"
        "8 965 570,92 57 61,+43 1 2232697,1\n"
        ",74 31 13 28,030 1234567,2\n"
        "6 176 827,27 24 33 62,+1 212 5551234,\n",
        encoding="utf-8",
    )
    df = act("csv.import", None, {"file": str(tmp_path / "c.csv"), "decimal": decimal})
    assert df.schema == {"Gruppen": pl.String, "Paare": pl.String, "Telefon": pl.String, "Menge": pl.Int64}
    assert df["Gruppen"].to_list() == ["8 965 570", None, "6 176 827"]
    assert df["Telefon"].to_list() == ["+43 1 2232697", "030 1234567", "+1 212 5551234"]


def test_spaces_between_thousands_in_amounts_and_percentages(act, tmp_path):
    (tmp_path / "a.csv").write_text(
        "Betrag;Fremd;Rabatt;Lücken;Code\n"
        "1 234,50 €;$ 1 234 567.5;1 234,5 %;12 34,50 €;€ 0815\n"
        "-2 000,00 €;$ -5;5 %;1 234,50 €;€ 4711\n"
        "€ 12;12 $;-0,5 %;5 €;€ 12\n"
        "1 000 000-;3 000.25-;+0,5%;5 €;€ 1\n",
        encoding="utf-8",
    )
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "a.csv")}, ctx)
    assert df["Betrag"].to_list() == [1234.5, -2000.0, 12.0, -1000000.0]
    assert df["Fremd"].to_list() == [1234567.5, -5.0, 12.0, -3000.25]
    assert df["Rabatt"].to_list() == [12.345, 0.05, -0.005, 0.005]
    assert df["Lücken"].to_list() == ["12 34,50 €", "1 234,50 €", "5 €", "5 €"]  # 12 34: not thousands
    assert df["Code"].to_list() == ["€ 0815", "€ 4711", "€ 12", "€ 1"]  # a leading zero after the sign: a code
    assert not ctx.warnings


def test_percentages_are_read_exactly(act, tmp_path):
    (tmp_path / "p.csv").write_text("P;Q\n1,1%;41,2 %\n33,3%;13,14%\n", encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "p.csv")})
    assert df["P"].to_list() == [0.011, 0.333] and df["Q"].to_list() == [0.412, 0.1314]  # not 0.011000000000000001
    act("csv.export", df, {"file": str(tmp_path / "out.csv")})
    assert (tmp_path / "out.csv").read_text(encoding="utf-8-sig").splitlines() == ["P;Q", "0,011;0,412", "0,333;0,1314"]
    (tmp_path / "e.csv").write_text('P,Q\n19.99 %,108.9%\n-19.69 %,"1,234.5%"\n+5%,-1112 %\n', encoding="utf-8")
    df = act("csv.import", None, {"file": str(tmp_path / "e.csv")})
    assert df["P"].to_list() == [0.1999, -0.1969, 0.05] and df["Q"].to_list() == [1.089, 12.345, -11.12]
    (tmp_path / "t.csv").write_text("P\n1.234,5%\n0,07%\n", encoding="utf-8")
    assert act("csv.import", None, {"file": str(tmp_path / "t.csv"), "decimal": ","})["P"].to_list() == [12.345, 0.0007]


def test_a_utf8_file_with_one_wrong_byte_stays_utf8(act, tmp_path):
    (tmp_path / "u.csv").write_bytes("Name\nMüller\nJürgen\n".encode() + b"Bad\xff\n")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "u.csv")}, ctx)
    assert df["Name"].to_list() == ["Müller", "Jürgen", "Bad�"]
    assert ctx.warnings == ["u.csv: 1 character is not UTF-8, shown as �"]
    assert ctx.messages[-1] == ("info", "Encoding utf-8, delimiter none (one column), decimal point")


def test_a_windows_file_with_one_utf8_character(act, tmp_path):
    (tmp_path / "w.csv").write_bytes("Name\nMüller\nWeiß\n".encode("cp1252") + "Jürgen\n".encode())
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "w.csv")}, ctx)
    assert df["Name"].to_list() == ["Müller", "Weiß", "JÃ¼rgen"]
    assert ctx.warnings == ["w.csv was read as Windows (ANSI), but it has UTF-8 characters too: they look wrong, e.g. Ã¼ for ü"]


def test_a_chosen_encoding_that_does_not_fit(act, tmp_path):
    (tmp_path / "w.csv").write_bytes("Größe\nweiß\n".encode("cp1252"))
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "w.csv"), "encoding": "utf-8"}, ctx)
    assert df.columns == ["Gr��e"]
    assert ctx.warnings == [
        "w.csv is not UTF-8: 3 characters are shown as �. Choose another encoding (e.g. Windows (ANSI)) or Detect automatically"
    ]
    (tmp_path / "u.csv").write_text("Größe\nweiß\n", encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "u.csv"), "encoding": "cp1252"}, ctx)
    assert df.columns == ["GrÃ¶ÃŸe"]
    assert ctx.warnings == [
        "u.csv looks like UTF-8, not Windows (ANSI): characters like ü look wrong (Ã¼). Choose UTF-8 or Detect automatically"
    ]
    (tmp_path / "x.csv").write_bytes(b"a\n\x81\n")  # a byte that Windows (ANSI) doesn't have
    ctx = StubContext()
    assert act("csv.import", None, {"file": str(tmp_path / "x.csv"), "encoding": "cp1252"}, ctx)["a"].to_list() == ["�"]
    assert ctx.warnings == ["x.csv: 1 character is not Windows (ANSI), shown as �"]
    ctx = StubContext()
    act("csv.import", None, {"file": str(tmp_path / "w.csv"), "encoding": "cp1252"}, ctx)
    assert not ctx.warnings


def test_rows_with_more_values_than_the_header(act, tmp_path):
    (tmp_path / "r.csv").write_text('a;b\n1;2;3\n4;5\n6;7;\n"8;8";9;10;11\n', encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "r.csv")}, ctx)
    assert df.to_dict(as_series=False) == {"a": ["1", "4", "6", "8;8"], "b": [2, 5, 7, 9]}
    assert ctx.warnings == ["In r.csv, 2 rows have more values than the first row: the extra values were left out"]
    (tmp_path / "s.csv").write_text("a;b;\n1;2;\n3;4;\n", encoding="utf-8")  # empty values at the end are no loss
    ctx = StubContext()
    assert act("csv.import", None, {"file": str(tmp_path / "s.csv")}, ctx)["a"].to_list() == [1, 3]
    assert not ctx.warnings


def test_date_times_with_nanoseconds_keep_them(act, tmp_path):
    """2025-01-01 08:30:00.000000001 was read as 08:30:00: the detection kept microseconds only. A
    column with a date-time beyond the years of nanoseconds keeps microseconds, with a warning."""
    text = "At;Other\n2025-01-01 08:30:00.000000001;2025-01-01 08:30:00.000000001\n2025-01-01 08:30:00.5;3000-01-01 08:30:00\n"
    (tmp_path / "ns.csv").write_text(text, encoding="utf-8")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "ns.csv")}, ctx)
    assert df.schema == {"At": pl.Datetime("ns"), "Other": pl.Datetime("us")}
    assert df["At"].cast(pl.Int64).to_list() == [1_735_720_200 * 10**9 + 1, 1_735_720_200 * 10**9 + 500_000_000]
    assert df["Other"].dt.year().to_list() == [2025, 3000]
    assert ctx.warnings == [
        "Other: the digits below a microsecond were cut, e.g. in '2025-01-01 08:30:00.000000001': nanoseconds fit only "
        "from 1677 to 2262, and the column has date-times beyond"
    ]


def test_a_quote_out_of_place_is_shown_with_its_line(act, tmp_path):
    """Polars' words were the message: "CSV malformed: expected 3 rows, actual 5 rows, in chunk
    starting at row_offset 3, length 39", or "could not parse `"24"x` as dtype `str`"."""
    inside = "line 4 has a quote in a value that doesn't start with one: '24\";Monitor'. In a CSV file, such a value is in quotes"
    cases = [
        ('Size;Item\n1;2\n3;4\n24";Monitor\n5;6\n', {}, inside),
        ('Size;Item\r\n1;2\r\n3;4\r\n24";Monitor\r\n', {}, inside),
        ('Title "a\nSize;Item\n24";Monitor\n', {"skip_lines": 1}, "line 3 has a quote in a value"),  # the skipped line counts
        ('Size,Item\n"1,5",2\n"24"x,y\n', {}, "line 3 has a value that goes on after its closing quote: '\"24\"x,y'. In a CSV file, a quote"),
        ("Size;Item\n" + "1;2\n" * 100_000 + 'Monitor 24";' + "x" * 80 + "\n", {}, "line 100002 has a quote .*: 'Monitor 24\";" + "x" * 48 + "…'"),
    ]
    for text, options, message in cases:
        (tmp_path / "q.csv").write_text(text, encoding="utf-8", newline="")
        start = time.perf_counter()
        with pytest.raises(ActionError, match="^Could not read q.csv: " + message.replace(".", r"\.").replace("\\.*", ".*")):
            act("csv.import", None, {"file": str(tmp_path / "q.csv"), **options})
        assert time.perf_counter() - start < 2
    (tmp_path / "q.csv").write_text('Size;Item\n24" and 27";Monitor\n24"";TV\n"24"" x";Radio\n', encoding="utf-8")
    assert act("csv.import", None, {"file": str(tmp_path / "q.csv")})["Size"].to_list() == ['24" and 27"', '24""', '24" x']  # read as before


@pytest.mark.parametrize(
    ("data", "options", "rows"),
    [
        (b"Id;Name;Price\r1;Mouse;5,5\r2;Pad;1\r", {}, [(1, "Mouse", 5.5), (2, "Pad", 1.0)]),
        (b"Id;Name;Price\r1;Mouse;5,5\r2;Pad;1", {}, [(1, "Mouse", 5.5), (2, "Pad", 1.0)]),  # no line end at the end
        (b"Id,Name\r1,a\r2,b\r", {"delimiter": ","}, [(1, "a"), (2, "b")]),
        (b"title\rId;Name\r1;a\r", {"skip_lines": 1, "header": False}, [("Id", "Name"), ("1", "a")]),
        ("Kunde;Ort\rMüller;Köln\r".encode("cp1252"), {}, [("Müller", "Köln")]),
        ("Kunde;Ort\rMüller;Köln\r".encode("utf-16"), {}, [("Müller", "Köln")]),
        (b'Id;Note\r1;"a\rb"\r2;c\r', {"detect_types": False}, [("1", "a\nb"), ("2", "c")]),
    ],
)
def test_a_file_with_carriage_returns_as_line_ends(act, tmp_path, data, options, rows):
    """A file whose lines end with \\r alone (old Macs) gave no rows and no message: Polars read it
    as one line, the column names. A file without \\n has \\r as its line end now."""
    (tmp_path / "cr.csv").write_bytes(data)
    assert act("csv.import", None, {"file": str(tmp_path / "cr.csv"), **options}).rows() == rows


def test_a_quote_out_of_place_in_a_file_with_carriage_returns_as_line_ends(act, tmp_path):
    (tmp_path / "cr.csv").write_bytes(b'Id;Name;Price\r1;Mouse;5\r2;Monitor 24";99\r3;Pad;1\r')
    with pytest.raises(ActionError, match=r"^Could not read cr\.csv: line 3 has a quote in a value that doesn't start with one: '2;Monitor"):
        act("csv.import", None, {"file": str(tmp_path / "cr.csv")})


def test_a_quote_that_is_never_closed_is_shown_with_its_line(act, tmp_path):
    """Polars' words were the message, with the rest of the file in them: "could not parse
    `"Mouse;5↵2;Pad;1↵3;Key;2↵` as dtype `str` at column 'column_1' (column number 2)"."""
    never = "has a value that starts with a quote that is never closed"
    cases = [
        (b'Id;Name;Price\n1;"Mouse;5\n2;Pad;1\n3;Key;2\n', f"line 2 {never}: '1;\"Mouse;5'"),
        (b'Id;Name;Price\n1;Mouse;"5\n2;Pad;1\n3;Key;2\n', f"line 2 {never}: '1;Mouse;\"5'"),
        (b'Id;Name;Price\n1;Mouse;5\n2;"Pad;1\n', f"line 3 {never}: '2;\"Pad;1'"),
        (b'Id,Name,Price\n1,"Mouse,5\n2,Pad,1\n3,Key,2\n', f"line 2 {never}: '1,\"Mouse,5'"),
    ]
    for data, message in cases:
        (tmp_path / "q.csv").write_bytes(data)
        with pytest.raises(ActionError, match=f"^Could not read q\\.csv: {re.escape(message)}\\. In a CSV file, a value in quotes ends with a quote"):
            act("csv.import", None, {"file": str(tmp_path / "q.csv")})


def test_the_quote_check_is_as_fast_as_one_without_carriage_returns_in_values():
    """A lone \\r in a value is part of the value for the check of quotes. The first pattern for that
    took 2.6 times as long as one without (on a file of 50 MB with the fault at its end, 1.8 s):
    measured against that one here, best of 3 each."""
    from tafl.plugins import csv_files

    line = b"12345;Some product name;99.5;2025-01-31;x\n"
    data = b"Id;Name;Price;Date;Flag\n" + line * 250_000 + b'9;Monitor 24";1;2025-01-31;x\n'
    plain = r'[^";\r\n]*+'  # without lone carriage returns
    without = re.compile(rf'(?:(?>"(?:[^"]|"")*+"|{plain}(?:"{plain}"{plain})*+)(?:;|\r?\n|\Z))*+')
    text = data.decode()

    def best(work) -> float:
        times = []
        for _ in range(3):
            start = time.perf_counter()
            work()
            times.append(time.perf_counter() - start)
        return min(times)

    assert csv_files._stray_quote(data, ";", 0).startswith("line 250002 has a quote")
    assert best(lambda: csv_files._stray_quote(data, ";", 0)) < 1.6 * best(lambda: without.match(text)) + 0.05


def test_a_lone_carriage_return_in_a_value_is_no_end_of_the_line(act, tmp_path):
    """A \\r without \\n in a value (Polars reads it as part of the value) ended the line for the
    check: the message named that line, which has no quote."""
    (tmp_path / "q.csv").write_bytes(b'Id;Name;Price\n1;Mou\rse;5\n2;Monitor 24";99\n3;Pad;1\n')
    with pytest.raises(ActionError, match=r"^Could not read q\.csv: line 3 has a quote in a value that doesn't start with one: '2;Monitor 24\";99'"):
        act("csv.import", None, {"file": str(tmp_path / "q.csv")})
    (tmp_path / "q.csv").write_bytes(b'Id;Name;Price\n1;Mou\rse;5\n2;Monitor;99\n')  # without the quote: read as before
    assert act("csv.import", None, {"file": str(tmp_path / "q.csv")})["Name"].to_list() == ["Mou\rse", "Monitor"]


def test_a_failed_export_leaves_the_old_file(act, tmp_path, monkeypatch):
    path = tmp_path / "out.csv"
    path.write_text("an older, complete export\n", encoding="utf-8")

    def broken(self, file=None, **options):
        Path(file).write_text("id;da", encoding="utf-8")
        raise pl.exceptions.ComputeError("the disk is gone")

    monkeypatch.setattr(pl.DataFrame, "write_csv", broken)
    with pytest.raises(pl.exceptions.ComputeError):
        act("csv.export", pl.DataFrame({"id": [1]}), {"file": str(path)})
    assert path.read_text(encoding="utf-8") == "an older, complete export\n"
    assert [p.name for p in tmp_path.iterdir()] == ["out.csv"]  # no temporary file left


@pytest.mark.skipif(sys.platform != "win32", reason="Windows locks open files")
@pytest.mark.parametrize("encoding", ["utf-8-sig", "cp1252"])
def test_export_to_a_file_that_is_open(act, tmp_path, encoding):
    path = tmp_path / "out.csv"
    path.write_text("old\n", encoding="utf-8")
    message = r"out.csv is open in another program \(e.g. Excel\): close it and run again"
    with open(path, "rb"), pytest.raises(ActionError, match=message):
        act("csv.export", pl.DataFrame({"id": [1]}), {"file": str(path), "encoding": encoding})
    assert path.read_text(encoding="utf-8") == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["out.csv"]


def test_export_binary_infinite_and_time_values(act, tmp_path):
    df = pl.DataFrame(
        {
            "id": [1, 2, 3],
            "data": [b"\x01\x02\xff", None, b""],
            "ratio": [float("inf"), float("nan"), 0.5],
            "at": [dt.time(9, 30), None, dt.time(23, 59, 1)],
        }
    )
    path = tmp_path / "blob.csv"
    act("csv.export", df, {"file": str(path), "decimal": "."})
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["id;data;ratio;at", "1;0x0102FF;;09:30:00", "2;;;", "3;0x;0.5;23:59:01"]


@pytest.mark.parametrize("datetime_format", ["dd.mm.yyyy hh:mm:ss", "yyyy-mm-dd hh:mm", "%Y-%m-%dT%H:%M:%S%.f"])
def test_export_keeps_the_fractions_of_a_second_of_times(act, tmp_path, datetime_format):
    """Times were written with whole seconds, whatever the date-time format: 08:30:00.5 was 08:30:00.
    Now they keep their fractions, in the text that is the same on every PC (as Pivot writes them)."""
    at = pl.Series("At", [dt.time(8, 30, 0, 500000), dt.time(8, 30), dt.time(23, 59, 59, 999999), None, dt.time(0, 0, 0, 1000)])
    exact = pl.Series("Exact", [30600 * 10**9 + 1, None, None, None, None], dtype=pl.Int64).cast(pl.Time)  # 1 ns
    df = pl.DataFrame([at, exact])
    path = tmp_path / "times.csv"
    act("csv.export", df, {"file": str(path), "datetime_format": datetime_format})
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines == ["At;Exact", "08:30:00.500;08:30:00.000000001", "08:30:00;", "23:59:59.999999;", ";", "00:00:00.001;"]
    assert lines[1:] == [";".join(row) for row in df.select(dtypes.text_expr(pl.all(), pl.Time)).fill_null("").rows()]


def test_export_writes_time_spans_as_text_like_1d_2h_3m(act, tmp_path):
    """A time-span column stopped the export: "datatype duration[μs] cannot be written to CSV". The
    text is the same on every PC, as Pivot and Split column write time spans."""
    span = pl.Series("Span", [dt.timedelta(days=1, hours=2, minutes=3), dt.timedelta(microseconds=1500), None, dt.timedelta(seconds=-5)])
    exact = pl.Series("Exact", [1, None, None, 2_000_000_000], dtype=pl.Duration("ns"))
    path = tmp_path / "spans.csv"
    act("csv.export", pl.DataFrame([span, exact]), {"file": str(path)})
    assert path.read_text(encoding="utf-8-sig").splitlines() == ["Span;Exact", "1d 2h 3m;1ns", "1500µs;", ";", "-5s;2s"]
    assert pl.select(dtypes.text_expr(pl.lit(span), pl.Duration)).to_series().to_list() == ["1d 2h 3m", "1500µs", None, "-5s"]


def test_lists_and_records_stop_the_export_with_their_columns(act, tmp_path):
    """Polars' message named no column: "CSV format does not support nested data"."""
    path = tmp_path / "out.csv"
    path.write_text("old\n", encoding="utf-8")
    df = pl.DataFrame({"Tags": [["a"], []], "Ok": [1, 2], "Pair": [{"x": 1}, None], "Grid": pl.Series([[1, 2], [3, 4]], dtype=pl.Array(pl.Int64, 2))})
    message = r"^A CSV file can't hold lists or records: Tags, Pair and Grid have them\. Remove these columns before this step"
    with pytest.raises(ActionError, match=message):
        act("csv.export", df, {"file": str(path)})
    with pytest.raises(ActionError, match=r"^A CSV file can't hold records: Pair has them\. Remove this column before"):
        act("csv.export", df.select("Ok", "Pair"), {"file": str(path)})
    many = pl.DataFrame({f"L{i}": [[i]] for i in range(7)})
    with pytest.raises(ActionError, match=r"lists: L0, L1, L2, L3, L4 and 2 more columns have them"):
        act("csv.export", many, {"file": str(path)})
    with pytest.raises(ActionError, match=r"lists: L0, L1, L2, L3, L4 and 1 more column have them"):
        act("csv.export", many.drop("L6"), {"file": str(path)})
    assert path.read_text(encoding="utf-8") == "old\n"  # the file stays as it was
    act("csv.export", df.select("Ok"), {"file": str(path)})
    assert path.read_text(encoding="utf-8-sig").splitlines() == ["Ok", "1", "2"]


_WIDE = """
import datetime as dt
import re
import sys
from pathlib import Path

import polars as pl
from tafl.api import ActionError
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
folder = Path(sys.argv[1])
kinds = [[1.5, None], [dt.time(8, 30, 0, 500000), None], [dt.timedelta(hours=1), None], [b"\\x01", None], ["a", None]]
df = pl.DataFrame({f"c{i}": kinds[i % 5] for i in range(4_001)})
registry.actions["csv.export"](df, {"file": str(folder / "wide.csv")}, StubContext())
lines = (folder / "wide.csv").read_text(encoding="utf-8-sig").splitlines()
assert lines[1].split(";")[:5] == ["1,5", "08:30:00.500", "1h", "0x01", "a"], lines[1][:80]
nested = pl.DataFrame({f"L{i}": [[i]] for i in range(4_001)})
try:
    registry.actions["csv.export"](nested, {"file": str(folder / "nested.csv")}, StubContext())
except ActionError as error:
    assert "L0, L1, L2, L3, L4 and 3,996 more columns have them" in str(error), error
else:
    raise AssertionError("no message")
(folder / "in.csv").write_text(";".join(f"c{i}" for i in range(4_001)) + "\\n1,5" + ";" * 4_000 + "\\n", encoding="utf-8")
back = registry.actions["csv.import"](None, {"file": str(folder / "in.csv")}, StubContext())
assert back.width == 4_001 and back["c0"].to_list() == [1.5] and back.schema["c4000"] == pl.String, back.schema
"""


def test_a_wide_table_is_exported_and_imported(tmp_path):
    """4,001 columns: the checks of the export and the detection of numbers go through every column,
    and a deep Polars expression would end the process. In a process of its own, so that such an end
    shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _WIDE, str(tmp_path)], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


def test_export_writes_decimals_with_15_digits_as_excel_does(act, tmp_path):
    values = [0.37 * 3, 1.1 * 3, 0.1, 1 / 3, 1234.5, -0.25, 2.0, 1e-7, 1.5e20, 0.1 + 0.2, -2 / 3]
    values += [225179981368524.5, 1e-20 / 3]
    path = tmp_path / "f.csv"
    act("csv.export", pl.DataFrame({"x": values}), {"file": str(path)})
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    assert lines[1:7] == ["1,11", "3,3", "0,1", "0,333333333333333", "1234,5", "-0,25"]  # not 1,1099999999999999
    assert lines[7:12] == ["2", "0,0000001", "150000000000000000000", "0,3", "-0,666666666666667"]
    assert lines[12:] == ["225179981368525", "0,00000000000000000000333333333333333"]  # 5 rounded up, as in Excel
    back = act("csv.import", None, {"file": str(path)})["x"].to_list()
    assert back[:9] == [1.11, 3.3, 0.1, 0.333333333333333, 1234.5, -0.25, 2.0, 1e-7, 1.5e20]
    assert back[9:] == [0.3, -0.666666666666667, 225179981368525.0, 3.33333333333333e-21]
    act("csv.export", pl.DataFrame({"x": values[:4]}), {"file": str(path), "decimal": ".", "encoding": "cp1252"})
    assert path.read_text(encoding="cp1252").splitlines() == ["x", "1.11", "3.3", "0.1", "0.333333333333333"]


def test_export_keeps_the_digits_of_whole_numbers_and_exact_decimals(act, tmp_path):
    """Whole numbers (Int) and exact decimals keep all their digits; decimal numbers (Float) get
    15 digits, as in Excel, also when they are whole."""
    df = pl.DataFrame(
        {
            "id": [1234567890123456789, -5],
            "whole": [9007199254740991.0, -1234567890123456.0],
            "exact": pl.Series([Decimal("12345678901234567.891"), Decimal("0.1")], dtype=pl.Decimal(20, 3)),
            "single": pl.Series([0.1, 1 / 3], dtype=pl.Float32),  # the digits a Float32 has
        }
    )
    path = tmp_path / "w.csv"
    act("csv.export", df, {"file": str(path)})
    assert path.read_text(encoding="utf-8-sig").splitlines() == [
        "id;whole;exact;single",
        "1234567890123456789;9007199254740990;12345678901234567,891;0,1",
        "-5;-1234567890123460;0,100;0,33333334",
    ]


def test_export_writes_big_decimal_numbers_with_15_digits_as_excel_does(act, tmp_path):
    """From 2^53 on, a decimal number (Float) holds only some whole numbers: formula results
    there get 15 digits too, as Excel shows them, not the digits of their rounding errors."""
    values = [0.37 * 3 * 1e16, 0.37 * 3 * 1e20, -0.37 * 3 * 1e16, 2.0**53, 123456789012345678.0, 1.5e20]
    path = tmp_path / "big.csv"
    act("csv.export", pl.DataFrame({"x": values}), {"file": str(path)})
    assert path.read_text(encoding="utf-8-sig").splitlines()[1:] == [
        "11100000000000000",  # not 11099999999999998
        "111000000000000000000",  # not 110999999999999980000
        "-11100000000000000",
        "9007199254740990",
        "123456789012346000",
        "150000000000000000000",
    ]


def test_export_keeps_the_largest_decimal_numbers_finite(act, tmp_path):
    """Rounded up to 15 digits, the largest decimal number would be infinite: it is rounded
    towards zero instead."""
    path = tmp_path / "max.csv"
    act("csv.export", pl.DataFrame({"x": [1.7976931348623157e308, -1.7976931348623157e308]}), {"file": str(path)})
    lines = path.read_text(encoding="utf-8-sig").splitlines()[1:]
    assert [float(line.replace(",", ".")) for line in lines] == [1.79769313486231e308, -1.79769313486231e308]


def test_export_rows_without_columns_gives_an_empty_file(act, tmp_path):
    rows = pl.DataFrame({"a": [1, 2, 3]}).drop("a")
    path = tmp_path / "out.csv"
    path.write_text("old\n", encoding="utf-8")
    ctx = StubContext()
    act("csv.export", rows, {"file": str(path)}, ctx)
    assert path.read_bytes() == b"\xef\xbb\xbf\r\n"
    assert ctx.messages == [("info", f"Wrote an empty file to {path}: the table has no columns")]
    act("csv.export", pl.DataFrame(), {"file": str(tmp_path / "none.csv")})  # the same as for a table without rows
    assert (tmp_path / "none.csv").read_bytes() == path.read_bytes()
    act("csv.export", rows, {"file": str(path), "encoding": "cp1252", "header": False})
    assert path.read_bytes() == b""
    assert [p.name for p in tmp_path.iterdir()] == ["none.csv", "out.csv"]


def test_dont_export_an_empty_table(act, registry, tmp_path):
    path, empty = tmp_path / "out.csv", pl.DataFrame(schema={"a": pl.Int64})
    path.write_text("yesterday\n", encoding="utf-8")
    ctx = StubContext()
    act("csv.export", empty, {"file": str(path), "skip_empty": True}, ctx)
    assert path.read_text(encoding="utf-8") == "yesterday\n"
    assert ctx.messages == [("info", "Not exported: the table is empty")]
    act("csv.export", empty, {"file": str(tmp_path / "new.csv"), "skip_empty": True})
    assert not (tmp_path / "new.csv").exists()
    act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(path), "skip_empty": True})
    assert path.read_text(encoding="utf-8-sig").splitlines() == ["a", "1"]
    act("csv.export", empty, {"file": str(path)})  # off: as before, the column names only
    assert path.read_text(encoding="utf-8-sig").splitlines() == ["a"]
    act("csv.export", pl.DataFrame(), {"file": str(path), "skip_empty": True})  # no columns, no rows: not exported
    assert path.read_text(encoding="utf-8-sig").splitlines() == ["a"]
    act("csv.export", pl.DataFrame(), {"file": str(path)})  # an empty file, as before
    assert path.read_text(encoding="utf-8-sig").strip() == ""
    export = registry.actions["csv.export"]
    assert export.version == 2 and export.upgrade({"file": "a.csv"}, 1) == {"file": "a.csv", "skip_empty": False}

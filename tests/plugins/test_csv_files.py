import datetime as dt
import sys
from decimal import Decimal
from pathlib import Path

import polars as pl
import pytest

from tafl.api import ActionError
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

import datetime as dt
import sys
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
        "Betrag stays text: its numbers are too long to be kept exactly, e.g. '12.345.678.901.234.567,89€'",
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


def test_a_utf8_file_with_one_wrong_byte_stays_utf8(act, tmp_path):
    (tmp_path / "u.csv").write_bytes("Name\nMüller\nJürgen\n".encode() + b"Bad\xff\n")
    ctx = StubContext()
    df = act("csv.import", None, {"file": str(tmp_path / "u.csv")}, ctx)
    assert df["Name"].to_list() == ["Müller", "Jürgen", "Bad�"]
    assert ctx.warnings == ["u.csv: 1 character is not UTF-8, shown as �"]
    assert ctx.messages[-1] == ("info", "Encoding utf-8, delimiter comma, decimal point")


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
    export = registry.actions["csv.export"]
    assert export.version == 2 and export.upgrade({"file": "a.csv"}, 1) == {"file": "a.csv", "skip_empty": False}

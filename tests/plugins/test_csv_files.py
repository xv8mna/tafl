import datetime as dt

import polars as pl
import pytest

from etl_tool.api import ActionError
from etl_tool.api.testing import StubContext

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
    assert ("info", "Encoding cp1252, delimiter semicolon") in ctx.messages


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

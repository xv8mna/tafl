"""Importing all files of a folder (and its subfolders), several Excel sheets, Parquet files."""

import datetime as dt

import polars as pl
import pytest
import xlsxwriter

from tafl.api import ActionError, find_files, sources_fingerprint
from tafl.api.params import resolve
from tafl.api.testing import StubContext


@pytest.fixture
def lake(tmp_path):
    """Daily exports in month folders, as a simple data lake."""
    jan, feb = tmp_path / "2025" / "01", tmp_path / "2025" / "02"
    jan.mkdir(parents=True)
    feb.mkdir(parents=True)
    (jan / "orders_2025-01-02.csv").write_bytes("Nr;Kunde;Netto;Datum\n1;Müller;1,5;02.01.2025\n2;Bäcker;20;02.01.2025\n".encode("cp1252"))
    (jan / "orders_2025-01-10.csv").write_text("Nr,Kunde,Netto,Datum,Notiz\n3,Huber,7,10.01.2025,eilig\n", encoding="utf-8")
    (feb / "orders_2025-02-01.csv").write_text("Nr;Kunde;Netto;Datum\n4;Weiß;3,25;01.02.2025\n", encoding="utf-8")
    (tmp_path / "readme.txt").write_text("not an export", encoding="utf-8")
    (jan / "~$orders_2025-01-02.csv").write_text("lock", encoding="utf-8")
    return tmp_path


def folder(path, **options):
    return {"source": "folder", "folder": str(path), **options}


def test_find_files_matches_names_in_natural_order(tmp_path):
    for name in ("day10.csv", "day2.csv", "Day1.CSV", "notes.txt", "~$day3.csv"):
        (tmp_path / name).write_text("x", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "day4.csv").write_text("x", encoding="utf-8")
    assert [p.name for p in find_files(tmp_path, "*.csv")] == ["Day1.CSV", "day2.csv", "day10.csv"]
    assert [p.name for p in find_files(tmp_path, "*.csv; *.txt", subfolders=True)] == [
        "Day1.CSV", "day2.csv", "day10.csv", "notes.txt", "day4.csv",
    ]


def test_all_csv_files_of_a_data_lake(act, lake):
    out = act("csv.import", None, folder(lake, pattern="orders_*.csv", subfolders=True, path_column=True))
    assert out.columns == ["File", "Path", "Nr", "Kunde", "Netto", "Datum", "Notiz"]
    assert out["File"].to_list() == ["orders_2025-01-02.csv"] * 2 + ["orders_2025-01-10.csv", "orders_2025-02-01.csv"]
    assert out["Path"][3] == str(lake.joinpath("2025", "02", "orders_2025-02-01.csv").relative_to(lake))
    assert out["Kunde"].to_list() == ["Müller", "Bäcker", "Huber", "Weiß"]  # each file's own encoding
    assert out["Netto"].to_list() == [1.5, 20.0, 7.0, 3.25]  # types detected for all files together
    assert out["Datum"][0] == dt.date(2025, 1, 2)
    assert out["Notiz"].to_list() == [None, None, "eilig", None]  # only one file has it


def test_folder_without_matching_files(act, lake):
    with pytest.raises(ActionError, match=r"No files named orders_\*\.csv in"):
        act("csv.import", None, folder(lake, pattern="orders_*.csv"))  # they are in the subfolders
    with pytest.raises(ActionError, match="Folder not found"):
        act("csv.import", None, folder(lake / "missing"))


def test_one_file_works_as_before(act, lake):
    path = lake / "2025" / "02" / "orders_2025-02-01.csv"
    out = act("csv.import", None, {"file": str(path)})
    assert out.columns == ["Nr", "Kunde", "Netto", "Datum"] and out.height == 1


def test_new_files_are_noticed(registry, lake):
    action = registry.actions["csv.import"]
    params = resolve(action.params, folder(lake, pattern="*.csv", subfolders=True))
    before = sources_fingerprint(params, StubContext())
    (lake / "2025" / "02" / "orders_2025-02-02.csv").write_text("Nr;Kunde\n5;Neu\n", encoding="utf-8")
    assert sources_fingerprint(params, StubContext()) != before


def test_old_import_steps_are_upgraded(registry):
    for action_id in ("csv.import", "excel.import"):
        action = registry.actions[action_id]
        assert action.version == 2
        assert action.upgrade({"file": "a"}, 1)["source"] == "file"
    assert registry.actions["excel.import"].upgrade({"file": "a", "sheet": "X"}, 1)["sheets"] == "one"


@pytest.fixture
def workbooks(tmp_path):
    def write(name, sheets):
        book = xlsxwriter.Workbook(str(tmp_path / name))
        for sheet, rows in sheets.items():
            page = book.add_worksheet(sheet)
            for r, row in enumerate(rows):
                page.write_row(r, 0, row)
        book.close()

    write("a.xlsx", {"2025-01": [["Nr", "Netto"], [1, 10.5]], "2025-02": [["Nr", "Netto"], [2, 20]], "Summe": [["Total"], [30.5]]})
    write("b.xlsx", {"2025-03": [["Nr", "Netto", "Notiz"], [3, 7, "x"]]})
    return tmp_path


def test_excel_sheets_whose_names_match_in_all_files(act, workbooks):
    out = act("excel.import", None, folder(workbooks, sheets="matching", sheet_pattern="2025*"))
    assert out.columns == ["File", "Sheet", "Nr", "Netto", "Notiz"]
    assert out["Sheet"].to_list() == ["2025-01", "2025-02", "2025-03"]
    assert out["File"].to_list() == ["a.xlsx", "a.xlsx", "b.xlsx"]
    assert out["Nr"].to_list() == [1, 2, 3] and out.schema["Nr"] == pl.Int64  # whole numbers, all sheets


def test_all_sheets_of_one_excel_file(act, workbooks):
    out = act("excel.import", None, {"file": str(workbooks / "a.xlsx"), "sheets": "all"})
    assert out.columns == ["Sheet", "Nr", "Netto", "Total"]
    assert out["Sheet"].to_list() == ["2025-01", "2025-02", "Summe"]
    one = act("excel.import", None, {"file": str(workbooks / "a.xlsx"), "sheet": "summe"})
    assert one.columns == ["Total"]


def test_a_sheet_missing_in_one_file_of_a_folder(act, workbooks):
    with pytest.raises(ActionError, match="Sheet '2025-01' not found in b.xlsx"):
        act("excel.import", None, folder(workbooks, sheet="2025-01"))


def test_parquet_files_of_a_folder(act, tmp_path):
    (tmp_path / "day1").mkdir()
    pl.DataFrame({"Nr": [1, 2], "Netto": [1.5, 2.5]}).write_parquet(tmp_path / "day1" / "part-0.parquet")
    pl.DataFrame({"Nr": [3], "Kunde": ["Huber"]}).write_parquet(tmp_path / "part-1.parquet")
    out = act("parquet.import", None, folder(tmp_path, subfolders=True, file_column=False, path_column=True))
    assert out.columns == ["Path", "Nr", "Netto", "Kunde"]
    assert out["Nr"].to_list() == [1, 2, 3] and out["Kunde"].to_list() == [None, None, "Huber"]
    single = act("parquet.import", None, {"file": str(tmp_path / "part-1.parquet")})
    assert single.to_dicts() == [{"Nr": 3, "Kunde": "Huber"}]


def test_files_in_a_folder(act, lake):
    out = act("files.list", None, {"folder": str(lake), "pattern": "*.csv;*.txt", "subfolders": True})
    assert out.columns == ["File", "Path", "Folder", "Extension", "Size", "Modified", "Full path"]
    assert out["File"].to_list() == ["orders_2025-01-02.csv", "orders_2025-01-10.csv", "orders_2025-02-01.csv", "readme.txt"]
    assert out["Folder"][3] == "" and out["Extension"][3] == "txt" and out["Size"][3] == 13
    assert out.schema["Modified"] == pl.Datetime("us")
    empty = act("files.list", None, {"folder": str(lake), "pattern": "*.xlsx"})
    assert empty.height == 0 and empty.width == 7


def test_source_columns_never_replace_data_columns(act, tmp_path):
    (tmp_path / "x.csv").write_text("File;Wert\nmine;1\n", encoding="utf-8")
    out = act("csv.import", None, folder(tmp_path))
    assert out.columns == ["File (source)", "File", "Wert"] and out["File"][0] == "mine"

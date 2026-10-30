"""Importing all files of a folder (and its subfolders), several Excel sheets, Parquet files."""

import datetime as dt
import re

import polars as pl
import pytest
import xlsxwriter

from tafl.api import ActionError, Mode, SkipRest, find_files, sources_fingerprint
from tafl.api.files import in_folder
from tafl.api.params import resolve
from tafl.api.testing import StubContext
from tafl.core import Engine, Project, Status


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
        assert action.version == 3
        assert action.upgrade({"file": "a"}, 1)["source"] == "file"
        assert action.upgrade({"source": "folder", "folder": "in"}, 2)["no_files"] == "error"
    assert registry.actions["excel.import"].upgrade({"file": "a", "sheet": "X"}, 1)["sheets"] == "one"
    parquet = registry.actions["parquet.import"]
    assert parquet.version == 2 and parquet.upgrade({"source": "folder", "folder": "in"}, 1)["no_files"] == "error"


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


@pytest.mark.parametrize(
    ("action_id", "pattern"), [("csv.import", "*.csv"), ("excel.import", "*.xlsx"), ("parquet.import", "*.parquet")]
)
def test_a_folder_without_files_as_chosen(act, tmp_path, action_id, pattern):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    message = f"No files named {pattern} in {inbox}"
    with pytest.raises(ActionError, match=re.escape(message)):  # the default, as before
        act(action_id, None, folder(inbox, pattern=pattern))
    ctx = StubContext()
    empty = act(action_id, None, folder(inbox, pattern=pattern, no_files="empty", path_column=True), ctx)
    assert empty.schema == {"File": pl.String, "Path": pl.String} and empty.height == 0
    assert ctx.warnings == [f"{message}: the table is empty"]
    with pytest.raises(SkipRest) as skipped:
        act(action_id, None, folder(inbox, pattern=pattern, no_files="skip"))
    assert skipped.value.message == message and skipped.value.df.schema == {"File": pl.String}
    with pytest.raises(ActionError, match="Folder not found"):  # a folder that isn't there is still an error
        act(action_id, None, folder(tmp_path / "missing", no_files="empty"))


@pytest.mark.parametrize("no_files", ["empty", "skip"])
def test_daily_exports_on_a_day_without_files(registry, tmp_path, no_files):
    """Import the inbox, export, move the files to an archive: a run on a day without files is
    no error, and the export of the last day stays."""
    (tmp_path / "inbox" / "2026").mkdir(parents=True)
    (tmp_path / "inbox" / "2026" / "a.csv").write_text("Nr;Netto\n1;2,5\n", encoding="utf-8")
    project = Project(name="Daily")
    project.path = tmp_path / "daily.tafl"
    table = project.add_table("Daily")
    source = folder("inbox", pattern="*.csv", subfolders=True, path_column=True, no_files=no_files)
    project.add_step(table, registry.actions["csv.import"], source)
    project.add_step(table, registry.actions["csv.export"], {"file": "out/daily.csv", "skip_empty": True})
    project.add_step(table, registry.actions["files.move"], {"column": "Path", "base": "inbox", "target": "archive"})
    first = Engine(project, registry).run(mode=Mode.RUN)[table.id]
    assert first.ok and (tmp_path / "archive" / "a.csv").exists()
    exported = (tmp_path / "out" / "daily.csv").read_bytes()

    for mode in (Mode.RUN, Mode.PREVIEW):  # the inbox is empty now
        result = Engine(project, registry).run(mode=mode)[table.id]
        assert result.ok and result.df.columns == ["File", "Path"] and result.df.height == 0
        if no_files == "empty":
            assert result.steps[0].messages[0][0] == "warning"
            if mode == Mode.RUN:
                assert result.steps[1].messages == [("info", "Not exported: the table is empty")]
        else:
            assert [s.status for s in result.steps] == [Status.OK, Status.SKIPPED, Status.SKIPPED]
    assert (tmp_path / "out" / "daily.csv").read_bytes() == exported


def test_in_folder_answers_as_find_files_does(tmp_path):
    (tmp_path / "sub").mkdir()
    for name in ("a.csv", "B.CSV", "c.txt", "~$d.csv", "sub/e.csv"):
        (tmp_path / name).touch()
    paths = [p for p in tmp_path.rglob("*") if p.is_file()]
    for patterns, subfolders in (("*.csv", False), ("*.csv; *.txt", True), ("", True), ("a*", False)):
        found = find_files(tmp_path, patterns, subfolders)
        assert [p for p in paths if in_folder(p, tmp_path, patterns, subfolders)] == [p for p in paths if p in found]

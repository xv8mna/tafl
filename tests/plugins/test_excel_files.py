import datetime as dt

import openpyxl
import polars as pl
import pytest
import xlsxwriter

from etl_tool.api import ActionError


@pytest.fixture
def workbook(tmp_path):
    path = tmp_path / "report.xlsx"
    wb = xlsxwriter.Workbook(path)
    date_format = wb.add_format({"num_format": "dd.mm.yyyy"})
    ws = wb.add_worksheet("Daten")
    ws.write("A1", "Monthly report")
    ws.write_row("A3", ["Kunde", "Menge", "Preis", "Datum", None, "Notiz"])
    ws.write_row("A4", ["Anna", 3, 9.99])
    ws.write_datetime("D4", dt.datetime(2025, 1, 31), date_format)
    ws.write("F4", "urgent")
    ws.write_row("A5", ["Ben", 5, 1.5])
    ws.write_datetime("D5", dt.datetime(2025, 2, 1), date_format)
    ws.write("F5", 12)
    wb.add_worksheet("Other").write_row("A1", ["x"])
    wb.close()
    return path


def test_import_sheet_with_header_further_down(act, workbook):
    df = act("excel.import", None, {"file": str(workbook), "sheet": "daten", "header_row": 3})
    assert df.columns == ["Kunde", "Menge", "Preis", "Datum", "Notiz"]  # empty unnamed column dropped
    assert df.schema == {"Kunde": pl.String, "Menge": pl.Int64, "Preis": pl.Float64, "Datum": pl.Date, "Notiz": pl.String}
    assert df["Datum"].to_list() == [dt.date(2025, 1, 31), dt.date(2025, 2, 1)]
    assert df["Notiz"].to_list() == ["urgent", "12"]


def test_unknown_sheet_lists_the_sheets(act, workbook):
    with pytest.raises(ActionError, match="Sheets: Daten, Other"):
        act("excel.import", None, {"file": str(workbook), "sheet": "Nope"})


def test_export_with_formats(act, tmp_path):
    df = pl.DataFrame(
        {
            "Name": ["Anna", "Ben"],
            "Qty": [3, 5],
            "Price": [1234.5, 0.25],
            "Rate": [0.19, 0.07],
            "Day": [dt.date(2025, 12, 31), None],
        }
    )
    path = tmp_path / "out.xlsx"
    act(
        "excel.export",
        df,
        {"file": str(path), "sheet": "Orders", "column_formats": [{"column": "Rate", "format": "0%"}]},
    )
    ws = openpyxl.load_workbook(path)["Orders"]
    assert [c.value for c in ws[1]] == ["Name", "Qty", "Price", "Rate", "Day"]
    assert ws["A1"].font.b
    assert ws.freeze_panes == "A2"
    assert ws["B2"].number_format == "0"
    assert ws["C2"].number_format == "#,##0.00"
    assert ws["D2"].number_format == "0%"
    assert ws["E2"].number_format == "dd.mm.yyyy"
    assert ws["E2"].value == dt.datetime(2025, 12, 31)


def test_export_checks_file_and_sheet_names(act, tmp_path):
    df = pl.DataFrame({"a": [1]})
    with pytest.raises(ActionError, match=r"\.xlsx"):
        act("excel.export", df, {"file": str(tmp_path / "out.xls")})
    with pytest.raises(ActionError, match="31 characters"):
        act("excel.export", df, {"file": str(tmp_path / "out.xlsx"), "sheet": "a/b"})

import datetime as dt
import decimal
import logging
import subprocess
import sys
from pathlib import Path

import openpyxl
import polars as pl
import pytest
import xlsxwriter

from tafl.api import ActionError
from tafl.api.testing import StubContext


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


@pytest.fixture
def edges(tmp_path):
    """A workbook as other programs write it: merged cells, formulas without saved results,
    cells with only a time."""
    path = tmp_path / "edges.xlsx"
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.title = "S"
    sheet.append(["Region", "Name", "Summe", "Zeit"])
    for row in (["Nord", "a", "=1+1", dt.time(9, 30)], [None, "b", "=1/0", dt.time(10, 0)], ["Süd", "c", "=2+2", dt.time(11, 15)]):
        sheet.append(row)
    sheet.merge_cells("A2:A3")
    book.save(path)
    return path


def test_import_cells_with_only_a_time(act, edges, caplog):
    with caplog.at_level(logging.WARNING):
        df = act("excel.import", None, {"file": str(edges)})
    assert df.schema["Zeit"] == pl.Time
    assert df["Zeit"].to_list() == [dt.time(9, 30), dt.time(10, 0), dt.time(11, 15)]
    assert df["Region"].to_list() == ["Nord", None, "Süd"]  # merged cells: the value is in the first cell only
    assert df["Summe"].to_list() == [None, None, None]  # formulas without saved results
    assert not [r for r in caplog.records if r.name.startswith("fastexcel")]  # its note on the console about them


def test_a_header_row_below_the_data(act, edges):
    ctx = StubContext()
    df = act("excel.import", None, {"file": str(edges), "header_row": 100}, ctx)
    assert df.shape == (0, 0)
    assert ctx.warnings == ["Sheet 'S' of edges.xlsx has nothing in row 100 (the row with the column names) or below"]


def test_export_binary_values_decimals_and_long_texts(act, tmp_path):
    df = pl.DataFrame(
        {
            "data": [b"\x01\x02\xff", None],
            "price": pl.Series([decimal.Decimal("1.50"), decimal.Decimal("2.25")], dtype=pl.Decimal(10, 2)),
            "text": ["x" * 40_000, "short"],
        }
    )
    ctx = StubContext()
    path = tmp_path / "out.xlsx"
    act("excel.export", df, {"file": str(path)}, ctx)
    ws = openpyxl.load_workbook(path).active
    assert [ws["A2"].value, ws["A3"].value] == ["0x0102FF", None]
    assert ws["B2"].value == 1.5 and ws["B2"].number_format == "#,##0.00"
    assert len(ws["C2"].value) == 32_767
    assert ctx.warnings == ["1 text was cut to 32,767 characters, the most an Excel cell holds"]


@pytest.mark.parametrize("existing", ["file", "sheet"])
def test_exact_decimals_without_decimal_places_get_the_whole_number_format(act, tmp_path, existing):
    """Every exact decimal got the decimal number format, so an ID of a decimal(30, 0) from a
    database showed as 12,345,678,901,234,600,000,000.00. Those without decimal places get the
    whole number format now, in a new workbook and in one sheet of an existing one; a format for
    the single column still comes first."""
    D = decimal.Decimal
    df = pl.DataFrame(
        {
            "Id": pl.Series([D("12345678901234567890123"), D("5")], dtype=pl.Decimal(30, 0)),
            "Code": pl.Series([D("17"), D("5")], dtype=pl.Decimal(5, 0)),
            "Amount": pl.Series([D("1.50"), D("2.25")], dtype=pl.Decimal(10, 2)),
            "Qty": [3, 5],
        }
    )
    path = tmp_path / "ids.xlsx"
    if existing == "sheet":
        act("excel.export", pl.DataFrame({"x": [1]}), {"file": str(path), "sheet": "Other"}, StubContext(mode="run"))
    params = {"file": str(path), "sheet": "Ids", "existing": existing}
    act("excel.export", df, params, StubContext(mode="run"))
    cells = openpyxl.load_workbook(path)["Ids"]
    assert [cells[f"{c}2"].number_format for c in "ABCD"] == ["0", "0", "#,##0.00", "0"]
    assert [cells["B2"].value, cells["C3"].value] == [17, 2.25]
    formats = {"integer_format": "#,##0", "column_formats": [{"column": "Code", "format": "00000"}]}
    act("excel.export", df, params | formats, StubContext(mode="run"))
    cells = openpyxl.load_workbook(path)["Ids"]
    assert [cells[f"{c}2"].number_format for c in "ABCD"] == ["#,##0", "00000", "#,##0.00", "#,##0"]


@pytest.mark.parametrize("existing", ["file", "sheet"])
def test_numbers_that_excel_rounds_are_counted(act, tmp_path, existing):
    """Excel keeps 15 significant digits: 9007199254740993 became 9007199254740992 without a
    word. Whole numbers and exact decimals stay numbers, and the step says how many of them each
    column has, in a new workbook and in one sheet of an existing one."""
    D = decimal.Decimal
    df = pl.DataFrame({
        "Id": pl.Series([9007199254740993, 9223372036854775807, 5, 10**18, None, -1234567890123456], dtype=pl.Int64),
        "Big": pl.Series([2**64 - 1, 7, 7, 7, 7, 7], dtype=pl.UInt64),
        "Amount": pl.Series([D("12345678901234567890.1234"), D("1.5"), D("123456789012345"), None, None, None], dtype=pl.Decimal(38, 4)),
        "Small": pl.Series([D("12345678.12")] * 6, dtype=pl.Decimal(10, 2)),  # at most 10 digits
        "Ratio": [0.1 + 0.2, 1e300, 2 / 3, 1.0, None, 123456789012345680.0],  # decimal numbers: Excel's own
    })  # fmt: skip
    path = tmp_path / "numbers.xlsx"
    if existing == "sheet":
        act("excel.export", pl.DataFrame({"x": [1]}), {"file": str(path), "sheet": "Other"}, StubContext(mode="run"))
    ctx = StubContext(mode="run")
    act("excel.export", df, {"file": str(path), "sheet": "Numbers", "existing": existing}, ctx)
    assert ctx.warnings == [
        "Excel keeps 15 significant digits of a number, and rounds longer ones: Id has 3 (e.g. 9007199254740993), Big has 1 "
        "(e.g. 18446744073709551615) and Amount has 1 (e.g. 12345678901234567890.1234). To keep all their digits, change "
        "these columns to text before this step (Change type / format)."
    ]
    cells = openpyxl.load_workbook(path)["Numbers"]
    assert [cells["A2"].value, cells["B2"].value, cells["C2"].value] == [9007199254740992, 1.844674407370955e19, 1.234567890123457e19]
    ctx = StubContext(mode="run")
    act("excel.export", df.select("Id", "Small", "Ratio").with_columns(pl.col("Id").cast(pl.String)), {"file": str(path), "sheet": "Numbers", "existing": existing}, ctx)
    assert not ctx.warnings and openpyxl.load_workbook(path)["Numbers"]["A2"].value == "9007199254740993"
    six = pl.DataFrame({f"N{i}": [10**17 + 1] for i in range(6)})
    ctx = StubContext(mode="run")
    act("excel.export", six, {"file": str(tmp_path / "six.xlsx")}, ctx)
    assert "N4 has 1 (e.g. 100000000000000001) and 1 more column has some. To keep" in ctx.warnings[0]
    ctx = StubContext(mode="run")
    act("excel.export", df.slice(1, 1).select("Id"), {"file": str(path), "sheet": "Numbers"}, ctx)
    assert ctx.warnings == [
        "Excel keeps 15 significant digits of a number, and rounds longer ones: Id has 1 (e.g. 9223372036854775807). To keep "
        "all their digits, change this column to text before this step (Change type / format)."
    ]


_WIDE_NUMBERS = """
import sys

import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
df = pl.DataFrame({f"c{i}": [9007199254740993, i] for i in range(4_001)})
ctx = StubContext(mode="run")
registry.actions["excel.export"](df, {"file": sys.argv[1]}, ctx)
assert ctx.warnings == [
    "Excel keeps 15 significant digits of a number, and rounds longer ones: "
    + ", ".join(f"c{i} has 1 (e.g. 9007199254740993)" for i in range(5))
    + " and 3,996 more columns have some. To keep all their digits, change these columns to text before this step "
    "(Change type / format)."
], ctx.warnings
"""


_WIDE_IDS = """
import sys
from decimal import Decimal

import openpyxl
import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
df = pl.DataFrame({f"c{i}": pl.Series([Decimal(i)], dtype=pl.Decimal(20 + i % 2, 0)) for i in range(4_001)})
registry.actions["excel.export"](df, {"file": sys.argv[1]}, StubContext(mode="run"))
cells = openpyxl.load_workbook(sys.argv[1], read_only=True).active
row = next(cells.iter_rows(min_row=2, max_row=2))
assert [cell.value for cell in row] == list(range(4_001)) and {cell.number_format for cell in row} == {"0"}, row[:3]
"""


def test_exact_decimals_without_decimal_places_of_a_wide_table_get_the_whole_number_format(tmp_path):
    """4,001 columns of two types of exact decimals without decimal places: each type gets the
    whole number format (in a process of its own, so that an end of the process shows as a failed
    test)."""
    done = subprocess.run([sys.executable, "-c", _WIDE_IDS, str(tmp_path / "ids.xlsx")], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


def test_numbers_that_excel_rounds_are_counted_in_a_wide_table(tmp_path):
    """4,001 columns of whole numbers: the count looks at all of them at once, and a deep Polars
    expression would end the process (in a process of its own, so that such an end shows as a
    failed test)."""
    done = subprocess.run([sys.executable, "-c", _WIDE_NUMBERS, str(tmp_path / "wide.xlsx")], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


class _Point:
    """A value of a plugin's own type."""

    def __str__(self) -> str:
        return "POINT (8.5 47.4)"


def test_export_lists_records_and_values_of_a_plugins_own_type_as_text(act, tmp_path):
    df = pl.DataFrame(
        [
            pl.Series("tags", [["red", "blue"], None, ["ab"] * 6_000]),
            pl.Series("size", [{"w": 1, "h": 2}, None, None]),
            pl.Series("place", [_Point(), None, None], dtype=pl.Object),
        ]
    )
    ctx = StubContext()
    path = tmp_path / "out.xlsx"
    act("excel.export", df, {"file": str(path)}, ctx)
    rows = [[cell.value for cell in row] for row in openpyxl.load_workbook(path).active.iter_rows()]
    assert rows[:3] == [["tags", "size", "place"], ["['red', 'blue']", "{'w': 1, 'h': 2}", "POINT (8.5 47.4)"], [None, None, None]]
    assert rows[3][0] == str(["ab"] * 6_000)[:32_767]
    assert ctx.warnings == ["1 text was cut to 32,767 characters, the most an Excel cell holds"]


def test_export_more_rows_than_a_sheet_holds(act, tmp_path):
    with pytest.raises(ActionError, match=r"The table has 1,048,576 rows, but an Excel sheet holds at most 1,048,575"):
        act("excel.export", pl.DataFrame({"a": range(1_048_576)}), {"file": str(tmp_path / "big.xlsx")})
    assert not list(tmp_path.iterdir())


def test_the_help_fits_both_choices_for_an_existing_file(registry):
    existing = next(p for p in registry.actions["excel.export"].params if p.key == "existing")
    assert existing.help.startswith("Replace the whole file: a new workbook with only this sheet. Replace only this sheet:")


def test_dont_export_an_empty_table(act, registry, tmp_path):
    path, empty = tmp_path / "out.xlsx", pl.DataFrame(schema={"a": pl.Int64})
    act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(path)})
    before = path.read_bytes()
    for existing in ("file", "sheet"):
        ctx = StubContext()
        act("excel.export", empty, {"file": str(path), "existing": existing, "skip_empty": True}, ctx)
        assert path.read_bytes() == before and ctx.messages == [("info", "Not exported: the table is empty")]
    act("excel.export", empty, {"file": str(tmp_path / "new.xlsx"), "skip_empty": True})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.xlsx"]
    act("excel.export", empty, {"file": str(path)})  # off: as before, the header only
    assert [[c.value for c in row] for row in openpyxl.load_workbook(path).active] == [["a"]]
    export = registry.actions["excel.export"]
    assert export.version == 3 and export.upgrade({"file": "a.xlsx"}, 2) == {"file": "a.xlsx", "skip_empty": False}
    assert export.upgrade({"file": "a.xlsx"}, 1) == {"file": "a.xlsx", "existing": "file", "skip_empty": False}


def test_a_table_without_columns_gives_an_empty_sheet(act, tmp_path):
    """As the CSV export gives an empty file, e.g. for a folder import without files and without
    its File column."""
    path = tmp_path / "out.xlsx"
    ctx = StubContext()
    act("excel.export", pl.DataFrame(), {"file": str(path), "sheet": "Data"}, ctx)
    book = openpyxl.load_workbook(path)
    assert book.sheetnames == ["Data"] and book["Data"].max_column == 1 and book["Data"]["A1"].value is None
    assert ctx.messages == [("info", f"Wrote 0 rows to {path} (sheet 'Data')")]
    act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(path), "sheet": "Other", "existing": "sheet"})
    act("excel.export", pl.DataFrame(), {"file": str(path), "sheet": "Other", "existing": "sheet"})
    book = openpyxl.load_workbook(path)
    assert book.sheetnames == ["Data", "Other"] and book["Other"]["A1"].value is None


def test_a_failed_export_leaves_the_old_file(act, tmp_path, monkeypatch):
    path = tmp_path / "out.xlsx"
    act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(path)})
    before = path.read_bytes()

    def broken(self, workbook=None, **options):
        Path(workbook.filename).write_bytes(b"PK")
        raise pl.exceptions.InvalidOperationError("broken")

    monkeypatch.setattr(pl.DataFrame, "write_excel", broken)
    with pytest.raises(pl.exceptions.InvalidOperationError):
        act("excel.export", pl.DataFrame({"a": [2]}), {"file": str(path)})
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["out.xlsx"]  # no temporary file left


@pytest.mark.skipif(sys.platform != "win32", reason="Windows locks open files")
def test_export_to_a_workbook_that_is_open(act, tmp_path):
    path = tmp_path / "out.xlsx"
    act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(path)})
    before = path.read_bytes()
    with open(path, "rb"), pytest.raises(ActionError, match=r"out.xlsx is open in another program \(e.g. Excel\)"):
        act("excel.export", pl.DataFrame({"a": [2]}), {"file": str(path)})
    assert path.read_bytes() == before
    assert [p.name for p in tmp_path.iterdir()] == ["out.xlsx"]

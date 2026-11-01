"""Excel export keeps text as text: on every path, each text is a string cell, exactly as it is.
No formula, no link, no raw XML and no empty cell comes from a text, and these tests look at the
parts of the file for that."""

import random
import re
import warnings
import zipfile

import fastexcel
import openpyxl
import polars as pl
import pytest
import xlsxwriter
from lxml import etree

from tafl.api.testing import StubContext

MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
OFFICE = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
PATHS = ("new file", "whole file replaced", "sheet replaced", "sheet added", "xlsm", "formatted")

NEXT_CELLS = (  # XML that would close its cell and go on with other cells
    '<r><t>one</t></r></is></c><c r="C2"><f>Report!A1+1</f></c><c r="D2" t="inlineStr"><is><r><t>end</t></r>'
)
FORMULAS = ["=1+1", "{=1+1}", '{=HYPERLINK("http://127.0.0.1:9/a","text")}', "{=", "{=}"]
LINKS = [
    "http://127.0.0.1:9/a",
    "https://127.0.0.1:9/b",
    "ftp://127.0.0.1:9/c",
    "mailto:nobody@127.0.0.1",
    r"external:\\127.0.0.1\share\x.xlsx",
    r"external:C:\Windows\win.ini",
    "internal:Sheet1!A1",
    "file:///C:/Windows/win.ini",
]
RICH = [
    "<r>abc</r>",
    "<r>a&b</r>",
    "<r><t>x</t></r>",
    "<r></r>",
    "<r><t>A</t></r></si><si><r><t>B</t></r>",
    "<r><t>a</r>",
    '<r><rPr><color rgb="FFFFFFFF"/><sz val="1"/></rPr><t>tiny white</t></r>',
    "<r>line\nbreak</r>",
    "<r>  spaces  </r>",
    "<r>\x01</r>",
    "<r>_x0041_</r>",
    NEXT_CELLS,
    "<r>",
    "</r>",
    "plain <r>x</r>",
    "<R>x</R>",
]
ESCAPES = [
    "_x0041_",
    "_x0041_x0042_",
    "a_x0041_x0042_b",
    "poster_x1200_x1600_final",
    "_x0041__x0042_",
    "\x01_x0041_",
    "Serial _x0041\x01 end",
    "_x000D\x1f",
    "_x005F_x0041_",
]
PLAIN = ["plain", "Müller & <Co>", "Tom's", '"Gold"', "a < b", " leading", "trailing ", "line\nbreak", "line\r\nbreak", "\ttab", "€ÄÖ"]
TEXTS = [*FORMULAS, *LINKS, *RICH, *ESCAPES, *PLAIN]
NAMES = [
    "<r>x</r>",
    "<r>a&b</r>",
    "<r><t>x</t></r>",
    "<r><t>A</t></r></si><si><r><t>B</t></r>",
    NEXT_CELLS,
    "{=1+1}",
    "=1+1",
    "http://127.0.0.1:9/a",
    "mailto:nobody@127.0.0.1",
    "a\x01b",
    "_x0041_",
    "_x0041_x0042_",
    "Müller & <Co>",
    "[x]",
]


def _old_workbook(path, macros=None) -> None:
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Report").write_formula("A1", "=SUM(1,2)", None, 3)
    book.add_worksheet("Data").write_row("A1", ["old", "rows"])
    if macros is not None:
        book.add_vba_project(str(macros))
    book.close()


def _export(act, how, folder, frame, ctx=None):
    """Exports the frame by one of the paths; gives the file and the name of the sheet."""
    path = folder / ("book.xlsm" if how == "xlsm" else "book.xlsx")
    params = {"file": str(path), "sheet": "Data"}
    if how == "whole file replaced":
        _old_workbook(path)
    elif how == "sheet replaced":
        _old_workbook(path)
        params["existing"] = "sheet"
    elif how == "sheet added":
        _old_workbook(path)
        params |= {"existing": "sheet", "sheet": "Fresh"}
    elif how == "xlsm":
        macros = folder / "vbaProject.bin"
        macros.write_bytes(b"not a real macro project, it only has to stay")
        _old_workbook(path, macros)
        params["existing"] = "sheet"
    elif how == "formatted":
        formats = [{"column": name, "format": "@"} for name in frame.columns]
        params |= {"table_style": "Table Style Medium 2", "autofit": False, "bold_header": False, "column_formats": formats}
    act("excel.export", frame, params, ctx)
    return path, params["sheet"]


def _sheet(path, name) -> tuple[etree._Element, list[etree._Element]]:
    """The sheet's XML and its relationships. All XML parts of the file must be well formed."""
    with zipfile.ZipFile(path) as package:
        parts = {part: package.read(part) for part in package.namelist() if part.endswith((".xml", ".rels"))}
    xml = {part: etree.fromstring(content) for part, content in parts.items()}
    entry = next(e for e in xml["xl/workbook.xml"].iter(f"{MAIN}sheet") if e.get("name") == name)
    relation = next(r for r in xml["xl/_rels/workbook.xml.rels"] if r.get("Id") == entry.get(f"{OFFICE}id"))
    part = relation.get("Target").lstrip("/").removeprefix("xl/")
    relations = xml.get(f"xl/worksheets/_rels/{part.rsplit('/', 1)[1]}.rels")
    return xml[f"xl/{part}"], [] if relations is None else list(relations)


def _assert_only_strings(path, name) -> None:
    """No formula, no link: each cell that holds something is a string."""
    sheet, relations = _sheet(path, name)
    assert not list(sheet.iter(f"{MAIN}f"))
    assert not list(sheet.iter(f"{MAIN}hyperlink"))
    assert [r for r in relations if r.get("TargetMode") == "External"] == []
    kinds = {cell.get("t") for cell in sheet.iter(f"{MAIN}c") if len(cell)}
    assert kinds <= {"s", "inlineStr"}


def _read(path, name) -> pl.DataFrame:
    return fastexcel.read_excel(str(path)).load_sheet(name, whitespace_as_null=False, schema_sample_rows=None).to_polars()


def _texts(*columns) -> pl.DataFrame:
    return pl.DataFrame({f"c{i}": column for i, column in enumerate(columns)}, schema={f"c{i}": pl.String for i in range(len(columns))})


@pytest.mark.parametrize("how", PATHS)
def test_each_text_stays_a_string_exactly(act, tmp_path, how):
    path, sheet = _export(act, how, tmp_path, _texts(TEXTS, TEXTS[::-1]))
    _assert_only_strings(path, sheet)
    back = _read(path, sheet)
    assert back["c0"].to_list() == TEXTS and back["c1"].to_list() == TEXTS[::-1]


@pytest.mark.parametrize("how", PATHS)
def test_each_column_name_stays_a_string_exactly(act, tmp_path, how):
    frame = pl.DataFrame({name: [f"value {i}"] for i, name in enumerate(NAMES)})
    path, sheet = _export(act, how, tmp_path, frame)
    _assert_only_strings(path, sheet)
    back = _read(path, sheet)
    assert back.columns == NAMES and back.row(0) == frame.row(0)


def test_a_column_name_like_a_pattern_is_only_a_name(act, tmp_path):
    frame = pl.DataFrame({"^x$": [1], "^a.*$": [2], "ab": [3], "*": [4]})
    path, sheet = _export(act, "new file", tmp_path, frame)
    assert _read(path, sheet).columns == frame.columns and _read(path, sheet).row(0) == (1, 2, 3, 4)


@pytest.mark.parametrize("how", ["new file", "sheet replaced"])
def test_a_table_without_rows_has_its_column_names_too(act, tmp_path, how):
    frame = pl.DataFrame(schema={"<r>a&b</r>": pl.String, "{=1+1}": pl.Int64})
    path, sheet = _export(act, how, tmp_path, frame)
    _assert_only_strings(path, sheet)
    assert [c.value for c in openpyxl.load_workbook(path)[sheet][1]] == frame.columns


@pytest.mark.parametrize("how", ["new file", "sheet replaced", "formatted"])
def test_the_excel_table_has_the_names_of_the_header_cells(act, tmp_path, how):
    """Excel repairs a workbook in which an Excel table and its header cells differ."""
    frame = pl.DataFrame({name: [1] for name in NAMES})
    path, _ = _export(act, how, tmp_path, frame)
    with zipfile.ZipFile(path) as package:
        (part,) = [p for p in package.namelist() if re.fullmatch(r"xl/tables/table\d+\.xml", p)]
        table = etree.fromstring(package.read(part))
    names = [column.get("name") for column in table.iter(f"{MAIN}tableColumn")]
    assert [re.sub("_x([0-9A-F]{4})_", lambda found: chr(int(found[1], 16)), name) for name in names] == NAMES


def test_escapes_in_a_column_name_are_read_as_excel_reads_the_cell(act, tmp_path):
    path, _ = _export(act, "new file", tmp_path, pl.DataFrame({"_x0041_x0042_": [1], "a\x01b": [2]}))
    with zipfile.ZipFile(path) as package:
        table = package.read("xl/tables/table1.xml").decode()
    assert 'name="_x005F_x0041_x005F_x0042_"' in table and 'name="a_x0001_b"' in table


def test_a_format_for_a_column_reaches_a_column_with_such_a_name(act, tmp_path):
    name = "<r>a&b</r>"
    frame = pl.DataFrame({name: [1.5], "other": [2.5]})
    path = tmp_path / "book.xlsx"
    act("excel.export", frame, {"file": str(path), "sheet": "Data", "column_formats": [{"column": name, "format": "0.000"}]})
    sheet = openpyxl.load_workbook(path)["Data"]
    assert sheet["A1"].value == name and sheet["A2"].number_format == "0.000" and sheet["B2"].number_format == "#,##0.00"
    assert sheet["A1"].font.b


@pytest.mark.parametrize("how", PATHS)
def test_no_url_gets_lost(act, tmp_path, how):
    """A sheet holds 65,530 links and a link 2,079 characters, and xlsxwriter leaves an empty cell
    after that. As text, they are all there."""
    long = "http://127.0.0.1:9/" + "x" * 2_100
    urls = [f"http://127.0.0.1:9/{number}" for number in range(65_540)] + [long]
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        path, sheet = _export(act, how, tmp_path, _texts(urls))
    _assert_only_strings(path, sheet)
    assert _read(path, sheet)["c0"].to_list() == urls


LONGEST = "<r>" + "&" * (32_767 - 7) + "</r>"


@pytest.mark.parametrize("how", PATHS)
def test_the_cut_comes_before_the_wrap(act, tmp_path, how):
    """The XML of the longest cell is longer than the text, and it must stay whole."""
    too_long = "<r>" + "&" * 40_000 + "</r>"
    cut_at_the_end_tag = LONGEST + "the rest"
    ctx = StubContext()
    path, sheet = _export(act, how, tmp_path, _texts([LONGEST, too_long, cut_at_the_end_tag, "x" * 40_000]), ctx)
    _assert_only_strings(path, sheet)
    assert _read(path, sheet)["c0"].to_list() == [LONGEST, too_long[:32_767], LONGEST, "x" * 32_767]
    assert ctx.warnings == ["3 texts were cut to 32,767 characters, the most an Excel cell holds"]


def test_a_very_long_column_name_is_cut_in_the_cell_and_in_the_table(act, tmp_path):
    name = "<r>" + "&" * 40_000 + "</r>"
    path, sheet = _export(act, "new file", tmp_path, pl.DataFrame({name: [1], LONGEST: [2]}))
    assert _read(path, sheet).columns == [name[:32_767], LONGEST]
    with zipfile.ZipFile(path) as package:
        names = [c.get("name") for c in etree.fromstring(package.read("xl/tables/table1.xml")).iter(f"{MAIN}tableColumn")]
    assert names == [name[:32_767], LONGEST]


@pytest.mark.parametrize("how", PATHS)
def test_an_empty_text_is_an_empty_cell(act, tmp_path, how):
    path, sheet = _export(act, how, tmp_path, _texts(["", "x", None, "y"]))
    assert _read(path, sheet)["c0"].to_list() == [None, "x", None, "y"]


@pytest.mark.parametrize("how", ["new file", "sheet replaced"])
def test_columns_that_become_text_stay_text(act, tmp_path, how):
    """Categories, and lists and structures, which the export writes as text."""
    frame = pl.DataFrame(
        {
            "category": pl.Series(["{=1+1}", "http://127.0.0.1:9/a"], dtype=pl.Categorical),
            "list": [["=1"], ["http://127.0.0.1:9/a"]],
            "structure": [{"a": "{=1+1}"}, {"a": "x"}],
        }
    )
    path, sheet = _export(act, how, tmp_path, frame)
    _assert_only_strings(path, sheet)
    back = _read(path, sheet)
    assert back["category"].to_list() == ["{=1+1}", "http://127.0.0.1:9/a"]
    assert back["list"].to_list() == ["['=1']", "['http://127.0.0.1:9/a']"]
    assert back["structure"].to_list() == ["{'a': '{=1+1}'}", "{'a': 'x'}"]


@pytest.mark.parametrize("how", ["new file", "sheet replaced"])
def test_numbers_and_dates_beside_the_texts_stay_what_they_are(act, tmp_path, how):
    frame = pl.DataFrame({"text": ["{=1+1}"], "number": [1.5], "whole": [3], "yes": [True]})
    path, sheet = _export(act, how, tmp_path, frame)
    assert _read(path, sheet).row(0) == ("{=1+1}", 1.5, 3, True)


def test_the_other_sheets_stay_as_they_were(act, tmp_path):
    path, _ = _export(act, "sheet replaced", tmp_path, _texts(TEXTS))
    book = openpyxl.load_workbook(path)
    assert book["Report"]["A1"].value == "=SUM(1,2)" and book.sheetnames == ["Report", "Data"]


_PIECES = ["<r>", "</r>", "<t>", "</t>", "</si>", "<si>", "</is>", "&", "&amp;", "_x0041_", "_x", "0041", "_", "x", "\x01", "\x1f", "\n", "\r"]
_PIECES += [" ", "=", "{=", "{", "}", "http://", "mailto:", "é", '"', "'", "a", "1"]


@pytest.mark.parametrize("how", ["new file", "sheet replaced"])
def test_random_pieces_read_back_exactly(act, tmp_path, how):
    chance = random.Random(61)
    texts = list(dict.fromkeys("".join(chance.choices(_PIECES, k=chance.randint(1, 8))) for _ in range(3_000)))
    texts += [f"<r>{text}</r>" for text in texts[:500]]
    path, sheet = _export(act, how, tmp_path, _texts(texts))
    _assert_only_strings(path, sheet)
    assert _read(path, sheet)["c0"].to_list() == texts

"""Excel export into an existing workbook: only one sheet is replaced, the rest stays as it is."""

import base64
import datetime as dt
import io
import posixpath
import random
import re
import sys
import time
import zipfile

import openpyxl
import polars as pl
import pytest
import xlsxwriter
from lxml import etree

from tafl.api import ActionError
from tafl.api.testing import StubContext
from tafl.api.xlsx import _CHART_FORMULA, _follow, _without_stand_ins

DATA = pl.DataFrame(
    {
        "Customer": ["Huber", "Müller & <Co>", "=not a formula"],
        "Day": [dt.date(2025, 1, 2), dt.date(2025, 1, 3), None],
        "Net": [10.5, float("nan"), 3.25],
        "Orders": [1, 2, 3],
    }
)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")
MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
RELATIONSHIP = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/"


@pytest.fixture
def report(tmp_path):
    """A report workbook: a sheet with formulas, a chart, a picture, a shape and a comment; the
    data sheet with the Excel table Sales and named ranges on it; a notes sheet. Plus a pivot
    table's data source on the data sheet and Excel's list of formula cells."""
    path = tmp_path / "report.xlsx"
    book = xlsxwriter.Workbook(path)
    page, data, notes = book.add_worksheet("Report"), book.add_worksheet("Data"), book.add_worksheet("Notes")
    page.write("A1", "Sales report", book.add_format({"bold": True, "font_size": 16, "bg_color": "yellow"}))
    page.write_formula("A2", "=SUM(Sales[Net])", None, 10)
    page.write_comment("A1", "made by hand")
    data.add_table(
        "A1:B5",
        {
            "name": "Sales",
            "columns": [{"header": "Customer"}, {"header": "Net"}],
            "data": [["old A", 1], ["old B", 2], ["old C", 3], ["old D", 4]],
        },
    )
    data.set_tab_color("red")
    chart = book.add_chart({"type": "column"})
    chart.add_series({"categories": "=Data!$A$2:$A$5", "values": "=Data!$B$2:$B$5", "name": "=Data!$B$1"})
    page.insert_chart("D2", chart)
    page.insert_image("D20", "dot.png", {"image_data": io.BytesIO(PNG)})
    page.insert_textbox("H2", "A note in a shape")
    book.define_name("SalesData", "=Data!$A$1:$B$5")
    book.define_name("Nets", "=Data!$B$2:$B$5")
    notes.write("A1", "keep me")
    book.close()
    _add_parts(
        path,
        {
            "xl/pivotCache/pivotCacheDefinition1.xml": (
                f'<pivotCacheDefinition xmlns="{MAIN}"><cacheSource type="worksheet">'
                '<worksheetSource ref="A1:B5" sheet="Data"/></cacheSource><cacheFields count="0"/></pivotCacheDefinition>',
                "pivotCacheDefinition",
            ),
            "xl/calcChain.xml": (f'<calcChain xmlns="{MAIN}"><c r="A2" i="1"/></calcChain>', "calcChain"),
        },
    )
    return path


def test_the_log_names_the_sheet_as_the_workbook_has_it(act, report):
    ctx = StubContext()
    act("excel.export", DATA.head(1), {"file": str(report), "sheet": "data", "existing": "sheet"}, ctx=ctx)
    assert ctx.messages[-1] == ("info", f"Wrote 1 row to sheet 'Data' of {report} (sheet replaced, the other sheets are kept)")
    for name in ("'Data", "Data'"):  # Excel doesn't allow these
        with pytest.raises(ActionError, match="no ' at the start or end"):
            act("excel.export", DATA, {"file": str(report), "sheet": name, "existing": "sheet"})


def test_only_the_data_sheet_is_replaced(act, report):
    before = _parts(report)
    params = {"file": str(report), "sheet": "data", "existing": "sheet", "table_style": "Table Style Medium 2"}
    act("excel.export", DATA, params)
    after = _parts(report)
    _check_package(report)
    assert set(before) - set(after) == {"xl/calcChain.xml"} and set(after) <= set(before)
    changed = {name for name in after if after[name] != before[name]}
    assert changed == {
        "[Content_Types].xml",
        "xl/_rels/workbook.xml.rels",  # without the list of formula cells
        "xl/workbook.xml",  # named ranges, calculation when opened
        "xl/styles.xml",
        "xl/worksheets/sheet2.xml",
        "xl/worksheets/_rels/sheet2.xml.rels",
        "xl/tables/table1.xml",
        "xl/charts/chart1.xml",
        "xl/pivotCache/pivotCacheDefinition1.xml",
    }  # the other sheets, the drawing with chart, picture and shape, the comment: all as they were

    book = openpyxl.load_workbook(report)
    assert book.sheetnames == ["Report", "Data", "Notes"]  # in its place, with its name's spelling
    data = book["Data"]
    assert data.sheet_properties.tabColor.rgb == "FFFF0000"
    assert [c.value for c in data[1]] == ["Customer", "Day", "Net", "Orders"] and data["A1"].font.b
    assert [c.value for c in data[3]] == ["Müller & <Co>", dt.datetime(2025, 1, 3), None, 2]  # NaN: empty
    assert data["A4"].value == "=not a formula" and data["A4"].data_type == "s"
    assert data["B2"].number_format == "dd.mm.yyyy" and data["C2"].number_format == "#,##0.00"
    assert data.freeze_panes == "A2"
    (table,) = data.tables.values()  # keeps its name: =SUM(Sales[Net]) still works
    assert (table.displayName, table.ref, table.tableStyleInfo.name) == ("Sales", "A1:D4", "TableStyleMedium2")
    page = book["Report"]
    assert page["A1"].font.b and page["A1"].fill.fgColor.rgb == "FFFFFF00" and page["A2"].value == "=SUM(Sales[Net])"
    assert book.calculation.fullCalcOnLoad
    # ranges over the old rows 2 to 5 now end at the new last row 4
    assert book.defined_names["SalesData"].attr_text == "Data!$A$1:$B$4"
    assert book.defined_names["Nets"].attr_text == "Data!$B$2:$B$4"
    chart = after["xl/charts/chart1.xml"].decode()
    assert re.findall(r"<c:f>(.*?)</c:f>", chart) == ["Data!$B$1", "Data!$A$2:$A$4", "Data!$B$2:$B$4"]
    pivot = etree.fromstring(after["xl/pivotCache/pivotCacheDefinition1.xml"])
    assert pivot.get("refreshOnLoad") == "1" and pivot.find(f".//{{{MAIN}}}worksheetSource").get("ref") == "A1:B4"

    back = act("excel.import", None, {"file": str(report), "sheet": "Data"})
    assert back["Customer"].to_list() == DATA["Customer"].to_list() and back["Day"][0] == dt.date(2025, 1, 2)


def test_a_new_sheet_is_added_at_the_end(act, report):
    act("excel.export", DATA, {"file": str(report), "sheet": "Sales", "existing": "sheet"})
    _check_package(report)
    book = openpyxl.load_workbook(report)
    assert book.sheetnames == ["Report", "Data", "Notes", "Sales"]
    assert list(book["Data"].tables) == ["Sales"] and list(book["Sales"].tables) == ["Sales_2"]  # names differ
    assert not book["Sales"].sheet_view.tabSelected  # otherwise Excel would group it with the selected sheet
    assert book.defined_names["Nets"].attr_text == "Data!$B$2:$B$5"  # the data sheet didn't change


def test_formats_are_added_only_once(act, report):
    params = {"file": str(report), "sheet": "Data", "existing": "sheet"}
    act("excel.export", DATA, params)
    styles = _parts(report)["xl/styles.xml"]
    act("excel.export", DATA, params)
    assert _parts(report)["xl/styles.xml"] == styles


def test_workbook_with_macros_keeps_them(act, tmp_path):
    path = tmp_path / "macros.xlsm"
    project = tmp_path / "vbaProject.bin"
    project.write_bytes(b"not a real macro project, it only has to stay")
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Start").write("A1", "Start")
    data = book.add_worksheet("Data")
    data.set_vba_name("DataSheet")
    data.hide()
    book.add_vba_project(str(project))
    book.close()

    act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"})
    _check_package(path)
    with zipfile.ZipFile(path) as archive:
        assert archive.read("xl/vbaProject.bin") == project.read_bytes()
        assert "macroEnabled" in archive.read("[Content_Types].xml").decode()
    book = openpyxl.load_workbook(path)
    data = book["Data"]
    assert data.sheet_state == "hidden" and data.sheet_properties.codeName == "DataSheet"
    assert data["A2"].value == "Huber"

    with pytest.raises(ActionError, match="macros"):  # Tafl can't write a whole workbook with macros
        act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "file"})
    with pytest.raises(ActionError, match="macros"):
        act("excel.export", DATA, {"file": str(tmp_path / "new.xlsm"), "existing": "sheet"})


def test_dates_in_a_workbook_with_1904_dates(act, tmp_path):
    path = tmp_path / "mac.xlsx"
    book = xlsxwriter.Workbook(path, {"date_1904": True})
    book.add_worksheet("Data")
    book.close()
    act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"})
    data = openpyxl.load_workbook(path)["Data"]
    assert data["B2"].value == dt.datetime(2025, 1, 2)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows locks open files")
def test_an_open_workbook_is_left_alone(act, report):
    before = report.read_bytes()
    with open(report, "rb"), pytest.raises(ActionError, match="open in another program"):
        act("excel.export", DATA, {"file": str(report), "sheet": "Data", "existing": "sheet"})
    assert report.read_bytes() == before
    assert [p.name for p in report.parent.iterdir()] == ["report.xlsx"]  # no temporary file left


def test_not_a_sheet_with_cells_or_not_a_workbook(act, tmp_path):
    path = tmp_path / "charts.xlsx"
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Data").write_column("A1", [1, 2])
    chart = book.add_chart({"type": "line"})
    chart.add_series({"values": "=Data!$A$1:$A$2"})
    book.add_chartsheet("Chart").set_chart(chart)
    book.close()
    with pytest.raises(ActionError, match="chart sheet"):
        act("excel.export", DATA, {"file": str(path), "sheet": "Chart", "existing": "sheet"})
    text = tmp_path / "text.xlsx"
    text.write_text("no workbook")
    with pytest.raises(ActionError, match="not an Excel workbook"):
        act("excel.export", DATA, {"file": str(text), "sheet": "Data", "existing": "sheet"})


def test_without_a_file_a_new_workbook_is_written(act, tmp_path):
    path = tmp_path / "new.xlsx"
    act("excel.export", DATA, {"file": str(path), "sheet": "2025", "existing": "sheet"})
    book = openpyxl.load_workbook(path)
    assert book.sheetnames == ["2025"] and list(book["2025"].tables) == ["Table_2025"]


def test_old_steps_replace_the_whole_file(registry):
    assert registry.actions["excel.export"].upgrade({"file": "x.xlsx"}, 1)["existing"] == "file"


def test_ranges_that_follow_the_data():
    formula = "SUM('My data'!$B$2:$B$5,Other!B2:B5,'My data'!B3:B5,[1]Data!B2:B5,'My data'!A1:D5)"
    assert _follow(formula, "my data", 5, 9) == (
        "SUM('My data'!$B$2:$B$9,Other!B2:B5,'My data'!B3:B5,[1]Data!B2:B5,'My data'!A1:D9)"
    )


def test_a_template_with_only_the_header_row(act, tmp_path):
    """Ranges down to row 2, the empty row left for the data, follow the data; row 1 alone stays."""
    path = tmp_path / "template.xlsx"
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Data").write_row("A1", ["Customer", "Day", "Net", "Orders"])
    chart = book.add_chart({"type": "column"})
    chart.add_series({"values": "=Data!$C$2:$C$2"})
    book.add_worksheet("Report").insert_chart("A1", chart)
    book.define_name("Nets", "=Data!$C$2:$C$2")
    book.define_name("Header", "=Data!$A$1:$D$1")
    book.close()
    act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"})
    names = {n: v.attr_text for n, v in openpyxl.load_workbook(path).defined_names.items()}
    assert names == {"Nets": "Data!$C$2:$C$4", "Header": "Data!$A$1:$D$1"}
    assert b"Data!$C$2:$C$4" in _parts(path)["xl/charts/chart1.xml"]


def test_what_the_replaced_sheet_had_of_its_own_is_named(act, tmp_path):
    path = tmp_path / "data.xlsx"
    book = xlsxwriter.Workbook(path)
    data = book.add_worksheet("Data")
    data.write_column("A1", ["Net", 1, 2])
    chart = book.add_chart({"type": "line"})
    chart.add_series({"values": "=Data!$A$2:$A$3"})
    data.insert_chart("C2", chart)
    data.insert_image("C20", "dot.png", {"image_data": io.BytesIO(PNG)})
    data.insert_textbox("H2", "a shape")
    data.write_comment("A1", "a note")
    data.conditional_format("A2:A3", {"type": "cell", "criteria": ">", "value": 1, "format": book.add_format({"bold": True})})
    data.data_validation("B2:B3", {"validate": "integer", "criteria": ">", "value": 0})
    book.close()
    ctx = StubContext()
    act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"}, ctx=ctx)
    assert ctx.messages[-2] == (
        "warning",
        "The old sheet 'Data' had its own charts, pictures, shapes, comments, conditional formats and data validation, "
        "and they were removed with it (the other sheets keep theirs). To keep such things, put them on another sheet.",
    )
    _check_package(path)
    assert openpyxl.load_workbook(path)["Data"]["A2"].value == "Huber"


@pytest.mark.parametrize("kind", ["pivotTable", "slicer", "timeline"])
def test_a_sheet_with_its_own_pivot_tables_or_slicers_is_not_replaced(act, tmp_path, kind):
    """Excel connects them with caches elsewhere in the workbook: Tafl leaves such a sheet alone."""
    path = tmp_path / "pivot.xlsx"
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Data").write_column("A1", ["Net", 1, 2])
    book.close()
    contents = _parts(path)
    contents[f"xl/{kind}s/{kind}1.xml"] = b"<x/>"
    folder = {"slicer": "http://schemas.microsoft.com/office/2007/relationships/",
              "timeline": "http://schemas.microsoft.com/office/2011/relationships/"}.get(kind, RELATIONSHIP)  # fmt: skip
    contents["xl/worksheets/_rels/sheet1.xml.rels"] = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        f'<Relationship Id="rId1" Type="{folder}{kind}" Target="../{kind}s/{kind}1.xml"/></Relationships>'
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    before = path.read_bytes()
    word = {"pivotTable": "pivot tables", "slicer": "slicers", "timeline": "timelines"}[kind]
    with pytest.raises(ActionError, match=f"has {word} of its own.*Move them to another sheet"):
        act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"})
    assert path.read_bytes() == before


def test_lost_objects_that_are_easy_to_mistake(act, tmp_path):
    """A new kind of chart with its stand-in text box, an old form button, a header picture and
    an empty list of validation rules; a drawing that is referred to but missing."""
    path = tmp_path / "data.xlsx"
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Data").write_column("A1", ["Net", 1])
    book.close()
    contents = _parts(path)
    drawing = (
        '<xdr:wsDr xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"><xdr:twoCellAnchor>'
        '<mc:AlternateContent><mc:Choice Requires="cx1"><xdr:graphicFrame/></mc:Choice>'
        "<mc:Fallback><xdr:sp><xdr:txBody>This chart isn't available</xdr:txBody></xdr:sp></mc:Fallback>"
        "</mc:AlternateContent></xdr:twoCellAnchor></xdr:wsDr>"
    )
    contents["xl/drawings/drawing1.xml"] = drawing.encode()
    contents["xl/drawings/_rels/drawing1.xml.rels"] = (
        b'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        b'<Relationship Id="rId1" Type="http://schemas.microsoft.com/office/2014/relationships/chartEx" Target="../charts/chartEx1.xml"/>'
        b"</Relationships>"
    )
    contents["xl/charts/chartEx1.xml"] = b"<x/>"
    contents["xl/drawings/vmlDrawing1.vml"] = b'<xml><v:shape><x:ClientData ObjectType="Button"/></v:shape></xml>'
    relations = (
        f'<Relationship Id="rId1" Type="{RELATIONSHIP}drawing" Target="../drawings/drawing1.xml"/>'
        f'<Relationship Id="rId2" Type="{RELATIONSHIP}vmlDrawing" Target="../drawings/vmlDrawing1.vml"/>'
        f'<Relationship Id="rId3" Type="{RELATIONSHIP}drawing" Target="../drawings/missing.xml"/>'
    )
    contents["xl/worksheets/_rels/sheet1.xml.rels"] = (
        f'<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">{relations}</Relationships>'
    ).encode()
    sheet = contents["xl/worksheets/sheet1.xml"].decode()
    contents["xl/worksheets/sheet1.xml"] = sheet.replace(
        "</worksheet>", '<dataValidations count="0"/><legacyDrawingHF r:id="rId9"/></worksheet>'
    ).encode()
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in contents.items():
            archive.writestr(name, content)
    ctx = StubContext()
    act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"}, ctx=ctx)
    warning = next(text for level, text in ctx.messages if level == "warning")
    assert warning.startswith("The old sheet 'Data' had its own charts, form controls and header and footer pictures,")


def _with_part(path, name: str, content: bytes, relation: tuple[str, str] | None = None) -> None:
    """The workbook with ``content`` in the part ``name``; ``relation``: a relationship (of this
    relationships part, its text) to add to it as well."""
    contents = _parts(path)
    contents[name] = content
    if relation is not None:
        contents[relation[0]] = (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            f"{relation[1]}</Relationships>"
        ).encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for part, data in contents.items():
            archive.writestr(part, data)


def _data_workbook(path) -> None:
    book = xlsxwriter.Workbook(path)
    book.add_worksheet("Data").write_column("A1", ["Net", 1])
    book.close()


# The meaning of _without_stand_ins, as a regular expression that takes too long
_STAND_IN = re.compile(rb"<(\w+):Fallback\b.*?</\1:Fallback>|<(\w+):Choice\b[^>]*Requires=\"a14\"[^>]*>.*?</\2:Choice>", re.S)
_TOKENS = [
    b"<mc:AlternateContent>", b"</mc:AlternateContent>", b'<mc:Choice Requires="a14">', b'<mc:Choice xmlns:a14="x" Requires="a14" y="1">',
    b'<mc:Choice Requires="cx1">', b"</mc:Choice>", b"<mc:Fallback>", b'<mc:Fallback a="1">', b"</mc:Fallback>", b"<x:Fallback>",
    b"</x:Fallback>", b"<xdr:sp>", b"</xdr:sp>", b"<mc:Fallbacks>", b"<Fallback>", b"</Fallback>", b"text", b"<a:Choice/>", b"<mc:Fallback/>",
]  # fmt: skip


def test_the_scan_for_stand_ins_finds_what_the_regular_expression_finds():
    generator = random.Random(2)
    for _ in range(5_000):
        xml = b"".join(generator.choices(_TOKENS, k=generator.randrange(0, 14)))
        assert _without_stand_ins(xml) == _STAND_IN.sub(b"", xml), xml


@pytest.mark.parametrize(
    "drawing",
    [
        b"<a:Fallback " * 200_000,  # no end tag: each start tag read the rest of the part
        b"".join(b"<a%d:Fallback " % n for n in range(200_000)),  # no end tag of any of these names
        b'<a:Choice Requires="a14" ' * 200_000,
        b'<a:Choice Requires="a14" Requires="a14"' * 200_000 + b"<",  # one long tag that ends nowhere
        b"<a:Fallback>" * 200_000 + b"</a:Fallback>",
        b"<a:Fallback></a:Fallback>" * 200_000,
        b"</a:Fallback>" * 200_000,
    ],
    ids=["open", "other names", "choice", "one long tag", "one end tag", "pairs", "only end tags"],
)
def test_the_scan_for_stand_ins_takes_no_long_time(drawing):
    start = time.perf_counter()
    kept = _without_stand_ins(drawing)
    assert time.perf_counter() - start < 2
    assert len(kept) in (0, len(drawing))


def test_a_drawing_full_of_open_tags_is_no_reason_to_wait(act, tmp_path):
    """Start tags without an end tag took 20 s for 8,700 bytes, with the Python lock held."""
    path = tmp_path / "data.xlsx"
    _data_workbook(path)
    drawing = b"<xdr:wsDr xmlns:a='x' xmlns:xdr='y'>" + b"<a:Fallback " * 10_000 + b"</xdr:wsDr>"
    relation = f'<Relationship Id="rId1" Type="{RELATIONSHIP}drawing" Target="../drawings/slow.xml"/>'
    _with_part(path, "xl/drawings/slow.xml", drawing, ("xl/worksheets/_rels/sheet1.xml.rels", relation))
    start = time.perf_counter()
    act("excel.export", DATA, {"file": str(path), "sheet": "Data", "existing": "sheet"})
    assert time.perf_counter() - start < 2


def test_a_chart_full_of_open_tags_is_no_reason_to_wait(act, report):
    """Each <f without a > read the rest of the part: 120,000 of them took 25 s."""
    chart = next(name for name in _parts(report) if name.startswith("xl/charts/chart"))
    _with_part(report, chart, b"<c:chartSpace>" + b"<f " * 80_000 + b"</c:chartSpace>")
    start = time.perf_counter()
    act("excel.export", DATA, {"file": str(report), "sheet": "Data", "existing": "sheet"})
    assert time.perf_counter() - start < 2
    assert _parts(report)[chart] == b"<c:chartSpace>" + b"<f " * 80_000 + b"</c:chartSpace>"


def test_chart_formulas_are_found_with_and_without_attributes():
    formulas = '<c:f>Data!$A$2:$A$9</c:f><f>x</f><c:f a="1">y</c:f><c:fx>z</c:fx><c:f/><f b="2" c="3">w&amp;v</f>'
    assert [m[3] for m in _CHART_FORMULA.finditer(formulas)] == ["Data!$A$2:$A$9", "x", "y", "w&amp;v"]


def test_a_part_of_2_gib_or_more_gets_zip64_and_the_others_stay_as_they_are(act, report, monkeypatch):
    """Copying such a part stopped with "File size too large, try using force_zip64". The test has a
    lower limit, for a part of 3 MiB. Excel may offer to repair a file, so the small parts have none."""
    big = bytes(3 << 20)
    with zipfile.ZipFile(report, "a", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("xl/media/big.bin", big)
    monkeypatch.setattr(zipfile, "ZIP64_LIMIT", 2 << 20)
    act("excel.export", DATA, {"file": str(report), "sheet": "Data", "existing": "sheet"})
    with zipfile.ZipFile(report) as archive:
        assert archive.testzip() is None
        assert archive.read("xl/media/big.bin") == big
        newer = {info.filename for info in archive.infolist() if info.extract_version >= zipfile.ZIP64_VERSION}
    assert newer == {"xl/media/big.bin"}
    assert openpyxl.load_workbook(report, read_only=True)["Data"]["A2"].value == "Huber"


def test_a_tag_cut_in_two_by_a_piece_is_found():
    from tafl.api.xlsx import _SHEET_OWN, _search

    for offset in (-20, -5, 0, 5):  # around the end of the first 1 MiB piece
        text = b"x" * ((1 << 20) + offset) + b"<conditionalFormatting sqref='A1'>"
        assert _search(io.BytesIO(text), _SHEET_OWN) == [True, False, False], offset


def _parts(path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _add_parts(path, parts: dict[str, tuple[str, str]]) -> None:
    """Adds parts to the workbook, each with a relationship from the workbook part."""
    contents = _parts(path)
    rels, types = contents["xl/_rels/workbook.xml.rels"].decode(), contents["[Content_Types].xml"].decode()
    for number, (name, (xml, kind)) in enumerate(parts.items(), start=90):
        contents[name] = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n{xml}'.encode()
        target = posixpath.relpath(name, "xl")
        relation = f'<Relationship Id="rId{number}" Type="{RELATIONSHIP}{kind}" Target="{target}"/>'
        rels = rels.replace("</Relationships>", relation + "</Relationships>")
        content_type = f"application/vnd.openxmlformats-officedocument.spreadsheetml.{kind}+xml"
        types = types.replace("</Types>", f'<Override PartName="/{name}" ContentType="{content_type}"/></Types>')
    contents["xl/_rels/workbook.xml.rels"], contents["[Content_Types].xml"] = rels.encode(), types.encode()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in contents.items():
            archive.writestr(name, content)


def _check_package(path) -> None:
    """Every relationship leads to a part, every part has a content type and every content type
    belongs to a part."""
    parts = _parts(path)
    types = etree.fromstring(parts["[Content_Types].xml"])
    defaults = {d.get("Extension").lower() for d in types if d.tag.endswith("Default")}
    overrides = {o.get("PartName").lstrip("/").lower() for o in types if o.tag.endswith("Override")}
    names = {name.lower() for name in parts}
    assert overrides <= names, overrides - names
    for name in parts:
        if name != "[Content_Types].xml":
            assert name.lower() in overrides or name.rsplit(".", 1)[-1].lower() in defaults, name
        if name.endswith(".rels"):
            folder = posixpath.dirname(posixpath.dirname(name))
            source = posixpath.join(folder, posixpath.basename(name)[: -len(".rels")])
            assert source == "" or source.lower() in names, name
            for relation in etree.fromstring(parts[name]):
                if relation.get("TargetMode") != "External":
                    target = relation.get("Target")
                    part = target[1:] if target.startswith("/") else posixpath.normpath(posixpath.join(folder, target))
                    assert part.lower() in names, (name, target)

"""Writing one sheet into an existing Excel workbook (.xlsx, .xlsm): everything else stays.

An Excel file is a zip of XML parts. ``replace_sheet`` lets xlsxwriter write the new sheet
into an empty workbook and moves that sheet and its Excel table into the existing workbook:
in place of the sheet with that name, or as the last sheet. All other parts are copied as
they are, so the other sheets keep their formulas, formatting, charts, pictures, shapes,
pivot tables, slicers, macros and data connections. The cell formats the new sheet uses are
added to the workbook's formats.

So that what is built on the sheet keeps working:

- the sheet keeps its place, tab color, visibility and code name (for macros), and its
  Excel table keeps its name (used by formulas like ``=SUM(Sales[Net])`` and pivot tables);
- ranges over the sheet's rows of data (from row 1 or 2 down to the old last row) in charts,
  named ranges and pivot table sources end at the new last row;
- Excel calculates the formulas again when it opens the file, and refreshes the pivot
  tables that use the sheet.
"""

from __future__ import annotations

import contextlib
import copy
import html
import io
import os
import posixpath
import re
import shutil
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import IO
from urllib.parse import unquote
from xml.sax.saxutils import escape

from lxml import etree
from xlsxwriter import Workbook

from .errors import ActionError

_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
_PACKAGE = "http://schemas.openxmlformats.org/package/2006/relationships"
_DOCUMENT = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
_REL_ID = f"{{{_DOCUMENT}}}id"
_WORKSHEET = "application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"
_TABLE = "application/vnd.openxmlformats-officedocument.spreadsheetml.table+xml"
_STYLES = "application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"
_CHARTS = (
    "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
    "application/vnd.ms-office.chartex+xml",
)
_PARSER = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=True)
_DECLARATION = b'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\r\n'

# In the sheet xlsxwriter writes: the formats of cells and rows (<c r="A1" s="3">), cells whose
# text is in the shared list of texts (<c r="A1" t="s"><v>7</v></c>), formats of columns (before
# the cells) and of conditional formatting (after them).
_CELL_FORMAT = re.compile(rb'" s="(\d+)"')
_SHARED_TEXT = re.compile(rb'" t="s"><v>(\d+)</v>')
_COLUMN_FORMAT = re.compile(r'(<col\b[^>]*? style=")(\d+)"')
_EXTRA_FORMAT = re.compile(rb'(\bdxfId=")(\d+)"')
# A range like A1:B20, and one on a sheet: Data!$B$2:$B$20 or 'My data'!B2:B20
_RANGE = r"(\$?[A-Za-z]{1,3}\$?)(\d+)(:\$?[A-Za-z]{1,3}\$?)(\d+)(?![\w(])"
_SHEET_RANGE = re.compile(r"(?<![\w.'\]])('(?:[^']|'')+'|[^\W\d][\w.]*)!" + _RANGE)
_CHART_FORMULA = re.compile(r"(<((?:\w+:)?f)\b[^>]*>)([^<]*)(</\2>)")
_KEPT_VIEW = ("tabSelected", "showGridLines", "showZeros", "rightToLeft", "zoomScale", "zoomScaleNormal")
_FONT_ORDER = (
    "b", "i", "strike", "condense", "extend", "outline", "shadow", "u", "vertAlign",
    "sz", "color", "name", "family", "charset", "scheme",
)  # fmt: skip
_BEFORE_CALC = (
    "fileVersion", "fileSharing", "workbookPr", "AlternateContent", "workbookProtection",
    "bookViews", "sheets", "functionGroups", "externalReferences", "definedNames",
)  # fmt: skip


def replace_sheet(
    target: Path, sheet: str, write: Callable[[Workbook], object], warn: Callable[[str], None] | None = None
) -> bool:
    """Writes a sheet into the existing workbook ``target`` (see the module's description).
    True if it replaced a sheet, False if it added one.

    The old sheet's own charts, pictures, comments, conditional formats and data validation go
    with it: ``warn`` is told so. A sheet with pivot tables, slicers or timelines of its own is
    not replaced (an ActionError): Excel connects them with other parts of the workbook.

    ``write`` gets an empty xlsxwriter workbook (with the target's date system; text that
    starts with = stays text) and adds one sheet to it, e.g. with
    ``df.write_excel(workbook=book, worksheet=sheet)``. The target is replaced only when all
    is written; until then the new file is next to it, with a name starting with ~$.
    """
    try:
        archive = zipfile.ZipFile(target)
    except zipfile.BadZipFile:
        raise ActionError(f"{target.name} is not an Excel workbook, or it is protected with a password") from None
    temporary = target.with_name(f"~${target.stem}.{os.getpid()}{target.suffix}")
    try:
        with archive, tempfile.TemporaryDirectory(prefix="tafl-") as folder:
            book = _Package(archive)
            replacement = _Replacement(book, sheet)
            written = Path(folder) / "sheet.xlsx"
            options = {"date_1904": replacement.date_1904, "strings_to_formulas": False, "nan_inf_to_errors": True}
            new = Workbook(str(written), options)
            try:
                write(new)
            except BaseException:
                with contextlib.suppress(Exception):
                    new.close()
                raise
            new.close()
            with zipfile.ZipFile(written) as source:
                replacement.run(_Package(source))
            book.save(temporary)
        try:
            os.replace(temporary, target)
        except PermissionError:
            raise ActionError(f"Can't write {target.name}. Is it open in Excel?") from None
    finally:
        temporary.unlink(missing_ok=True)
    if replacement.lost and warn is not None:
        warn(
            f"The old sheet '{replacement.name}' had {_listed(replacement.lost)} of its own: they are gone "
            "with it (the other sheets keep theirs). To keep such things, put them on another sheet."
        )
    return replacement.replaced


class _Replacement:
    """Moves the sheet of a workbook written by xlsxwriter into ``book``."""

    def __init__(self, book: _Package, sheet: str):
        self.book = book
        documents = book.related("", "officeDocument")
        if not documents:
            raise ActionError("The file is not an Excel workbook")
        self.workbook_part = documents[0][1]
        self.workbook = book.xml(self.workbook_part)
        if self.workbook.tag != _m("workbook"):
            raise ActionError("The workbook is saved as Strict Open XML: save it in Excel as 'Excel Workbook (*.xlsx)'")
        self.rels_part = _rels_part(self.workbook_part)
        self.rels = book.xml(self.rels_part)
        self.types = book.xml("[Content_Types].xml")
        properties = self.workbook.find(_m("workbookPr"))
        self.date_1904 = properties is not None and properties.get("date1904") in ("1", "true")
        self.reachable = book.reachable()
        self.sheets = self.workbook.find(_m("sheets"))
        found = [e for e in self.sheets.findall(_m("sheet")) if e.get("name", "").lower() == sheet.lower()]
        self.entry = found[0] if found else None
        self.replaced = self.entry is not None
        self.name = self.entry.get("name") if self.replaced else sheet
        self.part = ""
        self.properties = None  # the old sheet's tab color, code name, ...
        self.view: dict[str, str] = {}
        self.old_last: int | None = None  # the old sheet's last row of data
        self.old_tables: list[tuple[str, str, str]] = []  # part, id and name of the old sheet's tables
        self.table_names: set[str] = set()
        self.lost: list[str] = []  # what the old sheet had of its own and the new one doesn't
        if self.replaced:
            self._read_old_sheet()

    def _read_old_sheet(self) -> None:
        relation = next((r for r in self.rels if r.get("Id") == self.entry.get(_REL_ID)), None)
        if relation is None or _kind(relation) != "worksheet":
            raise ActionError(f"'{self.name}' is a chart sheet, not a sheet with cells: choose another sheet name")
        self.part = _resolve(self.workbook_part, relation.get("Target", ""))
        used = None
        with self.book.open(self.part) as stream:  # only the start: the cells come later
            for event, element in etree.iterparse(stream, ("start", "end"), resolve_entities=False, huge_tree=True):
                if element.tag == _m("sheetData"):
                    break
                if event == "end" and element.tag == _m("sheetPr"):
                    self.properties = copy.deepcopy(element)
                elif event == "start" and element.tag == _m("dimension"):
                    used = element.get("ref")
                elif event == "start" and element.tag == _m("sheetView") and not self.view:
                    self.view = dict(element.attrib) or {"workbookViewId": "0"}
        tables = [(part, self.book.xml(part)) for _, part in self.book.related(self.part, "table")]
        self.old_tables = [(part, t.get("id", ""), t.get("displayName") or t.get("name", "")) for part, t in tables]
        self.old_last = _last_row(tables[0][1].get("ref") if tables else used)
        self._check_own_objects()

    def _check_own_objects(self) -> None:
        """Pivot tables, slicers and timelines on the old sheet are refused; its charts, pictures,
        comments, conditional formats and data validation are listed in ``lost``."""
        kinds = {_kind(relation) for relation in self.book.relations(self.part)}
        refused = [word for kind, word in (("pivotTable", "pivot tables"), ("slicer", "slicers"), ("timeline", "timelines")) if kind in kinds]
        if refused:
            raise ActionError(
                f"The sheet '{self.name}' has {_listed(refused)} of its own, and they can't be kept when the sheet "
                "is replaced. Move them to another sheet in Excel (cut and paste), or choose 'Replace the whole file'."
            )
        drawn: set[str] = set()
        for _, drawing in self.book.related(self.part, "drawing"):
            drawn |= {_kind(relation) for relation in self.book.relations(drawing)}
            if re.search(rb"<(\w+:)?(sp|cxnSp)[\s>]", self.book.read(drawing)):
                drawn.add("shape")
        found = {
            "charts": "chart" in drawn,
            "pictures": "image" in drawn,
            "shapes": "shape" in drawn,
            "comments": bool(kinds & {"comments", "threadedComment"}),
            "form controls": "ctrlProp" in kinds,
        }
        with self.book.open(self.part) as stream:
            rules = _search(stream, (rb"<(\w+:)?conditionalFormatting[\s>]", rb"<(\w+:)?dataValidations?[\s>]"))
        found["conditional formats"], found["data validation"] = rules
        self.lost = [what for what, there in found.items() if there]

    def run(self, new: _Package) -> None:
        new_book = new.related("", "officeDocument")[0][1]
        new_part = new.related(new_book, "worksheet")[0][1]
        relations = new.relations(new_part)
        tables = {}
        for relation in relations:
            if relation.get("TargetMode") == "External":  # links
                continue
            if _kind(relation) != "table":
                raise ValueError(f"replace_sheet can't move a sheet's {_kind(relation)} into another workbook")
            tables[relation.get("Id")] = new.xml(_resolve(new_part, relation.get("Target", "")))

        # The cells get the numbers their formats have in this workbook, and their texts
        # directly (this workbook's list of texts stays as it is).
        formats, extra_formats = self._add_formats(new, new_book)
        shared = new.related(new_book, "sharedStrings")
        texts = re.findall(rb"<si>(.*?)</si>", new.read(shared[0][1]), re.S) if shared else []
        cells = {str(n).encode(): f'" s="{number}"'.encode() for n, number in formats.items()}
        inline = {str(n).encode(): b'" t="inlineStr"><is>' + text + b"</is>" for n, text in enumerate(texts)}
        head, body = new.read(new_part).split(b"<sheetData", 1)
        head = _COLUMN_FORMAT.sub(lambda m: f'{m[1]}{formats[int(m[2])]}"', head.decode("utf-8"))
        head = _adopt(head, self.view, self.properties)
        body = b"".join(_swap(_SHARED_TEXT, _swap(_CELL_FORMAT, rows, cells), inline) for rows in _rows(body))
        end = max(body.rfind(b"</sheetData>"), 0)
        body = body[:end] + _EXTRA_FORMAT.sub(lambda m: b'%s%d"' % (m[1], extra_formats[int(m[2])]), body[end:])
        if not self.replaced:
            self._add_sheet()

        sheet_rels = etree.Element(f"{{{_PACKAGE}}}Relationships", nsmap={None: _PACKAGE})
        free = list(self.old_tables)  # the new tables take over their parts, ids and names
        ids, names = self._taken()
        new_last = None
        for relation in relations:
            relation = copy.deepcopy(relation)
            table = tables.get(relation.get("Id"))
            if table is not None:
                if free:
                    part, table_id, name = free.pop(0)
                    self.book.remove(_rels_part(part))  # e.g. the query that filled the old table
                else:
                    part = self._free_part("tables/table{}.xml")
                    table_id, name = str(max(ids, default=0) + 1), _unique(table.get("name", "Table"), names)
                    self._set_type(part, _TABLE)
                ids.add(int(table_id))
                names.add(name.lower())
                self.table_names.add(name.lower())
                table.set("id", table_id)
                table.set("name", name)
                table.set("displayName", name)
                for element in table.iter():
                    for key, value in element.attrib.items():
                        if key.endswith("DxfId"):
                            element.set(key, str(extra_formats[int(value)]))
                self.book.write(part, table)
                relation.set("Target", "/" + part)
                new_last = new_last or _last_row(table.get("ref"))
            sheet_rels.append(relation)
        dimension = re.search(r'<dimension ref="([^"]*)"', head)
        new_last = new_last or _last_row(dimension[1] if dimension else None)

        self.book.write(self.part, head.encode("utf-8") + b"<sheetData" + body)
        if len(sheet_rels):
            self.book.write(_rels_part(self.part), sheet_rels)
        else:
            self.book.remove(_rels_part(self.part))
        self._update_workbook(new_last)
        self._clean_up()

    def _add_formats(self, new: _Package, new_book: str) -> tuple[dict[int, int], dict[int, int]]:
        """The numbers that the new workbook's cell formats (and its tables' formats) have in
        this workbook: equal ones are used, the others added."""
        source = _Styles(new.xml(new.related(new_book, "styles")[0][1]))
        formats, extra = range(len(source.items("cellXfs"))), range(len(source.items("dxfs")))
        found = self.book.related(self.workbook_part, "styles")
        if not found:  # a workbook without formats gets the new one's
            part = self._free_part("styles{}.xml")
            self.book.write(part, source.root)
            etree.SubElement(
                self.rels, f"{{{_PACKAGE}}}Relationship", Id=self._free_id(), Type=f"{_DOCUMENT}/styles", Target="/" + part
            )
            self._set_type(part, _STYLES)
            return {n: n for n in formats}, {n: n for n in extra}
        styles = _Styles(self.book.xml(found[0][1]))
        numbers = {n: styles.cell_format(source, n) for n in formats}, {n: styles.extra_format(source, n) for n in extra}
        self.book.write(found[0][1], styles.root)
        return numbers

    def _add_sheet(self) -> None:
        self.part = self._free_part("worksheets/sheet{}.xml")
        relation = self._free_id()
        etree.SubElement(self.rels, f"{{{_PACKAGE}}}Relationship", Id=relation, Type=f"{_DOCUMENT}/worksheet", Target="/" + self.part)
        number = max((int(e.get("sheetId", "0")) for e in self.sheets.findall(_m("sheet"))), default=0) + 1
        self.entry = etree.SubElement(self.sheets, _m("sheet"), {"name": self.name, "sheetId": str(number), _REL_ID: relation})
        self._set_type(self.part, _WORKSHEET)

    def _update_workbook(self, new_last: int | None) -> None:
        calculation = self.workbook.find(_m("calcPr"))
        if calculation is None:
            calculation = etree.SubElement(self.workbook, _m("calcPr"))
            _insert(self.workbook, calculation, after=_BEFORE_CALC)
        calculation.set("fullCalcOnLoad", "1")  # the sheet's values changed: Excel calculates the formulas again
        for relation in self.rels.findall(f"{{{_PACKAGE}}}Relationship"):
            if _kind(relation) == "calcChain":  # Excel's list of the formula cells; it makes a new one
                self.rels.remove(relation)
        # A sheet with only its header row: ranges down to row 2 (the empty row a template leaves
        # for the data) follow the data; a range of row 1 alone may be the header, it stays.
        old = 2 if self.old_last == 1 else self.old_last
        follow = old is not None and new_last is not None and old != new_last
        on_sheet = set(self.table_names)  # tables and named ranges that pivot tables can use
        position = str(self.sheets.findall(_m("sheet")).index(self.entry))
        names = self.workbook.find(_m("definedNames"))
        for defined in [] if names is None else names.findall(_m("definedName")):
            if defined.get("name", "").lower() == "_xlnm._filterdatabase" and defined.get("localSheetId") == position:
                names.remove(defined)  # the old sheet's filter range: the new sheet has its filter in the table
                continue
            if _on_sheet(defined.text or "", self.name):
                on_sheet.add(defined.get("name", "").lower())
                if follow:
                    defined.text = _follow(defined.text or "", self.name, old, new_last)
        if names is not None and not len(names):
            self.workbook.remove(names)

        for _, part in self.book.related(self.workbook_part, "pivotCacheDefinition"):
            cache = self.book.xml(part)
            source = cache.find(f"{_m('cacheSource')}/{_m('worksheetSource')}")
            if source is None or source.get(_REL_ID):  # not built on a sheet of this workbook
                continue
            if source.get("sheet", "").lower() == self.name.lower():
                if follow and source.get("ref"):
                    source.set("ref", _follow_range(source.get("ref"), old, new_last))
            elif source.get("name", "").lower() not in on_sheet:
                continue
            cache.set("refreshOnLoad", "1")
            self.book.write(part, cache)
        if follow:
            self._follow_charts(old, new_last)
        self.book.write(self.workbook_part, self.workbook)
        self.book.write(self.rels_part, self.rels)

    def _follow_charts(self, old_last: int, new_last: int) -> None:
        for override in self.types.findall(f"{{{_TYPES}}}Override"):
            part = override.get("PartName", "").lstrip("/")
            if override.get("ContentType") not in _CHARTS or self.book.name(part) is None:
                continue
            try:
                text = self.book.read(part).decode("utf-8")
            except UnicodeDecodeError:
                continue

            def follow(m: re.Match) -> str:
                formula = html.unescape(m[3])
                moved = _follow(formula, self.name, old_last, new_last)
                return m[0] if moved == formula else m[1] + escape(moved) + m[4]

            changed = _CHART_FORMULA.sub(follow, text)
            if changed != text:
                self.book.write(part, changed.encode("utf-8"))

    def _clean_up(self) -> None:
        """Removes the parts only the old sheet used (its drawings, comments, tables, ...)."""
        for key in self.reachable - self.book.reachable():
            part = self.book.name(key)
            if part is not None:
                self.book.remove(part)
                self.book.remove(_rels_part(part))
                self._set_type(part, None)
        self.book.write("[Content_Types].xml", self.types)

    def _taken(self) -> tuple[set[int], set[str]]:
        """The ids and names of the other tables, and the named ranges (table names must differ)."""
        old = {part.lower() for part, _, _ in self.old_tables}
        ids, names = set(), set()
        for override in self.types.findall(f"{{{_TYPES}}}Override"):
            part = override.get("PartName", "").lstrip("/")
            if override.get("ContentType") == _TABLE and part.lower() not in old and self.book.name(part):
                table = self.book.xml(part)
                if table.get("id", "").isdigit():
                    ids.add(int(table.get("id")))
                names |= {table.get("name", "").lower(), table.get("displayName", "").lower()}
        return ids, names | {d.get("name", "").lower() for d in self.workbook.iter(_m("definedName"))}

    def _free_part(self, pattern: str) -> str:
        folder, number = posixpath.dirname(self.workbook_part), 1
        while self.book.name(posixpath.join(folder, pattern.format(number))) is not None:
            number += 1
        return posixpath.join(folder, pattern.format(number))

    def _free_id(self) -> str:
        ids, number = {r.get("Id") for r in self.rels}, 1
        while f"rId{number}" in ids:
            number += 1
        return f"rId{number}"

    def _set_type(self, part: str, content_type: str | None) -> None:
        """Sets (or with None removes) the content type of a part."""
        for override in self.types.findall(f"{{{_TYPES}}}Override"):
            if override.get("PartName", "").lower() == "/" + part.lower():
                if content_type is None:
                    self.types.remove(override)
                else:
                    override.set("ContentType", content_type)
                return
        if content_type is not None:
            etree.SubElement(self.types, f"{{{_TYPES}}}Override", PartName="/" + part, ContentType=content_type)


class _Styles:
    """The cell formats of a workbook (its styles part)."""

    _SECTIONS = ("numFmts", "fonts", "fills", "borders", "cellStyleXfs", "cellXfs", "cellStyles", "dxfs")

    def __init__(self, root: etree._Element):
        self.root = root

    def items(self, section: str) -> list[etree._Element]:
        parent = self.root.find(_m(section))
        return [] if parent is None else [c for c in parent if isinstance(c.tag, str)]

    def add(self, section: str, element: etree._Element) -> int:
        """The number of an equal entry of the section; the element is added if there is none."""
        parent = self.root.find(_m(section))
        if parent is None:
            parent = etree.SubElement(self.root, _m(section))
            _insert(self.root, parent, after=self._SECTIONS[: self._SECTIONS.index(section)])
        entries = [c for c in parent if isinstance(c.tag, str)]
        key = _key(element)
        for number, entry in enumerate(entries):
            if _key(entry) == key:
                return number
        parent.append(element)
        parent.set("count", str(len(entries) + 1))
        return len(entries)

    def cell_format(self, source: _Styles, number: int) -> int:
        xf = copy.deepcopy(source.items("cellXfs")[number])
        if int(xf.get("numFmtId", "0")) >= 164:  # 0 to 163 are Excel's own number formats
            code = {f.get("numFmtId"): f.get("formatCode", "") for f in source.items("numFmts")}[xf.get("numFmtId")]
            xf.set("numFmtId", str(self.number_format(code)))
        xf.set("fontId", str(self.font(source, int(xf.get("fontId", "0")))))
        for section, attribute, same in (("fills", "fillId", 2), ("borders", "borderId", 1)):
            index = int(xf.get(attribute, "0"))  # no fill, the grey pattern and no border are in every workbook
            if index >= same:
                xf.set(attribute, str(self.add(section, copy.deepcopy(source.items(section)[index]))))
        xf.set("xfId", "0")
        return self.add("cellXfs", xf)

    def extra_format(self, source: _Styles, number: int) -> int:
        dxf = copy.deepcopy(source.items("dxfs")[number])
        for number_format in dxf.iter(_m("numFmt")):
            if int(number_format.get("numFmtId", "0")) >= 164:
                number_format.set("numFmtId", str(self.number_format(number_format.get("formatCode", ""))))
        return self.add("dxfs", dxf)

    def number_format(self, code: str) -> int:
        numbers = {f.get("formatCode"): int(f.get("numFmtId", "0")) for f in self.items("numFmts")}
        if code in numbers:
            return numbers[code]
        number = max([163, *numbers.values()]) + 1
        self.add("numFmts", etree.Element(_m("numFmt"), numFmtId=str(number), formatCode=code))
        return number

    def font(self, source: _Styles, number: int) -> int:
        """The workbook's default font with what the source font changes (e.g. bold)."""
        fonts, own = source.items("fonts"), self.items("fonts")
        if number == 0 or not own:
            return 0
        usual = [_key(c) for c in fonts[0] if isinstance(c.tag, str)]
        font = copy.deepcopy(own[0])
        for child in fonts[number]:
            if isinstance(child.tag, str) and _key(child) not in usual:
                for same in font.findall(child.tag):
                    font.remove(same)
                font.append(copy.deepcopy(child))
        order = {name: i for i, name in enumerate(_FONT_ORDER)}
        font[:] = sorted(font, key=lambda c: order.get(etree.QName(c).localname, len(order)) if isinstance(c.tag, str) else 0)
        return self.add("fonts", font)


class _Package:
    """The parts of a zip package (an Excel file). Changes stay here until ``save``."""

    def __init__(self, archive: zipfile.ZipFile):
        self.archive = archive
        self.parts = {i.filename.lower(): i.filename for i in archive.infolist() if not i.is_dir()}
        self.changed: dict[str, bytes] = {}
        self.removed: set[str] = set()

    def name(self, part: str) -> str | None:
        """The part's name in the zip (part names ignore case); None if there is no such part."""
        return self.parts.get(part.lower())

    def read(self, part: str) -> bytes:
        name = self.parts[part.lower()]
        return self.changed[name] if name in self.changed else self.archive.read(name)

    def open(self, part: str) -> IO[bytes]:
        name = self.parts[part.lower()]
        return io.BytesIO(self.changed[name]) if name in self.changed else self.archive.open(name)

    def xml(self, part: str) -> etree._Element:
        return etree.fromstring(self.read(part), _PARSER)

    def write(self, part: str, content: bytes | etree._Element) -> None:
        if not isinstance(content, bytes):
            content = _DECLARATION + etree.tostring(content, encoding="UTF-8", xml_declaration=False)
        name = self.parts.setdefault(part.lower(), part)
        self.changed[name] = content
        self.removed.discard(name)

    def remove(self, part: str) -> None:
        name = self.parts.pop(part.lower(), None)
        if name is not None:
            self.changed.pop(name, None)
            self.removed.add(name)

    def relations(self, part: str) -> list[etree._Element]:
        """The relationships of a part ("" for the package itself)."""
        rels = _rels_part(part)
        return [] if self.name(rels) is None else self.xml(rels).findall(f"{{{_PACKAGE}}}Relationship")

    def related(self, part: str, kind: str = "") -> list[tuple[etree._Element, str]]:
        """(relationship, part) for the parts a part refers to, or for those of one kind."""
        return [
            (relation, _resolve(part, relation.get("Target", "")))
            for relation in self.relations(part)
            if relation.get("TargetMode") != "External" and (not kind or _kind(relation) == kind)
        ]

    def reachable(self) -> set[str]:
        """The parts (in lower case) that the relationships lead to, from the package on."""
        seen: set[str] = set()
        todo = [""]
        while todo:
            for _, part in self.related(todo.pop()):
                if part.lower() not in seen and self.name(part) is not None:
                    seen.add(part.lower())
                    todo.append(part)
        return seen

    def save(self, path: Path) -> None:
        done = set()
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as out:
            for info in self.archive.infolist():
                if info.is_dir() or info.filename in self.removed:
                    continue
                entry = _entry(info.filename, info.date_time)
                if info.filename in self.changed:
                    out.writestr(entry, self.changed[info.filename])
                else:
                    with self.archive.open(info) as source, out.open(entry, "w") as target:
                        shutil.copyfileobj(source, target, 1 << 20)
                done.add(info.filename)
            for name, content in self.changed.items():
                if name not in done:
                    out.writestr(_entry(name), content)


def _adopt(head: str, view: dict[str, str], properties: etree._Element | None) -> str:
    """The new sheet (the part before its cells) takes over the old one's zoom, grid lines and
    properties (tab color, code name). A new sheet's tab is not selected: Excel would group
    it with the selected sheet."""

    def adopt_view(m: re.Match) -> str:
        attributes = {k: v for k, v in re.findall(r'(\w+)="([^"]*)"', m[1]) if k not in _KEPT_VIEW}
        attributes |= {k: escape(v, {'"': "&quot;"}) for k, v in view.items() if k in _KEPT_VIEW}
        return "<sheetView" + "".join(f' {k}="{v}"' for k, v in attributes.items()) + m[2]

    head = re.sub(r"<sheetView\b([^>]*?)(/?>)", adopt_view, head, count=1)
    if properties is not None:
        properties = copy.deepcopy(properties)
        properties.attrib.pop("filterMode", None)  # no rows of the new sheet are filtered
        properties.tail = None
        etree.cleanup_namespaces(properties)
        head = re.sub(r"<sheetPr\b[^>]*/>|<sheetPr\b.*?</sheetPr>", "", head, count=1, flags=re.S)
        start = head.index(">", head.index("<worksheet")) + 1
        element = etree.tostring(properties, encoding="unicode").replace(f' xmlns="{_MAIN}"', "", 1)
        head = head[:start] + element + head[start:]
    return head


def _follow(formula: str, sheet: str, old_last: int, new_last: int) -> str:
    """Ranges on the sheet from row 1 or 2 down to the old last row end at the new last row."""

    def move(m: re.Match) -> str:
        return f"{m[1]}!{_moved(m, 2, old_last, new_last)}" if _sheet_name(m[1]).lower() == sheet.lower() else m[0]

    return _SHEET_RANGE.sub(move, formula)


def _follow_range(ref: str, old_last: int, new_last: int) -> str:
    found = re.fullmatch(_RANGE, ref)
    return _moved(found, 1, old_last, new_last) if found else ref


def _moved(m: re.Match, first: int, old_last: int, new_last: int) -> str:
    start_column, start_row, end_column, end_row = m.group(first, first + 1, first + 2, first + 3)
    if int(start_row) in (1, 2) and int(end_row) == old_last:
        end_row = str(new_last)
    return f"{start_column}{start_row}{end_column}{end_row}"


def _on_sheet(formula: str, sheet: str) -> bool:
    return any(_sheet_name(m[1]).lower() == sheet.lower() for m in _SHEET_RANGE.finditer(formula))


def _sheet_name(name: str) -> str:
    return name[1:-1].replace("''", "'") if name.startswith("'") else name


def _last_row(ref: str | None) -> int | None:
    found = re.search(r"(\d+)$", ref or "")
    return int(found[1]) if found else None


def _unique(name: str, taken: set[str]) -> str:
    candidate, number = name, 1
    while candidate.lower() in taken:
        number += 1
        candidate = f"{name}_{number}"
    return candidate


def _rows(cells: bytes, size: int = 1 << 24) -> Iterator[bytes]:
    """The sheet's cells in pieces of whole rows."""
    start = 0
    while start < len(cells):
        end = cells.find(b"</row>", start + size)
        end = len(cells) if end < 0 else end + len(b"</row>")
        yield cells[start:end]
        start = end


def _swap(pattern: re.Pattern, data: bytes, replacements: dict[bytes, bytes]) -> bytes:
    """Replaces each match of the pattern by the replacement for the number in it. A split instead
    of a Python call per match: a sheet can have millions of cells."""
    pieces = pattern.split(data)
    pieces[1::2] = [replacements[number] for number in pieces[1::2]]
    return b"".join(pieces)


def _key(element: etree._Element) -> tuple:
    """Equal for elements with the same tag, attributes and children."""
    children = tuple(_key(c) for c in element if isinstance(c.tag, str))
    return element.tag, tuple(sorted(element.attrib.items())), children


def _insert(parent: etree._Element, element: etree._Element, after: tuple[str, ...]) -> None:
    """Moves a child behind the last child named in ``after`` (XML schemas fix the order)."""
    position = 0
    for index, child in enumerate(parent):
        if isinstance(child.tag, str) and etree.QName(child).localname in after:
            position = index + 1
    parent.insert(position, element)


def _search(stream: IO[bytes], patterns: tuple[bytes, ...]) -> list[bool]:
    """Whether each pattern occurs in the stream, read in pieces (sheets can be large)."""
    found = [False] * len(patterns)
    compiled = [re.compile(pattern) for pattern in patterns]
    rest = b""
    while chunk := stream.read(1 << 20):
        text = rest + chunk
        for index, pattern in enumerate(compiled):
            found[index] = found[index] or bool(pattern.search(text))
        rest = text[-256:]  # a tag that is cut in two is found in the next piece
    return found


def _listed(words: list[str]) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + " and " + words[-1]


def _kind(relation: etree._Element) -> str:
    return relation.get("Type", "").rsplit("/", 1)[-1]


def _m(tag: str) -> str:
    return f"{{{_MAIN}}}{tag}"


def _rels_part(part: str) -> str:
    folder, name = posixpath.split(part)
    return posixpath.join(folder, "_rels", f"{name}.rels")


def _resolve(source: str, target: str) -> str:
    """The part a relationship's target means (relative to the source part's folder)."""
    target = unquote(target)
    if target.startswith("/"):
        return target[1:]
    return posixpath.normpath(posixpath.join(posixpath.dirname(source), target))


def _entry(name: str, date_time: tuple = (1980, 1, 1, 0, 0, 0)) -> zipfile.ZipInfo:
    entry = zipfile.ZipInfo(name, date_time)
    entry.compress_type = zipfile.ZIP_DEFLATED
    return entry

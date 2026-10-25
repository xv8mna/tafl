"""The project index: what the Find panel searches and groups (no Qt)."""

import importlib.util
import os
import threading
import time
from pathlib import Path

import pytest

from tafl.api import ActionError, Cancelled, Endpoint, P, action, source_params
from tafl.api.endpoints import CMD, FILE, FOLDER, IN, OUT, SQL
from tafl.core import Engine, Parameter, Project, Registry
from tafl.core import index as ix
from tafl.core.index import Location, Query

ROOT = Path(__file__).resolve().parents[1]


def _module(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def demo(tmp_path) -> Project:
    project = _module(ROOT / "examples" / "make_examples.py").build_project()
    project.path = tmp_path / "demo.tafl"
    return project


@pytest.fixture
def project(tmp_path) -> Project:
    return Project(name="Test", path=tmp_path / "test.tafl")


def add(project, registry, table, action_id, params=None, **step):
    target = project.find_table(table) or project.add_table(table)
    added = project.add_step(target, registry.actions[action_id], params or {})
    for key, value in step.items():
        setattr(added, key, value)
    return added


def build(project, registry) -> ix.ProjectIndex:
    return ix.build(Engine(project, registry))


def find(index, text="", **options) -> list[ix.Hit]:
    return ix.search(index, Query(text, **options))


def names(hits) -> list[str]:
    return [f"{hit.entry.table} {hit.entry.number}" for hit in hits]


def marked(line: ix.Line, kind: str) -> list[str]:
    return [line.text[start:end] for start, end, k in line.marks if k == kind]


def with_actions(registry, *actions) -> Registry:
    """A registry with the built-in actions and some of a test."""
    own = Registry()
    own.actions = {**registry.actions, **{a.id: a for a in actions}}
    own.connectors = dict(registry.connectors)
    return own


# --- kinds and entries -------------------------------------------------------------------------


def test_step_kind_of_plugin_actions():
    def run(df, p, ctx):
        return df

    assert ix.step_kind(action("t.gen", "Generate", kind="source", params=[P.Int("rows", "Rows")])(run)) == "in"
    assert ix.step_kind(action("t.copy", "Copy", kind="source", params=[P.TableRef("table", "Table")])(run)) == "step"
    files = action("t.files", "Files", kind="source", params=[P.TableRef("t", "T"), P.Folder("f", "F")])(run)
    assert ix.step_kind(files) == "in"
    assert ix.step_kind(action("t.look", "Look up", params=[P.File("file", "File")])(run)) == "in"
    listed = action("t.listed", "Listed", params=[P.List("rows", "Rows", fields=[P.ConnectionRef("c", "C")])])(run)
    assert ix.step_kind(listed) == "in"
    assert ix.step_kind(action("t.web", "Web", endpoints=lambda p: [])(run)) == "in"
    assert ix.step_kind(action("t.upper", "Upper", params=[P.Column("column", "Column")])(run)) == "step"
    assert ix.step_kind(action("t.mail", "Mail", kind="command")(run)) == "cmd"
    assert ix.step_kind(action("t.out", "Out", kind="export")(run)) == "out"


def test_one_entry_per_step_in_project_order(registry, demo):
    group = demo.add_group("Sales", "green")
    demo.tables[1].group = group.id
    demo.tables[2].enabled = False
    demo.tables[1].steps[2].enabled = False
    index = build(demo, registry)
    assert len(index.entries) == sum(len(t.steps) for t in demo.tables)
    assert [(e.table, e.number) for e in index.entries[:4]] == [("Customers", 1), ("Customers", 2), ("Customers", 3), ("Orders", 1)]
    orders = index.entries[3]
    assert (orders.action, orders.kind, orders.group, orders.group_color, orders.group_id) == (
        "Import CSV / text files",
        "in",
        "Sales",
        "green",
        group.id,
    )
    assert orders.summary == "Import orders.csv" and orders.table_id == demo.tables[1].id
    assert [e.kind for e in index.entries if e.table == "Orders"] == ["in", *["step"] * 7, "out", "out"]
    assert not index.entries[5].enabled and index.entries[5].table_enabled
    assert all(not e.table_enabled for e in index.entries if e.table == "Revenue by customer")
    assert index.entry(orders.step_id) is orders and index.entry("nope") is None
    assert index.directory == demo.directory and index.parameters == [] and index.groups == [group.id]


def test_values_in_the_order_of_the_settings(registry, project):
    add(project, registry, "Customers", "csv.import", {"file": "c.csv"})
    add(project, registry, "Orders", "csv.import", {"file": "o.csv"})
    add(
        project,
        registry,
        "Orders",
        "table.merge",
        {"other": project.tables[0].id, "match": [{"column": "CustomerNo", "other_column": "No"}], "columns": ["Name", "City"]},
    )
    columns = [{"name": "Gross", "formula": "[Net] * 1.19"}, {"name": "", "formula": "[Net]  -\n 1"}]
    add(project, registry, "Orders", "calc.columns", {"columns": columns}, note="VAT 19 %")
    index = build(project, registry)
    merge, calculate = index.entries[2], index.entries[3]
    settings = [(v.label, v.text, v.key, v.row, v.field) for v in merge.values if not v.key.startswith("#")]
    assert settings == [
        ("Other table", "Customers", "other", 0, ""),
        ("Match rows where, row 1 – Column in this table", "CustomerNo", "match", 1, "column"),
        ("Match rows where, row 1 – equals column in the other table", "No", "match", 1, "other_column"),
        ("Columns to add (empty = all)", "Name, City", "columns", 0, ""),
        ("Suffix for column names that exist already", " (2)", "suffix", 0, ""),
    ]  # the merge type (a choice) is left out
    assert [(v.label, v.text, v.key, v.row, v.field) for v in calculate.values] == [
        ("New columns, row 1 – Column name", "Gross", "columns", 1, "name"),
        ("Formula [Gross]", "[Net] * 1.19", "columns", 1, "formula"),
        ("New columns, row 2 – Formula", "[Net]  -\n 1", "columns", 2, "formula"),
        ("Note", "VAT 19 %", ix.NOTE, 0, ""),
        ("", calculate.summary, ix.SUMMARY, 0, ""),
        ("Action", "Calculate columns", ix.ACTION, 0, ""),
        ("Table", "Orders", ix.TABLE, 0, ""),
    ]
    assert calculate.note == "VAT 19 %" and calculate.locations == () and calculate.parameters == ()
    assert "[net] * 1.19" in calculate.haystack and "vat 19 %" in calculate.haystack and "orders" in calculate.haystack


def test_hidden_settings_are_left_out(registry, project):
    add(project, registry, "Orders", "csv.import", {"source": "folder", "folder": "inbox", "file": "old.csv"})
    entry = build(project, registry).entries[0]
    texts = [v.text for v in entry.values]
    assert "inbox" in texts and "old.csv" not in texts and "old.csv" not in entry.haystack


def test_a_long_summary_is_cut(registry, project):
    add(project, registry, "Orders", "db.command", {"connection": "Shop", "sql": "UPDATE x SET a = 1 " * 40})
    entry = build(project, registry).entries[0]
    assert len(entry.summary) <= ix.SUMMARY_LENGTH
    long = action("t.long", "Long", kind="source", summary=lambda p: "x" * 1000)(lambda df, p, ctx: df)
    other = Project(path=project.path)
    other.add_step(other.add_table("T"), long, {})
    entry = ix.build(Engine(other, with_actions(registry, long))).entries[0]
    assert len(entry.summary) == ix.SUMMARY_LENGTH and entry.summary.endswith("…")


def test_a_summary_is_on_one_line(registry, project):
    """Line 2 of a result has room for one line: before, a condition on several lines was drawn
    over the result's title and below it."""
    add(project, registry, "T", "csv.import", {"file": "a.csv"})
    add(project, registry, "T", "rows.filter_formula", {"condition": "[a] > 1\nAND [b] < 2\n  OR [c] = 3"}, enabled=False)
    index = build(project, registry)
    entry = index.entries[1]
    assert entry.summary == "Keep rows where [a] > 1 AND [b] < 2 OR [c] = 3"
    (off,), (named,) = find(index, off=True), find(index, "filter")  # only the action's name matches
    assert off.value is None and named.value is None
    assert off.line.text == named.line.text == entry.summary
    assert ix.tooltip(entry).splitlines()[0] == f"T, step 2: {entry.summary}"


# --- connections and parameters ----------------------------------------------------------------


def test_connections_with_their_details(registry, project):
    project.add_connection("Shop", "sqlserver", server=r".\SQLEXPRESS", database="ShopDB", driver="", trust_certificate=True)
    project.add_connection("Local", "sqlite", file="data/shop.db")
    project.add_connection("Old", "nosuchdb", host="db1", password="secret")
    add(project, registry, "Orders", "db.import", {"connection": "shop", "table": "dbo.Orders"})
    add(project, registry, "Orders", "db.command", {"connection": "Local", "sql": "DELETE FROM x"})
    add(project, registry, "Orders", "db.export", {"connection": "Old", "table": "T"})
    add(project, registry, "Orders", "db.export", {"connection": "Nowhere", "table": "T"})
    entries = build(project, registry).entries
    connection = [v for v in entries[0].values if v.key == "connection"]
    assert [(v.label, v.text) for v in connection] == [
        ("Connection", "shop"),
        ("Shop", r"Microsoft SQL Server, .\SQLEXPRESS, ShopDB"),
    ]
    assert entries[0].locations[0].connection == r"Shop (Microsoft SQL Server: .\SQLEXPRESS, ShopDB)"
    assert entries[1].locations[0].connection == "Local (SQLite: data/shop.db)"
    assert entries[1].locations[0].endpoint == Endpoint(CMD, SQL, place="Local", name="DELETE FROM x", setting="sql")
    assert entries[2].locations[0].connection == "Old (nosuchdb: db1)"  # no password
    assert "secret" not in entries[2].haystack
    assert entries[3].locations[0].connection == "Nowhere (not defined in this project)"
    assert [v.text for v in entries[3].values if v.key == "connection"] == ["Nowhere"]


def test_no_connection_chosen_yet(registry, project):
    """A table or SQL typed before a connection is chosen. Before, the place was " (not defined
    in this project)", with an empty name."""
    project.add_connection("Shop", "sqlite", file="shop.db")
    add(project, registry, "T", "db.export", {"connection": "", "table": "dbo.Out"})
    add(project, registry, "T", "db.command", {"sql": "EXEC dbo.Refresh"})
    add(project, registry, "T", "db.export", {"connection": "Shop", "table": "dbo.In"})
    add(project, registry, "T", "files.delete", {"column": "Path"})
    index = build(project, registry)
    exported, command = index.entries[:2]
    assert exported.locations[0].connection == command.locations[0].connection == ""
    assert ix.overview(exported).text == "dbo.Out  on  ?  ·  add rows"
    assert ix.tooltip(exported).splitlines()[1] == "Writes into dbo.Out on ? (add rows)"
    assert ix.tooltip(command).splitlines()[1] == "Changes SQL on ?: EXEC dbo.Refresh"
    found = ix.sections(index, find(index), "place")
    assert [s.title for s in found] == ["Shop (SQLite: shop.db)", "No connection chosen", "Files named in a column"]
    assert [(s.title, names(s.hits)) for s in found[1].sections] == [("dbo.Out", ["T 1"]), ("SQL", ["T 2"])]
    assert "not defined" not in found[1].tooltip


def test_a_connection_of_only_spaces_or_empty_now_is_none_chosen(registry, project):
    """The line says "?" as the tooltip and the section do. Before, it showed the spaces, and a
    database that is empty with the values now was "on the project folder"."""
    server = action(
        "t.server",
        "Write to a server",
        kind="export",
        params=[P.Text("server", "Server")],
        endpoints=lambda p: [Endpoint(OUT, "database", place=p.server, name="dbo.Later", setting="server")],
    )(lambda df, p, ctx: df)
    project.parameters = [Parameter("Server", "")]
    add(project, registry, "T", "db.export", {"connection": "   ", "table": "dbo.Spaces"})
    add(project, registry, "T", "db.command", {"connection": "   ", "sql": "EXEC dbo.Refresh"})
    project.add_step(project.tables[0], server, {"server": "{Server}"})
    index = ix.build(Engine(project, with_actions(registry, server)))
    spaces, command, later = index.entries
    assert ix.overview(spaces).text == "dbo.Spaces  on  ?  ·  add rows"
    assert ix.tooltip(spaces).splitlines()[1] == "Writes into dbo.Spaces on ? (add rows)"
    assert ix.overview(command).text == "SQL on ?: EXEC dbo.Refresh"
    assert ix.overview(later).text == "dbo.Later  on  {Server}   now: dbo.Later on ?"
    assert ix.tooltip(later).splitlines()[1:] == ["Writes into dbo.Later on ?", "    now dbo.Later on ?"]
    assert [s.title for s in ix.sections(index, find(index), "place")] == ["No connection chosen"]


@pytest.fixture
def with_parameters(registry, project):
    project.parameters = [
        Parameter("Folder", r"D:\Data"),
        Parameter("Month", "2026-10"),
        Parameter("Day", "2026-10-01"),
        Parameter("Bad", "=[NoColumn] + 1"),
        Parameter("Unused", "x"),
    ]
    project.add_connection("Local", "sqlite", file=r"{Folder}\shop.db")
    add(project, registry, "Orders", "csv.import", {"file": r"{Folder}\orders_{month}.csv"})
    add(project, registry, "Orders", "rows.filter_formula", {"condition": "[Date] >= {Day}"})
    add(project, registry, "Orders", "csv.export", {"file": r"out\{Bad}.csv"})
    add(project, registry, "Orders", "db.command", {"connection": "Local", "sql": "DELETE FROM t WHERE Day = {Day}"})
    add(project, registry, "Orders", "csv.export", {"file": r"out\{Nope}.csv"}, note="Uses {Month}")
    return project


def test_values_now_with_the_parameters(registry, with_parameters):
    entries = build(with_parameters, registry).entries
    imported, filtered, bad, sql, unknown = entries
    file = imported.values[0]
    assert (file.label, file.text, file.now, file.key) == ("File", r"{Folder}\orders_{month}.csv", r"D:\Data\orders_2026-10.csv", "file")
    assert imported.parameters == ("Folder", "Month") and imported.now_problem == ""
    assert r"{folder}\orders_{month}.csv" in imported.haystack and r"d:\data\orders_2026-10.csv" in imported.haystack
    location = imported.locations[0]
    assert location.endpoint == Endpoint(IN, FILE, place="{Folder}", name="orders_{month}.csv", setting="file")
    assert location.now == Endpoint(IN, FILE, place=r"D:\Data", name="orders_2026-10.csv", setting="file")
    assert location.folder == os.path.normpath(r"D:\Data")
    formula = filtered.values[0]
    assert (formula.text, formula.now) == ("[Date] >= {Day}", "[Date] >= DATE '2026-10-01'")
    assert filtered.parameters == ("Day",)
    assert bad.now_problem.startswith("The parameter {Bad} has an error") and bad.parameters == ("Bad",)
    assert all(not v.now for v in bad.values) and bad.locations[0].now is None
    assert bad.locations[0].folder == str(with_parameters.directory / "out")
    statement = [v for v in sql.values if v.key == "sql"][0]
    assert statement.now == "DELETE FROM t WHERE Day = '2026-10-01'"
    details = [v for v in sql.values if v.label == "Local"][0]
    assert (details.text, details.now) == (r"SQLite, {Folder}\shop.db", r"SQLite, D:\Data\shop.db")
    assert sql.parameters == ("Folder", "Day")  # the connection (with its details) comes before the SQL
    assert sql.locations[0].now.name == "DELETE FROM t WHERE Day = '2026-10-01'"
    assert unknown.parameters == () and unknown.values[0].now == ""  # {Nope} is no parameter; the note doesn't count
    assert unknown.locations[0].folder == str(with_parameters.directory / "out")


def test_a_parameter_that_an_upgrade_writes_is_filled_in(registry, project):
    """The values now come from the settings as the action gets them: also a {Name} that only the
    upgrade of a step saved by an older version of its action writes."""

    def migrate(params, version):
        return {"file": r"{Inbox}\orders.csv" if params.get("inbox") else "orders.csv"}

    reader = action("t.read", "Read", kind="source", params=[P.File("file", "File")], version=2, migrate=migrate)
    reader = reader(lambda df, p, ctx: df)
    project.parameters = [Parameter("Inbox", r"D:\In")]
    project.add_step(project.add_table("T"), reader, {"inbox": True}, version=1)
    entry = ix.build(Engine(project, with_actions(registry, reader))).entries[0]
    assert (entry.values[0].text, entry.values[0].now) == (r"{Inbox}\orders.csv", r"D:\In\orders.csv")
    assert entry.parameters == ("Inbox",) and entry.locations[0].now.place == r"D:\In"


def test_a_place_with_a_parameter_that_has_an_error_stays_as_written(registry, project):
    project.parameters = [Parameter("Bad", "=[NoColumn]")]
    add(project, registry, "Orders", "csv.import", {"file": r"{Bad}\orders.csv"})
    location = build(project, registry).entries[0].locations[0]
    assert location.now is None and location.folder == "{Bad}" and location.place_key == "path:" + os.path.normcase("{Bad}")


# --- locations ---------------------------------------------------------------------------------


def test_full_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("TAFL_TEST_FOLDER", str(tmp_path / "env"))
    assert ix.full_path("data/inbox", tmp_path) == str(tmp_path / "data" / "inbox")
    assert ix.full_path(r" .\data\inbox\ ", tmp_path) == str(tmp_path / "data" / "inbox")
    assert ix.full_path(str(tmp_path / "x" / ".." / "data"), None) == str(tmp_path / "data")
    assert ix.full_path("$TAFL_TEST_FOLDER/in", tmp_path) == str(tmp_path / "env" / "in")
    assert ix.full_path("", tmp_path) == str(tmp_path) and ix.full_path("", None) == ""
    assert ix.full_path("data", None) == "data"
    assert ix.full_path("{Folder}/in", tmp_path) == "{Folder}/in"


def test_the_files_the_import_read_and_files_in_a_column(registry, project):
    add(project, registry, "Daily", "csv.import", {"source": "folder", "folder": "inbox", "pattern": "orders_*.csv"})
    add(project, registry, "Daily", "files.move", {"which": "imported", "target": "archive"})
    add(project, registry, "Daily", "files.delete", {"column": "Full path"})
    add(project, registry, "Daily", "files.copy", {"column": "Path", "base": r".\inbox\\", "target": "backup"})
    add(project, registry, "Other", "table.derive", {"table": project.tables[0].id})
    add(project, registry, "Other", "files.delete", {"which": "imported"})
    entries = build(project, registry).entries
    folder = entries[0].locations[0]
    moved, target = entries[1].locations
    assert moved.endpoint.imported and moved.via == folder.endpoint and moved.folder == folder.folder == str(project.directory / "inbox")
    assert (moved.place_key, moved.name_key) == (folder.place_key, folder.name_key)
    assert target.endpoint == Endpoint(OUT, FOLDER, place="archive", setting="target")
    listed = entries[2].locations[0]
    assert (listed.folder, listed.place_key, listed.name_key) == ("", "column:", "column:full path")
    below = entries[3].locations[0]
    assert below.folder == str(project.directory / "inbox") and below.place_key == folder.place_key
    other = entries[5].locations[0]
    assert other.via is None and other.folder == "" and other.place_key == "imported:"


def test_files_in_a_folder_is_no_import_of_the_files(registry, project):
    """'Files in a folder' lists the files but reads none for the later steps: a command on the
    files the import read moves nothing (it warns). Before, it showed the listed folder's files."""
    add(project, registry, "Listing", "files.list", {"folder": "inbox", "pattern": "*.csv"})
    add(project, registry, "Listing", "files.move", {"which": "imported", "target": "archive"})
    add(project, registry, "Excel", "excel.import", {"source": "folder", "folder": "books", "pattern": "*.xlsx"})
    add(project, registry, "Excel", "files.delete", {"which": "imported"})
    index = build(project, registry)
    _, moved, imported, deleted = index.entries
    files = moved.locations[0]
    assert files.via is None and files.folder == "" and files.place_key == "imported:"
    assert ix.overview(moved).text == "the files the import read  →  archive"
    assert ix.tooltip(moved).splitlines()[1] == "Changes the files the import read"
    place = {s.title: s for s in ix.sections(index, find(index), "place")}
    assert names(place["Files the import read"].hits) == ["Listing 2"]
    assert [(s.title, names(s.hits)) for s in place["inbox"].sections] == [("*.csv", ["Listing 1"])]
    assert deleted.locations[0].via == imported.locations[0].endpoint  # an import of all files in a folder


def test_an_import_with_source_params_and_no_endpoints_reads_the_files_of_its_folder(registry, project):
    """A plugin's import with ``source_params`` and ``source_files`` but without ``endpoints=``
    (written before it existed). Before, a later step on the files the import read was listed
    under "Files the import read", as if the table started with no folder import."""
    reader = action("t.read", "Read my files", kind="source", params=source_params("", "*.txt"))(lambda df, p, ctx: df)
    project.add_step(project.add_table("P"), reader, {"source": "folder", "folder": "inbox", "pattern": "*.txt"})
    add(project, registry, "P", "files.delete", {"which": "imported"})
    index = ix.build(Engine(project, with_actions(registry, reader)))
    imported, deleted = index.entries
    assert imported.locations[0].endpoint.folder_import and deleted.locations[0].via == imported.locations[0].endpoint
    assert ix.overview(deleted).text == "the files the import read  (*.txt in inbox)  ·  to the Recycle Bin"
    place = {s.title: s for s in ix.sections(index, find(index), "place")}
    assert list(place) == ["inbox"] and [(s.title, names(s.hits)) for s in place["inbox"].sections] == [("*.txt", ["P 1", "P 2"])]


def test_endpoints_of_plugins_are_made_usable(registry, project):
    def odd(p):
        return [Endpoint("up", "web", place=None, name=Path("a.csv")), Endpoint(OUT, FILE), "not one"]

    plugin = action("t.odd", "Odd", kind="export", endpoints=odd)(lambda df, p, ctx: df)
    add(project, registry, "T", "csv.import", {"file": "a.csv"})
    project.add_step(project.tables[0], plugin, {})
    entry = ix.build(Engine(project, with_actions(registry, plugin))).entries[1]
    assert [loc.endpoint for loc in entry.locations] == [Endpoint(OUT, FILE, place="", name="a.csv")]  # the empty one is left out


def test_broken_plugin_code_gives_plain_entries(registry, project):
    def ends(p):
        raise SystemExit(1)

    def migrate(params, version):
        raise KeyboardInterrupt

    def summary(p):
        raise RuntimeError("broken")

    exits = action("t.exits", "Exits", kind="export", params=[P.File("file", "File")], endpoints=ends, summary=summary)(lambda df, p, ctx: df)
    old = action("t.old", "Old", kind="export", params=[P.File("file", "File")], version=2, migrate=migrate)(lambda df, p, ctx: df)
    own = with_actions(registry, exits, old)
    add(project, own, "T", "csv.import", {"file": "a.csv"})
    project.add_step(project.tables[0], exits, {"file": "x.csv"})
    project.add_step(project.tables[0], old, {"file": "y.csv", "level": 3}, version=1)
    project.add_step(project.tables[0], old, {"file": "z.csv"}, version=5)  # saved by a newer version
    project.add_step(project.tables[0], "t.gone", {"file": "w.csv", "n": 3})
    entries = ix.build(Engine(project, own)).entries
    assert entries[1].summary == "Exits" and [loc.endpoint.name for loc in entries[1].locations] == ["x.csv"]  # the default
    for entry in entries[2:4]:
        assert entry.action == "Old" and entry.kind == "out" and entry.locations == ()
        assert [(v.label, v.key) for v in entry.values][:1] == [("file", "file")]
    gone = entries[4]
    assert (gone.action, gone.kind, gone.summary) == ("t.gone", "step", "Unknown action 't.gone'")
    assert [(v.label, v.text) for v in gone.values][:1] == [("file", "w.csv")]


def test_stopping_stops_building(registry, project):
    def migrate(params, version):
        raise KeyboardInterrupt

    old = action("t.old", "Old", version=2, migrate=migrate)(lambda df, p, ctx: df)
    own = with_actions(registry, old)
    add(project, own, "T", "csv.import", {"file": "a.csv"})
    project.add_step(project.tables[0], old, {}, version=1)
    engine = Engine(project, own)
    engine.cancel = threading.Event()
    engine.cancel.set()
    with pytest.raises(Cancelled):
        ix.build(engine)


# --- searching ---------------------------------------------------------------------------------


def test_an_empty_search_lists_the_imports_exports_and_commands(registry, demo):
    hits = find(build(demo, registry))
    assert names(hits) == ["Customers 1", "Orders 1", "Orders 9", "Orders 10", "Revenue by customer 5"]
    assert all(hit.value is None and hit.status == "" for hit in hits)
    assert hits[0].line.text == "customers.xlsx, sheet Kunden  in  data"
    assert hits[0].title == ix.Line("Customers · 1 · Import Excel files")
    assert hits[0].focus == ("file", 0, "")
    assert hits[0].tooltip.splitlines() == [
        "Customers, step 1: Import customers.xlsx (Kunden)",
        "Reads " + str(demo.directory / "data" / "customers.xlsx") + ", sheet Kunden",
    ]
    assert Query().empty and not Query(" ").text.strip() and not Query(off=True).empty and not Query("x").empty


def test_all_words_must_match_in_any_value(registry, demo):
    index = build(demo, registry)
    hits = find(index, "orders XLSX")
    assert names(hits) == ["Orders 9"]
    hit = hits[0]
    assert hit.value.key == "file" and hit.line.text == "File: output/orders.xlsx"
    assert marked(hit.line, "label") == ["File:"] and marked(hit.line, "match") == ["orders", "xlsx"]
    assert marked(hit.title, "match") == ["Orders"]
    assert hit.focus == ("file", 0, "")
    assert names(find(index, "customerno customers")) == ["Customers 2", "Orders 4"]
    assert find(index, "orders nothing") == []


def test_the_value_matching_most_words_is_shown(registry, demo):
    index = build(demo, registry)
    hits = find(index, "round 1.19")
    assert names(hits) == ["Orders 6"]
    assert hits[0].value.label == "Formula [Gross]" and hits[0].focus == ("columns", 1, "formula")
    assert hits[0].line.text == "Formula [Gross]: ROUND([Net] * 1.19, 2)"
    tie = find(index, "gross 1.19")[0]  # the column name and the formula match one word each: the first
    assert tie.value.label == "New columns, row 1 – Column name" and tie.line.text.endswith(": Gross")


def test_names_are_never_the_shown_value(registry, demo):
    hits = find(build(demo, registry), "revenue by")
    assert names(hits) == [f"Revenue by customer {n}" for n in range(1, 6)]
    derive = hits[0]  # only the table's name matches
    assert derive.value is None and derive.line == ix.Line("Start from Orders")
    assert derive.focus is None and marked(derive.title, "match") == ["Revenue", "by"]
    sort = hits[3]  # the summary matches one word
    assert sort.value.key == ix.SUMMARY and sort.line.text == sort.entry.summary and sort.focus is None
    assert marked(sort.line, "match") == ["by", "Revenue"] and marked(sort.line, "label") == []
    export = hits[4]  # the file comes before the summary
    assert export.value.key == "file" and export.focus == ("file", 0, "")


def test_upper_and_lower_case(registry, demo):
    index = build(demo, registry)
    assert names(find(index, "STORNIERT")) == ["Orders 3"]
    assert find(index, "STORNIERT", case=True) == []
    assert names(find(index, "Revenue", case=True)) == names(find(index, "revenue"))
    assert find(index, "REVENUE", case=True) == []


def test_regular_expressions(registry, demo):
    index = build(demo, registry)
    hits = find(index, r"orders\.(csv|xlsx)", regex=True)
    assert names(hits) == ["Orders 1", "Orders 9", "Orders 10"]
    assert marked(hits[1].line, "match") == ["orders.xlsx"]
    assert find(index, r"ORDERS\.csv", regex=True, case=True) == []
    with pytest.raises(ValueError, match="^Not a valid pattern: "):
        find(index, "orders(", regex=True)
    with pytest.raises(ValueError, match="^Not a valid pattern: "):
        find(index, "[", regex=True, off=True)


def test_no_pattern_takes_long(registry, project):
    """Patterns that make a backtracking matcher (Python's re) try every way to split a long word.
    Before, the first one took seconds here, twice as long for each letter more."""
    add(project, registry, "T", "csv.import", {"file": r"data\orders_daily_export.csv"})
    add(project, registry, "T", "excel.export", {"file": r"out\report.xlsx"})
    index = build(project, registry)
    for pattern, step, match in ((r"(\w+\s?)+$", "T 1", "csv"), ("(.*)*xlsx$", "T 2", r" out\report.xlsx")):
        started = time.perf_counter()
        hits = find(index, pattern, regex=True)
        assert time.perf_counter() - started < 1, pattern
        hit = next(hit for hit in hits if names([hit]) == [step])
        assert hit.value.key == "file" and marked(hit.line, "match") == [match], pattern
    assert names(find(index, "(.*)*xlsx$", regex=True)) == ["T 2"]
    assert find(index, "(a+)+$", regex=True) == []


def test_patterns_are_matched_as_in_the_filter_pane(registry, project):
    """Rust's regular expressions (Polars): no look-around or back-references."""
    add(project, registry, "T", "csv.import", {"file": "orders.csv"}, note="Kunden → Übersicht \ue000 orders")
    index = build(project, registry)
    for pattern in ("orders(?=.csv)", r"(o)\1"):
        with pytest.raises(ValueError, match="^Not a valid pattern: (look-around|backreferences)"):
            find(index, pattern, regex=True)
    note = find(index, "über", regex=True)[0]
    assert note.value.key == ix.NOTE and marked(note.line, "match") == ["Über"]
    assert find(index, "ÜBER", regex=True, case=True) == []
    exact = find(index, "orders", regex=True, case=True)[0]  # a pattern that is plain text
    assert exact.line.text == "File: orders.csv" and marked(exact.line, "match") == ["orders"]
    end = find(index, "orders$", regex=True)[0]  # only the note ends with it
    assert end.value.key == ix.NOTE and marked(end.line, "match") == ["orders"]
    assert marked(find(index, "kunden.*", regex=True)[0].line, "match") == ["Kunden → Übersicht \ue000 orders"]


def test_a_lone_surrogate_in_a_text(registry, project):
    """Half of a character (from "\\ud800" in a project file) in a note: Polars takes no such text.
    Before, every pattern was "not valid" (an error of UTF-8). The places of the matches are those
    in the text, also after characters of several bytes."""
    note = "Kunden \ud800 → " + "Übersicht " * 12 + "target " + "rest " * 40
    add(project, registry, "T", "csv.import", {"file": "orders.csv"}, note=note)
    index = build(project, registry)
    assert names(find(index, "orders", regex=True)) == ["T 1"]
    hit = find(index, "tar.et", regex=True)[0]
    flat = " ".join(note.split())
    start = flat.index("target") - 40
    assert hit.line.text == "Note: …" + flat[start : start + 200] + "…"
    assert marked(hit.line, "match") == ["target"]
    assert marked(find(index, "kunden . →", regex=True)[0].line, "match") == ["Kunden \ud800 →"]


def test_matches_in_a_note_and_a_long_text(registry, project):
    formula = "  ".join(f"[Column {n}]" for n in range(12)) + "\n + [Target] + " + "[Rest] + " * 30 + "1"
    add(project, registry, "T", "csv.import", {"file": "a.csv"})
    add(project, registry, "T", "calc.columns", {"columns": [{"name": "X", "formula": formula}]}, note="Ask Anna")
    index = build(project, registry)
    note = find(index, "anna")[0]
    assert note.line.text == "Note: Ask Anna" and note.focus == (ix.NOTE, 0, "")
    long = find(index, "target")[0]
    text = long.line.text
    assert text.startswith("Formula [X]: …") and text.endswith("…") and "\n" not in text and "  " not in text[14:]
    assert marked(long.line, "match") == ["Target"]
    assert len(text) <= len("Formula [X]: ") + 202
    pattern = find(index, r"tar.et\]", regex=True)[0]
    assert pattern.line.text == text and marked(pattern.line, "match") == ["Target]"]


def test_parameters_and_the_values_now_in_the_line(registry, with_parameters):
    index = build(with_parameters, registry)
    hit = find(index, "2026-10.csv")[0]
    assert hit.line.text == r"File: {Folder}\orders_{month}.csv   now: D:\Data\orders_2026-10.csv"
    assert marked(hit.line, "label") == ["File:", "now:"]
    assert marked(hit.line, "parameter") == ["{Folder}", "{month}"]
    assert marked(hit.line, "match") == ["2026-10.csv"]
    overview = find(index)[0].line
    assert overview.text == r"orders_{month}.csv  in  {Folder}   now: orders_2026-10.csv in D:\Data"
    assert marked(overview, "label") == ["now:"] and marked(overview, "parameter") == ["{month}", "{Folder}"]


def test_filters(registry, demo):
    demo.tables[2].enabled = False
    demo.tables[1].steps[2].enabled = False
    index = build(demo, registry)
    off = find(index, off=True)
    assert names(off) == ["Orders 3", *[f"Revenue by customer {n}" for n in range(1, 6)]]
    assert {hit.status for hit in off} == {"off"}
    assert names(find(index, "export", off=True)) == ["Revenue by customer 5"]
    problems = {demo.tables[1].steps[3].id: ("error", "Table 'Customers' has an error\nmore"), demo.tables[0].steps[0].id: ("warning", "Odd")}
    assert find(index, problems=True) == []
    found = ix.search(index, Query(problems=True), problems=problems)
    assert names(found) == ["Customers 1", "Orders 4"]
    assert found[1].line.text == "Error: Table 'Customers' has an error" and marked(found[1].line, "label") == ["Error:"]
    assert found[1].status == "error" and found[0].status == "warning"
    with_text = ix.search(index, Query("customers", problems=True), problems=problems)
    assert [hit.value.key if hit.value else None for hit in with_text] == ["file", "other"]
    changed = ix.search(index, Query(changed=True), changed=[demo.tables[1].steps[1].id])
    assert names(changed) == ["Orders 2"]
    assert len(find(index, changed=True)) == 0


def test_the_parameter_filter(registry, with_parameters):
    index = build(with_parameters, registry)
    assert names(find(index, parameter="")) == ["Orders 1", "Orders 2", "Orders 3", "Orders 4"]
    hits = find(index, parameter="day")
    assert names(hits) == ["Orders 2", "Orders 4"]
    assert hits[0].value.key == "condition" and hits[1].value.key == "sql"
    assert names(find(index, "delete", parameter="DAY")) == ["Orders 4"]
    assert find(index, parameter="Unused") == [] and find(index, parameter="Nope") == []
    assert find(index, parameter="")[0].value.key == "file"


def test_signatures_and_changed_steps(registry, demo):
    baseline = ix.signatures(demo, registry.actions)
    assert set(baseline) == {s.id for t in demo.tables for s in t.steps}
    assert ix.changed_steps(build(demo, registry), baseline) == set()
    orders = demo.tables[1]
    orders.steps[2].params["conditions"][0]["value"] = "offen"
    orders.steps[3].note = "Check the keys"
    orders.steps[4].enabled = False
    demo.tables[2].enabled = False
    new = demo.add_step(orders, registry.actions["rows.sort"], {})
    orders.name = "All orders"  # renaming the table or moving a step is no change
    orders.steps.insert(0, orders.steps.pop(1))
    changed = ix.changed_steps(build(demo, registry), baseline)
    revenue = {s.id for s in demo.tables[2].steps}
    assert changed == {orders.steps[2].id, orders.steps[3].id, orders.steps[4].id, new.id, *revenue}
    assert ix.signatures(demo, registry.actions)[new.id] == build(demo, registry).entry(new.id).signature


def test_signatures_compare_the_settings_with_their_defaults(registry, demo):
    """A step saved with only some of its settings is the same with all of them written, as the
    settings form writes them. Before, such a step stayed "changed" after a setting was typed
    back to how it was."""
    baseline = ix.signatures(demo, registry.actions)
    first = demo.tables[1].steps[0]
    assert first.params == {"file": "data/orders.csv"}
    action = registry.actions[first.action]
    first.params = dict(action.resolve(first.params))  # every setting, the others with their defaults
    assert len(first.params) > 1 and ix.changed_steps(build(demo, registry), baseline) == set()
    first.params = {**first.params, "header": False}  # a setting that differs from its default
    assert ix.changed_steps(build(demo, registry), baseline) == {first.id}
    # Settings that can't be read (a step saved by a newer version) compare as they are
    first.params, first.version = {"file": "data/orders.csv"}, action.version + 1
    baseline = ix.signatures(demo, registry.actions)
    assert ix.changed_steps(build(demo, registry), baseline) == set()
    first.params = dict(action.resolve(first.params))
    assert ix.changed_steps(build(demo, registry), baseline) == {first.id}


# --- sections ----------------------------------------------------------------------------------


def test_sections_by_kind(registry, project):
    add(project, registry, "T", "csv.import", {"file": "a.csv"})
    add(project, registry, "T", "rows.sort", {"by": [{"column": "a.csv"}]})
    add(project, registry, "T", "files.delete", {"column": "a.csv"})
    add(project, registry, "T", "csv.export", {"file": "b.csv"})
    index = build(project, registry)
    found = ix.sections(index, find(index, "a.csv"), "kind")
    assert [(s.key, s.title, s.count) for s in found] == [
        ("kind:in", "Imports", 1),
        ("kind:cmd", "Commands", 1),
        ("kind:step", "Other steps", 1),
    ]
    assert ix.sections(index, [], "kind") == []


def test_sections_by_table_and_group(registry, demo):
    sales = demo.add_group("Sales", "green")
    base = demo.add_group("Base", "blue")
    demo.tables[0].group = base.id
    demo.tables[2].group = sales.id
    demo.tables[2].enabled = False
    index = build(demo, registry)
    found = ix.sections(index, find(index), "table")
    assert [(s.key, s.title, s.color, s.count) for s in found] == [
        (f"group:{sales.id}", "Sales", "green", 1),
        (f"group:{base.id}", "Base", "blue", 1),
        ("group:", "Not in a group", "", 3),
    ]
    assert [(s.key, s.title) for s in found[0].sections] == [(f"table:{demo.tables[2].id}", "Revenue by customer (off)")]
    assert [hit.entry.number for hit in found[2].sections[0].hits] == [1, 9, 10]
    for table in demo.tables:
        table.group = ""
    index = build(demo, registry)
    assert [s.title for s in ix.sections(index, find(index), "table")] == ["Customers", "Orders", "Revenue by customer (off)"]


def test_sections_by_place(registry, project, tmp_path):
    outside = tmp_path.parent / "elsewhere"
    project.add_connection("Shop", "sqlite", file="shop.db")
    add(project, registry, "A", "csv.import", {"source": "folder", "folder": r"data\inbox", "pattern": "orders_*.csv"})
    add(project, registry, "A", "files.move", {"which": "imported", "target": str(outside)})
    add(project, registry, "A", "csv.export", {"file": r".\data\inbox\\x.csv"})
    add(project, registry, "A", "csv.export", {"file": str(project.directory / "data" / "inbox" / "X.csv")})
    add(project, registry, "A", "csv.export", {"file": "top.csv"})
    add(project, registry, "A", "db.export", {"connection": "shop", "table": "dbo.T"})
    add(project, registry, "A", "db.command", {"connection": "Shop", "sql": "EXEC x"})
    add(project, registry, "A", "rows.sort", {"by": [{"column": "x"}]})
    add(project, registry, "B", "table.derive", {"table": project.tables[0].id})
    add(project, registry, "B", "files.delete", {"which": "imported"})
    add(project, registry, "B", "files.delete", {"column": "Full path"})
    index = build(project, registry)
    found = ix.sections(index, find(index) + find(index, "sort"), "place")
    inbox = str(project.directory / "data" / "inbox")
    assert [(s.title, s.tooltip, s.count) for s in found] == [
        (str(outside), str(outside), 1),  # by title
        (os.path.join("data", "inbox"), inbox, 4),
        ("Project folder", str(project.directory), 1),
        ("Shop (SQLite: shop.db)", "Shop (SQLite: shop.db)", 2),
        ("Files named in a column", found[4].tooltip, 1),
        ("Files the import read", found[5].tooltip, 1),
        ("No file, folder or database", "", 1),
    ]
    assert [(s.title, names(s.hits)) for s in found[1].sections] == [
        ("orders_*.csv", ["A 1", "A 2"]),
        ("x.csv", ["A 3", "A 4"]),
    ]
    assert [s.title for s in found[0].sections] == ["The folder itself"]
    assert [s.title for s in found[3].sections] == ["dbo.T", "SQL"]
    assert [s.title for s in found[4].sections] == ["listed in column Full path"]
    assert found[5].sections == [] and names(found[5].hits) == ["B 2"]
    assert names(found[6].hits) == ["A 8"]
    assert found[1].key == "place:" + found[1].sections[0].hits[0].entry.locations[0].place_key
    assert found[1].sections[0].key.startswith(found[1].key.replace("place:", "name:", 1) + "|")


# --- texts -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("endpoint", "text"),
    [
        (Endpoint(IN, FILE, place="inbox", name="orders_{Month}.csv"), "orders_{Month}.csv  in  inbox"),
        (Endpoint(OUT, FILE, place="output", name="orders.xlsx", part="Orders"), "orders.xlsx, sheet Orders  in  output"),
        (
            Endpoint(OUT, FILE, name="r.xlsx", part="Data", mode="replace only this sheet"),
            "r.xlsx, sheet Data  in  the project folder  ·  replace only this sheet",
        ),
        (Endpoint(IN, FOLDER, place="inbox", name="orders_*.csv", mode="with subfolders"), "orders_*.csv  in  inbox  ·  with subfolders"),
        (Endpoint(IN, FOLDER, place="books", name="*.xlsx", part="Kunden"), "*.xlsx, sheet Kunden  in  books"),
        (Endpoint(OUT, FOLDER, place="archive", mode="keep subfolders"), "archive  ·  keep subfolders"),
        (Endpoint(CMD, FILE, column="Full path"), "files listed in column Full path"),
        (Endpoint(CMD, FILE, place="inbox", column="Path", mode="for good"), "files listed in column Path  below  inbox  ·  for good"),
        (Endpoint(CMD, FILE, imported=True), "the files the import read"),
        (Endpoint(OUT, "database", place="Shop", name="dbo.Customers", mode="add rows"), "dbo.Customers  on  Shop  ·  add rows"),
        (Endpoint(CMD, SQL, place="Shop", name="EXEC dbo.Refresh"), "SQL on Shop: EXEC dbo.Refresh"),
        (Endpoint(CMD, SQL, place="Shop", name="UPDATE x\n  SET " + "a = 1, " * 20), "SQL on Shop: " + ("UPDATE x SET " + "a = 1, " * 9)[:59] + "…"),
    ],
)
def test_location_texts(endpoint, text):
    assert ix.location_text(Location(endpoint)) == text


def test_location_texts_with_the_import_and_the_values_now():
    via = Endpoint(IN, FOLDER, place="inbox", name="orders_*.csv")
    assert ix.location_text(Location(Endpoint(CMD, FILE, imported=True), via=via)) == "the files the import read  (orders_*.csv in inbox)"
    location = Location(Endpoint(IN, FILE, place="{F}", name="a_{M}.csv"), now=Endpoint(IN, FILE, place="D:/x", name="a_10.csv"))
    assert ix.location_text(location) == "a_{M}.csv  in  {F}"
    assert ix.location_text(location, now=True) == "a_10.csv  in  D:/x"


def test_overview_and_tooltip_of_a_move(registry, project):
    add(project, registry, "A", "csv.import", {"source": "folder", "folder": "inbox", "pattern": "*.csv"})
    add(project, registry, "A", "files.move", {"which": "imported", "target": "archive", "subfolders": True}, note="Daily")
    group = project.add_group("Files", "teal")
    project.tables[0].group = group.id
    entry = build(project, registry).entries[1]
    assert ix.overview(entry) == ix.Line("the files the import read  (*.csv in inbox)  →  archive  ·  keep subfolders")
    assert ix.tooltip(entry).splitlines() == [
        f"A, step 2: {entry.summary}",
        "Group: Files",
        "Changes the files the import read from " + str(project.directory / "inbox" / "*.csv"),
        "Writes into " + str(project.directory / "archive") + " (keep subfolders)",
        "Note: Daily",
    ]


def test_overview_and_tooltip_with_the_values_now(registry, with_parameters):
    entries = build(with_parameters, registry).entries
    assert ix.tooltip(entries[0]).splitlines()[1:] == [
        r"Reads {Folder}\orders_{month}.csv",
        "    now " + os.path.normpath(r"D:\Data") + os.sep + "orders_2026-10.csv",
    ]
    assert ix.tooltip(entries[2]).splitlines()[-1].startswith("The values now are unknown: The parameter {Bad} has an error")
    command = ix.overview(entries[3])
    assert command.text == "SQL on Local: DELETE FROM t WHERE Day = {Day}   now: DELETE FROM t WHERE Day = '2026-10-01'"
    summary = ix.overview(entries[1])  # no endpoints: the summary
    assert summary.text == entries[1].summary and marked(summary, "parameter") == ["{Day}"]


def test_several_locations_are_joined():
    entry = ix.Entry(
        "t1", "T", True, "", "", "", "s1", 1, "Act", "out", True, "", "", (),
        (Location(Endpoint(OUT, FILE, name="a.csv")), Location(Endpoint(OUT, FILE, name="b.csv"))),
        (), "", "", "",
    )  # fmt: skip
    assert ix.overview(entry).text == "a.csv  in  the project folder; b.csv  in  the project folder"


# --- speed -------------------------------------------------------------------------------------


def _best(work, times=3) -> float:
    best = float("inf")
    for _ in range(times):
        started = time.perf_counter()
        work()
        best = min(best, time.perf_counter() - started)
    return best


def test_speed_with_the_big_project(registry, tmp_path):
    project = _module(ROOT / "tools" / "make_big_project.py").build_project()
    project.path = tmp_path / "big.tafl"
    project.parameters = [Parameter("Folder", "data"), Parameter("Month", "=STRFTIME(TODAY(), '%Y-%m')")]
    for table in project.tables[:8]:
        table.steps[0].params["file"] = "{Folder}/orders_{Month}.csv"
    engine = Engine(project, registry)
    engine.parameter_values()
    index = ix.build(engine)
    assert len(index.entries) >= 450
    assert _best(lambda: ix.build(engine)) < 0.1
    patterns = (Query("orders", regex=True), Query(".", regex=True), Query(r"x\d+_0", regex=True, case=True))
    for query in (Query(), Query("orders"), Query("orders csv"), Query("x"), *patterns):
        for by in ("kind", "table", "place"):
            assert _best(lambda q=query, b=by: ix.sections(index, ix.search(index, q), b)) < 0.1


def test_speed_with_long_texts(registry, project):
    """Many long SQL texts and patterns that match nearly every character (the first key of
    ".*orders"). Before, the excerpts marked every match of every long text: 0.5 s and more here."""
    project.add_connection("Shop", "sqlite", file="shop.db")
    project.parameters = [Parameter("Year", "2026")]
    for n in range(460):
        line = f"UPDATE dbo.orders_{n} SET amount = amount * 1.19 WHERE country = 'DE' AND year = {{Year}};\n"
        add(project, registry, f"T{n}", "db.command", {"connection": "Shop", "sql": (line * 30)[:2000]}, note=f"Note {n}")
    engine = Engine(project, registry)
    engine.parameter_values()
    index = ix.build(engine)
    for pattern in (".", r"\w", "[a-z]", "x*", "orders"):
        query = Query(pattern, regex=True)
        assert _best(lambda q=query: ix.search(index, q)) < 0.1, pattern
    hit = find(index, "Year = 2026", regex=True)[0]  # the excerpt of the SQL with the values now
    assert hit.line.text.startswith("SQL: UPDATE dbo.orders_0 SET") and marked(hit.line, "match") == ["year = 2026"] * 2


def test_a_formula_parameter_is_worked_out_once(registry, project, monkeypatch):
    project.parameters = [Parameter("Month", "=STRFTIME(TODAY(), '%Y-%m')")]
    add(project, registry, "T", "csv.import", {"file": "orders_{Month}.csv"})
    engine = Engine(project, registry)
    engine.parameter_values()
    from tafl.core import parameters

    def fail(*args, **kwargs):
        raise ActionError("worked out again")

    monkeypatch.setattr(parameters, "values", fail)
    assert ix.build(engine).entries[0].values[0].now.startswith("orders_2")


# --- more cases: parameters in places, group names, incomplete steps, odd paths ----------------


def test_the_files_the_import_read_go_by_the_values_now(registry, project):
    """A folder import with {Month} in its file names and a command that takes its files: one
    entry under Place, with the names now; written as the settings have them, the values now too."""
    project.parameters = [Parameter("Month", "2026-02")]
    add(project, registry, "Daily", "csv.import", {"source": "folder", "folder": "inbox", "pattern": "orders_{Month}-*.csv"})
    add(project, registry, "Daily", "files.move", {"which": "imported", "target": "archive"})
    index = build(project, registry)
    imported, moved = index.entries
    files = moved.locations[0]
    assert files.via == imported.locations[0].endpoint and files.via_now == imported.locations[0].now
    assert files.name_key == imported.locations[0].name_key == "orders_2026-02-*.csv"
    inbox = next(s for s in ix.sections(index, find(index), "place") if s.title == "inbox")
    assert [(s.title, names(s.hits)) for s in inbox.sections] == [("orders_2026-02-*.csv", ["Daily 1", "Daily 2"])]
    assert ix.overview(moved).text == (
        "the files the import read  (orders_{Month}-*.csv in inbox)  →  archive   now: orders_2026-02-*.csv"
    )
    assert ix.location_text(files, now=True) == "the files the import read  (orders_2026-02-*.csv in inbox)"
    folder = str(project.directory / "inbox")
    assert ix.tooltip(moved).splitlines()[1:3] == [
        f"Changes the files the import read from {folder}{os.sep}orders_{{Month}}-*.csv",
        f"    now {folder}{os.sep}orders_2026-02-*.csv",
    ]
    assert find(index, "orders_2026-02") and names(find(index, "orders_{month}")) == ["Daily 1"]


def test_the_files_the_import_read_with_a_parameter_in_the_folder(registry, project):
    project.parameters = [Parameter("Base", r"D:\Data")]
    add(project, registry, "Daily", "csv.import", {"source": "folder", "folder": r"{Base}\inbox", "pattern": "*.csv"})
    add(project, registry, "Daily", "files.delete", {"which": "imported"})
    entry = build(project, registry).entries[1]
    assert ix.overview(entry).text == (
        r"the files the import read  (*.csv in {Base}\inbox)  ·  to the Recycle Bin   now: *.csv in D:\Data\inbox"
    )
    assert ix.tooltip(entry).splitlines()[1:3] == [
        r"Changes the files the import read from {Base}\inbox" + os.sep + "*.csv (to the Recycle Bin)",
        "    now " + os.path.normpath(r"D:\Data\inbox") + os.sep + "*.csv (to the Recycle Bin)",
    ]


def test_a_folder_itself_with_a_parameter(registry, project):
    project.parameters = [Parameter("Month", "2026-10")]
    add(project, registry, "Listing", "files.mkdir", {"folder": r"output\{Month}"})
    add(project, registry, "Listing", "files.copy", {"which": "column", "column": "Path", "target": r"archive\{Month}"})
    made, copied = build(project, registry).entries
    assert ix.overview(made).text == r"output\{Month}  ·  create   now: output\2026-10"
    assert ix.overview(copied).text == r"files listed in column Path  →  archive\{Month}   now: archive\2026-10"


def test_the_sheet_of_a_folder_import(registry, project):
    """The sheet of all workbooks in a folder is in the written part, as in "now:" (before, only there)."""
    project.parameters = [Parameter("Sheet", "Orders")]
    add(project, registry, "Books", "excel.import", {"source": "folder", "folder": "books", "pattern": "*.xlsx", "sheet": "Kunden"})
    add(project, registry, "Books", "excel.import", {"source": "folder", "folder": "books", "pattern": "*.xlsx", "sheet": "{Sheet}"})
    fixed, chosen = build(project, registry).entries
    assert ix.overview(fixed).text == "*.xlsx, sheet Kunden  in  books"
    assert ix.overview(chosen).text == "*.xlsx, sheet {Sheet}  in  books   now: *.xlsx, sheet Orders"
    assert ix.tooltip(fixed).splitlines()[1] == "Reads " + str(project.directory / "books" / "*.xlsx") + ", sheet Kunden"


def test_how_a_file_is_read_with_the_values_now(registry, project):
    """"now:" has how the files are read when that differs, and only that when nothing else does.
    Before, it left it out and had the file names again ("now: *.xlsx")."""
    project.parameters = [Parameter("Pat", "2025*"), Parameter("Folder", "archive")]
    sheets = {"source": "folder", "pattern": "*.xlsx", "sheets": "matching", "sheet_pattern": "{Pat}"}
    add(project, registry, "Books", "excel.import", {**sheets, "folder": "books"})
    add(project, registry, "Books", "files.delete", {"which": "imported"})
    add(project, registry, "Moved", "excel.import", {**sheets, "folder": "{Folder}"})
    same_place, deleted, moved = build(project, registry).entries
    assert ix.overview(same_place).text == "*.xlsx  in  books  ·  sheets {Pat}   now: sheets 2025*"
    assert ix.overview(moved).text == "*.xlsx  in  {Folder}  ·  sheets {Pat}   now: *.xlsx in archive  ·  sheets 2025*"
    assert ix.overview(deleted).text == "the files the import read  (*.xlsx in books)  ·  to the Recycle Bin"  # no sheets


def test_a_match_only_in_the_group_name_is_shown(registry, demo):
    group = demo.add_group("Sources", "teal")
    demo.tables[0].group = group.id
    index = build(demo, registry)
    hits = find(index, "sources")
    assert names(hits) == ["Customers 1", "Customers 2", "Customers 3"]
    assert hits[0].line.text == "Group: Sources" and marked(hits[0].line, "match") == ["Sources"]
    assert hits[0].focus == ("file", 0, "")  # Enter: the file, as without a search
    hit = find(index, "sources kunden")[0]  # the sheet matches one word, the group the other: the sheet
    assert hit.value.key == "sheet"


def test_what_the_list_shows_of_a_file_is_found(registry, project):
    add(project, registry, "T", "excel.import", {"file": "customers.xlsx"})
    add(project, registry, "T", "files.delete", {"which": "column", "column": "Path"})
    add(project, registry, "T", "files.copy", {"which": "column", "column": "Path", "target": "copies", "subfolders": True})
    index = build(project, registry)
    for text, step in (("first sheet", "T 1"), ("recycle", "T 2"), ("keep subfolders", "T 3")):
        hits = find(index, text)
        assert names(hits) == [step], text
        assert hits[0].line.text == ix.overview(hits[0].entry).text and marked(hits[0].line, "match"), text
    assert names(find(index, "for good")) == []


def test_enter_on_how_a_file_is_read_or_written_goes_to_the_setting_that_says_it(registry, project):
    """Before, Enter on "to the Recycle Bin" went to the files' setting (Which files), not to Delete."""
    add(project, registry, "T", "excel.import", {"file": "customers.xlsx"})
    add(project, registry, "T", "files.delete", {"which": "column", "column": "Path"})
    add(project, registry, "T", "files.copy", {"which": "column", "column": "Path", "target": "copies", "subfolders": True})
    add(project, registry, "T", "excel.export", {"file": "report.xlsx", "sheet": "Data", "existing": "sheet"})
    add(project, registry, "T", "db.export", {"connection": "Shop", "table": "dbo.T", "mode": "upsert", "key_columns": ["No"]})
    add(project, registry, "T", "files.mkdir", {"folder": "made"})
    add(project, registry, "U", "csv.import", {"source": "folder", "folder": "inbox", "pattern": "*.csv", "subfolders": True})
    add(project, registry, "V", "excel.import", {"source": "folder", "folder": "books", "sheets": "all"})
    index = build(project, registry)
    for text, step, setting in (
        ("first sheet", "T 1", "sheet"),
        ("recycle", "T 2", "how"),
        ("keep subfolders", "T 3", "subfolders"),
        ("replace only", "T 4", "existing"),
        ("update or add", "T 5", "mode"),
        ("create", "T 6", "folder"),  # no setting of its own: the folder
        ("with subfolders", "U 1", "subfolders"),
        ("all sheets", "V 1", "sheets"),
    ):
        hits = find(index, text)
        assert names(hits) == [step] and hits[0].value.key == ix.MODE, text
        assert hits[0].focus == (setting, 0, ""), text


def test_enter_on_a_step_that_is_not_complete_goes_to_the_empty_setting(registry, project):
    add(project, registry, "T", "csv.import", {"file": ""})
    add(project, registry, "T", "table.merge", {"other": "", "match": [{"column": "a", "other_column": ""}]})
    add(project, registry, "T", "rows.sort", {"by": [{"column": "x"}]})
    index = build(project, registry)
    imported, merged, sorted_ = (ix._hit(e, None, Query(off=True), None, None) for e in index.entries)
    assert imported.focus == ("file", 0, "") and imported.entry.empty == ("file", 0, "")
    assert merged.focus == ("other", 0, "")
    assert sorted_.focus is None and sorted_.entry.empty is None
    project.tables[0].steps[1].params["other"] = project.tables[0].id
    entry = build(project, registry).entries[1]
    assert entry.empty == ("match", 1, "other_column")


def test_odd_paths(registry, project, tmp_path):
    add(project, registry, "T", "csv.import", {"file": "a.csv"})
    add(project, registry, "T", "csv.export", {"file": "C:x.csv"})
    add(project, registry, "T", "csv.export", {"file": "C:\\"})
    index = build(project, registry)
    drive, top = index.entries[1].locations[0], index.entries[2].locations[0]
    assert (drive.endpoint.place, drive.endpoint.name, drive.folder) == ("C:", "x.csv", "C:")
    assert ix.location_text(drive) == "x.csv  in  C:" and ix.tooltip(index.entries[1]).splitlines()[1] == "Writes into C:x.csv"
    assert ix.location_text(top) == "(no file name)  in  C:\\"
    place = {s.title: [n.title for n in s.sections] for s in ix.sections(index, find(index), "place")}
    assert place["C:"] == ["x.csv"] and place["C:\\"] == ["(no file name)"]
    assert ix.full_path("C:data", tmp_path) == "C:data" and ix.full_path("C:", tmp_path) == "C:"

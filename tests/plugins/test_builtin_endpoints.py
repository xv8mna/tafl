"""What the built-in actions read, write or change outside the project (their endpoints)."""

import pytest

from tafl.api import Endpoint, Kind
from tafl.api.endpoints import CMD, DATABASE, FILE, FOLDER, IN, OUT, SQL
from tafl.core.index import step_kind


@pytest.fixture
def endpoints(registry):
    def of(action_id: str, params: dict) -> list[Endpoint]:
        action = registry.actions[action_id]
        return action.endpoints_of(action.resolve(params))

    return of


@pytest.mark.parametrize("action_id", ["csv.import", "parquet.import"])
def test_imports_of_one_file_or_a_folder(endpoints, action_id):
    assert endpoints(action_id, {"file": "data/orders.csv"}) == [
        Endpoint(IN, FILE, place="data", name="orders.csv", setting="file")
    ]
    folder = {"source": "folder", "folder": r"{Inbox}\daily", "pattern": "orders_*", "subfolders": True}
    assert endpoints(action_id, folder) == [
        Endpoint(
            IN,
            FOLDER,
            place=r"{Inbox}\daily",
            name="orders_*",
            mode="with subfolders",
            setting="folder",
            mode_setting="subfolders",
            folder_import=True,
        )
    ]
    assert endpoints(action_id, {"source": "folder", "folder": "inbox", "pattern": ""})[0].name == "*"
    assert endpoints(action_id, {}) == []


def test_excel_import_says_the_sheets(endpoints):
    one = endpoints("excel.import", {"file": "data/customers.xlsx", "sheet": "Kunden"})
    assert one == [Endpoint(IN, FILE, place="data", name="customers.xlsx", part="Kunden", setting="file")]
    first = endpoints("excel.import", {"file": "customers.xlsx"})
    assert first == [Endpoint(IN, FILE, name="customers.xlsx", mode="first sheet", setting="file", mode_setting="sheet")]
    every = endpoints("excel.import", {"file": "c.xlsx", "sheets": "all", "sheet": "Hidden"})
    assert every == [Endpoint(IN, FILE, name="c.xlsx", mode="all sheets", setting="file", mode_setting="sheets")]
    folder = {"source": "folder", "folder": "in", "sheets": "matching", "sheet_pattern": "2025*", "subfolders": True}
    assert endpoints("excel.import", folder) == [
        Endpoint(
            IN,
            FOLDER,
            place="in",
            name="*.xlsx;*.xlsm;*.xlsb;*.xls",
            mode="sheets 2025*, with subfolders",
            setting="folder",
            mode_setting="sheet_pattern",
            folder_import=True,
        )
    ]
    one = endpoints("excel.import", {"source": "folder", "folder": "in", "sheet": "One", "subfolders": True})[0]
    assert (one.part, one.mode, one.mode_setting, one.folder_import) == ("One", "with subfolders", "subfolders", True)
    assert endpoints("excel.import", {}) == []


def test_file_exports(endpoints):
    assert endpoints("csv.export", {"file": r"output\orders.csv"}) == [
        Endpoint(OUT, FILE, place="output", name="orders.csv", setting="file")
    ]
    assert endpoints("excel.export", {"file": "output/orders.xlsx", "sheet": "Orders"}) == [
        Endpoint(OUT, FILE, place="output", name="orders.xlsx", part="Orders", setting="file")
    ]
    kept = endpoints("excel.export", {"file": "report.xlsx", "sheet": "Data", "existing": "sheet"})
    assert kept == [
        Endpoint(OUT, FILE, name="report.xlsx", part="Data", mode="replace only this sheet", setting="file", mode_setting="existing")
    ]
    assert endpoints("csv.export", {}) == endpoints("excel.export", {"file": " "}) == []


def test_database_import_of_a_table_or_a_query(endpoints):
    assert endpoints("db.import", {"connection": "Shop", "table": "dbo.Orders"}) == [
        Endpoint(IN, DATABASE, place="Shop", name="dbo.Orders", setting="table")
    ]
    query = {"connection": "Shop", "mode": "query", "table": "dbo.Orders", "query": "SELECT * FROM x"}
    assert endpoints("db.import", query) == [Endpoint(IN, SQL, place="Shop", name="SELECT * FROM x", setting="query")]
    assert endpoints("db.import", {"connection": "Shop"}) == [Endpoint(IN, DATABASE, place="Shop", setting="table")]
    assert endpoints("db.import", {}) == []


@pytest.mark.parametrize(
    ("mode", "words"),
    [
        ("insert", "add rows"),
        ("replace", "replace all rows"),
        ("replace_keys", "replace rows by Day, Shop"),
        ("replace_where", "replace rows where …"),
        ("update", "update rows by Day, Shop"),
        ("upsert", "update or add rows by Day, Shop"),
        ("delete", "delete rows by Day, Shop"),
    ],
)
def test_database_export_says_what_it_does(endpoints, mode, words):
    params = {"connection": "Shop", "table": "dbo.Sales", "mode": mode, "key_columns": ["Day", "Shop"], "condition": "1 = 1"}
    assert endpoints("db.export", params) == [
        Endpoint(OUT, DATABASE, place="Shop", name="dbo.Sales", mode=words, setting="table", mode_setting="mode")
    ]


def test_database_export_without_keys_and_sql_commands(endpoints):
    assert endpoints("db.export", {"connection": "Shop", "table": "T", "mode": "update"})[0].mode == "update rows by ?"
    assert endpoints("db.export", {}) == []
    assert endpoints("db.command", {"connection": "Shop", "sql": "EXEC dbo.Refresh"}) == [
        Endpoint(CMD, SQL, place="Shop", name="EXEC dbo.Refresh", setting="sql")
    ]
    assert endpoints("db.command", {}) == []


def test_files_in_a_folder(endpoints):
    """It reads no files for the later steps (they can't take "the files the import read")."""
    assert endpoints("files.list", {"folder": "inbox", "pattern": "*.csv", "subfolders": True}) == [
        Endpoint(IN, FOLDER, place="inbox", name="*.csv", mode="with subfolders", setting="folder", mode_setting="subfolders")
    ]
    assert endpoints("files.list", {"folder": "inbox", "pattern": " "}) == [
        Endpoint(IN, FOLDER, place="inbox", name="*", setting="folder")
    ]
    assert endpoints("files.list", {}) == []


@pytest.mark.parametrize(("action_id", "direction"), [("files.copy", IN), ("files.move", CMD)])
def test_copy_and_move_files(endpoints, action_id, direction):
    listed = {"column": "Path", "base": "inbox", "target": "archive", "subfolders": True}
    assert endpoints(action_id, listed) == [
        Endpoint(direction, FILE, place="inbox", setting="column", column="Path"),
        Endpoint(OUT, FOLDER, place="archive", mode="keep subfolders", setting="target", mode_setting="subfolders"),
    ]
    imported = {"which": "imported", "column": "Path", "target": "archive"}
    assert endpoints(action_id, imported) == [
        Endpoint(direction, FILE, setting="which", imported=True),
        Endpoint(OUT, FOLDER, place="archive", setting="target"),
    ]
    assert endpoints(action_id, {"column": "Full path"}) == [Endpoint(direction, FILE, setting="column", column="Full path")]
    assert endpoints(action_id, {}) == []


def test_delete_rename_and_create_a_folder(endpoints):
    assert endpoints("files.delete", {"column": "Full path"}) == [
        Endpoint(CMD, FILE, mode="to the Recycle Bin", setting="column", column="Full path", mode_setting="how")
    ]
    assert endpoints("files.delete", {"which": "imported", "how": "permanent"}) == [
        Endpoint(CMD, FILE, mode="for good", setting="which", imported=True, mode_setting="how")
    ]
    rename = {"column": "Path", "base": "inbox", "new_name": "New name", "which": "imported"}  # rename has no "which"
    assert endpoints("files.rename", rename) == [
        Endpoint(CMD, FILE, place="inbox", mode="new names in New name", setting="column", column="Path", mode_setting="new_name")
    ]
    assert endpoints("files.rename", {}) == []
    assert endpoints("files.mkdir", {"folder": r"archive\{Month}"}) == [
        Endpoint(CMD, FOLDER, place=r"archive\{Month}", mode="create", setting="folder")
    ]
    assert endpoints("files.mkdir", {}) == []


def test_transforms_and_starting_from_a_table_have_none(registry, endpoints):
    assert endpoints("table.derive", {"table": "t1"}) == []
    for action in registry.actions.values():
        if action.kind == Kind.TRANSFORM:
            assert action.endpoints is None, action.id
            params = action.resolve({})
            assert action.endpoints_of(params) == [], action.id


def test_every_built_in_action_with_something_outside_tells_it(registry):
    for action in registry.actions.values():
        outside = action.kind in (Kind.EXPORT, Kind.COMMAND) or (action.kind == Kind.SOURCE and action.id != "table.derive")
        assert (action.endpoints is not None) == outside, action.id


def test_kinds_of_the_built_in_actions(registry):
    kinds = {action.id: step_kind(action) for action in registry.actions.values()}
    assert {k for k, v in kinds.items() if v == "in"} == {
        "csv.import",
        "excel.import",
        "parquet.import",
        "db.import",
        "files.list",
    }
    assert {k for k, v in kinds.items() if v == "out"} == {"csv.export", "excel.export", "db.export"}
    assert {k for k, v in kinds.items() if v == "cmd"} == {
        "db.command",
        "files.copy",
        "files.move",
        "files.rename",
        "files.delete",
        "files.mkdir",
    }
    assert kinds["table.derive"] == kinds["table.merge"] == kinds["calc.columns"] == "step"
    assert step_kind(None) == "step"


def test_endpoints_with_parameters_as_written_and_filled_in(endpoints):
    written = endpoints("csv.import", {"file": "{Folder}/orders_{Month}.csv"})
    filled = endpoints("csv.import", {"file": "D:/Data/orders_2026-10.csv"})
    assert written[0].place == "{Folder}" and filled[0].place == "D:/Data"
    assert written[0].name == "orders_{Month}.csv" and filled[0].name == "orders_2026-10.csv"

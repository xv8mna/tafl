"""The endpoints of actions: what a step reads, writes or changes outside the project."""

import pytest

import tafl.api as api
from tafl.api import Endpoint, P, action, file_endpoint, source_endpoints, source_params
from tafl.api.endpoints import CMD, DATABASE, FILE, FOLDER, IN, OUT, SQL, default_endpoints


def _action(kind="transform", params=(), endpoints=None):
    return action("test.endpoints", "Test", kind=kind, params=params, endpoints=endpoints)(lambda df, p, ctx: df)


def test_the_api_exports_endpoints():
    for name in ("Endpoint", "endpoints", "file_endpoint", "source_endpoints"):
        assert name in api.__all__ and hasattr(api, name)
    assert api.API_VERSION == 1
    assert api.endpoints.Endpoint is Endpoint


@pytest.mark.parametrize(
    ("path", "place", "name"),
    [
        ("orders.csv", "", "orders.csv"),
        ("data/orders.csv", "data", "orders.csv"),
        (r"{Folder}\orders.csv", "{Folder}", "orders.csv"),
        (r"D:\Data\in\orders_{Month}.csv", r"D:\Data\in", "orders_{Month}.csv"),
        (r"C:\orders.csv", "C:\\", "orders.csv"),
        (r"\\server\share\orders.csv", r"\\server\share", "orders.csv"),
        ("/orders.csv", "/", "orders.csv"),
        ("  data/orders.csv  ", "data", "orders.csv"),
        ("", "", ""),
    ],
)
def test_file_endpoint_splits_the_path_as_written(path, place, name):
    endpoint = file_endpoint(path, OUT, part="Orders", mode="replace only this sheet", setting="file", mode_setting="existing")
    assert endpoint == Endpoint(
        OUT, FILE, place=place, name=name, part="Orders", mode="replace only this sheet", setting="file", mode_setting="existing"
    )


def test_default_endpoints_come_from_file_folder_and_connection_settings():
    params = [
        P.Choice("mode", "Mode", ["a", "b"]),
        P.File("file", "File"),
        P.Folder("folder", "Folder"),
        P.Text("text", "Text"),
        P.File("hidden", "Hidden", visible_if={"mode": "b"}),
        P.ConnectionRef("connection", "Connection"),
    ]
    test = _action("source", params)
    found = default_endpoints(test, test.resolve({"file": r"in\orders.csv", "folder": "archive", "text": "x.csv", "hidden": "h.csv", "connection": "Shop"}))
    assert found == [
        Endpoint(IN, FILE, place="in", name="orders.csv", setting="file"),
        Endpoint(IN, FOLDER, place="archive", setting="folder"),
        Endpoint(IN, DATABASE, place="Shop", setting="connection"),
    ]


@pytest.mark.parametrize(("kind", "direction"), [("source", IN), ("transform", IN), ("export", OUT), ("command", CMD)])
def test_the_direction_comes_from_the_kind_of_the_action(kind, direction):
    test = _action(kind, [P.File("file", "File")])
    assert test.endpoints_of(test.resolve({"file": "a.csv"})) == [Endpoint(direction, FILE, name="a.csv", setting="file")]


def test_a_connection_with_shown_sql_is_sql():
    params = [
        P.ConnectionRef("connection", "Connection"),
        P.Choice("mode", "Read", ["table", "query"]),
        P.Text("table", "Table", visible_if={"mode": "table"}),
        P.Code("query", "Query", visible_if={"mode": "query"}),
        P.Code("formula", "Python", language="python"),
    ]
    test = _action("source", params)
    query = test.resolve({"connection": "Shop", "mode": "query", "query": "SELECT 1", "formula": "x"})
    assert test.endpoints_of(query) == [Endpoint(IN, SQL, place="Shop", name="SELECT 1", setting="query")]
    table = test.resolve({"connection": "Shop", "mode": "table", "query": "SELECT 1"})
    assert test.endpoints_of(table) == [Endpoint(IN, DATABASE, place="Shop", setting="connection")]
    empty = test.resolve({"connection": "Shop", "mode": "query", "query": "  "})
    assert test.endpoints_of(empty) == [Endpoint(IN, DATABASE, place="Shop", setting="connection")]


def test_files_and_folders_in_the_rows_of_a_list():
    params = [
        P.List(
            "copies",
            "Copies",
            fields=[
                P.Choice("to", "To", ["file", "folder"]),
                P.File("file", "File", visible_if={"to": "file"}),
                P.Folder("folder", "Folder", visible_if={"to": "folder"}),
            ],
        )
    ]
    test = _action("export", params)
    rows = [{"to": "file", "file": "a/b.csv", "folder": "x"}, {"to": "folder", "folder": "out"}, {"to": "file", "file": ""}]
    assert test.endpoints_of(test.resolve({"copies": rows})) == [
        Endpoint(OUT, FILE, place="a", name="b.csv", setting="copies"),
        Endpoint(OUT, FOLDER, place="out", setting="copies"),
    ]


def test_empty_and_odd_values_give_no_endpoints():
    test = _action("export", [P.File("file", "File"), P.Folder("folder", "Folder"), P.ConnectionRef("connection", "Connection")])
    assert test.endpoints_of(test.resolve({})) == []
    assert test.endpoints_of(test.resolve({"file": "  ", "folder": 5, "connection": ["x"]})) == []
    assert _action("transform", [P.Text("text", "Text")]).endpoints_of(P.Params(text="a.csv")) == []


def test_an_action_tells_its_own_endpoints():
    test = _action("source", [P.Text("url", "Address")], endpoints=lambda p: [Endpoint(IN, FILE, place=p.url, name="data.json")])
    assert test.endpoints is not None
    assert test.endpoints_of(test.resolve({"url": "https://example.com"})) == [Endpoint(IN, FILE, place="https://example.com", name="data.json")]


def test_a_broken_endpoints_function_gives_the_default_and_other_things_are_left_out():
    def broken(p):
        raise ValueError("broken")

    test = _action("export", [P.File("file", "File")], endpoints=broken)
    assert test.endpoints_of(test.resolve({"file": "a.csv"})) == [Endpoint(OUT, FILE, name="a.csv", setting="file")]
    mixed = _action("export", [P.File("file", "File")], endpoints=lambda p: ["a.csv", None, Endpoint(OUT, FOLDER, place="out")])
    assert mixed.endpoints_of(mixed.resolve({"file": "a.csv"})) == [Endpoint(OUT, FOLDER, place="out")]
    generator = _action("export", endpoints=lambda p: (Endpoint(OUT, FILE, name=name) for name in "ab"))
    assert [e.name for e in generator.endpoints_of(P.Params())] == ["a", "b"]


def test_old_plugins_without_endpoints_keep_working():
    @action("test.old", "Old", kind="export", params=[P.File("file", "File")])
    def old(df, p, ctx):
        return df

    assert old.endpoints is None
    assert old.endpoints_of(old.resolve({"file": "out/x.csv"})) == [Endpoint(OUT, FILE, place="out", name="x.csv", setting="file")]


def test_source_endpoints_of_one_file_and_of_a_folder():
    spec = source_params("Text files (*.txt)", "*.txt")
    test = _action("source", spec, endpoints=source_endpoints)
    assert test.endpoints_of(test.resolve({"file": r"{Inbox}\orders.txt"})) == [
        Endpoint(IN, FILE, place="{Inbox}", name="orders.txt", setting="file")
    ]
    folder = test.resolve({"source": "folder", "folder": "inbox", "pattern": "orders_*.txt", "subfolders": True})
    assert source_endpoints(folder) == [
        Endpoint(
            IN,
            FOLDER,
            place="inbox",
            name="orders_*.txt",
            mode="with subfolders",
            setting="folder",
            mode_setting="subfolders",
            folder_import=True,
        )
    ]
    assert source_endpoints(test.resolve({"source": "folder", "folder": "inbox", "pattern": ""})) == [
        Endpoint(IN, FOLDER, place="inbox", name="*", setting="folder", folder_import=True)
    ]
    assert source_endpoints(test.resolve({"source": "folder", "folder": "", "file": "a.txt"})) == []
    assert source_endpoints(test.resolve({"file": ""})) == []
    assert source_endpoints({}) == []


def test_an_import_with_source_params_gets_its_endpoints_without_saying_so():
    """As with ``endpoints=source_endpoints`` (an import written before it existed). Before, its
    folder was the folder itself, without the file names, and no import of the files in it."""
    spec = [*source_params("Text files (*.txt)", "*.txt"), P.ConnectionRef("connection", "Look up in")]
    old = _action("source", spec)
    folder = old.resolve({"source": "folder", "folder": "inbox", "pattern": "orders_*.txt", "subfolders": True, "connection": "Shop"})
    assert old.endpoints_of(folder) == [*source_endpoints(folder), Endpoint(IN, DATABASE, place="Shop", setting="connection")]
    assert old.endpoints_of(folder)[0].folder_import
    one = old.resolve({"file": r"in\a.txt", "folder": "inbox"})
    assert old.endpoints_of(one) == [Endpoint(IN, FILE, place="in", name="a.txt", setting="file")] == source_endpoints(one)
    assert old.endpoints_of(old.resolve({"source": "folder", "folder": " "})) == []
    # Only an import whose folder is that of source_params: other folders stay the folder itself
    command = _action("command", spec)
    assert command.endpoints_of(folder) == [
        Endpoint(CMD, FOLDER, place="inbox", setting="folder"),
        Endpoint(CMD, DATABASE, place="Shop", setting="connection"),
    ]
    own = _action("source", [P.Folder("folder", "Folder"), P.Text("pattern", "Names"), P.Choice("source", "Read", ["folder"])])
    assert own.endpoints_of(own.resolve({"folder": "inbox", "pattern": "*.txt", "source": "folder"})) == [
        Endpoint(IN, FOLDER, place="inbox", setting="folder")
    ]

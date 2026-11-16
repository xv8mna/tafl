"""The columns that a step adds (``adds_columns``) are the columns that the imports make: for a
folder of 2 files with every combination of the switches, the columns before the data are the
declared ones whose ``when`` fits the step's settings, in that order and with that type."""

import datetime as dt
import itertools

import polars as pl
import pytest
import xlsxwriter

from tafl.api import AddedColumn, SkipRest

DATA = ["Id", "Name"]  # the columns of every file and sheet below


def type_name(dtype: pl.DataType) -> str:
    """The type name of Change type for a Polars type."""
    if dtype == pl.String:
        return "text"
    if dtype.is_integer():
        return "integer"
    if dtype.is_float() or isinstance(dtype, pl.Decimal):
        return "number"
    if dtype == pl.Date:
        return "date"
    if dtype == pl.Datetime:
        return "datetime"
    if dtype == pl.Boolean:
        return "boolean"
    return str(dtype)


def switched_on(column: AddedColumn, settings) -> bool:
    """``when`` read as a ``visible_if``: each setting has the value, or one of the values."""
    for key, expected in (column.when or {}).items():
        allowed = expected if isinstance(expected, (tuple, list, set, frozenset)) else (expected,)
        if settings[key] not in allowed:
            return False
    return True


def check_added(registry, act, action_id: str, settings: dict, data: list[str] = DATA) -> list[str]:
    """Runs the import and compares the columns before ``data`` with the declaration. Returns the
    names of the added columns."""
    action = registry.actions[action_id]
    resolved = action.resolve(settings)
    expected = [column for column in action.adds_columns if switched_on(column, resolved)]
    out = act(action_id, None, settings)
    assert out.columns == [column.name for column in expected] + data, settings
    assert [type_name(out.schema[column.name]) for column in expected] == [column.type for column in expected], settings
    return [column.name for column in expected]


@pytest.fixture
def csv_folder(tmp_path):
    folder = tmp_path / "csv"
    folder.mkdir()
    (folder / "a.csv").write_text("Id;Name\n1;Anna\n2;Ben\n", encoding="utf-8")
    (folder / "b.csv").write_text("Id;Name\n3;Carla\n", encoding="utf-8")
    return folder


@pytest.fixture
def parquet_folder(tmp_path):
    folder = tmp_path / "parquet"
    folder.mkdir()
    pl.DataFrame({"Id": [1, 2], "Name": ["Anna", "Ben"]}).write_parquet(folder / "a.parquet")
    pl.DataFrame({"Id": [3], "Name": ["Carla"]}).write_parquet(folder / "b.parquet")
    return folder


@pytest.fixture
def excel_folder(tmp_path):
    """2 workbooks, each with the sheets Jan and Feb."""
    folder = tmp_path / "excel"
    folder.mkdir()
    for name, first in (("a.xlsx", 1), ("b.xlsx", 10)):
        with xlsxwriter.Workbook(folder / name) as book:
            for number, sheet in enumerate(("Jan", "Feb")):
                cells = book.add_worksheet(sheet)
                cells.write_row(0, 0, DATA)
                cells.write_row(1, 0, [first + number, f"{sheet} {name}"])
    return folder


SWITCHES = list(itertools.product(["file", "folder"], [True, False], [True, False]))


@pytest.mark.parametrize(("action_id", "folder_fixture", "first"), [("csv.import", "csv_folder", "a.csv"), ("parquet.import", "parquet_folder", "a.parquet")])
def test_the_columns_of_a_file_import_are_those_declared(registry, act, request, action_id, folder_fixture, first):
    folder = request.getfixturevalue(folder_fixture)
    seen = set()
    for source, file_column, path_column in SWITCHES:
        settings = {"source": source, "file": str(folder / first), "folder": str(folder), "file_column": file_column, "path_column": path_column}
        seen.add(tuple(check_added(registry, act, action_id, settings)))
    assert seen == {(), ("File",), ("Path",), ("File", "Path")}  # every declared column was looked at


def test_the_columns_of_an_excel_import_are_those_declared(registry, act, excel_folder):
    seen = set()
    for (source, file_column, path_column), sheets, sheet_column in itertools.product(SWITCHES, ["one", "all", "matching"], [True, False]):
        settings = {
            "source": source,
            "file": str(excel_folder / "a.xlsx"),
            "folder": str(excel_folder),
            "file_column": file_column,
            "path_column": path_column,
            "sheets": sheets,
            "sheet_pattern": "J*",
            "sheet_column": sheet_column,
        }
        seen.add(tuple(check_added(registry, act, "excel.import", settings)))
    assert ("File", "Path", "Sheet") in seen and ("Sheet",) in seen and () in seen and len(seen) == 8


@pytest.mark.parametrize("action_id", ["csv.import", "parquet.import", "excel.import"])
def test_an_empty_folder_gives_the_declared_columns(registry, act, tmp_path, action_id):
    """With "Continue with an empty table" and with "Skip the rest of the table" (the table the
    step ends with): the columns are the declared ones whose when fits, and nothing else."""
    sheets = [("one", True), ("all", True), ("all", False), ("matching", True)] if action_id == "excel.import" else [("one", True)]
    seen = set()
    for (file_column, path_column), (choice, sheet_column), no_files in itertools.product(
        itertools.product([True, False], repeat=2), sheets, ["empty", "skip"]
    ):
        settings = {"source": "folder", "folder": str(tmp_path), "file_column": file_column, "path_column": path_column, "no_files": no_files}
        if action_id == "excel.import":
            settings |= {"sheets": choice, "sheet_pattern": "J*", "sheet_column": sheet_column}
        try:
            out = act(action_id, None, settings)
        except SkipRest as skipped:
            out = skipped.df
        action = registry.actions[action_id]
        expected = [column for column in action.adds_columns if switched_on(column, action.resolve(settings))]
        assert [(name, type_name(dtype)) for name, dtype in out.schema.items()] == [(c.name, c.type) for c in expected], settings
        assert out.height == 0
        seen.add(tuple(out.columns))
    assert ("File", "Path", "Sheet") in seen if action_id == "excel.import" else ("File", "Path") in seen


def test_a_column_named_like_an_added_one_gets_another_name(registry, act, tmp_path):
    """Not declared: the docs say that the added column is then File (source)."""
    (tmp_path / "a.csv").write_text("File;Id\nx.pdf;1\n", encoding="utf-8")
    (tmp_path / "b.csv").write_text("File;Id\ny.pdf;2\n", encoding="utf-8")
    out = act("csv.import", None, {"source": "folder", "folder": str(tmp_path), "path_column": True})
    assert out.columns == ["File (source)", "Path", "File", "Id"]
    assert out["File (source)"].to_list() == ["a.csv", "b.csv"] and out["File"].to_list() == ["x.pdf", "y.pdf"]
    assert [column.name for column in registry.actions["csv.import"].adds_columns] == ["File", "Path"]


@pytest.mark.parametrize("files", [2, 0])
def test_files_in_a_folder_makes_the_declared_columns(registry, act, tmp_path, files):
    for number in range(files):
        (tmp_path / f"{number}.csv").write_text("x", encoding="utf-8")
    out = act("files.list", None, {"folder": str(tmp_path)})
    declared = registry.actions["files.list"].adds_columns
    assert all(column.when is None for column in declared)
    assert [(name, type_name(dtype)) for name, dtype in out.schema.items()] == [(column.name, column.type) for column in declared]
    assert out.height == files
    if files:
        assert isinstance(out["Modified"][0], dt.datetime) and out["Size"].to_list() == [1, 1]


def test_the_type_names_are_those_of_change_type(registry):
    """AddedColumn.TYPES can't drift from the options of Change type."""
    fields = {field.key: field for field in registry.actions["columns.change_type"].params[0].fields}
    assert tuple(fields["type"].values) == AddedColumn.TYPES


def test_only_the_imports_with_fixed_columns_declare_them(registry):
    declared = {action.id: [column.name for column in action.adds_columns] for action in registry.actions.values() if action.adds_columns}
    assert declared == {
        "csv.import": ["File", "Path"],
        "excel.import": ["File", "Path", "Sheet"],
        "files.list": ["File", "Path", "Folder", "Extension", "Size", "Modified", "Full path"],
        "parquet.import": ["File", "Path"],
    }

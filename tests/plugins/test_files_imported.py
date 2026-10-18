"""Copy, move and delete files: "All files this table's import read" takes the files that the
folder import of the table read, also files that gave no rows."""

import types
from pathlib import Path

import polars as pl
import pytest
import send2trash

from tafl.api import ActionError, Mode, action
from tafl.api.testing import StubContext
from tafl.cli import main
from tafl.core import Engine, Project, Registry, Status

NO_FOLDER_IMPORT = (
    "This table doesn't start with an import of all files in a folder, so no files were read: "
    "choose “Listed in a column” in Which files"
)


@pytest.fixture
def inbox(tmp_path) -> Path:
    """The inbox of the daily exports: a file with rows, one with only its header row, and one
    whose rows the filter leaves out."""
    folder = tmp_path / "inbox"
    (folder / "2026").mkdir(parents=True)
    (folder / "a.csv").write_text("Nr;Netto\n1;2,5\n2;-1\n", encoding="utf-8")
    (folder / "header.csv").write_text("Nr;Netto\n", encoding="utf-8")
    (folder / "2026" / "returns.csv").write_text("Nr;Netto\n3;-4\n", encoding="utf-8")
    return folder


ALL = ["2026/returns.csv", "a.csv", "header.csv"]


def files_in(folder: Path) -> list[str]:
    return sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*") if p.is_file()) if folder.exists() else []


@pytest.fixture
def daily(registry, tmp_path, inbox) -> Project:
    """The daily exports of the README: import the inbox, keep the sales, export, move the files."""
    project = Project(name="Daily")
    project.path = tmp_path / "daily.tafl"
    table = project.add_table("Daily")
    source = {"source": "folder", "folder": "inbox", "pattern": "*.csv", "subfolders": True, "no_files": "skip"}
    source["path_column"] = True
    project.add_step(table, registry.actions["csv.import"], source)
    project.add_step(table, registry.actions["rows.filter_formula"], {"condition": "[Netto] > 0"})
    project.add_step(table, registry.actions["csv.export"], {"file": "out/daily.csv"})
    move = {"which": "imported", "target": "archive", "subfolders": True}
    project.add_step(table, registry.actions["files.move"], move)
    return project


def test_all_files_the_import_read_are_moved(registry, daily, tmp_path, inbox):
    result = Engine(daily, registry).run()["t1"]
    assert result.ok and result.df["Nr"].to_list() == [1]
    assert files_in(tmp_path / "archive") == ALL and files_in(inbox) == []  # also the files without rows
    assert result.steps[-1].messages == [("info", f"Moved 3 files to {tmp_path / 'archive'}")]
    exported = (tmp_path / "out" / "daily.csv").read_text(encoding="utf-8-sig")
    assert exported.splitlines() == ["File;Path;Nr;Netto", "a.csv;a.csv;1;2,5"]

    again = Engine(daily, registry).run()["t1"]  # the inbox is empty now: the rest is skipped
    assert again.ok and [s.status for s in again.steps] == [Status.OK, *[Status.SKIPPED] * 3]


def test_listed_in_a_column_moves_only_the_files_with_rows(registry, daily, tmp_path, inbox):
    move = daily.tables[0].steps[-1]
    move.params = {"which": "column", "column": "Path", "base": "inbox", "target": "archive", "subfolders": True}
    assert Engine(daily, registry).run()["t1"].ok
    assert files_in(tmp_path / "archive") == ["a.csv"] and files_in(inbox) == ["2026/returns.csv", "header.csv"]


def test_without_the_subfolders(registry, daily, tmp_path):
    daily.tables[0].steps[-1].params["subfolders"] = False
    assert Engine(daily, registry).run()["t1"].ok
    assert files_in(tmp_path / "archive") == ["a.csv", "header.csv", "returns.csv"]


def _registry_with(*actions) -> Registry:
    module = types.ModuleType("helpers")
    for number, made in enumerate(actions):
        setattr(module, f"action{number}", made)
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    return registry


def test_a_file_that_comes_after_the_import_stays(daily, tmp_path, inbox):
    @action("demo.arrives", "A file arrives", kind="command")
    def arrives(df, p, ctx):
        (inbox / "late.csv").write_text("Nr;Netto\n9;9\n", encoding="utf-8")

    registry = _registry_with(arrives)
    daily.add_step("Daily", registry.actions["demo.arrives"], {}, index=3)
    assert Engine(daily, registry).run()["t1"].ok
    assert files_in(tmp_path / "archive") == ALL and files_in(inbox) == ["late.csv"]  # read by the next run


def test_a_cached_import_keeps_the_files_it_read(registry, daily, tmp_path, inbox):
    engine = Engine(daily, registry)
    preview = engine.evaluate("Daily")
    assert preview.ok and not preview.steps[0].cached and files_in(inbox) == ALL
    run = engine.evaluate("Daily", mode=Mode.RUN)  # no reload: the import comes from the cache
    assert run.ok and run.steps[0].cached
    assert files_in(tmp_path / "archive") == ALL and files_in(inbox) == []


def test_another_table_cant_use_the_files(registry, daily, tmp_path, inbox):
    copies = daily.add_table("Copies")
    daily.add_step(copies, registry.actions["table.derive"], {"table": "t1"})
    daily.add_step(copies, registry.actions["files.copy"], {"which": "imported", "target": "copies"})
    daily.tables[0].steps[-1].enabled = False  # the files stay in the inbox
    results = Engine(daily, registry).run()
    assert results["t1"].ok
    assert results[copies.id].steps[-1].error == NO_FOLDER_IMPORT
    assert files_in(tmp_path / "copies") == [] and files_in(inbox) == ALL


def test_an_import_of_one_file_reads_no_folder(registry, tmp_path, inbox):
    project = Project(name="One")
    project.path = tmp_path / "one.tafl"
    table = project.add_table("One")
    project.add_step(table, registry.actions["csv.import"], {"file": "inbox/a.csv"})
    project.add_step(table, registry.actions["files.move"], {"which": "imported", "target": "archive"})
    assert Engine(project, registry).run()[table.id].steps[-1].error == NO_FOLDER_IMPORT
    assert files_in(inbox) == ALL


def test_the_problem_shows_while_the_project_is_edited(registry, daily, tmp_path, inbox):
    """The command doesn't run in the preview, but says what would stop it in a run: before, the
    step showed nothing until a (scheduled) run failed."""
    project = Project(name="One")
    project.path = tmp_path / "one.tafl"
    table = project.add_table("One")
    project.add_step(table, registry.actions["csv.import"], {"file": "inbox/a.csv"})
    for command in ("files.move", "files.copy", "files.delete"):
        project.add_step(table, registry.actions[command], {"which": "imported", "target": "archive"})
    project.add_step(table, registry.actions["files.move"], {"which": "column", "column": "Nr", "target": "archive"})
    preview = Engine(project, registry).evaluate("One")
    assert preview.ok and [s.status for s in preview.steps] == [Status.OK, *[Status.SKIPPED] * 4]
    for step in preview.steps[1:4]:
        assert step.messages == [("warning", NO_FOLDER_IMPORT), ("info", "Runs only when the project is run")]
    assert preview.steps[4].messages == [("info", "Runs only when the project is run")]
    daily_preview = Engine(daily, registry).evaluate("Daily")  # a folder import: nothing to say
    assert daily_preview.steps[-1].messages == [("info", "Runs only when the project is run")]
    run = Engine(project, registry).run()[table.id]
    assert run.steps[1].status == Status.ERROR and run.steps[1].error == NO_FOLDER_IMPORT
    assert files_in(inbox) == ALL


def test_delete_all_files_the_import_read(registry, daily, inbox, monkeypatch):
    recycled = []
    monkeypatch.setattr(send2trash, "send2trash", lambda path: recycled.append(path))  # not the real bin
    step = daily.tables[0].steps[-1]
    step.action, step.params, step.version = "files.delete", {"which": "imported"}, 2
    assert Engine(daily, registry).run()["t1"].ok
    assert sorted(p.relative_to(inbox).as_posix() for p in recycled) == ALL and files_in(inbox) == ALL
    daily.tables[0].steps[-1].params["how"] = "permanent"
    assert Engine(daily, registry).run()["t1"].ok and files_in(inbox) == []


def test_the_summaries_and_old_steps(registry, daily):
    engine = Engine(daily, registry)
    assert engine.describe(daily.tables[0].steps[-1]) == "Move the files the import read to archive"
    old = daily.add_step("Daily", "files.copy", {"column": "Path", "target": "archive"}, version=1)  # saved by 0.3.0
    assert engine.describe(old) == "Copy the files in Path to archive"
    assert registry.actions["files.copy"].upgrade(old.params, 1)["which"] == "column"
    assert registry.actions["files.rename"].version == 1  # it needs the new names from its column


def test_a_plugin_test_without_an_import(act, tmp_path, inbox):
    table = pl.DataFrame({"Nr": [1]})
    with pytest.raises(ActionError, match=f"^{NO_FOLDER_IMPORT}$"):
        act("files.copy", table, {"which": "imported", "target": str(tmp_path / "out")})
    ctx = StubContext()
    ctx.imported_folder, ctx.imported_files = inbox, (inbox / "a.csv", inbox / "header.csv")
    act("files.copy", table, {"which": "imported", "target": str(tmp_path / "out")}, ctx)
    assert files_in(tmp_path / "out") == ["a.csv", "header.csv"]


def workbook(path: Path, sheets: dict[str, list[list]]) -> None:
    import xlsxwriter

    book = xlsxwriter.Workbook(str(path))
    for name, rows in sheets.items():
        sheet = book.add_worksheet(name)
        for number, row in enumerate(rows):
            sheet.write_row(number, 0, row)
    book.close()


def test_a_workbook_without_a_chosen_sheet_is_no_file_the_import_read(registry, tmp_path):
    """With a sheet pattern, a workbook of the folder whose sheets don't match gives no rows and
    isn't read: it stays in the inbox (e.g. a workbook put there by mistake). One whose matching
    sheet has only a header row was read."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    workbook(inbox / "orders.xlsx", {"Orders": [["Nr"], [1]], "Notes": [["x"]]})
    workbook(inbox / "empty.xlsx", {"Orders 2": [["Nr"]]})
    workbook(inbox / "budget.xlsx", {"Plan": [["Nr"], [2]]})
    project = Project(name="Excel")
    project.path = tmp_path / "excel.tafl"
    table = project.add_table("Orders")
    source = {"source": "folder", "folder": "inbox", "pattern": "*.xlsx", "sheets": "matching", "sheet_pattern": "Orders*"}
    project.add_step(table, registry.actions["excel.import"], source)
    project.add_step(table, registry.actions["files.move"], {"which": "imported", "target": "archive"})
    result = Engine(project, registry).run()[table.id]
    assert result.ok and result.df["File"].to_list() == ["orders.xlsx"]
    assert files_in(tmp_path / "archive") == ["empty.xlsx", "orders.xlsx"] and files_in(inbox) == ["budget.xlsx"]


def test_the_import_notes_the_files_it_read(act, inbox):
    ctx = StubContext()
    act("csv.import", None, {"source": "folder", "folder": str(inbox), "pattern": "*.csv", "subfolders": True}, ctx)
    assert ctx.imported_folder == inbox
    assert ctx.imported_files == (inbox / "2026" / "returns.csv", inbox / "a.csv", inbox / "header.csv")
    ctx = StubContext()
    act("csv.import", None, {"file": str(inbox / "a.csv")}, ctx)
    assert ctx.imported_files is None and ctx.imported_folder is None


# --- on the command line --------------------------------------------------------------------------


def test_the_daily_exports_on_the_command_line(daily, tmp_path, inbox, capsys):
    path = daily.save(tmp_path / "daily.tafl")
    assert main(["run", str(path)]) == 0
    assert "Moved 3 files to" in capsys.readouterr().out
    assert files_in(tmp_path / "archive") == ALL and files_in(inbox) == []
    assert main(["run", str(path)]) == 0  # a day without files


def test_another_table_on_the_command_line(registry, daily, tmp_path, inbox, capsys):
    copies = daily.add_table("Copies")
    daily.add_step(copies, registry.actions["table.derive"], {"table": "t1"})
    daily.add_step(copies, registry.actions["files.copy"], {"which": "imported", "target": "copies"})
    path = daily.save(tmp_path / "daily.tafl")
    assert main(["run", str(path)]) == 1
    output = capsys.readouterr().out
    assert NO_FOLDER_IMPORT in output and "Moved 3 files to" in output
    assert files_in(tmp_path / "archive") == ALL and files_in(tmp_path / "copies") == []

"""File commands on the files listed in a table: copy, move, rename, delete, create a folder."""

import polars as pl
import pytest
import send2trash

from etl_tool.api import ActionError
from etl_tool.api.testing import StubContext
from etl_tool.core import Engine, Project


@pytest.fixture
def inbox(tmp_path):
    folder = tmp_path / "inbox"
    (folder / "2025" / "01").mkdir(parents=True)
    (folder / "a.csv").write_text("a", encoding="utf-8")
    (folder / "2025" / "01" / "b.csv").write_text("b", encoding="utf-8")
    return folder


def listing(act, folder):
    return act("files.list", None, {"folder": str(folder), "subfolders": True})


def test_move_the_listed_files_into_an_archive(act, tmp_path, inbox):
    table = listing(act, inbox)
    assert table["Full path"].to_list() == [str(inbox / "2025" / "01" / "b.csv"), str(inbox / "a.csv")]  # by path
    ctx = StubContext()
    assert act("files.move", table, {"column": "Full path", "target": str(tmp_path / "archive")}, ctx) is table
    assert sorted(p.name for p in (tmp_path / "archive").iterdir()) == ["a.csv", "b.csv"]
    assert not (inbox / "a.csv").exists() and not (inbox / "2025" / "01" / "b.csv").exists()
    assert ctx.messages[-1] == ("info", f"Moved 2 files to {tmp_path / 'archive'}")


def test_copy_keeps_the_subfolders_of_paths_below_a_folder(act, tmp_path, inbox):
    table = listing(act, inbox)
    params = {"column": "Path", "base": str(inbox), "target": str(tmp_path / "copy"), "subfolders": True}
    act("files.copy", table, params)
    assert (tmp_path / "copy" / "2025" / "01" / "b.csv").read_text(encoding="utf-8") == "b"
    assert (inbox / "a.csv").exists()  # copied, not moved


@pytest.mark.parametrize(
    ("existing", "result"),
    [("number", ["a (2).csv", "a.csv"]), ("skip", ["a.csv"]), ("replace", ["a.csv"])],
)
def test_a_file_that_is_there_already(act, tmp_path, inbox, existing, result):
    target = tmp_path / "out"
    target.mkdir()
    (target / "a.csv").write_text("old", encoding="utf-8")
    table = pl.DataFrame({"File": [str(inbox / "a.csv")]})
    act("files.copy", table, {"column": "File", "target": str(target), "existing": existing})
    assert sorted(p.name for p in target.iterdir()) == result
    assert (target / "a.csv").read_text(encoding="utf-8") == ("a" if existing == "replace" else "old")
    with pytest.raises(ActionError, match="exists already"):
        act("files.copy", table, {"column": "File", "target": str(target), "existing": "error"})


def test_missing_files_stop_or_are_skipped(act, tmp_path, inbox):
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "gone.csv"), None]})
    with pytest.raises(ActionError, match="File not found: .*gone.csv"):
        act("files.copy", table, {"column": "File", "target": str(tmp_path / "out")})
    ctx = StubContext()
    act("files.copy", table, {"column": "File", "target": str(tmp_path / "out"), "missing": "skip"}, ctx)
    assert ctx.messages[-1] == ("info", f"Copied 1 file to {tmp_path / 'out'} (1 skipped)")


def test_rename_with_names_from_a_column(act, inbox):
    table = listing(act, inbox).with_columns(New=pl.col("File").str.replace(".csv", ".done", literal=True))
    act("files.rename", table, {"column": "Path", "base": str(inbox), "new_name": "New"})
    assert (inbox / "a.done").exists() and (inbox / "2025" / "01" / "b.done").exists()
    with pytest.raises(ActionError, match="is a path"):
        act("files.rename", pl.DataFrame({"F": [str(inbox / "a.done")], "N": ["sub\\x.csv"]}), {"column": "F", "new_name": "N"})


def test_delete_to_the_recycle_bin_or_for_good(act, inbox, monkeypatch):
    recycled = []
    monkeypatch.setattr(send2trash, "send2trash", lambda path: recycled.append(path))  # not the real bin
    table = listing(act, inbox)
    act("files.delete", table, {"column": "Full path"})
    assert [p.name for p in recycled] == ["b.csv", "a.csv"] and (inbox / "a.csv").exists()
    act("files.delete", table, {"column": "Full path", "how": "permanent"})
    assert not (inbox / "a.csv").exists() and not (inbox / "2025" / "01" / "b.csv").exists()


def test_create_a_folder(act, tmp_path):
    act("files.mkdir", None, {"folder": str(tmp_path / "x" / "y")})
    act("files.mkdir", None, {"folder": str(tmp_path / "x" / "y")})  # there already: fine
    assert (tmp_path / "x" / "y").is_dir()


def test_commands_run_only_when_the_project_is_run(registry, tmp_path, inbox):
    project = Project(name="Archive")
    project.path = tmp_path / "archive.tafl"
    table = project.add_table("Inbox")
    project.add_step(table, registry.actions["files.list"], {"folder": "inbox", "subfolders": True})
    project.add_step(table, registry.actions["files.move"], {"column": "Full path", "target": "archive"})
    engine = Engine(project, registry)
    assert engine.evaluate(table.id).ok and (inbox / "a.csv").exists()  # editing: nothing moves
    assert engine.run()[table.id].ok
    assert not (inbox / "a.csv").exists() and (tmp_path / "archive" / "a.csv").exists()

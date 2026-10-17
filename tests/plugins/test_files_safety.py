"""File commands: all files are checked before the first one changes, files with the same name
never replace each other, and names Windows doesn't allow get a plain message."""

import os
import re
import shutil

import polars as pl
import pytest
import send2trash

from tafl.api import ActionError
from tafl.api.testing import StubContext


@pytest.fixture
def inbox(tmp_path):
    """The same file name in two date folders, as daily exports have it."""
    folder = tmp_path / "inbox"
    for month, text in (("01", "3;4"), ("02", "5;6")):
        (folder / "2025" / month).mkdir(parents=True)
        (folder / "2025" / month / "b.csv").write_text(text, encoding="utf-8")
    (folder / "a.csv").write_text("a", encoding="utf-8")
    return folder


def listing(act, folder, subfolders=True):
    return act("files.list", None, {"folder": str(folder), "subfolders": subfolders})


def files_in(folder):
    """{path below the folder: text} of all files in it."""
    if not folder.exists():
        return {}
    return {p.relative_to(folder).as_posix(): p.read_text(encoding="utf-8") for p in folder.rglob("*") if p.is_file()}


ALL = {"2025/01/b.csv": "3;4", "2025/02/b.csv": "5;6", "a.csv": "a"}


# --- two listed files with the same name ----------------------------------------------------------


@pytest.mark.parametrize("command", ["files.move", "files.copy"])
@pytest.mark.parametrize("existing", ["replace", "error"])
def test_two_files_with_the_same_name_stop_before_anything_changes(act, tmp_path, inbox, command, existing):
    params = {"column": "Full path", "target": str(tmp_path / "flat"), "existing": existing}
    with pytest.raises(ActionError) as error:
        act(command, listing(act, inbox), params)
    message = str(error.value)
    assert str(inbox / "2025" / "01" / "b.csv") in message and str(inbox / "2025" / "02" / "b.csv") in message
    assert f"would both be {'moved' if command == 'files.move' else 'copied'} to {tmp_path / 'flat' / 'b.csv'}" in message
    assert message.endswith("Choose “Keep both” to number them.")  # full paths: the subfolders don't help
    assert files_in(inbox) == ALL and files_in(tmp_path / "flat") == {}


def test_with_paths_below_a_folder_the_message_suggests_the_subfolders(act, tmp_path, inbox):
    params = {"column": "Path", "base": str(inbox), "target": str(tmp_path / "flat"), "existing": "replace"}
    with pytest.raises(ActionError, match="Choose “Keep both” to number them, or keep the subfolders."):
        act("files.move", listing(act, inbox), params)
    params["subfolders"] = True
    act("files.move", listing(act, inbox), params)
    assert files_in(tmp_path / "flat") == ALL


def test_keep_both_numbers_files_with_the_same_name(act, tmp_path, inbox):
    ctx = StubContext()
    act("files.move", listing(act, inbox), {"column": "Full path", "target": str(tmp_path / "flat")}, ctx)
    assert files_in(tmp_path / "flat") == {"b.csv": "3;4", "b (2).csv": "5;6", "a.csv": "a"}
    assert files_in(inbox) == {} and ctx.messages[-1] == ("info", f"Moved 3 files to {tmp_path / 'flat'}")


def test_keep_both_counts_files_there_already_and_the_ones_just_written(act, tmp_path, inbox):
    (tmp_path / "flat").mkdir()
    (tmp_path / "flat" / "b.csv").write_text("old", encoding="utf-8")
    act("files.copy", listing(act, inbox), {"column": "Full path", "target": str(tmp_path / "flat")})
    assert files_in(tmp_path / "flat") == {"b.csv": "old", "b (2).csv": "3;4", "b (3).csv": "5;6", "a.csv": "a"}


def test_skip_leaves_the_second_file_where_it_is(act, tmp_path, inbox):
    ctx = StubContext()
    params = {"column": "Full path", "target": str(tmp_path / "flat"), "existing": "skip"}
    act("files.move", listing(act, inbox), params, ctx)
    assert files_in(tmp_path / "flat") == {"b.csv": "3;4", "a.csv": "a"}
    assert files_in(inbox) == {"2025/02/b.csv": "5;6"}
    assert ctx.messages[-1] == ("info", f"Moved 2 files to {tmp_path / 'flat'} (1 skipped)")


def test_replace_still_replaces_a_file_that_was_there_before(act, tmp_path, inbox):
    (tmp_path / "flat").mkdir()
    (tmp_path / "flat" / "a.csv").write_text("old", encoding="utf-8")
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "2025" / "01" / "b.csv")]})
    act("files.move", table, {"column": "File", "target": str(tmp_path / "flat"), "existing": "replace"})
    assert files_in(tmp_path / "flat") == {"a.csv": "a", "b.csv": "3;4"}


def test_replace_never_replaces_one_of_the_listed_files(act, tmp_path, inbox):
    (inbox / "2025" / "01" / "a.csv").write_text("other a", encoding="utf-8")
    table = listing(act, inbox)  # inbox\a.csv stays where it is, inbox\2025\01\a.csv would replace it
    params = {"column": "Full path", "target": str(inbox), "existing": "replace"}
    with pytest.raises(ActionError, match=r"would replace .*a\.csv, which is in the column Full path too"):
        act("files.move", table.filter(pl.col("File") == "a.csv"), params)
    assert (inbox / "a.csv").read_text(encoding="utf-8") == "a"
    act("files.move", table.filter(pl.col("File") == "a.csv"), {**params, "existing": "number"})
    assert files_in(inbox) == {**ALL, "a (2).csv": "other a"}


def test_replace_never_puts_a_file_over_a_folder(act, tmp_path, inbox):
    (tmp_path / "flat" / "a.csv").mkdir(parents=True)
    table = pl.DataFrame({"File": [str(inbox / "a.csv")]})
    with pytest.raises(ActionError, match=r"a\.csv is a folder: a file can't replace it"):
        act("files.move", table, {"column": "File", "target": str(tmp_path / "flat"), "existing": "replace"})
    act("files.move", table, {"column": "File", "target": str(tmp_path / "flat")})
    assert files_in(tmp_path / "flat") == {"a (2).csv": "a"}


def test_a_file_named_twice_is_copied_once(act, tmp_path, inbox):
    spellings = ["a.csv", str(inbox / "a.csv"), str(inbox / "x" / ".." / "a.csv")]
    if os.name == "nt":
        spellings.append(str(inbox / "a.csv").upper())
    table = pl.DataFrame({"File": spellings})
    ctx = StubContext()
    act("files.copy", table, {"column": "File", "base": str(inbox), "target": str(tmp_path / "out")}, ctx)
    assert files_in(tmp_path / "out") == {"a.csv": "a"}
    assert ctx.messages[-1] == ("info", f"Copied 1 file to {tmp_path / 'out'}")


# --- everything is checked first --------------------------------------------------------------------


@pytest.mark.parametrize("command", ["files.copy", "files.move"])
def test_a_missing_file_stops_before_any_file_is_copied_or_moved(act, tmp_path, inbox, command):
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "2025" / "01" / "b.csv"), str(inbox / "gone.csv")]})
    params = {"column": "File", "target": str(tmp_path / "out")}
    with pytest.raises(ActionError, match=r"File not found: .*gone\.csv"):
        act(command, table, params)
    assert files_in(inbox) == ALL and not (tmp_path / "out").exists()
    act(command, table, {**params, "missing": "skip"})
    act("files.copy", table, {**params, "missing": "skip", "existing": "skip"})  # a second run: no "a (2).csv"
    assert sorted(files_in(tmp_path / "out")) == ["a.csv", "b.csv"]


def test_stop_with_an_error_stops_before_any_file_is_copied(act, tmp_path, inbox):
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / "b.csv").write_text("old", encoding="utf-8")
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "2025" / "01" / "b.csv")]})
    with pytest.raises(ActionError, match=r"b\.csv exists already"):
        act("files.copy", table, {"column": "File", "target": str(tmp_path / "out"), "existing": "error"})
    assert files_in(tmp_path / "out") == {"b.csv": "old"}


def test_a_folder_in_the_list_is_named_as_such(act, tmp_path, inbox):
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "2025")]})
    with pytest.raises(ActionError, match=r"2025 is a folder, not a file"):
        act("files.delete", table, {"column": "File", "how": "permanent"})
    assert (inbox / "a.csv").exists()


def test_delete_checks_all_files_first(act, inbox, monkeypatch):
    recycled = []
    monkeypatch.setattr(send2trash, "send2trash", lambda path: recycled.append(path))  # not the real bin
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "gone.csv")]})
    for how in ("recycle", "permanent"):
        with pytest.raises(ActionError, match="File not found"):
            act("files.delete", table, {"column": "File", "how": how})
    assert recycled == [] and (inbox / "a.csv").exists()
    twice = pl.DataFrame({"File": [str(inbox / "a.csv"), "a.csv"]})
    ctx = StubContext()
    act("files.delete", twice, {"column": "File", "base": str(inbox), "how": "permanent"}, ctx)
    assert not (inbox / "a.csv").exists() and ctx.messages[-1] == ("info", "Deleted 1 file")


def test_a_failure_part_way_says_how_many_files_were_done(act, tmp_path, inbox, monkeypatch):
    copyfileobj = shutil.copyfileobj

    def full_disk(reader, writer, *args):
        if reader.name.endswith("b.csv"):
            writer.write(b"half")
            raise OSError(28, "There is not enough space on the disk")
        return copyfileobj(reader, writer, *args)

    monkeypatch.setattr(shutil, "copyfileobj", full_disk)
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "2025" / "01" / "b.csv")]})
    out = tmp_path / "out"
    message = f"^Can't write {re.escape(str(out / 'b.csv'))}: There is not enough space on the disk. 1 file was copied before it$"
    with pytest.raises(ActionError, match=message):
        act("files.copy", table, {"column": "File", "target": str(out)})
    assert files_in(out) == {"a.csv": "a"}  # no half-written b.csv, no temporary file


# --- folders ----------------------------------------------------------------------------------------


def test_copying_into_a_folder_that_is_a_file(act, tmp_path, inbox):
    (tmp_path / "target is a file").write_text("x", encoding="utf-8")
    table = listing(act, inbox)
    for target, subfolders in ((tmp_path / "target is a file", False), (tmp_path / "out", True)):
        if subfolders:
            (target / "2025").mkdir(parents=True)
            (target / "2025" / "01").write_text("x", encoding="utf-8")  # a file where a subfolder goes
        params = {"column": "Path", "base": str(inbox), "target": str(target), "subfolders": subfolders}
        with pytest.raises(ActionError, match=r"is a file, not a folder$"):
            act("files.copy", table, params)
    assert files_in(tmp_path / "out") == {"2025/01": "x"}


def test_create_folder_gives_plain_messages(act, tmp_path):
    (tmp_path / "file").write_text("x", encoding="utf-8")
    with pytest.raises(ActionError, match=r"^.*file is a file, not a folder$"):
        act("files.mkdir", None, {"folder": str(tmp_path / "file")})
    with pytest.raises(ActionError, match=r"^.*file is a file, not a folder$"):
        act("files.mkdir", None, {"folder": str(tmp_path / "file" / "below")})
    with pytest.raises(ActionError, match=r"^'\?' can't be part of a file or folder name: .*what\?$"):
        act("files.mkdir", None, {"folder": str(tmp_path / "what?")})
    with pytest.raises(ActionError, match=r"^CON is reserved for a device in Windows"):
        act("files.mkdir", None, {"folder": str(tmp_path / "con" / "x")})
    with pytest.raises(ActionError, match=r"^A file or folder name can have at most 255 characters: F{50}… has 256$"):
        act("files.mkdir", None, {"folder": str(tmp_path / "new" / ("F" * 256))})
    assert sorted(os.listdir(tmp_path)) == ["file"]


def test_a_numbered_name_that_would_be_too_long_stops_before_any_file_is_copied(act, tmp_path, inbox):
    long = inbox / ("L" * 251 + ".csv")  # 255 characters, the most Windows allows
    long.write_text("long", encoding="utf-8")
    (tmp_path / "out").mkdir()
    (tmp_path / "out" / long.name).write_text("there", encoding="utf-8")
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(long)]})
    with pytest.raises(ActionError, match=r"^A file or folder name can have at most 255 characters: L{50}… has 259$"):
        act("files.copy", table, {"column": "File", "target": str(tmp_path / "out")})
    assert files_in(tmp_path / "out") == {long.name: "there"}


# --- rename -----------------------------------------------------------------------------------------


@pytest.fixture
def names(tmp_path):
    folder = tmp_path / "names"
    folder.mkdir()
    for name in ("a.csv", "Groß.csv", "readme.txt"):
        (folder / name).write_text(name, encoding="utf-8")
    return folder


def rename(act, table, ctx=None, **params):
    return act("files.rename", table, {"column": "File", "new_name": "New", **params}, ctx)


@pytest.mark.parametrize("existing", ["number", "replace", "skip", "error"])
def test_a_new_name_equal_to_the_current_one_does_nothing(act, names, existing):
    table = listing(act, names, False).with_columns(New=pl.col("File").str.replace(".csv", ".done", literal=True))
    ctx = StubContext()
    rename(act, table.with_columns(File=pl.col("Full path")), ctx, existing=existing)
    assert sorted(os.listdir(names)) == ["Groß.done", "a.done", "readme.txt"]
    assert ctx.messages[-1] == ("info", "Renamed 2 files (1 already had its new name)")


@pytest.mark.parametrize("existing", ["number", "replace", "skip", "error"])
def test_a_change_only_in_upper_and_lower_case_renames_the_file(act, names, existing):
    table = pl.DataFrame({"File": [str(names / "Groß.csv"), str(names / "a.csv")], "New": ["groß.csv", "A.CSV"]})
    ctx = StubContext()
    rename(act, table, ctx, existing=existing)
    assert sorted(os.listdir(names)) == ["A.CSV", "groß.csv", "readme.txt"]
    assert (names / "groß.csv").read_text(encoding="utf-8") == "Groß.csv"
    assert ctx.messages[-1] == ("info", "Renamed 2 files")


@pytest.mark.skipif(os.name != "nt", reason="names in another case find the file only in Windows")
def test_the_name_on_the_disk_counts_not_its_spelling_in_the_table(act, names):
    table = pl.DataFrame({"File": [str(names / "GROß.CSV"), str(names / "README.TXT")], "New": ["Groß.csv", "readme.TXT"]})
    ctx = StubContext()
    rename(act, table, ctx)
    assert sorted(os.listdir(names)) == ["Groß.csv", "a.csv", "readme.TXT"]
    assert ctx.messages[-1] == ("info", "Renamed 1 file (1 already had its new name)")


def test_two_files_renamed_to_the_same_name(act, names):
    table = pl.DataFrame({"File": [str(names / "a.csv"), str(names / "Groß.csv")], "New": ["x.csv", "x.csv"]})
    for existing in ("replace", "error"):
        with pytest.raises(ActionError, match=r"a\.csv and .*Groß\.csv would both be renamed to .*x\.csv\. Choose"):
            rename(act, table, existing=existing)
    assert sorted(os.listdir(names)) == ["Groß.csv", "a.csv", "readme.txt"]
    rename(act, table)
    assert files_in(names) == {"x.csv": "a.csv", "x (2).csv": "Groß.csv", "readme.txt": "readme.txt"}


def test_a_listed_file_is_not_replaced_before_it_is_renamed(act, names):
    swap = pl.DataFrame({"File": [str(names / "a.csv"), str(names / "readme.txt")], "New": ["readme.txt", "a.csv"]})
    with pytest.raises(ActionError, match=r"would replace .*readme\.txt, which is in the column File too"):
        rename(act, swap, existing="replace")
    assert files_in(names)["readme.txt"] == "readme.txt"
    chain = pl.DataFrame({"File": [str(names / "readme.txt"), str(names / "a.csv")], "New": ["b.txt", "readme.txt"]})
    rename(act, chain, existing="error")  # readme.txt is renamed first, then its name is free
    assert files_in(names) == {"b.txt": "readme.txt", "readme.txt": "a.csv", "Groß.csv": "Groß.csv"}


@pytest.mark.parametrize(
    ("new", "message"),
    [
        ("..", r"^'\.\.' can't be a file name: \. and \.\. stand for folders$"),
        (".", r"^'\.' can't be a file name"),
        ("a 08:30.csv", r"^':' can't be part of a file or folder name: a 08:30\.csv\. For a time in a name"),
        ("a?*.csv", r"^'\?' and '\*' can't be part of a file or folder name: a\?\*\.csv$"),
        ("a\tb.csv", r"^Line breaks, tabs and other control characters can't be part"),
        ("a.csv.", r"^A file or folder name can't end with a dot or a space: 'a\.csv\.'$"),
        ("nul.txt", r"^NUL is reserved for a device in Windows and can't be a file or folder name: nul\.txt$"),
        ("com1", r"^COM1 is reserved"),
        ("N" * 300 + ".csv", r"^A file or folder name can have at most 255 characters: N{50}… has 304$"),
    ],
)
def test_names_windows_does_not_allow_stop_before_any_file_is_renamed(act, names, new, message):
    table = pl.DataFrame({"File": [str(names / "a.csv"), str(names / "readme.txt")], "New": ["fine.csv", new]})
    for existing in ("number", "replace"):
        with pytest.raises(ActionError, match=message):
            rename(act, table, existing=existing)
    assert sorted(os.listdir(names)) == ["Groß.csv", "a.csv", "readme.txt"]


def test_rename_checks_all_files_first(act, names):
    table = pl.DataFrame({"File": [str(names / "a.csv"), str(names / "gone.csv")], "New": ["b.csv", "c.csv"]})
    with pytest.raises(ActionError, match="File not found"):
        rename(act, table)
    assert sorted(os.listdir(names)) == ["Groß.csv", "a.csv", "readme.txt"]
    ctx = StubContext()
    rename(act, table, ctx, missing="skip")
    assert sorted(os.listdir(names)) == ["Groß.csv", "b.csv", "readme.txt"]
    assert ctx.messages[-1] == ("info", "Renamed 1 file (1 skipped)")

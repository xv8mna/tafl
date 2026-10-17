"""Moving and copying files changes both files completely or not at all: a file that is open in
another program, read-only or on another drive never leaves a copy behind or loses the file it
was to replace, and the message names the file that is in the way."""

import contextlib
import errno
import os
import re
import stat
import sys
from pathlib import Path

import polars as pl
import pytest
import send2trash

from tafl.api import ActionError, files
from tafl.api.testing import StubContext

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows locks open files")
OPEN = r" is open in another program \(e\.g\. Excel\): close it and run again"


@contextlib.contextmanager
def held(path: Path, share_read: bool):
    """Holds a file open as another program does: Excel lets others read an open file, some
    programs don't."""
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    handle = kernel.CreateFileW(str(path), 0x80000000 | 0x40000000, 1 if share_read else 0, None, 3, 0, None)
    assert handle not in (None, wintypes.HANDLE(-1).value), ctypes.get_last_error()
    try:
        yield
    finally:
        kernel.CloseHandle(handle)


@pytest.fixture
def folders(tmp_path):
    """inbox\\a.csv and b.csv, and archive\\a.csv from yesterday."""
    inbox, archive = tmp_path / "inbox", tmp_path / "archive"
    inbox.mkdir()
    archive.mkdir()
    (inbox / "a.csv").write_text("new a", encoding="utf-8")
    (inbox / "b.csv").write_text("new b", encoding="utf-8")
    (archive / "a.csv").write_text("archived yesterday", encoding="utf-8")
    return inbox, archive


@pytest.fixture(params=["same drive", "other drive"])
def drive(request, monkeypatch):
    """Moves between two folders work as on one drive, or fail as between two drives: then the
    file is copied and the source deleted."""
    if request.param == "other drive":
        real_replace, real_rename = os.replace, os.rename

        def check(source, target):
            if Path(source).parent != Path(target).parent:
                raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")

        def replace(source, target, *args, **kwargs):
            check(source, target)
            return real_replace(source, target, *args, **kwargs)

        def rename(source, target, *args, **kwargs):
            check(source, target)
            return real_rename(source, target, *args, **kwargs)

        monkeypatch.setattr(os, "replace", replace)
        monkeypatch.setattr(os, "rename", rename)
    return request.param


def contents(folder: Path) -> dict[str, str]:
    """{name: text} of all files in the folder, also temporary ones."""
    return {p.name: p.read_text(encoding="utf-8") for p in sorted(folder.iterdir())}


def move(act, sources, archive, ctx=None, **params):
    table = pl.DataFrame({"File": [str(path) for path in sources]})
    return act("files.move", table, {"column": "File", "target": str(archive), **params}, ctx)


def test_moving_replaces_the_old_file(act, folders, drive):
    inbox, archive = folders
    ctx = StubContext()
    move(act, [inbox / "a.csv", inbox / "b.csv"], archive, ctx, existing="replace")
    assert contents(archive) == {"a.csv": "new a", "b.csv": "new b"} and contents(inbox) == {}
    assert ctx.messages[-1] == ("info", f"Moved 2 files to {archive}")


def test_keep_both_and_stop_never_replace_a_file_that_appears_meanwhile(folders, drive):
    inbox, archive = folders
    with pytest.raises(ActionError, match=f"^{re.escape(str(archive / 'a.csv'))} exists already$"):
        files.move_file(inbox / "a.csv", archive / "a.csv")
    with pytest.raises(ActionError, match="exists already"):
        files.copy_file(inbox / "a.csv", archive / "a.csv")
    assert contents(archive) == {"a.csv": "archived yesterday"} and contents(inbox)["a.csv"] == "new a"


@windows
@pytest.mark.parametrize("share_read", [True, False])
@pytest.mark.parametrize("existing", ["replace", "number"])
def test_a_source_open_in_another_program_stays_where_it_is(act, folders, drive, share_read, existing):
    inbox, archive = folders
    with held(inbox / "a.csv", share_read), pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'a.csv'))}{OPEN}$"):
        move(act, [inbox / "a.csv"], archive, existing=existing)
    assert contents(archive) == {"a.csv": "archived yesterday"}  # no copy, and the old file is kept
    assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


@windows
def test_a_failure_part_way_says_how_many_files_were_moved(act, folders, drive):
    inbox, archive = folders
    message = f"^{re.escape(str(inbox / 'b.csv'))}{OPEN}. 1 file was moved before it$"
    with held(inbox / "b.csv", True), pytest.raises(ActionError, match=message):
        move(act, [inbox / "a.csv", inbox / "b.csv"], archive, existing="replace")
    assert contents(archive) == {"a.csv": "new a"} and contents(inbox) == {"b.csv": "new b"}


@windows
@pytest.mark.parametrize("command", ["files.move", "files.copy"])
def test_a_target_open_in_another_program_is_kept(act, folders, drive, command):
    inbox, archive = folders
    table = pl.DataFrame({"File": [str(inbox / "a.csv")]})
    message = f"^{re.escape(str(archive / 'a.csv'))}{OPEN}$"
    with held(archive / "a.csv", True), pytest.raises(ActionError, match=message):
        act(command, table, {"column": "File", "target": str(archive), "existing": "replace"})
    assert contents(archive) == {"a.csv": "archived yesterday"}
    assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


@windows
def test_a_source_that_cant_be_read(act, folders):
    inbox, archive = folders
    table = pl.DataFrame({"File": [str(inbox / "a.csv")]})
    with held(inbox / "a.csv", False), pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'a.csv'))}{OPEN}$"):
        act("files.copy", table, {"column": "File", "target": str(archive), "existing": "replace"})
    assert contents(archive) == {"a.csv": "archived yesterday"}


@windows
def test_rename_and_delete_name_the_open_file(act, folders, monkeypatch):
    inbox, _ = folders
    table = pl.DataFrame({"File": [str(inbox / "a.csv")], "New": ["c.csv"]})
    with held(inbox / "a.csv", True):
        with pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'a.csv'))}{OPEN}$"):
            act("files.rename", table, {"column": "File", "new_name": "New"})
        with pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'a.csv'))}{OPEN}$"):
            act("files.delete", table, {"column": "File", "how": "permanent"})
    assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


def test_the_recycle_bin_names_the_open_file(act, folders, monkeypatch):
    inbox, _ = folders

    def in_use(path):  # as send2trash reports Windows' sharing violation
        raise OSError(None, "The process cannot access the file because it is being used by another process", str(path), -2147024864)

    def aborted(path):  # as send2trash reports an operation that Windows stopped
        raise OSError(None, None, [str(path)], 0)

    table = pl.DataFrame({"File": [str(inbox / "a.csv")]})
    monkeypatch.setattr(send2trash, "send2trash", in_use)  # not the real bin
    with pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'a.csv'))}{OPEN}$"):
        act("files.delete", table, {"column": "File", "how": "recycle"})
    monkeypatch.setattr(send2trash, "send2trash", aborted)
    with pytest.raises(ActionError, match=f"^Could not move {re.escape(str(inbox / 'a.csv'))} to the Recycle Bin$"):
        act("files.delete", table, {"column": "File", "how": "recycle"})
    assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


def test_a_read_only_file_is_moved_as_on_one_drive(act, folders, drive):
    inbox, archive = folders
    (inbox / "b.csv").chmod(stat.S_IREAD)
    move(act, [inbox / "b.csv"], archive)
    try:
        assert contents(archive) == {"a.csv": "archived yesterday", "b.csv": "new b"} and contents(inbox) == {"a.csv": "new a"}
        assert not os.access(archive / "b.csv", os.W_OK)  # still read-only
    finally:
        (archive / "b.csv").chmod(stat.S_IREAD | stat.S_IWRITE)


@windows
def test_a_read_only_target_is_not_replaced(folders, drive):
    inbox, archive = folders
    (archive / "a.csv").chmod(stat.S_IREAD)
    try:
        for command in (files.move_file, files.copy_file):
            with pytest.raises(ActionError, match=f"^{re.escape(str(archive / 'a.csv'))} is read-only, so it can't be replaced$"):
                command(inbox / "a.csv", archive / "a.csv", replace=True)
        assert contents(archive) == {"a.csv": "archived yesterday"} and contents(inbox)["a.csv"] == "new a"
    finally:
        (archive / "a.csv").chmod(stat.S_IREAD | stat.S_IWRITE)


def test_a_move_to_another_drive_that_fails_at_the_end_puts_everything_back(folders, monkeypatch):
    inbox, archive = folders
    real_replace, real_rename = os.replace, os.rename

    def replace(source, target):
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")
        return real_replace(source, target)

    def rename(source, target):
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")
        if Path(target).name == "a.csv":
            raise OSError(errno.EIO, "The device is not ready")
        return real_rename(source, target)

    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "rename", rename)
    message = f"^Could not move {re.escape(str(inbox / 'a.csv'))} to {re.escape(str(archive / 'a.csv'))}: The device is not ready$"
    with pytest.raises(ActionError, match=message):
        files.move_file(inbox / "a.csv", archive / "a.csv", replace=True)
    assert contents(archive) == {"a.csv": "archived yesterday"}
    assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


def test_read_only_files_stop_before_the_first_change(act, folders):
    inbox, archive = folders
    (archive / "b.csv").write_text("old b", encoding="utf-8")
    (archive / "b.csv").chmod(stat.S_IREAD)
    (inbox / "b.csv").chmod(stat.S_IREAD)
    try:
        both = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "b.csv")], "New": ["c.csv", "d.csv"]})
        for command in ("files.move", "files.copy"):
            with pytest.raises(ActionError, match=f"^{re.escape(str(archive / 'b.csv'))} is read-only, so it can't be replaced$"):
                act(command, both, {"column": "File", "target": str(archive), "existing": "replace"})
        with pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'b.csv'))} is read-only, so it can't be deleted$"):
            act("files.delete", both, {"column": "File", "how": "permanent"})
        (inbox / "d.csv").write_text("old d", encoding="utf-8")
        (inbox / "d.csv").chmod(stat.S_IREAD)
        with pytest.raises(ActionError, match=f"^{re.escape(str(inbox / 'd.csv'))} is read-only, so it can't be replaced$"):
            act("files.rename", both, {"column": "File", "new_name": "New", "existing": "replace"})
        assert contents(archive) == {"a.csv": "archived yesterday", "b.csv": "old b"}
        assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b", "d.csv": "old d"}
    finally:
        for path in (archive / "b.csv", inbox / "b.csv", inbox / "d.csv"):
            path.chmod(stat.S_IREAD | stat.S_IWRITE)


def test_copying_into_a_folder_where_no_new_files_can_be_created(act, folders, monkeypatch):
    inbox, archive = folders
    real_open = open

    def no_new_files(file, mode="r", *args, **kwargs):  # existing files may be changed
        if "x" in mode and Path(file).parent == archive:
            raise PermissionError(13, "Permission denied")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(files, "open", no_new_files, raising=False)
    table = pl.DataFrame({"File": [str(inbox / "a.csv"), str(inbox / "b.csv")]})
    ctx = StubContext()
    with pytest.raises(ActionError, match=f"^No permission to write in {re.escape(str(archive))}. 1 file was copied before it$"):
        act("files.copy", table, {"column": "File", "target": str(archive), "existing": "replace"}, ctx)
    assert contents(archive) == {"a.csv": "new a"}  # b.csv would be a new file
    assert ctx.warnings == [
        f"{archive / 'a.csv'} was written over directly, as no new files can be created in {archive}: "
        "a failure while writing would leave it half-written"
    ]
    assert (archive / "a.csv").stat().st_mtime == (inbox / "a.csv").stat().st_mtime

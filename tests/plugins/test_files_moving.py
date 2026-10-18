"""Moving and copying files changes both files completely or not at all: a file that is open in
another program, read-only or on another drive never leaves a copy behind or loses the file it
was to replace, and the message names the file that is in the way."""

import contextlib
import errno
import os
import re
import shutil
import stat
import subprocess
import sys
import threading
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


def test_moving_into_a_folder_where_no_new_files_can_be_created_names_that_folder(folders, monkeypatch):
    inbox, archive = folders
    real_open, real_replace, real_rename = open, os.replace, os.rename

    def no_new_files(file, mode="r", *args, **kwargs):
        if "x" in mode and Path(file).parent == archive:
            raise PermissionError(13, "Permission denied")
        return real_open(file, mode, *args, **kwargs)

    def denied(real):
        def rename(source, target, *args, **kwargs):  # as Windows refuses a new file there
            if Path(target).parent == archive and not Path(source).name.startswith("~$"):
                raise PermissionError(13, "Access is denied")
            return real(source, target, *args, **kwargs)

        return rename

    monkeypatch.setattr(os, "replace", denied(real_replace))
    monkeypatch.setattr(os, "rename", denied(real_rename))
    with monkeypatch.context() as patch:
        patch.setattr(files, "open", no_new_files, raising=False)
        for name, replace in (("b.csv", False), ("b.csv", True), ("a.csv", True)):
            with pytest.raises(ActionError, match=f"^No permission to write in {re.escape(str(archive))}$"):
                files.move_file(inbox / name, archive / name, replace=replace)
    with pytest.raises(ActionError, match=f"^No permission to move or rename {re.escape(str(inbox / 'b.csv'))}$"):
        files.move_file(inbox / "b.csv", archive / "b.csv")  # new files can be created there: the source is the problem
    assert contents(archive) == {"a.csv": "archived yesterday"} and contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


# --- Ctrl+C or the end of the process at any point of a move ----------------------------------------

CHANGES = [(os, "replace"), (os, "rename"), (os, "remove"), (os, "chmod"), (shutil, "copyfileobj"), (shutil, "copystat")]


def every_file(root: Path) -> dict[str, str]:
    return {p.relative_to(root).as_posix(): p.read_text(encoding="utf-8") for p in sorted(root.rglob("*")) if p.is_file()}


def interrupting(monkeypatch, root: Path, stops: set[int], after: bool, states: list) -> list[int]:
    """Ctrl+C before (or after) the file operations in ``root`` whose numbers are in ``stops``
    (counted from 1). ``states`` gets the files after each operation: what a process that ends
    there leaves. Returns the counter."""
    count = [0]

    def path(arg) -> str:
        return str(arg) if isinstance(arg, str | os.PathLike) else str(getattr(arg, "name", ""))  # or an open file

    def wrap(real):
        def operation(*args, **kwargs):
            if not any(str(root) in path(arg) for arg in args):
                return real(*args, **kwargs)
            count[0] += 1
            stop = count[0] in stops
            if stop and not after:
                raise KeyboardInterrupt
            result = real(*args, **kwargs)
            states.append(every_file(root))
            if stop:
                result = None  # as for Ctrl+C right after open(): the new file object is closed
                raise KeyboardInterrupt
            return result

        return operation

    for module, name in CHANGES:
        monkeypatch.setattr(module, name, wrap(getattr(module, name)))
    monkeypatch.setattr(files, "open", wrap(open), raising=False)
    return count


def make_folders(root: Path, read_only: bool) -> tuple[Path, Path]:
    inbox, archive = root / "inbox", root / "archive"
    inbox.mkdir(parents=True)
    archive.mkdir()
    (inbox / "a.csv").write_text("new a", encoding="utf-8")
    (archive / "a.csv").write_text("archived yesterday", encoding="utf-8")
    if read_only:
        (inbox / "a.csv").chmod(stat.S_IREAD)
    return inbox, archive


def writable(root: Path) -> None:
    for path in root.rglob("*"):
        path.chmod(stat.S_IREAD | stat.S_IWRITE)


@pytest.mark.parametrize("read_only", [False, True])
@pytest.mark.parametrize("replace", [True, False])
def test_ctrl_c_at_any_point_of_a_move_moves_the_file_or_changes_nothing(tmp_path, monkeypatch, drive, replace, read_only):
    """Ctrl+C (once, or again while the move is put back) before or after each file operation:
    then the file is moved or nothing changed, and no temporary file is left."""
    name = "a.csv" if replace else "new.csv"  # replaces the old a.csv, or a new name
    before = {"archive/a.csv": "archived yesterday", "inbox/a.csv": "new a"}
    moved = {f"archive/{name}": "new a"} if replace else {"archive/a.csv": "archived yesterday", "archive/new.csv": "new a"}

    def move(root, stops, after):
        inbox, archive = make_folders(root, read_only)
        with monkeypatch.context() as patch:
            count = interrupting(patch, root, stops, after, [])
            try:
                files.move_file(inbox / "a.csv", archive / name, replace=replace)
                stopped = False
            except KeyboardInterrupt:
                stopped = True
        result = every_file(root)
        assert result in (before, moved), (stops, after, result)
        if result == before and read_only:
            assert not os.access(inbox / "a.csv", os.W_OK)  # still read-only
        writable(root)
        return count[0], stopped

    operations, _ = move(tmp_path / "count", set(), False)
    assert operations >= (1 if drive == "same drive" else 10)
    for number in range(1, operations + 1):
        for after in (False, True):
            for stops in ({number}, {number, number + 1}):
                _, stopped = move(tmp_path / f"{number}-{after}-{len(stops)}", stops, after)
                assert stopped or (after and drive == "other drive")  # an operation that failed stops nothing


@pytest.mark.parametrize("replace", [True, False])
def test_a_process_ended_during_a_move_never_loses_the_file(tmp_path, monkeypatch, drive, replace):
    """A process can end at any point (e.g. in the Task Manager), with nothing put back: the
    file is complete in the source or the target, and an old target in its place or aside, also
    after the next export of the file deleted what the process left."""
    root = tmp_path / "moved"
    inbox, archive = make_folders(root, read_only=False)
    name = "a.csv" if replace else "new.csv"
    states = [every_file(root)]
    with monkeypatch.context() as patch:
        interrupting(patch, root, set(), False, states)
        files.move_file(inbox / "a.csv", archive / name, replace=replace)
    assert len(states) >= 2
    for number, state in enumerate(states):
        left = tmp_path / str(number)  # the files as the process left them there
        for path, text in state.items():
            (left / path).parent.mkdir(parents=True, exist_ok=True)
            (left / path).write_text(text, encoding="utf-8")
        files._remove_left_overs(left / "archive" / name)  # this process isn't writing them: as if it had ended
        after = every_file(left)
        assert "new a" in (after.get("inbox/a.csv"), after.get(f"archive/{name}")), (state, after)
        if replace and after.get("archive/a.csv") != "new a":
            assert "archived yesterday" in after.values(), (state, after)
        assert all(text == "archived yesterday" for path, text in after.items() if "~$" in path), (state, after)


class LeakingLock:
    """A lock whose ``number``-th ``with`` takes it and then gets Ctrl+C before the block that
    releases it begins: CPython looks for Ctrl+C right after ``__enter__``, so the lock stays
    taken."""

    def __init__(self, real, number: int) -> None:
        self.real, self.number, self.taken = real, number, 0

    def __enter__(self):
        self.real.acquire()
        self.taken += 1
        if self.taken == self.number:
            raise KeyboardInterrupt
        return self

    def __exit__(self, *exc):
        self.real.release()


@pytest.mark.parametrize("replace", [True, False])
@pytest.mark.parametrize("drive", ["other drive"], indirect=True)
def test_ctrl_c_right_after_the_lock_of_the_temporary_files_was_taken(tmp_path, monkeypatch, drive, replace):
    """The move still ends, with the file moved or nothing changed: before, it waited for the
    lock forever (the command line's only thread had it)."""
    name = "a.csv" if replace else "new.csv"
    before = {"archive/a.csv": "archived yesterday", "inbox/a.csv": "new a"}
    moved = {f"archive/{name}": "new a"} if replace else {"archive/a.csv": "archived yesterday", "archive/new.csv": "new a"}
    kind = type(files._ACTIVE_LOCK)

    def move(root: Path, number: int) -> tuple[int, str]:
        inbox, archive = make_folders(root, read_only=False)
        lock = LeakingLock(kind(), number)  # a new lock of the same kind
        monkeypatch.setattr(files, "_ACTIVE_LOCK", lock)
        outcome = []

        def work() -> None:
            try:
                files.move_file(inbox / "a.csv", archive / name, replace=replace)
                outcome.append("moved")
            except KeyboardInterrupt:
                outcome.append("stopped")

        thread = threading.Thread(target=work, daemon=True)
        thread.start()
        thread.join(10)
        assert not thread.is_alive(), f"waits for the lock taken by its {number}. with"
        assert every_file(root) in (before, moved), (number, every_file(root))
        return lock.taken, outcome[0]

    taken, outcome = move(tmp_path / "count", 0)
    assert outcome == "moved" and taken >= 2
    for number in range(1, taken + 1):
        assert move(tmp_path / str(number), number)[1] == "stopped", number


def test_when_the_old_target_cant_be_put_back_it_keeps_a_name_of_its_own(folders, monkeypatch):
    inbox, archive = folders
    real_remove, real_replace, real_rename = os.remove, os.replace, os.rename

    def other_drive(source, target):
        if Path(source).parent != Path(target).parent:
            raise OSError(errno.EXDEV, "The system cannot move the file to a different disk drive")

    def remove(path, *args, **kwargs):
        if Path(path) == inbox / "a.csv":
            raise PermissionError(13, "Access is denied")
        return real_remove(path, *args, **kwargs)

    def replace(source, target, *args, **kwargs):
        other_drive(source, target)
        if Path(source).name.startswith("~$") and Path(source).read_text(encoding="utf-8") == "archived yesterday":
            raise OSError(errno.EIO, "The device is not ready")  # putting the old file back
        return real_replace(source, target, *args, **kwargs)

    def rename(source, target, *args, **kwargs):
        other_drive(source, target)
        return real_rename(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "remove", remove)
    monkeypatch.setattr(os, "replace", replace)
    monkeypatch.setattr(os, "rename", rename)
    source = re.escape(str(inbox / "a.csv"))
    with pytest.raises(ActionError, match=rf"^No permission to delete {source}, so it can't be moved. The old a.csv is a \(old\).csv now$"):
        files.move_file(inbox / "a.csv", archive / "a.csv", replace=True)
    assert contents(archive) == {"a (old).csv": "archived yesterday", "a.csv": "new a"}
    assert contents(inbox) == {"a.csv": "new a", "b.csv": "new b"}


# --- left-over temporary files ------------------------------------------------------------------------


def ended_process() -> int:
    """The id of a process that has ended."""
    child = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True, timeout=60)
    return int(child.stdout)


@pytest.mark.parametrize(("command", "drive"), [("files.copy", "same drive"), ("files.move", "other drive")], indirect=["drive"])
def test_the_target_folder_is_searched_once_for_left_over_files(act, tmp_path, monkeypatch, command, drive):
    """Copy files and moves to another drive look for the temporary files that ended processes
    left once per folder, not once for each file: in a folder of many files that took long."""
    inbox, archive = tmp_path / "inbox", tmp_path / "archive"
    inbox.mkdir()
    (archive / "sub").mkdir(parents=True)
    for number in range(20):
        (inbox / f"f{number}.csv").write_text(str(number), encoding="utf-8")
    (archive / "f1.csv").write_text("old", encoding="utf-8")
    left = archive / f"~$f2.{files._COMPUTER}-{ended_process():x}-abcdef.csv"
    left.write_text("left", encoding="utf-8")
    looked = []
    real = os.scandir

    def scandir(path="."):
        looked.append(Path(path))
        return real(path)

    table = pl.DataFrame({"File": [str(inbox / f"f{number}.csv") for number in range(20)]})
    with monkeypatch.context() as patch:
        patch.setattr(os, "scandir", scandir)
        act(command, table, {"column": "File", "target": str(archive), "existing": "replace"})
        if command == "files.copy":  # a step of its own: it looks again
            act(command, table.head(2), {"column": "File", "target": str(archive), "existing": "replace"})
    searched = [path for path in looked if path == archive]
    assert searched == ([archive, archive] if command == "files.copy" else [archive])
    assert not left.exists()  # still deleted
    assert sorted(p.name for p in archive.iterdir()) == sorted(["sub", *(f"f{number}.csv" for number in range(20))])

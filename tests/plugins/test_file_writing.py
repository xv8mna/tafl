"""Exports write a new file next to the old one and replace it only when it is complete."""

import contextlib
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zlib
from pathlib import Path

import polars as pl
import pytest
import xlsxwriter

from tafl.api import ActionError, Cancelled, files
from tafl.api.files import read_error, replace_file
from tafl.api.testing import StubContext

windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows locks open files")


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


def test_the_new_file_replaces_the_old_one_when_complete(tmp_path):
    target = tmp_path / "out" / "orders.csv"
    with replace_file(target) as temporary:
        assert temporary.parent == target.parent and temporary.name.startswith("~$orders.") and temporary.suffix == ".csv"
        assert temporary.exists() and not target.exists()  # the folder is created
        temporary.write_text("new", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "new"
    assert [p.name for p in target.parent.iterdir()] == ["orders.csv"]


def test_after_an_error_the_old_file_stays(tmp_path):
    target = tmp_path / "orders.csv"
    target.write_text("old", encoding="utf-8")
    for error in (ValueError("broken"), Cancelled()):
        with pytest.raises(type(error)), replace_file(target) as temporary:
            temporary.write_text("half", encoding="utf-8")
            raise error
        assert target.read_text(encoding="utf-8") == "old"
        assert [p.name for p in tmp_path.iterdir()] == ["orders.csv"]
    with pytest.raises(ActionError, match="Can't write orders.csv: There is not enough space on the disk"):
        with replace_file(target):
            raise OSError("There is not enough space on the disk. (os error 112): C:\\x\\~$orders.1234.csv")
    assert target.read_text(encoding="utf-8") == "old"


def test_two_exports_at_the_same_time(tmp_path):
    target = tmp_path / "orders.csv"
    with replace_file(target) as first, replace_file(target) as second:
        assert first != second
        pl.DataFrame({"a": range(100_000)}).write_csv(first)
        pl.DataFrame({"a": [1, 2]}).write_csv(second)
    content = target.read_bytes()  # the export that ended last, complete
    assert content.splitlines()[:3] == [b"a", b"0", b"1"] and len(content.splitlines()) == 100_001
    assert b"\x00" not in content
    assert [p.name for p in tmp_path.iterdir()] == ["orders.csv"]


def test_plain_messages_for_files_that_cant_be_written(tmp_path, monkeypatch):
    (tmp_path / "folder.csv").mkdir()
    with pytest.raises(ActionError, match=r"folder.csv is a folder: choose a file name"):
        with replace_file(tmp_path / "folder.csv"):
            pass

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    with monkeypatch.context() as patch:
        patch.setattr(files, "open", denied, raising=False)
        with pytest.raises(ActionError, match=f"^No permission to write in {re.escape(str(tmp_path))}$"):
            with replace_file(tmp_path / "a.csv"):
                pass
    with monkeypatch.context() as patch:
        patch.setattr(Path, "mkdir", denied)
        with pytest.raises(ActionError, match=f"^No permission to create the folder {re.escape(str(tmp_path / 'new'))}$"):
            with replace_file(tmp_path / "new" / "a.csv"):
                pass
    assert sorted(p.name for p in tmp_path.iterdir()) == ["folder.csv"]


@windows
def test_a_read_only_file(act, tmp_path):
    target = tmp_path / "out.csv"
    target.write_text("old", encoding="utf-8")
    target.chmod(stat.S_IREAD)
    try:
        with pytest.raises(ActionError, match="out.csv is read-only, so it can't be replaced"):
            act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(target)})
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert target.read_text(encoding="utf-8") == "old"


@windows
@pytest.mark.parametrize("share_read", [True, False])
def test_files_held_open_by_another_program(act, tmp_path, share_read):
    message = r"is open in another program \(e.g. Excel\): close it and run again"
    csv = tmp_path / "out.csv"
    csv.write_text("old", encoding="utf-8")
    with held(csv, share_read), pytest.raises(ActionError, match=f"^out.csv {message}$"):
        act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(csv)})
    assert csv.read_text(encoding="utf-8") == "old"

    book = tmp_path / "report.xlsx"
    workbook = xlsxwriter.Workbook(book)
    workbook.add_worksheet("Data")
    workbook.close()
    before = book.read_bytes()
    for existing in ("file", "sheet"):
        with held(book, share_read), pytest.raises(ActionError, match=f"^report.xlsx {message}$"):
            act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(book), "sheet": "Data", "existing": existing})
    assert book.read_bytes() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["out.csv", "report.xlsx"]


def test_temporary_files_are_left_out_of_folder_imports(act, tmp_path):
    (tmp_path / "a.csv").write_text("x\n1\n", encoding="utf-8")
    with replace_file(tmp_path / "b.csv") as temporary:
        temporary.write_text("x\n2\n", encoding="utf-8")
        out = act("csv.import", None, {"source": "folder", "folder": str(tmp_path), "pattern": "*.csv"})
    assert out["File"].to_list() == ["a.csv"] and (tmp_path / "b.csv").exists()


# --- temporary files left by a process that was ended -----------------------------------------------

STOPPED = """
import os, sys
from pathlib import Path
from tafl.api.files import replace_file

with replace_file(Path(sys.argv[1])) as temporary:
    temporary.write_text("half", encoding="utf-8")
    print(temporary.name, flush=True)
    os._exit(1)  # as when the process is ended in the Task Manager
"""


def test_the_next_export_deletes_what_a_stopped_export_left(tmp_path):
    target = tmp_path / "orders.csv"
    target.write_text("old", encoding="utf-8")
    pc = files._COMPUTER
    for _ in range(2):  # the second export deletes what the first one left, and leaves its own
        child = subprocess.run([sys.executable, "-c", STOPPED, str(target)], capture_output=True, text=True, timeout=60)
        left = child.stdout.strip()
        found = re.fullmatch(rf"~\$orders\.{re.escape(pc)}-([0-9a-f]+)-[0-9a-f]{{6}}\.csv", left)
        assert found, child.stderr
        assert sorted(p.name for p in tmp_path.iterdir()) == ["orders.csv", left]
    ended = int(found.group(1), 16)  # the process that wrote it
    removed = [
        f"~$ORDERS.{pc.lower()}-{ended:x}-ABCDEF.CSV",
        f"~$orders.{pc}-{os.getpid():x}-abcdef.csv",
        f"~$orders.{pc}-{ended:x}-abcdef.old.csv",  # an old orders.csv a move put aside: a new one is there
    ]
    kept = [
        f"~$orders.{pc}-{os.getppid():x}-abcdef.csv",  # an export of a process that still runs
        f"~$orders.OTHER-PC-{ended:x}-abcdef.csv",  # another computer's, writing into a shared folder
        f"~$orders.{ended:x}-abcdef.csv",  # without the computer: can't be told
        "~$orders.csv",  # Excel's lock file of a workbook orders.csv
        "~$orders.1a2b3c4d.csv",
        f"~$orders.{pc}-{ended:x}-abcdef.xlsx",  # of another file
        f"~$order.{pc}-{ended:x}-abcdef.csv",
    ]
    for name in removed + kept:
        (tmp_path / name).write_text("x", encoding="utf-8")
    with replace_file(target) as temporary:
        with replace_file(target) as second:  # another export of the same file at the same time
            assert temporary.exists()
            second.write_text("second", encoding="utf-8")
        temporary.write_text("new", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "new"
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted(["orders.csv", *kept])


OTHER_COMPUTER = """
import os, sys
from pathlib import Path
from tafl.api.files import replace_file

with replace_file(Path(sys.argv[1])) as temporary:
    temporary.write_text("the other computer's export", encoding="utf-8")
    print(temporary.name, os.getpid(), flush=True)
    sys.stdin.readline()  # until its export is to end
"""


def test_the_temporary_file_of_another_computer_is_left_alone(tmp_path, monkeypatch):
    """Another computer may export into the same shared folder: its process ids say nothing
    here, so its temporary file is never deleted, and its export ends as it should."""
    target = tmp_path / "orders.csv"
    target.write_text("old", encoding="utf-8")
    other = {**os.environ, "COMPUTERNAME": "OTHER-PC", "PYTHONPATH": os.pathsep.join(sys.path)}
    child = subprocess.Popen(
        [sys.executable, "-c", OTHER_COMPUTER, str(target)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, env=other
    )
    try:
        left, process = child.stdout.readline().split()
        running = files._running
        monkeypatch.setattr(files, "_running", lambda number: number != int(process) and running(number))  # not here
        with replace_file(target) as temporary:  # an export of this computer at the same time
            temporary.write_text("ours", encoding="utf-8")
        assert (tmp_path / left).exists() and target.read_text(encoding="utf-8") == "ours"
        assert re.fullmatch(r"~\$orders\.OTHER-PC-[0-9a-f]+-[0-9a-f]{6}\.csv", left)
        child.communicate("\n", timeout=60)
    finally:
        child.kill()
    assert child.returncode == 0 and target.read_text(encoding="utf-8") == "the other computer's export"
    assert [p.name for p in tmp_path.iterdir()] == ["orders.csv"]


@windows
def test_the_computer_in_the_names_of_temporary_files(monkeypatch):
    monkeypatch.setenv("COMPUTERNAME", "Desk-1")
    assert files._computer_name() == "DESK-1"
    for odd in ("laptop.example.org", "Büro", "A" * 16):  # only letters, digits and - (at most 15)
        monkeypatch.setenv("COMPUTERNAME", odd)
        assert re.fullmatch(r"[0-9A-F]{8}", files._computer_name())
    monkeypatch.setenv("COMPUTERNAME", "B" * 16)
    assert files._computer_name() != f"{zlib.crc32(b'A' * 16):08X}"  # another name, another number


def test_in_a_batch_each_folder_is_searched_once(tmp_path, monkeypatch):
    looked = []
    real = os.scandir

    def scandir(path="."):
        looked.append(Path(path))
        return real(path)

    monkeypatch.setattr(os, "scandir", scandir)
    for name in ("a.csv", "b.csv"):
        with replace_file(tmp_path / name) as temporary:
            temporary.write_text(name, encoding="utf-8")
    assert looked == [tmp_path, tmp_path]  # each export looks
    looked.clear()
    with files.batch():
        with files.batch():  # the same batch
            files.copy_file(tmp_path / "a.csv", tmp_path / "c.csv")
        files.copy_file(tmp_path / "b.csv", tmp_path / "d.csv")
        files.copy_file(tmp_path / "b.csv", tmp_path / "sub" / "d.csv")
    assert looked == [tmp_path, tmp_path / "sub"]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["a.csv", "b.csv", "c.csv", "d.csv", "sub"]


@windows
def test_a_hidden_file_stays_hidden(act, tmp_path):
    target = tmp_path / "out.csv"
    target.write_text("old", encoding="utf-8")
    subprocess.run(["attrib", "+h", str(target)], check=True)
    act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(target)})
    assert target.read_text(encoding="utf-8-sig").split() == ["a", "1"]
    assert os.stat(target).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN
    visible = tmp_path / "new.csv"
    act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(visible)})
    assert not os.stat(visible).st_file_attributes & stat.FILE_ATTRIBUTE_HIDDEN


def test_the_temporary_folders_of_a_stopped_process_are_deleted(act, tmp_path, monkeypatch):
    """Sheet exports and formulas work in folders in %TEMP%: ones a process left when it was
    ended are deleted by the next one."""
    temp = tmp_path / "temp"
    temp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(temp))
    ended = subprocess.run([sys.executable, "-c", "import os; print(os.getpid())"], capture_output=True, text=True).stdout
    ended = int(ended)
    left = temp / f"tafl-{ended:x}-k2j4_x9q"
    (left / "sub").mkdir(parents=True)
    (left / "sub" / "sheet.xlsx").write_bytes(b"x")
    kept = [f"tafl-{os.getppid():x}-abcdefgh", f"tafl-{os.getpid():x}-abcdefgh", "tafl-abcdefgh", "tafl-formulas-x"]
    for name in kept:
        (temp / name).mkdir()
    book = tmp_path / "b.xlsx"
    act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(book)})
    act("excel.export", pl.DataFrame({"b": [2]}), {"file": str(book), "sheet": "Other", "existing": "sheet"})
    assert sorted(p.name for p in temp.iterdir()) == sorted(kept)  # also its own folder is gone


# --- long names -------------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["L" * 241 + ".csv", "L" * 251 + ".csv", "\U0001f600" * 125 + ".csv"])
def test_names_of_up_to_255_characters(act, tmp_path, name):
    target = tmp_path / name
    target.write_text("old", encoding="utf-8")
    with replace_file(target) as temporary:
        assert temporary.name.startswith("~$") and len(temporary.name.encode("utf-16-le")) // 2 <= 255
        temporary.write_text("new", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "new"
    act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(target)})
    assert [p.name for p in tmp_path.iterdir()] == [name]


def test_names_over_255_characters(act, tmp_path):
    for name in ("L" * 252 + ".csv", "\U0001f600" * 126 + ".csv"):  # an emoji counts twice in Windows
        with pytest.raises(ActionError, match=r"^A file or folder name can have at most 255 characters: .{50}… has 256$"):
            act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(tmp_path / name)})
    assert list(tmp_path.iterdir()) == []


# --- folders where no new files can be created --------------------------------------------------------


def no_new_files(folder: Path):
    """open() as in a folder where existing files may be changed, but no new ones created."""

    def opener(file, mode="r", *args, **kwargs):
        if "x" in mode and Path(file).parent == folder:
            raise PermissionError(13, "Permission denied")
        return open(file, mode, *args, **kwargs)

    return opener


def test_a_folder_where_no_new_files_can_be_created(act, tmp_path, monkeypatch):
    target, book = tmp_path / "old.csv", tmp_path / "old.xlsx"
    target.write_text("an older, complete export", encoding="utf-8")
    act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(book)})
    monkeypatch.setattr(files, "open", no_new_files(tmp_path), raising=False)
    warnings = []
    with replace_file(target, warnings.append) as temporary:
        assert temporary.parent != tmp_path
        temporary.write_text("new", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "new"
    assert warnings == [
        f"old.csv was written over directly, as no new files can be created in {tmp_path}: "
        "a failure while writing would leave it half-written"
    ]
    with pytest.raises(ValueError), replace_file(target) as temporary:
        temporary.write_text("half", encoding="utf-8")
        raise ValueError("broken")
    assert target.read_text(encoding="utf-8") == "new"  # a failed export leaves it as it was

    ctx = StubContext()
    act("csv.export", pl.DataFrame({"a": [1, 2]}), {"file": str(target)}, ctx)
    assert target.read_text(encoding="utf-8-sig").split() == ["a", "1", "2"]
    act("excel.export", pl.DataFrame({"b": [3]}), {"file": str(book)}, ctx)
    for existing in ("file", "sheet"):
        act("excel.export", pl.DataFrame({"b": [4]}), {"file": str(book), "sheet": "Other", "existing": existing}, ctx)
    assert pl.read_excel(book, sheet_name="Other")["b"].to_list() == [4]
    assert [w.split(" was written over directly")[0] for w in ctx.warnings] == ["old.csv", *["old.xlsx"] * 3]
    with pytest.raises(ActionError, match=f"^No permission to write in {re.escape(str(tmp_path))}$"):
        act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(tmp_path / "new.csv")})
    assert sorted(p.name for p in tmp_path.iterdir()) == ["old.csv", "old.xlsx"]

    def full_disk(*args):
        raise OSError(28, "There is not enough space on the disk")

    monkeypatch.setattr(shutil, "copyfileobj", full_disk)
    with pytest.raises(ActionError, match=r"^Can't write old.csv: There is not enough space on the disk. It may be half-written now$"):
        with replace_file(target) as temporary:
            temporary.write_text("newer", encoding="utf-8")


@windows
def test_a_folder_where_no_new_files_can_be_created_and_the_file_is_open(tmp_path, monkeypatch):
    target = tmp_path / "old.csv"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(files, "open", no_new_files(tmp_path), raising=False)
    with held(target, True), pytest.raises(ActionError, match=r"^old.csv is open in another program \(e.g. Excel\)"):
        with replace_file(target):
            pass
    target.chmod(stat.S_IREAD)
    try:
        with pytest.raises(ActionError, match=r"^old.csv is read-only, so it can't be replaced$"), replace_file(target):
            pass
    finally:
        target.chmod(stat.S_IREAD | stat.S_IWRITE)
    assert target.read_text(encoding="utf-8") == "old"


# --- names and folders that can't be written ------------------------------------------------------------


def test_plain_messages_for_targets_that_cant_be_files(act, tmp_path):
    (tmp_path / "blocker").write_text("x", encoding="utf-8")
    cases = [
        ("blocker/sub/x.csv", f"^{re.escape(str(tmp_path / 'blocker'))} is a file, not a folder$"),
        ("con.csv", r"^CON is reserved for a device in Windows and can't be a file or folder name: con\.csv$"),
        ("out/nul/x.csv", r"^NUL is reserved for a device in Windows and can't be a file or folder name: .*nul$"),
        ("a<b.csv", r"^'<' can't be part of a file or folder name: a<b\.csv$"),
        ("a|b?.csv", r"^'\|' and '\?' can't be part of a file or folder name: a\|b\?\.csv$"),
    ]
    for name, message in cases:
        with pytest.raises(ActionError, match=message):
            act("csv.export", pl.DataFrame({"a": [1]}), {"file": str(tmp_path / name)})
        with pytest.raises(ActionError, match=message.replace(r"\.csv", r"\.xlsx")):
            act("excel.export", pl.DataFrame({"a": [1]}), {"file": str(tmp_path / name.replace(".csv", ".xlsx"))})
    assert [p.name for p in tmp_path.iterdir()] == ["blocker"]


# --- reading ----------------------------------------------------------------------------------------------


def test_plain_messages_for_files_that_cant_be_read(tmp_path):
    path = tmp_path / "l.xlsx"
    path.write_bytes(b"x")
    in_use = "l.xlsx is open in another program (e.g. Excel): close it and run again"
    polars = OSError("The process cannot access the file because it is being used by another process. (os error 32): C:/l.xlsx")
    calamine = Exception(
        "calamine error: Xlsx error: I/O error: The process cannot access the file because it is being used by another "
        "process. (os error 32)\n\nContext:\n0: Could not open workbook at C:\\x\\l.xlsx"
    )
    assert str(read_error(polars, path)) == str(read_error(calamine, path)) == in_use
    assert str(read_error(PermissionError(13, "Permission denied"), path)) == "No permission to read l.xlsx"
    assert str(read_error(ValueError("File is not a zip file"), path)) == "Can't open l.xlsx: File is not a zip file"
    assert str(read_error(OSError("unexpected end of file (os error 38): C:/l.xlsx"), path)) == "Can't open l.xlsx: unexpected end of file"


@windows
def test_a_file_held_open_without_sharing_cant_be_read(tmp_path):
    path = tmp_path / "l.csv"
    path.write_text("a\n1\n", encoding="utf-8")
    with held(path, False):
        with pytest.raises(PermissionError) as error:
            path.read_bytes()
        assert str(read_error(error.value, path)) == "l.csv is open in another program (e.g. Excel): close it and run again"


@windows
def test_imports_of_a_file_held_open_without_sharing(act, tmp_path):
    data = pl.DataFrame({"a": [1]})
    (tmp_path / "l.csv").write_text("a\n1\n", encoding="utf-8")
    act("excel.export", data, {"file": str(tmp_path / "l.xlsx")})
    data.write_parquet(tmp_path / "l.parquet")
    for kind, name in (("csv", "l.csv"), ("excel", "l.xlsx"), ("parquet", "l.parquet")):
        with held(tmp_path / name, False), pytest.raises(ActionError) as error:
            act(f"{kind}.import", None, {"file": str(tmp_path / name)})
        assert str(error.value) == f"{name} is open in another program (e.g. Excel): close it and run again"


def test_copying_a_file_into_place(tmp_path):
    source, target = tmp_path / "a.csv", tmp_path / "out" / "a.csv"
    source.write_text("a", encoding="utf-8")
    ctx = StubContext()
    files.copy_file(source, target, warn=ctx.warn)
    assert target.read_text(encoding="utf-8") == "a" and target.stat().st_mtime == source.stat().st_mtime
    source.write_text("b", encoding="utf-8")
    files.copy_file(source, target, replace=True)
    assert target.read_text(encoding="utf-8") == "b" and sorted(p.name for p in target.parent.iterdir()) == ["a.csv"]
    assert ctx.messages == []

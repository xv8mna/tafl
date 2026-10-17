"""Exports write a new file next to the old one and replace it only when it is complete."""

import contextlib
import re
import stat
import sys
from pathlib import Path

import polars as pl
import pytest
import xlsxwriter

from tafl.api import ActionError, Cancelled, files
from tafl.api.files import replace_file

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

"""A file command that is stopped part-way (Stop in the window, Ctrl+C on the command line) says
how many files it had copied, moved, renamed or deleted before the stop."""

import signal
import threading
from pathlib import Path

import polars as pl
import pytest
import send2trash

from tafl.api import Cancelled, Context, files
from tafl.cli import main
from tafl.core import Engine, Project

COMMANDS = {
    "files.copy": ("copy_file", "Copied 2 files to {archive} before the stop"),
    "files.move": ("move_file", "Moved 2 files to {archive} before the stop"),
    "files.rename": ("move_file", "Renamed 2 files before the stop"),
    "files.delete": ("delete_file", "Deleted 2 files before the stop"),
}


@pytest.fixture
def inbox(tmp_path) -> Path:
    folder = tmp_path / "inbox"
    folder.mkdir()
    for name in "abcd":
        (folder / f"{name}.csv").write_text(name, encoding="utf-8")
    return folder


def table(inbox: Path) -> pl.DataFrame:
    paths = sorted(inbox.iterdir())
    return pl.DataFrame({"File": [str(p) for p in paths], "New": [f"{p.stem}.done" for p in paths]})


def params(command: str, archive: Path) -> dict:
    return {"column": "File", "target": str(archive), "new_name": "New", "how": "permanent"} | (
        {"how": "recycle"} if command == "files.recycle" else {}
    )


def stopping(monkeypatch, name: str, after: int, how: str, cancel: threading.Event | None = None) -> list[int]:
    """Stops the step at the ``after``-th file: Stop is pressed while it is changed (it stops
    before the next one), or Ctrl+C comes right after it was changed, or just before."""
    real = getattr(files, name)
    calls = [0]

    def change(*args, **kwargs):
        calls[0] += 1
        if calls[0] == after and how == "ctrl+c before":
            raise KeyboardInterrupt
        result = real(*args, **kwargs)
        if calls[0] == after:
            if how == "stop":
                cancel.set()
            else:
                raise KeyboardInterrupt
        return result

    monkeypatch.setattr(files, name, change)
    return calls


@pytest.mark.parametrize("how", ["stop", "ctrl+c after", "ctrl+c before"])
@pytest.mark.parametrize("command", [*COMMANDS, "files.recycle"])
def test_a_stopped_command_says_how_many_files_it_changed(act, registry, tmp_path, inbox, monkeypatch, command, how):
    archive = tmp_path / "archive"
    recycled = []
    monkeypatch.setattr(send2trash, "send2trash", lambda path: recycled.append(path) or Path(path).unlink())  # not the real bin
    name, message = COMMANDS.get(command, ("delete_file", "Moved 2 files to the Recycle Bin before the stop"))
    cancel = threading.Event()
    calls = stopping(monkeypatch, name, 3 if how == "ctrl+c before" else 2, how, cancel)
    ctx = Context(mode="run", cancel=cancel)
    action = registry.actions["files.delete" if command == "files.recycle" else command]
    with pytest.raises(Cancelled if how == "stop" else KeyboardInterrupt):
        action(table(inbox), params(command, archive), ctx)
    assert ctx.messages == [("info", message.format(archive=archive))]
    left = sorted(p.name for p in inbox.iterdir())
    assert left == {"files.copy": ["a.csv", "b.csv", "c.csv", "d.csv"], "files.rename": ["a.done", "b.done", "c.csv", "d.csv"]}.get(
        command, ["c.csv", "d.csv"]
    )
    assert calls[0] == (3 if how == "ctrl+c before" else 2)


def test_the_run_results_keep_the_message_of_the_stopped_step(registry, tmp_path, inbox, monkeypatch):
    project = Project(name="Archive")
    project.path = tmp_path / "archive.tafl"
    listed = project.add_table("Inbox")
    project.add_step(listed, registry.actions["files.list"], {"folder": "inbox"})
    project.add_step(listed, registry.actions["files.move"], {"column": "Full path", "target": "archive"})
    project.add_step(listed, registry.actions["rows.sort"], {"by": [{"column": "File"}]})
    cancel = threading.Event()
    stopping(monkeypatch, "move_file", 2, "stop", cancel)
    reported = {}
    with pytest.raises(Cancelled):
        Engine(project, registry).run(cancel=cancel, done=lambda table_id, result: reported.update({table_id: result}))
    stopped = reported[listed.id]
    assert stopped.stopped and len(stopped.steps) == 1
    assert stopped.stopped_messages == [("info", f"Moved 2 files to {tmp_path / 'archive'} before the stop")]


def test_ctrl_c_on_the_command_line(registry, tmp_path, inbox, monkeypatch, capsys):
    project = Project(name="Archive")
    listed = project.add_table("Inbox")
    project.add_step(listed, registry.actions["files.list"], {"folder": "inbox"})
    project.add_step(listed, registry.actions["files.move"], {"column": "Full path", "target": "archive"})
    path = project.save(tmp_path / "archive.tafl")
    real, calls = files.move_file, [0]

    def ctrl_c(*args, **kwargs):  # Ctrl+C right after the second file was moved
        real(*args, **kwargs)
        calls[0] += 1
        if calls[0] == 2:
            signal.raise_signal(signal.SIGINT)

    monkeypatch.setattr(files, "move_file", ctrl_c)
    assert main(["run", str(path)]) == 130
    lines = capsys.readouterr().out.splitlines()
    assert lines[2:4] == [
        "  · 2. Move the files in Full path to archive   (stopped)",
        f"          i Moved 2 files to {tmp_path / 'archive'} before the stop",
    ]
    assert sorted(p.name for p in inbox.iterdir()) == ["c.csv", "d.csv"]

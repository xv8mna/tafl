"""When the program ends without an error message: the memory check, the file of the running step, the
parent of a run."""

import ctypes
import io
import json
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

import pytest

from tafl import crash
from tafl.core import Engine, Project, Status

GB = 1024**3
windows_only = pytest.mark.skipif(sys.platform != "win32", reason="a Windows way of ending a program")

PLUGIN = '''
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

from tafl.api import action


def end_at_once():
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0xC0000409)


@action("demo.no_memory", "No memory")
def no_memory(df, p, ctx):
    """As Rust does it: one line, then the end, with no Python code after it."""
    os.write(2, b"memory allocation of 196656 bytes failed\\n")
    end_at_once()


@action("demo.ends", "Ends")
def ends(df, p, ctx):
    end_at_once()


@action("demo.waits", "Waits")
def waits(df, p, ctx):
    Path(os.environ["DEMO_FILE"]).write_text(str(os.getpid()), encoding="utf-8")
    time.sleep(60)
    return df


@action("demo.starts_a_program", "Starts a program")
def starts_a_program(df, p, ctx):
    command = [sys.executable, "-c", "import time; time.sleep(30)"]
    program = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)  # the test waits for pipes
    Path(os.environ["DEMO_FILE"]).write_text(str(program.pid), encoding="utf-8")
    return df
'''

LEFT_BY_A_WINDOW = """
import ctypes
from ctypes import wintypes

from tafl import crash

crash.start(catch_errors=True)
kernel32 = ctypes.WinDLL("kernel32")
kernel32.GetStdHandle.restype = wintypes.HANDLE
kernel32.GetStdHandle.argtypes = [wintypes.DWORD]
kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, wintypes.LPVOID, wintypes.LPVOID]
kernel32.GetCurrentProcess.restype = wintypes.HANDLE
kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
with crash.running("Sales", "Orders", 2, "Pivot"):
    line = b"memory allocation of 196656 bytes failed\\n"
    kernel32.WriteFile(kernel32.GetStdHandle(0xFFFFFFF4), line, len(line), ctypes.byref(wintypes.DWORD()), None)
    kernel32.TerminateProcess(kernel32.GetCurrentProcess(), 0xC0000409)
"""


@pytest.fixture
def folder(tmp_path, monkeypatch) -> Path:
    """Where the program keeps its files. ``start`` of a test ends with the test."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "local"))
    yield tmp_path / "local" / "Tafl"
    crash.stop()


@pytest.fixture
def plugins(tmp_path) -> Path:
    plugins = tmp_path / "plugins"
    plugins.mkdir()
    (plugins / "demo.py").write_text(PLUGIN, encoding="utf-8")
    return plugins


def _project(folder: Path, *actions: str) -> Path:
    (folder / "orders.csv").write_text("Id\n1\n", encoding="utf-8")
    project = Project(name="Sales")
    table = project.add_table("Orders")
    project.add_step(table, "csv.import", {"file": "orders.csv"})
    for action_id in actions:
        project.add_step(table, action_id, {})
    return project.save(folder / "sales.tafl")


def _tafl(*args: str, **env: str) -> subprocess.CompletedProcess:
    """``tafl`` as a program, so that a run gets its parent."""
    environment = {**os.environ, "PYTHONIOENCODING": "utf-8", **env}
    environment.pop("TAFL_PLUGINS", None)
    command = [sys.executable, "-m", "tafl", *args]
    return subprocess.run(command, capture_output=True, encoding="utf-8", env=environment, timeout=120)


def test_the_free_memory_is_known():
    assert 0 < crash.free_memory() <= crash.installed_memory()


def test_a_step_does_not_start_when_memory_is_short(registry, folder, tmp_path, monkeypatch):
    project = Project.load(_project(tmp_path, "rows.sort"))
    table = project.tables[0]
    table.steps[1].params = {"by": [{"column": "Id"}]}
    engine = Engine(project, registry)
    crash.start()
    monkeypatch.setattr(crash, "free_memory", lambda: 300 * 1024**2)
    first, second = engine.run(mode="preview")[table.id].steps
    assert first.status == Status.ERROR
    assert first.error == (
        "Only 300 MB of memory is free on this computer, so this step did not start (it needs at least "
        "512 MB). Close other programs, then calculate again."
    )
    assert second.status == Status.BLOCKED

    monkeypatch.setattr(crash, "free_memory", lambda: GB)  # the error isn't kept: the next calculation runs
    assert engine.run(mode="preview")[table.id].ok

    monkeypatch.setattr(crash, "free_memory", lambda: 300 * 1024**2)  # a step in the cache needs no memory
    assert engine.run(mode="preview")[table.id].ok


def test_a_step_short_of_memory_first_lets_go_of_the_cache(registry, folder, tmp_path, monkeypatch):
    """Results that can be calculated again must not stop a step."""
    project = Project.load(_project(tmp_path, "rows.sort"))
    table = project.tables[0]
    table.steps[1].params = {"by": [{"column": "Id"}]}
    engine = Engine(project, registry)
    crash.start()
    monkeypatch.setattr(crash, "free_memory", lambda: GB if len(engine.cache) == 0 else 300 * 1024**2)
    assert engine.run(mode="preview")[table.id].ok


def test_linux_counts_the_memory_it_can_hand_out(tmp_path, monkeypatch):
    """Its free memory alone is small on a healthy computer: the files it read stay in memory."""
    info = tmp_path / "meminfo"
    info.write_text("MemTotal:  16000000 kB\nMemFree:  200000 kB\nMemAvailable:  9000000 kB\n", encoding="ascii")
    monkeypatch.setattr(crash, "_MEMINFO", info)
    assert crash._available() == 9000000 * 1024


def test_without_a_free_figure_the_installed_memory_stays_known(tmp_path, monkeypatch):
    monkeypatch.setattr(crash.sys, "platform", "darwin")
    monkeypatch.setattr(crash, "_MEMINFO", tmp_path / "none")
    pages = {"SC_PAGE_SIZE": 4096, "SC_PHYS_PAGES": 1000}
    monkeypatch.setattr(crash.os, "sysconf", pages.__getitem__, raising=False)
    assert crash._memory_status() == (4096000, None)


def test_only_windows_runs_a_project_in_a_child(monkeypatch):
    monkeypatch.setattr(crash.sys, "platform", "linux")
    monkeypatch.setattr(sys, "argv", ["tafl", "run", "a.tafl"])
    monkeypatch.setattr(crash, "_run_in_child", lambda ctrl_c: pytest.fail("a child"))
    monkeypatch.setattr("tafl.cli.main", lambda argv, ctrl_c: 0)
    assert crash.main() == 0


def test_without_start_nothing_is_checked_or_written(registry, folder, tmp_path, monkeypatch):
    project = Project.load(_project(tmp_path))
    monkeypatch.setattr(crash, "free_memory", lambda: 0)
    assert crash.short_of_memory() is None
    assert Engine(project, registry).run(mode="preview")[project.tables[0].id].ok
    assert not folder.exists()


def test_a_computer_that_cant_tell_its_memory_runs_as_ever(folder, monkeypatch):
    monkeypatch.setattr(crash, "free_memory", lambda: None)
    crash.start()
    assert crash.short_of_memory() is None


def test_the_file_says_which_steps_run(folder, monkeypatch):
    monkeypatch.setattr(crash, "free_memory", lambda: GB)
    crash.start()
    path = folder / f"running-{os.getpid()}.json"
    with crash.running("Sales", "Orders", 2, "Pivot"):
        with crash.running("Sales", "Totals", 1, "Import CSV"):  # a run beside a calculation
            steps = json.loads(path.read_text(encoding="utf-8"))
        assert [step["table"] for step in json.loads(path.read_text(encoding="utf-8"))] == ["Orders"]
    assert steps == [
        {"project": "Sales", "table": "Orders", "number": 2, "action": "Pivot", "free": GB},
        {"project": "Sales", "table": "Totals", "number": 1, "action": "Import CSV", "free": GB},
    ]
    assert json.loads(path.read_text(encoding="utf-8")) == []


def test_the_engine_writes_down_the_step_that_runs(registry, folder, tmp_path, monkeypatch):
    project = Project.load(_project(tmp_path))
    seen, running = [], crash.running

    def spy(*step):
        seen.append(step)
        return running(*step)

    monkeypatch.setattr(crash, "running", spy)
    assert Engine(project, registry).run(mode="preview")[project.tables[0].id].ok
    assert seen == [("Sales", "Orders", 1, registry.actions["csv.import"].name)]


def test_a_clean_end_leaves_nothing(folder):
    crash.start(catch_errors=True)
    with crash.running("Sales", "Orders", 2, "Pivot"):
        pass
    assert len(list(folder.iterdir())) == (2 if sys.platform == "win32" else 1)
    crash.stop()
    assert list(folder.iterdir()) == []
    assert crash.last_words() == []


@windows_only
def test_the_file_of_a_program_that_runs_stays(folder):
    crash.start()
    with crash.running("Sales", "Orders", 2, "Pivot"):
        assert crash.last_words() == []
        assert (folder / f"running-{os.getpid()}.json").exists()


def test_an_end_between_two_steps_tells_nothing(folder):
    folder.mkdir(parents=True)
    (folder / "running-1.json").write_text("[]", encoding="utf-8")
    (folder / "running-2.json").write_text("[{", encoding="utf-8")  # cut off
    assert crash.last_words() == []
    assert list(folder.iterdir()) == []


@windows_only
def test_a_window_that_ended_without_a_word_tells_it_once(folder):
    ended = subprocess.run([sys.executable, "-c", LEFT_BY_A_WINDOW], capture_output=True, timeout=60)
    assert ended.returncode == 0xC0000409, ended.stderr
    assert ended.stderr == b""  # the line went to the file, as in a program without a console
    stopped = crash.last_words()
    assert len(stopped) == 1
    text = stopped[0].says("Tafl stopped")
    assert text.startswith("Tafl stopped in table “Orders”, step 2 (Pivot) of project Sales: the computer ran out of memory.")
    assert text.endswith(" GB was free when the step started.")
    assert crash.last_words() == []
    assert list(folder.iterdir()) == []


def test_without_the_line_of_rust_the_cause_is_a_guess():
    stopped = crash.Stopped([{"project": "Sales", "table": "Orders", "number": 2, "action": "Pivot", "free": None}])
    assert stopped.says("Tafl stopped") == (
        "Tafl stopped in table “Orders”, step 2 (Pivot) of project Sales, without a message. "
        "The usual cause is too little memory."
    )
    assert crash.Stopped([]).says("The run stopped") == (
        "The run stopped, without a message. The usual cause is too little memory."
    )


@windows_only
def test_a_run_that_ends_without_a_word(folder, plugins, tmp_path):
    path = _project(tmp_path, "demo.no_memory")
    ended = _tafl("--plugins", str(plugins), "run", str(path))
    assert ended.returncode == crash.CRASHED
    assert ended.stderr.startswith("memory allocation of 196656 bytes failed\n")
    message = ended.stderr.splitlines()[-1]
    assert message.startswith("Error: The run stopped in table “Orders”, step 2 (No memory) of project Sales: ")
    assert ": the computer ran out of memory. " in message
    assert message.endswith(" The steps after it did not run (exit code 0xc0000409).")
    log = (folder / "tafl.log").read_text(encoding="utf-8")
    assert log.count("\n") == 1 and message.removeprefix("Error: ") in log and " ERROR tafl: " in log
    assert [p.name for p in folder.iterdir()] == ["tafl.log"]  # nothing left for the next start


@windows_only
def test_a_run_that_ends_without_a_line(folder, plugins, tmp_path):
    ended = _tafl("--plugins", str(plugins), "run", str(_project(tmp_path, "demo.ends")))
    assert ended.returncode == crash.CRASHED
    assert ended.stderr == (
        "Error: The run stopped in table “Orders”, step 2 (Ends) of project Sales, without a message. The usual cause "
        "is too little memory. The steps after it did not run (exit code 0xc0000409).\n"
    )


def test_the_other_ends_of_a_run_stay_as_they_were(folder, plugins, tmp_path):
    missing = tmp_path / "Müller “x”.tafl"
    ended = _tafl("run", str(missing))
    assert (ended.returncode, ended.stderr) == (2, f"Error: There is no project file {missing}\n")

    path = _project(tmp_path)
    done = _tafl("run", str(path))
    assert done.returncode == 0 and done.stderr == "" and "✓ 1. Import orders.csv" in done.stdout

    project = Project.load(path)
    project.add_step(project.tables[0], "rows.sort", {})
    project.save(path)
    failed = _tafl("run", str(path))
    assert failed.returncode == 1 and "Failed in" in failed.stdout and failed.stderr == ""
    assert not folder.exists() or list(folder.iterdir()) == []


@pytest.mark.parametrize(
    ("args", "is_run"),
    [
        (["run", "a.tafl"], True),
        (["--plugins", "run", "show", "a.tafl"], False),
        (["--plugins", "folder", "run", "a.tafl"], True),
        (["show", "a.tafl", "--table", "run"], False),
        (["plugins"], False),
        (["--version"], False),
        (["--plugins"], False),
        ([], False),
    ],
)
def test_only_a_run_gets_a_parent(args, is_run):
    assert crash._is_run(args) is is_run


@pytest.mark.parametrize("frozen", [True, False])
def test_the_child_is_this_program_again(frozen, folder, monkeypatch):
    """A built program is its own command; Python needs its options and the script again."""
    started = []

    class Child:
        pid, stderr = 1, io.BytesIO(b"")

        def wait(self) -> int:
            return 0

    def popen(command, env, **handles):
        started.append((command, env[crash._CHILD]))
        return Child()

    monkeypatch.setattr(crash.subprocess, "Popen", popen)
    monkeypatch.setattr(crash, "_end_with_this_program", lambda process: None)
    monkeypatch.setattr(crash.signal, "signal", lambda *arguments: None)
    monkeypatch.setattr(sys, "frozen", frozen, raising=False)
    monkeypatch.setattr(sys, "argv", ["tafl", "run", "a.tafl"])
    monkeypatch.setattr(sys, "orig_argv", ["python", "-X", "utf8", "-m", "tafl", "run", "a.tafl"])
    assert crash._run_in_child() == 0
    arguments = ["run", "a.tafl"] if frozen else ["-X", "utf8", "-m", "tafl", "run", "a.tafl"]
    assert started == [([sys.executable, *arguments], f"run-{os.getpid()}")]


@pytest.mark.parametrize(
    ("before", "during", "code", "result", "killed"),
    [
        (True, False, 1, 130, True),  # while the program loaded: the child would not hear it
        (False, True, 0xC000013A, 130, False),  # while the child started
        (False, True, 0xFFFFFFFF, 130, False),
        (False, True, 130, 130, False),
        (False, True, 1, 1, False),  # it ended by itself first
        (False, False, 0xC000013A, crash.CRASHED, False),
    ],
)
def test_a_ctrl_c_stops_the_run_at_every_moment(before, during, code, result, killed, folder, monkeypatch):
    heard, ended = threading.Event(), []
    if before:
        heard.set()

    class Child:
        pid, stderr = 1, io.BytesIO(b"")

        def kill(self) -> None:
            ended.append(True)

        def wait(self) -> int:
            if during:
                heard.set()
            return code

    monkeypatch.setattr(crash.subprocess, "Popen", lambda command, env, **handles: Child())
    monkeypatch.setattr(crash, "_end_with_this_program", lambda process: None)
    monkeypatch.setattr(crash.signal, "signal", lambda *arguments: None)
    monkeypatch.setattr(sys, "argv", ["tafl", "run", "a.tafl"])
    assert crash._run_in_child(heard) == result
    assert bool(ended) is killed


@pytest.mark.parametrize("stream", [None, io.StringIO()])
def test_a_program_without_handles_gives_the_child_empty_ones(stream, folder, monkeypatch):
    """Windows gives a program that was started without handles no output: the child would get a pipe
    that nobody reads, and end at the first line it prints."""
    given = []

    class Child:
        pid, stderr = 1, io.BytesIO(b"")

        def wait(self) -> int:
            return 0

    def popen(command, env, stdin, stdout, stderr):
        given.append((stdin, stdout))
        return Child()

    monkeypatch.setattr(crash.subprocess, "Popen", popen)
    monkeypatch.setattr(crash, "_end_with_this_program", lambda process: None)
    monkeypatch.setattr(crash.signal, "signal", lambda *arguments: None)
    monkeypatch.setattr(sys, "argv", ["tafl", "run", "a.tafl"])
    monkeypatch.setattr(sys, "stdin", stream)
    monkeypatch.setattr(sys, "stdout", stream)
    assert crash._run_in_child() == 0
    empty = subprocess.DEVNULL if stream is None else None
    assert given == [(empty, empty)]


@pytest.mark.parametrize(("code", "result"), [(0, 0), (120, 120), (7, 7), (0xC0000409, crash.CRASHED), (0xFFFFFFFF, crash.CRASHED)])
def test_only_the_end_of_a_program_from_outside_counts_as_a_stop(code, result, folder, monkeypatch):
    class Child:
        pid, stderr = 1, io.BytesIO(b"")

        def wait(self) -> int:
            return code

    told = []
    monkeypatch.setattr(crash.subprocess, "Popen", lambda command, env, **handles: Child())
    monkeypatch.setattr(crash, "_end_with_this_program", lambda process: None)
    monkeypatch.setattr(crash, "_tell", told.append)
    monkeypatch.setattr(crash.signal, "signal", lambda *arguments: None)
    monkeypatch.setattr(sys, "argv", ["tafl", "run", "a.tafl"])
    assert crash._run_in_child() == result
    assert bool(told) is (result == crash.CRASHED)


def test_a_run_that_was_ended_from_outside_tells_the_window_nothing(folder):
    folder.mkdir(parents=True)
    file = folder / "running-run-7.json"
    file.write_text(json.dumps([{"project": "p", "table": "t", "number": 2, "action": "a"}]), encoding="utf-8")
    assert crash.last_words() == [] and not file.exists()


def _ended_soon(process: int, seconds: float) -> bool:
    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(0x100000, False, process)  # SYNCHRONIZE
    if not handle:  # gone
        return True
    try:
        return kernel32.WaitForSingleObject(handle, int(seconds * 1000)) == 0
    finally:
        kernel32.CloseHandle(handle)


@windows_only
def test_a_run_ends_with_its_parent(folder, plugins, tmp_path):
    """Stopping a task in the Task Scheduler ends the parent: the work must not go on alone."""
    marker = tmp_path / "pid.txt"
    path = _project(tmp_path, "demo.waits")
    command = [sys.executable, "-m", "tafl", "--plugins", str(plugins), "run", str(path)]
    environment = {**os.environ, "DEMO_FILE": str(marker)}
    environment.pop("TAFL_PLUGINS", None)
    parent = subprocess.Popen(command, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(600):
            if marker.exists() and marker.read_text(encoding="utf-8"):
                break
            time.sleep(0.05)
        child = int(marker.read_text(encoding="utf-8"))
        assert child != parent.pid
        assert not _ended_soon(child, 0.1)
        parent.kill()
        parent.wait(10)
        assert _ended_soon(child, 10)
    finally:
        parent.kill()


@windows_only
def test_a_program_that_a_step_starts_goes_on_after_the_run(folder, plugins, tmp_path):
    marker = tmp_path / "pid.txt"
    path = _project(tmp_path, "demo.starts_a_program")
    done = _tafl("--plugins", str(plugins), "run", str(path), DEMO_FILE=str(marker))
    assert done.returncode == 0, done.stderr
    program = int(marker.read_text(encoding="utf-8"))
    try:
        assert not _ended_soon(program, 0.5)
    finally:
        subprocess.run(["taskkill", "/F", "/PID", str(program)], capture_output=True)

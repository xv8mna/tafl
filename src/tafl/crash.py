"""When the program ends without an error message.

When a library written in Rust (Polars) gets no memory, it prints one line and ends the whole
program at once: no ``except``, ``finally`` or ``atexit`` runs, so the program can't report that
itself. Three things make up for it:

- ``short_of_memory``: a step doesn't start when too little memory is left, and fails with a
  normal error. It can't see a step that suddenly needs much more than is free.
- ``running``: a small file says which step runs, and a clean end removes it. The file of a program
  that ended without an error message tells the next start what happened (``last_words``).
- ``main``: on Windows, ``tafl run`` works in a child process. A small parent waits for it and
  says how it ended, because nobody is there to start a scheduled run again.

This module needs only the standard library: the parent must stay small, as memory may be short,
and it must start fast.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from tafl import APP_FOLDER, COMMAND

MIN_FREE = 512 * 1024**2  # a step doesn't start with less memory left
CRASHED = 3  # exit code of a run that ended without an error message
_STOPPED = 130  # exit code of a run that Ctrl+C stopped
_STATUS = 0xC0000000  # from here on, exit codes are the status of a Windows program that was ended
_CHILD = "TAFL_RUN_CHILD"  # set for the child process of a run
_NO_MEMORY = "memory allocation of"  # the start of the line Rust prints before it ends the program
_STD_ERROR = 0xFFFFFFF4  # STD_ERROR_HANDLE
_MEMINFO = Path("/proc/meminfo")

_lock = threading.Lock()
_watching = False  # between start and stop
_marker: IO[str] | None = None  # the file of this program; open, so that it can't be removed while it runs
_errors: IO[bytes] | None = None  # see _catch_errors
_steps: dict[object, dict[str, Any]] = {}  # the steps that run now (a run beside a calculation: two)


def _memory_status() -> tuple[int | None, int | None]:
    """The bytes of memory the computer has, and the bytes a program can still get: the free
    memory or what Windows can still promise with its page file, whichever is less. A program
    ends when the second runs out, and the computer gets very slow when the first does. None for
    what can't be told."""
    if sys.platform == "win32":
        import ctypes

        class MemoryStatus(ctypes.Structure):  # MEMORYSTATUSEX
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong)
                for name in ("total", "free", "page_total", "page_free", "virtual_total", "virtual_free", "extended")
            ]

        status = MemoryStatus()
        status.length = ctypes.sizeof(MemoryStatus)
        try:
            known = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
        except OSError:
            return None, None
        return (int(status.total), int(min(status.free, status.page_free))) if known else (None, None)
    installed = None
    with contextlib.suppress(AttributeError, OSError, ValueError):
        installed = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    return installed, _available()


def _available() -> int | None:
    """What Linux can hand out, the files it keeps in memory included: its free memory alone is
    small on a healthy computer. None elsewhere."""
    with contextlib.suppress(OSError, ValueError, IndexError):
        for line in _MEMINFO.read_text(encoding="ascii").splitlines():
            if line.startswith("MemAvailable:"):
                return int(line.split()[1]) * 1024
    return None


def installed_memory() -> int | None:
    """The bytes of memory the computer has, or None if it can't be told."""
    return _memory_status()[0]


def free_memory() -> int | None:
    """The bytes of memory a program can still get, or None if it can't be told."""
    return _memory_status()[1]


def _gb(size: int) -> str:
    return f"{size / 1024**3:.1f} GB"


def _folder() -> Path:
    return Path(os.environ.get("LOCALAPPDATA") or Path.home()) / APP_FOLDER


def start(*, name: str | None = None, catch_errors: bool = False) -> None:
    """From now on the steps of this program check the memory and write down that they run. The
    window and a run call this, so that a test or another use of the engine leaves no files.
    ``name``: of its file (default: the process id). ``catch_errors``: this program has no console
    (see ``_catch_errors``)."""
    global _watching, _marker
    with _lock:
        if _watching:
            return
        _watching = True
        path = _folder() / f"running-{name or os.getpid()}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            _marker = path.open("w", encoding="utf-8")
            if catch_errors:
                _catch_errors(path.with_suffix(".txt"))
        except OSError:  # a report less, no reason to stop
            pass


def _catch_errors(path: Path) -> None:
    """Sends what a library prints to the file ``path``: a window has no console, so the line
    that tells why the program ended would be lost."""
    global _errors
    if sys.platform == "win32":
        import ctypes
        import msvcrt

        _errors = path.open("ab", buffering=0)
        handle = ctypes.c_void_p(msvcrt.get_osfhandle(_errors.fileno()))
        ctypes.windll.kernel32.SetStdHandle(ctypes.c_ulong(_STD_ERROR), handle)


def stop() -> None:
    """The clean end: there is nothing to tell the next start."""
    global _watching, _marker, _errors
    with _lock:
        _watching = False
        _steps.clear()
        if _errors is not None and sys.platform == "win32":
            import ctypes

            ctypes.windll.kernel32.SetStdHandle(ctypes.c_ulong(_STD_ERROR), ctypes.c_void_p(0))
        for file in (_marker, _errors):
            if file is not None:
                with contextlib.suppress(OSError):
                    file.close()
                    Path(file.name).unlink(missing_ok=True)
        _marker = _errors = None


def short_of_memory() -> str | None:
    """Why a step must not start, or None."""
    free = free_memory() if _watching else None
    if free is None or free >= MIN_FREE:
        return None
    return (
        f"Only {free // 1024**2} MB of memory is free on this computer, so this step did not start (it needs at "
        f"least {MIN_FREE // 1024**2} MB). Close other programs, then calculate again."
    )


@contextlib.contextmanager
def running(project: str, table: str, number: int, action: str) -> Iterator[None]:
    """Writes down that this step runs, for as long as the block runs."""
    if _marker is None:
        yield
        return
    key = object()
    _steps[key] = {"project": project, "table": table, "number": number, "action": action, "free": free_memory()}
    _save()
    try:
        yield
    finally:
        _steps.pop(key, None)
        _save()


def _save() -> None:
    with _lock, contextlib.suppress(OSError, ValueError):  # ValueError: closed by stop
        if _marker is not None:
            _marker.seek(0)
            _marker.write(json.dumps(list(_steps.values())))
            _marker.truncate()
            _marker.flush()


@dataclass
class Stopped:
    """What a program that ended without an error message left: the steps that ran at the end, and the last
    things it printed."""

    steps: list[dict[str, Any]]
    errors: str = ""

    def says(self, what: str) -> str:
        """``what`` ("The run stopped"), where it happened, and why."""
        places = " and ".join(
            f"table “{step['table']}”, step {step['number']} ({step['action']}) of project {step['project']}"
            for step in self.steps
        )
        text = f"{what} in {places}" if places else what
        if _NO_MEMORY not in self.errors:
            return f"{text}, without a message. The usual cause is too little memory."
        text += ": the computer ran out of memory."
        free = [step["free"] for step in self.steps if step.get("free") is not None]
        return f"{text} {_gb(min(free))} was free when the step started." if free else text


def last_words(name: str | None = None) -> list[Stopped]:
    """What programs that ended without an error message left (with ``name``, only that one), once: the
    files are removed then. Windows doesn't remove the file of a program that still runs. A
    program that ended between two steps and printed nothing has nothing to tell, and neither has
    a run whose parent is gone: it was ended from outside, as the parent tells all the others."""
    found = []
    for path in sorted(_folder().glob(f"running-{name or '*'}.json")):
        try:
            text = path.read_text(encoding="utf-8")
            path.unlink()
        except OSError:
            continue
        errors = ""
        with contextlib.suppress(OSError):
            errors = path.with_suffix(".txt").read_text(encoding="utf-8", errors="replace")[-2000:]
            path.with_suffix(".txt").unlink()
        try:
            steps = json.loads(text)
        except ValueError:
            steps = []
        if (steps or errors.strip()) and (name is not None or not path.stem.startswith("running-run-")):
            found.append(Stopped(steps, errors))
    return found


def main(argv: list[str] | None = None, *, ctrl_c: threading.Event | None = None) -> int:
    """The program ``tafl``: ``cli.main``, and on Windows ``tafl run`` in a child process, which
    starts this program again. Elsewhere a run is one process: a signal for the parent would not
    reach a child, and no job ends the child with its parent."""
    name = os.environ.pop(_CHILD, None)
    if argv is None and name is None and sys.platform == "win32" and _is_run(sys.argv[1:]):
        return _run_in_child(ctrl_c)
    from tafl.cli import main as command_line

    if name is None:
        return command_line(argv, ctrl_c=ctrl_c)
    if sys.stderr is not None:
        sys.stderr.reconfigure(encoding="utf-8", errors="backslashreplace")  # the parent reads this
    start(name=name)
    try:
        return command_line(argv, ctrl_c=ctrl_c)
    finally:
        stop()


def _is_run(args: list[str]) -> bool:
    """Whether the command is ``run``. A wrong answer only means a run without a parent."""
    rest = iter(args)
    for arg in rest:
        if arg == "--plugins":
            next(rest, None)
        elif not arg.startswith("-"):
            return arg == "run"
    return False


def _run_in_child(ctrl_c: threading.Event | None = None) -> int:
    """Starts this program again for the work, and tells how it ended if it ended without an error message.
    The child's output goes where this program's goes; its errors come through here, to be read."""
    heard = ctrl_c or threading.Event()  # also a Ctrl+C from before the child could hear it
    signal.signal(signal.SIGINT, lambda signum, frame: heard.set())  # it reaches the child too, which ends the run
    arguments = sys.argv[1:] if getattr(sys, "frozen", False) else sys.orig_argv[1:]
    name = f"run-{os.getpid()}"  # not the child's id: Python in a virtual environment is a second process
    # A program started without handles has neither: the child would get a dead pipe, or no start at all
    given, output = (subprocess.DEVNULL if stream is None else None for stream in (sys.stdin, sys.stdout))
    child = subprocess.Popen(
        [sys.executable, *arguments], env={**os.environ, _CHILD: name}, stdin=given, stdout=output, stderr=subprocess.PIPE
    )
    _end_with_this_program(child.pid)
    too_early = heard.is_set()
    if too_early:
        child.kill()
    printed: deque[str] = deque(maxlen=20)
    # In a thread: a program that a step started may keep the errors open after the child has ended
    reader = threading.Thread(target=_pass_on, args=(child.stderr, printed), daemon=True)
    reader.start()
    code = child.wait()
    reader.join(2)
    left = last_words(name)  # also after an end by itself: nothing for the next start to tell
    by_itself = 0 <= code < _STATUS
    if too_early or (heard.is_set() and not by_itself):  # the child had no time to say so
        with contextlib.suppress(AttributeError, OSError, ValueError):
            print("Error: Stopped", file=sys.stderr, flush=True)
        return _STOPPED
    if by_itself:
        return code
    stopped = left[0] if left else Stopped([])
    stopped.errors = "".join(printed)
    text = stopped.says("The run stopped")
    _tell(f"{text} The steps after it did not run (exit code {code & 0xFFFFFFFF:#x}).")
    return CRASHED


def _pass_on(errors: IO[bytes], printed: deque[str]) -> None:
    """Passes the errors of the child on as they come, and keeps the last ones."""
    for line in errors:
        text = line.decode("utf-8", "replace").replace("\r\n", "\n")
        printed.append(text)
        with contextlib.suppress(AttributeError, OSError, ValueError):  # no stderr, or it is closed
            sys.stderr.write(text)
            sys.stderr.flush()


def _tell(text: str) -> None:
    """On the screen, and in the log file, which is all a scheduled run has."""
    with contextlib.suppress(AttributeError, OSError, ValueError):
        print(f"Error: {text}", file=sys.stderr, flush=True)
    with contextlib.suppress(OSError):
        _folder().mkdir(parents=True, exist_ok=True)
        with (_folder() / f"{COMMAND}.log").open("a", encoding="utf-8") as log:
            log.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} ERROR tafl: {text}\n")


def _end_with_this_program(process: int) -> None:
    """Puts the child in a Windows job that ends with this program: stopping a run (Task Scheduler,
    Task Manager) ends the parent, and the child must not go on alone. Programs that the child
    starts stay out of the job, and go on after the run as they always did."""
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32")
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    limits = (ctypes.c_byte * 144)()  # JOBOBJECT_EXTENDED_LIMIT_INFORMATION, LimitFlags at byte 16
    silent_breakaway, kill_on_close = 0x1000, 0x2000
    ctypes.c_uint32.from_buffer(limits, 16).value = silent_breakaway | kill_on_close
    job = kernel32.CreateJobObjectW(None, None)  # never closed: Windows closes it when this program ends
    set_quota_and_terminate = 0x0101
    handle = kernel32.OpenProcess(set_quota_and_terminate, False, process)
    if job and handle and kernel32.SetInformationJobObject(job, 9, limits, ctypes.sizeof(limits)):  # 9: extended limits
        kernel32.AssignProcessToJobObject(job, handle)
    if handle:
        kernel32.CloseHandle(handle)

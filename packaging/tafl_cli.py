"""tafl.exe of the Windows download: the command line."""

import signal
import threading

# Ctrl+C while the program loads its modules is kept for main(), which then stops at once: a
# KeyboardInterrupt in the middle of loading a library can crash the program.
ctrl_c = threading.Event()
signal.signal(signal.SIGINT, lambda signum, frame: ctrl_c.set())

import multiprocessing  # noqa: E402
import multiprocessing.spawn  # noqa: E402
import os  # noqa: E402
import sys  # noqa: E402

if multiprocessing.spawn.is_forking(sys.argv):  # a plugin's worker process: Python's own handling
    signal.signal(signal.SIGINT, signal.default_int_handler)

from tafl.cli import main  # noqa: E402


def use_python_io_variables() -> None:
    """Apply PYTHONIOENCODING to the output as Python does (a built program ignores it), e.g. for a
    log file in UTF-8, and PYTHONUTF8=1 too, but only to the output: the built program doesn't
    switch Python to its UTF-8 mode. Python writes to the console in UTF-8 in any case."""
    encoding, _, errors = os.environ.get("PYTHONIOENCODING", "").partition(":")
    if not encoding and os.environ.get("PYTHONUTF8") == "1":
        encoding = "utf-8"
    for stream, stream_errors in ((sys.stdout, errors), (sys.stderr, "backslashreplace")):
        raw = getattr(getattr(stream, "buffer", None), "raw", None)
        if stream is None or type(raw).__name__ == "_WindowsConsoleIO":
            continue
        if encoding or (stream is sys.stdout and errors):
            stream.reconfigure(encoding=encoding or None, errors=stream_errors or None)


multiprocessing.freeze_support()  # a plugin's worker processes start this program again
use_python_io_variables()
raise SystemExit(main(ctrl_c=ctrl_c))

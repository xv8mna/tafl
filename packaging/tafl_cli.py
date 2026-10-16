"""tafl.exe of the Windows download: the command line."""

import multiprocessing
import os
import sys

from etl_tool.cli import main


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
raise SystemExit(main())

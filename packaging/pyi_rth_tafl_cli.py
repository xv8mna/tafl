"""Run-time hook of tafl.exe (see tafl.spec): the first code of the program after PyInstaller's own
start, before PyInstaller's other run-time hooks load modules. From here on a Ctrl+C sets
``tafl_ctrl_c``, and tafl_cli.py, which runs in the same module (__main__) after the hooks, stops
at once: a KeyboardInterrupt while Python loads a module prints a long traceback, and can crash
the program."""

import signal
import threading

tafl_ctrl_c = threading.Event()
signal.signal(signal.SIGINT, lambda signum, frame: tafl_ctrl_c.set())

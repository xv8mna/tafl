import signal
import threading

# A Ctrl+C from the start is kept for main(), which then stops at once: a KeyboardInterrupt while
# Python loads the modules prints a long traceback. (crash.main does the same, but only once its
# own module is loaded.)
ctrl_c = threading.Event()
signal.signal(signal.SIGINT, lambda signum, frame: ctrl_c.set())

from tafl.crash import main  # noqa: E402

raise SystemExit(main(ctrl_c=ctrl_c))

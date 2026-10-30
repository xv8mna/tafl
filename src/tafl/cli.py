"""Command line: run projects, look at tables, list plugins.

    tafl run PROJECT [--table NAME ...] [--preview] [--set NAME=VALUE ...]
    tafl show PROJECT --table NAME [--step N] [--rows 20] [--set NAME=VALUE ...]
    tafl plugins

Exit codes: 0 done, 1 a table has an error (plugins: a plugin failed to load), 2 the project or
the command line can't be used, 3 a run ended without a word (see crash.py), 130 stopped with Ctrl+C.
"""

from __future__ import annotations

import argparse
import contextlib
import signal
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator
from pathlib import Path

import polars as pl

from tafl import APP_NAME, COMMAND, __version__
from tafl.api import Cancelled, Mode
from tafl.api.parameters import Failed, as_text
from tafl.core import (
    EXTENSION,
    Engine,
    Project,
    ProjectError,
    Registry,
    Status,
    StepResult,
    Table,
    TableResult,
    parameters,
)
from tafl.core.cancel import CancelEvent, stopping_queries
from tafl.crash import CRASHED

STOPPED = 130  # Ctrl+C
_SUMMARY = 300  # characters of a step's summary that are printed at most, as in the window


def _codes(*codes: tuple[int, str]) -> str:
    """The exit codes, for the end of a help text."""
    return "exit codes:\n" + "\n".join(f"  {code:<5}{meaning}" for code, meaning in codes)


_USAGE = ((2, "the project or the command line can't be used"), (STOPPED, "stopped with Ctrl+C"))
_RUN_CODES = _codes(
    (0, "done (tables that are turned off are skipped)"),
    (1, "a table has an error"),
    _USAGE[0],
    (CRASHED, "the run stopped before its end, usually because the computer ran out of memory"),
    _USAGE[1],
)
_SHOW_CODES = _codes((0, "done (also for a table that is turned off)"), (1, "the table has an error"), *_USAGE)
_PLUGIN_CODES = _codes((0, "all plugins loaded"), (1, "a plugin failed to load"))
_CODES = (
    _codes((0, "done"), (1, "a table has an error (plugins: a plugin failed to load)"), *_USAGE)
    + f"\n\n'{COMMAND} COMMAND --help' tells more about a command."
)


def main(argv: list[str] | None = None, *, ctrl_c: threading.Event | None = None) -> int:
    """Runs the command line ``argv`` (default: the program's own). ``ctrl_c`` is set by a Ctrl+C
    that came before, while the program started (see packaging/tafl_cli.py): then it stops at
    once. As the program (no ``argv``), Ctrl+C is ignored once the work is over, up to the end:
    a Ctrl+C while Python ends can make the program hang."""
    args = _parser().parse_args(argv)
    console = Console()
    stop = CancelEvent()  # setting it also stops a formula that runs, e.g. of a parameter
    code: int | None = None
    try:
        with stopping_queries(stop), _ctrl_c(stop, ctrl_c, program=argv is None):
            code = _main(args, console, stop)
    except (KeyboardInterrupt, Cancelled):
        if code is None:  # a Ctrl+C after the work is over doesn't change its result
            console.error("Stopped")
            code = STOPPED
    return code


def _parser() -> argparse.ArgumentParser:
    lines = argparse.RawDescriptionHelpFormatter
    parser = argparse.ArgumentParser(
        prog=COMMAND, description=f"{APP_NAME}: run and inspect projects.", epilog=_CODES, formatter_class=lines
    )
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--plugins", action="append", default=[], metavar="FOLDER", help="load plugins from FOLDER too")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    project = f"the project file (*{EXTENSION})"
    values = "a value for the project's parameter NAME (a fixed value, or =formula)"

    run = commands.add_parser(
        "run", help="calculate all tables and execute exports and commands", epilog=_RUN_CODES, formatter_class=lines
    )
    run.add_argument("project", help=project)
    run.add_argument("--table", action="append", metavar="NAME", help="only this table (and the tables it uses)")
    run.add_argument("--preview", action="store_true", help="calculate only; skip exports and commands")
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", dest="values", help=values)

    show = commands.add_parser(
        "show", help="show the steps of a table and its data", epilog=_SHOW_CODES, formatter_class=lines
    )
    show.add_argument("project", help=project)
    show.add_argument("--table", required=True, metavar="NAME", help="the table to show")
    show.add_argument("--step", type=int, metavar="N", help="show the data after step N (default: the last step)")
    show.add_argument("--rows", type=_at_least_one, default=20, metavar="N", help="rows to show (default 20)")
    show.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", dest="values", help=values)

    commands.add_parser(
        "plugins", help="list plugins, their actions and load errors", epilog=_PLUGIN_CODES, formatter_class=lines
    )
    return parser


def _at_least_one(text: str) -> int:
    try:
        number = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"'{text}' is not a whole number") from None
    if number < 1:
        raise argparse.ArgumentTypeError(f"needs at least 1, not {number}")
    return number


@contextlib.contextmanager
def _ctrl_c(stop: threading.Event, before: threading.Event | None = None, *, program: bool = False) -> Iterator[None]:
    """Ctrl+C sets ``stop`` before it interrupts: the engine then stops the calculation, instead
    of taking the interruption for an error of the step it came in. The interruption comes when
    the program runs Python code again, which a query in DuckDB may not let it do until the query
    is over: so ``stop`` is also set at once by another thread, which stops the query. ``before``
    set: Ctrl+C came before the block. Afterwards Ctrl+C has its previous handling again, or with
    ``program`` it is ignored."""
    if threading.current_thread() is not threading.main_thread():  # signals reach only the main thread
        yield
        return
    over = False
    watching = False

    def interrupt(signum: int, frame: object) -> None:
        if over:  # a Ctrl+C that came while the block ended: the end goes on
            return
        # The handler may have interrupted the stop event's own code while it held its lock:
        # then only the flag is set here, and the watching thread does the rest of set()
        if watching and isinstance(stop, CancelEvent):
            stop.set_from_signal()
        else:
            stop.set()
        raise KeyboardInterrupt

    previous = signal.signal(signal.SIGINT, interrupt)
    end_watching = _watch_for_ctrl_c(stop)
    watching = end_watching is not _NOT_WATCHING
    try:
        if before is not None and before.is_set():
            interrupt(signal.SIGINT, None)
        yield
    finally:
        over = True  # first: a Ctrl+C from here on doesn't interrupt the end
        if program:
            signal.signal(signal.SIGINT, signal.SIG_IGN)
        else:
            signal.signal(signal.SIGINT, previous if previous is not None else signal.default_int_handler)
        end_watching()


def _not_watching() -> None:
    pass


_NOT_WATCHING = _not_watching  # what _watch_for_ctrl_c gives when it can't watch


def _watch_for_ctrl_c(stop: threading.Event) -> Callable[[], None]:
    """Sets ``stop`` from another thread when Ctrl+C comes: Python writes the number of each
    signal into a socket (``signal.set_wakeup_fd``). Returns the function that ends it."""
    try:
        reader, writer = socket.socketpair()
    except OSError:  # no sockets: Ctrl+C still stops the run, a query only when it is over
        return _NOT_WATCHING
    writer.setblocking(False)
    try:
        previous = signal.set_wakeup_fd(writer.fileno(), warn_on_full_buffer=False)
    except (OSError, ValueError):
        reader.close()
        writer.close()
        return _NOT_WATCHING

    def watch() -> None:
        with contextlib.suppress(OSError):
            while data := reader.recv(64):  # empty when the writer is closed
                if signal.SIGINT in data:
                    stop.set()

    thread = threading.Thread(target=watch, name="ctrl-c", daemon=True)
    thread.start()

    def end() -> None:
        signal.set_wakeup_fd(previous)
        writer.close()
        thread.join(1)
        reader.close()

    return end


def _main(args: argparse.Namespace, console: Console, stop: threading.Event) -> int:
    registry = Registry.default(args.plugins)
    if stop.is_set():  # Ctrl+C while a plugin was loading: that plugin has it as its error
        raise Cancelled()
    for warning in registry.warnings:
        console.warn(warning)
    if args.command == "plugins":
        return _plugins(registry, console)
    try:
        engine = Engine(_load(args.project), registry)
        engine.cancel = stop  # with it set, a KeyboardInterrupt is Ctrl+C, not a plugin's error
        for name, error in registry.errors:
            console.warn(f"Plugin '{name}' failed to load: {error.splitlines()[-1]}")
        if (code := _parameters(engine, args.values, console)) is not None:
            return code
        if args.command == "run":
            return _run(engine, args, console, stop)
        return _show(engine, args, console, stop)
    except ProjectError as e:
        console.error(str(e))
        return 2
    except OSError as e:
        console.error(f"{e.strerror or e}: {e.filename}" if e.filename else str(e))
        return 2


def _load(name: str) -> Project:
    path = Path(name).resolve()
    if path.is_dir():
        raise ProjectError(f"{path} is a folder, not a project file")
    if not path.exists():
        raise ProjectError(f"There is no project file {path}")
    try:
        return Project.load(path)
    except OSError as e:  # e.g. no permission
        raise ProjectError(f"The project file {path} can't be read: {e.strerror or e}") from e


def _parameters(engine: Engine, given: list[str], console: Console) -> int | None:
    """Takes the --set values. 2 if a value is given for no parameter, or a name can't be used
    (a name that nothing uses only gets a warning)."""
    overrides = {}
    for item in given:
        name, sign, value = item.partition("=")
        if not sign or not name.strip():
            console.error(f"--set needs NAME=VALUE, not '{item}'")
            return 2
        overrides[name.strip()] = value
    found = parameters.problems(engine.project, overrides, ignore_unused=True)
    for problem in found:
        console.error(problem)
    if found:
        return 2
    for warning in parameters.ignored(engine.project):
        console.warn(warning)
    engine.parameter_overrides = overrides
    return None


def _list_parameters(engine: Engine, console: Console) -> None:
    """The parameters with the values the calculation used (a run works them out anew)."""
    if engine.project.parameters:
        console.print("Parameters")
        for name, value in engine.parameter_values().items():
            shown = f"error: {value.message}" if isinstance(value, Failed) else as_text(value)
            console.print(f"  {{{name}}} = {shown}")


def _run(engine: Engine, args: argparse.Namespace, console: Console, stop: threading.Event) -> int:
    """Prints each table as soon as it is calculated. After Ctrl+C, what ran before the stop is
    printed too: the tables that were done, and the steps of the table that was stopped."""
    started = time.perf_counter()
    failed = listed = False

    def done(table_id: str, result: TableResult) -> None:
        nonlocal failed, listed
        if not listed:  # the values the run uses (a run works them out anew)
            _list_parameters(engine, console)
            listed = True
        failed |= _print_table(engine, engine.project.table(table_id), result, console)

    try:
        mode = Mode.PREVIEW if args.preview else Mode.RUN
        engine.run(args.table, mode=mode, cancel=stop, done=done, keep_results=False)  # printed: nothing here needs them
    except (KeyboardInterrupt, Cancelled):
        console.print(f"Stopped after {time.perf_counter() - started:.2f}s")
        raise
    if not listed:  # a project without tables
        _list_parameters(engine, console)
    console.print(f"{'Failed' if failed else 'Done'} in {time.perf_counter() - started:.2f}s")
    return 1 if failed else 0


def _print_table(engine: Engine, table: Table, result: TableResult, console: Console) -> bool:
    """Prints a table's steps with their results; True if it has an error."""
    console.print(table.name)
    if result.skipped:
        console.print(f"  {console.symbol(Status.DISABLED)} skipped: {result.skipped}")
        return False
    failed = result.error is not None
    if result.error:
        console.print(f"  {console.symbol(Status.ERROR)} {result.error}")
    for number, (step, step_result) in enumerate(zip(table.steps, result.steps), start=1):
        console.step(number, engine.describe(step), step_result)
        failed |= step_result.status == Status.ERROR
    if result.stopped:
        for number, step in enumerate(table.steps[len(result.steps) :], start=len(result.steps) + 1):
            running = number == len(result.steps) + 1
            console.step(number, engine.describe(step), None, stopped=running)
            if running:  # e.g. how many files it had moved
                console.notes(result.stopped_messages)
    return failed


def _show(engine: Engine, args: argparse.Namespace, console: Console, stop: threading.Event) -> int:
    table = engine.project.table(args.table)
    if args.step is not None and not 1 <= args.step <= len(table.steps):
        console.error(f"Table '{table.name}' has steps 1 to {len(table.steps)}")
        return 2
    result = engine.evaluate(table.id, until=None if args.step is None else args.step - 1, cancel=stop)
    _list_parameters(engine, console)
    console.print(table.name)
    if result.skipped:
        console.print(f"  {console.symbol(Status.DISABLED)} skipped: {result.skipped}")
    if result.error:
        console.print(f"  {console.symbol(Status.ERROR)} {result.error}")
    for number, step in enumerate(table.steps, start=1):
        step_result = result.steps[number - 1] if number <= len(result.steps) else None
        console.step(number, engine.describe(step), step_result, selected=number == len(result.steps))
    if result.skipped:
        return 0
    shown = result.steps[-1] if result.steps else None
    if shown is None or shown.df is None:
        return 1
    console.print(f"\n{_size(shown.df)}")
    console.table(shown.df, args.rows)
    return 0 if result.ok else 1


def _plugins(registry: Registry, console: Console) -> int:
    for info in registry.plugins:
        origin = "built-in" if info.builtin else info.path
        console.print(f"{info.name}  ({origin})")
        for action_id in info.actions:
            action = registry.actions[action_id]
            console.print(f"    {action.id:<24} {action.name}  [{action.category}]")
        for connector_id in info.connectors:
            console.print(f"    {connector_id:<24} {registry.connectors[connector_id].name}  [connection type]")
        for error in info.errors:
            console.print("    ERROR: " + error.replace("\n", "\n           "))
    return 1 if registry.errors else 0


def _size(df: pl.DataFrame) -> str:
    """"1 row × 3 columns"."""
    return f"{_count(df.height, 'row')} × {_count(df.width, 'column')}"


def _count(number: int, word: str) -> str:
    return f"{number:,} {word}{'' if number == 1 else 's'}"


class Console:
    """Prints with Unicode symbols when the terminal can show them, plain ASCII otherwise."""

    _SYMBOLS = {
        Status.OK: ("✓", "OK  "),
        Status.ERROR: ("✗", "ERR "),
        Status.SKIPPED: ("–", "SKIP"),
        Status.DISABLED: ("○", "OFF "),
        Status.BLOCKED: ("·", "--  "),
    }
    _WORDS = {"×": "x", "→": "->", "↓": "desc", "…": "...", "–": "-"}  # without Unicode

    def __init__(self) -> None:
        encoding = getattr(sys.stdout, "encoding", None) or "ascii"
        try:
            "✓✗–○·─│×→".encode(encoding)
            self.unicode = True
        except (UnicodeEncodeError, LookupError):
            self.unicode = False
        try:
            sys.stdout.reconfigure(errors="replace")
        except AttributeError:
            pass

    def symbol(self, status: Status) -> str:
        unicode, ascii_ = self._SYMBOLS[status]
        return unicode if self.unicode else ascii_

    def plain(self, text: str) -> str:
        """``text`` as it is printed: without Unicode, the symbols as words."""
        if not self.unicode:
            for symbol, word in self._WORDS.items():
                text = text.replace(symbol, word)
        return text

    def print(self, text: str = "") -> None:
        print(self.plain(text), flush=True)  # in a log file too, even if the run is ended

    def table(self, df: pl.DataFrame, rows: int) -> None:
        """The first ``rows`` rows of ``df``. Without Unicode the symbols in the data become words
        before Polars lines up the columns."""
        shown = df.head(rows)
        if not self.unicode:
            symbols, words = list(self._WORDS), list(self._WORDS.values())
            shown = shown.with_columns(pl.col(pl.String).str.replace_many(symbols, words))
            names = [self.plain(name) for name in shown.columns]
            if len(set(names)) == len(names):
                shown.columns = names
        with pl.Config(
            tbl_rows=rows,
            tbl_cols=-1,
            tbl_width_chars=250,
            fmt_str_lengths=40,
            tbl_hide_dataframe_shape=True,
            tbl_formatting="UTF8_FULL_CONDENSED" if self.unicode else "ASCII_FULL_CONDENSED",
        ):
            self.print(str(shown))

    def warn(self, text: str) -> None:
        print(f"Warning: {text}", file=sys.stderr)

    def error(self, text: str) -> None:
        print(f"Error: {text}", file=sys.stderr)

    def step(
        self, number: int, description: str, result: StepResult | None, selected: bool = False, stopped: bool = False
    ) -> None:
        """``result`` None: the step didn't run; ``stopped``: it was running when the run was stopped."""
        marker = ">" if selected else " "
        if len(description) > _SUMMARY:  # e.g. a filter with thousands of values
            description = description[: _SUMMARY - 1].rstrip() + "…"
        if stopped:
            self.print(f" {marker}{self.symbol(Status.BLOCKED)} {number}. {description}   (stopped)")
            return
        if result is None:
            self.print(f" {marker}      {number}. {description}")
            return
        details = ""
        if result.status == Status.OK and result.df is not None:
            details = f"   {_size(result.df)}"
            details += "   (cached)" if result.cached else f"   {result.seconds:.2f}s"
        elif result.status == Status.ERROR and result.seconds:  # it ran, then failed
            details = f"   {result.seconds:.2f}s"
        elif result.status == Status.SKIPPED:
            details = f"   (skipped: {result.skip_reason})" if result.skip_rest else "   (runs only when the project is run)"
        self.print(f" {marker}{self.symbol(result.status)} {number}. {description}{details}")
        if result.error and result.status == Status.ERROR:
            for line in result.error.splitlines():
                self.print(f"          {line}")
        self.notes((level, text) for level, text in result.messages if level == "warning" or result.status != Status.SKIPPED)

    def notes(self, messages: Iterable[tuple[str, str]]) -> None:
        """A step's messages, below it."""
        for level, message in messages:
            self.print(f"          {'!' if level == 'warning' else 'i'} {message}")


if __name__ == "__main__":
    raise SystemExit(main())

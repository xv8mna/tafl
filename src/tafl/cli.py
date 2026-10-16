"""Command line: run projects, look at tables, list plugins.

    tafl run PROJECT [--table NAME ...] [--preview] [--set NAME=VALUE ...]
    tafl show PROJECT --table NAME [--step N] [--rows 20] [--set NAME=VALUE ...]
    tafl plugins
"""

from __future__ import annotations

import argparse
import sys
import time

import polars as pl

from tafl import APP_NAME, COMMAND, __version__
from tafl.api import Mode
from tafl.api.parameters import Failed, as_text
from tafl.core import Engine, Project, ProjectError, Registry, Status, StepResult, parameters


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog=COMMAND, description=f"{APP_NAME}: run and inspect projects.")
    parser.add_argument("--version", action="version", version=f"{APP_NAME} {__version__}")
    parser.add_argument("--plugins", action="append", default=[], metavar="FOLDER", help="load plugins from FOLDER too")
    commands = parser.add_subparsers(dest="command", required=True)

    run = commands.add_parser("run", help="calculate all tables and execute exports and commands")
    run.add_argument("project")
    run.add_argument("--table", action="append", metavar="NAME", help="only this table (and the tables it uses)")
    run.add_argument("--preview", action="store_true", help="calculate only; skip exports and commands")
    run.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", dest="values",
                     help="a value for the project's parameter NAME (a fixed value, or =formula)")  # fmt: skip

    show = commands.add_parser("show", help="show the steps of a table and its data")
    show.add_argument("project")
    show.add_argument("--table", required=True, metavar="NAME")
    show.add_argument("--step", type=int, metavar="N", help="show the data after step N (default: the last step)")
    show.add_argument("--rows", type=int, default=20, metavar="N", help="rows to show (default 20)")
    show.add_argument("--set", action="append", default=[], metavar="NAME=VALUE", dest="values",
                      help="a value for the project's parameter NAME (a fixed value, or =formula)")  # fmt: skip

    commands.add_parser("plugins", help="list plugins, their actions and load errors")

    args = parser.parse_args(argv)
    console = Console()
    registry = Registry.default(args.plugins)
    if args.command == "plugins":
        return _plugins(registry, console)
    try:
        engine = Engine(Project.load(args.project), registry)
        for name, error in registry.errors:
            console.warn(f"Plugin '{name}' failed to load: {error.splitlines()[-1]}")
        if (code := _parameters(engine, args.values, console)) is not None:
            return code
        if args.command == "run":
            return _run(engine, args, console)
        return _show(engine, args, console)
    except (ProjectError, OSError) as e:
        console.error(str(e))
        return 2


def _parameters(engine: Engine, given: list[str], console: Console) -> int | None:
    """Takes the --set values. 2 if a value is given for no parameter, or a name can't be used."""
    overrides = {}
    for item in given:
        name, sign, value = item.partition("=")
        if not sign or not name.strip():
            console.error(f"--set needs NAME=VALUE, not '{item}'")
            return 2
        overrides[name.strip()] = value
    found = parameters.problems(engine.project, overrides)
    for problem in found:
        console.error(problem)
    if found:
        return 2
    engine.parameter_overrides = overrides
    return None


def _list_parameters(engine: Engine, console: Console) -> None:
    """The parameters with the values the calculation used (a run works them out anew)."""
    if engine.project.parameters:
        console.print("Parameters")
        for name, value in engine.parameter_values().items():
            shown = f"error: {value.message}" if isinstance(value, Failed) else as_text(value)
            console.print(f"  {{{name}}} = {shown}")


def _run(engine: Engine, args: argparse.Namespace, console: Console) -> int:
    started = time.perf_counter()
    results = engine.run(args.table, mode=Mode.PREVIEW if args.preview else Mode.RUN)
    _list_parameters(engine, console)
    failed = False
    for table_id, result in results.items():
        table = engine.project.table(table_id)
        console.print(table.name)
        if result.skipped:
            console.print(f"  {console.symbol(Status.DISABLED)} skipped: {result.skipped}")
            continue
        if result.error:
            console.print(f"  {console.symbol(Status.ERROR)} {result.error}")
            failed = True
        for number, (step, step_result) in enumerate(zip(table.steps, result.steps), start=1):
            console.step(number, engine.describe(step), step_result)
            failed |= step_result.status == Status.ERROR
    console.print(f"{'Failed' if failed else 'Done'} in {time.perf_counter() - started:.2f}s")
    return 1 if failed else 0


def _show(engine: Engine, args: argparse.Namespace, console: Console) -> int:
    table = engine.project.table(args.table)
    if args.step is not None and not 1 <= args.step <= len(table.steps):
        console.error(f"Table '{table.name}' has steps 1 to {len(table.steps)}")
        return 2
    result = engine.evaluate(table.id, until=None if args.step is None else args.step - 1)
    _list_parameters(engine, console)
    console.print(table.name)
    if result.error:
        console.print(f"  {console.symbol(Status.ERROR)} {result.error}")
    for number, step in enumerate(table.steps, start=1):
        step_result = result.steps[number - 1] if number <= len(result.steps) else None
        console.step(number, engine.describe(step), step_result, selected=number == len(result.steps))
    shown = result.steps[-1] if result.steps else None
    if shown is None or shown.df is None:
        return 1
    df = shown.df
    console.print(f"\n{df.height:,} rows × {df.width} columns")
    with pl.Config(
        tbl_rows=args.rows,
        tbl_cols=-1,
        tbl_width_chars=250,
        fmt_str_lengths=40,
        tbl_hide_dataframe_shape=True,
        tbl_formatting="UTF8_FULL_CONDENSED" if console.unicode else "ASCII_FULL_CONDENSED",
    ):
        console.print(str(df.head(args.rows)))
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


class Console:
    """Prints with Unicode symbols when the terminal can show them, plain ASCII otherwise."""

    _SYMBOLS = {
        Status.OK: ("✓", "OK  "),
        Status.ERROR: ("✗", "ERR "),
        Status.SKIPPED: ("–", "SKIP"),
        Status.DISABLED: ("○", "OFF "),
        Status.BLOCKED: ("·", "--  "),
    }

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

    def print(self, text: str = "") -> None:
        if not self.unicode:
            text = text.replace("×", "x").replace("→", "->").replace("↓", "desc").replace("…", "...").replace("–", "-")
        print(text)

    def warn(self, text: str) -> None:
        print(f"Warning: {text}", file=sys.stderr)

    def error(self, text: str) -> None:
        print(f"Error: {text}", file=sys.stderr)

    def step(self, number: int, description: str, result: StepResult | None, selected: bool = False) -> None:
        marker = ">" if selected else " "
        if result is None:
            self.print(f" {marker}      {number}. {description}")
            return
        details = ""
        if result.status == Status.OK and result.df is not None:
            details = f"   {result.df.height:,} rows × {result.df.width} columns"
            details += "   (cached)" if result.cached else f"   {result.seconds:.2f}s"
        elif result.status == Status.SKIPPED:
            details = "   (runs only when the project is run)"
        self.print(f" {marker}{self.symbol(result.status)} {number}. {description}{details}")
        if result.error and result.status == Status.ERROR:
            for line in result.error.splitlines():
                self.print(f"          {line}")
        for level, message in result.messages:
            if level == "warning" or result.status != Status.SKIPPED:
                self.print(f"          {'!' if level == 'warning' else 'i'} {message}")


if __name__ == "__main__":
    raise SystemExit(main())

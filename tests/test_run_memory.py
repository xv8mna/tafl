"""A run that lets go of finished steps and tables, as ``tafl run`` does: what it still holds."""

import os
import re
import subprocess
import sys
import types
import weakref

import polars as pl
import pytest

from tafl import cli
from tafl.api import action
from tafl.core import Engine, Project, Registry


def _project(folder) -> Project:
    """Orders; Copy and Both use it; Lonely reads the same file as Orders."""
    (folder / "orders.csv").write_text("Id\n2\n1\n", encoding="utf-8")
    project = Project(name="Run")
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    project.add_step(orders, "rows.sort", {"by": [{"column": "Id"}]})
    copy = project.add_table("Copy")
    project.add_step(copy, "table.derive", {"table": orders.id})
    project.add_step(copy, "rows.sort", {"by": [{"column": "Id", "order": "desc"}]})
    both = project.add_table("Both")
    project.add_step(both, "csv.import", {"file": "orders.csv"})
    project.add_step(both, "table.append", {"tables": [orders.id]})
    lonely = project.add_table("Lonely")
    project.add_step(lonely, "csv.import", {"file": "orders.csv"})
    project.add_step(lonely, "rows.sort", {"by": [{"column": "Id", "order": "desc"}]})
    return Project.load(project.save(folder / "run.tafl"))


def _names(project: Project, results) -> dict:
    return {project.table(table_id).name: result for table_id, result in results.items()}


def test_a_table_goes_when_no_later_table_uses_it(registry, tmp_path):
    project = _project(tmp_path)
    seen, held, rows = {}, [], {}

    def done(table_id, result):
        name = project.table(table_id).name
        seen[name] = result
        rows[name] = result.df.rows()  # the data is there for ``done``, also of a table that uses another
        held.append([table for table, finished in seen.items() if finished.steps[-1].df is not None])

    results = _names(project, Engine(project, registry).run(done=done, keep_results=False))
    assert held == [["Orders"], ["Orders", "Copy"], ["Orders", "Both"], ["Lonely"]]  # Both is the last to use Orders
    assert all(step.df is None for result in results.values() for step in result.steps)
    assert all(result.ok for result in results.values())
    assert rows == {
        "Orders": [(1,), (2,)],
        "Copy": [(2,), (1,)],
        "Both": [(2,), (1,), (1,), (2,)],
        "Lonely": [(2,), (1,)],
    }


def test_what_several_tables_read_stays_until_the_last_of_them_has_it(registry, tmp_path):
    project = _project(tmp_path)
    engine = Engine(project, registry)
    read_again, cached = {}, {}

    def done(table_id, result):
        name = project.table(table_id).name
        read_again[name], cached[name] = result.steps[0].cached, len(engine.cache)

    engine.run(done=done, keep_results=False)
    assert read_again == {"Orders": False, "Copy": False, "Both": True, "Lonely": True}
    assert cached == {"Orders": 1, "Copy": 1, "Both": 1, "Lonely": 1}  # the import: no sorted table, and no copy of a table
    assert len(engine.cache) == 0


def test_what_one_table_reads_stays_out_of_the_cache(registry, tmp_path):
    """It would hold on to the data for all steps of the table. A table that is off reads nothing."""
    project = _project(tmp_path)
    for table in project.tables[2:]:
        table.enabled = False
    engine = Engine(project, registry)
    cached = []
    engine.run(done=lambda *_: cached.append(len(engine.cache)), keep_results=False)
    assert cached == [0, 0, 0, 0]


def test_a_table_that_starts_from_another_table_reads_nothing_from_outside(registry, tmp_path):
    project = _project(tmp_path)
    twin = project.add_table("Twin")
    project.add_step(twin, "table.derive", {"table": project.tables[0].id})
    engine = Engine(project, registry)
    cached = {}

    def done(table_id, result):
        cached[project.table(table_id).name] = result.steps[0].cached

    engine.run(done=done, keep_results=False)
    assert not cached["Copy"] and not cached["Twin"]
    assert len(engine.cache) == 0


def test_a_run_of_some_tables_counts_only_those(registry, tmp_path):
    project = _project(tmp_path)
    engine = Engine(project, registry)
    cached = []
    engine.run(["Lonely"], done=lambda *_: cached.append(len(engine.cache)), keep_results=False)
    assert cached == [0]


@pytest.fixture
def watching() -> tuple[Registry, list]:
    """The built-in actions, and a step that notes which results of the steps before it are alive."""
    module = types.ModuleType("watching")
    frames, alive = [], []

    @action("demo.watch", "Watch")
    def watch(df, p, ctx):
        alive.append([frame() is not None for frame in frames])
        out = df.clone()
        frames.append(weakref.ref(out))
        return out

    module.watch = watch
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    return registry, alive


def _watched(folder) -> Project:
    (folder / "orders.csv").write_text("Id\n2\n1\n", encoding="utf-8")
    project = Project(name="Watched")
    table = project.add_table("Orders")
    project.add_step(table, "csv.import", {"file": "orders.csv"})
    for _ in range(4):
        project.add_step(table, "demo.watch", {})
    return Project.load(project.save(folder / "watched.tafl"))


def test_the_data_of_a_step_goes_once_the_next_step_has_run(watching, tmp_path):
    registry, alive = watching
    Engine(_watched(tmp_path), registry).run(keep_results=False)
    assert alive == [[], [True], [False, True], [False, False, True]]  # a step needs the step before it


def test_the_window_keeps_the_data_of_all_steps(watching, tmp_path):
    registry, alive = watching
    Engine(_watched(tmp_path), registry).run()
    assert alive == [[], [True], [True, True], [True, True, True]]


def _mixed(folder) -> Project:
    """Tables that read step data after the steps: a folder import with "All files this table's
    import read", the rest of a table skipped, a table that uses it, a failing step, steps that are off."""
    (folder / "inbox").mkdir(parents=True)
    (folder / "inbox" / "a.csv").write_text("Id;Amount\n1;5\n2;7\n", encoding="utf-8")
    (folder / "inbox" / "b.csv").write_text("Id;Amount\n3;9\n", encoding="utf-8")
    (folder / "one.csv").write_text("Id;Amount\n1;5\n", encoding="utf-8")
    project = Project(name="Mixed")
    daily = project.add_table("Daily")
    project.add_step(daily, "csv.import", {"source": "folder", "folder": "inbox", "pattern": "*.csv"})
    project.add_step(daily, "rows.sort", {"by": [{"column": "Id", "order": "desc"}]})
    project.add_step(daily, "csv.export", {"file": "out/daily.csv"})
    project.add_step(daily, "files.copy", {"which": "imported", "target": "copies"})
    empty = project.add_table("Empty")
    project.add_step(empty, "csv.import", {"file": "one.csv"})
    project.add_step(empty, "rows.filter_formula", {"condition": "[Amount] > 100"})
    project.add_step(empty, "flow.skip_if_empty", {})
    project.add_step(empty, "calc.columns", {"columns": [{"name": "Double", "formula": "[Amount] * 2"}]})
    project.add_step(empty, "csv.export", {"file": "out/empty.csv"}).enabled = False
    after = project.add_table("After")
    project.add_step(after, "table.derive", {"table": empty.id})
    project.add_step(after, "rows.sort", {"by": [{"column": "Id"}]})
    joined = project.add_table("Joined")
    project.add_step(joined, "csv.import", {"file": "one.csv"})
    project.add_step(joined, "table.append", {"tables": [daily.id]})
    project.add_step(joined, "rows.sort", {"by": [{"column": "Id"}]}).enabled = False
    project.add_step(joined, "rows.sort", {"by": [{"column": "Id", "order": "desc"}]})
    broken = project.add_table("Broken")
    project.add_step(broken, "table.derive", {"table": joined.id})
    project.add_step(broken, "calc.columns", {"columns": [{"name": "X", "formula": "[Missing] + 1"}]})
    project.add_step(broken, "rows.sort", {"by": [{"column": "Id"}]})
    used = project.add_table("Uses broken")
    project.add_step(used, "table.derive", {"table": broken.id})
    return Project.load(project.save(folder / "mixed.tafl"))


def _report(folder, registry, capsys, *, keep_results: bool) -> str:
    """What ``tafl run`` prints of a run, printed after the run, when all data has gone."""
    project = _mixed(folder)
    engine = Engine(project, registry)
    results = engine.run(keep_results=keep_results)
    assert sorted(path.name for path in (folder / "copies").iterdir()) == ["a.csv", "b.csv"]
    assert (folder / "out" / "daily.csv").read_text(encoding="utf-8-sig") == "File;Id;Amount\nb.csv;3;9\na.csv;2;7\na.csv;1;5\n"
    capsys.readouterr()
    for table_id, result in results.items():
        cli._print_table(engine, project.table(table_id), result, cli.Console())
    return re.sub(r"\d+\.\d\ds", "0.00s", capsys.readouterr().out).replace(str(folder), "<folder>")


def test_the_report_of_a_run_is_the_same_without_the_data(registry, tmp_path, capsys):
    (tmp_path / "keep").mkdir()
    (tmp_path / "go").mkdir()
    kept = _report(tmp_path / "keep", registry, capsys, keep_results=True)
    assert _report(tmp_path / "go", registry, capsys, keep_results=False) == kept
    assert "  ✓ 2. Sort by Id ↓   3 rows × 3 columns   0.00s\n" in kept
    assert "  ✓ 3. Skip the rest if the table is empty   0 rows × 2 columns   0.00s\n" in kept
    assert "  ✓ 1. Import one.csv   1 row × 2 columns   (cached)\n" in kept
    assert "  ✓ 1. Start from Joined   4 rows × 3 columns   0.00s\n" in kept


def test_by_default_a_run_keeps_everything(registry, tmp_path):
    """The window shows the results of a run, and a preview afterwards finds them in the cache."""
    project = _project(tmp_path)
    engine = Engine(project, registry)
    results = _names(project, engine.run())
    assert all(step.df is not None for result in results.values() for step in result.steps)
    assert len(engine.cache) == 6
    assert all(step.cached for step in engine.run(mode="preview")[project.tables[-1].id].steps)


def test_a_table_used_twice_goes_after_its_last_user(registry, tmp_path):
    project = _project(tmp_path)
    # Lonely uses Orders too, so that the last use comes two tables after the first
    project.add_step(project.tables[3], "table.append", {"tables": [project.tables[0].id]})
    results = _names(project, Engine(project, registry).run(keep_results=False))
    assert all(result.ok for result in results.values())


def test_a_table_can_use_a_table_that_used_another(registry, tmp_path):
    (tmp_path / "orders.csv").write_text("Id\n1\n", encoding="utf-8")
    project = Project(name="Chain")
    first = project.add_table("First")
    project.add_step(first, "csv.import", {"file": "orders.csv"})
    second = project.add_table("Second")
    project.add_step(second, "table.derive", {"table": first.id})
    third = project.add_table("Third")
    project.add_step(third, "table.derive", {"table": second.id})
    project = Project.load(project.save(tmp_path / "chain.tafl"))
    rows = {}
    engine = Engine(project, registry)
    engine.run(done=lambda table_id, result: rows.update({table_id: result.df.rows()}), keep_results=False)
    assert rows == {first.id: [(1,)], second.id: [(1,)], third.id: [(1,)]}
    assert len(engine.cache) == 0


def test_a_table_that_is_off_and_a_circular_reference_end_as_ever(registry, tmp_path):
    project = _project(tmp_path)
    one, two = project.add_table("One"), project.add_table("Two")
    project.add_step(one, "table.derive", {"table": two.id})
    project.add_step(two, "table.derive", {"table": one.id})
    project.tables[1].enabled = False
    results = _names(project, Engine(project, registry).run(keep_results=False))
    assert results["Copy"].skipped == "turned off"
    assert results["Orders"].ok and results["Lonely"].ok
    assert "Circular reference" in results["One"].first_error


def test_tafl_run_lets_go_and_still_prints_the_sizes(tmp_path, monkeypatch, capsys):
    path = _project(tmp_path).path
    asked = []
    run = Engine.run

    def spy(self, *args, **options):
        asked.append(options.get("keep_results", True))
        return run(self, *args, **options)

    monkeypatch.setattr(Engine, "run", spy)
    assert cli.main(["run", str(path)]) == 0
    assert asked == [False]
    output = capsys.readouterr().out
    assert "Both\n" in output and "2. Append" in output and "4 rows × 1 column" in output
    assert len(re.findall(r"\d+ rows × 1 column   ", output)) == 8  # of every step


_MEMORY = """
import ctypes
import sys
from ctypes import wintypes


class Counters(ctypes.Structure):  # PROCESS_MEMORY_COUNTERS
    _fields_ = [("size", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
        (name, ctypes.c_size_t) for name in ("peak_set", "set", "a", "b", "c", "d", "used", "peak")
    ]


def memory():
    counters = Counters(ctypes.sizeof(Counters))
    process = ctypes.WinDLL("kernel32").GetCurrentProcess
    process.restype = wintypes.HANDLE
    read = ctypes.WinDLL("psapi").GetProcessMemoryInfo
    read.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    read(process(), ctypes.byref(counters), counters.size)
    return counters
"""

_RUN = (
    _MEMORY
    + """
from tafl import cli

assert cli.main(["run", sys.argv[1]]) == 0
print(memory().peak >> 20)
"""
)

_FORMULA = (
    _MEMORY
    + """
import polars as pl

from tafl.api import expressions

part = 12_500
parts = [pl.select(pl.int_range(k * part, (k + 1) * part).cast(pl.Float64).alias("Amount")) for k in range(80)]
df = pl.concat(parts, rechunk=False)
df = df.with_columns((pl.col("Amount") + c).alias(f"Column {c}") for c in range(int(sys.argv[1]) - 1))
expressions.evaluate(df.head(10), "[Amount] + 2")  # DuckDB's own start is not part of the formula
before = memory().used
assert expressions.evaluate(df, "[Amount] + 2").len() == df.height
print((memory().peak - before) >> 20)
"""
)


def _peak(folder, code: str, *args: str) -> int:
    """In a process of its own, because only there the peak says something."""
    environment = {**os.environ, "APPDATA": str(folder / "appdata")}  # no plugins of this computer
    environment.pop("TAFL_PLUGINS", None)
    done = subprocess.run([sys.executable, "-c", code, *args], capture_output=True, text=True, env=environment, timeout=120)
    assert done.returncode == 0, done.stderr[-500:]
    return int(done.stdout.split()[-1])


def _sorts(folder, count: int):
    project = Project(name="Sorts")
    table = project.add_table("Rows")
    project.add_step(table, "parquet.import", {"file": "rows.parquet"})
    for number in range(count):
        order = "desc" if number % 2 else "asc"
        project.add_step(table, "rows.sort", {"by": [{"column": "Amount", "order": order}]})
    return project.save(folder / f"sorts{count}.tafl")


@pytest.mark.skipif(sys.platform != "win32", reason="reads the peak of the process with a Windows call")
def test_the_memory_of_a_run_doesnt_grow_with_the_steps_of_a_table(tmp_path):
    """Each step of a table would otherwise stay until the end of the table, in its result and in
    the cache (1 million rows: 1.6 to 1.8 times the peak of one sort, by the number of threads)."""
    place = pl.int_range(1_000_000, eager=True)
    columns = {"Id": place, "Amount": (place.hash(1) % 1_000_000) / 100, "Cost": (place.hash(2) % 1_000_000) / 100}
    pl.DataFrame(columns).write_parquet(tmp_path / "rows.parquet")
    one = _peak(tmp_path, _RUN, str(_sorts(tmp_path, 1)))
    six = _peak(tmp_path, _RUN, str(_sorts(tmp_path, 6)))
    assert six < 1.3 * one, (six, one)


@pytest.mark.skipif(sys.platform != "win32", reason="reads the peak of the process with a Windows call")
def test_the_memory_of_a_formula_doesnt_grow_with_the_columns_it_doesnt_use(tmp_path):
    """DuckDB gets the row number and the columns of the formula: Polars lines up the parts of all
    columns it hands over, which copies them (60 columns in 80 parts: almost 5 times the peak of one)."""
    wide = _peak(tmp_path, _FORMULA, "60")
    alone = _peak(tmp_path, _FORMULA, "1")
    assert wide < 2 * alone, (wide, alone)

"""A run that lets go of finished tables, as ``tafl run`` does: what it still holds after each table."""

from tafl import cli
from tafl.core import Engine, Project


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


def test_what_a_table_read_stays_for_the_tables_that_read_the_same(registry, tmp_path):
    project = _project(tmp_path)
    engine = Engine(project, registry)
    read_again = {}

    def done(table_id, result):
        read_again[project.table(table_id).name] = result.steps[0].cached

    engine.run(done=done, keep_results=False)
    assert read_again == {"Orders": False, "Copy": False, "Both": True, "Lonely": True}
    assert len(engine.cache) == 1  # the import: no sorted table, and no copy of a table


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
    assert len(engine.cache) == 1


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

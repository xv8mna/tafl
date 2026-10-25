import re
import tempfile
from pathlib import Path

import pytest

from tafl.core import Connection, Engine, Parameter, Project, ProjectError, Status


def test_open_select_and_calculate(qtbot, settle, session, demo):
    session.open_project(demo)
    settle(session)
    assert session.table.name == "Customers" and session.result.ok
    session.select_table(session.project.table("Orders").id)
    settle(session)
    assert session.result.df.height == 156
    assert "t1" in session.results  # Customers, used by the merge, is known too
    assert session.input_columns(session.table.steps[3]) == ["OrderNo", "CustomerNo", "Date", "Net", "Status"]


def test_add_edit_undo_redo(qtbot, settle, session, demo, registry):
    session.open_project(demo)
    session.select_table(session.project.table("Orders").id)
    session.select_step(session.table.steps[2].id)
    step = session.add_step(registry.actions["rows.sort"])
    assert session.table.steps[3] is step and session.step is step
    assert step.params["by"] == [{"column": "", "order": "asc"}]  # a list that needs a row starts with one
    session.set_params(step.id, {"by": [{"column": "Net", "order": "desc"}]})
    settle(session)
    net = session.step_result(step).df["Net"].to_list()
    assert net == sorted(net, reverse=True)

    session.undo()
    assert session.find_step(step.id)[1].params["by"] == [{"column": "", "order": "asc"}]
    session.undo()
    assert step.id not in [s.id for s in session.table.steps]
    session.redo()
    session.redo()
    assert session.find_step(step.id)[1].params["by"][0]["column"] == "Net"
    assert session.dirty


def test_errors_reach_the_step_result(qtbot, settle, session, demo, registry):
    session.open_project(demo)
    session.select_table(session.project.table("Orders").id)
    step = session.add_step(registry.actions["rows.sort"])
    settle(session)
    result = session.step_result(step)
    assert result.status == Status.ERROR and "Sort by, row 1 – Column: required" in result.error


def test_new_table_is_named_after_its_file(qtbot, settle, session, registry, tmp_path):
    csv = tmp_path / "sales 2026.csv"
    csv.write_text("a;b\n1;2\n", encoding="utf-8")
    table = session.add_table(registry.actions["csv.import"])
    assert table.name == "Table 1"
    session.set_params(table.steps[0].id, {"file": str(csv)})
    assert table.name == "sales 2026"
    settle(session)
    assert session.result.df.columns == ["a", "b"]

    lake = tmp_path / "daily exports"
    lake.mkdir()
    (lake / "2026-01-01.csv").write_text("a;b\n3;4\n", encoding="utf-8")
    folder = session.add_table(registry.actions["csv.import"])
    session.set_params(folder.steps[0].id, {"source": "folder", "folder": str(lake)})
    assert folder.name == "daily exports"  # a folder import is named after the folder
    settle(session)
    assert session.result.df.columns == ["File", "a", "b"]


def test_saving_elsewhere_keeps_file_paths_working(qtbot, settle, session, demo, tmp_path):
    session.open_project(demo)
    session.save_project(tmp_path / "elsewhere" / "copy.tafl")
    first = session.project.tables[0].steps[0]
    assert Path(first.params["file"]).is_absolute()  # the data is outside the new folder
    session.save_project(demo)  # back next to the data: relative again
    assert first.params["file"] == "data/customers.xlsx"
    assert not session.dirty


def test_saving_into_a_folder_without_permission(qtbot, settle, session, demo, tmp_path, monkeypatch):
    session.open_project(demo)
    first = session.project.tables[0].steps[0]
    session.set_note(first.id, "checked")  # an undo step: its paths stay as they were too

    def denied(*args, **kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(tempfile, "mkstemp", denied)
    folder = tmp_path / "denied"
    with pytest.raises(ProjectError, match=f"^No permission to save in {re.escape(str(folder))}: save the project in another folder$"):
        session.save_project(folder / "copy.tafl")
    assert first.params["file"] == "data/customers.xlsx"  # not saved: the paths are as they were
    assert session.project.path == demo.resolve()
    session.undo()
    assert session.find_step(first.id)[1].params["file"] == "data/customers.xlsx"


def test_undo_and_redo_after_saving_elsewhere_keep_file_paths_working(session, registry, tmp_path):
    """The undo and redo steps get the paths of the new folder too. Before, undoing an edit of a
    path made before saving brought back a path relative to the old folder, which then pointed
    into the new one."""
    project = Project(name="x")
    project.add_connection("shop", "sqlite", file="data/shop.db")
    step = project.add_step(project.add_table("Orders"), registry.actions["csv.import"], {"file": "data/orders.csv"})
    project.save(tmp_path / "here" / "x.tafl")
    session.open_project(tmp_path / "here" / "x.tafl")
    session.set_params(step.id, {"file": "data/orders 2.csv"})
    session.set_connections([Connection("shop", "sqlite", {"file": "data/shop 2.db"})])
    session.set_note(step.id, "checked")  # its undo step shares the settings with the edit before
    session.undo()  # the note is a redo step now
    session.save_project(tmp_path / "x.tafl")  # one folder up: the paths start with here/

    def paths() -> tuple[str, str]:
        return session.find_step(step.id)[1].params["file"], session.project.connections[0].settings["file"]

    assert paths() == ("here/data/orders 2.csv", "here/data/shop 2.db")
    session.redo()
    assert session.find_step(step.id)[1].note == "checked" and paths() == ("here/data/orders 2.csv", "here/data/shop 2.db")
    session.undo()
    session.undo()
    assert paths() == ("here/data/orders 2.csv", "here/data/shop.db")
    session.undo()
    assert paths() == ("here/data/orders.csv", "here/data/shop.db")
    session.redo()
    session.redo()
    assert paths() == ("here/data/orders 2.csv", "here/data/shop 2.db")


def test_saving_elsewhere_keeps_paths_that_start_with_a_parameter(session, registry, tmp_path):
    """{Folder}\\orders.csv may stand for any folder: it stays as it is. A parameter later in a
    relative path is part of a file name: the path is kept working as any other."""
    project = Project(name="x", parameters=[Parameter("Folder", "D:\\Data"), Parameter("Day", "2026-10-01")])
    project.add_connection("shop", "sqlite", file="{Folder}\\shop.db")
    table = project.add_table("Orders")
    project.add_step(table, registry.actions["csv.import"], {"file": "{Folder}\\orders.csv"})
    daily = project.add_table("Daily")
    project.add_step(daily, registry.actions["csv.import"], {"file": "data\\{Day}.csv"})
    project.save(tmp_path / "here" / "x.tafl")
    session.open_project(tmp_path / "here" / "x.tafl")
    session.save_project(tmp_path / "elsewhere" / "x.tafl")
    assert session.project.tables[0].steps[0].params["file"] == "{Folder}\\orders.csv"
    assert session.project.connections[0].settings["file"] == "{Folder}\\shop.db"
    moved = session.project.tables[1].steps[0].params["file"]
    assert Path(moved).resolve() == (tmp_path / "here" / "data" / "{Day}.csv").resolve()  # outside the new folder: the full path
    session.save_project(tmp_path / "here" / "x.tafl")  # back: relative again
    assert session.project.tables[1].steps[0].params["file"] == "data/{Day}.csv"
    assert session.project.tables[0].steps[0].params["file"] == "{Folder}\\orders.csv"


def test_parameter_values_stay_until_a_reload(session, settle, demo):
    """The checks of formulas use the values of the calculation: worked out again when the
    parameters change and on Reload data, not with every edit (=RANDOM() stays the same)."""
    session.open_project(demo)
    session.set_parameters([Parameter("R", "=RANDOM()")])
    first = session.parameter_values()["R"]
    session.set_note(session.project.tables[0].steps[0].id, "An edit")
    settle(session)
    assert session.parameter_values()["R"] == first == session.engine.snapshot().parameter_values()["R"]
    session.reload_sources()
    second = session.parameter_values()["R"]
    assert second != first
    session.set_parameters([Parameter("R", "=RANDOM()"), Parameter("S", "1")])
    assert session.parameter_values()["R"] not in (first, second)
    session.undo()
    assert session.parameter_values()["R"] == second  # the same parameters as before: the same values


def test_calculating_all_tables_goes_on_after_a_selection_or_an_edit(qtbot, settle, session, demo):
    """calculate_all (the Find panel's Problems filter in the tables view): the selected table is
    calculated first, then the others, until every table has a result. Before, a selection or an
    edit while it calculated stopped it for good."""

    def calculated() -> set[str]:
        settle(session)
        return set(session.results) & {t.id for t in session.project.tables}

    session.open_project(demo)
    customers, orders, revenue = (t.id for t in session.project.tables)
    assert calculated() == {customers}
    session.calculate_all()
    session.select_table(orders)  # the selected table first: it stops the others for a while
    assert calculated() == {customers, orders, revenue}

    session.open_project(demo)  # another project: only the selected table again
    session.select_table(orders)
    assert calculated() == {customers, orders}
    session.calculate_all()
    session.set_note(session.project.tables[0].steps[0].id, "An edit")
    session.calculate_now()  # what the edit starts a moment later
    assert calculated() == {customers, orders, revenue}

    session.open_project(demo)
    session.calculate_all()
    session.calculate_all(False)  # the filter is turned off again
    session.select_table(orders)
    assert calculated() == {customers, orders}


def test_calculating_all_tables_stops_after_an_unexpected_error(qtbot, settle, session, demo, monkeypatch):
    """An unexpected error while all tables are calculated is shown once, not again after every
    selection."""
    session.open_project(demo)
    settle(session)
    errors = []
    session.error.connect(errors.append)
    monkeypatch.setattr(Engine, "run", lambda self, *args, **kwargs: 1 / 0)
    session.calculate_all()
    settle(session)
    for table in session.project.tables[1:]:
        session.select_table(table.id)
        settle(session)
    assert len(errors) == 1 and "ZeroDivisionError" in errors[0]


def test_run_project_writes_exports_and_previews_reuse_it(qtbot, settle, session, demo):
    session.open_project(demo)
    settle(session)
    with qtbot.waitSignal(session.run_finished, timeout=30_000) as finished:
        session.run_project()
    assert all(r.ok for r in finished.args[0].values())
    assert (demo.parent / "output" / "revenue.xlsx").exists()
    settle(session)
    assert session.result.steps[0].cached


def test_renaming_a_connection_updates_its_steps(session, registry, tmp_path):
    project = Project(name="x")
    project.add_connection("db", "sqlite", file="data.db")
    table = project.add_table("T")
    step = project.add_step(table, registry.actions["db.import"], {"connection": "db", "table": "t"})
    project.save(tmp_path / "x.tafl")
    session.open_project(tmp_path / "x.tafl")
    session.set_connections([Connection("main", "sqlite", {"file": "data.db"})], {"db": "main"})
    assert session.find_step(step.id)[1].params["connection"] == "main"
    session.find_step(step.id)[1].params["connection"] = ["main"]  # a file written by hand
    session.set_connections([Connection("db", "sqlite", {"file": "data.db"})], {"main": "db"})
    assert session.find_step(step.id)[1].params["connection"] == ["main"] and session.dirty


def test_undo_of_a_connection_rename_brings_back_the_old_name_in_the_steps(session, registry, tmp_path):
    project = Project(name="x")
    project.add_connection("Old", "sqlite", file="data.db")
    table = project.add_table("T")
    step = project.add_step(table, registry.actions["db.import"], {"connection": "Old", "table": "t"})
    project.save(tmp_path / "x.tafl")
    session.open_project(tmp_path / "x.tafl")
    session.set_connections([Connection("New", "sqlite", {"file": "data.db"})], renames={"Old": "New"})
    assert session.find_step(step.id)[1].params["connection"] == "New"
    session.undo()
    assert [c.name for c in session.project.connections] == ["Old"]
    assert session.find_step(step.id)[1].params["connection"] == "Old"
    session.redo()
    assert [c.name for c in session.project.connections] == ["New"]
    assert session.find_step(step.id)[1].params["connection"] == "New"
    session.undo()
    assert session.find_step(step.id)[1].params["connection"] == "Old"


def test_the_input_of_a_step_waits_for_new_results(qtbot, settle, session, demo):
    """After a step is moved, the old results don't show what goes into the steps after it."""
    session.open_project(demo)
    session.select_table(session.project.table("Orders").id)
    settle(session)
    calculate, numbering = session.table.steps[5], session.table.steps[6]
    assert "Gross" not in session.input_columns(calculate)
    session.move_step(numbering.id, 5)  # now before Calculate columns
    assert session.input_frame(calculate) is None  # not the old output of the numbering step
    assert session.input_problem(calculate).startswith("The steps before this one are still being calculated")
    settle(session)
    assert session.input_columns(calculate)[-1] == "Order of customer" and "Gross" not in session.input_columns(calculate)


def test_why_a_step_has_no_input(qtbot, settle, session, demo):
    session.open_project(demo)
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    settle(session)
    assert session.input_problem(orders.steps[0]).startswith("This is the first step")
    session.set_table_enabled([orders.id], False)
    settle(session)
    assert session.input_frame(orders.steps[5]) is None
    assert session.input_problem(orders.steps[5]) == "“Orders” is turned off, so it isn't calculated. Turn it on to see a preview."

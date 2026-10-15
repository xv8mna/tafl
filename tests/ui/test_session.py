from pathlib import Path

from etl_tool.core import Connection, Project, Status


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

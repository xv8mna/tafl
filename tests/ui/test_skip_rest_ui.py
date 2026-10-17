"""How the window shows a table whose rest was skipped (SkipRest)."""

import pytest

from tafl.core import Project
from tafl.ui.steps_panel import _ROLE


@pytest.fixture
def skipping(tmp_path):
    """Orders: import, filter (no rows are left), skip the rest if empty, calculate, export."""
    (tmp_path / "orders.csv").write_text("Id;Amount\n1;5\n2;7\n", encoding="utf-8")
    project = Project(name="Test")
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders.csv"})
    project.add_step(orders, "rows.filter_formula", {"condition": "[Amount] > 100"})
    project.add_step(orders, "flow.skip_if_empty", {})
    project.add_step(orders, "calc.columns", {"columns": [{"name": "Double", "formula": "[Amount] * 2"}]})
    project.add_step(orders, "csv.export", {"file": "out/orders.csv"})
    return project.save(tmp_path / "skip.tafl")


def test_steps_settings_and_data_say_why_steps_are_skipped(window, settle, session, skipping):
    window.open_path(skipping)
    session.select_table(session.project.table("Orders").id)
    settle(session)
    items = [window.steps_panel.list.item(row).data(_ROLE) for row in range(window.steps_panel.list.count())]
    assert [(i["status"], i["info"]) for i in items] == [
        ("ok", "2 × 2"), ("ok", "0 × 2"), ("ok", "0 × 2"), ("skipped", "skipped"), ("skipped", "skipped"),
    ]
    assert items[2]["tooltip"] == "The steps after this one are skipped: the table is empty"
    assert items[4]["tooltip"] == "Skipped: the table is empty"
    assert session.table_status(session.table.id) == "ok"

    assert window.settings_panel.info.text() == "This step is skipped: the table is empty."
    assert window.data_panel.info.text().endswith("skipped: the table is empty")
    session.select_step(session.table.steps[2].id)
    assert window.settings_panel.info.text() == "The steps after this one are skipped: the table is empty."


def test_run_results_and_map(qtbot, window, settle, session, skipping):
    window.open_path(skipping)
    settle(session)
    with qtbot.waitSignal(session.run_finished, timeout=30_000):
        session.run_project()
    settle(session)
    top = window.run_panel.tree.topLevelItem(0)
    assert top.text(1) == "ok, rest skipped: the table is empty"
    children = [top.child(i) for i in range(top.childCount())]
    assert [c.text(1) for c in children[2:]] == [
        "0 rows × 2 columns", "skipped: the table is empty", "skipped: the table is empty",
    ]
    assert [children[2].child(i).text(0) for i in range(children[2].childCount())] == ["The table is empty"]
    assert children[3].childCount() == 0  # the reason is in its result
    assert "no errors" in window.run_panel.summary.text()
    assert not (skipping.parent / "out").exists()

    window.show_map(True)
    settle(session)
    node = window.map_view.nodes[session.project.table("Orders").id]
    assert node.status == "ok"
    assert node._skipped() == "\nRest skipped: the table is empty"
    assert node._skipped(session.project.tables[0].steps[4].id) == "\nSkipped: the table is empty"
    assert node._skipped(session.project.tables[0].steps[0].id) == ""

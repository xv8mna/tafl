from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from etl_tool.ui.map import HEADER, NODE_WIDTH


def open_map(window, session, demo, settle):
    window.open_path(demo)
    window.show_map(True)
    window.resize(1500, 950)
    window.show()
    settle(session)
    view = window.map_view
    view.rebuild()
    view.fit()
    return view


def drag(view, start: QPointF, end: QPointF) -> None:
    """Press at a scene point, move in two steps, release (like a real mouse drag)."""
    viewport = view.canvas.viewport()
    a, b = view.canvas.mapFromScene(start), view.canvas.mapFromScene(end)
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=a)
    QTest.mouseMove(viewport, pos=(a + b) / 2)
    QTest.mouseMove(viewport, pos=b)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=b)


def test_a_link_goes_before_the_exports_with_a_guessed_key(settle, session, demo):
    session.open_project(demo)
    session.set_overview(True)
    settle(session)
    customers, orders, revenue = session.project.tables
    assert session.guess_key(orders.id, customers.id) == "CustomerNo"  # key-like names first
    step = session.add_link(customers.id, revenue.id, "left")
    assert revenue.steps.index(step) == 4 and revenue.steps[5].action == "excel.export"
    assert step.params["how"] == "left" and step.params["other"] == customers.id
    assert step.params["match"] == [{"column": "Name", "other_column": "Name"}]  # the only shared columns: Name, City
    assert session.step is step
    settle(session)
    assert session.step_result(step).df.columns[-1] == "City (2)"


def test_append_link(settle, session, demo):
    session.open_project(demo)
    settle(session)
    customers = session.project.tables[0]
    copy = session.add_derived_table(customers.id)
    step = session.add_link(customers.id, copy.id, "append")
    assert step.action == "table.append" and step.params["tables"] == [customers.id]
    settle(session)
    assert session.step_result(step).df.height == 12


def test_dragging_from_a_table_onto_another_adds_a_merge(qtbot, window, settle, session, demo, monkeypatch):
    view = open_map(window, session, demo, settle)
    asked = []
    monkeypatch.setattr(view, "choose_link_kind", lambda source, target, pos: asked.append((source.name, target.name)) or "inner")
    customers, revenue = session.project.tables[0], session.project.tables[2]
    target = view.nodes[revenue.id]
    drag(view, view.nodes[customers.id].out_anchor(), target.mapToScene(QPointF(NODE_WIDTH / 2, HEADER / 2)))
    assert asked == [("Customers", "Revenue by customer")]
    qtbot.waitUntil(lambda: any(s.action == "table.merge" for s in revenue.steps))
    step = next(s for s in revenue.steps if s.action == "table.merge")
    assert step.params["how"] == "inner" and session.step is step
    qtbot.waitUntil(lambda: any(e.link.step == step.id for e in view.edges))  # the arrow appears


def test_dropping_a_connection_on_an_empty_spot_starts_a_new_table(qtbot, window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    orders = session.project.tables[1]
    port = view.nodes[orders.id].out_anchor()
    drag(view, port, port + QPointF(150, 260))
    qtbot.waitUntil(lambda: len(session.project.tables) == 4)
    new = session.project.tables[-1]
    assert new.name == "Orders (derived)" and new.steps[0].params == {"table": orders.id}
    assert new.position is not None and session.table is new


def test_clicking_an_arrow_edits_its_step_and_delete_removes_it(qtbot, window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    orders = session.project.tables[1]
    edge = next(e for e in view.edges if e.target.table_id == orders.id)
    point = view.canvas.mapFromScene(edge.path().pointAtPercent(0.5))
    QTest.mouseClick(view.canvas.viewport(), Qt.MouseButton.LeftButton, pos=point)
    assert session.table is orders and session.step.action == "table.merge"
    assert view.settings.title.text() == "Merge with another table"
    QTest.keyClick(view.canvas, Qt.Key.Key_Delete)
    assert not any(s.action == "table.merge" for s in orders.steps)
    qtbot.waitUntil(lambda: not any(e.target.table_id == orders.id for e in view.edges))
    session.undo()
    assert any(s.action == "table.merge" for s in session.project.table("Orders").steps)


def test_adding_a_step_from_a_table_box(window, settle, session, demo, registry):
    view = open_map(window, session, demo, settle)
    customers = session.project.tables[0]
    view.add_step(customers.id, registry.actions["rows.sort"])
    assert session.table is customers and session.step.action == "rows.sort"
    assert view.steps.list.count() == 4

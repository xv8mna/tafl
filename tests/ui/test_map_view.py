from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from tafl.core import Engine, Link, Project
from tafl.ui.map import HEADER, NODE_WIDTH, ROW, Arc, Unit, auto_layout, node_rows


def open_map(window, session, demo, settle):
    window.open_path(demo)
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    return view


def test_links_name_the_step_that_uses_a_table(session, demo):
    session.open_project(demo)
    links = session.engine.links()
    customers, orders, revenue = (t.id for t in session.project.tables)
    merge = session.project.tables[1].steps[3]
    derive = session.project.tables[2].steps[0]
    assert Link(customers, orders, merge.id, 3, True) in links
    assert Link(orders, revenue, derive.id, 0, True) in links
    assert session.engine.dependencies(revenue) == [orders]


def test_tables_are_arranged_in_data_flow_order(registry):
    project = Project()
    a, b, c, d = (project.add_table(name) for name in "ABCD")
    project.add_step(c, registry.actions["table.append"], {"tables": [b.id, a.id]})
    project.add_step(b, registry.actions["table.derive"], {"table": a.id})
    project.add_step(d, registry.actions["table.derive"], {"table": c.id})
    links = Engine(project, registry).links()
    positions = auto_layout([Unit(t.id, 80.0) for t in project.tables], [Arc(x.source, x.target) for x in links])
    assert positions[a.id].x() < positions[b.id].x() < positions[c.id].x() < positions[d.id].x()


def test_map_shows_tables_steps_and_arrows(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    assert window.views.currentWidget() is view
    assert set(view.nodes) == {t.id for t in session.project.tables}
    assert len(view.edges) == 2
    orders = view.nodes[session.project.table("Orders").id]
    assert [(r.number, r.kind) for r in orders.rows] == [
        (1, "step"), (0, "gap"), (4, "link"), (0, "gap"), (9, "export"), (10, "export"),
    ]
    assert orders.rows[2].text == "Lookup Customers on CustomerNo"
    edge = next(e for e in view.edges if e.target is orders)
    assert edge.path().pointAtPercent(1).y() == orders.pos().y() + HEADER + 3 + 2 * ROW + ROW / 2
    assert all(n.status == "ok" for n in view.nodes.values())  # the map calculates every table
    assert orders.info == "156 × 11"


def test_the_map_is_a_workspace(window, settle, session, demo):
    """Steps, settings and data of the selected table are shown next to the map."""
    view = open_map(window, session, demo, settle)
    session.select_table(session.project.table("Orders").id)
    settle(session)
    assert view.steps.list.count() == 10
    assert view.settings.title.text() == "Export to CSV / text file"
    assert view.data.model.rowCount() == 156


def test_double_click_on_a_step_opens_it_in_the_table_view(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    window.resize(1400, 900)
    window.show()
    orders = view.nodes[session.project.table("Orders").id]
    point = orders.mapToScene(QPointF(NODE_WIDTH / 2, HEADER + 3 + 2 * ROW + ROW / 2))  # "Lookup Customers"
    QTest.mouseDClick(view.canvas.viewport(), Qt.MouseButton.LeftButton, pos=view.canvas.mapFromScene(point))
    assert window.views.currentWidget() is window.splitter
    assert session.table.name == "Orders" and session.step.action == "table.merge"
    assert not session.overview


def test_dragging_keeps_the_arrangement_and_can_be_undone(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    node = view.nodes[session.project.table("Customers").id]
    node.setPos(node.pos() + QPointF(0, 300))
    view.save_positions()
    assert session.dirty
    saved = {t.name: t.position for t in session.project.tables}
    assert all(p is not None for p in saved.values())
    assert Project.from_dict(session.project.to_dict()).tables[0].position == saved["Customers"]
    session.undo()
    assert all(t.position is None for t in session.project.tables)
    session.redo()
    view.arrange()
    assert all(t.position is None for t in session.project.tables)


def test_new_table_on_the_map_is_placed_and_selected(window, settle, session, demo, registry):
    view = open_map(window, session, demo, settle)
    view.new_table(registry.actions["excel.import"], QPointF(900, 600))
    assert window.views.currentWidget() is view  # its settings are right here
    assert session.table.position == (900, 600) and session.step.action == "excel.import"
    assert view.settings.title.text() == "Import Excel files"


def test_rows_of_an_empty_table(session):
    table = session.project.add_table("Empty")
    assert [r.text for r in node_rows(session, table, set())] == ["no steps yet"]

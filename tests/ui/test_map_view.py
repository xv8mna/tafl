import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest

from tafl.core import Engine, Link, Project
from tafl.ui.map import HEADER, NODE_WIDTH, ROW, Arc, Unit, auto_layout, node_rows
from tafl.ui.map.view import MapView


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


def test_the_automatic_fit_ends_in_a_wide_low_window(window, settle, session, demo, qapp, monkeypatch):
    """In such a window the height sets the scale, and a scale that followed the scroll bars would
    show the bottom bar, need no bar with the lower room, and so on without end. The limit here
    ends that loop, so that it is a failure and not a window that hangs. Every 40 pixels: the width
    at which this happens depends on the fonts."""
    fits = []
    refit = MapView.refit

    def counted(view):
        fits.append(view.canvas.viewport().size())
        if len(fits) > 300:
            view.auto_fit = False
        refit(view)

    monkeypatch.setattr(MapView, "refit", counted)
    view = open_map(window, session, demo, settle)
    window.show()
    canvas = view.canvas
    for height in (540, 580):
        for width in range(920, 1321, 40):
            fits.clear()
            view.auto_fit = True
            window.resize(width, height)
            for _ in range(3):
                qapp.processEvents()
            assert len(fits) <= 4, (width, height)
            if canvas.transform().m11() > 0.3001:  # at the lower limit of the fit a project can be too big
                seen = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
                assert seen.contains(view.scene.itemsBoundingRect()), (width, height)  # the margin takes the bars


def chain_of_tables(registry, tmp_path, count=12):
    (tmp_path / "a.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    project = Project(name="Chain")
    before = project.add_table("T0")
    project.add_step(before, registry.actions["csv.import"], {"file": "a.csv"})
    for number in range(1, count):
        table = project.add_table(f"T{number}")
        project.add_step(table, registry.actions["table.derive"], {"table": before.id})
        before = table
    return project.save(tmp_path / "chain.tafl")


def test_show_all_stays_when_the_automatic_fit_showed_a_part(window, settle, session, registry, tmp_path, qapp):
    """The bars go when the scale gets smaller, and the automatic fit that the bigger viewport
    starts must not take the scale back to its lower limit."""
    view = open_map(window, session, chain_of_tables(registry, tmp_path), settle)
    window.show()
    window.resize(1366, 728)
    qapp.processEvents()
    canvas = view.canvas
    assert view.auto_fit
    assert not canvas.mapToScene(canvas.viewport().rect()).boundingRect().contains(view.scene.itemsBoundingRect())
    view.fit()
    qapp.processEvents()
    assert canvas.mapToScene(canvas.viewport().rect()).boundingRect().contains(view.scene.itemsBoundingRect())


def test_show_all_stays_in_force_when_the_window_changes_size(window, settle, session, registry, tmp_path, qapp):
    """As the automatic fit before it, until a zoom or a move: also in a smaller window."""
    view = open_map(window, session, chain_of_tables(registry, tmp_path), settle)
    window.show()
    window.resize(1366, 728)
    qapp.processEvents()
    view.fit()
    canvas = view.canvas
    for width, height in ((1000, 600), (1600, 900), (900, 560)):
        window.resize(width, height)
        qapp.processEvents()
        seen = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
        assert seen.contains(view.scene.itemsBoundingRect()), (width, height)


def grouped_chains(registry, tmp_path):
    (tmp_path / "a.csv").write_text("a,b\n1,2\n", encoding="utf-8")
    project = Project(name="Groups")
    for g in range(3):
        group = project.add_group(f"G{g}")
        before = project.add_table(f"G{g}T0")
        project.add_step(before, registry.actions["csv.import"], {"file": "a.csv"})
        before.group = group.id
        for number in range(1, 8):
            table = project.add_table(f"G{g}T{number}")
            project.add_step(table, registry.actions["table.derive"], {"table": before.id})
            table.group = group.id
            before = table
    return project.save(tmp_path / "groups.tafl")


def all_in_view(view):
    canvas = view.canvas
    return canvas.mapToScene(canvas.viewport().rect()).boundingRect().contains(view.scene.itemsBoundingRect())


def test_show_all_stays_in_force_when_groups_close_and_open(window, settle, session, registry, tmp_path, qapp):
    """A map with other boxes gets the scale for them, not the one it had."""
    view = open_map(window, session, grouped_chains(registry, tmp_path), settle)
    window.show()
    window.resize(1366, 728)
    qapp.processEvents()
    assert not all_in_view(view)  # the automatic fit: 30 % at least
    view.fit()
    qapp.processEvents()
    small = view.canvas.transform().m11()
    assert all_in_view(view)
    view.group_bar.collapse_all.click()
    view.rebuild()
    qapp.processEvents()
    assert all_in_view(view) and view.canvas.transform().m11() > small
    view.group_bar.collapse_all.click()  # Expand all
    view.rebuild()
    qapp.processEvents()
    assert all_in_view(view) and view.canvas.transform().m11() == pytest.approx(small)


@pytest.mark.parametrize("move", ["bar", "key"])
def test_a_move_with_a_scroll_bar_or_a_key_ends_show_all(window, settle, session, registry, tmp_path, qapp, move):
    """As a drag of the background does: the next change of the map must not take the view back."""
    view = open_map(window, session, grouped_chains(registry, tmp_path), settle)
    window.show()
    window.resize(1366, 728)
    qapp.processEvents()
    view.fit()
    qapp.processEvents()
    canvas = view.canvas
    across, down = canvas.horizontalScrollBar(), canvas.verticalScrollBar()
    bar = down if down.maximum() > down.value() else across
    assert view.auto_fit == "all" and bar.maximum() > bar.value()
    if move == "bar":
        bar.setSliderPosition(bar.maximum())  # as a drag of the handle
    else:
        canvas.setFocus()
        QTest.keyClick(canvas, Qt.Key.Key_PageDown if bar is down else Qt.Key.Key_Right)
    moved = bar.value()
    assert not view.auto_fit
    session.rename_table(session.project.tables[0].id, "First")
    view.rebuild()
    qapp.processEvents()
    assert bar.value() == moved


def test_a_key_that_cannot_move_the_map_keeps_show_all(window, settle, session, registry, tmp_path, qapp):
    """At the end of the bar (or with no bar), Down and Page Down move nothing."""
    view = open_map(window, session, chain_of_tables(registry, tmp_path, 20), settle)
    window.show()
    window.resize(1366, 728)
    qapp.processEvents()
    view.fit()
    qapp.processEvents()
    canvas = view.canvas
    down = canvas.verticalScrollBar()
    down.setValue(down.maximum())  # not by the user: Show all stays
    assert view.auto_fit == "all"
    seen = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
    canvas.setFocus()
    for key in (Qt.Key.Key_Down, Qt.Key.Key_PageDown):
        QTest.keyClick(canvas, key)
    assert canvas.mapToScene(canvas.viewport().rect()).boundingRect() == seen
    assert view.auto_fit == "all"
    window.resize(1000, 600)
    qapp.processEvents()
    assert all_in_view(view)


@pytest.mark.parametrize("bars",["", "QScrollBar:vertical { width: 30px } QScrollBar:horizontal { height: 30px }"])
def test_show_all_shows_all_of_a_project_at_a_small_scale(window, settle, session, registry, tmp_path, qapp, bars):
    """Below about 18 % the margin around the project is narrower on the screen than half a scroll
    bar, so the scale leaves room for both bars, as wide as a style sheet makes them. At these
    sizes the bars are there."""
    view = open_map(window, session, chain_of_tables(registry, tmp_path), settle)
    window.show()
    canvas = view.canvas
    canvas.setStyleSheet(bars)
    view.auto_fit = False  # as after a zoom: the size of the window doesn't change the view
    for width, height in ((800, 500), (840, 540), (880, 500), (900, 600), (960, 540), (1024, 600)):
        window.resize(width, height)
        qapp.processEvents()
        view.fit()
        qapp.processEvents()
        seen = canvas.mapToScene(canvas.viewport().rect()).boundingRect()
        assert seen.contains(view.scene.itemsBoundingRect()), (width, height)

"""Map features for big projects: groups (bar, keys, collapsing), finding tables, turning tables
off, and a map arranged by hand that stays free of overlaps. Tested on the demo project."""

from PySide6.QtCore import QPointF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog

from tafl.api import Mode
from tafl.ui.map import NODE_WIDTH


def open_map(window, session, demo, settle):
    window.resize(1500, 950)
    window.show()
    window.open_path(demo)
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    return view


def view_center(view) -> QPointF:
    return view.canvas.mapToScene(view.canvas.viewport().rect().center())


def test_the_map_fits_the_window_when_opened(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    assert 0.3 <= view.canvas.transform().m11() <= 1.0
    visible = view.canvas.mapToScene(view.canvas.viewport().rect()).boundingRect()
    assert visible.contains(view.scene.itemsBoundingRect())


def test_a_collapsed_group_is_one_box_with_arrows_to_its_tables(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    assert set(view.nodes) == {revenue.id} and set(view.group_nodes) == {group.id}
    box = view.group_nodes[group.id]
    assert [r.text for r in box.rows] == ["Orders", "⋯ 1 more table"]  # only Orders has an arrow to the outside
    (edge,) = view.edges
    assert edge.source is box and edge.source_key == orders.id
    assert edge.path().pointAtPercent(0) == box.out_anchor(orders.id)
    settle(session)
    view.update_statuses()
    assert box.summary == "all ok"
    assert view.group_bar.chips[group.id].text() == "1  Inputs (2)  ▸"

    view.set_collapsed(group.id, False)
    view.rebuild()
    assert set(view.nodes) == {t.id for t in session.project.tables} and not view.group_nodes
    session.undo()
    assert session.project.group(group.id).collapsed


def test_collapsing_a_group_on_a_map_arranged_by_hand(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    view.rebuild()
    view.save_positions()  # the map is arranged by hand from now on
    before = {t: QPointF(view.nodes[t].pos()) for t in (customers.id, orders.id)}
    view.set_collapsed(group.id, True)
    view.rebuild()
    box = view.group_nodes[group.id]
    assert box.pos() == QPointF(min(p.x() for p in before.values()), min(p.y() for p in before.values()))

    view.box_pressed(box)
    box.setPos(box.pos() + QPointF(0, 400))
    view.box_released(box, moved=True)
    view.set_collapsed(group.id, False)
    view.rebuild()
    for table_id, point in before.items():  # the tables come back where the box is now (whole pixels)
        moved = view.nodes[table_id].pos() - point
        assert abs(moved.x()) <= 1 and abs(moved.y() - 400) <= 1
    assert not view.frames[group.id].rect.intersects(view.nodes[revenue.id].box_rect())


def test_number_keys_and_the_group_bar_jump_to_groups(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    inputs = session.add_group("Inputs", [customers.id, orders.id])
    reports = session.add_group("Reports", [revenue.id])
    view.rebuild()
    assert [c.text() for c in view.group_bar.chips.values()] == ["1  Inputs (2)", "2  Reports (1)"]
    QTest.keyClick(view.canvas, Qt.Key.Key_2)
    assert view.frames[reports.id].rect.contains(view_center(view)) and not view.auto_fit
    view.group_bar.chips[inputs.id].click()
    assert view.frames[inputs.id].rect.contains(view_center(view))
    QTest.keyClick(view.canvas, Qt.Key.Key_0)  # everything
    visible = view.canvas.mapToScene(view.canvas.viewport().rect()).boundingRect()
    assert visible.contains(view.scene.itemsBoundingRect())


def test_ctrl_g_groups_the_selected_tables(window, settle, session, demo, monkeypatch):
    view = open_map(window, session, demo, settle)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Sources", True))
    customers, orders, _ = session.project.tables
    view.nodes[customers.id].setSelected(True)
    view.nodes[orders.id].setSelected(True)
    QTest.keyClick(view.canvas, Qt.Key.Key_G, Qt.KeyboardModifier.ControlModifier)
    (group,) = session.project.groups
    assert group.name == "Sources" and [t.name for t in session.project.members(group.id)] == ["Customers", "Orders"]


def test_collapse_all_and_expand_all(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    session.add_group("Inputs", [customers.id, orders.id])
    session.add_group("Reports", [revenue.id])
    view.rebuild()
    view.group_bar.collapse_all.click()
    view.rebuild()
    assert all(g.collapsed for g in session.project.groups) and not view.nodes
    assert view.group_bar.collapse_all.text() == "Expand all"
    view.group_bar.collapse_all.click()
    view.rebuild()
    assert not any(g.collapsed for g in session.project.groups) and len(view.nodes) == 3


def test_a_table_dropped_into_a_group_gets_a_free_spot(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id])
    view.rebuild()
    first = view.nodes[customers.id]
    view.nodes[revenue.id].setPos(first.pos() + QPointF(0, first.height + 60))  # right below the group
    view.save_positions()
    view.rebuild()

    node = view.nodes[orders.id]
    view.box_pressed(node)
    node.setPos(view.nodes[customers.id].pos() + QPointF(10, 10))  # on top of Customers
    view.box_released(node, moved=True)
    assert session.project.table(orders.id).group == group.id
    view.rebuild()
    frame = view.frames[group.id]
    a, b = view.nodes[customers.id].box_rect(), view.nodes[orders.id].box_rect()
    assert b.top() > a.bottom()  # below Customers, not on it
    assert not frame.rect.intersects(view.nodes[revenue.id].box_rect())  # Revenue made room
    session.undo()
    assert session.project.table(orders.id).group == ""


def test_menu_join_and_leave_on_a_map_arranged_by_hand(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id])
    view.rebuild()
    view.save_positions()
    view.join_group([revenue.id], group.id)
    view.rebuild()
    assert view.nodes[revenue.id].pos().x() == view.nodes[customers.id].pos().x()  # below Customers
    assert view.nodes[revenue.id].pos().y() > view.nodes[customers.id].box_rect().bottom()
    view.leave_group([revenue.id])
    view.rebuild()
    assert session.project.table(revenue.id).group == ""
    assert not view.frames[group.id].rect.intersects(view.nodes[revenue.id].box_rect())


def test_dropping_a_table_on_a_collapsed_group_adds_it(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    node = view.nodes[revenue.id]
    view.box_pressed(node)
    node.setPos(view.group_nodes[group.id].pos())
    view.box_released(node, moved=True)
    view.rebuild()
    assert session.project.table(revenue.id).group == group.id and not view.nodes
    assert session.project.table(revenue.id).position is None  # it goes below the others when expanded


def test_find_a_table_in_a_collapsed_group(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, _ = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    QTest.keyClick(view.canvas, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    view.search.setText("customer")
    assert view._search_hits == [customers.id, session.project.tables[2].id]  # also "Revenue by customer"
    view.search.setText("customers")
    assert view._search_hits == [customers.id]
    view.find_next()
    view.rebuild()
    assert not session.project.group(group.id).collapsed
    assert session.table_id == customers.id
    node = view.nodes[customers.id]
    assert node.isSelected() and node.search_match
    visible = view.canvas.mapToScene(view.canvas.viewport().rect()).boundingRect()
    assert visible.contains(node.box_rect())


def test_turned_off_tables_are_shown_and_skipped(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    session.set_table_enabled([orders.id], False)
    settle(session)
    view.rebuild()
    assert [view.nodes[t.id].info for t in (orders, revenue)] == ["off", "skipped"]
    assert view.nodes[customers.id].status == "ok"
    session.select_table(revenue.id)
    settle(session)
    assert "is not calculated: it uses “Orders”, which is turned off" in view.data.message.text()
    item = next(i for i in window.tables_panel._table_items() if i.text(0) == "Orders")
    assert item.font(0).italic()

    results = session.engine.run(mode=Mode.PREVIEW)
    window.run_panel.show_results(results)
    assert ", 2 skipped, " in window.run_panel.summary.text()  # one is off, one uses it: both skipped
    assert "no errors" in window.run_panel.summary.text()

    group = session.add_group("Everything", [t.id for t in session.project.tables])
    session.set_table_enabled([t.id for t in session.project.members(group.id)], True)
    settle(session)
    assert [session.table_status(t.id) for t in session.project.tables] == ["ok", "ok", "ok"]


def test_arrows_going_backwards_go_around_the_boxes(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, _ = session.project.tables
    view.nodes[customers.id].setPos(view.nodes[orders.id].pos() + QPointF(NODE_WIDTH + 200, 0))
    view.save_positions()
    view.rebuild()
    edge = next(e for e in view.edges if e.link.source == customers.id)
    boxes = [view.nodes[t].box_rect().adjusted(4, 4, -4, -4) for t in (customers.id, orders.id)]
    path = edge.path()
    points = [path.pointAtPercent(i / 200) for i in range(201)]
    assert not any(box.contains(point) for box in boxes for point in points)

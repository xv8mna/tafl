from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractItemView

from tafl.core import Engine, Project
from tafl.ui.map import GROUP_TITLE, NODE_WIDTH, Arc, Unit, auto_layout


def test_groups_are_saved_with_the_project(tmp_path):
    project = Project()
    sources = project.add_group("Sources", "green")
    a = project.add_table("A")
    project.add_table("B")
    a.group = sources.id
    loaded = Project.load(project.save(tmp_path / "p.tafl"))
    assert [(g.id, g.name, g.color) for g in loaded.groups] == [("g1", "Sources", "green")]
    assert [t.group for t in loaded.tables] == ["g1", ""]
    data = project.to_dict()
    data["tables"][1]["group"] = "g99"  # a group that doesn't exist
    assert Project.from_dict(data).tables[1].group == ""


def test_group_operations_can_be_undone(session):
    a, b, c = (session.project.add_table(n) for n in "ABC")
    sources = session.add_group("Sources", [a.id, b.id])
    other = session.add_group("Other", [c.id])
    assert (sources.color, other.color) == ("blue", "green")  # each group gets its own color
    session.rename_group(sources.id, "Inputs")
    session.set_group_color(sources.id, "orange")
    session.set_table_group([c.id], sources.id)
    assert [t.group for t in session.project.tables] == [sources.id] * 3
    session.delete_group(sources.id)
    assert [t.group for t in session.project.tables] == ["", "", ""]
    assert [g.name for g in session.project.groups] == ["Other"]
    session.undo()
    restored = session.project.group(sources.id)
    assert restored.name == "Inputs" and restored.color == "orange"
    assert all(t.group == sources.id for t in session.project.tables)


def test_place_table_moves_into_groups_and_reorders(session):
    a, b, c, d = (session.project.add_table(n) for n in "ABCD")
    group = session.add_group("G", [b.id])
    session.place_table(d.id, group.id)  # end of the group: right after B
    assert [t.name for t in session.project.tables] == ["A", "B", "D", "C"]
    assert d.group == group.id
    session.place_table(a.id, None, after=c.id)
    assert [t.name for t in session.project.tables] == ["B", "D", "C", "A"]
    session.place_table(c.id, group.id, before=b.id)
    assert [t.name for t in session.project.tables] == ["C", "B", "D", "A"] and c.group == group.id


def test_layout_keeps_each_group_in_its_own_band(registry):
    project = Project()
    group = project.add_group("G")
    a, b, c = project.add_table("A"), project.add_table("B"), project.add_table("C")
    a.group = b.group = group.id
    project.add_step(b, registry.actions["table.derive"], {"table": a.id})
    project.add_step(c, registry.actions["table.derive"], {"table": a.id})  # C is in B's column
    units = [Unit(t.id, 80.0, t.group) for t in project.tables]
    positions = auto_layout(units, [Arc(link.source, link.target) for link in Engine(project, registry).links()])
    members = [QRectF(positions[t].x(), positions[t].y(), NODE_WIDTH, 80) for t in (a.id, b.id)]
    frame = members[0].united(members[1]).adjusted(-14, -14 - GROUP_TITLE, 14, 14)
    outsider = QRectF(positions[c.id].x(), positions[c.id].y(), NODE_WIDTH, 80)
    assert positions[b.id].x() == positions[c.id].x() and not frame.intersects(outsider)


def test_map_draws_a_frame_around_each_group(window, settle, session, demo):
    window.open_path(demo)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    frame = view.frames[group.id]
    assert [m.table_id for m in frame.members] == [customers.id, orders.id]
    for member in (customers, orders):
        assert frame.rect.contains(view.nodes[member.id].sceneBoundingRect().center())
    assert not frame.rect.intersects(view.nodes[revenue.id].sceneBoundingRect())


def test_dragging_a_table_out_of_and_into_a_group(qtbot, window, settle, session, demo):
    window.open_path(demo)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()

    node = view.nodes[customers.id]
    view.box_pressed(node)
    node.setPos(node.pos() + QPointF(0, 900))  # far below the frame
    view.box_released(node, moved=True)
    qtbot.waitUntil(lambda: customers.group == "")
    assert customers.position is not None  # the arrangement is kept

    view.rebuild()
    node = view.nodes[revenue.id]
    view.box_pressed(node)
    node.setPos(view.frames[group.id].rect.center() - QPointF(NODE_WIDTH / 2, 20))
    view.box_released(node, moved=True)
    qtbot.waitUntil(lambda: revenue.group == group.id)
    session.undo()
    assert session.project.table(revenue.id).group == ""


def test_dragging_a_group_title_moves_its_tables(qtbot, window, settle, session, demo):
    window.open_path(demo)
    customers, orders, _ = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    window.show_map(True)
    window.resize(1500, 950)
    window.show()
    settle(session)
    view = window.map_view
    view.rebuild()
    view.fit()
    before = {t: QPointF(view.nodes[t].pos()) for t in (customers.id, orders.id)}
    title = view.frames[group.id].title_rect().center()
    viewport = view.canvas.viewport()
    start = view.canvas.mapFromScene(title)
    end = view.canvas.mapFromScene(title + QPointF(0, 120))
    QTest.mousePress(viewport, Qt.MouseButton.LeftButton, pos=start)
    QTest.mouseMove(viewport, pos=end)
    QTest.mouseRelease(viewport, Qt.MouseButton.LeftButton, pos=end)
    qtbot.waitUntil(lambda: session.project.tables[0].position is not None)
    moved = [session.project.table(t).position[1] - before[t].y() for t in before]
    assert abs(moved[0] - moved[1]) <= 1 and moved[0] > 50  # positions are saved as whole pixels


def test_tables_view_shows_groups_as_folders(window, session, demo):
    window.open_path(demo)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id])
    tree = window.tables_panel.tree
    assert tree.topLevelItemCount() == 3  # the group, Orders, Revenue by customer
    folder = tree.topLevelItem(0)
    assert folder.text(0) == "Inputs" and folder.child(0).text(0) == "Customers"
    panel = window.tables_panel
    below = QAbstractItemView.DropIndicatorPosition.BelowItem
    assert panel.drop_place(folder, below) == (group.id, None, None)
    assert panel.drop_place(folder.child(0), below) == (group.id, None, customers.id)
    assert panel.drop_place(tree.topLevelItem(1), QAbstractItemView.DropIndicatorPosition.AboveItem) == (None, orders.id, None)
    assert panel.drop_place(None, below) == (None, None, None)


def test_joining_a_group_from_the_table_list_moves_the_table_to_it(session):
    a, b = session.project.add_table("A"), session.project.add_table("B")
    a.position, b.position = (0, 0), (900, 500)
    group = session.add_group("G", [a.id], positions={a.id: (0, 0)})
    assert a.position == (0, 0)  # placed by the map: kept
    session.set_table_group([b.id], group.id)
    assert b.position is None  # the map puts it below A
    b.position = (0, 120)
    session.set_table_group([b.id], None)
    assert b.position == (0, 120)  # leaving: the map only moves it out of the frame
    session.add_group("H", [a.id])
    assert a.position is None  # a group made in the table list is put together on the map


def test_collapsing_groups_is_one_undo_step(session):
    a, b = session.project.add_table("A"), session.project.add_table("B")
    g = session.add_group("G", [a.id])
    h = session.add_group("H", [b.id])
    session.set_groups_collapsed([g.id, h.id], True, {a.id: (10, 20)}, {g.id: (10, 20)})
    assert g.collapsed and h.collapsed and g.position == (10, 20) and a.position == (10, 20)
    assert Project.from_dict(session.project.to_dict()).group(g.id).position == (10, 20)
    session.undo()
    assert not any(x.collapsed for x in session.project.groups)
    session.redo()
    session.set_groups_collapsed([g.id], False)
    assert session.project.group(g.id).position is None and session.project.group(h.id).collapsed

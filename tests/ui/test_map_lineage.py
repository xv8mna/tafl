"""The map's highlight of what the selected table uses and what uses it (key H)."""

import importlib.util
import time
from pathlib import Path

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QMenu

from tafl.core import Link
from tafl.ui import theme
from tafl.ui.map import Lineage, MapView, lineage_of
from tafl.ui.map import view as map_view
from tafl.ui.map.items import _DIMMED

ROOT = Path(__file__).resolve().parents[2]


def link(source: str, target: str, enabled: bool = True) -> Link:
    return Link(source, target, f"{target}-{source}", 0, enabled)


def test_lineage_of_a_chain_and_a_diamond():
    chain = [link("A", "B"), link("B", "C"), link("C", "D")]
    assert lineage_of(chain, "B") == ({"A"}, {"C", "D"})
    assert lineage_of(chain, "D") == ({"A", "B", "C"}, set())
    assert lineage_of(chain, "X") == (set(), set())
    diamond = [link("A", "B"), link("A", "C"), link("B", "D"), link("C", "D"), link("E", "C")]
    assert lineage_of(diamond, "D") == ({"A", "B", "C", "E"}, set())
    assert lineage_of(diamond, "A") == (set(), {"B", "C", "D"})
    assert lineage_of(diamond, "C") == ({"A", "E"}, {"D"})  # B is a sibling: neither


def test_lineage_ignores_steps_that_are_off_and_survives_cycles():
    assert lineage_of([link("A", "B", enabled=False), link("B", "C")], "C") == ({"B"}, set())
    cycle = [link("A", "B"), link("B", "C"), link("C", "A")]
    assert lineage_of(cycle, "A") == ({"B", "C"}, {"B", "C"})  # never the table itself
    assert lineage_of([link("A", "A")], "A") == (set(), set())


def open_map(window, session, demo, settle):
    window.open_path(demo)
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    return view


def press(view: MapView, key: Qt.Key) -> None:
    QTest.keyClick(view.canvas, key)


def edge(view: MapView, source: str, target: str):
    return next(e for e in view.edges if (e.link.source, e.link.target) == (source, target))


def test_h_shows_what_a_table_uses_and_what_uses_it(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    messages = []
    view.message.connect(messages.append)
    view.message_update.connect(lambda shown, new: messages.append((shown, new)))
    status = window.statusBar()
    session.select_table(orders.id)
    press(view, Qt.Key.Key_H)
    assert view.lineage == Lineage(orders.id, frozenset({customers.id}), frozenset({revenue.id}))
    assert messages[-1] == "“Orders” uses 1 table and is used by 1 · H or Esc ends the highlight"
    assert status.currentMessage() == messages[-1]
    assert [view.lineage_role(t.id) for t in session.project.tables] == ["uses", "table", "used_by"]
    assert view.lineage_button.isChecked()

    press(view, Qt.Key.Key_H)  # again: off
    assert view.lineage is None and view.lineage_role(orders.id) is None
    assert not view.lineage_button.isChecked()
    assert status.currentMessage() == ""  # the status bar no longer says how to end it
    count = len(messages)
    view.set_lineage(False)  # off already: nothing to say
    assert len(messages) == count

    press(view, Qt.Key.Key_H)
    press(view, Qt.Key.Key_Escape)  # Esc ends it too
    assert view.lineage is None

    session.select_table(revenue.id)
    view.toggle_lineage()
    assert view.lineage.uses == {customers.id, orders.id} and not view.lineage.used_by
    assert messages[-1] == "“Revenue by customer” uses 2 tables and is used by 0 · H or Esc ends the highlight"


def test_esc_in_the_search_field_ends_the_highlight_too(window, settle, session, demo, qtbot):
    window.show()
    view = open_map(window, session, demo, settle)
    session.select_table(session.project.tables[1].id)
    view.set_lineage(True)
    window.activateWindow()
    QTest.keyClick(view.canvas, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    qtbot.waitUntil(view.search.hasFocus, timeout=2000)
    QTest.keyClicks(view.search, "rev")
    QTest.keyClick(view.search, Qt.Key.Key_Escape)
    assert view.lineage is None and not view.lineage_button.isChecked()
    assert view.search.text() == "" and view.canvas.hasFocus()


def test_the_highlight_dims_tables_that_are_off_as_well(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    other = session.add_derived_table(customers.id)
    session.set_table_enabled([other.id], False)  # a table that is off, outside the highlight
    settle(session)
    view.rebuild()
    node = view.nodes[other.id]
    assert node.status == "disabled"

    def drawn_opacity() -> float:  # the opacity the box is drawn with, as the scene sets it up
        image = QImage(400, 400, QImage.Format.Format_ARGB32)
        painter = QPainter(image)
        painter.setOpacity(node.opacity())
        node.paint(painter, None)
        opacity = painter.opacity()
        painter.end()
        return opacity

    assert drawn_opacity() == 0.5  # off: faded
    session.select_table(orders.id)
    view.set_lineage(True)
    assert view.lineage_role(other.id) == "" and drawn_opacity() == 0.5 * _DIMMED  # fainter than the dimmed tables


def test_the_highlight_follows_the_selection_and_ends_with_the_project(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    messages = []
    view.message.connect(messages.append)
    session.select_table(orders.id)
    view.set_lineage(True)
    session.select_table(customers.id)
    assert view.lineage.table == customers.id and view.lineage.used_by == {orders.id, revenue.id}
    assert messages[-1].startswith("“Customers” uses 0 tables and is used by 2")
    session.select_step(customers.steps[1].id)  # the same table: nothing new to say
    assert len([m for m in messages if m.startswith("“Customers”")]) == 1

    session.select_table(None)  # no table: off
    assert view.lineage is None
    assert not view.lineage_button.isEnabled()
    view.set_lineage(True)
    assert view.lineage is None and messages[-1] == "Select a table first"

    session.select_table(orders.id)
    assert view.lineage_button.isEnabled()
    view.set_lineage(True)
    session.open_project(demo)  # another project: off
    assert view.lineage is None


def test_turning_a_step_off_changes_the_highlight(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    session.select_table(revenue.id)
    view.set_lineage(True)
    merge = orders.steps[3]
    session.set_enabled(merge.id, False)  # Orders no longer uses Customers
    assert view.lineage.uses == {orders.id}
    assert view.lineage_role(customers.id) == ""
    session.undo()
    view.rebuild()
    assert view.lineage.uses == {customers.id, orders.id}  # recomputed after a rebuild


def test_boxes_and_arrows_get_the_colours_of_the_highlight(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    other = session.add_derived_table(customers.id)  # uses Customers, unrelated to Orders
    view.rebuild()
    session.select_table(orders.id)
    view.set_lineage(True)
    assert view.lineage_role(other.id) == ""
    assert view.nodes[other.id].opacity() == _DIMMED
    assert all(view.nodes[t.id].opacity() == 1.0 for t in (customers, orders, revenue))

    into, out_of, aside = edge(view, customers.id, orders.id), edge(view, orders.id, revenue.id), edge(view, customers.id, other.id)
    assert into.lineage_role() == "uses" and into.pen().color().name() == theme.MAP_USES
    assert out_of.lineage_role() == "used_by" and out_of.pen().color().name() == theme.MAP_USED_BY
    assert into.pen().widthF() == 2.2 and into.zValue() > aside.zValue()
    assert aside.lineage_role() == "" and aside.opacity() == _DIMMED
    render(view)

    session.select_table(revenue.id)  # Customers → Orders is between two tables it uses
    assert edge(view, customers.id, orders.id).lineage_role() == "uses"
    session.select_table(customers.id)
    assert edge(view, orders.id, revenue.id).lineage_role() == "used_by"
    assert edge(view, customers.id, other.id).lineage_role() == "used_by"

    view.set_lineage(False)  # as before: everything at full strength, the selected table's arrows lit
    assert all(e.opacity() == 1.0 and e.lineage_role() is None for e in view.edges)
    assert all(n.opacity() == 1.0 for n in view.nodes.values())
    assert edge(view, customers.id, orders.id).pen().color().name() == theme.ACCENT


def test_a_collapsed_group_shows_its_members_in_the_highlight(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    other = session.add_derived_table(revenue.id)
    group = session.add_group("Inputs", [customers.id, orders.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    box = view.group_nodes[group.id]
    session.select_table(revenue.id)
    view.set_lineage(True)
    assert view.lineage.uses == {customers.id, orders.id}  # also through the arrow inside the group
    assert box.opacity() == 1.0 and view.nodes[other.id].opacity() == 1.0
    render(view)

    lone = session.add_table(session.registry.actions["csv.import"])  # selected, uses nothing
    view.rebuild()
    assert view.lineage.table == lone.id and view.lineage_role(customers.id) == ""
    assert view.group_nodes[group.id].opacity() == _DIMMED  # none of its tables has a part in it
    render(view)


def test_the_context_menu_entry(window, settle, session, demo, monkeypatch):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    shown = []

    class Menu(QMenu):
        def exec(self, *args):  # instead of showing it: choose the entry
            entry = next(a for a in self.actions() if a.text().startswith("Show what it uses"))
            shown.append((entry.text(), entry.isCheckable(), entry.isChecked()))
            entry.trigger()

    monkeypatch.setattr(map_view, "QMenu", Menu)
    session.select_table(customers.id)
    view.node_menu(view.nodes[orders.id], QPoint())  # on for Orders (it gets selected)
    assert shown[-1] == ("Show what it uses and what uses it\tH", True, False)
    assert session.table_id == orders.id and view.lineage.table == orders.id
    view.node_menu(view.nodes[orders.id], QPoint())  # checked: off
    assert shown[-1][2] is True and view.lineage is None
    view.set_lineage(True)
    view.node_menu(view.nodes[revenue.id], QPoint())  # on for another table: it moves there
    assert shown[-1][2] is False and view.lineage.table == revenue.id


def test_the_button_above_the_map(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    button = view.lineage_button
    assert button.isCheckable() and button.isEnabled() and not button.isChecked()
    button.click()
    assert view.lineage is not None and button.isChecked()
    button.click()
    assert view.lineage is None and not button.isChecked()
    assert "(H)" in button.toolTip()
    # In the title row beside the hint, not in the group bar: the chips keep their room
    assert view.group_bar.findChildren(type(button)) == [] and not view.group_bar.isAncestorOf(button)


def test_the_button_says_less_in_a_narrow_window(window, settle, session, demo, qtbot):
    view = open_map(window, session, demo, settle)
    window.resize(1500, 900)
    window.show()
    qtbot.waitUntil(lambda: view.lineage_button.text() == "Uses / used by")
    window.resize(window.minimumSizeHint().width(), 900)
    qtbot.waitUntil(lambda: view.lineage_button.text() == "⇄")
    window.resize(1500, 900)
    qtbot.waitUntil(lambda: view.lineage_button.text() == "Uses / used by")
    window.hide()
    assert view.lineage_button.minimumSizeHint().width() == 0  # it never makes the map wider
    header = view.layout().itemAt(0).layout()
    view.lineage_button.hide()
    without = header.minimumSize().width()
    view.lineage_button.show()
    assert header.minimumSize().width() <= without + header.spacing()


def test_the_button_does_nothing_while_it_is_not_drawn(window, settle, session, demo):
    """Narrower than its short text (a narrow map beside the Find panel), it isn't drawn: then
    a click and the tooltip go to the map behind it, and Tab skips it."""
    view = open_map(window, session, demo, settle)
    window.resize(1500, 900)
    window.show()
    button = view.lineage_button
    focus = button.focusPolicy()
    middle = button.mapTo(window, button.rect().center())

    def click() -> None:  # as a real click: to the widget at that place in the window
        QTest.mouseClick(window.windowHandle(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, middle)

    button.resize(button._width("⇄") - 1, button.height())
    assert not button.drawn() and view.childAt(button.mapTo(view, button.rect().center())) is not button
    assert button.focusPolicy() == Qt.FocusPolicy.NoFocus
    click()
    assert view.lineage is None
    button.resize(button.sizeHint())
    assert button.drawn() and button.focusPolicy() == focus
    click()
    assert view.lineage is not None
    window.hide()


def test_the_map_gets_the_focus_when_the_button_is_no_longer_drawn(window, settle, session, demo, qtbot):
    """The button has the keyboard focus (Tab) when a narrower window hides it: then the map has
    it, so that H and 1–9 keep working. Before, no widget had the focus."""
    view = open_map(window, session, demo, settle)
    session.select_table(session.project.table("Orders").id)
    window.resize(1500, 900)
    window.show()
    window.activateWindow()
    button = view.lineage_button
    button.setFocus(Qt.FocusReason.TabFocusReason)
    qtbot.waitUntil(lambda: QApplication.focusWidget() is button, timeout=2000)
    button.resize(button._width("⇄") - 1, button.height())
    assert not button.drawn() and QApplication.focusWidget() is view.canvas
    QTest.keyClick(QApplication.focusWidget(), Qt.Key.Key_H)
    assert view.lineage is not None and view.lineage.table == session.project.table("Orders").id
    window.hide()


def render(view: MapView) -> None:
    image = QImage(1600, 1200, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    view.scene.render(painter)
    painter.end()


def _best(fn, n: int = 5) -> float:
    times = []
    for _ in range(n):
        start = time.perf_counter()
        fn()
        times.append(time.perf_counter() - start)
    return min(times)


def test_speed_with_a_big_project(window, session, tmp_path):
    spec = importlib.util.spec_from_file_location("make_big_project", ROOT / "tools" / "make_big_project.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    window.open_path(module.build_project().save(tmp_path / "big.tafl"))
    window.show_map(True)
    view = window.map_view
    assert _best(view.rebuild) < 0.5  # about 30 ms here
    links = session.engine.links()
    busiest = max(session.project.tables, key=lambda t: sum(t.id in (x.source, x.target) for x in links))
    session.select_table(busiest.id)

    def on_and_off():
        view.set_lineage(True)
        view.set_lineage(False)

    assert _best(on_and_off) < 0.15  # about 6 ms here
    session.set_groups_collapsed([g.id for g in session.project.groups], True)
    assert _best(view.rebuild) < 0.5
    assert any(box.footer for box in view.group_nodes.values())


def test_turning_a_step_on_or_off_redraws_its_arrow_and_row(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    merge = orders.steps[3]
    session.select_table(revenue.id)
    view.set_lineage(True)
    arrow = edge(view, customers.id, orders.id)
    session.set_enabled(merge.id, False)  # as the arrow's menu does
    assert not view._rebuild_timer.isActive() and edge(view, customers.id, orders.id) is arrow  # no new map
    assert not arrow.link.enabled and arrow.pen().style() == Qt.PenStyle.DashLine and "(step is off)" in arrow.toolTip()
    assert arrow.lineage_role() == "" and arrow.opacity() == _DIMMED  # it leaves the highlight
    row = next(index for index, row in enumerate(view.nodes[orders.id].rows) if row.key == merge.id)
    assert view.nodes[orders.id].dimmed(row)
    assert view.lineage_role(customers.id) == "" and view.nodes[customers.id].opacity() == _DIMMED
    session.set_enabled(merge.id, True)
    assert arrow.link.enabled and arrow.pen().style() == Qt.PenStyle.SolidLine and "(step is off)" not in arrow.toolTip()
    assert arrow.lineage_role() == "uses" and arrow.pen().color().name() == theme.MAP_USES
    assert not view.nodes[orders.id].dimmed(row) and view.lineage_role(customers.id) == "uses"
    # A new summary: the row says it at once
    session.set_params(merge.id, {"columns": ["Name"]})
    assert view.nodes[orders.id].rows[row].text == session.describe(merge)
    assert not view._rebuild_timer.isActive()
    # Another table to use: a new arrow
    other = session.add_derived_table(customers.id)
    view.rebuild()
    session.set_params(merge.id, {"other": other.id})
    assert view._rebuild_timer.isActive()
    view.rebuild()
    assert edge(view, other.id, orders.id).link.step == merge.id
    assert not any((e.link.source, e.link.target) == (customers.id, orders.id) for e in view.edges)


def test_the_message_of_the_highlight_ends_with_it(window, settle, session, demo, qtbot):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    window.resize(1500, 900)
    window.show()
    session.select_table(orders.id)
    view.set_lineage(True)
    status = window.statusBar()
    assert status.currentMessage() == "“Orders” uses 1 table and is used by 1 · H or Esc ends the highlight"
    session.rename_table(orders.id, "All orders")  # the name as it is now
    view.rebuild()
    assert status.currentMessage().startswith("“All orders” uses 1 table")
    window.show_map(False)  # in the tables view H and Esc don't end it
    assert status.currentMessage() == "" and view.lineage is not None
    window.show_map(True)
    assert status.currentMessage().startswith("“All orders” uses 1 table")
    session.new_project()  # the highlight ends
    assert view.lineage is None and status.currentMessage() == ""
    window.hide()


def test_the_message_of_the_highlight_leaves_other_messages_alone(window, settle, session, demo):
    """It is shown when the highlight starts or moves to another table; other messages stay
    their time, also when the highlight changes with an edit or the view changes."""
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    window.resize(1500, 900)
    window.show()
    status = window.statusBar()
    session.select_table(orders.id)
    view.set_lineage(True)
    view.add_link(revenue.id, orders.id, "lookup")  # Orders uses Revenue now: a circular reference
    added = status.currentMessage()
    assert added.startswith("Added step 9 to “Orders”")
    view.rebuild()
    assert view.lineage.uses == {customers.id, revenue.id} and status.currentMessage() == added
    view.delete_steps([session.step_id])
    deleted = "Deleted step 9 of “Orders” (Ctrl+Z brings it back)"
    view.rebuild()
    assert view.lineage.uses == {customers.id} and status.currentMessage() == deleted
    window.show_map(False)
    assert status.currentMessage() == deleted
    window.show_map(True)
    assert status.currentMessage() == deleted
    status.clearMessage()  # nothing else to say: the map says again what it shows
    window.show_map(False)
    window.show_map(True)
    assert status.currentMessage() == "“Orders” uses 1 table and is used by 1 · H or Esc ends the highlight"
    session.select_table(customers.id)  # another table: the highlight says what it shows now
    assert status.currentMessage().startswith("“Customers” uses 0 tables and is used by 2")
    window.hide()


def test_a_circular_reference_has_a_colour_of_its_own(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    messages = []
    view.message.connect(messages.append)
    session.add_link(revenue.id, customers.id, "lookup")  # Customers uses Revenue, which uses it
    view.rebuild()
    session.select_table(customers.id)
    view.set_lineage(True)
    assert view.lineage.uses == view.lineage.used_by == {orders.id, revenue.id}
    assert [view.lineage_role(t.id) for t in (customers, orders, revenue)] == ["table", "both", "both"]
    assert messages[-1] == (
        "“Customers” uses 2 tables and is used by 2: a circular reference with 2 of them · H or Esc ends the highlight"
    )
    between = edge(view, orders.id, revenue.id)
    assert between.lineage_role() == "both" and between.pen().color().name() == theme.MAP_CYCLE
    assert edge(view, revenue.id, customers.id).lineage_role() == "uses"
    assert edge(view, customers.id, orders.id).lineage_role() == "used_by"
    render(view)


def test_highlighted_tables_hidden_in_a_collapsed_group_are_marked(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    hidden = session.add_derived_table(customers.id)  # in the group, no arrow out of it
    group = session.add_group("Inputs", [customers.id, orders.id, hidden.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    box = view.group_nodes[group.id]
    assert [row.text for row in box.rows] == ["Orders", "⋯ 2 more tables"]
    session.select_table(revenue.id)
    view.set_lineage(True)
    assert box.hidden_roles() == [(customers.id, "uses")]
    session.select_table(customers.id)
    assert box.hidden_roles() == [(customers.id, "table"), (hidden.id, "used_by")]
    render(view)
    view.set_lineage(False)
    assert box.hidden_roles() == []

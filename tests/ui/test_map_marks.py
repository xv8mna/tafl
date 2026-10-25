"""The marks on the map's rows ("in": reads from outside the project, "out": writes or changes
something outside) and the counts on collapsed groups ("3 in · 2 out")."""

import types

from PySide6.QtGui import QImage, QPainter

from tafl.api import P, action
from tafl.core import Registry
from tafl.ui import theme
from tafl.ui.map import ROW, node_height, node_rows
from tafl.ui.map.items import counts_text, group_counts
from tafl.ui.session import Session


def open_map(window, session, demo, settle):
    window.open_path(demo)
    window.show_map(True)
    settle(session)
    view = window.map_view
    view.rebuild()
    return view


def marks(rows):
    return [(row.number, row.kind, row.mark) for row in rows]


def render(view) -> None:
    """Paint the whole map once (the marks, footers and bars are drawn without errors)."""
    image = QImage(1600, 1200, QImage.Format.Format_ARGB32)
    painter = QPainter(image)
    view.scene.render(painter)
    painter.end()


def test_imports_and_exports_have_marks(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = (view.nodes[t.id] for t in session.project.tables)
    assert marks(customers.rows) == [(1, "step", "in"), (0, "gap", "")]
    assert marks(orders.rows) == [
        (1, "step", "in"), (0, "gap", ""), (4, "link", ""), (0, "gap", ""), (9, "export", "out"), (10, "export", "out"),
    ]
    assert marks(revenue.rows)[0] == (1, "link", "")  # table.derive reads another table, not a file
    assert marks(revenue.rows)[-1] == (5, "export", "out")
    render(view)


def test_a_file_command_is_marked_out(session, demo, registry):
    session.open_project(demo)
    orders = session.project.tables[1]
    session.project.add_step(orders, registry.actions["files.move"], {"which": "column", "column": "Name", "target": "archive"})
    session.project.add_step(orders, registry.actions["files.mkdir"], {"folder": "archive"})
    rows = node_rows(session, orders, set())
    assert marks(rows)[-2:] == [(11, "export", "out"), (12, "export", "out")]


def _plugins() -> Registry:
    module = types.ModuleType("outside")

    @action("demo.generate", "Generate rows", kind="source", params=[P.Int("rows", "Rows", default=3)])
    def generate(df, p, ctx):
        return df

    @action("demo.copy", "Copy a table", kind="source", params=[P.TableRef("table", "Table")])
    def copy(df, p, ctx):
        return df

    @action("demo.look_up", "Look up in a file", params=[P.File("file", "File")])
    def look_up(df, p, ctx):
        return df

    @action("demo.upper", "Upper case", params=[P.Column("column", "Column")])
    def upper(df, p, ctx):
        return df

    module.generate, module.copy, module.look_up, module.upper = generate, copy, look_up, upper
    registry = Registry()
    registry.load_builtin()
    registry.add_module(module)
    return registry


def test_plugin_actions_get_marks_by_what_they_do(qapp):
    session = Session(_plugins())
    try:
        project = session.project
        generated = project.add_table("Generated")
        project.add_step(generated, "demo.generate", {})
        project.add_step(generated, "demo.upper", {"column": "a"})
        project.add_step(generated, "demo.look_up", {"file": "prices.csv"})  # reads a file: shown, marked
        project.add_step(generated, "demo.upper", {"column": "b"})
        copied = project.add_table("Copied")
        project.add_step(copied, "demo.copy", {"table": generated.id})
        assert marks(node_rows(session, generated, set())) == [
            (1, "step", "in"), (0, "gap", ""), (3, "step", "in"), (0, "gap", ""),
        ]
        assert marks(node_rows(session, copied, set())) == [(1, "step", "")]  # reads another table only
    finally:
        session.cancel_run()
        session.wait()


def test_a_missing_plugin_has_no_mark(session):
    table = session.project.add_table("T")
    session.project.add_step(table, "gone.action", {})
    assert marks(node_rows(session, table, set())) == [(1, "step", "")]


def test_a_collapsed_group_counts_imports_and_exports(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    customers, orders, revenue = session.project.tables
    group = session.add_group("Inputs", [customers.id, orders.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    box = view.group_nodes[group.id]
    assert box.counts == (2, 2) and box.footer == "2 in · 2 out"
    assert box.height == node_height(box.rows) + ROW
    assert [r.text for r in box.rows] == ["Orders", "⋯ 1 more table"]  # the rows stay as they were
    footer_y = box.height - ROW  # the footer is no row
    assert box.row_at(footer_y) is None
    render(view)

    session.set_enabled(orders.steps[-1].id, False)  # on or off: the counts stay
    session.set_table_enabled([customers.id], False)
    view.rebuild()
    assert view.group_nodes[group.id].counts == (2, 2)


def test_a_group_without_imports_or_exports_has_no_footer(window, settle, session, demo):
    view = open_map(window, session, demo, settle)
    orders = session.project.tables[1]
    derived = session.add_derived_table(orders.id)
    group = session.add_group("Derived", [derived.id])
    view.set_collapsed(group.id, True)
    view.rebuild()
    box = view.group_nodes[group.id]
    assert box.counts == (0, 0) and box.footer == ""
    assert box.height == node_height(box.rows)


def test_counts_text_and_group_counts(session, demo):
    session.open_project(demo)
    customers, orders, revenue = session.project.tables
    assert group_counts(session, [revenue]) == (0, 1)
    assert group_counts(session, [customers, orders, revenue]) == (2, 3)
    kinds: dict[str, str] = {}
    group_counts(session, [orders], kinds)
    assert kinds["csv.import"] == "in" and kinds["excel.export"] == "out" and kinds["table.merge"] == "step"
    assert counts_text((3, 2)) == "3 in · 2 out"
    assert counts_text((3, 0)) == "3 in"
    assert counts_text((0, 1)) == "1 out"
    assert counts_text((0, 0)) == ""


def test_mark_colours_are_readable():
    """Text on the boxes and in lists: at least 4.5 : 1."""

    def luminance(color: str) -> float:
        channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
        r, g, b = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
        return 0.2126 * r + 0.7152 * g + 0.0722 * b

    def contrast(a: str, b: str) -> float:
        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    for kind in ("in", "out", "cmd", "step"):
        boxes = (theme.MAP_BOX,) if kind != "step" else ()  # the map has no "step" mark
        for background in (*boxes, theme.BASE, theme.ALTERNATE, theme.WINDOW):
            assert contrast(theme.MARK_COLORS[kind], background) >= 4.5, (kind, background)
    assert theme.mark_color("in").name() == theme.MARK_COLORS["in"]
    assert theme.mark_color("unknown").name() == theme.TEXT_DIM
    assert contrast(theme.TEXT, theme.FIND_MATCH) >= 4.5

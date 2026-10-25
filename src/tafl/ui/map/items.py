"""What the project map is made of: table boxes, collapsed groups, arrows, group frames."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPainterPathStroker, QPen, QPolygonF
from PySide6.QtWidgets import (
    QGraphicsItem,
    QGraphicsObject,
    QGraphicsPathItem,
    QGraphicsSceneContextMenuEvent,
    QGraphicsSceneHoverEvent,
    QGraphicsSceneMouseEvent,
    QStyleOptionGraphicsItem,
)

from tafl.core import Group, Link, Status, Step, Table
from tafl.core.index import step_kind

from .. import theme
from ..session import Session, first_error, missing_settings, waiting_text
from .layout import GROUP_TITLE, HEADER, NODE_WIDTH, PADDING, ROW, frame_rect

if TYPE_CHECKING:
    from .view import MapView

_FADED = 0.5  # opacity of tables that are off or skipped
_DIMMED = 0.25  # opacity of what the highlight of a table's uses and users leaves out
_MARKS = {"in": "in", "out": "out", "cmd": "out"}  # step kind -> the mark on its row
# The highlight of what the selected table uses and what uses it: role -> color (MapView.lineage_role)
_ROLE_COLORS = {"table": theme.ACCENT, "uses": theme.MAP_USES, "used_by": theme.MAP_USED_BY, "both": theme.MAP_CYCLE}
_ROLE_WORDS = {  # for a tooltip, {0}: the selected table
    "table": "selected",
    "uses": "“{0}” uses it",
    "used_by": "it uses “{0}”",
    "both": "a circular reference with “{0}”",
}


@dataclass
class NodeRow:
    text: str
    key: str = ""  # step id (table box) or table id (collapsed group); empty for "⋯" rows
    number: int = 0
    kind: str = "step"  # "step", "link" (uses another table), "export", "table", "gap"
    enabled: bool = True
    mark: str = ""  # "in": reads from outside the project; "out": writes or changes something outside


def kind_of(session: Session, step: Step, kinds: dict[str, str]) -> str:
    """``step_kind`` of the step's action, kept in ``kinds`` (action id -> kind) while the map is built."""
    kind = kinds.get(step.action)
    if kind is None:
        kind = kinds[step.action] = step_kind(session.action(step))
    return kind


def node_rows(session: Session, table: Table, linked_steps: set[str], kinds: dict[str, str] | None = None) -> list[NodeRow]:
    """The import, steps using other tables, exports and steps reading from outside the project;
    the steps between are counted. ``kinds``: see ``kind_of``."""
    kinds = {} if kinds is None else kinds
    rows: list[NodeRow] = []
    skipped = 0
    for number, step in enumerate(table.steps, start=1):
        action = session.action(step)
        export = action is not None and action.side_effects
        outside = kind_of(session, step, kinds)
        if number != 1 and step.id not in linked_steps and not export and outside != "in":
            skipped += 1
            continue
        if skipped:
            rows.append(NodeRow(f"⋯ {skipped} step{'s' if skipped > 1 else ''}", kind="gap"))
            skipped = 0
        kind = "link" if step.id in linked_steps else "export" if export else "step"
        rows.append(NodeRow(session.describe(step), step.id, number, kind, step.enabled, _MARKS.get(outside, "")))
    if skipped:
        rows.append(NodeRow(f"⋯ {skipped} more step{'s' if skipped > 1 else ''}", kind="gap"))
    if not table.steps:
        rows.append(NodeRow("no steps yet", kind="gap"))
    return rows


def group_counts(session: Session, members: list[Table], kinds: dict[str, str] | None = None) -> tuple[int, int]:
    """For a collapsed group: its steps that read from outside the project, and those that write or
    change something outside (on or off). ``kinds``: see ``kind_of``."""
    kinds = {} if kinds is None else kinds
    marks = [_MARKS.get(kind_of(session, step, kinds), "") for table in members for step in table.steps]
    return marks.count("in"), marks.count("out")


def counts_text(counts: tuple[int, int]) -> str:
    """The footer of a collapsed group: "3 in · 2 out"; a part that is zero is left out."""
    return " · ".join(f"{number} {word}" for number, word in zip(counts, ("in", "out")) if number)


def group_rows(members: list[Table], connected: set[str]) -> list[NodeRow]:
    """A collapsed group lists its tables that are connected to the outside (arrows end there);
    the others are counted. Without such tables it lists the first few."""
    shown = [t for t in members if t.id in connected] or members[:4]
    rows = [NodeRow(t.name, t.id, kind="table", enabled=t.enabled) for t in shown]
    hidden = len(members) - len(shown)
    if hidden:
        rows.append(NodeRow(f"⋯ {hidden} more table{'s' if hidden > 1 else ''}", kind="gap"))
    return rows


def node_height(rows: list[NodeRow]) -> float:
    return HEADER + ROW * len(rows) + PADDING


def _arrow_head(painter: QPainter, tip: QPointF, color: QColor) -> None:
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPolygon(QPolygonF([tip, tip + QPointF(-10, -5.5), tip + QPointF(-10, 5.5)]))


def _port(painter: QPainter, center: QPointF, highlighted: bool, radius: float = 4.5) -> None:
    painter.setPen(QPen(QColor(theme.MAP_BOX), 2))
    painter.setBrush(QColor(theme.ACCENT if highlighted else theme.MAP_EDGE))
    painter.drawEllipse(center, radius, radius)


def _mark_font(base: QFont) -> QFont:
    """The font of the "in" and "out" marks: bold, one point smaller."""
    font = QFont(base)
    font.setBold(True)
    if font.pointSizeF() > 1:
        font.setPointSizeF(font.pointSizeF() - 1)
    elif font.pixelSize() > 1:
        font.setPixelSize(font.pixelSize() - 1)
    return font


class Box(QGraphicsObject):
    """Shared behavior of table boxes and collapsed groups: rows, anchors, dragging."""

    def __init__(self, view: MapView, rows: list[NodeRow]) -> None:
        super().__init__()
        self.view = view
        self.rows = rows
        self.height = node_height(rows)
        self.edges: list[Edge] = []
        self.skipped_after = 0  # number of the step that skipped the rest of the table (0: none did)
        self._press_pos: QPointF | None = None
        self.setFlags(
            QGraphicsItem.GraphicsItemFlag.ItemIsMovable
            | QGraphicsItem.GraphicsItemFlag.ItemIsSelectable
            | QGraphicsItem.GraphicsItemFlag.ItemSendsGeometryChanges
        )
        self.setAcceptHoverEvents(True)
        self.setZValue(1)

    def boundingRect(self) -> QRectF:
        return QRectF(-6, -2, NODE_WIDTH + 14, self.height + 4)

    def box_rect(self) -> QRectF:
        """The box in scene coordinates, without the margin for ports."""
        return QRectF(self.pos().x(), self.pos().y(), NODE_WIDTH, self.height)

    def row_offset(self, key: str) -> float:
        """Height of a row inside the box (where arrows arrive)."""
        index = next((i for i, r in enumerate(self.rows) if r.key == key), 0)
        return HEADER + PADDING / 2 + index * ROW + ROW / 2

    def row_at(self, y: float) -> NodeRow | None:
        index = int((y - HEADER - PADDING / 2) // ROW)
        return self.rows[index] if y > HEADER and 0 <= index < len(self.rows) else None

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemPositionHasChanged:
            self.view.box_moving(self)
        elif change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.update()
        return super().itemChange(change, value)

    def _paint_frame(self, painter: QPainter, border: QColor, width: float, dashed: bool = False) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        pen = QPen(border, width)
        if dashed:
            pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        painter.setBrush(QColor(theme.MAP_BOX))
        painter.drawRoundedRect(QRectF(0, 0, NODE_WIDTH, self.height), 6, 6)

    def _paint_header(self, painter: QPainter, name: str, info: str, name_color: QColor, info_color: QColor, left: float = 16) -> None:
        base = painter.font()
        bold = QFont(base)
        bold.setBold(True)
        info_width = QFontMetrics(base).horizontalAdvance(info) + 8 if info else 0
        painter.setPen(info_color)
        painter.drawText(
            QRectF(NODE_WIDTH - info_width - 14, 0, info_width, HEADER), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, info
        )
        painter.setFont(bold)
        painter.setPen(name_color)
        width = NODE_WIDTH - left - 14 - info_width - 10
        painter.drawText(
            QRectF(left, 0, width, HEADER),
            Qt.AlignmentFlag.AlignVCenter,
            QFontMetrics(bold).elidedText(name, Qt.TextElideMode.ElideRight, int(width)),
        )
        painter.setFont(base)
        painter.setPen(QPen(QColor(theme.BORDER), 1))
        painter.drawLine(QPointF(5, HEADER), QPointF(NODE_WIDTH, HEADER))

    def dimmed(self, index: int) -> bool:
        """Whether row ``index`` is painted like a step that is off: it is off, or it comes after
        the step that skipped the rest of the table (a row "⋯ 2 steps" right after that step too)."""
        row = self.rows[index]
        if not row.enabled:
            return True
        if not self.skipped_after:
            return False
        if row.number:
            return row.number > self.skipped_after
        before = next((r.number for r in reversed(self.rows[:index]) if r.number), 0)
        return row.kind == "gap" and before >= self.skipped_after

    def _paint_rows(self, painter: QPainter, numbers: bool) -> None:
        base = painter.font()
        metrics = QFontMetrics(base)
        mark_font = _mark_font(base)
        mark_metrics = QFontMetrics(mark_font)
        number_width = metrics.horizontalAdvance("00") + 8 if numbers else 14
        for index, row in enumerate(self.rows):
            top = HEADER + PADDING / 2 + index * ROW
            if row.number:
                painter.setPen(QColor(theme.MAP_TEXT_DIM))
                painter.drawText(QRectF(16, top, number_width, ROW), Qt.AlignmentFlag.AlignVCenter, str(row.number))
            left = 16 + number_width
            if row.kind == "table":  # status dot of the table
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.status_color(self.view.session.table_status(row.key)))
                painter.drawEllipse(QPointF(20, top + ROW / 2), 3.5, 3.5)
            dimmed = self.dimmed(index)
            width = NODE_WIDTH - left - 12
            if row.mark:  # "in" or "out" at the right end
                mark_width = mark_metrics.horizontalAdvance(row.mark)
                painter.setFont(mark_font)
                painter.setPen(QColor(theme.TEXT_DISABLED) if dimmed else theme.mark_color(row.mark))
                painter.drawText(
                    QRectF(NODE_WIDTH - 12 - mark_width, top, mark_width, ROW),
                    Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter,
                    row.mark,
                )
                painter.setFont(base)
                width -= mark_width + 8
            color = theme.TEXT_DISABLED if dimmed else theme.MAP_TEXT_DIM if row.kind == "gap" else theme.TEXT
            painter.setPen(QColor(color))
            text = metrics.elidedText(row.text, Qt.TextElideMode.ElideRight, int(width))
            painter.drawText(QRectF(left, top, width, ROW), Qt.AlignmentFlag.AlignVCenter, text)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self._press_pos = QPointF(self.pos())
        super().mousePressEvent(event)
        self.view.box_pressed(self)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        super().mouseReleaseEvent(event)
        moved = self._press_pos is not None and self.pos() != self._press_pos
        self._press_pos = None
        self.view.box_released(self, moved)


class TableNode(Box):
    """A table: header with status, size and name; one row per important step. The dot on the
    right edge is the output: drag it onto another table to let that table use this one."""

    def __init__(self, view: MapView, table: Table, rows: list[NodeRow]) -> None:
        super().__init__(view, rows)
        self.table_id = table.id
        self.name = table.name
        self.status = "pending"
        self.info = ""
        self.link_target = False  # highlighted while a new arrow is dragged over it
        self.search_match = False
        self._port_hover = False
        self._linking = False

    def in_anchor(self, step_id: str) -> QPointF:
        return self.mapToScene(QPointF(0, self.row_offset(step_id)))

    def out_anchor(self, key: str = "") -> QPointF:
        return self.mapToScene(QPointF(NODE_WIDTH, HEADER / 2))

    def on_port(self, pos: QPointF) -> bool:
        return (pos - QPointF(NODE_WIDTH, HEADER / 2)).manhattanLength() < 14

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Any = None) -> None:
        current = self.view.session.table_id == self.table_id
        if self.status in ("disabled", "skipped"):  # on top of the highlight's dimming, if any
            painter.setOpacity(painter.opacity() * _FADED)
        role = self.view.lineage_role(self.table_id)
        if self.search_match:
            border, width = QColor(theme.STATUS_COLORS["warning"]), 2.0
        elif role in ("uses", "used_by", "both"):
            border, width = QColor(_ROLE_COLORS[role]), 2.5
        elif current or self.isSelected() or self.link_target:
            border, width = QColor(theme.ACCENT), 2.0
        else:
            border, width = QColor(theme.BORDER), 1.0
        self._paint_frame(painter, border, width, dashed=self.link_target)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.status_color(self.status))
        painter.drawRoundedRect(QRectF(0, 0, 5, self.height), 2.5, 2.5)
        self._paint_header(painter, self.name, self.info, QColor(theme.TEXT), QColor(theme.MAP_TEXT_DIM))
        self._paint_rows(painter, numbers=True)
        for index, row in enumerate(self.rows):
            if row.kind == "link":
                top = HEADER + PADDING / 2 + index * ROW
                _port(painter, QPointF(0, top + ROW / 2), self.view.port_highlighted(row.key))
        out_active = current or self._port_hover or any(e.source is self and e.lit for e in self.edges)
        _port(painter, QPointF(NODE_WIDTH, HEADER / 2), out_active, 6 if self._port_hover else 4.5)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton and self.on_port(event.pos()):
            self._linking = True
            self.view.start_link(self)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._linking:
            self.view.move_link(event.scenePos())
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._linking:
            self._linking = False
            self.view.finish_link(self, event.scenePos(), event.screenPos())
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self.on_port(event.pos()):
            return
        row = self.row_at(event.pos().y())
        self.view.open_table.emit(self.table_id, row.key if row else "")

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        on_port = self.on_port(event.pos())
        if on_port != self._port_hover:
            self._port_hover = on_port
            self.setCursor(Qt.CursorShape.CrossCursor if on_port else Qt.CursorShape.ArrowCursor)
            self.update()
        row = self.row_at(event.pos().y())
        if on_port:
            self.setToolTip(
                f"Drag onto another table: it merges or appends “{self.name}”.\n"
                f"Drag onto an empty spot: a new table starting from “{self.name}”."
            )
        elif self.status in ("disabled", "skipped"):
            reason = self.view.session.results.get(self.table_id)
            why = reason.skipped if reason is not None and reason.skipped else "turned off"
            hint = "\nRight-click → Turn table on" if self.status == "disabled" else ""
            self.setToolTip(f"{self.name}: not calculated ({why}){hint}")
        elif row is not None and row.kind != "gap":
            self.setToolTip(f"Step {row.number}: {row.text}{self._skipped(row.key)}\nDouble-click to open this step")
        else:
            self.setToolTip(
                f"{self.name}{self._skipped()}{self._incomplete()}\nDouble-click to open this table · right-click for more"
            )

    def _incomplete(self) -> str:
        """A line for the tooltip when a step is not filled in yet (or one of a table it uses)."""
        if self.status == "waiting" and (waiting := self.view.session.waits_for(self.table_id)) is not None:
            return "\n" + waiting_text(waiting, capital=True)
        failed = first_error(self.view.session.results.get(self.table_id)) if self.status == "incomplete" else None
        if failed is None:
            return ""
        return f"\nStep {failed[0]} is not complete yet: fill in {', '.join(missing_settings(failed[1]))}"

    def _skipped(self, step_id: str = "") -> str:
        """A line for the tooltip when a step skipped the rest of the table (SkipRest)."""
        result = self.view.session.results.get(self.table_id)
        if result is None:
            return ""
        if not step_id:
            ended = result.skipped_from
            return f"\nRest skipped: {ended.skip_reason}" if ended is not None else ""
        outcome = next((s for s in result.steps if s.step_id == step_id), None)
        if outcome is None or outcome.status != Status.SKIPPED or not outcome.skip_rest:
            return ""
        return f"\nSkipped: {outcome.skip_reason}"

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        if self._port_hover:
            self._port_hover = False
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        if not self.isSelected():
            self.scene().clearSelection()
            self.setSelected(True)
        self.view.node_menu(self, event.screenPos())


class GroupNode(Box):
    """A collapsed group: one box listing its tables that have arrows to or from the outside.
    Double-click the header to expand it; click a row to select that table. A line at the bottom
    counts the steps that read from outside the project and those that write or change something
    outside ("3 in · 2 out")."""

    def __init__(
        self, view: MapView, group: Group, rows: list[NodeRow], members: list[str], counts: tuple[int, int] = (0, 0)
    ) -> None:
        super().__init__(view, rows)
        self.group_id = group.id
        self.name = group.name
        self.color = group.color
        self.members = members
        self.summary = ""
        self.summary_color = QColor(theme.MAP_TEXT_DIM)
        self.counts = counts  # steps "in", steps "out" (see group_counts)
        if any(counts):
            self.height += ROW

    @property
    def footer(self) -> str:
        """The line below the rows ("" when there is none)."""
        return counts_text(self.counts)

    def in_anchor(self, table_id: str) -> QPointF:
        return self.mapToScene(QPointF(0, self.row_offset(table_id)))

    def out_anchor(self, table_id: str = "") -> QPointF:
        return self.mapToScene(QPointF(NODE_WIDTH, self.row_offset(table_id)))

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Any = None) -> None:
        color = theme.group_color(self.color)
        selected = self.isSelected() or self.view.session.table_id in self.members
        self._paint_frame(painter, color if not selected else QColor(theme.ACCENT), 2.0)
        tint = QColor(color)
        tint.setAlpha(40)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(tint)
        painter.drawRoundedRect(QRectF(1, 1, NODE_WIDTH - 2, HEADER - 1), 5, 5)
        painter.setPen(color.lighter(130))
        painter.drawText(QRectF(10, 0, 16, HEADER), Qt.AlignmentFlag.AlignVCenter, "▸")
        self._paint_header(painter, f"{self.name}  ({len(self.members)})", self.summary, color.lighter(130), self.summary_color, left=26)
        self._paint_rows(painter, numbers=False)
        self._paint_footer(painter)
        for index, row in enumerate(self.rows):
            center = HEADER + PADDING / 2 + index * ROW + ROW / 2
            if row.kind == "gap" and self.view.lineage is not None:  # "⋯ 4 more tables": a bar per color
                roles = {role for _, role in self.hidden_roles()}
                colors = [color for role, color in _ROLE_COLORS.items() if role in roles]
                for number, color in enumerate(colors):
                    part = (ROW - 6) / len(colors)
                    painter.fillRect(QRectF(5, center - ROW / 2 + 3 + number * part, 3, part), QColor(color))
            if row.kind == "table":
                role = self.view.lineage_role(row.key)
                if role:  # a bar in the colour of the highlight
                    painter.fillRect(QRectF(5, center - ROW / 2 + 3, 3, ROW - 6), QColor(_ROLE_COLORS[role]))
                ins = any(e.target is self and e.target_key == row.key for e in self.edges)
                outs = any(e.source is self and e.source_key == row.key for e in self.edges)
                lit = any(e.lit and row.key in (e.link.source, e.link.target) for e in self.edges)
                if ins:
                    _port(painter, QPointF(0, center), lit)
                if outs:
                    _port(painter, QPointF(NODE_WIDTH, center), lit)

    def _paint_footer(self, painter: QPainter) -> None:
        """The line "3 in · 2 out" below the rows, the words in the colours of the marks."""
        if not self.footer:
            return
        dim = QColor(theme.MAP_TEXT_DIM)
        pieces: list[tuple[str, QColor]] = []
        for number, word in zip(self.counts, ("in", "out")):
            if number:
                if pieces:
                    pieces.append((" · ", dim))
                pieces += [(f"{number} ", dim), (word, theme.mark_color(word))]
        metrics = QFontMetrics(painter.font())
        top = HEADER + PADDING / 2 + len(self.rows) * ROW
        x = 16.0
        for text, color in pieces:
            painter.setPen(color)
            painter.drawText(QRectF(x, top, NODE_WIDTH - x, ROW), Qt.AlignmentFlag.AlignVCenter, text)
            x += metrics.horizontalAdvance(text)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        row = self.row_at(event.pos().y())
        if row is not None and row.kind == "table":
            self.view.open_table.emit(row.key, "")
        else:
            self.view.set_collapsed(self.group_id, False)

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        super().mousePressEvent(event)
        row = self.row_at(event.pos().y())
        if row is not None and row.kind == "table":
            self.view.session.select_table(row.key)

    def hidden_roles(self) -> list[tuple[str, str]]:
        """With the highlight on: the tables of the group that its rows don't list ("⋯ 4 more
        tables") and that are highlighted, with their roles (see MapView.lineage_role)."""
        listed = {row.key for row in self.rows if row.kind == "table"}
        return [(t, role) for t in self.members if t not in listed and (role := self.view.lineage_role(t))]

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        row = self.row_at(event.pos().y())
        hidden = self.hidden_roles() if row is not None and row.kind == "gap" else []
        if row is not None and row.kind == "table":
            self.setToolTip(f"{row.text}\nClick to select · double-click to open")
        elif hidden:
            project = self.view.session.project
            names = {t.id: t.name for t in project.tables}
            selected = names.get(self.view.lineage.table, "")
            lines = [f"{names.get(t, t)}: {_ROLE_WORDS[role].format(selected)}" for t, role in hidden]
            self.setToolTip("Highlighted, not listed here:\n" + "\n".join(lines))
        else:
            self.setToolTip(
                f"Group “{self.name}” (collapsed)\nDouble-click to expand · drop tables here to add them · right-click for more"
            )

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        self.view.group_menu(self.group_id, event.screenPos())


class Edge(QGraphicsPathItem):
    """An arrow from the table that is used to the step that uses it (or to the row of a
    collapsed group). Click it to edit that step; right-click to turn it off or delete it."""

    def __init__(
        self, view: MapView, link: Link, source: Box, source_key: str, target: Box, target_key: str, tooltip: str
    ) -> None:
        super().__init__()
        self.view = view
        self.link = link
        self.source, self.source_key = source, source_key
        self.target, self.target_key = target, target_key
        self.lit = False
        self._hover = False
        self._end = QPointF()
        source.edges.append(self)
        target.edges.append(self)
        self.setFlag(QGraphicsItem.GraphicsItemFlag.ItemIsSelectable)
        self.setAcceptHoverEvents(True)
        self.setToolTip(tooltip)
        self.refresh_pen()
        self.update_path()

    def refresh_pen(self) -> None:
        current = self.view.session.table_id
        self.lit = self._hover or self.isSelected() or current in (self.link.source, self.link.target)
        role = self.lineage_role()
        width = 2.4 if self._hover or self.isSelected() else 2.2 if role else 2.0 if self.lit else 1.6
        if role:
            color = _ROLE_COLORS[role]
        else:
            color = theme.ACCENT if self.lit else theme.MAP_EDGE
        pen = QPen(QColor(color), width)
        if not self.link.enabled:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setZValue(0.6 if role else 0.5 if self.lit else 0)
        self.setOpacity(_DIMMED if role == "" else 1.0)

    def lineage_role(self) -> str | None:
        """With the highlight of a table's uses and users on: "uses" for an arrow between tables it
        uses (or into it), "used_by" for one between tables that use it (or out of it), "both" for
        one that is both (a circular reference), else "". None when the highlight is off. Arrows
        of steps that are off are never part of it."""
        lineage = self.view.lineage
        if lineage is None:
            return None
        source, target = self.link.source, self.link.target
        if not self.link.enabled:
            return ""
        uses = (target == lineage.table or target in lineage.uses) and source in lineage.uses
        used_by = (source == lineage.table or source in lineage.used_by) and target in lineage.used_by
        return "both" if uses and used_by else "uses" if uses else "used_by" if used_by else ""

    def update_path(self) -> None:
        start = self.source.out_anchor(self.source_key)
        end = self.target.in_anchor(self.target_key)
        self._end = end
        if end.x() - start.x() < 40:  # going backwards: around both boxes
            path = _detour(start, end, self.source.box_rect(), self.target.box_rect())
        else:
            path = QPainterPath(start)
            lane = self._lane(start, end)
            if lane is None:
                bend = max(60.0, abs(end.x() - start.x()) / 2)
                path.cubicTo(start + QPointF(bend, 0), end - QPointF(bend, 0), end - QPointF(9, 0))
            else:  # around the boxes standing between: along a lane above or below them
                r = 40.0
                path.cubicTo(start + QPointF(r, 0), QPointF(start.x() + r, lane), QPointF(start.x() + 2 * r, lane))
                path.lineTo(end.x() - 2 * r, lane)
                path.cubicTo(QPointF(end.x() - r, lane), end - QPointF(r, 0), end - QPointF(9, 0))
        self.prepareGeometryChange()
        self.setPath(path)

    def _lane(self, start: QPointF, end: QPointF) -> float | None:
        """A height at which the arrow passes the boxes between its ends; None if none is in the way."""
        if end.x() - start.x() < 200:
            return None
        between = [
            box
            for item in self.view.boxes()
            if item is not self.source and item is not self.target
            and (box := item.box_rect()).right() > start.x() + 20 and box.left() < end.x() - 20
        ]
        low, high = min(start.y(), end.y()) - 12, max(start.y(), end.y()) + 12
        if not any(box.top() < high and box.bottom() > low for box in between):
            return None
        above = min(box.top() for box in between) - 18
        below = max(box.bottom() for box in between) + 18
        middle = (start.y() + end.y()) / 2
        return above if abs(above - middle) <= abs(below - middle) else below

    def shape(self) -> QPainterPath:
        """Only the line itself (12 px wide) reacts to the mouse, not the area under the curve."""
        stroker = QPainterPathStroker()
        stroker.setWidth(12)
        return stroker.createStroke(self.path())

    def boundingRect(self) -> QRectF:
        return super().boundingRect().adjusted(-12, -12, 12, 12)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self.pen())
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(self.path())
        _arrow_head(painter, self._end - QPointF(4.5, 0), self.pen().color())

    def itemChange(self, change: QGraphicsItem.GraphicsItemChange, value: Any) -> Any:
        if change == QGraphicsItem.GraphicsItemChange.ItemSelectedHasChanged:
            self.refresh_pen()
        return super().itemChange(change, value)

    def hoverEnterEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hover = True
        self.refresh_pen()

    def hoverLeaveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        self._hover = False
        self.refresh_pen()

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        super().mousePressEvent(event)
        self.view.edge_clicked(self)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        self.view.open_table.emit(self.link.target, self.link.step)

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        self.view.edge_menu(self, event.screenPos())


def _detour(start: QPointF, end: QPointF, source: QRectF, target: QRectF) -> QPainterPath:
    """Out to the right, along a lane between or below the boxes, back in from the left."""
    out_x = max(start.x(), source.right()) + 26
    in_x = min(end.x(), target.left()) - 26
    if target.top() > source.bottom() + 24:
        lane = (source.bottom() + target.top()) / 2
    elif source.top() > target.bottom() + 24:
        lane = (target.bottom() + source.top()) / 2
    else:
        lane = max(source.bottom(), target.bottom()) + 26
    points = [start, QPointF(out_x, start.y()), QPointF(out_x, lane), QPointF(in_x, lane), QPointF(in_x, end.y()), end - QPointF(9, 0)]
    return _rounded(points, 12)


def _rounded(points: list[QPointF], radius: float) -> QPainterPath:
    """A polyline with rounded corners."""
    path = QPainterPath(points[0])
    for before, corner, after in zip(points, points[1:], points[2:]):
        into, out = corner - before, after - corner
        into_length, out_length = math.hypot(into.x(), into.y()), math.hypot(out.x(), out.y())
        if into_length < 1 or out_length < 1:
            path.lineTo(corner)
            continue
        r = min(radius, into_length / 2, out_length / 2)
        path.lineTo(corner - into * (r / into_length))
        path.quadTo(corner, corner + out * (r / out_length))
    path.lineTo(points[-1])
    return path


class GroupFrame(QGraphicsObject):
    """A colored frame around the tables of a group. Drag its title to move the whole group;
    double-click the title to rename it; ▾ collapses it."""

    def __init__(self, view: MapView, group: Group, members: list[TableNode]) -> None:
        super().__init__()
        self.view = view
        self.group_id = group.id
        self.name = group.name
        self.color = group.color
        self.members = members
        self.rect = QRectF()
        self.drop_target = False
        self._drag_start: QPointF | None = None
        self._member_start: list[QPointF] = []
        self.setZValue(-1)
        self.setAcceptHoverEvents(True)
        self.update_rect()

    def update_rect(self) -> None:
        self.prepareGeometryChange()
        self.rect = frame_rect(m.box_rect() for m in self.members)

    def title_rect(self) -> QRectF:
        return QRectF(self.rect.left(), self.rect.top(), self.rect.width(), GROUP_TITLE)

    def collapse_rect(self) -> QRectF:
        return QRectF(self.rect.right() - 30, self.rect.top(), 26, GROUP_TITLE)

    def set_drop_target(self, on: bool) -> None:
        if on != self.drop_target:
            self.drop_target = on
            self.update()

    def boundingRect(self) -> QRectF:
        return self.rect.adjusted(-3, -3, 3, 3)

    def shape(self) -> QPainterPath:
        """Only the title bar reacts to the mouse; inside the frame the canvas works as usual."""
        path = QPainterPath()
        path.addRect(self.title_rect())
        return path

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Any = None) -> None:
        color = theme.group_color(self.color)
        fill = QColor(color)
        fill.setAlpha(48 if self.drop_target else 22)
        border = QColor(color)
        border.setAlpha(255 if self.drop_target else 150)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(border, 2 if self.drop_target else 1.2))
        painter.setBrush(fill)
        painter.drawRoundedRect(self.rect, 8, 8)
        font = QFont(painter.font())
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(color.lighter(130))
        title = f"{self.view.group_number(self.group_id)}  {self.name}   ({len(self.members)})"
        text = QFontMetrics(font).elidedText(title, Qt.TextElideMode.ElideRight, int(self.rect.width() - 54))
        painter.drawText(self.title_rect().adjusted(12, 0, -40, 0), Qt.AlignmentFlag.AlignVCenter, text)
        painter.drawText(self.collapse_rect(), Qt.AlignmentFlag.AlignCenter, "▾")

    def mousePressEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            event.ignore()
            return
        if self.collapse_rect().contains(event.scenePos()):
            self.view.set_collapsed(self.group_id, True)
            return
        self._drag_start = event.scenePos()
        self._member_start = [QPointF(m.pos()) for m in self.members]
        event.accept()

    def mouseMoveEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if self._drag_start is None:
            return
        delta = event.scenePos() - self._drag_start
        for member, start in zip(self.members, self._member_start):
            member.setPos(start + delta)
        self.update_rect()

    def mouseReleaseEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        moved = self._drag_start is not None and event.scenePos() != self._drag_start
        self._drag_start = None
        if moved:
            self.view.frame_moved(self.group_id)

    def mouseDoubleClickEvent(self, event: QGraphicsSceneMouseEvent) -> None:
        if not self.collapse_rect().contains(event.scenePos()):
            self.view.rename_group(self.group_id)

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        if self.collapse_rect().contains(event.scenePos()):
            self.setCursor(Qt.CursorShape.PointingHandCursor)
            self.setToolTip("Collapse the group into one box")
        else:
            self.setCursor(Qt.CursorShape.SizeAllCursor)
            self.setToolTip(f"Group “{self.name}”: drag to move it · double-click to rename · right-click for more")

    def contextMenuEvent(self, event: QGraphicsSceneContextMenuEvent) -> None:
        self.view.group_menu(self.group_id, event.screenPos())


class LinkDrag(QGraphicsPathItem):
    """The dashed arrow that follows the mouse while a new connection is dragged."""

    def __init__(self, start: QPointF) -> None:
        super().__init__()
        self.start = start
        self.end = start
        self.setPen(QPen(QColor(theme.ACCENT), 2, Qt.PenStyle.DashLine))
        self.setZValue(5)

    def move_to(self, end: QPointF) -> None:
        self.end = end
        bend = max(40.0, abs(end.x() - self.start.x()) / 2)
        path = QPainterPath(self.start)
        path.cubicTo(self.start + QPointF(bend, 0), end - QPointF(bend, 0), end)
        self.prepareGeometryChange()
        self.setPath(path)

    def boundingRect(self) -> QRectF:
        return super().boundingRect().adjusted(-12, -12, 12, 12)

    def paint(self, painter: QPainter, option: QStyleOptionGraphicsItem, widget: Any = None) -> None:
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(self.pen())
        painter.drawPath(self.path())
        _arrow_head(painter, self.end + QPointF(4, 0), self.pen().color())

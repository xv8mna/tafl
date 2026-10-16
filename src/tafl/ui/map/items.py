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

from tafl.core import Group, Link, Table

from .. import theme
from ..session import Session
from .layout import GROUP_TITLE, HEADER, NODE_WIDTH, PADDING, ROW, frame_rect

if TYPE_CHECKING:
    from .view import MapView

_FADED = 0.5  # opacity of tables that are off or skipped


@dataclass
class NodeRow:
    text: str
    key: str = ""  # step id (table box) or table id (collapsed group); empty for "⋯" rows
    number: int = 0
    kind: str = "step"  # "step", "link" (uses another table), "export", "table", "gap"
    enabled: bool = True


def node_rows(session: Session, table: Table, linked_steps: set[str]) -> list[NodeRow]:
    """The import, steps using other tables and exports; the steps between are counted."""
    rows: list[NodeRow] = []
    skipped = 0
    for number, step in enumerate(table.steps, start=1):
        action = session.action(step)
        export = action is not None and action.side_effects
        if number != 1 and step.id not in linked_steps and not export:
            skipped += 1
            continue
        if skipped:
            rows.append(NodeRow(f"⋯ {skipped} step{'s' if skipped > 1 else ''}", kind="gap"))
            skipped = 0
        kind = "link" if step.id in linked_steps else "export" if export else "step"
        rows.append(NodeRow(session.engine.describe(step), step.id, number, kind, step.enabled))
    if skipped:
        rows.append(NodeRow(f"⋯ {skipped} more step{'s' if skipped > 1 else ''}", kind="gap"))
    if not table.steps:
        rows.append(NodeRow("no steps yet", kind="gap"))
    return rows


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


class Box(QGraphicsObject):
    """Shared behavior of table boxes and collapsed groups: rows, anchors, dragging."""

    def __init__(self, view: MapView, rows: list[NodeRow]) -> None:
        super().__init__()
        self.view = view
        self.rows = rows
        self.height = node_height(rows)
        self.edges: list[Edge] = []
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

    def _paint_rows(self, painter: QPainter, numbers: bool) -> None:
        metrics = QFontMetrics(painter.font())
        number_width = metrics.horizontalAdvance("00") + 8 if numbers else 14
        for index, row in enumerate(self.rows):
            top = HEADER + PADDING / 2 + index * ROW
            dim = row.kind == "gap" or not row.enabled
            if row.number:
                painter.setPen(QColor(theme.TEXT_DIM))
                painter.drawText(QRectF(16, top, number_width, ROW), Qt.AlignmentFlag.AlignVCenter, str(row.number))
            left = 16 + number_width
            if row.kind == "table":  # status dot of the table
                painter.setPen(Qt.PenStyle.NoPen)
                painter.setBrush(theme.status_color(self.view.session.table_status(row.key)))
                painter.drawEllipse(QPointF(20, top + ROW / 2), 3.5, 3.5)
            painter.setPen(QColor(theme.TEXT_DISABLED if dim else theme.TEXT))
            text = metrics.elidedText(row.text, Qt.TextElideMode.ElideRight, int(NODE_WIDTH - left - 12))
            painter.drawText(QRectF(left, top, NODE_WIDTH - left - 12, ROW), Qt.AlignmentFlag.AlignVCenter, text)

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
        if self.status in ("disabled", "skipped"):
            painter.setOpacity(_FADED)
        if self.search_match:
            border, width = QColor(theme.STATUS_COLORS["warning"]), 2.0
        elif current or self.isSelected() or self.link_target:
            border, width = QColor(theme.ACCENT), 2.0
        else:
            border, width = QColor(theme.BORDER), 1.0
        self._paint_frame(painter, border, width, dashed=self.link_target)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(theme.status_color(self.status))
        painter.drawRoundedRect(QRectF(0, 0, 5, self.height), 2.5, 2.5)
        self._paint_header(painter, self.name, self.info, QColor(theme.TEXT), QColor(theme.TEXT_DIM))
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
            self.setToolTip(f"Step {row.number}: {row.text}\nDouble-click to open this step")
        else:
            self.setToolTip(f"{self.name}\nDouble-click to open this table · right-click for more")

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
    Double-click the header to expand it; click a row to select that table."""

    def __init__(self, view: MapView, group: Group, rows: list[NodeRow], members: list[str]) -> None:
        super().__init__(view, rows)
        self.group_id = group.id
        self.name = group.name
        self.color = group.color
        self.members = members
        self.summary = ""
        self.summary_color = QColor(theme.TEXT_DIM)

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
        for index, row in enumerate(self.rows):
            if row.kind == "table":
                center = HEADER + PADDING / 2 + index * ROW + ROW / 2
                ins = any(e.target is self and e.target_key == row.key for e in self.edges)
                outs = any(e.source is self and e.source_key == row.key for e in self.edges)
                lit = any(e.lit and row.key in (e.link.source, e.link.target) for e in self.edges)
                if ins:
                    _port(painter, QPointF(0, center), lit)
                if outs:
                    _port(painter, QPointF(NODE_WIDTH, center), lit)

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

    def hoverMoveEvent(self, event: QGraphicsSceneHoverEvent) -> None:
        row = self.row_at(event.pos().y())
        if row is not None and row.kind == "table":
            self.setToolTip(f"{row.text}\nClick to select · double-click to open")
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
        width = 2.4 if self._hover or self.isSelected() else 2.0 if self.lit else 1.6
        pen = QPen(QColor(theme.ACCENT if self.lit else theme.MAP_EDGE), width)
        if not self.link.enabled:
            pen.setStyle(Qt.PenStyle.DashLine)
        self.setPen(pen)
        self.setZValue(0.5 if self.lit else 0)

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

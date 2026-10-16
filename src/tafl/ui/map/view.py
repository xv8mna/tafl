"""The project map: tables, how they are connected, groups — and next to it the selected
table's steps, settings and data, so a whole project can be built from here.

For big projects: the group bar (and the keys 1–9) jumps to a group, groups can be collapsed
into one box each, and Ctrl+F finds a table by name. A map arranged by hand stays free of
overlaps: what is in the way of a growing group moves down (``layout.tidy``).

Rebuilding the scene always happens a moment later (``schedule_rebuild``): items must not be
deleted while one of them is still handling a mouse event.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from PySide6.QtCore import QPoint, QPointF, QRectF, QStringListModel, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import (
    QCompleter,
    QGraphicsScene,
    QGraphicsView,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSplitter,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from tafl.api import Action
from tafl.core import Group

from .. import theme
from ..action_menu import fill_action_menu
from ..data_panel import DataPanel
from ..session import Session
from ..settings_panel import SettingsPanel
from ..steps_panel import StepsPanel
from ..tables_panel import confirm_delete_table
from .items import Box, Edge, GroupFrame, GroupNode, LinkDrag, NodeRow, TableNode, group_rows, node_height, node_rows
from .layout import (
    GAP_X,
    GAP_Y,
    GROUP_PAD,
    GROUP_TITLE,
    HEADER,
    NODE_WIDTH,
    Arc,
    Unit,
    auto_layout,
    free_spot,
    place,
    tidy,
)

_MIN_ZOOM, _MAX_ZOOM = 0.05, 2.5
_GROUP_UNIT = "group:"  # a collapsed group on the map: "group:<group id>"
_NEW_GROUP = "\0new"  # stands for a group that is about to be created
LINK_CHOICES = [
    ("lookup", "Lookup: add columns (first match)"),
    ("left", "Left join"),
    ("inner", "Inner join"),
    ("full", "Full join"),
    None,
    ("append", "Append its rows below"),
    None,
    ("matching", "Keep rows with a match"),
    ("non_matching", "Keep rows without a match"),
]
_CHIP_STYLE = f"""
QToolButton#Chip {{ border: 1px solid {theme.BORDER_LIGHT}; border-radius: 11px; padding: 2px 10px; background: {theme.RAISED}; }}
QToolButton#Chip:hover {{ background: {theme.BORDER_LIGHT}; border-color: {theme.MAP_EDGE}; }}
QToolButton#Chip[error="true"] {{ border-color: {theme.STATUS_COLORS["error"]}; }}
QToolButton#Plain {{ border: none; padding: 2px 8px; color: {theme.TEXT_DIM}; }}
QToolButton#Plain:hover {{ color: {theme.TEXT}; }}
"""


class _Canvas(QGraphicsView):
    def __init__(self, scene: QGraphicsScene, view: MapView) -> None:
        super().__init__(scene, view)
        self.map_view = view
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setViewportUpdateMode(QGraphicsView.ViewportUpdateMode.FullViewportUpdate)
        self.setFrameShape(QGraphicsView.Shape.NoFrame)
        font = self.font()  # text drawn at any zoom: hinting for one size spaces the letters unevenly
        font.setHintingPreference(QFont.HintingPreference.PreferNoHinting)
        self.setFont(font)
        self.viewport().setFont(font)

    def zoom(self, factor: float) -> None:
        current = self.transform().m11()
        target = min(max(current * factor, _MIN_ZOOM), _MAX_ZOOM)
        self.scale(target / current, target / current)

    def wheelEvent(self, event: Any) -> None:
        self.map_view.auto_fit = False
        if event.modifiers() & Qt.KeyboardModifier.ControlModifier:
            self.zoom(1.15 ** (event.angleDelta().y() / 120))
            return
        super().wheelEvent(event)

    def mousePressEvent(self, event: Any) -> None:
        # Shift + drag on the background selects boxes with a rectangle; a plain drag pans.
        empty = self.itemAt(event.position().toPoint()) is None
        if empty and event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
            self.setDragMode(QGraphicsView.DragMode.RubberBandDrag)
        elif empty:
            self.map_view.auto_fit = False
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: Any) -> None:
        super().mouseReleaseEvent(event)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)

    def keyPressEvent(self, event: Any) -> None:
        if not self.map_view.key_pressed(event):
            super().keyPressEvent(event)

    def resizeEvent(self, event: Any) -> None:
        super().resizeEvent(event)
        self.map_view.refit()

    def drawBackground(self, painter: QPainter, rect: QRectF) -> None:
        painter.fillRect(rect, QColor(theme.MAP_CANVAS))
        if self.transform().m11() < 0.45:
            return
        step = 24
        painter.setPen(QPen(QColor(theme.MAP_DOT), 2))
        x0 = int(rect.left()) - int(rect.left()) % step
        y0 = int(rect.top()) - int(rect.top()) % step
        for x in range(x0, int(rect.right()) + step, step):
            for y in range(y0, int(rect.bottom()) + step, step):
                painter.drawPoint(x, y)

    def contextMenuEvent(self, event: Any) -> None:
        if self.itemAt(event.pos()) is None:
            self.map_view.canvas_menu(event.globalPos(), self.mapToScene(event.pos()))
        else:
            super().contextMenuEvent(event)


class _ChipStrip(QScrollArea):
    """The chips of the group bar; when there are more than fit, the mouse wheel scrolls them."""

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

    def wheelEvent(self, event: Any) -> None:
        bar = self.horizontalScrollBar()
        delta = event.angleDelta()
        bar.setValue(bar.value() - (delta.y() or delta.x()) // 2)


class GroupBar(QWidget):
    """One chip per group: click it (or press its number on the map) to jump there; right-click
    for more. "+ Group" makes a group of the selected tables."""

    def __init__(self, view: MapView) -> None:
        super().__init__(view)
        self.view = view
        self.setStyleSheet(_CHIP_STYLE)
        self.strip = _ChipStrip(self)
        self.chips: dict[str, QToolButton] = {}
        self._texts: dict[str, str] = {}
        self._expanded = True
        add = self._button(self, "Chip", "+ Group", lambda _=False: self.view.group_selected())
        add.setToolTip("Make a group of the selected tables (Ctrl+G).\nSelect tables with Ctrl+click or Shift+drag.")
        self.collapse_all = self._button(self, "Plain", "Collapse all", lambda _=False: self._collapse_all())
        show_all = self._button(self, "Plain", "Show all", lambda _=False: self.view.fit())
        show_all.setToolTip("Fit the whole project into the window (key 0)")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 3, 6, 3)
        layout.setSpacing(6)
        layout.addWidget(self.strip, 1)
        layout.addWidget(add)
        layout.addWidget(self.collapse_all)
        layout.addWidget(show_all)
        self.setFixedHeight(36)

    def rebuild(self) -> None:
        old = self.strip.takeWidget()
        if old is not None:
            old.deleteLater()  # a chip may still be handling a click
        inner = QWidget(self.strip)
        row = QHBoxLayout(inner)
        row.setContentsMargins(2, 2, 2, 2)
        row.setSpacing(6)
        project = self.view.session.project
        self.chips, self._texts = {}, {}
        for number, group in enumerate(project.groups, start=1):
            chip = self._button(inner, "Chip", "", lambda _=False, g=group.id: self.view.jump_to_group(g))
            chip.setIcon(theme.group_icon(group.color))
            chip.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextBesideIcon)
            count = len(project.members(group.id))
            prefix = f"{number}  " if number <= 9 else ""
            self._texts[group.id] = f"{prefix}{group.name} ({count}){'  ▸' if group.collapsed else ''}"
            key = f" (key {number})" if number <= 9 else ""
            chip.setToolTip(f"Jump to “{group.name}”{key}\nRight-click: collapse, add the selected tables, turn off…")
            chip.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
            chip.customContextMenuRequested.connect(
                lambda pos, g=group.id, c=chip: self.view.group_menu(g, c.mapToGlobal(pos))
            )
            row.addWidget(chip)
            self.chips[group.id] = chip
        if not project.groups:
            hint = QLabel("Groups keep big projects tidy: select tables, then Ctrl+G", inner)
            hint.setObjectName("Dim")
            row.addWidget(hint)
        row.addStretch(1)
        self.strip.setWidget(inner)
        self._expanded = any(not g.collapsed for g in project.groups)
        self.collapse_all.setVisible(bool(project.groups))
        self.collapse_all.setText("Collapse all" if self._expanded else "Expand all")
        self.collapse_all.setToolTip("Every group as one box: an overview" if self._expanded else "Show all tables")
        self.update_statuses()

    def _collapse_all(self) -> None:
        self.view.collapse([g.id for g in self.view.session.project.groups], self._expanded)

    def _button(self, parent: QWidget, kind: str, text: str, clicked: Callable[..., Any]) -> QToolButton:
        button = QToolButton(parent)
        button.setObjectName(kind)
        button.setText(text)
        button.setCursor(Qt.CursorShape.PointingHandCursor)
        button.clicked.connect(clicked)
        return button

    def update_statuses(self) -> None:
        """A chip shows how many of the group's tables have errors."""
        session = self.view.session
        for group_id, chip in self.chips.items():
            errors = sum(session.table_status(t.id) == "error" for t in session.project.members(group_id))
            chip.setText(self._texts[group_id] + (f"  ✗ {errors}" if errors else ""))
            if chip.property("error") != bool(errors):
                chip.setProperty("error", bool(errors))
                chip.style().unpolish(chip)
                chip.style().polish(chip)


class MapView(QWidget):
    open_table = Signal(str, str)  # table id, step id ("" = the table as a whole): open in the table view
    message = Signal(str)  # for the status bar

    def __init__(self, session: Session, edit_connections: Callable[[], None], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.scene = QGraphicsScene(self)
        self.canvas = _Canvas(self.scene, self)
        self.nodes: dict[str, TableNode] = {}
        self.group_nodes: dict[str, GroupNode] = {}  # collapsed groups
        self.edges: list[Edge] = []
        self.frames: dict[str, GroupFrame] = {}  # expanded groups
        self.auto_fit = True  # fit the map into the window until the user zooms, pans, drags or jumps
        self._saving = False
        self._grabbed: Box | None = None
        self._drag_frames: dict[str, QRectF] = {}  # the group frames as they were when a drag began
        self._drag_groups: set[str] = set()  # groups that are dragged as a whole
        self._link: LinkDrag | None = None
        self._link_source: TableNode | None = None
        self._pending_focus: str | None = None  # table to show after the next rebuild
        self._flashing: GroupFrame | None = None
        self._search_hits: list[str] = []
        self._search_index = -1
        self._rebuild_timer = QTimer(self)
        self._rebuild_timer.setSingleShot(True)
        self._rebuild_timer.setInterval(0)
        self._rebuild_timer.timeout.connect(self.rebuild)
        self._flash_timer = QTimer(self)
        self._flash_timer.setSingleShot(True)
        self._flash_timer.setInterval(800)
        self._flash_timer.timeout.connect(self._end_flash)

        title = QLabel("Project map", self)
        title.setObjectName("Title")
        self.search = QLineEdit(self)
        self.search.setPlaceholderText("Find table…  (Ctrl+F)")
        self.search.setClearButtonEnabled(True)
        self.search.setFixedWidth(230)
        self.search.setToolTip("Matching tables get an orange border; Enter jumps to the next one")
        self.search.textChanged.connect(self._search_changed)
        self.search.returnPressed.connect(self.find_next)
        self._names = QStringListModel(self)
        completer = QCompleter(self._names, self)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated[str].connect(self.find_table)
        self.search.setCompleter(completer)
        hint = QLabel("Drag a table's ● onto another table to merge it · double-click to open · Shift+drag selects", self)
        hint.setObjectName("Dim")
        new_table = QPushButton("+ New table", self)
        new_menu = QMenu(new_table)
        new_menu.aboutToShow.connect(
            lambda: fill_action_menu(new_menu, session.registry, sources=True, chosen=lambda a: self.new_table(a))
        )
        new_table.setMenu(new_menu)
        arrange = QPushButton("Arrange automatically", self)
        arrange.clicked.connect(self.arrange)
        header = QHBoxLayout()
        header.setContentsMargins(6, 4, 6, 2)
        header.addWidget(title)
        header.addSpacing(10)
        header.addWidget(self.search)
        header.addSpacing(10)
        header.addWidget(hint, 1)
        header.addWidget(new_table)
        header.addWidget(arrange)
        self.group_bar = GroupBar(self)

        self.empty = QLabel("No tables yet. Create one with “+ New table”.", self)
        self.empty.setObjectName("Dim")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        board = QWidget(self)
        board_layout = QVBoxLayout(board)
        board_layout.setContentsMargins(0, 0, 0, 0)
        board_layout.setSpacing(0)
        board_layout.addWidget(self.group_bar)
        board_layout.addWidget(self.canvas, 1)
        board_layout.addWidget(self.empty, 1)
        self.steps = StepsPanel(session, self)
        self.settings = SettingsPanel(session, edit_connections, self)
        self.data = DataPanel(session, self)
        self.left = QSplitter(Qt.Orientation.Vertical, self)
        self.left.addWidget(board)
        self.left.addWidget(self.data)
        self.left.setStretchFactor(0, 1)
        self.left.setSizes([540, 300])
        self.right = QSplitter(Qt.Orientation.Vertical, self)
        self.right.addWidget(self.steps)
        self.right.addWidget(self.settings)
        self.right.setSizes([300, 540])
        self.splitter = QSplitter(Qt.Orientation.Horizontal, self)
        self.splitter.addWidget(self.left)
        self.splitter.addWidget(self.right)
        self.splitter.setStretchFactor(0, 1)
        self.splitter.setSizes([1000, 420])

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addLayout(header)
        layout.addWidget(self.splitter, 1)

        session.project_changed.connect(self._project_changed)
        session.structure_changed.connect(self.schedule_rebuild)
        session.layout_changed.connect(self._layout_changed)
        session.results_changed.connect(self.update_statuses)
        session.selection_changed.connect(self.update_highlight)
        self.rebuild()

    # --- building ----------------------------------------------------------------------------

    def schedule_rebuild(self) -> None:
        self._rebuild_timer.start()

    def boxes(self) -> list[Box]:
        return [*self.nodes.values(), *self.group_nodes.values()]

    def group_number(self, group_id: str) -> str:
        """The key that jumps to a group ("1" to "9"; "" for the tenth and later)."""
        ids = [g.id for g in self.session.project.groups]
        number = ids.index(group_id) + 1 if group_id in ids else 0
        return str(number) if 0 < number <= 9 else ""

    def rebuild(self) -> None:
        self._rebuild_timer.stop()
        self._link = None
        self._flashing = None
        self.scene.clear()
        self.nodes, self.group_nodes, self.edges, self.frames = {}, {}, [], {}
        session = self.session
        project = session.project
        collapsed = {g.id for g in project.groups if g.collapsed}
        unit_of = {t.id: _GROUP_UNIT + t.group if t.group in collapsed else t.id for t in project.tables}
        # Arrows between boxes; arrows inside a collapsed group are not drawn.
        links = [link for link in session.engine.links() if unit_of[link.source] != unit_of[link.target]]
        linked_steps = {link.step for link in links}
        connected = {link.source for link in links} | {link.target for link in links}

        boxes: dict[str, Box] = {}
        units: list[Unit] = []
        for table in project.tables:
            if table.group not in collapsed:
                node = TableNode(self, table, node_rows(session, table, linked_steps))
                self.nodes[table.id] = boxes[table.id] = node
                units.append(Unit(table.id, node.height, table.group, table.position))
        for group in project.groups:
            members = project.members(group.id)
            if group.id in collapsed and members:
                box = GroupNode(self, group, group_rows(members, connected), [t.id for t in members])
                self.group_nodes[group.id] = box
                boxes[_GROUP_UNIT + group.id] = box
                position = group.position or _top_left([t.position for t in members if t.position is not None])
                units.append(Unit(_GROUP_UNIT + group.id, box.height, "", position))
        for box in boxes.values():
            self.scene.addItem(box)

        def ends(link: Any) -> tuple[Box, str, Box, str]:
            source, target = boxes[unit_of[link.source]], boxes[unit_of[link.target]]
            source_key = link.source if isinstance(source, GroupNode) else ""
            target_key = link.target if isinstance(target, GroupNode) else link.step
            return source, source_key, target, target_key

        arcs = []
        for link in links:
            source, source_key, target, target_key = ends(link)
            source_offset = source.row_offset(source_key) if isinstance(source, GroupNode) else HEADER / 2
            arcs.append(Arc(unit_of[link.source], unit_of[link.target], source_offset, target.row_offset(target_key)))
        for unit_id, point in place(units, arcs).items():
            boxes[unit_id].setPos(point)

        for group in project.groups:
            members = [self.nodes[t.id] for t in project.members(group.id) if t.id in self.nodes]
            if members:
                frame = GroupFrame(self, group, members)
                self.scene.addItem(frame)
                self.frames[group.id] = frame
        for link in links:
            source, source_key, target, target_key = ends(link)
            names = f"{project.table(link.source).name} → {project.table(link.target).name}"
            step = session.engine.describe(session.find_step(link.step)[1])
            off = "" if link.enabled else "  (step is off)"
            tooltip = f"{names}, step {link.index + 1}: {step}{off}\nClick to edit · right-click for more"
            edge = Edge(self, link, source, source_key, target, target_key, tooltip)
            self.scene.addItem(edge)
            self.edges.append(edge)

        self._grow_scene(reset=True)
        self.canvas.setVisible(bool(project.tables))
        self.group_bar.setVisible(bool(project.tables))
        self.empty.setVisible(not project.tables)
        self.group_bar.rebuild()
        self._names.setStringList(sorted(t.name for t in project.tables))
        self._mark_search_hits()
        self.update_statuses()
        self.update_highlight()
        focus, self._pending_focus = self._pending_focus, None
        if focus is not None and focus in self.nodes:
            self.focus_table(focus)
        else:
            self.refit()

    def update_statuses(self) -> None:
        session = self.session
        for table_id, node in self.nodes.items():
            node.status = session.table_status(table_id)
            result = session.results.get(table_id)
            node.info = {"disabled": "off", "skipped": "skipped"}.get(node.status, "")
            if not node.info and result is not None:
                node.info = f"{result.df.height:,} × {result.df.width}" if result.df is not None else "error"
            node.update()
        for box in self.group_nodes.values():
            statuses = [session.table_status(t) for t in box.members]
            errors = statuses.count("error")
            off = statuses.count("disabled") + statuses.count("skipped")
            if errors:
                box.summary = f"{errors} error{'s' if errors > 1 else ''}"
                box.summary_color = theme.status_color("error")
            elif off:
                box.summary = "all off" if off == len(statuses) else f"{off} off"
                box.summary_color = QColor(theme.TEXT_DIM)
            elif "pending" in statuses:
                box.summary, box.summary_color = "", QColor(theme.TEXT_DIM)
            else:
                box.summary, box.summary_color = "all ok", theme.status_color("ok")
            box.update()
        self.group_bar.update_statuses()

    def update_highlight(self) -> None:
        """Arrows into and out of the selected table are drawn in the accent color."""
        for edge in self.edges:
            edge.refresh_pen()
        for box in self.boxes():
            box.update()

    def update_edges(self) -> None:
        for edge in self.edges:
            edge.update_path()

    def port_highlighted(self, step_id: str) -> bool:
        return any(e.link.step == step_id and e.lit for e in self.edges)

    def fit(self, overview: bool = False) -> None:
        """The whole project in the window (at most 100%). ``overview`` (the automatic fit) doesn't
        zoom out below 30%: a bigger project shows its beginning, top left."""
        bounds = self.scene.itemsBoundingRect()
        viewport = self.canvas.viewport().rect()
        if bounds.isEmpty() or viewport.width() < 50 or viewport.height() < 50:
            return
        self._show(bounds, 1.0, 0.3 if overview else _MIN_ZOOM)

    def refit(self) -> None:
        if self.auto_fit:
            self.fit(overview=True)

    def _show(self, rect: QRectF, max_scale: float, min_scale: float = _MIN_ZOOM) -> None:
        """Zoom so that ``rect`` fills the window, within the limits; if it doesn't fit, show its
        top left part."""
        margin = 40
        viewport = self.canvas.viewport().rect()
        scale = min(viewport.width() / (rect.width() + 2 * margin), viewport.height() / (rect.height() + 2 * margin))
        scale = min(max(scale, min_scale), max_scale)
        self.canvas.resetTransform()
        self.canvas.scale(scale, scale)
        width, height = viewport.width() / scale, viewport.height() / scale
        center = rect.center()
        if width < rect.width() + 2 * margin:
            center.setX(rect.left() - margin + width / 2)
        if height < rect.height() + 2 * margin:
            center.setY(rect.top() - margin + height / 2)
        self.canvas.centerOn(center)

    def _grow_scene(self, reset: bool = False) -> None:
        bounds = self.scene.itemsBoundingRect().adjusted(-400, -300, 600, 400)
        self.scene.setSceneRect(bounds if reset else self.scene.sceneRect().united(bounds))

    def _project_changed(self) -> None:
        self.auto_fit = True
        self.search.clear()
        self.rebuild()

    def _layout_changed(self) -> None:
        if not self._saving:
            self.schedule_rebuild()

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.canvas.setFocus()  # so 1–9, Ctrl+G and Ctrl+F work right away

    # --- positions ---------------------------------------------------------------------------

    def _hand_made(self) -> bool:
        """Whether the map was arranged by hand (else it is arranged automatically)."""
        project = self.session.project
        return any(t.position is not None for t in project.tables) or any(g.position is not None for g in project.groups)

    def _unit_id(self, box: Box) -> str:
        return box.table_id if isinstance(box, TableNode) else _GROUP_UNIT + box.group_id

    def _box_of(self, unit_id: str) -> Box | None:
        if unit_id.startswith(_GROUP_UNIT):
            return self.group_nodes.get(unit_id.removeprefix(_GROUP_UNIT))
        return self.nodes.get(unit_id)

    def _unit_state(
        self, memberships: dict[str, str | None] | None = None
    ) -> tuple[dict[str, QPointF], dict[str, float], dict[str, str]]:
        """Positions, heights and groups of the boxes on the map (``memberships`` applied)."""
        project = self.session.project
        memberships = memberships or {}
        positions: dict[str, QPointF] = {}
        heights: dict[str, float] = {}
        groups: dict[str, str] = {}
        for table_id, node in self.nodes.items():
            positions[table_id], heights[table_id] = node.pos(), node.height
            group = memberships[table_id] if table_id in memberships else project.table(table_id).group
            groups[table_id] = group or ""
        for group_id, box in self.group_nodes.items():
            unit = _GROUP_UNIT + group_id
            positions[unit], heights[unit], groups[unit] = box.pos(), box.height, ""
        return positions, heights, groups

    def _apply(self, positions: dict[str, QPointF]) -> None:
        for unit_id, point in positions.items():
            box = self._box_of(unit_id)
            if box is not None and box.pos() != point:
                box.setPos(point)
        for frame in self.frames.values():
            frame.update_rect()
        self.update_edges()

    def save_positions(self, memberships: dict[str, str | None] | None = None, unplaced: Iterable[str] = ()) -> None:
        """Keep the arrangement as it is now. ``memberships`` changes groups in the same undo
        step; ``unplaced`` tables forget their place (they went into a collapsed group)."""
        project = self.session.project
        positions = {t.id: t.position for t in project.tables if t.id not in self.nodes and t.position is not None}
        positions.update({tid: (node.pos().x(), node.pos().y()) for tid, node in self.nodes.items()})
        for table_id in unplaced:
            positions.pop(table_id, None)
        group_positions = {gid: (box.pos().x(), box.pos().y()) for gid, box in self.group_nodes.items()}
        self._saving = True
        try:
            self.session.set_positions(positions, memberships, group_positions)
        finally:
            self._saving = False
        for frame in self.frames.values():
            frame.update_rect()
        self.update_edges()
        self._grow_scene()

    def arrange(self) -> None:
        self.session.set_positions(None)
        self.auto_fit = True
        self.rebuild()

    # --- dragging boxes and groups -----------------------------------------------------------

    def box_pressed(self, box: Box) -> None:
        if isinstance(box, TableNode):
            self.session.select_table(box.table_id)
        self._grabbed = box
        self._drag_frames = {gid: QRectF(frame.rect) for gid, frame in self.frames.items()}
        dragged = {n.table_id for n in self._dragged(box) if isinstance(n, TableNode)}
        self._drag_groups = {gid for gid, frame in self.frames.items() if all(m.table_id in dragged for m in frame.members)}

    def box_moving(self, box: Box) -> None:
        for edge in box.edges:  # only this box's arrows while dragging; all of them on release
            edge.update_path()
        if box is self._grabbed and isinstance(box, TableNode) and self._drag_frames:
            target = self._drop_group(box, self._drag_frames)
            joining = target is not None and target != self._group_of(box)
            for gid, frame in self.frames.items():
                frame.set_drop_target(joining and gid == target)

    def box_released(self, box: Box, moved: bool) -> None:
        frames, self._drag_frames, self._grabbed = self._drag_frames, {}, None
        for frame in self.frames.values():
            frame.set_drop_target(False)
        if not moved:
            return
        self.auto_fit = False
        dragged = self._dragged(box)
        memberships = {b.table_id: self._drop_group(b, frames) for b in dragged if isinstance(b, TableNode)}
        hidden = [t for t, g in memberships.items() if g in self.group_nodes]  # dropped onto a collapsed group
        positions, heights, groups = self._unit_state(memberships)
        for table_id in hidden:
            del positions[table_id]
        moved_units = [u for u in map(self._unit_id, dragged) if u in positions]
        self._apply(tidy(positions, heights, groups, moved=moved_units))
        self.save_positions(memberships, unplaced=hidden)

    def frame_moved(self, group_id: str) -> None:
        """A group was dragged by its title: what it now covers moves down."""
        self.auto_fit = False
        self._apply(tidy(*self._unit_state()))
        self.save_positions()

    def _dragged(self, box: Box) -> list[Box]:
        dragged = [b for b in self.boxes() if b.isSelected()]
        return dragged if box in dragged else [*dragged, box]

    def _group_of(self, node: TableNode) -> str | None:
        return self.session.project.table(node.table_id).group or None

    def _drop_group(self, node: TableNode, frames: dict[str, QRectF]) -> str | None:
        """The group of a table dropped where it is now: the frame its center is in (its own
        first) or the collapsed group it is on; None elsewhere. A group dragged as a whole
        stays as it is."""
        own = self._group_of(node)
        if own in self._drag_groups:
            return own
        center = node.box_rect().center()
        if own in frames and frames[own].contains(center):
            return own
        inside = next((gid for gid, rect in frames.items() if rect.contains(center)), None)
        if inside is not None:
            return inside
        return next((gid for gid, box in self.group_nodes.items() if box.box_rect().contains(center)), None)

    # --- connecting tables -------------------------------------------------------------------

    def start_link(self, node: TableNode) -> None:
        self._link_source = node
        self._link = LinkDrag(node.out_anchor())
        self._link.move_to(node.out_anchor())
        self.scene.addItem(self._link)

    def move_link(self, position: QPointF) -> None:
        if self._link is None:
            return
        self._link.move_to(position)
        target = self.node_at(position, exclude=self._link_source)
        for node in self.nodes.values():
            if node.link_target != (node is target):
                node.link_target = node is target
                node.update()

    def finish_link(self, node: TableNode, scene_pos: QPointF, screen_pos: QPoint) -> None:
        if self._link is not None:
            self.scene.removeItem(self._link)
            self._link = None
        for other in self.nodes.values():
            if other.link_target:
                other.link_target = False
                other.update()
        target = self.node_at(scene_pos, exclude=node)
        if target is not None:
            kind = self.choose_link_kind(node, target, screen_pos)
            if kind:
                QTimer.singleShot(0, lambda: self.add_link(node.table_id, target.table_id, kind))
        elif (scene_pos - node.out_anchor()).manhattanLength() > 40 and self.box_at(scene_pos) is None:
            position, group = self._new_table_spot(scene_pos - QPointF(0, HEADER / 2))
            QTimer.singleShot(0, lambda: self.session.add_derived_table(node.table_id, position, group))

    def choose_link_kind(self, source: TableNode, target: TableNode, screen_pos: QPoint) -> str | None:
        """Ask how ``target`` should use ``source``."""
        menu = QMenu(self)
        menu.addAction(f"{target.name} uses {source.name}:").setEnabled(False)
        for choice in LINK_CHOICES:
            if choice is None:
                menu.addSeparator()
            else:
                menu.addAction(choice[1]).setData(choice[0])
        chosen = menu.exec(screen_pos)
        return chosen.data() if chosen is not None else None

    def add_link(self, source_id: str, target_id: str, kind: str) -> None:
        step = self.session.add_link(source_id, target_id, kind)
        target = self.session.project.table(target_id)
        number = target.steps.index(step) + 1
        keyless = step.action != "table.append" and not step.params.get("match", [{}])[0].get("column")
        hint = " — choose the matching columns on the right" if keyless else ""
        self.message.emit(f"Added step {number} to “{target.name}”: {self.session.engine.describe(step)}{hint}")

    def node_at(self, position: QPointF, exclude: TableNode | None = None) -> TableNode | None:
        for item in self.scene.items(position):
            if isinstance(item, TableNode) and item is not exclude:
                return item
        return None

    def box_at(self, position: QPointF) -> Box | None:
        return next((i for i in self.scene.items(position) if isinstance(i, Box)), None)

    # --- arrows ------------------------------------------------------------------------------

    def edge_clicked(self, edge: Edge) -> None:
        self.session.select_table(edge.link.target)
        self.session.select_step(edge.link.step)

    def edge_menu(self, edge: Edge, screen_pos: QPoint) -> None:
        _, step = self.session.find_step(edge.link.step)
        menu = QMenu(self)
        menu.addAction("Edit this step", lambda: self.edge_clicked(edge))
        menu.addAction(
            "Turn step off" if step.enabled else "Turn step on", lambda: self.session.set_enabled(step.id, not step.enabled)
        )
        menu.addSeparator()
        menu.addAction("Delete step\tDel", lambda: self.delete_steps([step.id]))
        menu.exec(screen_pos)

    def delete_steps(self, step_ids: list[str]) -> None:
        for step_id in step_ids:
            table, step = self.session.find_step(step_id)
            number = table.steps.index(step) + 1
            self.session.delete_step(step_id)
            self.message.emit(f"Deleted step {number} of “{table.name}” (Ctrl+Z brings it back)")

    # --- tables ------------------------------------------------------------------------------

    def new_table(self, action: Action, position: QPointF | None = None) -> None:
        """A new table; put at ``position`` (or below, if taken), it joins the group there."""
        if position is None:
            self.session.add_table(action)
        else:
            spot, group = self._new_table_spot(position)
            self.session.add_table(action, position=spot, group=group)

    def _new_table_spot(self, point: QPointF) -> tuple[tuple[float, float], str | None]:
        group = next((gid for gid, frame in self.frames.items() if frame.rect.contains(point)), None)
        positions, heights, groups = self._unit_state()
        spot = free_spot(point, node_height([NodeRow("")]), group or "", positions, heights, groups)
        return (spot.x(), spot.y()), group

    def selected_tables(self) -> list[str]:
        return [n.table_id for n in self.nodes.values() if n.isSelected()]

    def node_menu(self, node: TableNode, screen_pos: QPoint) -> None:
        table_id = node.table_id
        selected = self.selected_tables() or [table_id]
        project = self.session.project
        menu = QMenu(self)
        if len(selected) > 1:
            all_on = all(project.table(t).enabled for t in selected)
            menu.addAction(f"Group these {len(selected)} tables…\tCtrl+G", lambda: self.group_tables(selected))
            menu.addAction(
                f"Turn these {len(selected)} tables {'off' if all_on else 'on'}",
                lambda: self.session.set_table_enabled(selected, not all_on),
            )
            menu.addSeparator()
        menu.addAction("Open in table view", lambda: self.open_table.emit(table_id, ""))
        fill_action_menu(
            menu.addMenu("Add step"), self.session.registry, sources=False, chosen=lambda a: self.add_step(table_id, a)
        )
        menu.addAction("New table starting from this one", lambda: self.derive_table(node))
        self.fill_group_menu(menu.addMenu("Group"), selected)
        if len(selected) == 1:
            enabled = project.table(table_id).enabled
            menu.addAction(
                "Turn table off" if enabled else "Turn table on",
                lambda: self.session.set_table_enabled([table_id], not enabled),
            )
        menu.addSeparator()
        menu.addAction("Rename…\tF2", lambda: self.rename_table(table_id))
        menu.addAction("Duplicate", lambda: self.session.duplicate_table(table_id))
        menu.addAction("Run this table", lambda: self.session.run_project([table_id]))
        menu.addSeparator()
        menu.addAction("Delete…\tDel", lambda: self.delete_tables(selected))
        menu.exec(screen_pos)

    def derive_table(self, node: TableNode) -> None:
        position, group = self._new_table_spot(QPointF(node.pos().x() + NODE_WIDTH + GAP_X, node.pos().y()))
        self.session.add_derived_table(node.table_id, position, group)

    def add_step(self, table_id: str, action: Action) -> None:
        self.session.select_table(table_id)
        self.session.add_step(action)

    def rename_table(self, table_id: str) -> None:
        table = self.session.project.table(table_id)
        name, ok = QInputDialog.getText(self, "Rename table", "Name:", text=table.name)
        if ok:
            try:
                self.session.rename_table(table_id, name)
            except ValueError as e:
                QMessageBox.warning(self, "Rename table", str(e))

    def delete_tables(self, table_ids: list[str]) -> None:
        if len(table_ids) == 1:
            confirm_delete_table(self, self.session, table_ids[0])
            return
        names = ", ".join(self.session.project.table(t).name for t in table_ids)
        text = f"Delete these {len(table_ids)} tables?\n\n{names}\n\n(Edit → Undo brings them back.)"
        if QMessageBox.question(self, "Delete tables", text) == QMessageBox.StandardButton.Yes:
            self.session.delete_tables(table_ids)

    # --- finding and jumping -----------------------------------------------------------------

    def focus_table(self, table_id: str) -> None:
        """Select a table and show it (expanding its group if that is collapsed)."""
        project = self.session.project
        table = project.find_table(table_id)
        if table is None:
            return
        self.session.select_table(table.id)
        node = self.nodes.get(table.id)
        if node is None:
            group = project.group(table.group)
            if group is not None and group.collapsed:
                self._pending_focus = table.id
                self.collapse([group.id], False)
            return
        self.auto_fit = False
        scale = min(max(self.canvas.transform().m11(), 0.8), 1.2)
        self.canvas.resetTransform()
        self.canvas.scale(scale, scale)
        self.canvas.centerOn(node.box_rect().center())
        self.scene.clearSelection()
        node.setSelected(True)

    def jump_to_group(self, group_id: str) -> None:
        group = self.session.project.group(group_id)
        if group is None:
            return
        count = len(self.session.project.members(group_id))
        frame, box = self.frames.get(group_id), self.group_nodes.get(group_id)
        if frame is None and box is None:
            self.message.emit(f"Group “{group.name}” has no tables yet")
            return
        self.auto_fit = False
        self._show(frame.rect if frame is not None else box.box_rect(), 1.0)
        self.scene.clearSelection()
        if frame is not None:
            self._flash(frame)
        else:
            box.setSelected(True)
        self.message.emit(f"Group “{group.name}”: {count} table{'s' if count != 1 else ''}")
        self.canvas.setFocus()

    def _flash(self, frame: GroupFrame) -> None:
        self._end_flash()
        frame.set_drop_target(True)
        self._flashing = frame
        self._flash_timer.start()

    def _end_flash(self) -> None:
        if self._flashing is not None and self._flashing in self.frames.values():
            self._flashing.set_drop_target(False)
        self._flashing = None

    def _search_changed(self, _text: str) -> None:
        self._search_index = -1
        self._mark_search_hits()

    def _mark_search_hits(self) -> None:
        text = self.search.text().strip().lower()
        self._search_hits = [t.id for t in self.session.project.tables if text and text in t.name.lower()]
        hits = set(self._search_hits)
        for table_id, node in self.nodes.items():
            if node.search_match != (table_id in hits):
                node.search_match = table_id in hits
                node.update()

    def find_next(self) -> None:
        """Jump to the next table whose name contains the search text."""
        text = self.search.text().strip()
        if not self._search_hits:
            if text:
                self.message.emit(f"No table name contains “{text}”")
            return
        self._search_index = (self._search_index + 1) % len(self._search_hits)
        self.focus_table(self._search_hits[self._search_index])
        if len(self._search_hits) > 1:
            self.message.emit(
                f"Table {self._search_index + 1} of {len(self._search_hits)} containing “{text}” · Enter: next one"
            )

    def find_table(self, name: str) -> None:
        table = self.session.project.find_table(name)
        if table is not None:
            self.focus_table(table.id)

    # --- groups ------------------------------------------------------------------------------

    def fill_group_menu(self, menu: QMenu, table_ids: list[str]) -> None:
        project = self.session.project
        current = {project.table(t).group for t in table_ids}
        for group in project.groups:
            entry = menu.addAction(
                theme.group_icon(group.color), group.name, lambda g=group.id: self.join_group(table_ids, g)
            )
            entry.setCheckable(True)
            entry.setChecked(current == {group.id})
        if project.groups:
            menu.addSeparator()
        menu.addAction("New group…\tCtrl+G", lambda: self.group_tables(table_ids))
        if current != {""}:
            menu.addAction("Remove from group", lambda: self.leave_group(table_ids))

    def group_selected(self) -> None:
        selected = self.selected_tables()
        if not selected:
            self.message.emit("Select tables first: Ctrl+click them, or Shift+drag a rectangle around them")
            return
        self.group_tables(selected)

    def group_tables(self, table_ids: list[str]) -> None:
        name, ok = QInputDialog.getText(
            self, "New group", "Name of the group:", text=f"Group {len(self.session.project.groups) + 1}"
        )
        if ok and name.strip():
            self.create_group(name, table_ids)

    def create_group(self, name: str, table_ids: list[str]) -> Group:
        """A new group of these tables. On a map arranged by hand, tables that are far apart are
        gathered, and what the new frame covers moves down."""
        positions = None
        if self._hand_made():
            nodes = [self.nodes[t] for t in table_ids if t in self.nodes]
            state, heights, groups = self._unit_state(dict.fromkeys(table_ids, _NEW_GROUP))
            if self._covers_others(nodes):
                state.update(self._gathered(nodes))
            result = tidy(state, heights, groups, moved=[n.table_id for n in nodes])
            positions = {u: (p.x(), p.y()) for u, p in result.items() if u in self.nodes}
        group = self.session.add_group(name, table_ids, positions=positions)
        key = self.group_number(group.id)
        self.message.emit(f"Group “{group.name}” created" + (f": press {key} on the map to jump to it" if key else ""))
        return group

    def join_group(self, table_ids: list[str], group_id: str) -> None:
        """Put tables into a group; on a map arranged by hand they move below its tables."""
        project = self.session.project
        group = project.group(group_id)
        if group is None:
            return
        memberships: dict[str, str | None] = dict.fromkeys(table_ids, group_id)
        if not self._hand_made():
            self.session.set_table_group(table_ids, group_id)
            return
        if group.collapsed:
            self.save_positions(memberships, unplaced=table_ids)
            return
        members = [self.nodes[t.id] for t in project.members(group_id) if t.id not in table_ids and t.id in self.nodes]
        joining = [self.nodes[t] for t in table_ids if t in self.nodes]
        if members:
            x = min(m.pos().x() for m in members)
            y = max(m.pos().y() + m.height for m in members) + GAP_Y
            for node in joining:
                node.setPos(x, y)
                y += node.height + GAP_Y
        positions, heights, groups = self._unit_state(memberships)
        self._apply(tidy(positions, heights, groups, moved=[n.table_id for n in joining]))
        self.save_positions(memberships)

    def leave_group(self, table_ids: list[str]) -> None:
        """Take tables out of their group; on a map arranged by hand they move out of its frame."""
        if not self._hand_made():
            self.session.set_table_group(table_ids, None)
            return
        memberships: dict[str, str | None] = dict.fromkeys(table_ids)
        positions, heights, groups = self._unit_state(memberships)
        self._apply(tidy(positions, heights, groups, moved=table_ids))
        self.save_positions(memberships)

    def _covers_others(self, nodes: list[TableNode]) -> bool:
        """Whether a frame around these boxes would cover other boxes."""
        if not nodes:
            return False
        frame = QRectF()
        for node in nodes:
            frame = frame.united(node.box_rect())
        frame.adjust(-GROUP_PAD, -GROUP_PAD - GROUP_TITLE, GROUP_PAD, GROUP_PAD)
        return any(b not in nodes and b.box_rect().intersects(frame) for b in self.boxes())

    def _gathered(self, nodes: list[TableNode]) -> dict[str, QPointF]:
        """The tables arranged as a block (columns in data-flow order) where the top left one is."""
        ids = {n.table_id for n in nodes}
        arcs = [
            Arc(link.source, link.target, HEADER / 2, self.nodes[link.target].row_offset(link.step))
            for link in self.session.engine.links()
            if link.source in ids and link.target in ids
        ]
        block = auto_layout([Unit(n.table_id, n.height) for n in nodes], arcs)
        corner = QPointF(min(n.pos().x() for n in nodes), min(n.pos().y() for n in nodes))
        shift = corner - QPointF(min(p.x() for p in block.values()), min(p.y() for p in block.values()))
        return {unit_id: point + shift for unit_id, point in block.items()}

    def set_collapsed(self, group_id: str, collapsed: bool) -> None:
        self.collapse([group_id], collapsed)

    def collapse(self, group_ids: list[str], collapsed: bool) -> None:
        """Collapse groups into one box each (where their tables were), or expand them (their
        tables where the box is; what is in the way moves down)."""
        if not self._hand_made():
            self.session.set_groups_collapsed(group_ids, collapsed)
            return
        project = self.session.project
        if collapsed:
            positions = {tid: (node.pos().x(), node.pos().y()) for tid, node in self.nodes.items()}
            group_positions = {gid: (box.pos().x(), box.pos().y()) for gid, box in self.group_nodes.items()}
            for group_id in group_ids:
                members = [self.nodes[t.id] for t in project.members(group_id) if t.id in self.nodes]
                if members:
                    group_positions[group_id] = (min(m.pos().x() for m in members), min(m.pos().y() for m in members))
            self.session.set_groups_collapsed(group_ids, True, positions, group_positions)
            return
        state, heights, groups = self._unit_state()
        for group_id in group_ids:
            box = self.group_nodes.get(group_id)
            if box is None:
                continue
            del state[_GROUP_UNIT + group_id]
            for table_id, (point, height) in self._unfolded(group_id, box.pos()).items():
                state[table_id], heights[table_id], groups[table_id] = point, height, group_id
        result = tidy(state, heights, groups)
        positions = {u: (p.x(), p.y()) for u, p in result.items() if not u.startswith(_GROUP_UNIT)}
        group_positions = {
            u.removeprefix(_GROUP_UNIT): (p.x(), p.y()) for u, p in result.items() if u.startswith(_GROUP_UNIT)
        }
        self.session.set_groups_collapsed(group_ids, False, positions, group_positions)

    def _unfolded(self, group_id: str, origin: QPointF) -> dict[str, tuple[QPointF, float]]:
        """Where the tables of a collapsed group go when it is expanded at ``origin`` (and how
        tall they are): as they were before, or arranged automatically."""
        session = self.session
        members = session.project.members(group_id)
        links = session.engine.links()
        linked_steps = {link.step for link in links}
        heights = {t.id: node_height(node_rows(session, t, linked_steps)) for t in members}
        spots = {t.id: QPointF(*t.position) for t in members if t.position is not None}
        if spots:  # tables without a place go below the others
            x = min(p.x() for p in spots.values())
            y = max(spots[t].y() + heights[t] for t in spots) + GAP_Y
            for table in members:
                if table.id not in spots:
                    spots[table.id] = QPointF(x, y)
                    y += heights[table.id] + GAP_Y
        else:
            arcs = [Arc(link.source, link.target) for link in links if link.source in heights and link.target in heights]
            spots = auto_layout([Unit(t.id, heights[t.id]) for t in members], arcs)
        shift = origin - QPointF(min(p.x() for p in spots.values()), min(p.y() for p in spots.values()))
        return {table_id: (point + shift, heights[table_id]) for table_id, point in spots.items()}

    def select_group(self, group_id: str) -> None:
        frame = self.frames.get(group_id)
        if frame is not None:
            self.scene.clearSelection()
            for member in frame.members:
                member.setSelected(True)

    def rename_group(self, group_id: str) -> None:
        group = self.session.project.group(group_id)
        if group is not None:
            name, ok = QInputDialog.getText(self, "Rename group", "Name:", text=group.name)
            if ok:
                self.session.rename_group(group_id, name)

    def group_menu(self, group_id: str, screen_pos: QPoint) -> None:
        project = self.session.project
        group = project.group(group_id)
        if group is None:
            return
        members = [t.id for t in project.members(group_id)]
        all_on = all(project.table(t).enabled for t in members)
        selected = [t for t in self.selected_tables() if t not in members]
        key = self.group_number(group_id)
        menu = QMenu(self)
        menu.addAction("Jump to group" + (f"\t{key}" if key else ""), lambda: self.jump_to_group(group_id))
        menu.addAction(
            "Expand" if group.collapsed else "Collapse into one box",
            lambda: self.set_collapsed(group_id, not group.collapsed),
        )
        if group_id in self.frames:
            menu.addAction("Select its tables", lambda: self.select_group(group_id))
        if selected:
            menu.addAction(
                f"Add the {len(selected)} selected table{'s' if len(selected) > 1 else ''}",
                lambda: self.join_group(selected, group_id),
            )
        menu.addSeparator()
        menu.addAction("Rename…", lambda: self.rename_group(group_id))
        colors = menu.addMenu("Color")
        for name in theme.GROUP_COLORS:
            entry = colors.addAction(
                theme.group_icon(name), name.capitalize(), lambda c=name: self.session.set_group_color(group_id, c)
            )
            entry.setCheckable(True)
            entry.setChecked(name == group.color)
        if members:
            menu.addAction(
                f"Turn all its tables {'off' if all_on else 'on'}",
                lambda: self.session.set_table_enabled(members, not all_on),
            )
        menu.addSeparator()
        menu.addAction("Ungroup (the tables stay)", lambda: self.session.delete_group(group_id))
        menu.exec(screen_pos)

    # --- canvas ------------------------------------------------------------------------------

    def canvas_menu(self, screen_pos: QPoint, scene_pos: QPointF) -> None:
        selected = self.selected_tables()
        groups = self.session.project.groups
        menu = QMenu(self)
        fill_action_menu(
            menu.addMenu("New table here"),
            self.session.registry,
            sources=True,
            chosen=lambda a: self.new_table(a, scene_pos - QPointF(0, HEADER / 2)),
        )
        if selected:
            menu.addAction(f"Group the {len(selected)} selected tables…\tCtrl+G", lambda: self.group_tables(selected))
        menu.addSeparator()
        if any(not g.collapsed for g in groups):
            menu.addAction("Collapse all groups", lambda: self.collapse([g.id for g in groups], True))
        if any(g.collapsed for g in groups):
            menu.addAction("Expand all groups", lambda: self.collapse([g.id for g in groups], False))
        menu.addAction("Arrange automatically", self.arrange)
        menu.addAction("Show all\t0", lambda: self.fit())
        menu.exec(screen_pos)

    def key_pressed(self, event: Any) -> bool:
        key, text = event.key(), event.text()
        control = bool(event.modifiers() & Qt.KeyboardModifier.ControlModifier)
        selected_edges = [e for e in self.edges if e.isSelected()]
        selected_tables = self.selected_tables()
        if key in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            if selected_edges:
                self.delete_steps([e.link.step for e in selected_edges])
            elif selected_tables:
                self.delete_tables(selected_tables)
            return True
        if control and key == Qt.Key.Key_G:
            self.group_selected()
            return True
        if control and key == Qt.Key.Key_F:
            self.search.setFocus()
            self.search.selectAll()
            return True
        if not control and len(text) == 1 and text.isdigit():
            number = int(text)
            groups = self.session.project.groups
            if number == 0:
                self.fit()
            elif number <= len(groups):
                self.jump_to_group(groups[number - 1].id)
            return True
        if key == Qt.Key.Key_F2 and self.session.table_id:
            self.rename_table(self.session.table_id)
            return True
        if key == Qt.Key.Key_Escape:
            self.scene.clearSelection()
            self.search.clear()
            return True
        return False


def _top_left(points: list[tuple[float, float]]) -> tuple[float, float] | None:
    if not points:
        return None
    return min(p[0] for p in points), min(p[1] for p in points)

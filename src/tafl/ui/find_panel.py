"""The Find panel beside the tables and the map: finds steps by their texts (table and action
names, files, folders, sheets, SQL, formulas, column names, notes, connections, parameters). With
an empty search it lists what the project reads, writes and changes outside of it."""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from PySide6.QtCore import (
    QAbstractItemModel,
    QEvent,
    QItemSelectionModel,
    QModelIndex,
    QObject,
    QPoint,
    QRect,
    QSettings,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QActionGroup, QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLayoutItem,
    QLineEdit,
    QMenu,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QToolButton,
    QTreeView,
    QVBoxLayout,
    QWidget,
)

from tafl.core import Project, Status
from tafl.core import index as project_index
from tafl.core.index import Hit, Line, Query, Section

from . import theme
from .labels import ElidedLabel
from .session import Session, _rebased, count, missing_settings, waiting_text

_TYPING_MS = 150  # a search starts this long after the last key
_LATER_MS = 200  # and again this long after the project or the results changed
_GROUPINGS = {"kind": "Kind", "table": "Table", "place": "Place"}
_STATUS_WORDS = ("error", "warning", "incomplete", "waiting", "off")
_RANK = {"label": 1, "parameter": 2, "match": 3}  # which mark wins where they overlap
_NAME_SHOWN = 14  # characters of a parameter's name on the "Uses {…}" button
_TITLE_ROOM = 140  # pixels a result's title keeps before its group is shown beside it
_LEFT = Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter
_RIGHT = Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter

_STYLE = f"""
QToolButton#Toggle {{
    border: 1px solid {theme.BORDER_LIGHT}; border-radius: 4px; padding: 2px 7px; background: {theme.RAISED};
}}
QToolButton#Toggle:hover {{ background: {theme.BORDER_LIGHT}; }}
QToolButton#Toggle:checked {{ background: {theme.SELECTION}; border-color: {theme.ACCENT}; color: {theme.TEXT}; }}
QToolButton#Toggle:disabled {{ color: {theme.TEXT_DISABLED}; background: transparent; }}
QToolButton#Toggle[popupMode="1"] {{ padding-right: 18px; }}
QToolButton#Toggle::menu-button {{ border-left: 1px solid {theme.BORDER_LIGHT}; width: 14px; }}
"""


def step_problems(session: Session) -> dict[str, tuple[str, str]]:
    """step id -> (kind, message) for the Problems filter, from the session's results."""
    problems: dict[str, tuple[str, str]] = {}
    for table in session.project.tables:
        result = session.results.get(table.id)
        if result is None or result.skipped or not table.enabled:
            continue
        if result.error is not None:  # the table as a whole: shown at its first step
            if table.steps:
                problems[table.steps[0].id] = ("error", result.error)
            continue
        for outcome in result.steps:
            if outcome.status == Status.ERROR:
                if needed := missing_settings(outcome):
                    problems[outcome.step_id] = ("incomplete", f"fill in {', '.join(needed)}")
                elif outcome.inherited and (waiting := session.waits_for(table.id)) is not None:
                    problems[outcome.step_id] = ("waiting", waiting_text(waiting))
                else:
                    problems[outcome.step_id] = ("error", outcome.error or "Error")
            elif outcome.status != Status.BLOCKED:  # a blocked step: the step before it has the error
                warning = next((text for level, text in outcome.messages if level == "warning"), None)
                if warning is not None:
                    problems[outcome.step_id] = ("warning", warning)
    return problems


# --- the model ---------------------------------------------------------------------------------


class _Node:
    """A row of the list: a section (a header) or a result, with its place in the tree."""

    __slots__ = ("children", "item", "parent", "row")

    def __init__(self, item: Section | Hit, parent: _Node | None, row: int) -> None:
        self.item = item
        self.parent = parent
        self.row = row
        self.children: list[_Node] = []


class FindModel(QAbstractItemModel):
    """The sections and results of a search as a tree; ``Qt.UserRole`` gives the Section or the
    Hit. The search, the filters and the order come from ``tafl.core.index``."""

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.roots: list[_Node] = []
        self._shown: tuple = ()
        self._results: list[_Node] = []  # in list order

    def set_sections(self, sections: list[Section]) -> bool:
        """Show these sections. False (and the model stays as it is) when they look the same as
        the ones shown: the current row and the scroll position are kept."""
        shown = _look(sections)
        if shown == self._shown:
            _swap(self.roots, sections)  # the same rows, with the newest entries in them
            return False
        self.beginResetModel()
        self.roots = _nodes(sections, None)
        self._results = [node for root in self.roots for node in _walk(root) if isinstance(node.item, Hit)]
        self._shown = shown
        self.endResetModel()
        return True

    def results(self) -> list[_Node]:
        return list(self._results)

    def index_of(self, node: _Node) -> QModelIndex:
        return self.createIndex(node.row, 0, node)

    def node(self, index: QModelIndex) -> _Node | None:
        return index.internalPointer() if index.isValid() else None

    def index(self, row: int, column: int, parent: QModelIndex = QModelIndex()) -> QModelIndex:  # noqa: B008
        nodes = self._children(parent)
        if column != 0 or not 0 <= row < len(nodes):
            return QModelIndex()
        return self.createIndex(row, 0, nodes[row])

    def parent(self, child: QModelIndex | None = None) -> Any:
        if child is None:  # QObject.parent()
            return super().parent()
        node = self.node(child)
        if node is None or node.parent is None:
            return QModelIndex()
        return self.createIndex(node.parent.row, 0, node.parent)

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return len(self._children(parent)) if parent.column() <= 0 else 0

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:  # noqa: B008
        return 1

    def flags(self, index: QModelIndex) -> Qt.ItemFlag:
        node = self.node(index)
        if node is None:
            return Qt.ItemFlag.NoItemFlags
        if isinstance(node.item, Hit):
            return Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable
        return Qt.ItemFlag.ItemIsEnabled

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        node = self.node(index)
        if node is None:
            return None
        item = node.item
        if role == Qt.ItemDataRole.UserRole:
            return item
        if role == Qt.ItemDataRole.DisplayRole:
            return f"{item.title.text}\n{item.line.text}" if isinstance(item, Hit) else f"{item.title} ({item.count})"
        if role == Qt.ItemDataRole.ToolTipRole:
            return item.tooltip or None
        return None

    def _children(self, parent: QModelIndex) -> list[_Node]:
        node = self.node(parent)
        return self.roots if node is None else node.children


def _nodes(sections: list[Section], parent: _Node | None) -> list[_Node]:
    nodes = []
    for section in sections:
        node = _Node(section, parent, len(nodes))
        node.children = _nodes(section.sections, node)
        node.children += [_Node(hit, node, len(node.children) + n) for n, hit in enumerate(section.hits)]
        nodes.append(node)
    return nodes


def _walk(node: _Node):
    yield node
    for child in node.children:
        yield from _walk(child)


def _key(node: _Node) -> tuple[str, str]:
    """(section key, step id) of a result: the same in a new list of the same search."""
    return (node.parent.item.key if node.parent is not None else "", node.item.entry.step_id)


def _look(sections: list[Section]) -> tuple:
    """What the list shows of the sections: equal looks need no new model."""
    return tuple(
        (
            s.key,
            s.title,
            s.color,
            s.tooltip,
            _look(s.sections),
            tuple(
                (h.entry.step_id, h.entry.kind, h.entry.group, h.entry.enabled and h.entry.table_enabled, h.title, h.line)
                + (h.status, h.focus, h.tooltip)
                for h in s.hits
            ),
        )
        for s in sections
    )


def _swap(nodes: list[_Node], sections: list[Section]) -> None:
    """Put the new sections and results into the nodes of the same looking ones."""
    for node, section in zip(nodes, sections):
        node.item = section
        _swap(node.children[: len(section.sections)], section.sections)
        for child, hit in zip(node.children[len(section.sections) :], section.hits):
            child.item = hit


# --- painting ----------------------------------------------------------------------------------


@lru_cache(maxsize=4096)
def _runs(line: Line) -> tuple[tuple[str, str], ...]:
    """The text of a line in parts of one kind each ("" for plain text)."""
    text = line.text
    if not line.marks:
        return ((text, ""),)
    ranks = [0] * len(text)
    for start, end, kind in line.marks:
        rank = _RANK.get(kind, 0)
        for position in range(max(start, 0), min(end, len(text))):
            if rank > ranks[position]:
                ranks[position] = rank
    kinds = {rank: kind for kind, rank in _RANK.items()} | {0: ""}
    runs: list[tuple[str, str]] = []
    start = 0
    for position in range(1, len(text) + 1):
        if position == len(text) or ranks[position] != ranks[start]:
            runs.append((text[start:position], kinds[ranks[start]]))
            start = position
    return tuple(runs)


class ResultDelegate(QStyledItemDelegate):
    """Headers in one line (bold, with the count); results in two: the kind mark, table · step ·
    action, group and status; below it the setting that matched, the match emphasised."""

    groups = True  # the group of a result's table (not while the list is grouped by table)

    def sizeHint(self, option: QStyleOptionViewItem, index: QModelIndex) -> QSize:
        line = QFontMetrics(option.font).height()
        if isinstance(index.data(Qt.ItemDataRole.UserRole), Hit):
            return QSize(120, 2 * line + 10)
        return QSize(120, line + 8)

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index: QModelIndex) -> None:
        item = index.data(Qt.ItemDataRole.UserRole)
        painter.save()
        if isinstance(item, Hit):
            self._paint_hit(painter, option, item)
        elif isinstance(item, Section):
            self._paint_section(painter, option, item)
        painter.restore()

    def _paint_section(self, painter: QPainter, option: QStyleOptionViewItem, section: Section) -> None:
        rect = option.rect
        line = QFontMetrics(option.font).height()
        left, right = rect.left() + 2, rect.right() - 6
        top = rect.top() + (rect.height() - line) // 2
        if section.color:
            theme.group_icon(section.color).paint(painter, QRect(left, top + (line - 12) // 2, 12, 12))
            left += 17
        bold = QFont(option.font)
        bold.setBold(True)
        number = f" ({section.count})"
        number_width = QFontMetrics(option.font).horizontalAdvance(number)
        # A folder or file keeps its end, which tells folders below the same long one apart
        path = section.key.startswith(("place:path:", "name:path:"))
        mode = Qt.TextElideMode.ElideMiddle if path else Qt.TextElideMode.ElideRight
        title = QFontMetrics(bold).elidedText(section.title, mode, max(right - left - number_width, 0))
        title_width = QFontMetrics(bold).horizontalAdvance(title)
        painter.setFont(bold)
        painter.setPen(option.palette.text().color())
        painter.drawText(QRect(left, top, title_width + 1, line), _LEFT, title)
        painter.setFont(option.font)
        painter.setPen(QColor(theme.TEXT_DIM))
        painter.drawText(QRect(left + title_width, top, number_width + 1, line), _LEFT, number)

    def _paint_hit(self, painter: QPainter, option: QStyleOptionViewItem, hit: Hit) -> None:
        rect = option.rect
        palette = option.palette
        selected = bool(option.state & QStyle.StateFlag.State_Selected)
        off = not (hit.entry.enabled and hit.entry.table_enabled)
        painter.fillRect(rect, palette.highlight() if selected else palette.base())
        painter.setPen(QPen(QColor(theme.BORDER)))
        painter.drawLine(rect.bottomLeft(), rect.bottomRight())
        text = QColor(theme.TEXT_DISABLED) if off else palette.highlightedText().color() if selected else palette.text().color()
        dim = QColor(theme.TEXT_DISABLED) if off else QColor(theme.SELECTED_DIM if selected else theme.TEXT_DIM)
        font = QFont(option.font)
        bold = QFont(font)
        bold.setBold(True)
        small = QFont(bold)
        small.setPointSizeF(max(font.pointSizeF() - 1, 6))
        metrics = QFontMetrics(font)
        line = metrics.height()
        left, right = rect.left() + 4, rect.right() - 6
        top = rect.top() + 5

        # Line 1: the kind mark, the title, then the group and the status at the right
        mark_width = max(QFontMetrics(small).horizontalAdvance(kind) for kind in project_index.KINDS) + 8
        painter.setFont(small)
        painter.setPen(dim if off or selected else theme.mark_color(hit.entry.kind))  # colors are hard to read on blue
        painter.drawText(QRect(left, top, mark_width, line), _LEFT, hit.entry.kind)
        title_left = left + mark_width
        end = right
        status = hit.status if hit.status in _STATUS_WORDS else ""
        if status:
            width = metrics.horizontalAdvance(status)
            color = theme.status_text_color(status) if status in ("error", "warning") and not (off or selected) else dim
            painter.setFont(font)
            painter.setPen(color)
            painter.drawText(QRect(end - width, top, width + 1, line), _RIGHT, status)
            end -= width + 10
        if hit.entry.group and self.groups and end - title_left >= _TITLE_ROOM + 60:  # else the title needs the room
            room = min((end - title_left) // 3, end - title_left - _TITLE_ROOM)
            group = metrics.elidedText(hit.entry.group, Qt.TextElideMode.ElideRight, room)
            width = metrics.horizontalAdvance(group)
            painter.setFont(font)
            painter.setPen(dim)
            painter.drawText(QRect(end - width, top, width + 1, line), _RIGHT, group)
            end -= width + 10 if width else 0
        colors = {"": text, "match": text if not off else dim, "label": dim, "parameter": dim}
        _paint_line(painter, hit.title, QRect(title_left, top, max(end - title_left, 0), line), font, bold, colors)

        # Line 2, under the title
        parameter = dim if off else text if selected else QColor(theme.FORMULA_COLORS["parameter"])
        colors = {"": text, "match": text if not off else dim, "label": dim, "parameter": parameter}
        _paint_line(painter, hit.line, QRect(title_left, top + line + 2, max(right - title_left, 0), line), font, bold, colors)


def _paint_line(painter: QPainter, line: Line, rect: QRect, font: QFont, bold: QFont, colors: dict[str, QColor]) -> None:
    """A line part by part: matches bold on the match color, cut off with "…" at the right."""
    runs = list(_runs(line))
    fonts = [bold if kind == "match" else font for _, kind in runs]
    widths = [QFontMetrics(shown).horizontalAdvance(text) for (text, _), shown in zip(runs, fonts)]
    dots = QFontMetrics(font).horizontalAdvance("…")
    fits = sum(widths) <= rect.width()
    if not fits and runs[0][1] == "label" and "match" in (kind for _, kind in runs):
        # The first match beyond the right end: the label at the start gives up its middle
        seen = next(n for n, (_, kind) in enumerate(runs) if kind == "match") + 1
        excess = sum(widths[:seen]) - (rect.width() - dots)
        if excess > 0:
            label = QFontMetrics(font).elidedText(runs[0][0], Qt.TextElideMode.ElideMiddle, max(widths[0] - excess, 60))
            runs[0], widths[0] = (label, "label"), QFontMetrics(font).horizontalAdvance(label)
            fits = sum(widths) <= rect.width()
    limit = rect.left() + (rect.width() if fits else max(rect.width() - dots, 0))
    x, color = rect.left(), colors[""]
    for (text, kind), shown, width in zip(runs, fonts, widths):
        cut = x + width > limit
        if cut:
            text = _prefix(QFontMetrics(shown), text, limit - x)
            width = QFontMetrics(shown).horizontalAdvance(text)
        color = colors.get(kind, colors[""])
        if kind == "match" and text:
            painter.fillRect(QRect(x, rect.top(), width, rect.height()), QColor(theme.FIND_MATCH))
        if text:
            painter.setFont(shown)
            painter.setPen(color)
            painter.drawText(QRect(x, rect.top(), width + 1, rect.height()), _LEFT, text)
        x += width
        if cut:
            break
    if not fits and rect.width() >= dots:
        painter.setFont(font)
        painter.setPen(color)
        painter.drawText(QRect(x, rect.top(), dots + 1, rect.height()), _LEFT, "…")


def _prefix(metrics: QFontMetrics, text: str, width: int) -> str:
    """The longest beginning of ``text`` that is at most ``width`` pixels wide."""
    low, high = 0, len(text)
    while low < high:
        middle = (low + high + 1) // 2
        if metrics.horizontalAdvance(text[:middle]) <= width:
            low = middle
        else:
            high = middle - 1
    return text[:low].rstrip()


# --- the panel ---------------------------------------------------------------------------------


class _Flow(QLayout):
    """Buttons in a row that goes on in a second row when the panel is narrow."""

    def __init__(self, spacing: int = 4) -> None:
        super().__init__()
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int) -> QLayoutItem | None:
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int) -> QLayoutItem | None:
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self) -> Qt.Orientation:
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, width: int) -> int:
        return self._place(QRect(0, 0, width, 0), move=False)

    def setGeometry(self, rect: QRect) -> None:
        super().setGeometry(rect)
        self._place(rect, move=True)

    def sizeHint(self) -> QSize:
        items = self._shown()
        width = sum(item.sizeHint().width() for item in items) + self.spacing() * max(len(items) - 1, 0)
        return QSize(width, max((item.sizeHint().height() for item in items), default=0))

    def minimumSize(self) -> QSize:
        items = self._shown()
        width = max((item.minimumSize().width() for item in items), default=0)
        return QSize(width, max((item.sizeHint().height() for item in items), default=0))

    def _shown(self) -> list[QLayoutItem]:
        return [item for item in self._items if not item.isEmpty()]

    def _place(self, rect: QRect, *, move: bool) -> int:
        """Place the items in rows, each centred in its row; returns the height they need."""
        rows: list[list[QLayoutItem]] = [[]]
        x = rect.x()
        for item in self._shown():
            width = item.sizeHint().width()
            if rows[-1] and x + width > rect.x() + rect.width():
                rows.append([])
                x = rect.x()
            rows[-1].append(item)
            x += width + self.spacing()
        y = rect.y()
        for row in rows:
            height = max((item.sizeHint().height() for item in row), default=0)
            x = rect.x()
            for item in row:
                hint = item.sizeHint()
                if move:
                    item.setGeometry(QRect(QPoint(x, y + (height - hint.height()) // 2), hint))
                x += hint.width() + self.spacing()
            y += height + self.spacing()
        return max(y - self.spacing() - rect.y(), 0)


class FindPanel(QWidget):
    """Finds steps by any of their texts; with an empty search, lists all imports, exports and
    commands. A click selects the step (``go_to``); the main window does the selecting."""

    go_to = Signal(str, str)  # table id, step id: select them in the current view
    focus_setting = Signal(str, int, str)  # key, row, field: after go_to, focus the setting
    message = Signal(str)  # for the status bar
    leave = Signal()  # Esc in an empty search field: the focus goes back to the view

    def __init__(self, session: Session, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.session = session
        self.settings = QSettings()
        self.setStyleSheet(_STYLE)
        self._index: project_index.ProjectIndex | None = None
        self._stale = True  # the index is out of date
        self._token = session.engine.reload_token  # the engine's reload token when it was built
        self._opened: Project | None = None  # a copy of the steps as opened (see _take_baseline)
        self._baseline: dict[str, str] = {}  # their signatures
        self._parameter = ""  # the parameter of "Uses {…}" ("" = any)
        self._collapsed: set[str] = set()  # the keys of the sections collapsed by hand
        self._current: tuple[str, str] | None = None  # (section key, step id) of the current result
        # The current result left the list: the keys of the results then, its place among them,
        # and the result itself
        self._gap: tuple[list[tuple[str, str]], int, Hit] | None = None
        self._hits: list[Hit] = []  # the results the list shows, in project order
        self._searched: Query | None = None  # the search the list shows
        self._restoring = False

        self.search = QLineEdit(self)
        self.search.setClearButtonEnabled(True)
        self.search.setPlaceholderText("Find in project…  (Ctrl+Shift+F)")
        self.search.setToolTip(
            "Names of tables and steps, files, folders, sheets, SQL, formulas, column names, notes, "
            "connections and parameters.\nAll words must match. Enter: next result (F3), Down: to the list"
        )
        self.case_button = self._toggle("Aa", "Match upper and lower case")
        self.regex_button = self._toggle(".*", "Regular expression")
        self.case_button.setChecked(_flag(self.settings.value("find/case", False)))
        self.regex_button.setChecked(_flag(self.settings.value("find/regex", False)))
        self.problems_button = self._toggle("Problems", "Errors, warnings, steps not complete yet and steps waiting for them")
        self.off_button = self._toggle("Off", "Steps and tables that are turned off")
        self.changed_button = self._toggle("Changed", "Steps changed since the project was opened")
        self.parameter_button = self._toggle("Uses {…}", "Steps that use a parameter: any, or the one chosen in the menu")
        self.parameter_button.setPopupMode(QToolButton.ToolButtonPopupMode.MenuButtonPopup)
        self._parameters_menu = QMenu(self.parameter_button)
        self._parameters_menu.aboutToShow.connect(self._fill_parameters)
        self.parameter_button.setMenu(self._parameters_menu)
        self.group_buttons: dict[str, QToolButton] = {}
        self._grouping = QButtonGroup(self)
        self._grouping.setExclusive(True)
        for by, text in _GROUPINGS.items():
            button = self._toggle(text, f"Group the results by {text.lower()}")
            self._grouping.addButton(button)
            self.group_buttons[by] = button
        self.group_buttons["kind"].setToolTip("Imports, exports, commands and other steps")
        self.group_buttons["table"].setToolTip("Map group, table, steps")
        self.group_buttons["place"].setToolTip("Folder or connection, file or database table, steps: who reads or writes what")
        by = str(self.settings.value("find/group_by", "kind"))
        self._group_by = by if by in _GROUPINGS else "kind"
        self.group_buttons[self._group_by].setChecked(True)
        self.summary = ElidedLabel(self)
        self.summary.setObjectName("Dim")

        self.model = FindModel(self)
        self.view = QTreeView(self)
        self.view.setHeaderHidden(True)
        self.view.setUniformRowHeights(False)
        self.view.setIndentation(14)
        self.view.setExpandsOnDoubleClick(False)
        self.view.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.view.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.view.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._delegate = ResultDelegate(self.view)
        self.view.setItemDelegate(self._delegate)
        self.view.setModel(self.model)

        top = QHBoxLayout()
        top.setSpacing(4)
        top.addWidget(self.search, 1)
        top.addWidget(self.case_button)
        top.addWidget(self.regex_button)
        filters = _Flow()
        for button in (self.problems_button, self.off_button, self.changed_button, self.parameter_button):
            filters.addWidget(button)
        grouping = _Flow()
        label = QLabel("Group by:", self)
        label.setObjectName("Dim")
        grouping.addWidget(label)
        for button in self.group_buttons.values():
            grouping.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 4, 4, 6)
        layout.setSpacing(5)
        layout.addLayout(top)
        layout.addLayout(filters)
        layout.addLayout(grouping)
        layout.addWidget(self.summary)
        layout.addWidget(self.view, 1)

        self._typing = QTimer(self)
        self._typing.setSingleShot(True)
        self._typing.setInterval(_TYPING_MS)
        self._typing.timeout.connect(self._timed)
        self._later = QTimer(self)
        self._later.setSingleShot(True)
        self._later.setInterval(_LATER_MS)
        self._later.timeout.connect(self._timed)

        self.search.textChanged.connect(lambda _text: self._typing.start())
        self.search.installEventFilter(self)
        self.view.installEventFilter(self)
        self.view.clicked.connect(self._clicked)
        self.view.doubleClicked.connect(self._double_clicked)
        self.view.collapsed.connect(lambda index: self._collapsed_changed(index, True))
        self.view.expanded.connect(lambda index: self._collapsed_changed(index, False))
        self.view.selectionModel().currentChanged.connect(self._current_changed)
        for button in (self.case_button, self.regex_button):
            button.toggled.connect(self._option_toggled)
        for button in (self.off_button, self.changed_button, self.parameter_button):
            button.toggled.connect(lambda _on: self.refresh())
        self.problems_button.toggled.connect(self._problems_toggled)
        for by, button in self.group_buttons.items():
            button.clicked.connect(lambda _checked=False, by=by: self.set_group_by(by))

        session.project_changed.connect(self._project_changed)
        session.structure_changed.connect(self._edited)
        session.step_changed.connect(self._step_changed)
        session.results_changed.connect(self._results_changed)
        session.busy_changed.connect(self._busy_changed)
        self._take_baseline()

    def _toggle(self, text: str, tip: str) -> QToolButton:
        button = QToolButton(self)
        button.setObjectName("Toggle")
        button.setText(text)
        button.setToolTip(tip)
        button.setCheckable(True)
        button.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        return button

    # --- what the panel offers ---------------------------------------------------------------

    def focus_search(self) -> None:
        """The keyboard focus in the search field, its text selected."""
        self.search.setFocus(Qt.FocusReason.ShortcutFocusReason)
        self.search.selectAll()

    def set_text(self, text: str) -> None:
        """Search for ``text`` now (no delay)."""
        self.search.setText(text)
        self.refresh()

    def set_group_by(self, by: str) -> None:
        """Group the results by "kind", "table" or "place"; remembered."""
        by = by if by in _GROUPINGS else "kind"
        self.group_buttons[by].setChecked(True)
        if by != self._group_by:
            self._group_by = by
            self.settings.setValue("find/group_by", by)
        self.refresh()

    @property
    def group_by(self) -> str:
        return self._group_by

    def choose_parameter(self, name: str) -> None:
        """Only the steps that use parameter ``name`` ("" = any parameter)."""
        self._set_parameter(name)
        if self.parameter_button.isChecked():
            self.refresh()
        else:
            self.parameter_button.setChecked(True)  # searches

    def _set_parameter(self, name: str) -> None:
        self._parameter = name
        shown = name if len(name) <= _NAME_SHOWN else name[: _NAME_SHOWN - 1] + "…"
        self.parameter_button.setText(f"Uses {{{shown}}}" if name else "Uses {…}")

    def refresh(self) -> None:
        """Search again now."""
        self._typing.stop()
        self._later.stop()
        session = self.session
        before = self._index
        if self._stale or self._index is None:
            self._index = project_index.build(session.engine)
            self._token = session.engine.reload_token
            self._stale = False
        known = {name.lower() for name in self._index.parameters}
        if self._parameter and self._parameter.lower() not in known:  # renamed or removed
            self._set_parameter(_new_name(self._parameter, before, self._index))
        self.parameter_button.setEnabled(bool(known) or self.parameter_button.isChecked())
        query = self.query()
        changed = project_index.changed_steps(self._index, self._baseline) if query.changed else set()
        try:
            hits = project_index.search(self._index, query, problems=step_problems(session), changed=changed)
            error = ""
        except ValueError as e:  # a regular expression with a mistake
            hits, error = [], str(e)
        self._delegate.groups = self._group_by != "table"  # the sections are the groups then
        self._hits = hits
        self._show(project_index.sections(self._index, hits, self._group_by), same_search=query == self._searched)
        self._searched = query
        self._show_summary(query, hits, error)

    def query(self) -> Query:
        """The search as the panel's field and buttons set it."""
        return Query(
            text=self.search.text(),
            case=self.case_button.isChecked(),
            regex=self.regex_button.isChecked(),
            problems=self.problems_button.isChecked(),
            off=self.off_button.isChecked(),
            changed=self.changed_button.isChecked(),
            parameter=self._parameter if self.parameter_button.isChecked() else None,
        )

    def hits(self) -> list[Hit]:
        """The results in list order (a step under two places is there twice)."""
        return [node.item for node in self.model.results()]

    def current(self) -> Hit | None:
        node = self.model.node(self.view.currentIndex())
        return node.item if node is not None and isinstance(node.item, Hit) else None

    def next(self, step: int = 1) -> None:
        """Go to the next (or previous) result, after the last one to the first (F3, Shift+F3)."""
        if self._pending():
            self.refresh()
        results = self.model.results()
        if not results:
            self.message.emit("No results in the Find panel")
            return
        node = self.model.node(self.view.currentIndex())
        if node in results:
            self._go(results[(results.index(node) + step) % len(results)])
        else:
            self._go(self._resumed(step, results))

    def activate(self, hit_row: int, *, focus: bool = False) -> None:
        """Go to result ``hit_row`` (of ``hits()``); with ``focus``, also to its setting."""
        if self._pending():
            self.refresh()
        results = self.model.results()
        if 0 <= hit_row < len(results):
            self._go(results[hit_row], focus=focus)

    # --- searching ---------------------------------------------------------------------------

    def _pending(self) -> bool:
        return self._stale or self._index is None or self._typing.isActive() or self._later.isActive()

    def _timed(self) -> None:
        if self.isVisible():  # the panel does nothing while it is hidden
            self.refresh()

    def _schedule(self) -> None:
        if self.isVisible():
            self._later.start()

    def _edited(self) -> None:
        self._stale = True
        self._follow_folder()
        self._schedule()

    def _step_changed(self, _step_id: str) -> None:
        self._edited()

    def _busy_changed(self, _busy: bool) -> None:
        if self.problems_button.isChecked():  # "calculating all tables…"
            self._schedule()

    def _results_changed(self) -> None:
        if self.session.engine.reload_token != self._token:  # values like =NOW() are new now
            self._stale = True
        self._schedule()  # the statuses

    def _project_changed(self) -> None:
        self._stale = True
        for button in (self.problems_button, self.off_button, self.changed_button, self.parameter_button):
            button.blockSignals(True)
            button.setChecked(False)
            button.blockSignals(False)
        self._set_parameter("")
        self.search.blockSignals(True)
        self.search.clear()
        self.search.blockSignals(False)
        self._current = None
        self._gap = None
        self._collapsed.clear()  # the keys of another project's sections may be the same
        # The steps as opened a moment later: old steps are upgraded right after the project is set
        self._opened = None
        QTimer.singleShot(0, self, self._take_baseline)
        self._schedule()

    def _take_baseline(self) -> None:
        """The steps as they are now count as opened: "Changed" lists the steps that differ."""
        project = self.session.project
        self._opened = Project.from_dict(project.to_dict(), path=project.path)
        self._baseline = project_index.signatures(self._opened, self.session.registry.actions)
        if self.changed_button.isChecked():
            self._schedule()

    def _follow_folder(self) -> None:
        """Saved in another folder: the session wrote the relative paths anew for it, in the steps
        and in the undo steps. The steps as opened get the same paths, so that a step that is as
        opened (also after an Undo) stays so: "Changed" doesn't list it."""
        opened, folder = self._opened, self.session.project.directory
        if opened is None or folder is None or opened.directory == folder:
            return
        actions = self.session.registry.actions
        for table in opened.tables:
            for step in table.steps:
                if (action := actions.get(step.action)) is not None:
                    step.params = _rebased(step.params, action.params, opened.directory, folder)
        opened.path = self.session.project.path
        self._baseline = project_index.signatures(opened, actions)

    def _option_toggled(self) -> None:
        self.settings.setValue("find/case", self.case_button.isChecked())
        self.settings.setValue("find/regex", self.regex_button.isChecked())
        self.refresh()

    def _problems_toggled(self, on: bool) -> None:
        self.session.calculate_all(on)  # the tables not calculated yet have no problems to show
        self.refresh()

    def _fill_parameters(self) -> None:
        menu = self._parameters_menu
        menu.clear()
        choices = QActionGroup(menu)
        choices.setExclusive(True)
        names = [p.name for p in self.session.project.parameters]
        for name in ["", *names]:
            action = menu.addAction(f"{{{name}}}" if name else "Any parameter")
            action.setCheckable(True)
            action.setChecked(name.lower() == self._parameter.lower())
            action.triggered.connect(lambda _checked=False, name=name: self.choose_parameter(name))
            choices.addAction(action)
            if not name and names:
                menu.addSeparator()

    def _show_summary(self, query: Query, hits: list[Hit], error: str) -> None:
        calculating = query.problems and self.session.busy  # more problems may follow
        if error:
            text = error
        elif query.empty:
            kinds = [hit.entry.kind for hit in hits]
            words = (("in", "import"), ("out", "export"), ("cmd", "command"))
            parts = [count(number, word) for kind, word in words if (number := kinds.count(kind))]
            text = " · ".join(parts) or "No imports, exports or commands yet"
        elif hits:
            tables = {hit.entry.table_id for hit in hits}
            text = f"{count(len(hits), 'step')} in {count(len(tables), 'table')}"
        else:
            text = "" if calculating else "Nothing found"
        if calculating:
            text = f"{text} · calculating all tables…" if text else "Calculating all tables…"
        self.summary.setText(text)

    # --- the list ----------------------------------------------------------------------------

    def _show(self, sections: list[Section], *, same_search: bool = True) -> None:
        shown = self.model.results()
        if not self.model.set_sections(sections):
            return
        self._restoring = True
        try:
            self.view.expandAll()
            for node in self._sections():
                if node.item.key in self._collapsed:
                    self.view.collapse(self.model.index_of(node))
        finally:
            self._restoring = False
        current = self._find(self._current)
        if current is not None:
            index = self.model.index_of(current)
            self.view.selectionModel().setCurrentIndex(index, self._select_flags())
            self.view.scrollTo(index)
        elif not same_search:
            self._gap = None  # F3 starts at the first result of another search
        elif self._current is not None:
            keys = [_key(node) for node in shown]
            if self._current in keys:  # the current result left the list: F3 goes on from its place
                position = keys.index(self._current)
                self._gap = (keys, position, shown[position].item)

    def _sections(self) -> list[_Node]:
        return [node for root in self.model.roots for node in _walk(root) if isinstance(node.item, Section)]

    def _find(self, key: tuple[str, str] | None) -> _Node | None:
        """The result of a step in a section; else of the step in any section."""
        if key is None:
            return None
        results = self.model.results()
        return next(
            (n for n in results if n.parent is not None and n.parent.item.key == key[0] and n.item.entry.step_id == key[1]),
            next((n for n in results if n.item.entry.step_id == key[1]), None),
        )

    def _resumed(self, step: int, results: list[_Node]) -> _Node:
        """Where F3 (or Shift+F3) goes without a current result: after (or before) the place of the
        current result that left the list, for example as its problem was fixed; else to the first
        (or the last) result."""
        if self._gap is not None:
            keys, position = self._placed() or self._gap[:2]
            nodes = {_key(node): node for node in results}
            firsts: dict[str, _Node] = {}  # by step id: the section keys differ in another grouping
            for node in results:
                firsts.setdefault(node.item.entry.step_id, node)
            others = keys[position + 1 :] if step > 0 else reversed(keys[:position])
            for key in others:
                found = nodes.get(key) or firsts.get(key[1])
                if found is not None:
                    return found
        return results[0] if step > 0 else results[-1]

    def _placed(self) -> tuple[list[tuple[str, str]], int] | None:
        """The keys of the results in list order with the one that left put back where it would be:
        as the list is grouped now, and among the results that came since (a fixed error can show
        the next error of its table at its place). And its place among them; None when its step
        was deleted."""
        then, position, gone = self._gap
        order = {entry.step_id: number for number, entry in enumerate(self._index.entries)}
        if gone.entry.step_id not in order:
            return None
        hits = sorted([*self._hits, gone], key=lambda hit: order[hit.entry.step_id])
        sections = project_index.sections(self._index, hits, self._group_by)
        nodes = [node for root in _nodes(sections, None) for node in _walk(root) if isinstance(node.item, Hit)]
        keys = [_key(node) for node in nodes]
        places = [number for number, node in enumerate(nodes) if node.item is gone]  # a step under two places: twice
        return keys, next((number for number in places if keys[number] == then[position]), places[0])

    def _select_flags(self) -> Any:
        return QItemSelectionModel.SelectionFlag.ClearAndSelect | QItemSelectionModel.SelectionFlag.Rows

    def _go(self, node: _Node, *, focus: bool = False) -> None:
        """Select a result in the list and go to its step."""
        parent = node.parent
        while parent is not None:  # in a collapsed section: open it
            self.view.expand(self.model.index_of(parent))
            parent = parent.parent
        index = self.model.index_of(node)
        self.view.selectionModel().setCurrentIndex(index, self._select_flags())
        self.view.scrollTo(index)
        hit: Hit = node.item
        self.go_to.emit(hit.entry.table_id, hit.entry.step_id)
        if focus and hit.focus is not None:
            self.focus_setting.emit(*hit.focus)

    def _current_changed(self, current: QModelIndex, _previous: QModelIndex) -> None:
        node = self.model.node(current)
        if node is not None:
            self._gap = None  # F3 goes on from here
        if node is not None and isinstance(node.item, Hit):
            self._current = _key(node)

    def _collapsed_changed(self, index: QModelIndex, collapsed: bool) -> None:
        node = self.model.node(index)
        if self._restoring or node is None or not isinstance(node.item, Section):
            return
        if collapsed:
            self._collapsed.add(node.item.key)
        else:
            self._collapsed.discard(node.item.key)

    def _clicked(self, index: QModelIndex) -> None:
        node = self.model.node(index)
        if node is None:
            return
        if isinstance(node.item, Hit):
            self._go(node)
        else:
            self.view.setExpanded(index, not self.view.isExpanded(index))

    def _double_clicked(self, index: QModelIndex) -> None:
        node = self.model.node(index)
        if node is not None and isinstance(node.item, Hit):
            self._go(node, focus=True)

    def _visible_results(self) -> list[_Node]:
        """The results whose sections are open."""

        def shown(node: _Node) -> bool:
            parent = node.parent
            while parent is not None:
                if not self.view.isExpanded(self.model.index_of(parent)):
                    return False
                parent = parent.parent
            return True

        return [node for node in self.model.results() if shown(node)]

    def _step_in_list(self, step: int) -> None:
        """Up/Down in the list: the previous or next result, headers skipped."""
        results = self._visible_results()
        if not results:
            return
        node = self.model.node(self.view.currentIndex())
        if node is None or node not in results:
            target = self._after(node, step, results)
        else:
            position = results.index(node) + step
            target = results[position] if 0 <= position < len(results) else None
        if target is not None:
            self._go(target)

    def _after(self, node: _Node | None, step: int, results: list[_Node]) -> _Node | None:
        """From a header (or nothing): the first result below it, or the last above it."""
        if node is None:
            return results[0] if step > 0 else results[-1]
        order = [n for root in self.model.roots for n in _walk(root)]
        position = order.index(node)
        found = [n for n in (order[position + 1 :] if step > 0 else reversed(order[:position])) if n in results]
        return found[0] if found else None

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() == QEvent.Type.KeyPress:
            if watched is self.search:
                return self._search_key(event)
            if watched is self.view:
                return self._list_key(event)
        return super().eventFilter(watched, event)

    def _search_key(self, event: Any) -> bool:
        key, modifiers = event.key(), event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self.next(-1 if modifiers & Qt.KeyboardModifier.ShiftModifier else 1)
            return True
        if key == Qt.Key.Key_Down and not modifiers:
            if self._pending():
                self.refresh()
            node = self.model.node(self.view.currentIndex())
            if node is None or not isinstance(node.item, Hit):
                results = self._visible_results()
                if results:
                    self.view.selectionModel().setCurrentIndex(self.model.index_of(results[0]), self._select_flags())
            self.view.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        if key == Qt.Key.Key_Escape:
            if self.search.text():
                self.search.clear()
                self.refresh()
            else:
                self.leave.emit()
            return True
        return False

    def _list_key(self, event: Any) -> bool:
        key, modifiers = event.key(), event.modifiers()
        if key in (Qt.Key.Key_Up, Qt.Key.Key_Down) and not modifiers:
            self._step_in_list(-1 if key == Qt.Key.Key_Up else 1)
            return True
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            index = self.view.currentIndex()
            node = self.model.node(index)
            if node is not None and isinstance(node.item, Hit):
                self._go(node, focus=True)
            elif node is not None:
                self.view.setExpanded(index, not self.view.isExpanded(index))
            return True
        if key == Qt.Key.Key_Escape:
            self.search.setFocus(Qt.FocusReason.OtherFocusReason)
            return True
        return False

    def showEvent(self, event: Any) -> None:
        super().showEvent(event)
        self.refresh()


def _flag(value: Any) -> bool:
    """A yes/no from QSettings (an ini file gives the text "true")."""
    return value is True or str(value).lower() == "true"


def _new_name(name: str, before: project_index.ProjectIndex | None, now: project_index.ProjectIndex) -> str:
    """The name parameter ``name`` has now, after a rename; "" when it was removed (or it isn't
    clear). A rename writes {New} for {Old} in every step: the new name is the one parameter that
    is new and used by the steps that used the old one."""
    if before is None:
        return ""
    old = {known.lower() for known in before.parameters}
    if name.lower() not in old:
        return ""
    users = _users(before, name)
    found = [new for new in now.parameters if new.lower() not in old and _users(now, new) == users]
    return found[0] if len(found) == 1 else ""


def _users(index: project_index.ProjectIndex, name: str) -> set[str]:
    """The ids of the steps that use parameter ``name``."""
    name = name.strip().lower()
    return {entry.step_id for entry in index.entries if any(used.strip().lower() == name for used in entry.parameters)}

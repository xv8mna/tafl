"""The grey look of the app: the colors, one palette and a small style sheet."""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QIcon, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QStyleFactory

WINDOW = "#3c3f41"  # panels
BASE = "#2f3133"  # lists, grid, input fields
ALTERNATE = "#35383a"  # every second grid row
RAISED = "#4a4d50"  # buttons, headers
BORDER = "#26282a"
BORDER_LIGHT = "#55595c"
TEXT = "#d6d6d6"
# Hints and secondary text: at least 4.5 : 1 against WINDOW, BASE and ALTERNATE (WCAG AA)
TEXT_DIM = "#adb0b3"
PLACEHOLDER = "#9a9da0"  # grey text in empty fields: 4.8 : 1 against BASE
TEXT_DISABLED = "#6f7275"  # what is off or not possible now
SELECTION = "#3d6185"
ACCENT = "#4f9ee3"
ERROR_TEXT = "#f2caca"  # messages about an error, on the dark background
SELECTED_DIM = "#c5d4e3"  # secondary text on a selected row

# The project map
MAP_CANVAS = "#2b2d2f"
MAP_DOT = "#3a3d40"  # the dot grid
MAP_BOX = "#45494a"  # table boxes
MAP_TEXT_DIM = "#b4b7ba"  # secondary text in the boxes ("⋯ 3 steps"): 4.5 : 1 against MAP_BOX
MAP_EDGE = "#7a7f84"  # arrows

GROUP_COLORS = {
    "blue": "#4f9ee3",
    "green": "#6aab73",
    "orange": "#d9a35b",
    "purple": "#a37acc",
    "teal": "#4fb5a8",
    "red": "#e06c6c",
    "grey": "#8c8f93",
}

# Formulas (on BASE)
FORMULA_COLORS = {
    "column": "#7cc3f0",
    "unknown": "#e06c6c",  # a [column] the step doesn't have
    "function": "#e5c07b",
    "keyword": "#c69ce6",
    "text": "#9fcf8a",
    "number": "#d7a77a",
    "comment": "#7f848e",
    "parameter": "#56c2b6",  # {Name} of a project parameter
    "bracket": "#4a5a6a",  # behind the matching ( )
}

STATUS_COLORS = {
    "ok": "#6aab73",
    "error": "#e06c6c",
    "warning": "#d9a35b",
    "skipped": "#8c8f93",
    "disabled": "#5a5d60",
    "blocked": "#8c7a5a",
    "pending": "#5f7f9f",
    "incomplete": "#8fa3b8",  # a step that is not filled in yet
}

STYLE_SHEET = f"""
QSplitter::handle {{ background: {BORDER}; }}
QSplitter::handle:horizontal {{ width: 3px; }}
QSplitter::handle:vertical {{ height: 3px; }}
QToolBar {{ background: {WINDOW}; border: none; border-bottom: 1px solid {BORDER}; spacing: 2px; padding: 3px 6px; }}
QToolBar QToolButton {{ padding: 4px 8px; border-radius: 4px; }}
QToolBar QToolButton:hover {{ background: {RAISED}; }}
QToolBar QToolButton:pressed {{ background: {BORDER_LIGHT}; }}
QToolBar QToolButton:checked {{ background: {SELECTION}; color: white; }}
QStatusBar {{ background: #353739; border-top: 1px solid {BORDER}; color: {TEXT_DIM}; }}
QListView, QTreeView, QTableView {{ border: 1px solid {BORDER}; }}
QTableView {{ gridline-color: #3f4245; }}
QHeaderView::section {{
    background: #3a3d40; color: {TEXT}; padding: 3px 6px;
    border: none; border-right: 1px solid {BORDER}; border-bottom: 1px solid {BORDER};
}}
QScrollArea {{ border: none; }}
QMenu {{ background: {WINDOW}; border: 1px solid {BORDER}; padding: 4px 0; }}
QMenu::item {{ padding: 4px 24px 4px 14px; }}
QMenu::item:selected {{ background: {SELECTION}; color: white; }}
QMenu::item:disabled {{ color: {TEXT_DISABLED}; }}
QMenu::separator {{ height: 1px; background: {BORDER_LIGHT}; margin: 4px 8px; }}
QToolTip {{ background: #4b4e51; color: #e6e6e6; border: 1px solid {BORDER}; padding: 4px; }}
QLabel#PanelTitle {{ color: {TEXT_DIM}; font-weight: 600; padding: 6px 2px 2px 2px; }}
QLabel#Title {{ font-size: 11pt; font-weight: 600; }}
QLabel#Dim {{ color: {TEXT_DIM}; }}
QLabel#ErrorBox {{
    background: #4a2f31; border: 1px solid #7a3b3e; color: {ERROR_TEXT}; border-radius: 4px; padding: 6px 8px;
}}
QLabel#WarningBox {{
    background: #4a4030; border: 1px solid #7a6538; color: #f0dcb4; border-radius: 4px; padding: 6px 8px;
}}
QLabel#InfoBox {{
    background: #33414f; border: 1px solid #47617a; color: #cfe0f0; border-radius: 4px; padding: 6px 8px;
}}
QLineEdit[missing="true"], QComboBox[missing="true"] {{ color: {STATUS_COLORS["error"]}; }}
QFrame#ListCard {{ background: {ALTERNATE}; border: 1px solid {BORDER}; border-radius: 4px; }}
"""


def apply(app: QApplication) -> None:
    """Fusion style with the grey palette; Fusion draws input fields from the palette."""
    app.setStyle(QStyleFactory.create("Fusion"))
    app.setPalette(palette())
    app.setStyleSheet(STYLE_SHEET)


def palette() -> QPalette:
    p = QPalette()
    colors = {
        QPalette.ColorRole.Window: WINDOW,
        QPalette.ColorRole.WindowText: TEXT,
        QPalette.ColorRole.Base: BASE,
        QPalette.ColorRole.AlternateBase: ALTERNATE,
        QPalette.ColorRole.ToolTipBase: "#4b4e51",
        QPalette.ColorRole.ToolTipText: "#e6e6e6",
        QPalette.ColorRole.PlaceholderText: PLACEHOLDER,
        QPalette.ColorRole.Text: TEXT,
        QPalette.ColorRole.Button: RAISED,
        QPalette.ColorRole.ButtonText: TEXT,
        QPalette.ColorRole.BrightText: "#ffffff",
        QPalette.ColorRole.Light: "#5c6064",
        QPalette.ColorRole.Midlight: "#4f5356",
        QPalette.ColorRole.Mid: "#36393b",
        QPalette.ColorRole.Dark: BORDER,
        QPalette.ColorRole.Shadow: "#1c1d1e",
        QPalette.ColorRole.Highlight: SELECTION,
        QPalette.ColorRole.HighlightedText: "#ffffff",
        QPalette.ColorRole.Link: ACCENT,
        QPalette.ColorRole.LinkVisited: "#9b7fd4",
    }
    for role, color in colors.items():
        p.setColor(role, QColor(color))
    for role in (QPalette.ColorRole.Text, QPalette.ColorRole.WindowText, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(TEXT_DISABLED))
    p.setColor(QPalette.ColorGroup.Disabled, QPalette.ColorRole.Base, QColor(WINDOW))
    return p


def status_color(status: str) -> QColor:
    return QColor(STATUS_COLORS.get(status, STATUS_COLORS["pending"]))


def status_icon(status: str, size: int = 12) -> QIcon:
    """A small filled circle in the status color."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(status_color(status))
    painter.drawEllipse(QRectF(1.5, 1.5, size - 3, size - 3))
    painter.end()
    return QIcon(pixmap)


def group_color(name: str) -> QColor:
    return QColor(GROUP_COLORS.get(name, GROUP_COLORS["grey"]))


def group_icon(name: str, size: int = 12) -> QIcon:
    """A small rounded square in the group's color."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(group_color(name))
    painter.drawRoundedRect(QRectF(1, 1, size - 2, size - 2), 3, 3)
    painter.end()
    return QIcon(pixmap)


def monospace_font() -> QFont:
    font = QFont("Cascadia Mono")
    if not font.exactMatch():
        font = QFont("Consolas")
    font.setStyleHint(QFont.StyleHint.Monospace)
    return font


def app_pixmap(size: int = 256) -> QPixmap:
    """The app icon: a grey table with a blue arrow running through it."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    s = size / 64
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#4a4d50"))
    painter.drawRoundedRect(QRectF(2 * s, 2 * s, 60 * s, 60 * s), 12 * s, 12 * s)
    painter.setBrush(QColor("#2f3133"))
    painter.drawRoundedRect(QRectF(10 * s, 12 * s, 44 * s, 40 * s), 4 * s, 4 * s)
    painter.setBrush(QColor("#6f7377"))
    painter.drawRect(QRectF(10 * s, 12 * s, 44 * s, 9 * s))
    painter.setPen(QPen(QColor("#4a4d50"), 1.5 * s))
    for y in (30, 39):
        painter.drawLine(QPointF(10 * s, y * s), QPointF(54 * s, y * s))
    painter.drawLine(QPointF(32 * s, 21 * s), QPointF(32 * s, 52 * s))
    arrow = QPainterPath()
    arrow.moveTo(16 * s, 46 * s)
    arrow.cubicTo(24 * s, 46 * s, 30 * s, 26 * s, 44 * s, 26 * s)
    painter.setPen(QPen(QColor(ACCENT), 5 * s, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.drawPath(arrow)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(ACCENT))
    head = QPainterPath()
    head.moveTo(52 * s, 26 * s)
    head.lineTo(42 * s, 19 * s)
    head.lineTo(42 * s, 33 * s)
    head.closeSubpath()
    painter.drawPath(head)
    painter.end()
    return pixmap


def app_icon() -> QIcon:
    icon = QIcon()
    for size in (16, 24, 32, 48, 64, 128, 256):
        icon.addPixmap(app_pixmap(size))
    return icon

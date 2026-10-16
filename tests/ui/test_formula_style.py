"""Colors in formulas and the matching bracket."""

import time

from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import QPlainTextEdit

from etl_tool.ui import theme
from etl_tool.ui.formula_style import FormulaHighlighter, bracket_selections


def _colors(edit):
    """(text, color name) of each colored piece, line by line."""
    found = []
    block = edit.document().firstBlock()
    while block.isValid():
        for piece in block.layout().formats():
            text = block.text()[piece.start : piece.start + piece.length]
            color = piece.format.foreground().color().name()
            name = next((k for k, v in theme.FORMULA_COLORS.items() if QColor(v).name() == color), color)
            found.append((text, name))
        block = block.next()
    return found


def test_each_part_has_its_color(qapp):
    edit = QPlainTextEdit()
    highlighter = FormulaHighlighter(edit.document())
    highlighter.set_columns(["Net", "Date"])
    edit.setPlainText("ROUND([Net] * 1.19, 2) -- VAT\nCASE WHEN Date > [Nett] THEN 'big' END /* a\nnote */")
    assert _colors(edit) == [
        ("ROUND", "function"),
        ("[Net]", "column"),
        ("1.19", "number"),
        ("2", "number"),
        ("-- VAT", "comment"),
        ("CASE", "keyword"),
        ("WHEN", "keyword"),
        ("Date", "column"),  # a name without brackets
        ("[Nett]", "unknown"),  # not a column of the step
        ("THEN", "keyword"),
        ("'big'", "text"),
        ("END", "keyword"),
        ("/* a", "comment"),
        ("note */", "comment"),  # the comment goes on in the next line
    ]
    highlighter.set_columns(None)  # no data yet: no name is marked unknown
    assert ("[Nett]", "column") in _colors(edit)


def test_a_text_that_is_not_closed_runs_to_the_end(qapp):
    edit = QPlainTextEdit()
    FormulaHighlighter(edit.document())
    edit.setPlainText("[Net] || 'abc\n|| [x]")
    assert _colors(edit)[1:] == [("'abc", "text"), ("|| [x]", "text")]
    cursor = edit.textCursor()
    cursor.setPosition(13)
    cursor.insertText("'")  # closed now: the next line is a formula again
    assert _colors(edit)[-1] == ("[x]", "column")


def test_the_matching_bracket(qapp):
    edit = QPlainTextEdit()
    edit.setPlainText("ROUND(([Net] + 1) * 2, 2)")
    cursor = edit.textCursor()
    cursor.setPosition(6)  # after the first (
    edit.setTextCursor(cursor)
    selected = [s.cursor.selectionStart() for s in bracket_selections(edit)]
    assert selected == [5, 24]
    cursor.setPosition(7)  # after the second (
    edit.setTextCursor(cursor)
    assert [s.cursor.selectionStart() for s in bracket_selections(edit)] == [6, 16]
    cursor.movePosition(QTextCursor.MoveOperation.End)
    edit.setTextCursor(cursor)
    assert [s.cursor.selectionStart() for s in bracket_selections(edit)] == [24, 5]


def test_coloring_a_long_formula_is_quick(qapp):
    edit = QPlainTextEdit()
    highlighter = FormulaHighlighter(edit.document())
    highlighter.set_columns([f"c{i}" for i in range(300)])
    edit.setPlainText("\n".join(f"CASE WHEN [c{i}] > {i} THEN 'x{i}' ELSE UPPER([c{i}]) END ||" for i in range(40)) + " ''")
    started = time.perf_counter()
    for _ in range(20):
        edit.textCursor().insertText("1")
    assert (time.perf_counter() - started) / 20 < 0.01


def test_later_lines_follow_a_change_of_where_a_comment_ends(qapp):
    edit = QPlainTextEdit()
    highlighter = FormulaHighlighter(edit.document())
    highlighter.set_columns(["Net", "Month"])
    edit.setPlainText("/* note\n*/ [Net] * 2")
    assert _colors(edit)[-2:] == [("[Net]", "column"), ("2", "number")]
    cursor = edit.textCursor()
    cursor.setPosition(7)  # the end of line 1: comments nest, so line 2 is inside the comment now
    cursor.insertText(" /* x")
    assert _colors(edit)[-1] == ("*/ [Net] * 2", "comment")
    edit.setPlainText("[Month] + INTERVAL 1 MONTH")  # a unit, not the column of that name
    assert _colors(edit)[-1] == ("MONTH", "keyword")


def test_parameters_have_their_color(qapp):
    edit = QPlainTextEdit()
    highlighter = FormulaHighlighter(edit.document())
    edit.setPlainText("[A] > {Day} || '{day} {Other}' || [{Day}] || {Other}")
    assert ("{Other}", "parameter") in _colors(edit)  # before the names are known
    highlighter.set_parameters(["Day"])
    assert _colors(edit) == [
        ("[A]", "column"),
        ("{Day}", "parameter"),
        ("'", "text"),
        ("{day}", "parameter"),  # filled in inside a text, too
        (" {Other}'", "text"),  # an unknown name in a text stays text
        ("[{Day}]", "column"),
        ("{Other}", "unknown"),
    ]

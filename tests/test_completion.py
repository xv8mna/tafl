"""Suggestions while typing a formula: what is typed at the cursor, ranking, what is written in."""

import pytest

from etl_tool.core.completion import Context, column_suggestions, context_at, insertion, suggestions

COLUMNS = column_suggestions([("Net", "number", None), ("Customer Name", "text", None), ("Netto]x", "number", None), ("Gross", "number", 1)])


@pytest.mark.parametrize(
    ("formula", "cursor", "expected"),
    [
        ("[", 1, Context("column", 0, 1, "")),
        ("[Ne", 3, Context("column", 0, 3, "Ne")),
        ("[Ne + 1", 3, Context("column", 0, 3, "Ne")),  # not closed: only up to the cursor is replaced
        ("[Ne]t", 3, Context("column", 0, 4, "Ne")),  # inside a closed name: all of it is replaced
        ("[Net] + ro", 10, Context("word", 8, 10, "ro")),
        ("ROUND(", 5, Context("word", 0, 5, "ROUND")),
    ],
)
def test_what_is_typed_at_the_cursor(formula, cursor, expected):
    assert context_at(formula, cursor) == expected


@pytest.mark.parametrize(("formula", "cursor"), [("r", 1), ("[Net]", 5), ("'rou", 4), ("-- rou", 6), ("2", 1), ("[Net] + 1", 9)])
def test_nothing_is_suggested_inside_texts_comments_or_for_one_letter(formula, cursor):
    assert context_at(formula, cursor) is None


def test_ctrl_space_suggests_from_the_first_letter():
    assert context_at("r", 1, forced=True) == Context("word", 0, 1, "r")
    assert context_at("[Net] + ", 8, forced=True) == Context("word", 8, 8, "")


def test_ranking():
    names = [s.name for s in suggestions(Context("word", 0, 2, "ro"), COLUMNS)]
    assert names[:2] == ["ROUND", "ROW_NUMBER"]  # functions of the list before DuckDB's others
    assert [s.name for s in suggestions(Context("column", 0, 1, "net"), COLUMNS)] == ["Net", "Netto]x"]
    assert [s.name for s in suggestions(Context("column", 0, 1, "name"), COLUMNS)] == ["Customer Name"]  # contains
    word = suggestions(Context("word", 0, 3, "gro"), COLUMNS)
    assert word[0].name == "Gross" and word[0].detail == "number (row 1)"
    hint = suggestions(Context("word", 0, 7, "dateadd"), COLUMNS)[0]
    assert hint.name == "DATEADD" and "INTERVAL" in hint.detail
    assert [s.name for s in suggestions(Context("word", 0, 2, "ne"), COLUMNS)][:3] == ["Net", "Netto]x", "NEWID"]
    assert suggestions(Context("word", 0, 0, ""), COLUMNS)[0].name == "Net"  # Ctrl+Space on nothing: columns first


def test_what_is_written_in():
    def write(formula, cursor, name, forced=False):
        context = context_at(formula, cursor, forced=forced)
        item = next(s for s in suggestions(context, COLUMNS) if s.name == name)
        new, at = insertion(context, item, formula)
        return new[:at] + "|" + new[at:]

    assert write("[Ne", 3, "Netto]x") == "[Netto]]x]|"
    assert write("[Ne + 1", 3, "Net") == "[Net]| + 1"
    assert write("rou", 3, "ROUND") == "round(|)"  # typed in lowercase
    assert write("ROU", 3, "ROUND") == "ROUND(|)"
    assert write("ROUND([Net])", 3, "ROUND") == "ROUND|([Net])"  # the ( is there already
    assert write("ii", 2, "IIF") == "iif(|, , )"
    assert write("[Net] > 1 an", 12, "AND") == "[Net] > 1 and |"
    assert write("ne", 2, "Net") == "[Net]|"
    assert write("[Net] + ", 8, "Gross", forced=True) == "[Net] + [Gross]|"

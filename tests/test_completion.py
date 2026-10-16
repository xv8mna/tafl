"""Suggestions while typing a formula: what is typed at the cursor, ranking, what is written in."""

import datetime as dt

import pytest

from etl_tool.core.completion import Context, Suggestion, column_suggestions, context_at, insertion, suggestions

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


def test_a_bracket_typed_before_other_text_keeps_it():
    def write(formula, cursor, name):
        context = context_at(formula, cursor)
        item = next(s for s in suggestions(context, COLUMNS) if s.name == name)
        new, at = insertion(context, item, formula)
        return new[:at] + "|" + new[at:]

    assert write("[[Net] * 2", 1, "Gross") == "[Gross]|[Net] * 2"
    assert write("[abc + [Net]", 1, "Gross") == "[Gross]|abc + [Net]"
    assert write("[1 + 2\n-- [Net] note", 1, "Gross") == "[Gross]|1 + 2\n-- [Net] note"
    assert write("[Ne] + 1", 3, "Net") == "[Net]| + 1"  # inside a name: all of it is replaced
    assert write("roun ([Net])", 4, "ROUND") == "round| ([Net])"  # the ( after a space is there already


def test_no_list_where_a_type_or_a_unit_goes():
    """Enter must apply these formulas, not write a function in."""
    for formula in ("[Date] + INTERVAL 1 MONTH", "[x]::DATE", "CAST([x] AS INTEGER)"[:-1], "[Date] + INTERVAL '3' DAY"):
        assert context_at(formula, len(formula)) is None, formula
    assert context_at("[x]::DATE", 9, forced=True) is not None  # Ctrl+Space still opens it


def test_hints_and_duplicates():
    names = [s.name for s in suggestions(Context("word", 0, 3, "max"), COLUMNS)]
    assert names.count("MAX") == 1  # DuckDB has MAX: no hint "MAX doesn't exist"
    assert [s.name for s in suggestions(Context("word", 0, 8, "datename"), COLUMNS)] == ["DATENAME"]
    proper = suggestions(Context("word", 0, 6, "proper"), COLUMNS)[0]
    assert proper.help.startswith("Instead of PROPER:") and "UPPER(LEFT(" in proper.help
    assert [s.name for s in suggestions(Context("word", 0, 2, "is"), COLUMNS)][:2] == ["IS NULL", "IS NOT NULL"]


def test_every_column_is_listed():
    many = column_suggestions([(f"c{i}", "integer", None) for i in range(300)])
    assert len(suggestions(Context("column", 0, 1, ""), many)) == 300


def test_parameters_after_a_brace():
    from etl_tool.api.parameters import Failed
    from etl_tool.core.completion import parameter_suggestions

    found = parameter_suggestions({"Day": dt.date(2026, 10, 15), "Run Date": "x" * 50, "Bad": Failed("oops")})
    assert [(s.name, s.detail) for s in found] == [("Day", "date: 2026-10-15"), ("Run Date", "text: " + "x" * 40 + "…"), ("Bad", "error")]
    assert context_at("[A] + {", 7) == Context("parameter", 6, 7, "")
    assert context_at("[A] + {Da}", 9) == Context("parameter", 6, 10, "Da")  # all of {Da} is replaced
    assert context_at("'Report {Ru", 11) == Context("parameter", 8, 11, "Ru")  # in a text it is filled in, too
    for formula in ("-- {Da", "/* {Da", "$${Da"):  # in comments and $$texts$$ nothing is filled in
        assert context_at(formula, len(formula)) is None
    assert context_at("{Day} + 1", 5) is None  # after the }
    context = context_at("{run d", 6)
    assert [s.name for s in suggestions(context, COLUMNS, found)] == ["Run Date"]
    assert insertion(context, found[1], "{run d + 1") == ("{Run Date} + 1", 10)


def test_parameters_in_column_names():
    """Names are filled in too ([Sales {Year}]): { lists the parameters inside [ ] and " "."""
    assert context_at("[Sales {", 8) == Context("parameter", 7, 8, "")
    assert context_at("[Sales {Ye] * 2", 10) == Context("parameter", 7, 10, "Ye")  # in a [ that isn't closed
    assert context_at("[Sales {Ye}] * 2", 10) == Context("parameter", 7, 11, "Ye")  # all of {Ye} is replaced
    assert context_at('"Sales {Y', 9) == Context("parameter", 7, 9, "Y")
    year = Suggestion("parameter", "Year", "whole number: 2026")
    assert insertion(context_at("[Sales {Ye] * 2", 10), year, "[Sales {Ye] * 2") == ("[Sales {Year}] * 2", 13)
    assert context_at("[Sales ", 7).kind == "column"  # before the { the column names, as before

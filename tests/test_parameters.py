"""Project parameters: their values, and filling {Name} into texts, formulas and SQL."""

import datetime as dt
import os
import re
import sqlite3
import threading
import time
from pathlib import Path

import polars as pl
import pytest

from etl_tool.api import ActionError, Context, P, expressions
from etl_tool.api.parameters import (
    Failed,
    Fixed,
    as_text,
    fill_formula,
    fill_sql,
    fill_text,
    fixed,
    formula_literal,
    formula_places,
    left_in_formula,
    plain,
    sql_literal,
    sql_spans,
    typed,
    unknown,
)
from etl_tool.core import Connection, Engine, Parameter, Project, Status, parameters

DAY = dt.date(2026, 10, 15)
BERLIN_SUMMER = dt.datetime(2026, 10, 15, 8, 30, tzinfo=dt.timezone(dt.timedelta(hours=2)))
LONG = "=(SELECT sum(hash(x)) FROM range(2000000000) t(x))"  # takes about a minute


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("500", 500),
        ("-3", -3),
        ("01067", "01067"),  # a postcode, not a number
        ("2.5", 2.5),
        ("2026-10-15", DAY),
        ("2026-10-15 08:30", dt.datetime(2026, 10, 15, 8, 30)),
        ("2026-10-15T08:30:15", dt.datetime(2026, 10, 15, 8, 30, 15)),
        ("2026-10-15 08:30+02:00", BERLIN_SUMMER.astimezone().replace(tzinfo=None)),  # in local time, as NOW()
        (r"D:\Data", r"D:\Data"),
        ("", ""),
        ("Report 2026", "Report 2026"),
    ],
)
def test_fixed_values_get_a_type(text, value):
    assert typed(text) == value and type(typed(text)) is type(value)
    assert getattr(typed(text), "tzinfo", None) is None


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ("1.0", 1.0),
        (" 2.50 ", 2.5),
        ("+5", 5),
        ("007.5", 7.5),
        ("2026-10-01 08:30", dt.datetime(2026, 10, 1, 8, 30)),
        (" 2026-10-01", dt.date(2026, 10, 1)),
        (" Report ", " Report "),
    ],
)
def test_fixed_values_keep_their_text(written, value):
    found = fixed(written)
    assert found == Fixed(value, written if isinstance(value, str) else written.strip())
    assert plain(found) == value and plain(value) == value
    assert as_text(found) == found.text
    assert formula_literal(found) == formula_literal(value) and sql_literal(found) == sql_literal(value)


def test_values_written_as_text_in_formulas_and_in_sql():
    assert [as_text(v) for v in (DAY, dt.datetime(2026, 10, 15, 8, 30), 2.0, 2.5, True, None)] == [
        "2026-10-15", "2026-10-15 08:30:00", "2", "2.5", "true", "",
    ]  # fmt: skip
    assert formula_literal(DAY) == "DATE '2026-10-15'"
    assert formula_literal(dt.datetime(2026, 10, 15, 8, 30)) == "TIMESTAMP '2026-10-15 08:30:00'"
    assert formula_literal("O'Brien") == "'O''Brien'"
    assert [formula_literal(v) for v in (5, 2.5, True, None)] == ["5", "2.5", "TRUE", "NULL"]
    assert sql_literal("O'Brien") == "'O''Brien'"
    assert [sql_literal(v) for v in (DAY, 5, 2.5, False, None)] == ["'2026-10-15'", "5", "2.5", "0", "NULL"]


def test_negative_numbers():
    """In formulas a negative number is in parentheses: DuckDB reads -5::UTINYINT as
    -(5::UTINYINT), which is 251, INTERVAL -5 DAY is an error and **- an operator of its own. In
    SQL it is plain -5, which also works where SQL needs a constant; -{X} with X = -5 gets a
    space, so that it doesn't become --5, the start of a comment."""
    assert [formula_literal(v) for v in (-5, -2.5, -0.0, Fixed(-5, "-5"), 5)] == ["(-5)", "(-2.5)", "(-0.0)", "(-5)", "5"]
    assert [sql_literal(v) for v in (-5, -2.5, -0.0, 0)] == ["-5", "-2.5", "-0.0", "0"]
    df = pl.DataFrame({"Amount": [10], "Day": [DAY], "Text": ["a"]})
    values = parameters.values(_project(("Offset", "-5"), ("Rate", "=-2.5")))
    formulas = {
        "[Amount] -{Offset} + 1000 -{Rate}*2": 10 + 5 + 1000 + 5.0,
        "[Amount]//{Offset} + 1": -1,  # not 10 //- (5 + 1)
        "[Amount]**{Offset} * 2": pytest.approx(2e-05),
        "2^{Offset}": 2**-5,
        "{Offset}::VARCHAR": "-5",
        "[Day] + INTERVAL {Offset} DAY": dt.datetime(2026, 10, 10),
        "[Text] ||{Offset}": "a-5",
        "{Rate}.abs()": 2.5,
    }
    for formula, value in formulas.items():
        assert expressions.evaluate(df, fill_formula(formula, values)).to_list() == [value], formula
    with pytest.raises(ActionError, match="out of range"):
        expressions.evaluate(df, fill_formula("{Offset}::UINTEGER", values))  # not 4294967291
    assert fill_formula("[Amount] -{Offset} + {Offset}::VARCHAR", values) == "[Amount] -(-5) + (-5) ::VARCHAR"
    assert fill_formula("5-{Offset}", values) == "5-(-5)" and fill_formula("5-{Rate}", {"Rate": 2.5}) == "5-2.5"
    assert expressions.evaluate(df, fill_formula("5-{Offset}", values)).to_list() == [10]
    sql = "SELECT 5-{Offset} AS v, -{Offset}-{Rate} AS w -- 5-{Offset}\n, 1 AS x"
    assert fill_sql(sql, values) == "SELECT 5- -5 AS v, - -5- -2.5 AS w -- 5-{Offset}\n, 1 AS x"
    con = sqlite3.connect(":memory:")
    try:
        assert con.execute(fill_sql(sql, values)).fetchall() == [(10, 7.5, 1)]
    finally:
        con.close()
    for number in (float("inf"), float("-inf"), float("nan")):
        with pytest.raises(ActionError, match=r"The parameter \{X\} can't be used here: SQL has no way to write"):
            fill_sql("x = {X}", {"X": number})
    assert expressions.evaluate(df, fill_formula("{X} > 0", {"X": float("inf")})).to_list() == [True]


def test_texts_and_paths_get_the_value_as_text():
    values = {"Day": DAY, "Folder": r"D:\Data"}
    assert fill_text(r"{Folder}\orders_{day}.csv", values) == r"D:\Data\orders_2026-10-15.csv"
    assert fill_text(r"\d{3} {Other} {Day", values) == r"\d{3} {Other} {Day"  # other braces stay


@pytest.mark.parametrize(
    ("written", "filled"),
    [
        ("SELECT x FROM Sales_{Year}", "SELECT x FROM Sales_ 2026"),  # not the table Sales_2026
        ("1{N}", "1 5"),  # not 15
        ("{N}0", "5 0"),
        ("1.{N}", "1. 5"),
        ("{N}.5", "5 .5"),
        ("'abc'{N}", "'abc' 5"),  # not the text abc'5
        ("{N}'abc'", "5 'abc'"),
        ("[a]{N}[b]", "[a] 5 [b]"),
        ('"a"{N}"b"', '"a" 5 "b"'),
        ("@{N} ${N} #{N} :{N}: ?{N}", "@ 5 $ 5 # 5 : 5 : ? 5"),  # ?5: a numbered parameter
        ("t€{N}°\xa0{N}", "t€ 5 °\xa0 5"),  # characters from U+0080 on are part of names in SQLite and DuckDB
        ("{A}{B}", "1  2"),
        ("x{Other}y {Other}{N}", "x{Other}y {Other} 5"),  # a {Name} that is no parameter stays as it is
        ("({N}), {N}+{N}*{N}/{N}<{N}", "(5), 5+5*5/5<5"),
        ("Sales_{Year} 'Sales_{Year}'", "Sales_ 2026 'Sales_2026'"),  # inside quotes: the text
    ],
)
def test_a_value_never_joins_what_is_next_to_it(written, filled):
    values = {"Year": 2026, "N": 5, "X": "x", "A": 1, "B": 2}
    assert fill_sql(written, values) == filled
    assert fill_formula(written, values) == filled


def test_in_texts_the_value_joins_what_is_next_to_it():
    values = {"Day": DAY, "N": 5}
    assert fill_text("orders_{Day}.csv {N}{N}", values) == "orders_2026-10-15.csv 55"
    assert fill_formula("[Sales_{N}]{N}", values) == "[Sales_5] 5"
    assert fill_sql("`a`{N}`b_{N}`", values) == "`a` 5 `b_{N}`"  # `…`: a name in SQL (but not in formulas)


def test_a_value_after_an_e_starts_no_escaped_text():
    """E'…' texts have backslash escapes: e{T} with T = \\ must not become e'\\' (a text that goes on
    after its quote)."""
    filled = fill_formula("e{T} || 'x'", {"T": "\\"})
    assert filled == "e '\\' || 'x'"
    texts = [filled[t.start : t.end] for t in expressions.tokenize(filled) if t.kind == "string"]
    assert texts == ["'\\'", "'x'"]
    assert expressions.evaluate(pl.DataFrame({"e": ["y"]}), fill_formula("[e] || {T} || 'x'", {"T": "\\"}))[0] == "y\\x"


def test_a_text_next_to_a_text_in_a_formula_is_in_parentheses():
    """DuckDB reads two texts with a line break between them as one ('a'\\n'b' is 'ab'): a value
    there would go on the other text, after an E'…' text with its backslash escapes, so that it
    could end that text and run as code."""
    df = pl.DataFrame({"x": [1]})
    values = {"T": "\\' || 40 + 2 --", "X": "x", "Day": dt.date(2026, 10, 1)}
    filled = fill_formula("E'Path: '\n{T}", values)
    assert filled == "E'Path: '\n('\\'' || 40 + 2 --')"
    with pytest.raises(ActionError, match="syntax error"):
        expressions.evaluate(df, filled)
    cases = {
        "'abc'\n{X}": "'abc'\n('x')",
        "{X}\r'abc'": "('x')\r'abc'",
        "'abc' -- c\n{X}": "'abc' -- c\n('x')",
        "'abc' /* c */ {X}": "'abc' /* c */ ('x')",
        "{Day}\n'abc'": "(DATE '2026-10-01')\n'abc'",  # not DATE '2026-10-01abc'
        "'abc'{X}": "'abc' ('x')",
    }
    for formula, written in cases.items():
        assert fill_formula(formula, values) == written
        with pytest.raises(ActionError, match="syntax error"):
            expressions.evaluate(df, written)
    assert fill_formula("'abc' || {X}\n|| {Day}", values) == "'abc' || 'x'\n|| DATE '2026-10-01'"
    assert fill_sql("'abc'\n{X}", values) == "'abc'\n'x'"  # SQLite and SQL Server don't join texts


def test_a_text_joined_to_an_e_text_gets_its_escapes():
    """A '…' text on a later line goes on an E'…' text before it (only spaces and comments
    between), with the backslash escapes: a value in it has its backslashes doubled too."""
    df = pl.DataFrame({"x": [1]})
    value = "\\' || 40 + 2 --"
    for formula, before in {
        "E'Path: '\n'{T}'": "Path: ",
        "E'Path: '\n'x{T}'": "Path: x",
        "E'a' -- c\n'{T}'": "a",
        "E'a'\n'b'\n'{T}'": "ab",
    }.items():
        assert expressions.evaluate(df, fill_formula(formula, {"T": value})).to_list() == [before + value]
    assert fill_formula("E'a' || '{T}'", {"T": "\\"}) == "E'a' || '\\'"  # not joined: a text of its own


def test_values_are_spaced_by_their_place_in_the_whole_text():
    """What is right before a {Name} can be the end of a text or a comment: it counts as well."""
    values = {"N": 5}
    assert fill_sql("'a'{N}/* b */{N}-- c\n{N}", values) == "'a' 5/* b */5-- c\n5"
    assert fill_formula("'a'{N}\"b\"{N}[c]{N}", values) == "'a' 5 \"b\" 5 [c] 5"
    assert fill_sql("{N}", values) == "5" and fill_formula("-{N}", values) == "-5"


def test_formulas_get_typed_values_and_texts_inside_quotes():
    values = {"Day": DAY, "Name": "O'Brien", "Limit": 500}
    formula = """[Date] >= {Day} AND [Net] > {Limit} AND [Customer] = {Name} || ' {Name} on {Day}'"""
    assert fill_formula(formula, values) == (
        "[Date] >= DATE '2026-10-15' AND [Net] > 500 AND [Customer] = 'O''Brien' || ' O''Brien on 2026-10-15'"
    )
    untouched = """$${Day}$$ -- {Day}\n/* {Day} /* {Day} */ {Day} */ $x${Day}$x$"""
    assert fill_formula(untouched, values) == untouched
    assert expressions.evaluate(pl.DataFrame({"x": [1]}), fill_formula("{Name} || ' ' || {Day}", values))[0] == "O'Brien 2026-10-15"


def test_column_names_in_formulas_get_the_text():
    """Settings that name new columns are filled in, so formulas must see the same names."""
    values = {"Year": Fixed(2026, "2026"), "Odd": 'a]b"c'}
    assert fill_formula("""[Sales {Year}] + "Sales {Year}" + [{Odd}] + "{Odd}" """, values) == (
        """[Sales 2026] + "Sales 2026" + [a]]b"c] + "a]b""c" """
    )
    df = pl.DataFrame({"Sales 2026": [1], 'a]b"c': [2]})
    assert expressions.evaluate(df, fill_formula("""[Sales {Year}] * 10 + [{Odd}] + "{Odd}" """, values)).to_list() == [14]


def test_backslashes_in_e_texts_of_formulas():
    values = {"Folder": "D:\\Data\\", "Name": "O'Brien"}
    filled = fill_formula("E'{Folder}' = '{Folder}' AND e'{Name}' = {Name} AND [x]LIKE'{Folder}'", values)
    assert filled == "E'D:\\\\Data\\\\' = 'D:\\Data\\' AND e'O''Brien' = 'O''Brien' AND [x]LIKE'D:\\Data\\'"
    assert expressions.evaluate(pl.DataFrame({"x": ["y"]}), filled).to_list() == [False]  # [x] isn't the folder
    assert expressions.evaluate(pl.DataFrame({"x": ["y"]}), fill_formula("E'{Folder}' || e'{Name}'", values))[0] == "D:\\Data\\O'Brien"


def test_sql_gets_values_in_the_databases_way():
    values = {"Day": DAY, "Name": "x'; DROP TABLE Orders; --"}
    sql = """SELECT * FROM "{Day}" WHERE Day = {Day} AND Name = {Name} AND Note LIKE '%{Day}%' -- {Day}\n/* {Name} */ AND [{Name}] = 1"""
    assert fill_sql(sql, values) == (
        """SELECT * FROM "{Day}" WHERE Day = '2026-10-15' AND Name = 'x''; DROP TABLE Orders; --'"""
        """ AND Note LIKE '%2026-10-15%' -- {Day}\n/* {Name} */ AND [{Name}] = 1"""
    )
    assert fill_sql("WHERE Day = {Day}", values, literal=lambda v: f"<{v}>") == "WHERE Day = <2026-10-15>"
    at = []
    sql = "SELECT {Day}, '{Day}', -- {Day}\n{Name}"
    assert fill_sql(sql, values, literal_at=lambda v, p: at.append((v, p)) or f"<{p}>") == "SELECT <7>, '2026-10-15', -- {Day}\n<32>"
    assert at == [(DAY, 7), (values["Name"], 32)]  # the plain value, and where its {Name} is in the SQL
    assert fill_sql("'it''s {Day}' = {Day}", values) == "'it''s 2026-10-15' = '2026-10-15'"  # doubled quotes
    seen = []
    assert fill_sql("{V} '{V}'", {"V": Fixed(2.5, "2.50")}, literal=lambda v: seen.append(v) or repr(v)) == "2.5 '2.50'"
    assert seen == [2.5]  # a connector's literal gets the plain value


def test_sql_comments_nest_and_dollar_quotes_stay():
    """SQL Server nests /* */ comments: a */ in a value must not end one early."""
    values = {"X": "*/ DROP TABLE t /*", "N": 1}
    sql = "SELECT 1 /* a /* b */ {X} */ , {N} /* {X} */ , $${X}$$, $tag$ {X} $tag$, price$ {N}, a$b$c {N}"
    assert fill_sql(sql, values) == (
        "SELECT 1 /* a /* b */ {X} */ , 1 /* {X} */ , $${X}$$, $tag$ {X} $tag$, price$ 1, a$b$c 1"
    )
    assert [kind for _start, _end, kind in sql_spans("'a' \"b\" [c] `d` -- e\n/* f /* g */ */ $$h$$ $t$i$t$ 'j")] == [
        "text", "name", "name", "name", "comment", "comment", "dollar", "dollar", "text",
    ]  # fmt: skip
    assert sql_spans("x /* a /* b */ c") == [(2, 16, "comment")]  # not closed: to the end


def test_sql_is_read_the_way_the_database_reads_it():
    """Databases differ in where their texts and comments end: the options of sql_spans say how
    one reads SQL, and fill_sql fills in by those spans."""
    sql = "/* a /* b */ ' */"
    assert sql_spans(sql) == [(0, 17, "comment")]
    assert sql_spans(sql, nested_comments=False) == [(0, 12, "comment"), (13, 17, "text")]
    assert sql_spans("-- a\rb'c'") == [(0, 4, "comment"), (6, 9, "text")]  # a carriage return ends it too
    assert sql_spans("-- a\nb'c'") == [(0, 4, "comment"), (6, 9, "text")]
    assert sql_spans("-- a\rb'c'", line_ends="\n") == [(0, 9, "comment")]  # SQLite
    assert sql_spans("$a$ 'x' $a$") == [(0, 11, "dollar")]
    assert sql_spans("$a$ 'x' $a$", dollar_quotes=False) == [(4, 7, "text")]
    assert sql_spans("@v(') '{X}'", words=re.compile(r"@\w+\([^ )]*\)")) == [(0, 5, "name"), (6, 11, "text")]
    sql, values = "SELECT /* a /* b */ ' */ , {X} -- ' AS q", {"X": " AS injected --"}
    assert fill_sql(sql, values) == "SELECT /* a /* b */ ' */ , ' AS injected --' -- ' AS q"
    assert fill_sql(sql, values, spans=sql_spans(sql, nested_comments=False)) == (
        "SELECT /* a /* b */ ' */ ,  AS injected -- -- ' AS q"  # in a text: the text as it is
    )


def test_unknown_names_where_they_would_be_filled_in():
    values = {"Day": DAY}
    assert unknown("{Dya} {day} {Dya} {Other} \\d{3}", values) == ["Dya", "Other"]
    assert unknown("{Dya} '{Mon}' [{Name}] -- {Note}\n/* {Note} */ $${Note}$$", values, sql=True) == ["Dya", "Mon"]


def test_a_parameter_with_an_error_is_an_error_where_it_is_used():
    values = {"Day": Failed("Division by zero")}
    for fill in (fill_text, fill_formula, fill_sql):
        with pytest.raises(ActionError, match=r"The parameter \{Day\} has an error: Division by zero"):
            fill("x {day}", values)
    assert fill_text("x {Other}", values) == "x {Other}"


def test_left_over_names_in_formulas():
    assert left_in_formula("[A] + {Dya}") == "Dya"
    assert left_in_formula("""'{Dya}' || [{Dya}] || "{Dya}" -- {Dya}""") is None
    assert formula_places("""{A} || '{B}' || [{C}] || "{D}" || $${E}$$ /* {F} */""") == [
        (0, 3, "A", "formula"), (8, 11, "B", "text"), (17, 20, "C", "name"), (26, 29, "D", "name"),
    ]  # fmt: skip
    with pytest.raises(ActionError, match=r"there is no parameter \{Dya\}"):
        expressions.evaluate(pl.DataFrame({"A": [1]}), "[A] + {Dya}")


def _project(*pairs) -> Project:
    return Project(parameters=[Parameter(name, value) for name, value in pairs])


def test_values_in_order_formulas_use_the_ones_above():
    project = _project(("Day", "=DATE '2026-10-15' - 1"), ("Month", "=strftime({Day}, '%Y-%m')"), ("Limit", "500"))
    assert parameters.values(project) == {"Day": dt.date(2026, 10, 14), "Month": "2026-10", "Limit": Fixed(500, "500")}
    given = parameters.values(project, {"day": "2026-01-31", "LIMIT ": "=1 + 1"})
    assert given == {"Day": Fixed(dt.date(2026, 1, 31), "2026-01-31"), "Month": "2026-01", "Limit": 2}
    assert parameters.values(_project(("Today", "=TODAY()")))["Today"] == dt.date.today()
    assert parameters.values(_project(("Version", " 1.0 "), ("File", "='v{Version}_' || {Version}")))["File"] == "v1.0_1.0"


def test_a_formula_with_an_error():
    found = parameters.values(_project(("Bad", "=1 +"), ("Later", "={Bad} + 1"), ("Below", "={Later2}")))
    assert isinstance(found["Bad"], Failed) and found["Bad"].message.startswith("the formula ends too early")
    assert found["Later"] == Failed("The parameter {Bad} has an error: " + found["Bad"].message)
    assert found["Below"].message.startswith("there is no parameter {Later2}")


def test_a_formula_can_only_use_the_parameters_above_it():
    found = parameters.values(_project(
        ("Month", "=strftime({day}, '%Y-%m')"), ("Day", "=TODAY()"), ("Next", "={Next} + 1"), ("Name", "='{Later}'"), ("Later", "x"),
    ))  # fmt: skip
    assert found["Month"] == Failed("{day} is below this parameter: a formula can only use the parameters above it")
    assert found["Next"] == Failed("a parameter's formula can't use itself")
    assert found["Name"] == Failed("{Later} is below this parameter: a formula can only use the parameters above it")
    assert found["Day"] == dt.date.today()


def test_a_formula_may_take_five_seconds():
    assert parameters.TIME_LIMIT == 5
    started = time.perf_counter()
    found = parameters.values(_project(("Slow", LONG), ("Next", "=1 + 1")), limit=0.3)
    assert time.perf_counter() - started < 10
    assert found == {"Slow": Failed("the formula took longer than 0.3 seconds"), "Next": 2}
    # A late interrupt must not stop the next formulas on this thread
    for _ in range(20):
        assert parameters.values(_project(("A", "=40 + 2")), limit=0.3) == {"A": 42}
        assert expressions.evaluate(pl.DataFrame({"x": [1]}), "[x] + 1").to_list() == [2]


def test_a_formula_that_ends_just_in_time_keeps_its_value():
    """An interrupt between two queries is forgotten by DuckDB: a result that is there counts."""
    for limit in (0.001, 0.005, 0.02):
        found = parameters.values(_project(("A", "=40 + 2")), limit=limit)["A"]
        assert found in (42, Failed(f"the formula took longer than {limit:g} seconds"))
        assert parameters.values(_project(("A", "=40 + 2")))["A"] == 42


def test_problems_with_names_and_overrides():
    project = _project(("Day", "1"), ("day", "2"), ("2nd", "3"), ("Run date", "4"), ("Month ", "5"), (" Year", "6"))
    assert parameters.problems(project, {"Month": "x", "RUN DATE": "5", "Week": "1"}) == [
        "There are two parameters named 'day'",
        "The parameter name '2nd' can't be used: letters, digits, _ and spaces, starting with a letter",
        "The parameter name 'Month ' can't be used: it starts or ends with a space",
        "The parameter name ' Year' can't be used: it starts or ends with a space",
        "The project has no parameter 'Week' (its parameters: Day, day, 2nd, Run date, Month ,  Year)",
    ]


def test_names_are_found_without_the_spaces_around_them():
    values = {"Day ": Fixed(DAY, "2026-10-15"), " Month": "10"}
    assert fill_text("{day}_{MONTH }", values) == "2026-10-15_10"
    assert parameters.values(_project(("Day ", "2026-10-15"), ("Next", "={Day} + 1")))["Next"] == dt.date(2026, 10, 16)


def test_hand_edited_values_are_kept():
    assert Parameter.from_dict({"name": "Zero", "value": 0}).value == "0"
    assert Parameter.from_dict({"name": "Rate", "value": 2.5}).value == "2.5"
    assert Parameter.from_dict({"name": "Empty", "value": None}).value == ""
    assert Parameter.from_dict({"name": "Missing"}).value == ""


def test_filling_the_settings_of_a_step():
    params = [
        P.Text("file", "File"),
        P.Expression("condition", "Condition"),
        P.Code("query", "SQL", language="sql"),
        P.Choice("mode", "Mode", [("{Day}", "x")]),
        P.List("columns", "Columns", [P.Text("name", "Name"), P.Expression("formula", "Formula")]),
        P.Columns("keep", "Keep"),
    ]
    raw = {
        "file": "orders_{Day}.csv",
        "condition": "[Date] = {Day}",
        "query": "WHERE d = {Day}",
        "mode": "{Day}",
        "columns": [{"name": "Col {Day}", "formula": "{Day}"}],
        "keep": ["A", "B {Day}"],
        "other": "{Day}",
    }
    filled = parameters.fill(params, raw, {"Day": DAY})
    assert filled == {
        "file": "orders_2026-10-15.csv",
        "condition": "[Date] = DATE '2026-10-15'",
        "query": "WHERE d = '2026-10-15'",
        "mode": "{Day}",  # a choice is not typed
        "columns": [{"name": "Col 2026-10-15", "formula": "DATE '2026-10-15'"}],
        "keep": ["A", "B 2026-10-15"],
        "other": "{Day}",  # no setting of the action
    }
    assert raw["file"] == "orders_{Day}.csv"  # the step itself keeps {Day}
    assert parameters.fill(params, raw, {}) == raw


def test_hidden_settings_are_not_filled_in():
    params = [
        P.Choice("mode", "Mode", ["table", "query"]),
        P.Text("table", "Table", visible_if={"mode": "table"}),
        P.Code("query", "SQL", visible_if={"mode": "query"}),
        P.List("rows", "Rows", [P.Bool("on", "On"), P.Text("text", "Text", visible_if={"on": True})]),
    ]
    values = {"Bad": Failed("Division by zero"), "Day": DAY}
    raw = {"table": "T_{Day}", "query": "WHERE x = {Bad}", "rows": [{"on": True, "text": "{Day}"}, {"text": "{Bad} {Dya}"}]}
    filled = parameters.fill(params, raw, values)  # mode: "table", the default
    assert filled == {"table": "T_2026-10-15", "query": "WHERE x = {Bad}", "rows": [{"on": True, "text": "2026-10-15"}, {"text": "{Bad} {Dya}"}]}
    assert parameters.unknown_names(params, {**raw, "table": "{Dya} {Mon}"}, values) == ["Dya", "Mon"]
    with pytest.raises(ActionError, match=r"\{Bad\} has an error"):
        parameters.fill(params, {**raw, "mode": "query"}, values)


def test_actions_get_only_the_filled_in_settings():
    """The values are not handed to actions: what they use is in the settings (and their cache key)."""
    assert not hasattr(Context(), "parameters")


def test_renaming_writes_the_new_name_everywhere():
    settings = {"file": "a_{day}_{Month}.csv", "columns": [{"formula": "{DAY} + 1"}], "n": 3}
    assert parameters.renamed(settings, {"Day": "Run Day", "Month": "Day"}) == {
        "file": "a_{Run Day}_{Day}.csv", "columns": [{"formula": "{Run Day} + 1"}], "n": 3,
    }  # fmt: skip


def test_parameters_are_saved_with_the_project(tmp_path):
    project = _project(("Day", "=TODAY() - 1"), ("Folder", r"D:\Data"))
    loaded = Project.load(project.save(tmp_path / "p.tafl"))
    assert [(p.name, p.value) for p in loaded.parameters] == [("Day", "=TODAY() - 1"), ("Folder", r"D:\Data")]
    assert "parameters" not in Project().to_dict()


# --- in a calculation -------------------------------------------------------------------------


@pytest.fixture
def project(tmp_path) -> Project:
    for day in ("2026-10-01", "2026-10-02"):
        (tmp_path / f"orders_{day}.csv").write_text(f"Id;Day\n1;{day}\n2;{day}\n3;2026-01-01\n", encoding="utf-8")
    project = Project(name="Test", parameters=[Parameter("Day", "2026-10-01"), Parameter("Min", "2")])
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    project.add_step(orders, "csv.import", {"file": "orders_{Day}.csv"})
    project.add_step(orders, "rows.filter_formula", {"condition": "[Day] = {Day} AND [Id] >= {Min}"})
    project.add_step(orders, "calc.columns", {"columns": [{"name": "Label {Min}", "formula": "'Day ' || {Day}"}]})
    return project


def test_a_calculation_fills_in_the_values(project, registry):
    result = Engine(project, registry).evaluate("Orders")
    assert result.ok, [s.error for s in result.steps]
    assert result.df.to_dicts() == [{"Id": 2, "Day": dt.date(2026, 10, 1), "Label 2": "Day 2026-10-01"}]


def test_other_values_calculate_again(project, registry):
    engine = Engine(project, registry)
    engine.evaluate("Orders")
    project.parameters[0].value = "2026-10-02"
    result = engine.evaluate("Orders")
    assert [s.cached for s in result.steps] == [False, False, False]
    assert result.df["Day"].to_list() == [dt.date(2026, 10, 2)]
    engine.parameter_overrides = {"day": "2026-10-01", "Min": "1"}
    assert engine.evaluate("Orders").df["Id"].to_list() == [1, 2]
    project.parameters[0].value = "2026-10-02"
    assert engine.evaluate("Orders").steps[0].cached  # the same values: from the cache


def test_a_parameter_with_an_error_stops_the_steps_that_use_it(project, registry):
    project.parameters[1].value = "=1 / 0 +"
    result = Engine(project, registry).evaluate("Orders")
    assert [s.status for s in result.steps] == [Status.OK, Status.ERROR, Status.BLOCKED]
    assert result.steps[1].error.startswith("The parameter {Min} has an error: the formula ends too early")


def test_an_unknown_name_in_a_formula(project, registry):
    project.tables[0].steps[1].params["condition"] = "[Id] > {Mni}"
    result = Engine(project, registry).evaluate("Orders")
    assert "there is no parameter {Mni} (Project → Parameters lists them)" in result.steps[1].error


def test_sql_and_connections_use_parameters(tmp_path, registry):
    with sqlite3.connect(tmp_path / "shop.db") as con:
        con.execute("CREATE TABLE Orders (Id INTEGER, Customer TEXT, Day TEXT)")
        con.executemany("INSERT INTO Orders VALUES (?, ?, ?)", [(1, "O'Brien", "2026-10-01"), (2, "Smith", "2026-10-02")])
    con.close()
    project = Project(
        name="Test",
        parameters=[Parameter("Db", "shop"), Parameter("Customer", "O'Brien"), Parameter("Day", "2026-10-01")],
        connections=[Connection("shop", "sqlite", {"file": "{Db}.db"})],
    )
    project.path = tmp_path / "test.tafl"
    orders = project.add_table("Orders")
    query = "SELECT * FROM Orders WHERE Customer = {Customer} AND Day = {Day} -- {Day}"
    project.add_step(orders, "db.import", {"connection": "shop", "mode": "query", "query": query})
    engine = Engine(project, registry)
    result = engine.evaluate("Orders")
    assert result.ok, [s.error for s in result.steps]
    assert result.df["Id"].to_list() == [1]
    engine.parameter_overrides = {"Customer": "x' OR '1'='1", "Day": "2026-10-02"}
    assert engine.evaluate("Orders").df.height == 0  # a value, never SQL
    engine.parameter_overrides = {"Db": "other"}  # another database: read again
    assert "other.db" in engine.evaluate("Orders").steps[0].error


def test_a_changed_connection_reads_again(tmp_path, registry):
    for name, rows in (("a", 1), ("b", 2)):
        with sqlite3.connect(tmp_path / f"{name}.db") as con:
            con.execute("CREATE TABLE T (x INTEGER)")
            con.executemany("INSERT INTO T VALUES (?)", [(i,) for i in range(rows)])
        con.close()
    project = Project(name="Test", connections=[Connection("db", "sqlite", {"file": "a.db"})])
    project.path = tmp_path / "test.tafl"
    table = project.add_table("T")
    project.add_step(table, "db.import", {"connection": "db", "mode": "table", "table": "T"})
    engine = Engine(project, registry)
    assert engine.evaluate("T").df.height == 1
    project.connections[0].settings["file"] = "b.db"
    assert engine.evaluate("T").df.height == 2


def _database(tmp_path) -> Project:
    with sqlite3.connect(tmp_path / "shop.db") as con:
        con.execute("CREATE TABLE Orders (Id INTEGER, Rate TEXT)")
        con.executemany("INSERT INTO Orders VALUES (?, ?)", [(1, "2.50"), (2, "2.5")])
    con.close()
    project = Project(name="Test", connections=[Connection("shop", "sqlite", {"file": "shop.db"})])
    project.path = tmp_path / "test.tafl"
    return project


def test_hidden_settings_with_a_parameter_that_has_an_error(tmp_path, registry):
    project = _database(tmp_path)
    project.parameters = [Parameter("Bad", "=1 +"), Parameter("Day", "2026-10-01")]
    orders = project.add_table("Orders")
    project.add_step(orders, "db.import", {"connection": "shop", "mode": "table", "table": "Orders", "query": "SELECT {Bad} -- {Day}"})
    project.add_step(orders, "db.export", {"connection": "shop", "table": "Copy", "mode": "insert", "condition": "x = {Bad}"})
    engine = Engine(project, registry)
    result = engine.evaluate("Orders")
    assert [s.status for s in result.steps] == [Status.OK, Status.SKIPPED], result.first_error
    project.parameters[1].value = "2026-10-02"  # only in a hidden setting: the same data
    assert engine.evaluate("Orders").steps[0].cached
    project.tables[0].steps[1].params["mode"] = "replace_where"
    assert "The parameter {Bad} has an error" in engine.evaluate("Orders").steps[1].error


def test_fixed_values_keep_their_text_in_file_names_and_sql_quotes(tmp_path, registry):
    project = _database(tmp_path)
    project.parameters = [Parameter("Version", "1.0"), Parameter("Rate", "2.50")]
    orders = project.add_table("Orders")
    query = "SELECT Id, Rate, '{Rate}' AS Typed, {Rate} AS Value FROM Orders WHERE Rate = '{Rate}'"
    project.add_step(orders, "db.import", {"connection": "shop", "mode": "query", "query": query})
    project.add_step(orders, "csv.export", {"file": "report_v{Version}.csv"})
    results = Engine(project, registry).run()
    assert all(r.ok for r in results.values()), [r.first_error for r in results.values()]
    assert results[orders.id].df.to_dicts() == [{"Id": 1, "Rate": "2.50", "Typed": "2.50", "Value": 2.5}]
    assert (tmp_path / "report_v1.0.csv").is_file()


def test_the_values_stay_the_same_until_a_reload(project, registry):
    """=RANDOM() (or =NOW()) is worked out when the parameters change and on a reload (a run
    reloads), not for every calculation: previews in between agree, also those of snapshots."""
    project.parameters.append(Parameter("Random", "=RANDOM()"))
    project.tables[0].steps[2].params["columns"].append({"name": "R", "formula": "{Random}"})
    engine = Engine(project, registry)
    first = engine.parameter_values()["Random"]
    assert engine.evaluate("Orders").df["R"].to_list() == [first]
    twin = engine.snapshot()
    assert twin.parameter_values()["Random"] == first and twin.evaluate("Orders").steps[2].cached
    engine.reload()
    second = engine.parameter_values()["Random"]
    assert second != first and engine.evaluate("Orders").df["R"].to_list() == [second]
    twin.reload_token = engine.reload_token
    assert twin.parameter_values()["Random"] == second  # worked out once for both
    engine.parameter_overrides = {"Min": "1"}
    assert engine.snapshot().parameter_overrides == {"Min": "1"}
    assert engine.snapshot().evaluate("Orders").df["Id"].to_list() == [1, 2]
    engine.run()  # a run reloads: new values
    assert engine.parameter_values()["Random"] not in (first, second)


def test_parameter_values_are_worked_out_once_for_all_threads(project, registry):
    project.parameters.append(Parameter("Random", "=RANDOM()"))
    engine = Engine(project, registry)
    found = []
    threads = [threading.Thread(target=lambda: found.append(engine.snapshot().parameter_values()["Random"])) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(set(found)) == 1 and found[0] == engine.parameter_values()["Random"]


def test_the_values_in_use_are_kept(project, registry):
    """Up to 8 sets of values are kept: those not used for the longest time are dropped first,
    so the values in use stay while overrides come and go."""
    project.parameters.append(Parameter("Random", "=RANDOM()"))
    engine = Engine(project, registry)
    first = engine.parameter_values()["Random"]
    for minimum in range(1, 20):
        engine.parameter_overrides = {"Min": str(minimum)}
        assert engine.parameter_values()["Random"] != first
        engine.parameter_overrides = {}
        assert engine.parameter_values()["Random"] == first


@pytest.mark.skipif(os.name != "nt", reason="only Windows has hidden parts of files after a ':'")
def test_a_time_with_a_colon_is_no_file_name(project, registry, tmp_path):
    """On Windows, report_2026-10-01 08:30.csv would write into a hidden part of the file
    report_2026-10-01 08: refused instead."""
    project.parameters.append(Parameter("When", "2026-10-01 08:30"))
    project.add_step(project.tables[0], "csv.export", {"file": "report_{When}.csv"})
    result = Engine(project, registry).run()[project.tables[0].id]
    assert result.first_error.startswith("':' can't be part of a file or folder name: report_2026-10-01 08:30.csv.")
    assert not list(tmp_path.glob("report*"))
    ctx = Context(project_dir=tmp_path)
    for path in ("8:30.csv", "data\\x:y.csv", "C:\\data\\x.csv:hidden"):
        with pytest.raises(ActionError, match="':' can't be part of a file or folder name"):
            ctx.resolve_path(path)
    assert ctx.resolve_path("C:\\data\\x.csv") == Path("C:\\data\\x.csv")
    assert ctx.resolve_path("\\\\?\\C:\\data\\x.csv") == Path("\\\\?\\C:\\data\\x.csv")
    assert ctx.resolve_path("out\\x.csv") == tmp_path / "out" / "x.csv"


@pytest.mark.skipif(os.name != "nt", reason="only Windows has drives")
def test_a_drive_needs_a_backslash_after_it():
    """C:data.csv is in the current folder of drive C (wherever that is): not what was meant."""
    ctx = Context(project_dir="C:\\Projects")  # on the same drive it would be C:\Projects\data.csv
    messages = {
        "C:": "'C:' needs a \\ after it: C:\\ is the top folder of drive C",
        "C:data.csv": "'C:' needs a \\ after it: C:\\ is the top folder of drive C (in C:data.csv)",
        "t:30.csv": "'t:' needs a \\ after it: t:\\ is the top folder of drive T (in t:30.csv)",
    }
    for path, message in messages.items():
        with pytest.raises(ActionError) as error:
            ctx.resolve_path(path)
        assert str(error.value) == message
    assert ctx.resolve_path("C:\\data.csv") == Path("C:\\data.csv")


def test_an_unknown_name_in_a_text_gives_a_warning(project, registry):
    project.tables[0].steps[2].params["columns"][0]["name"] = "Label {Mni}"
    project.add_step(project.tables[0], "csv.export", {"file": "out_{Dya}.csv"})
    engine = Engine(project, registry)
    for cached in (False, True):
        result = engine.evaluate("Orders")
        assert result.steps[2].cached is cached and result.df.columns[-1] == "Label {Mni}"
        assert result.steps[2].messages == [("warning", "{Mni} is not a parameter of the project: it was left as it is")]
        assert result.steps[3].messages[0] == ("warning", "{Dya} is not a parameter of the project: it was left as it is")
    assert engine.evaluate("Orders").steps[0].messages == []
    plain_project = Project(name="Plain")  # without parameters, braces are just text
    plain_project.path = project.path
    table = plain_project.add_table("Orders")
    plain_project.add_step(table, "csv.import", {"file": "orders_2026-10-01.csv"})
    plain_project.add_step(table, "csv.export", {"file": "out_{Dya}.csv"})
    assert Engine(plain_project, registry).evaluate("Orders").steps[1].messages == [("info", "Runs only when the project is run")]


def test_the_sql_is_filled_in_by_the_connection_type(tmp_path, registry, monkeypatch):
    project = _database(tmp_path)
    project.parameters = [Parameter("Id", "1")]
    orders = project.add_table("Orders")
    project.add_step(orders, "db.import", {"connection": "shop", "mode": "query", "query": "SELECT * FROM Orders WHERE Id = {Id}"})
    seen = []

    def fill_sql(sql, values):
        seen.append((sql, dict(values)))
        return sql.replace("{Id}", "2")

    monkeypatch.setattr(registry.connectors["sqlite"], "fill_sql", fill_sql)
    assert Engine(project, registry).evaluate("Orders").df["Id"].to_list() == [2]
    assert seen == [("SELECT * FROM Orders WHERE Id = {Id}", {"Id": Fixed(1, "1")})]


def test_the_warnings_read_the_sql_as_the_connection_does(tmp_path, registry):
    """In SQLite only a line feed ends a -- comment: after a carriage return {Note} is still in it."""
    project = _database(tmp_path)
    project.parameters = [Parameter("Id", "1")]
    orders = project.add_table("Orders")
    query = "SELECT * FROM Orders -- {Note}\r{Other}\nWHERE Id = {Id} -- {Dya}"
    project.add_step(orders, "db.import", {"connection": "shop", "mode": "query", "query": query})
    result = Engine(project, registry).evaluate("Orders")
    assert result.ok, result.first_error
    assert result.steps[0].messages == []
    assert unknown(query, {"Id": 1}, sql=True) == ["Other"]  # read the default way: a carriage return ends a comment

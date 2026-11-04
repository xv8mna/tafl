import dataclasses
import datetime as dt
import random
import re
import threading
import time

import duckdb
import polars as pl
import pytest

from tafl.api import ActionError, expressions
from tafl.core import Engine, Project


def test_translate_brackets_strings_and_isnull():
    assert expressions.translate("[Unit Price] * 2") == '"Unit Price" * 2'
    assert expressions.translate("'[not a column]' || [a]]b]") == "'[not a column]' || \"a]b\""
    assert expressions.translate("ISNULL([x], 0) + isnull_count") == 'coalesce("x", 0) + isnull_count'
    assert expressions.translate("'it''s' || \"[q]\"") == "'it''s' || \"[q]\""


def test_add_columns_uses_previous_results_and_replaces_existing(people):
    out = expressions.add_columns(
        people,
        [("Double", "[Age] * 2"), ("Plus", "[Double] + 1"), ("Age", "[Age] + 100")],
    )
    assert out.columns == ["Name", "City", "Age", "Score", "Double", "Plus"]
    assert out["Plus"].to_list() == [69, 55, 83, 39, 117]
    assert out["Age"].to_list() == [134, 127, 141, 119, 158]
    assert out.schema["Double"] == pl.Int64  # DuckDB's INTEGER/HUGEINT normalized


def test_tsql_helpers(people):
    out = expressions.add_columns(
        people,
        [
            ("size", "IIF([Age] >= 40, 'old', 'young')"),
            ("name", "ISNULL([Name], '?')"),
            ("len", "LEN([City])"),
            ("pos", "CHARINDEX('r', [City])"),
            ("now", "GETDATE()"),
        ],
    )
    assert out["size"].to_list() == ["young", "young", "old", "young", "old"]
    assert out["name"][3] == "?"
    assert out["len"].to_list()[:2] == [6, 7]
    assert out["pos"][0] == 3
    assert out.schema["now"] == pl.Datetime("us")


def test_upper_writes_sharp_s_as_ss():
    df = pl.DataFrame({"s": ["Größe", "ẞ", None]})
    out = expressions.add_columns(df, [("u", "UPPER([s])"), ("c", "UCASE([s])"), ("m", "[s].upper()"), ("l", "LOWER('ẞ')")])
    assert out["u"].to_list() == out["c"].to_list() == out["m"].to_list() == ["GRÖSSE", "SS", None]
    assert out["l"][0] == "ß"
    with pytest.raises(ActionError, match="UPPER"):
        expressions.evaluate(df, "UPPER(1)")


def test_window_function_keeps_row_order():
    df = pl.DataFrame({"g": [i % 7 for i in range(50_000)], "v": list(range(50_000))})
    out = expressions.add_columns(df, [("total", "SUM([v]) OVER (PARTITION BY [g])"), ("same", "[v]")])
    assert out["same"].to_list() == df["v"].to_list()
    expected = df.group_by("g").agg(pl.col("v").sum().alias("total"))
    assert out.join(expected, on="g", suffix="_e").select((pl.col("total") == pl.col("total_e")).all()).item()


def test_filter_rows(people):
    assert expressions.filter_rows(people, "[Age] > 30 AND [City] = 'Berlin'")["Name"].to_list() == ["Anna", "Carla"]
    # NULL counts as false: Dieter has no city and is kept when removing Berlin rows
    assert expressions.filter_rows(people, "[City] = 'Berlin'", keep=False)["Name"].to_list() == ["ben", None, "Dieter"]


def test_errors_are_readable(people):
    with pytest.raises(ActionError, match=r"column \[Agee\] not found \(did you mean \[Age\]\?\)"):
        expressions.evaluate(people, "[Agee] + 1")
    with pytest.raises(ActionError) as error:  # the helper column used for row order stays hidden
        expressions.evaluate(people, "[zzzzz]")
    assert "__etl_row__" not in str(error.value)
    with pytest.raises(ActionError, match="one value per row"):
        expressions.evaluate(people, "SUM([Age])")
    with pytest.raises(ActionError, match="true or false"):
        expressions.filter_rows(people, "[Age] + 1")
    with pytest.raises(ActionError, match=r"to join text use \|\|"):
        expressions.evaluate(people, "[Name] + 'x'")
    with pytest.raises(ActionError, match=r"a \] is missing"):
        expressions.evaluate(people, "[Age")


def test_empty_table_keeps_types():
    empty = pl.DataFrame({"a": pl.Series([], dtype=pl.Int64)})
    out = expressions.add_columns(empty, [("b", "[a] * 1.5")])
    assert out.height == 0 and out.schema["b"] == pl.Float64


def test_tokens_keep_their_places():
    formula = "ROUND([Unit Price] * 2, 1) -- note\n|| 'it''s'"
    tokens = expressions.tokenize(formula)
    assert [t.kind for t in tokens if t.kind != "space"] == [
        "word", "open", "column", "operator", "number", "comma", "number", "close", "comment", "operator", "string",
    ]
    assert "".join(formula[t.start : t.end] for t in tokens) == formula
    unclosed = expressions.tokenize("[Name] || 'abc")
    assert unclosed[-1].kind == "string" and not unclosed[-1].closed
    assert expressions.column_names('[a]] b] + "c" + [a]] b]') == ["a] b", "c"]


def test_comments(people):
    assert expressions.evaluate(people, "[Age] * 2 -- with VAT")[0] == 68
    assert expressions.evaluate(people, "/* it's a note */ [Age]\n-- don't [count] this\n+ 1")[0] == 35
    assert expressions.translate("[a] -- [b]") == '"a"       '  # comments become spaces: places stay
    assert expressions.evaluate(people, "[Age] /* /* nested */ like DuckDB */ + 1")[0] == 35
    assert expressions.evaluate(people, "[Age]\r-- a line ends at a lone carriage return too\r+ 1")[0] == 35
    with pytest.raises(ActionError, match=r"a \*/ is missing"):  # the rest of the formula must not vanish
        expressions.evaluate(people, "[Age] > 30 /* older AND [City] = 'Berlin'")


def test_unusual_characters(people):
    assert expressions.evaluate(people, "$$a--b$$ || [Name]")[0] == "a--bAnna"  # DuckDB's $$ texts
    assert expressions.evaluate(people, "$x$:)$x$ || [Name]")[0] == ":)Anna"
    assert "".join(t.kind[0] for t in expressions.tokenize("x ² ①")) == "wswsw"  # letters for DuckDB
    with pytest.raises(ActionError, match="Formula error"):  # ² is not a digit for DuckDB
        expressions.evaluate(people, "[Age] * 10²")


def _kinds(formula):
    return [(t.kind, formula[t.start : t.end]) for t in expressions.tokenize(formula) if t.kind != "space"]


def test_texts_are_read_as_duckdb_reads_them():
    assert _kinds(r"E'\'' || e'a\\' || '\'") == [("string", r"E'\''"), ("operator", "||"), ("string", r"e'a\\'"), ("operator", "||"), ("string", "'\\'")]
    assert _kinds(r"_E'\'") == [("word", "_E"), ("string", r"'\'")]  # the E of a longer word starts no such text
    assert _kinds(r"éE'\'") == [("word", "éE"), ("string", r"'\'")]
    assert _kinds(r"1E'\''") == [("number", "1"), ("string", r"E'\''")]  # after a number it does
    assert _kinds(r"1_0E'\''") == [("number", "1_0"), ("string", r"E'\''")]
    assert _kinds(r"1.5_5E'\''") == [("number", "1.5_5"), ("string", r"E'\''")]
    assert _kinds(r"$1_0E'\''") == [("operator", "$1_0"), ("string", r"E'\''")]
    assert _kinds(r"?1aE'\'") == [("operator", "?1"), ("word", "aE"), ("string", r"'\'")]
    assert _kinds("b'1''0' X'1F'") == [("string", "b'1'"), ("string", "'0'"), ("string", "X'1F'")]
    assert _kinds("n'x'") == [("word", "n"), ("string", "'x'")]
    closed = expressions.tokenize(r"E'it\'s")[-1]
    assert closed.kind == "string" and not closed.closed


def test_texts_that_duckdb_joins():
    """A text on a later line is part of the text before it: after E'…' also with its backslashes."""
    for formula in ("E'a'\n'b\\'c'", "E'a' -- note\n 'b\\'c'", "e'a'\r'b\\'c'", "E'a'\f\n\t'b\\'c'", "E'a'\n'b'\n'c\\'d'", "E'a'/* a line\nends */'b\\'c'"):
        tokens = [t for t in expressions.tokenize(formula) if t.kind == "string"]
        assert all(t.prefix == "e" and t.closed for t in tokens) and [t.joined for t in tokens[:2]] == [False, True], formula
    for formula in ("E'a' 'b\\'", "E'a'/* same line */'b\\'", "E'a'\n||'b\\'", "$$a$$\n'b\\'", "E'a'\n\v'b\\'"):
        assert not any(t.prefix == "e" or t.joined for t in expressions.tokenize(formula)[1:]), formula
    assert _kinds("'a'\nE'b\\''")[1] == ("string", "E'b\\''")  # a text with its own prefix stands on its own


def test_dollars_and_letters_are_read_as_duckdb_reads_them():
    assert _kinds("a$b$c$b$") == [("word", "a$b$c$b$")]  # a $ goes on a word
    assert _kinds("$é$a'b$é$") == [("string", "$é$a'b$é$")]
    assert _kinds("$a1$x$a1$ $1$a$b$a$") == [("string", "$a1$x$a1$"), ("operator", "$1"), ("string", "$a$b$a$")]
    assert _kinds("١ a١ € x") == [("word", "١"), ("word", "a١"), ("word", "€ x")]  # from U+0080 on: letters
    assert _kinds("1..2 1.2.3 1__0") == [
        ("number", "1"), ("operator", ".."), ("number", "2"), ("number", "1.2"), ("number", ".3"), ("number", "1"), ("word", "__0"),
    ]  # fmt: skip
    assert _kinds("a::b:=c;d") == [("word", "a"), ("operator", "::"), ("word", "b"), ("operator", ":="), ("word", "c"), ("operator", ";"), ("word", "d")]


def test_wide_spaces_are_plain_spaces_before_duckdb_reads_them():
    """DuckDB's parser reads these as spaces outside texts, but only where its own quick look at
    the text finds them: what reaches it has none outside texts and names."""
    assert expressions.translate("[a] +　﻿1​⁠") == '"a" +  1  '
    assert expressions.translate("' ' || [ ]") == "' ' || \" \""
    assert _kinds(" E'\\''") == [("string", "E'\\''")]
    assert _kinds("$a $") == [("operator", "$"), ("word", "a"), ("operator", "$")]


_PIECES = (
    "E'", "e'", "'", "''", "\\", "\\'", "\\\\", "$$", "$a$", "$é$", "$_1$", '"', '""', "[", "]", "--", "/*", "*/", "\n", "\r", " ", "\t", "\f", "\v", "1", "1_0", ".", "..",
    "e", "E", "e5", "b'", "x'", "B'", "X'", "n'", "U&'", 'U&"', "a", "_", "é", " ", "​", "　", "﻿", " ", "\x85", "²", "١", "(", ")", ",", ";", ":",
    "::", ":=", "+", "-", "*", "/", "<", ">", "=", "!", "|", "&", "^", "~", "@", "#", "`", "?", "%", "{", "}", "\x01", "\x1c", "NOT", "NULLS", "WITH", "TIME", "$", "$1", "?1",
    "->", "=>", "'a'", "E'a'", "E'\\''", "x$a$", "' -- c\n'", "'/*\n*/'", "\\u", "\\x", "\\1", "'x'", "[z]", "1.5", "1e5", "||", "/* c */", "-- c\n",
)  # fmt: skip


def test_tokens_agree_with_duckdb_on_random_formulas(monkeypatch):
    """Random pieces, as far as DuckDB's scanner gets to the end: its tokens are ours. Formulas
    that aren't closed are compared too."""
    monkeypatch.setattr(expressions, "_unclosed", lambda formula, tokens: None)
    rng = random.Random(61)
    compared = 0
    for _ in range(30_000):
        formula = "".join(rng.choices(_PIECES, k=rng.randint(1, 12)))
        sql = f"SELECT (\n{expressions.translate(formula)}\n) AS value FROM __input ORDER BY __etl_row__"
        last = duckdb.tokenize(sql + " end_of_it")[-1:]
        if last and last[0][0] == len(sql.encode()) + 1:  # else DuckDB refuses it, e.g. a text that isn't closed
            compared += 1
            assert expressions._same_reading(sql), repr(formula)
    assert compared > 10_000


_MACRO = "CREATE OR REPLACE TEMP MACRO leftover() AS 42"
_BREAKOUTS = (
    rf"E'\'') AS value FROM __input; {_MACRO}; SELECT (E'\''",
    rf"e'\'') AS value FROM __input; {_MACRO}; SELECT (e'\''",
    rf"1_0E'\'') AS value FROM __input; {_MACRO}; SELECT (1_0E'\''",
    rf"$1_0E'\'') AS value FROM __input; {_MACRO}; SELECT ($1_0E'\''",
    f"x$a$) AS value FROM __input; {_MACRO}; SELECT (x$a$",
    f"$é$'$é$) AS value FROM __input; {_MACRO}; SELECT ('",
    f"E'a'\n'\\'') AS value FROM __input; {_MACRO}; SELECT (E'\\''",
    f"E'a'/*\n*/'\\'') AS value FROM __input; {_MACRO}; SELECT (E'\\''",
    f" E'\\'') AS value FROM __input; {_MACRO}; SELECT ( E'\\''",
)


def _left_on_the_connection():
    con = expressions._connection()
    return (
        con.execute("SELECT function_name FROM duckdb_functions() WHERE database_name = 'temp'").fetchall(),
        con.execute("SELECT table_name FROM duckdb_tables()").fetchall(),
        con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall(),
    )


def test_a_formula_cant_end_its_expression(people):
    for formula in _BREAKOUTS:
        with pytest.raises(ActionError, match="Formula error"):
            expressions.evaluate(people, formula)
    assert _left_on_the_connection() == ([], [], [])
    assert expressions.evaluate(people.head(1), r"E'\''")[0] == "'"  # the texts themselves are fine
    assert expressions.evaluate(people.head(1), "E'a\\tb'\n'c\\'d'")[0] == "a\tbc'd"
    assert expressions.evaluate(people.head(1), "'a' || [Name]　")[0] == "aAnna"


def test_blocked_functions_cant_hide_in_what_looks_like_a_text(people):
    hidden = (
        r"E'\'' || current_setting(E'threads') || E'\''",
        r"E'\'' || (SELECT value FROM duckdb_settings() WHERE name = E'\'threads\'') || E'\''",
        "x$a$||current_setting('threads')||x$a$",
        "current_setting​('threads')",
        "current_setting ('threads')",
        "current_setting⁠/* */('threads')",
        "duckdb_settings﻿()",
    )
    for formula in hidden:
        with pytest.raises(ActionError, match="a formula can only use the data of the step"):
            expressions.evaluate(people, formula)


def test_duckdb_itself_checks_every_formula(people, monkeypatch):
    """Should tokenize ever read a form wrongly, DuckDB's own reading stops the formula: here
    with the reading of E'…' texts taken away."""
    monkeypatch.setattr(expressions, "_TEXT_PREFIXES", {})
    with pytest.raises(ActionError, match="DuckDB doesn't read this as one expression"):
        expressions.evaluate(people, r"E'\'' || current_setting(E'threads') || E'\''")
    monkeypatch.undo()
    monkeypatch.setattr(expressions, "_unclosed", lambda formula, tokens: None)
    with pytest.raises(ActionError, match="DuckDB doesn't read this as one expression"):  # the check for ) is gone
        expressions.evaluate(people, f"1) AS value FROM __input; {_MACRO}; SELECT (1")
    monkeypatch.undo()
    assert _left_on_the_connection() == ([], [], [])
    assert expressions.evaluate(people, "[Age] + 1").to_list() == [35, 28, 42, 20, 59]


def test_duckdb_s_tokens_must_be_the_same_kind_and_all_be_there(monkeypatch):
    sql = "SELECT 'a' || x"
    assert expressions._same_reading(sql)
    tokens = expressions.tokenize(sql)
    as_text = [dataclasses.replace(token, kind="word") if token.kind == "string" else token for token in tokens]
    monkeypatch.setattr(expressions, "tokenize", lambda _sql: as_text)
    assert not expressions._same_reading(sql)  # a text for DuckDB, and a word here
    monkeypatch.setattr(expressions, "tokenize", lambda _sql: tokens[:3])
    assert not expressions._same_reading(sql)  # code after what was read here


def test_both_forms_of_a_time_zone_result_are_checked(people, monkeypatch):
    seen = []
    check = expressions._query
    monkeypatch.setattr(expressions, "_query", lambda con, sql, formula: seen.append(sql) or check(con, sql, formula))
    expressions.evaluate(people, "TIMESTAMPTZ '2026-01-01 10:00:00+00'")
    assert len(seen) == 2 and "CAST" in seen[1]


def test_a_pivot_without_its_values_says_what_it_needs(people):
    with pytest.raises(ActionError, match=r"a PIVOT in a formula needs its values: ON \[Column\] IN"):
        expressions.evaluate(people, "(SELECT x FROM (PIVOT __input ON Name USING sum(Age)) t(x) LIMIT 1)")


def test_a_very_long_word_needs_little_memory():
    assert [token.kind for token in expressions.tokenize("a" * 200_000)] == ["word"]
    assert [token.kind for token in expressions.tokenize("1" * 200_000)] == ["number"]


def test_a_shadowed_round_cant_reach_a_later_project(tmp_path, registry):
    """A formula can't leave anything on the connection that the next project uses."""
    (tmp_path / "a.csv").write_text("net\n10.26\n", encoding="utf-8")

    def project(formula):
        project = Project(name="Test")
        project.path = tmp_path / "test.tafl"
        table = project.add_table("T")
        project.add_step(table, "csv.import", {"file": "a.csv"})
        project.add_step(table, "calc.columns", {"columns": [{"name": "x", "formula": formula}]})
        return project

    poison = r"E'\'') AS value FROM __input; CREATE OR REPLACE TEMP MACRO round(a, b) AS system.main.round(a * 1.1, b); SELECT (E'\''"
    assert not Engine(project(poison), registry).evaluate("T").ok
    assert Engine(project("ROUND([net] * 1.19, 2)"), registry).evaluate("T").df["x"].to_list() == [12.21]
    assert _left_on_the_connection() == ([], [], [])


def test_functions_that_run_a_text_or_stay_in_force_are_refused(people):
    for formula in (
        "(SELECT a FROM json_execute_serialized_sql(json_serialize_sql('SELECT current_setting(''threads'') AS a')))",
        "json_serialize_plan('SELECT current_setting(''threads'')', optimize := TRUE)",  # the value is in the plan
        "(SELECT COUNT(*) FROM enable_logging(storage = 'file', storage_path = 'x'))",
        "(SELECT COUNT(*) FROM enable_logging())",
        "(SELECT COUNT(*) FROM disable_logging())",
        "(SELECT COUNT(*) FROM truncate_duckdb_logs())",
        "(SELECT COUNT(*) FROM enable_profiling())",
        "(SELECT COUNT(*) FROM disable_profiling())",
        "(SELECT COUNT(*) FROM checkpoint())",
        "(SELECT COUNT(*) FROM force_checkpoint())",
        "setseed(0.25)",  # RANDOM() of the next project would give the same numbers
        "(SELECT setseed(0.25))",
    ):
        with pytest.raises(ActionError, match="a formula can only use the data of the step"):
            expressions.evaluate(people, formula)
    assert expressions.evaluate(people, "[Age] + 1").to_list() == [35, 28, 42, 20, 59]


def test_half_a_character_in_a_formula(people):
    with pytest.raises(ActionError, match="broken character"):
        expressions.evaluate(people, "'\ud83d'")
    with pytest.raises(ActionError, match="broken character"):  # DuckDB's message cuts a ß in two
        expressions.evaluate(people, """U&'''{::,ß\\'WITH0"y\"""")
    with pytest.raises(ActionError, match="Formula error"):  # DuckDB stops reading at a NUL
        expressions.evaluate(people, "1\x00")


def test_parentheses_and_quotes_that_are_not_closed(people):
    with pytest.raises(ActionError, match=r"a \) is missing"):
        expressions.evaluate(people, "ROUND([Age] * 2")
    with pytest.raises(ActionError, match=r"one \) too many"):
        expressions.evaluate(people, "[Age] * 2)")
    with pytest.raises(ActionError, match="a closing ' is missing"):
        expressions.evaluate(people, "[Name] || 'x")
    with pytest.raises(ActionError, match="a closing ' is missing"):
        expressions.evaluate(people, r"[Name] || E'x\'")
    with pytest.raises(ActionError, match='a closing " is missing'):
        expressions.evaluate(people, '"Name')


def test_time_spans_give_a_clear_error(people):
    dated = people.with_columns(pl.date(2025, 1, 1).alias("Day"))
    for frame in (dated, dated.clear()):  # with no rows, Polars used to crash
        with pytest.raises(ActionError, match="time span"):
            expressions.evaluate(frame, "GETDATE() - [Day]")
    assert expressions.evaluate(dated, "TODAY() - [Day]").dtype == pl.Int64  # days
    with pytest.raises(ActionError, match="time span"):  # also inside a list
        expressions.evaluate(dated, "list_value(GETDATE() - [Day])")


def test_names_that_differ_only_in_case():
    df = pl.DataFrame({"Name": ["a"], "NAME": ["b"], "x": [1]})
    with pytest.raises(ActionError, match=r"\[Name\] and \[NAME\] differ only in upper and lower case"):
        expressions.evaluate(df, "[name] || 'x'")
    assert expressions.evaluate(df, "[x] + 1")[0] == 2  # other columns still work
    with pytest.raises(ActionError, match="differ only in upper and lower case"):  # also without brackets
        expressions.evaluate(df, "NAME || 'x'")
    umlauts = pl.DataFrame({"Äpfel": [1], "äpfel": [2]})  # DuckDB ignores the case of A-Z only
    assert expressions.evaluate(umlauts, "[Äpfel] * 10 + [äpfel]")[0] == 12


def test_helpful_messages(people):
    with pytest.raises(ActionError, match=r"column \[Scroe\] not found \(did you mean \[Score\]\?\)"):
        expressions.evaluate(people, "[Scroe] * 2")
    with pytest.raises(ActionError) as error:  # no suggestion is better than a wrong one
        expressions.evaluate(people, "[Quantity]")
    assert "did you mean" not in str(error.value)
    with pytest.raises(ActionError, match=r"there is no function UPPPER \(did you mean UPPER\?\)"):
        expressions.evaluate(people, "UPPPER([Name])")
    with pytest.raises(ActionError, match=r"there is no function EOMONTH: use LAST_DAY"):
        expressions.evaluate(people, "EOMONTH(TODAY())")
    with pytest.raises(ActionError, match=r"day is not a column: write the date part in quotes, DATEDIFF\('day'"):
        expressions.evaluate(people, "DATEDIFF(day, TODAY(), TODAY())")
    with pytest.raises(ActionError, match=r"DATEDIFF\('month', \.\.\.\)"):  # SQL Server's m: in DuckDB 'm' is minutes
        expressions.evaluate(people, "DATEDIFF(m, TODAY(), TODAY())")
    with pytest.raises(ActionError, match=r"column \[s\] not found"):  # not a date part outside a date function
        expressions.evaluate(people, "s + 1")
    with pytest.raises(ActionError, match=r"today is a function: write TODAY\(\)"):
        expressions.evaluate(people, "[Age] < today")
    with pytest.raises(ActionError, match=r"there is no function REPLICATE: use REPEAT"):
        expressions.evaluate(people, "REPLICATE('x', 3)")
    with pytest.raises(ActionError, match=r"there is no function TRY_CONVERT: use TRY_CAST"):
        expressions.evaluate(people, "TRY_CONVERT(INT, [Name])")
    with pytest.raises(ActionError, match=r"^Formula error: Overflow"):  # without DuckDB's "Out of Range Error:"
        expressions.evaluate(people, "CAST([Age] AS TINYINT) * 100")
    with pytest.raises(ActionError, match="The formula is empty"):
        expressions.evaluate(people, "-- only a note")
    with pytest.raises(ActionError) as error:  # DuckDB lists dozens of variants of +
        expressions.evaluate(people, "[Name] + 1")
    assert "Candidate" not in str(error.value) and len(str(error.value)) < 300


def test_a_condition_must_be_true_or_false(people):
    with pytest.raises(ActionError, match="this formula gives an integer"):
        expressions.filter_rows(people, "[Age] + 1")
    with pytest.raises(ActionError, match="this formula gives text"):
        expressions.filter_rows(people, "[Name]")


def test_results_a_column_cant_hold(people):
    with pytest.raises(ActionError, match="the result is a list of values"):
        expressions.evaluate(people, "STRING_SPLIT([City], 'r')")
    with pytest.raises(ActionError, match="structure of several values"):
        expressions.evaluate(people, "STRUCT_PACK(a := 1)")
    assert expressions.evaluate(people, "NOW()").dtype == pl.Datetime("us")  # local time, without a time zone
    with pytest.raises(ActionError, match="there is no function PROPER: for a capital first letter"):
        expressions.evaluate(people, "PROPER([Name])")


def test_columns_star_leaves_out_the_row_number():
    """The hidden column that keeps the rows in order is not one of the columns of COLUMNS(*)."""
    df = pl.DataFrame({"a1": [1, 2], "a2": [10, 20], "b": [100, 200]})
    assert expressions.evaluate(df, "COALESCE(*COLUMNS(*))").to_list() == [1, 2]
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*columns( * )))").to_list() == [111, 222]
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS(* EXCLUDE (b))))").to_list() == [11, 22]
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS(* EXCLUDE [b])))").to_list() == [11, 22]
    assert expressions.evaluate(df, "COUNT(*) OVER ()").to_list() == [2, 2]


def test_patterns_lambdas_and_queries_dont_see_the_row_number():
    """COLUMNS with a pattern or a lambda, and a query on __input, see only the table's columns,
    and the rows stay in their order."""
    df = pl.DataFrame({"a1": [1, 2, 3], "a2": [10, 20, 30], "b": [100, 300, 200]})
    total = [111, 322, 233]
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS('.*')))").to_list() == total
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS(c -> c LIKE '%')))").to_list() == total
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS(lambda c: true)))").to_list() == total
    assert expressions.evaluate(df, "TO_JSON(STRUCT_PACK(*COLUMNS('a1')))").to_list() == ['{"a1":1}', '{"a1":2}', '{"a1":3}']
    assert expressions.evaluate(df, "(SELECT SUM([a1]) FROM __input) + [a1]").to_list() == [7, 8, 9]
    ordered = "LIST_SUM(LIST_VALUE(*COLUMNS('a.*'))) * 1000 + ROW_NUMBER() OVER (ORDER BY [b] DESC)"
    assert expressions.evaluate(df, ordered).to_list() == [11003, 22001, 33002]
    with pytest.raises(ActionError, match='No matching columns found that match regex "_" Did you mean: "a1", "a2", "b"\nFormula'):
        expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS('_')))")
    with pytest.raises(ActionError, match="must give one value per row"):
        expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS('.*'))) + SUM([a1])")
    with pytest.raises(ActionError, match="don't work with a column named rowid"):  # DuckDB's own row numbers
        expressions.evaluate(df.rename({"b": "RowId"}), "LIST_SUM(LIST_VALUE(*COLUMNS('.*')))")
    assert expressions.evaluate(df.rename({"b": "RowId"}), "[RowId] + 1").to_list() == [101, 301, 201]


def _registered(monkeypatch):
    """The columns of the frames that DuckDB gets, from now on."""
    seen = []
    register = expressions._register
    monkeypatch.setattr(expressions, "_register", lambda con, frame, name: (seen.append(frame.columns), register(con, frame, name)))
    return seen


def test_duckdb_gets_only_the_columns_a_formula_names(monkeypatch):
    """The row number makes Polars line up the parts of all columns that DuckDB gets, which
    copies them. The values are those of the whole table, in all forms of writing a name."""
    df = pl.DataFrame({
        "Unit Price": [2, 3], "Qty": [1, 2], "a]b": [4, 5], 'q"r': [6, 7], "^x$": [8, 9], "*": [1, 1], "a$b": [1, 2], "Äpfel": [5, 6], "E": [7, 8],
        "unused": [0, 0], "also unused": [0, 0],
    })  # fmt: skip
    seen = _registered(monkeypatch)
    for formula, columns in {
        '[Unit Price] * "Qty"': ["Unit Price", "Qty"],
        "Qty + 1": ["Qty"],
        "qty + 1": ["Qty"],
        "[QTY] + 1": ["Qty"],
        '"qTy" + 1': ["Qty"],
        "[a]]b] + 1": ["a]b"],
        '"q""r" + 1': ['q"r'],
        "[^x$] + 1": ["^x$"],
        '"*" + 1': ["*"],
        "a$b + 1": ["a$b"],
        "Äpfel + [e]": ["Äpfel", "E"],
        "Qty.abs()": ["Qty"],
        "SUM(Qty) OVER (PARTITION BY [a]]b]) -- [E]": ["Qty", "a]b"],
        "(SELECT Qty + 1)": ["Qty"],
        "list_transform(list_value(1), x -> x + Qty).list_sum()": ["Qty"],
        "'[Qty]' || $$E$$ /* [E] */": [],
        "COUNT(*) OVER ()": [],
        "ROW_NUMBER() OVER (ORDER BY Qty DESC)": ["Qty"],
        "[__etl_row__] + 1": [],
        "1 + 1": [],
    }.items():
        seen.clear()
        values = expressions.evaluate(df, formula)
        assert seen == [["__etl_row__", *[c for c in df.columns if c in columns]]], formula
        with monkeypatch.context() as whole:
            whole.setattr(expressions, "_needs_all_columns", lambda formula: True)
            assert values.equals(expressions.evaluate(df, formula)), formula
    assert expressions.evaluate(df, "1 + 1").to_list() == [2, 2]  # one value per row, also without a column


def test_forms_that_use_columns_without_naming_them_get_the_whole_table(monkeypatch):
    df = pl.DataFrame({"a": [1, 2], "b": [30, 10], "c": ["x", "y"]})
    seen = _registered(monkeypatch)
    for formula, values in {
        "#2": [1, 2],  # the row number comes first
        "#3 + 1": [31, 11],
        "ROW_NUMBER() OVER (ORDER BY #3)": [2, 1],
        "LIST_SUM(LIST_VALUE(*COLUMNS(* EXCLUDE (c))))": [31, 12],
        "(* EXCLUDE (__etl_row__))": [1, 2],
        "CONCAT_WS('-', *COLUMNS(*))": ["1-30-x", "2-10-y"],
        'LENGTH(TO_JSON("__input"))': [38, 38],
        "LENGTH(TO_JSON([__input]))": [38, 38],
        '(SELECT MAX(z) FROM "__input" AS t(i, x, y, z))': ["y", "y"],
        "(SELECT MAX(z) FROM '__input' AS t(i, x, y, z))": ["y", "y"],  # named by a text
    }.items():
        seen.clear()
        assert expressions.evaluate(df, formula).to_list() == values, formula
        assert seen == [["__etl_row__", "a", "b", "c"]], formula


def test_a_column_duckdb_cant_read_only_matters_to_formulas_that_use_it():
    df = pl.DataFrame({"a": [1, 2], "big": pl.Series([2**70, 1], dtype=pl.Int128)})
    assert expressions.evaluate(df, "[a] + 1").to_list() == [2, 3]


def test_whole_numbers_of_128_bits():
    """Arrow, which hands the columns over to DuckDB, has no such numbers: they go as text and
    become HUGEINT and UHUGEINT again (an exact decimal would divide 5 // 2 into 2.5). A result
    of UHUGEINT from 2^127 on would come back negative from Arrow: it stops the formula."""
    df = pl.DataFrame({
        "id": pl.Series([2**127 - 1, -(2**127), None, 5], dtype=pl.Int128),
        "u": pl.Series([2**128 - 1, 0, None, 7], dtype=pl.UInt128),
        'a "b"': pl.Series([1, 2, 3, 4], dtype=pl.Int128),
    })  # fmt: skip
    assert expressions.filter_rows(df, "[id] > 0 AND [u] > 6")['a "b"'].to_list() == [1, 4]
    assert expressions.evaluate(df, "TYPEOF([id]) || ' ' || TYPEOF([u])").to_list() == ["HUGEINT UHUGEINT"] * 4
    assert expressions.evaluate(df.tail(2), "[id] // 2 + [u]").to_list() == [None, 9]
    assert expressions.evaluate(df, "COLUMNS('^i') = 5").to_list() == [False, False, None, True]  # on a table
    assert expressions.evaluate(df, '(SELECT MAX([a "b"]) FROM __input) + [a "b"]').to_list() == [5, 6, 7, 8]
    assert expressions.evaluate(df.tail(2), "[u]").to_list() == [None, 7]
    with pytest.raises(ActionError) as raised:
        expressions.evaluate(df, "[u]")
    assert str(raised.value) == (
        "Formula error: the whole number 340,282,366,920,938,463,463,374,607,431,768,211,455 is too big (at most "
        "170,141,183,460,469,231,731,687,303,715,884,105,727). For a decimal number write CAST(... AS DOUBLE)\nFormula: [u]"
    )
    assert expressions.evaluate(df, "CAST([u] AS DOUBLE)").to_list() == [2.0**128, 0.0, None, 7.0]
    con = expressions._connection()
    assert con.execute("SELECT view_name FROM duckdb_views() WHERE NOT internal").fetchall() == []
    assert con.execute("SELECT table_name FROM duckdb_tables()").fetchall() == []


def test_a_table_left_behind_by_an_interrupt_is_dropped():
    df = pl.DataFrame({"a": [1, 2]})
    con = expressions._connection()
    con.execute('CREATE TEMP TABLE "__input" AS SELECT 1 AS z')  # as if a formula on a table was stopped
    assert expressions.evaluate(df, "[a] + 1").to_list() == [2, 3]
    con.execute('CREATE TEMP TABLE "__input" AS SELECT 1 AS z')
    con.register("__frame", df)
    assert expressions.evaluate(df, "LIST_SUM(LIST_VALUE(*COLUMNS('a')))").to_list() == [1, 2]
    assert expressions.evaluate(df, "[a] * 2").to_list() == [2, 4]


def test_a_formula_that_ends_too_early(people):
    for formula in ("[Age] +", "CASE WHEN [Age] > 1 THEN 1", "[Age] > 1 AND\n-- a comment", "[Age] +\r"):
        with pytest.raises(ActionError, match="the formula ends too early"):
            expressions.evaluate(people, formula)
    for formula in ("ROUND([Age] 2)", "[Age] +\r 2 2"):  # a mistake inside, also after a lone \r
        with pytest.raises(ActionError, match='syntax error at or near "2"'):
            expressions.evaluate(people, formula)


def test_a_formula_uses_only_the_data_of_the_step(people, tmp_path):
    """Formulas are calculated as soon as a project opens, also one from someone else."""
    secret = tmp_path / "secret.csv"
    secret.write_text("a\n1\n", encoding="utf-8")
    path = secret.as_posix()
    for formula in (f"(SELECT COUNT(*) FROM read_csv('{path}'))", f"(SELECT content FROM read_text('{path}'))"):
        with pytest.raises(ActionError, match="a formula can only use the data of the step"):
            expressions.evaluate(people, formula)
    with pytest.raises(duckdb.Error, match="Cannot change configuration"):  # and it can't be switched back on
        expressions._connection().execute("SET enable_external_access = true")
    assert expressions.evaluate(people, "[Age] + 1").to_list() == [35, 28, 42, 20, 59]  # the data still works


def test_a_formula_cant_read_settings_or_its_temp_folder(people, tmp_path, monkeypatch):
    """DuckDB's settings show paths of this computer, and its temp folder would be readable."""
    for formula in ("current_setting('temp_directory')", "\"current_setting\"('threads')", "[current_setting]('threads')",
                    "(SELECT COUNT(*) FROM duckdb_settings())", "pragma_version()"):  # fmt: skip
        with pytest.raises(ActionError, match="a formula can only use the data of the step"):
            expressions.evaluate(people, formula)
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".tmp").mkdir()
    (tmp_path / ".tmp" / "inside.csv").write_text("a\n1\n", encoding="utf-8")
    with pytest.raises(ActionError, match="a formula can only use the data of the step"):
        expressions.evaluate(people, "(SELECT COUNT(*) FROM read_csv('.tmp/inside.csv'))")


def test_a_formula_cant_read_the_settings_as_a_table(people):
    """pg_settings shows the same as current_setting with a plain SELECT, in more ways of naming
    it than the words of a formula show."""
    for formula in (
        "(SELECT setting FROM pg_settings WHERE name = 'temp_directory')",
        "(SELECT setting FROM pg_catalog.pg_settings WHERE name = 'temp_directory')",
        "(SELECT setting FROM system.pg_catalog.pg_settings WHERE name = 'temp_directory')",
        "(SELECT setting FROM PG_Catalog.PG_Settings WHERE name = 'temp_directory')",
        "(SELECT setting FROM \"pg_settings\" WHERE name = 'temp_directory')",
        "(SELECT setting FROM \"PG_SETTINGS\" WHERE name = 'temp_directory')",
        "(SELECT setting FROM [pg_settings] WHERE name = 'temp_directory')",
        "(SELECT setting FROM 'pg_settings' WHERE name = 'temp_directory')",
        r"(SELECT setting FROM E'pg\x5fsettings' WHERE name = 'temp_directory')",
        "(SELECT setting FROM $$pg_settings$$ WHERE name = 'temp_directory')",
        "(SELECT MAX(setting) FROM __input, pg_settings)",
        "(SELECT 1 FROM __input POSITIONAL JOIN pg_settings LIMIT 1)",
        "(SELECT 1 FROM __input JOIN /* a */ pg_settings ON TRUE LIMIT 1)",
        "(FROM pg_settings SELECT MAX(setting))",
        "(WITH t AS (FROM pg_settings) SELECT MAX(setting) FROM t)",
        "EXISTS (FROM pg_settings)",
        "(SELECT MAX(setting) FROM (TABLE pg_settings))",
        "(SELECT MAX(max) FROM (SUMMARIZE pg_settings))",
        "(SELECT COUNT(*) FROM (DESCRIBE pg_settings))",
        "(SELECT COUNT(*) FROM (SHOW pg_settings))",
        "(SELECT COUNT(*) FROM (UNPIVOT pg_settings ON name INTO NAME a VALUE b))",
        "(SELECT COUNT(*) FROM (PIVOT pg_settings ON name IN ('temp_directory') USING FIRST(setting)))",
    ):
        with pytest.raises(ActionError, match="a formula can only use the data of the step"):
            expressions.evaluate(people, formula)
    with pytest.raises(ActionError, match="a PIVOT in a formula needs its values"):  # it needs a statement before it
        expressions.evaluate(people, "(SELECT COUNT(*) FROM (PIVOT __input ON [Age] IN (SELECT setting FROM pg_settings)))")


def test_a_table_named_by_a_text_is_refused(people):
    """A text can be built at run time, so it can't be checked: what reads a table by a text can't run."""
    for formula in (
        "(SELECT COUNT(*) FROM histogram_values('pg_' || 'settings', setting))",
        "(SELECT MAX(bin) FROM histogram_values('pg_settings', name, technique := 'sample'))",
        "(SELECT COUNT(*) FROM histogram('pg_' || 'settings', setting))",
        "(SELECT COUNT(*) FROM \"histogram_values\"('pg_settings', setting))",
        "(SELECT COUNT(*) FROM [HISTOGRAM]('pg_settings', setting))",
        "(SELECT COUNT(*) FROM system.main.histogram_values('pg_settings', setting))",
        "(SELECT COUNT(*) FROM __input, LATERAL histogram_values('pg_settings', setting))",
        "(SELECT COUNT(*) FROM histogram_values('__input', [Age]))",
        "(SELECT COUNT(*) FROM histogram_values('(SELECT current_setting(''threads'') AS v)', v))",
    ):
        with pytest.raises(ActionError, match="a formula can only use the data of the step"):
            expressions.evaluate(people, formula)


def test_the_names_of_those_tables_stay_free_for_columns_and_texts():
    frame = pl.DataFrame({"pg_settings": [1, 2], "histogram": [3, 4], "histogram_values": [5, 6], "note": ["pg_settings", "x"]})
    for formula, expected in (
        ("[pg_settings] + 1", [2, 3]),
        ('"pg_settings" + 1', [2, 3]),
        ("pg_settings + 1", [2, 3]),
        ("PG_SETTINGS + 1", [2, 3]),
        ("__input.pg_settings + 1", [2, 3]),
        ("COALESCE([pg_settings], 0) + 1", [2, 3]),
        ("(SELECT MAX(pg_settings) FROM __input)", [2, 2]),
        ("[histogram] + [histogram_values]", [8, 10]),
        ("histogram + histogram_values", [8, 10]),
        ("[note] = 'pg_settings'", [True, False]),
        ("[note] LIKE 'pg_%'", [True, False]),
        ("'FROM pg_settings'", ["FROM pg_settings"] * 2),
        ("CARDINALITY(HISTOGRAM([histogram]) OVER ())", [2, 2]),  # the normal function of that name
        ("(SELECT CARDINALITY(HISTOGRAM(histogram)) FROM __input)", [2, 2]),
        ("CARDINALITY(HISTOGRAM_EXACT([histogram], LIST_VALUE(3, 4)) OVER ())", [2, 2]),
        ("CARDINALITY(LIST_HISTOGRAM(LIST_VALUE([note], [note])))", [1, 1]),
    ):
        assert expressions.evaluate(frame, formula).to_list() == expected, formula


def test_catalog_views_show_only_the_table_of_the_formula(people):
    assert expressions.evaluate(people.head(1), "(SELECT string_agg(table_name) FROM information_schema.tables)")[0] == "__input"
    assert expressions.evaluate(people.head(1), "(SELECT COUNT(*) FROM pg_catalog.pg_type)")[0] > 0


def test_every_formula_passes_through_the_tree_of_duckdb(people):
    """It can be read for every statement that DuckDB reads, however deep. Where it can't, the answer is no."""
    assert expressions.evaluate(people.head(1), "CASE WHEN TRUE THEN " * 400 + "1" + " END" * 400)[0] == 1
    assert expressions._reads_blocked_table(expressions._connection(), "(SELECT")
    assert not expressions._reads_blocked_table(expressions._connection(), "SELECT 1")


def test_nothing_else_in_duckdb_leads_to_the_settings():
    """An update of DuckDB can bring a new way: then it belongs in the blocked names."""
    con = expressions._connection()
    views = con.execute("SELECT DISTINCT view_name FROM duckdb_views() WHERE sql ILIKE '%setting%'").fetchall()
    assert views == [("pg_settings",)]
    macros = con.execute(
        "SELECT DISTINCT function_name FROM duckdb_functions() WHERE function_type IN ('macro', 'table_macro') "
        r"AND regexp_matches(macro_definition, '(?i)\b(query|query_table|histogram_values)\s*\(|setting')"
    ).fetchall()
    assert {name for name, in macros if not name.startswith(expressions._BLOCKED_PREFIXES)} == expressions._BLOCKED_TABLES - {"pg_settings"}


def test_functions_of_extensions_and_unknown_types(people):
    with pytest.raises(ActionError, match=r"there is no function TEXT: for a date use STRFTIME") as error:
        expressions.evaluate(people, "TEXT([Age], '0')")
    assert "INSTALL" not in str(error.value)
    suggested = re.search(r"for a number (FORMAT\(.+\))$", str(error.value).split("\nFormula")[0]).group(1)
    assert expressions.evaluate(people, suggested.replace("[Amount]", "[Age]")).to_list()[0] == "34.00"  # also for whole numbers
    with pytest.raises(ActionError, match="there is no type INTEGR"):
        expressions.evaluate(people, "CAST([Age] AS INTEGR)")


def test_time_zones_collations_and_json_work_without_loading_extensions():
    """Formulas can't load extensions: these must be part of DuckDB itself (as in 1.5)."""
    df = pl.DataFrame({"d": [dt.datetime(2026, 3, 29, 0, 30)], "s": ['{"a": 1}'], "t": ["Äpfel"]})
    assert expressions.evaluate(df, "NOW() AT TIME ZONE 'UTC'").dtype == pl.Datetime("us")  # the GETUTCDATE hint
    assert expressions.evaluate(df, "timezone('Europe/Berlin', [d]::TIMESTAMPTZ)").len() == 1
    assert expressions.evaluate(df, "[t] COLLATE de < 'Birnen'").to_list() == [True]
    assert expressions.evaluate(df, "json_extract_string([s], '$.a')").to_list() == ["1"]


def test_a_view_left_behind_by_an_interrupt_is_dropped(people):
    """An interrupted unregister can leave the view a formula reads: the formulas of that thread
    must keep working."""
    con = expressions._connection()
    con.execute('CREATE TEMP VIEW "__input" AS SELECT 1 AS left_over')
    with pytest.raises(duckdb.CatalogException, match="already exists"):  # what every formula met before
        con.register("__input", people)
    assert expressions.evaluate(people, "[Age] + 1").to_list() == [35, 28, 42, 20, 59]
    assert expressions.evaluate(people, "[Age] * 2").to_list() == [68, 54, 82, 38, 116]


def test_an_interrupted_unregister_is_no_error(people, monkeypatch):
    class Interrupted:
        """The formula connection, interrupted while it unregisters the view."""

        def __init__(self, con):
            self._con = con

        def __getattr__(self, name):
            return getattr(self._con, name)

        def unregister(self, name):
            raise duckdb.InterruptException("INTERRUPT Error: Interrupted!")

    con = expressions._connection()
    monkeypatch.setattr(expressions, "_connection", lambda: Interrupted(con))
    assert expressions.evaluate(people, "[Age] + 1").to_list() == [35, 28, 42, 20, 59]
    monkeypatch.undo()
    assert expressions.evaluate(people.head(1), "[Name]").to_list() == ["Anna"]


def test_interrupts_while_registering_the_view(people):
    """An interrupt can hit any of DuckDB's calls of a formula: it is an error of that formula only."""
    con = expressions._connection()
    stop = threading.Event()

    def keep_interrupting():
        while not stop.is_set():
            con.interrupt()
            time.sleep(0.0005)

    thread = threading.Thread(target=keep_interrupting, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            try:
                expressions.evaluate(people, "[Age] + 1")
            except ActionError as e:
                assert "Interrupted" in str(e)
    finally:
        stop.set()
        thread.join()
    assert expressions.evaluate(people, "[Age] + 1").to_list() == [35, 28, 42, 20, 59]

import polars as pl
import pytest

from etl_tool.api import ActionError, expressions


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
    assert "".join(t.kind[0] for t in expressions.tokenize("x ² ①")) == "wsoso"
    with pytest.raises(ActionError, match="Formula error"):  # ² is not a digit for DuckDB
        expressions.evaluate(people, "[Age] * 10²")


def test_parentheses_and_quotes_that_are_not_closed(people):
    with pytest.raises(ActionError, match=r"a \) is missing"):
        expressions.evaluate(people, "ROUND([Age] * 2")
    with pytest.raises(ActionError, match=r"one \) too many"):
        expressions.evaluate(people, "[Age] * 2)")
    with pytest.raises(ActionError, match="a closing ' is missing"):
        expressions.evaluate(people, "[Name] || 'x")


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

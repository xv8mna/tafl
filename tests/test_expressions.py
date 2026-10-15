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
    with pytest.raises(ActionError, match="Missing ]"):
        expressions.evaluate(people, "[Age")


def test_empty_table_keeps_types():
    empty = pl.DataFrame({"a": pl.Series([], dtype=pl.Int64)})
    out = expressions.add_columns(empty, [("b", "[a] * 1.5")])
    assert out.height == 0 and out.schema["b"] == pl.Float64

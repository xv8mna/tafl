import datetime as dt

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.testing import StubContext


def replace(act, df, columns, pairs, ctx=None, **options):
    params = {"columns": columns, "replacements": [{"find": f, "replace": r} for f, r in pairs], **options}
    return act("text.replace", df, params, ctx)


def test_replace_whole_values(act):
    df = pl.DataFrame({"s": ["open", "OPEN", "done", None, " "]})
    out = replace(act, df, ["s"], [("open", "o"), ("", "missing"), ("done", "")], case_sensitive=False)
    assert out["s"].to_list() == ["o", "o", None, "missing", "missing"]


def test_replace_inside_text_and_regex(act):
    df = pl.DataFrame({"s": ["a-b-A", "Price: $5"]})
    assert replace(act, df, ["s"], [("a", "x")], match="contains")["s"].to_list() == ["x-b-A", "Price: $5"]
    assert replace(act, df, ["s"], [("a", "$")], match="contains", case_sensitive=False)["s"].to_list() == ["$-b-$", "Price: $5"]
    regex = replace(act, df, ["s"], [(r"(\w)-(\w)", "$2+$1")], match="regex")
    assert regex["s"].to_list() == ["b+a-A", "Price: $5"]
    with pytest.raises(ActionError, match="Invalid regular expression"):
        replace(act, df, ["s"], [("(", "x")], match="regex")


def test_replace_in_number_columns(act):
    df = pl.DataFrame({"n": [0, 1, None]})
    assert replace(act, df, ["n"], [("0", "100"), ("", "-1")])["n"].to_list() == [100, 1, -1]
    ctx = StubContext()
    out = replace(act, df, ["n"], [("0", "zero")], ctx)
    assert out["n"].to_list() == ["zero", "1", None]
    assert ctx.warnings


def test_clean_text(act):
    df = pl.DataFrame({"s": ["  müller  gmbh ", "a\nb", "   "], "zip": [1067, 80331, None]})
    out = act(
        "text.clean",
        df,
        {"columns": ["s"], "collapse_spaces": True, "line_breaks": True, "case": "title"},
    )
    assert out["s"].to_list() == ["Müller Gmbh", "A B", None]
    padded = act("text.clean", df, {"columns": ["zip"], "pad_length": 5})
    assert padded["zip"].to_list() == ["01067", "80331", None]


def test_invalid_regular_expressions_say_why(act):
    df = pl.DataFrame({"s": ["abc"]})
    with pytest.raises(ActionError, match=r"^Invalid regular expression '\(abc': unclosed group$"):
        replace(act, df, ["s"], [("a", "b"), ("(abc", "x")], match="regex")
    with pytest.raises(ActionError, match=r"'a\{2,1\}': invalid repetition count range"):
        replace(act, df, ["s"], [("a{2,1}", "x")], match="regex", case_sensitive=False)


def test_replace_typed_values(act):
    df = pl.DataFrame({"d": [dt.date(2025, 12, 31), None], "n": [1234567890123456789, 1234567890123456768]})
    out = replace(act, df, ["d"], [("31.12.25", "01.01.26")])
    assert out["d"].to_list() == [dt.date(2026, 1, 1), None]
    out = replace(act, df, ["n"], [("1234567890123456789", "1")])
    assert out["n"].to_list() == [1, 1234567890123456768]


def test_upper_case_writes_sharp_s_as_ss_like_formulas(act):
    df = pl.DataFrame({"s": ["Größe", "ẞ"]})
    out = act("text.clean", df, {"columns": ["s"], "case": "upper"})
    assert out["s"].to_list() == ["GRÖSSE", "SS"]
    formula = act("calc.columns", df, {"columns": [{"name": "s", "formula": "UPPER([s])"}]})
    assert formula["s"].to_list() == out["s"].to_list()

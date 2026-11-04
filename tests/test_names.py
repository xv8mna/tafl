"""Columns by their exact names: ``col``, ``cols`` and ``selector`` of the plugin API. Polars reads
* as all columns and a name that starts with ^ and ends with $ as a regular expression; these take
such names as they are, also in the Polars methods that the built-in actions give them to.

The values are compared as lists: Polars' own Series methods can't find a Series named ^...$."""

import re
import textwrap
from pathlib import Path

import polars as pl
import pytest

import tafl.api
from tafl.api import col, cols, selector
from tafl.api.testing import StubContext
from tafl.core import Registry

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def named() -> pl.DataFrame:
    """Columns named like patterns, beside the columns those patterns match."""
    return pl.DataFrame({"*": [1, 2, 3], "^a.*$": [4, 5, 6], "^x$": [7, 8, 9], "abc": [10, 20, 30], "x": [40, 50, 60]})


@pytest.mark.parametrize("name", ["*", "^a.*$", "^x$", "abc", "x"])
def test_a_column_is_taken_by_its_exact_name(named, name):
    picked = named.select(col(name))
    assert picked.columns == [name] and picked.to_series().to_list() == named[name].to_list()
    changed = named.with_columns(col(name) * 2)
    assert changed.columns == named.columns
    assert [c for c in named.columns if changed[c].to_list() != named[c].to_list()] == [name]


def test_cols_keep_the_order_and_a_repeat(named):
    assert named.select(cols(["^x$", "x", "*"])).columns == ["^x$", "x", "*"]
    assert named.select(cols(iter(["abc", "^a.*$"]))).columns == ["abc", "^a.*$"]  # any iterable
    assert cols([]) == []
    with pytest.raises(pl.exceptions.DuplicateError):  # as with the names as text
        named.select(cols(["x", "x"]))
    with pytest.raises(pl.exceptions.DuplicateError):
        named.select(cols(["*", "*"]))


@pytest.mark.parametrize("name", ["nope", "^a.*$", "*"])
def test_a_missing_column_is_an_error(name):
    other = pl.DataFrame({"abc": [1], "x": [2]})  # the patterns would match these
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        other.select(col(name))
    with pytest.raises(pl.exceptions.ColumnNotFoundError):
        other.unpivot(on=selector([name]))


def test_selector_keeps_the_order_of_the_names_for_unpivot(named):
    df = pl.DataFrame({"a": [1], "b": ["k"], "c": [2]})
    assert df.unpivot(on=selector(["c", "a"]), index=selector(["b"]))["variable"].to_list() == ["c", "a"]
    assert df.unpivot(on=selector(["a", "c"]), index=selector([]))["variable"].to_list() == ["a", "c"]
    turned = named.unpivot(on=selector(["^x$", "*"]), index=selector(["x"]))
    assert turned.rows() == [(40, "^x$", 7), (50, "^x$", 8), (60, "^x$", 9), (40, "*", 1), (50, "*", 2), (60, "*", 3)]


def test_the_methods_the_actions_use_take_exact_names():
    """The Polars methods that read names as text take these expressions instead: a later Polars
    that changes that makes this test fail."""
    df = pl.DataFrame({"*": [2, 1, 2, None], "^a.*$": ["p", "q", "p", "r"], "abc": [1, 1, 1, 1], "x": [9, 8, 7, 6]})
    assert df.drop(selector(["*"])).columns == ["^a.*$", "abc", "x"]
    assert df.sort(cols(["*", "^a.*$"]), descending=[True, False], nulls_last=True)["x"].to_list() == [9, 7, 8, 6]
    assert df.unique(subset=cols(["*"]), keep="first", maintain_order=True)["x"].to_list() == [9, 8, 6]
    grouped = df.group_by(cols(["^a.*$"]), maintain_order=True).agg(col("x").sum())
    assert grouped.rows() == [("p", 16), ("q", 8), ("r", 6)]
    numbered = df.select(pl.int_range(pl.len()).over(cols(["*"])).alias("n"))
    assert numbered["n"].to_list() == [0, 0, 1, 0]
    assert df.select(pl.struct(cols(["*", "^a.*$"])).n_unique()).item() == 3
    assert df.select(pl.coalesce(col("*"), col("x")).alias("c"))["c"].to_list() == [2, 1, 2, 6]
    other = pl.DataFrame({"*": [2, 3], "y": ["two", "three"]})
    joined = df.join(other, left_on=cols(["*"]), right_on=cols(["*"]), how="left", coalesce=True, maintain_order="left")
    assert joined.columns == ["*", "^a.*$", "abc", "x", "y"] and joined["y"].to_list() == ["two", None, "two", None]
    lists = pl.DataFrame({"*": [["a", "b"], []], "abc": [1, 2]})
    assert lists.explode(col("*"), empty_as_null=True, keep_nulls=True).rows() == [("a", 1), ("b", 1), (None, 2)]


def test_the_helpers_are_part_of_the_plugin_api():
    assert {"col", "cols", "selector"} <= set(tafl.api.__all__)
    assert tafl.api.API_VERSION == 1  # an addition: plugins for version 1 keep working


def _docstring_example() -> str:
    """The plugin in the docstring of tafl.api: the indented lines after "A minimal plugin::"."""
    lines = tafl.api.__doc__.split("A minimal plugin::\n", 1)[1].splitlines()
    end = next(i for i, line in enumerate(lines) if line and not line.startswith("    "))
    return textwrap.dedent("\n".join(lines[:end]))


def _markdown_example(path: Path, heading: str) -> str:
    """The first Python example below a heading of a Markdown file."""
    text = path.read_text(encoding="utf-8")
    return re.search(r"```python\n(.*?)```", text[text.index(heading) :], re.S).group(1)


@pytest.mark.parametrize(
    ("code", "action_id", "params"),
    [
        (_docstring_example, "example.upper", {"column": "*"}),
        (lambda: _markdown_example(ROOT / "README.md", "## Writing a plugin"), "my.prefix", {"columns": ["*"], "prefix": "#"}),
        (lambda: _markdown_example(ROOT / "docs" / "guide.md", "### A small plugin"), "my.prefix", {"columns": ["*"], "prefix": "#"}),
    ],
    ids=["api", "readme", "guide"],
)
def test_the_plugin_examples_change_only_the_column_named_star(tmp_path, code, action_id, params):
    (tmp_path / "example.py").write_text(code(), encoding="utf-8")
    registry = Registry()
    registry.load_folder(tmp_path)
    assert not registry.errors, registry.errors
    df = pl.DataFrame({"*": ["a", "b"], "abc": ["c", "d"]})
    out = registry.actions[action_id](df, params, StubContext())
    assert out.columns == ["*", "abc"]
    assert out["abc"].to_list() == ["c", "d"]
    assert out["*"].to_list() in (["A", "B"], ["#a", "#b"])

"""Columns that settings or data name are taken with ``col(name)`` or ``cols(names)`` from tafl.api,
never with ``pl.col(name)``: Polars reads * as all columns and a name that starts with ^ and ends
with $ as a pattern. This reads the source of tafl: ``pl.col`` only gets fixed texts and Polars
types (only tafl/api/names.py builds columns from names)."""

import ast
from pathlib import Path

import pytest

import tafl

SOURCE = Path(tafl.__file__).parent  # the tafl that runs: also a copy of it
EXACT = SOURCE / "api" / "names.py"


def _polars_names(tree: ast.Module) -> set[str]:
    """The names the module gives Polars and its functions: pl for ``import polars as pl``."""
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in ("polars", "polars.functions"):
                    names.add(alias.asname or "polars")
        elif isinstance(node, ast.ImportFrom) and node.module == "polars":
            names.update(alias.asname or alias.name for alias in node.names if alias.name == "functions")
    return names


def _is_polars(node: ast.expr, names: set[str]) -> bool:
    """pl, or pl.functions."""
    if isinstance(node, ast.Attribute) and node.attr == "functions":
        node = node.value
    return isinstance(node, ast.Name) and node.id in names


def _fixed(node: ast.expr, names: set[str]) -> bool:
    """A text written in the code, or a Polars type (pl.String, pl.Datetime("us")), or a list of them."""
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    if isinstance(node, (ast.List, ast.Tuple)):
        return all(_fixed(item, names) for item in node.elts)
    if isinstance(node, ast.Call):
        node = node.func
    return isinstance(node, ast.Attribute) and node.attr[:1].isupper() and _is_polars(node.value, names)


def _places(path: Path) -> list[str]:
    """Where the file takes a column with ``pl.col`` and something other than a fixed name."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names = _polars_names(tree)
    called = {id(node.func): node for node in ast.walk(tree) if isinstance(node, ast.Call)}
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in ("polars", "polars.functions"):
            if any(alias.name == "col" for alias in node.names):
                found.append(f"{path}:{node.lineno}: col imported from {node.module}")
        elif isinstance(node, ast.Attribute) and node.attr == "col" and _is_polars(node.value, names):
            call = called.get(id(node))
            if call is None or call.keywords or not all(_fixed(arg, names) for arg in call.args):
                found.append(f"{path}:{node.lineno}: {ast.unparse(call or node)}")
    return found


def test_columns_named_by_settings_or_data_are_taken_by_their_exact_names():
    paths = sorted(path for path in SOURCE.rglob("*.py") if path != EXACT)
    assert len(paths) > 50  # the whole package
    found = [place for path in paths for place in _places(path)]
    assert not found, "Use col(name) or cols(names) from tafl.api (Polars reads * and ^...$ as patterns):\n" + "\n".join(found)


@pytest.mark.parametrize(
    ("code", "count"),
    [
        ('import polars as pl\npl.col("value")', 0),
        ('import polars as pl\npl.col(["value", "count"])', 0),
        ("import polars as pl\npl.col(pl.String)", 0),
        ('import polars as pl\npl.col(pl.Datetime("us"), pl.List(pl.String))', 0),
        ("from tafl.api import col\ncol(name)", 0),
        ("import polars as pl\npl.col(name)", 1),
        ("import polars as pl\npl.col(p.column).str.strip_chars()", 1),
        ('import polars as pl\npl.col(f"{name} 1")', 1),
        ('import polars as pl\npl.col("value", name)', 1),
        ("import polars as pl\npl.col(names[0])", 1),
        ("import polars as pl\npl.col(*names)", 1),
        ("import polars as pl\npl.col(pl.lit(1))", 1),
        ("import polars as pl\ncolumns = list(map(pl.col, names))", 1),
        ("import polars\npolars.col(name)", 1),
        ("import polars.functions as F\nF.col(name)", 1),
        ("from polars import functions\nfunctions.col(name)", 1),
        ("from polars import col\ncol(name)", 1),
    ],
)
def test_the_check_finds_a_column_taken_by_a_name(tmp_path, code, count):
    path = tmp_path / "plugin.py"
    path.write_text(code, encoding="utf-8")
    assert len(_places(path)) == count

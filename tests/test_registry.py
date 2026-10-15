import textwrap

from etl_tool.core import Registry

GOOD = '''
import polars as pl
from etl_tool.api import P, action

@action("demo.upper", "Upper case", category="Text", params=[P.Column("column", "Column")])
def upper(df, p, ctx):
    """Upper-cases a column."""
    return df.with_columns(pl.col(p.column).str.to_uppercase())
'''

DUPLICATE = '''
from etl_tool.api import action

@action("rows.sort", "My own sort")
def sort(df, p, ctx):
    return df
'''

TOO_NEW = '''
REQUIRES_API = 99
from etl_tool.api import action

@action("demo.future", "Future")
def future(df, p, ctx):
    return df
'''


def write(folder, name, code):
    (folder / name).write_text(textwrap.dedent(code), encoding="utf-8")


def test_external_plugins_load_and_failures_are_isolated(tmp_path):
    write(tmp_path, "good.py", GOOD)
    write(tmp_path, "broken.py", "def oops(:\n")
    write(tmp_path, "duplicate.py", DUPLICATE)
    write(tmp_path, "too_new.py", TOO_NEW)
    write(tmp_path, "_ignored.py", "raise RuntimeError('must not be imported')")
    package = tmp_path / "pkg_plugin"
    package.mkdir()
    write(package, "__init__.py", "from .actions import *\n")
    write(package, "actions.py", GOOD.replace("demo.upper", "demo.pkg_upper"))

    registry = Registry()
    registry.load_builtin()
    registry.load_folder(tmp_path)

    plugins = {p.name: p for p in registry.plugins}
    assert plugins["good"].ok and plugins["good"].actions == ["demo.upper"]
    assert plugins["pkg_plugin"].actions == ["demo.pkg_upper"]
    assert "SyntaxError" in plugins["broken"].errors[0]
    assert "already used by plugin 'rows'" in plugins["duplicate"].errors[0]
    assert "Needs plugin API version 99" in plugins["too_new"].errors[0]
    assert "_ignored" not in plugins
    assert registry.actions["rows.sort"].name == "Sort"  # the built-in one stays
    assert registry.actions["demo.upper"].plugin == "good"
    assert registry.actions["demo.upper"].description == "Upper-cases a column."


def test_actions_grouped_by_category(registry):
    groups = registry.by_category()
    assert {"Import", "Export", "Rows", "Columns", "Text", "Calculate", "Combine"} <= set(groups)
    assert all(a.category == "Import" for a in groups["Import"])

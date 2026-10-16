import textwrap

from tafl.core import Registry

GOOD = '''
import polars as pl
from tafl.api import P, action

@action("demo.upper", "Upper case", category="Text", params=[P.Column("column", "Column")])
def upper(df, p, ctx):
    """Upper-cases a column."""
    return df.with_columns(pl.col(p.column).str.to_uppercase())
'''

DUPLICATE = '''
from tafl.api import action

@action("rows.sort", "My own sort")
def sort(df, p, ctx):
    return df
'''

TOO_NEW = '''
REQUIRES_API = 99
from tafl.api import action

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


def test_a_plugin_for_the_earlier_package_name_is_told_what_to_import(tmp_path):
    """Plugins written for 0.1.0 import etl_tool.api: the error says what changed."""
    write(tmp_path, "old.py", GOOD.replace("from tafl.api", "from etl_tool.api"))
    write(tmp_path, "other.py", "import no_such_module_here\n")
    registry = Registry()
    registry.load_folder(tmp_path)

    plugins = {p.name: p for p in registry.plugins}
    error = plugins["old"].errors[0]
    assert "No module named 'etl_tool'" in error
    assert error.splitlines()[-1].startswith("Since Tafl 0.2.0, plugins import from tafl.api instead of etl_tool.api")
    assert "tafl.api" not in plugins["other"].errors[0]


def test_the_hint_also_with_left_over_folders_and_the_old_error_name(tmp_path, monkeypatch):
    """A folder updated from 0.1.0 can keep src/etl_tool/api with only __pycache__ in it: then
    etl_tool.api can be imported, without anything in it."""
    (tmp_path / "left" / "etl_tool" / "api" / "__pycache__").mkdir(parents=True)
    monkeypatch.syspath_prepend(str(tmp_path / "left"))
    plugins_folder = tmp_path / "plugins"
    plugins_folder.mkdir()
    write(plugins_folder, "old.py", GOOD.replace("from tafl.api", "from etl_tool.api"))
    write(plugins_folder, "half.py", "from tafl.api import EtlError\n")
    registry = Registry()
    registry.load_folder(plugins_folder)

    for name in ("old", "half"):
        error = next(p for p in registry.plugins if p.name == name).errors[0]
        assert error.splitlines()[-1].startswith("Since Tafl 0.2.0, plugins import from tafl.api"), error
        assert "EtlError is called TaflError now" in error


def test_actions_grouped_by_category(registry):
    groups = registry.by_category()
    assert {"Import", "Export", "Rows", "Columns", "Text", "Calculate", "Combine"} <= set(groups)
    assert all(a.category == "Import" for a in groups["Import"])

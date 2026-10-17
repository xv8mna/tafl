import sys
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


def test_a_plugin_that_ends_the_program_while_loading_is_a_load_error(tmp_path):
    """sys.exit() at the top of a plugin used to end the app before it started."""
    write(tmp_path, "exits.py", "import sys\nsys.exit('plugin says bye')\n")
    write(tmp_path, "interrupts.py", "raise KeyboardInterrupt\n")
    write(tmp_path, "good.py", GOOD)
    registry = Registry()
    registry.load_folder(tmp_path)

    plugins = {p.name: p for p in registry.plugins}
    assert plugins["exits"].errors[0].splitlines()[-1] == "SystemExit: plugin says bye"
    assert plugins["interrupts"].errors[0].splitlines()[-1] == "KeyboardInterrupt"
    assert registry.actions["demo.upper"].plugin == "good"
    assert "tafl_ext_exits" not in sys.modules


def test_plugin_folders_that_dont_exist_get_a_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))  # its plugin folder may be missing
    monkeypatch.setenv("TAFL_PLUGINS", str(tmp_path / "from_env"))
    write(tmp_path, "a_file.py", GOOD)
    registry = Registry.default([tmp_path / "given", tmp_path / "a_file.py", tmp_path])
    assert registry.warnings == [
        f"The plugin folder {tmp_path / 'from_env'} does not exist",
        f"The plugin folder {tmp_path / 'given'} does not exist",
        f"The plugin folder {tmp_path / 'a_file.py'} is not a folder",
    ]
    assert "demo.upper" in registry.actions


def test_actions_grouped_by_category(registry):
    groups = registry.by_category()
    assert {"Import", "Export", "Rows", "Columns", "Text", "Calculate", "Combine"} <= set(groups)
    assert all(a.category == "Import" for a in groups["Import"])

"""The catalog of actions, settings, connection types and formula functions (``tafl catalog
--json``): complete, plain JSON in ASCII, unchanged unless planned (a golden file), and its
schemas fit the steps of the stored projects and the steps a program makes from it."""

import importlib.util
import json
import os
import re
import textwrap
import types
from pathlib import Path

import pytest

from tafl import __version__
from tafl.api import P, ValidationContext
from tafl.api import action as declare
from tafl.core import Registry, catalog
from tafl.core.functions import CURATED, all_functions

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = Path(__file__).resolve().parent / "golden_catalog.json"
UPDATE = "TAFL_UPDATE_GOLDEN"
KEYWORDS = {"$schema", "type", "properties", "patternProperties", "additionalProperties", "required", "enum", "items", "minItems", "minimum", "maximum"}


@pytest.fixture(scope="module")
def built(registry):
    return catalog.build(registry)


def entry(data, action_id):
    return next(action for action in data["actions"] if action["id"] == action_id)


# --- a small validator for the keywords that the schemas use ------------------------------------


def _same(value, other) -> bool:
    """JSON equality: true and 1 are different values."""
    return isinstance(value, bool) == isinstance(other, bool) and value == other


_TYPES = {
    "string": lambda v: isinstance(v, str),
    "boolean": lambda v: isinstance(v, bool),
    "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
    "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
    "array": lambda v: isinstance(v, list),
    "object": lambda v: isinstance(v, dict),
}


def problems(value, schema: dict, where: str = "params") -> list[str]:
    """What doesn't fit ``schema``; only the keywords in KEYWORDS."""
    found = []
    if "enum" in schema and not any(_same(value, allowed) for allowed in schema["enum"]):
        found.append(f"{where}: {value!r} is not one of {schema['enum']}")
    kind = schema.get("type")
    if kind is not None and not _TYPES[kind](value):
        return [*found, f"{where}: {value!r} is not {kind}"]
    if "minimum" in schema and value < schema["minimum"]:
        found.append(f"{where}: {value!r} is below {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        found.append(f"{where}: {value!r} is above {schema['maximum']}")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0):
            found.append(f"{where}: fewer than {schema['minItems']} items")
        for number, item in enumerate(value):
            found += problems(item, schema.get("items", {}), f"{where}[{number}]")
    if isinstance(value, dict):
        found += [f"{where}: {key} is missing" for key in schema.get("required", []) if key not in value]
        properties, patterns = schema.get("properties", {}), schema.get("patternProperties", {})
        for key, item in value.items():
            if key in properties:
                found += problems(item, properties[key], f"{where}.{key}")
            matching = [pattern for pattern in patterns if re.search(pattern, key)]
            for pattern in matching:
                found += problems(item, patterns[pattern], f"{where}.{key}")
            if key not in properties and not matching:
                extra = schema.get("additionalProperties", True)
                if extra is False:
                    found.append(f"{where}: unknown key {key!r}")
                elif isinstance(extra, dict):
                    found += problems(item, extra, f"{where}.{key}")
    return found


def keywords(schema: dict) -> set[str]:
    """The keywords a schema uses, also in its parts (not the names of its properties)."""
    used = set(schema)
    for part in [*schema.get("properties", {}).values(), *schema.get("patternProperties", {}).values()]:
        used |= keywords(part)
    for key in ("items", "additionalProperties"):
        if isinstance(schema.get(key), dict):
            used |= keywords(schema[key])
    return used


# --- complete, plain JSON ------------------------------------------------------------------------


def test_every_action_setting_and_option_is_in_the_catalog(registry, built):
    assert [action["id"] for action in built["actions"]] == sorted(registry.actions)
    for action in registry.actions.values():
        found = entry(built, action.id)
        assert (found["name"], found["category"], found["kind"], found["version"], found["plugin"]) == (
            action.name, action.category, str(action.kind), action.version, action.plugin
        )
        assert found["side_effects"] == action.side_effects
        assert_settings(action.params, found["settings"], action.id)
    assert [c["id"] for c in built["connection_types"]] == sorted(registry.connectors) == ["sqlite", "sqlserver"]
    for connection_type in built["connection_types"]:
        assert_settings(registry.connectors[connection_type["id"]].params, connection_type["settings"], connection_type["id"])
    plugins = {plugin["name"]: plugin for plugin in built["plugins"]}
    assert plugins["csv_files"] == {"name": "csv_files", "builtin": True, "path": None, "actions": ["csv.import", "csv.export"], "connection_types": [], "errors": []}
    assert sorted(a for plugin in built["plugins"] for a in plugin["actions"]) == sorted(registry.actions)


def assert_settings(params, settings, where):
    """The settings in the order of the form, list fields too, each with its options."""
    assert [setting["key"] for setting in settings] == [param.key for param in params], where
    for param, setting in zip(params, settings, strict=True):
        assert (setting["kind"], setting["label"], setting["required"], setting["help"]) == (param.kind, param.label, param.required, param.help)
        assert setting["default"] == json.loads(json.dumps(param.initial())), (where, param.key)
        if isinstance(param, P.Choice):
            assert setting["options"] == [{"value": value, "label": label} for value, label in param.options]
        if isinstance(param, P.List):
            assert setting["min_items"] == param.min_items
            assert_settings(param.fields, setting["fields"], f"{where}.{param.key}")


def test_the_settings_of_each_kind_have_their_fields(built):
    settings = {s["key"]: s for s in entry(built, "csv.import")["settings"]}
    assert settings["source"]["options"] == [{"value": "file", "label": "One file"}, {"value": "folder", "label": "All files in a folder"}]
    assert settings["file"] == {
        "key": "file", "kind": "file", "label": "File", "required": True, "default": "", "help": "",
        "visible_if": {"source": ["file"]}, "file_mode": "open", "file_filter": "Text files (*.csv *.txt *.tsv);;All files (*.*)",
    }  # fmt: skip
    assert settings["skip_lines"]["min"] == 0 and settings["skip_lines"]["max"] is None
    assert settings["decimal"]["visible_if"] == {"detect_types": [True]}
    excel = {s["key"]: s for s in entry(built, "excel.import")["settings"]}
    assert excel["sheet_column"]["visible_if"] == {"sheets": ["all", "matching"]}
    assert excel["sheet"]["placeholder"] == "empty = first sheet" and excel["sheet"]["multiline"] is False
    sql = {s["key"]: s for s in entry(built, "db.command")["settings"]}
    assert sql["sql"]["language"] == "sql" and sql["sql"]["multiline"] is True
    assert sql["connection"]["connection_types"] == []
    calculated = entry(built, "calc.columns")["settings"][0]
    formula = {f["key"]: f for f in calculated["fields"]}["formula"]
    assert (formula["kind"], formula["condition"], formula["creates"]) == ("expression", False, "name")
    condition = {s["key"]: s for s in entry(built, "rows.filter_formula")["settings"]}["condition"]
    assert (condition["condition"], condition["creates"]) == (True, None)
    merge = {s["key"]: s for s in entry(built, "table.merge")["settings"]}
    other_column = {f["key"]: f for f in merge["match"]["fields"]}["other_column"]
    assert other_column["column_source"] == "other" and merge["other"]["kind"] == "table"
    assert entry(built, "csv.import")["adds_columns"] == [
        {"name": "File", "type": "text", "when": {"source": ["folder"], "file_column": [True]}},
        {"name": "Path", "type": "text", "when": {"source": ["folder"], "path_column": [True]}},
    ]
    assert {"name": "Size", "type": "integer", "when": None} in entry(built, "files.list")["adds_columns"]


def test_the_output_is_json_in_ascii_that_reads_back(built):
    written = catalog.to_json(built)
    assert written.isascii() and written.endswith(b"}\n")
    assert json.loads(written) == built
    text = json.dumps(built, ensure_ascii=False)
    assert "→" in text and "€" in text  # characters that a pipe could change: escaped, and back
    assert "\\u2192" in written.decode("ascii")
    assert written.startswith(b'{\n "format": "tafl-catalog",\n "format_version": "1.0",\n')


def test_the_help_is_that_of_the_settings_panel(registry, built):
    """One function joins the lines of the docstrings, for the panel and the catalog."""
    for action in registry.actions.values():
        assert entry(built, action.id)["help"] == catalog.help_text(action.description)
    import_help = entry(built, "csv.import")["help"]
    description = registry.actions["csv.import"].description
    assert "\n" in description.split("\n\n")[0] and "\n" not in import_help.split("\n\n")[0]
    assert import_help.startswith("Reads a delimited text file (CSV, TXT, TSV), or all such files of a folder (and its subfolders) into one table: daily exports, for example.\n\n")
    assert catalog.help_text("One\n  line.\n\nTwo\nlines.\n") == "One line.\n\nTwo lines."


ODD = "Paints.\n\n\nOne\n  line.  \n   \nTwo\n\t\n\n \n\tlines.\n   "  # 3 line ends, lines with only spaces or a tab


def test_the_help_of_a_docstring_with_blank_lines_of_any_kind():
    """Blank lines end a paragraph, also lines with only spaces and several of them in a row;
    no paragraph starts or ends with a space."""
    assert catalog.help_text(ODD) == "Paints.\n\nOne line.\n\nTwo\n\nlines."
    assert catalog.help_text("\n \n  Alone  \n\n") == "Alone" and catalog.help_text(" \n\t\n") == "" and catalog.help_text("") == ""

    def paint(df, p, ctx):
        return df

    paint.__doc__ = "Paints.\n\n\n    Twice\n    here.\n      \n    Thrice."  # as written in a source file
    registry = Registry()
    registry.add_module(_module(declare("demo.paint", "Paint")(paint)))
    assert catalog.build(registry, [])["actions"][0]["help"] == "Paints.\n\nTwice here.\n\nThrice."


def _module(*actions):
    module = types.ModuleType("painting")
    for number, found in enumerate(actions):
        setattr(module, f"action{number}", found)
    return module


def test_top_level_and_functions(built):
    assert built["format"] == catalog.FORMAT == "tafl-catalog" and built["format_version"] == catalog.FORMAT_VERSION == "1.0"
    assert built["app"] == {"name": "Tafl", "version": __version__, "plugin_api": 1}
    assert built["project_file"] == {"format": "tafl-project", "format_version": 1}
    assert [f["name"] for f in built["functions"]] == [f.name for f in all_functions()]
    curated = [f for f in built["functions"] if f["curated"]]
    assert [f["name"] for f in curated] == [f.name for f in CURATED] and len(curated) > 100
    iif = curated[0]
    assert iif == {
        "name": "IIF", "category": "Conditions", "syntax": "IIF(condition, if_true, if_false)",
        "summary": "One of two values, depending on a condition (as in SQL Server)", "note": "",
        "example": "IIF([Amount] > 1000, 'big', 'small')", "aliases": [], "instead_of": ["IF"], "curated": True,
    }  # fmt: skip
    assert len(built["functions"]) - len(curated) > 300


# --- the golden file -------------------------------------------------------------------------------


def golden_part(data: dict) -> dict:
    """What the golden file holds: all but the app's version and DuckDB's own functions, which
    depend on the version of DuckDB."""
    data = json.loads(json.dumps(data))
    del data["app"]["version"]
    data["functions"] = [f for f in data["functions"] if f["curated"]]
    return data


def differences(old, new, where: str = "") -> list[str]:
    if isinstance(old, dict) and isinstance(new, dict):
        found = [f"{where}.{key}: removed" for key in old if key not in new]
        found += [f"{where}.{key}: new" for key in new if key not in old]
        for key in old.keys() & new.keys():
            found += differences(old[key], new[key], f"{where}.{key}")
        return found
    if isinstance(old, list) and isinstance(new, list):
        name = lambda item: item.get("id") or item.get("key") or item.get("name") if isinstance(item, dict) else None  # noqa: E731
        if len(old) != len(new):
            return [f"{where}: {len(old)} entries before, {len(new)} now ({[name(i) for i in old][:8]} ... -> {[name(i) for i in new][:8]} ...)"]
        return [problem for number, (a, b) in enumerate(zip(old, new)) for problem in differences(a, b, f"{where}[{name(a) or number}]")]
    return [] if _same(old, new) else [f"{where}: {old!r} -> {new!r}"]


def test_the_catalog_changes_only_on_purpose(built):
    """An unplanned change of the actions, settings or functions fails here. For a planned one,
    write the file again and decide whether readers need a new format_version (docs/catalog.md)."""
    current = golden_part(built)
    text = json.dumps(current, indent=1, ensure_ascii=True) + "\n"
    if os.environ.get(UPDATE) == "1":
        GOLDEN.write_text(text, encoding="ascii", newline="\n")
    found = differences(json.loads(GOLDEN.read_text(encoding="ascii")), current)
    assert not found, (
        f"The catalog differs from {GOLDEN.name} in {len(found)} places:\n  " + "\n  ".join(found[:20]) + "\n"
        f"If the change is planned, set the environment variable {UPDATE}=1 and run this test again: it writes "
        f"the file anew (then commit it). A change may need a new format_version (catalog.FORMAT_VERSION and "
        f"docs/catalog.md): a new minor version for additions only, a new major version for a change or removal "
        f"that a reader may use."
    )


# --- the schemas -----------------------------------------------------------------------------------


def test_the_schemas_use_only_the_keywords_that_the_validator_knows(built):
    for action in built["actions"]:
        schema = action["settings_schema"]
        assert keywords(schema) <= KEYWORDS, (action["id"], keywords(schema) - KEYWORDS)
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema" and schema["type"] == "object"
        assert schema["additionalProperties"] is False and schema["patternProperties"] == {"^_": {}}
        assert list(schema["properties"]) == [setting["key"] for setting in action["settings"]]
        for setting in action["settings"]:
            if setting["kind"] == "list":  # the rows: the same rule for unknown keys and Tafl's marks
                row = schema["properties"][setting["key"]]["items"]
                assert row["additionalProperties"] is False and row["patternProperties"] == {"^_": {}}, (action["id"], setting["key"])
                assert list(row["properties"]) == [field["key"] for field in setting["fields"]]


def test_a_misspelled_key_doesnt_fit_the_schema(built):
    """Tafl would keep it, and the setting would have its default: the schema says so first."""
    schema = entry(built, "csv.import")["settings_schema"]
    assert problems({"file": "a.csv", "delimiter": ";"}, schema) == []
    assert problems({"file": "a.csv", "delimeter": ";"}, schema) == ["params: unknown key 'delimeter'"]
    assert problems({"file": "a.csv", "_sheet": {"file": "a.csv", "stamp": 1}}, schema) == []  # Tafl's own mark
    assert problems({"file": "a.csv", "delimiter": "colon"}, schema) == ["params.delimiter: 'colon' is not one of ['auto', ';', ',', 'tab', '|']"]
    assert problems({"file": "a.csv", "skip_lines": -1, "header": "yes"}, schema) == [
        "params.skip_lines: -1 is below 0",
        "params.header: 'yes' is not boolean",
    ]
    rows = entry(built, "rows.filter")["settings_schema"]
    assert problems({"conditions": [{"column": "A", "operator": "=", "value": "1"}]}, rows) == []
    assert problems({"conditions": [{"column": "A", "_mark": {"x": 1}}], "_mark": 2}, rows) == []  # in rows too, as the warning
    assert problems({"conditions": [{"colum": "A"}]}, rows) == ["params.conditions[0]: column is missing", "params.conditions[0]: unknown key 'colum'"]
    assert problems({"conditions": []}, rows) == ["params.conditions: fewer than 1 items"]
    assert problems({}, rows) == ["params: conditions is missing"]
    assert problems({}, entry(built, "csv.import")["settings_schema"]) == []  # File is required only for one file


def project_steps(path: Path) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    return [step for table in data["tables"] for step in table["steps"]]


def test_the_steps_of_the_stored_projects_fit_their_schemas(built, tmp_path):
    spec = importlib.util.spec_from_file_location("make_big_project", ROOT / "tools" / "make_big_project.py")
    big = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(big)
    projects = {
        "demo.tafl": ROOT / "examples" / "demo.tafl",
        "excel-check.tafl": ROOT / "examples" / "excel-check.tafl",
        "big500.tafl": big.build_project().save(tmp_path / "big500.tafl"),
    }
    actions = {action["id"]: action for action in built["actions"]}
    counted = {}
    for name, path in projects.items():
        steps = project_steps(path)
        counted[name] = len(steps)
        for step in steps:
            action = actions[step["action"]]
            assert step["version"] == action["version"], (name, step)
            assert problems(step["params"], action["settings_schema"]) == [], (name, step)
    assert counted["demo.tafl"] >= 18 and counted["excel-check.tafl"] >= 4 and counted["big500.tafl"] >= 450


FILL = {"text": "x", "code": "SELECT 1", "expression": "1", "column": "A", "columns": ["A"], "file": "a.csv", "folder": "d", "table": "t1", "tables": ["t1"], "connection": "c"}


def shown(setting: dict, values: dict) -> bool:
    """``visible_if`` as docs/catalog.md describes it, with the values written so far."""
    return all(values.get(key) in allowed for key, allowed in (setting["visible_if"] or {}).items())


def from_catalog(settings: list[dict]) -> dict:
    """The settings of a new step as a program makes them from the catalog alone: the defaults,
    and a value in each required setting that is shown and empty."""
    values = {}
    for setting in settings:
        value = setting["default"]
        if setting["kind"] == "list":
            rows = list(value)
            while len(rows) < max(setting["min_items"], 1 if setting["required"] else 0):
                rows.append({})
            value = [{**from_catalog(setting["fields"]), **{k: v for k, v in row.items() if v not in ("", [], None)}} for row in rows]
        elif setting["required"] and value in ("", [], None) and shown(setting, values | {s["key"]: s["default"] for s in settings if s["key"] not in values}):
            value = FILL[setting["kind"]]
        values[setting["key"]] = value
    return values


def test_a_step_made_from_the_catalog_alone_passes_the_check(registry, built):
    for found in built["actions"]:
        values = from_catalog(found["settings"])
        action = registry.actions[found["id"]]
        assert action.check(action.resolve(values), ValidationContext()) == [], found["id"]
        assert problems(values, found["settings_schema"]) == [], found["id"]


def test_the_schema_requires_what_the_check_requires(registry, built):
    """A step with only the settings that its schema requires passes Tafl's check, but for the
    settings that are required only after a choice. A list that needs rows is required too,
    also when its setting isn't (e.g. Conditions of Filter rows)."""
    for found in built["actions"]:
        required = found["settings_schema"].get("required", [])
        filled = from_catalog(found["settings"])
        action = registry.actions[found["id"]]
        left = action.check(action.resolve({key: filled[key] for key in required}), ValidationContext())
        after_a_choice = {f"{s['label']}: required" for s in found["settings"] if s["required"] and s["visible_if"]}
        assert set(left) <= after_a_choice, (found["id"], left)
    assert entry(built, "rows.filter")["settings_schema"]["required"] == ["conditions"]
    assert entry(built, "calc.columns")["settings_schema"]["required"] == ["columns"]
    assert "required" not in entry(built, "csv.import")["settings_schema"]  # File only for one file


def test_the_16_settings_that_are_required_only_after_a_choice(built):
    """Not in the schema (no if/then/else): required with a visible_if, in the settings."""
    found = [
        (action["id"], setting["key"])
        for action in built["actions"]
        for setting in [*action["settings"], *(f for s in action["settings"] for f in s.get("fields", []))]
        if setting["required"] and setting["visible_if"]
    ]
    assert len(found) == 16 and len({action_id for action_id, _ in found}) == 11
    assert ("csv.import", "file") in found and ("calc.aggregate", "column") in found
    schema = entry(built, "csv.import")["settings_schema"]
    assert "required" not in schema and "if" not in json.dumps(schema)


# --- plugins -------------------------------------------------------------------------------------


OWN_KIND = '''
from dataclasses import dataclass
from decimal import Decimal
from typing import ClassVar

from tafl.api import AddedColumn, P, action


@dataclass(frozen=True, kw_only=True)
class Color(P.Param):
    """A setting of the plugin's own kind."""

    default: object = "#ff0000"
    kind: ClassVar[str] = "color"


@dataclass(frozen=True, kw_only=True)
class Shade(P.Text):
    """Derived from a known kind, with a kind of its own."""

    kind: ClassVar[str] = "shade"


@action(
    "demo.paint",
    "Paint",
    category="Paint",
    kind="source",
    params=[
        Color("color", "Color", required=True, default=""),
        Shade("shade", "Shade", default=("light", "dark")),
        P.Choice("size", "Size", [(1, "small"), (2.5, "medium"), (float("inf"), "endless")], default=float("nan")),
        P.Int("count", "Count", default=Decimal("3"), min=1),
        P.Text("tags", "Tags", default={"a": (1, 2), 3: {4}}),
    ],
    adds_columns=[AddedColumn("Painted", "boolean", when={"size": (1, 2.5)})],
)
def paint(df, p, ctx):
    """Paints.

    Twice."""
'''


def test_a_plugin_with_its_own_kind_of_setting(tmp_path, monkeypatch):
    """Its setting has the common fields only, and any value; values that are not plain JSON
    don't break the catalog (tuples become lists, anything else its text)."""
    (tmp_path / "paint.py").write_text(textwrap.dedent(OWN_KIND), encoding="utf-8")
    registry = Registry()
    registry.load_builtin()
    registry.load_folder(tmp_path)
    data = catalog.build(registry, [])
    assert json.loads(catalog.to_json(data)) == data
    paint = entry(data, "demo.paint")
    assert paint["help"] == "Paints.\n\nTwice." and paint["plugin"] == "paint" and data["functions"] == []
    settings = {setting["key"]: setting for setting in paint["settings"]}
    assert settings["color"] == {"key": "color", "kind": "color", "label": "Color", "required": True, "default": "", "help": "", "visible_if": None}
    assert settings["shade"] == {"key": "shade", "kind": "shade", "label": "Shade", "required": False, "default": ["light", "dark"], "help": "", "visible_if": None}
    assert settings["size"]["options"] == [{"value": 1, "label": "small"}, {"value": 2.5, "label": "medium"}, {"value": "inf", "label": "endless"}]
    assert settings["size"]["default"] == "nan" and settings["count"]["default"] == "3" and settings["count"]["min"] == 1
    assert settings["tags"]["default"] == {"a": [1, 2], "3": "{4}"}
    schema = paint["settings_schema"]
    assert schema["properties"]["color"] == {} and schema["properties"]["shade"] == {}
    assert schema["properties"]["size"] == {"enum": [1, 2.5, "inf"]} and schema["required"] == ["color"]
    assert paint["adds_columns"] == [{"name": "Painted", "type": "boolean", "when": {"size": [1, 2.5]}}]
    plugin = next(p for p in data["plugins"] if p["name"] == "paint")
    assert plugin == {"name": "paint", "builtin": False, "path": str(tmp_path / "paint.py"), "actions": ["demo.paint"], "connection_types": [], "errors": []}


# --- docs/catalog.md -----------------------------------------------------------------------------


DOCS = ROOT / "docs" / "catalog.md"


def shown_in(doc, real, where: str = "") -> list[str]:
    """What the docs show and the real output doesn't have; "..." stands for what is left out:
    a key, the items of a list, or the rest of a text."""
    if isinstance(doc, dict):
        if not isinstance(real, dict):
            return [f"{where}: {real!r} is not an object"]
        found = []
        for key, value in doc.items():
            if key != "...":
                found += shown_in(value, real[key], f"{where}.{key}") if key in real else [f"{where}.{key}: not in the output"]
        return found
    if isinstance(doc, list):
        if not isinstance(real, list) or ("..." not in doc and len(doc) != len(real)):
            return [f"{where}: {real!r} has other items"]
        position, found = 0, []
        for item in (item for item in doc if item != "..."):  # in the same order
            while position < len(real) and shown_in(item, real[position]):
                position += 1
            if position == len(real):
                found.append(f"{where}: {item!r} is not in the output (in this order)")
                break
            position += 1
        return found
    if isinstance(doc, str) and "..." in doc and isinstance(real, str):
        first, *rest = doc.split("...")
        start = len(first) if real.startswith(first) else -1
        for part in rest:
            start = real.find(part, start) if start >= 0 else -1
        return [] if start >= 0 else [f"{where}: {real[:80]!r}... is not {doc[:80]!r}"]
    return [] if _same(doc, real) else [f"{where}: {doc!r} in the docs, {real!r} in the output"]


def test_the_example_of_the_docs_is_the_real_output(built):
    text = DOCS.read_text(encoding="utf-8")
    step, example = (json.loads(block) for block in re.findall(r"```json\n(.*?)```", text, re.S))
    assert shown_in(example, entry(built, example["id"])) == []
    assert example["settings"][-1] == "..." and len(example["settings"]) == 5  # the check sees the shortened parts
    action = entry(built, step["action"])
    assert step["version"] == action["version"] and problems(step["params"], action["settings_schema"]) == []
    assert shown_in({"a": [1, "..."], "b": "x..."}, {"a": [1, 2], "b": "xyz", "c": 3}) == []  # the check itself
    assert shown_in({"a": [2, 1, "..."]}, {"a": [1, 2]}) and shown_in({"b": "y..."}, {"b": "xyz"})


def test_the_docs_name_the_version_the_kinds_and_the_keywords(built):
    text = DOCS.read_text(encoding="utf-8")
    assert f'`format_version` is a text `"major.minor"`, now `"{catalog.FORMAT_VERSION}"`.' in text
    kinds = text[text.index("Kinds, the value a step stores") : text.index("- `condition` (expression)")]
    assert set(re.findall(r"^\| `(\w+)` \|", kinds, re.M)) - {"kind"} == set(catalog._KINDS)  # without the heading
    listed = text[text.index("The schemas use only these keywords") :].split("\n\n")[0]
    assert set(re.findall(r"`([$\w]+)`", listed)) == KEYWORDS


def test_the_readable_list(built):
    text = "\n".join(catalog.lines(built))
    assert text.startswith(f"{len(built['actions'])} actions\n\nCalculate\n  calc.aggregate  Aggregate  (transform, version 1, plugin calculate)\n")
    assert "\nImport\n  files.list  Files in a folder  (source, version 1, plugin files)\n" in text  # by name
    assert "\n  csv.import  Import CSV / text files  (source, version 3, plugin csv_files)\n" in text
    assert '    source: choice, "Read", default "file", options "file", "folder"\n' in text
    assert '    file: file, "File", required, default "", shown when source is "file"\n' in text
    assert '    sheet_column: bool, "Add a column with the sheet name", default true, shown when sheets is "all" or "matching"\n' in text
    assert '    conditions: list, "Conditions", at least 1 row\n        column: column, "Column", required, default ""\n' in text
    assert "\nConnection types\n  sqlite  SQLite  (plugin databases)\n" in text
    assert text.endswith(f"\n\n{len(built['functions'])} formula functions: 'tafl catalog --json' describes them, with all of the above")

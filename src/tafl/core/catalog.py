"""The catalog: every action, setting, connection type and formula function of this Tafl, with the
plugins it loaded, for programs that write or check project files (``tafl catalog --json``).

It is made from the same declarations that the app builds its settings forms from, so it says
what Tafl runs. docs/catalog.md describes the format: ``FORMAT_VERSION`` gets a new minor version
for additions only, a new major version for a change or removal that a reader may use.
"""

from __future__ import annotations

import itertools
import json
import math
from collections.abc import Iterable, Iterator, Mapping, Sequence
from typing import Any

from tafl import APP_NAME, COMMAND, __version__
from tafl.api import API_VERSION, Action, AddedColumn, Connector, P
from tafl.api.params import Param

from . import functions as formula_functions
from . import project
from .functions import Function
from .registry import PluginInfo, Registry

FORMAT = "tafl-catalog"
FORMAT_VERSION = "1.0"
SCHEMA = "https://json-schema.org/draft/2020-12/schema"

# The kinds of settings that the catalog describes with the fields of their kind. A setting of
# another kind (a plugin's own class of setting) has only the common fields, and any value.
_KINDS: dict[str, type[Param]] = {
    "text": P.Text,
    "code": P.Code,
    "expression": P.Expression,
    "int": P.Int,
    "float": P.Float,
    "bool": P.Bool,
    "choice": P.Choice,
    "column": P.Column,
    "columns": P.Columns,
    "file": P.File,
    "folder": P.Folder,
    "table": P.TableRef,
    "tables": P.TableRefs,
    "connection": P.ConnectionRef,
    "list": P.List,
}
_TEXTS = ("text", "code", "expression", "column", "file", "folder", "table", "connection")


def help_text(description: str) -> str:
    """An action's help as the app shows it: the docstring in paragraphs, separated by one empty
    line. Blank lines end a paragraph, also lines with only spaces and several of them; the lines
    of a paragraph are joined with a space (docstrings wrap at ~100 characters; the label wraps by
    itself), without spaces at the start or the end."""
    paragraphs: list[str] = []
    lines: list[str] = []
    for line in [*description.splitlines(), ""]:  # the empty line at the end ends the last paragraph
        if line.strip():
            lines.append(line.strip())
        elif lines:
            paragraphs.append(" ".join(lines))
            lines = []
    return "\n\n".join(paragraphs)


def build(registry: Registry, function_list: Iterable[Function] | None = None) -> dict[str, Any]:
    """The catalog of the actions and connection types of ``registry`` and of the formula
    functions (default: ``functions.all_functions()``), as plain JSON values."""
    curated = set(formula_functions.CURATED)
    listed = formula_functions.all_functions() if function_list is None else function_list
    catalog = {
        "format": FORMAT,
        "format_version": FORMAT_VERSION,
        "app": {"name": APP_NAME, "version": __version__, "plugin_api": API_VERSION},
        "project_file": {"format": project.FORMAT, "format_version": project.FORMAT_VERSION},
        "plugins": [_plugin(info) for info in registry.plugins],
        "actions": [_action(registry.actions[action_id]) for action_id in sorted(registry.actions)],
        "connection_types": [_connection_type(registry.connectors[type_id]) for type_id in sorted(registry.connectors)],
        "functions": [_function(function, function in curated) for function in listed],
    }
    return _plain(catalog)


def to_json(catalog: Mapping[str, Any]) -> bytes:
    """The catalog as the command writes it: only ASCII (other characters as \\uXXXX escapes, so
    that no pipe can change them), indented by one space, with a line end at the end."""
    return (json.dumps(catalog, indent=1, ensure_ascii=True) + "\n").encode("utf-8")


def settings_schema(params: Sequence[Param]) -> dict[str, Any]:
    """A JSON Schema of a step's ``"params"``: the type of each value, the values of a choice,
    the limits of a number, the rows of a list, and the settings that a step must always store.
    Unknown keys are not allowed (Tafl runs a step with a misspelled key with a warning, and the
    setting keeps its default), except Tafl's own marks, which start with ``_``. Without
    ``visible_if``."""
    schema: dict[str, Any] = {
        "$schema": SCHEMA,
        "type": "object",
        "properties": {param.key: _schema(param) for param in params},
        "patternProperties": {"^_": {}},
        "additionalProperties": False,
    }
    if required := [param.key for param in params if _must_store(param)]:
        schema["required"] = required
    return schema


def lines(catalog: Mapping[str, Any]) -> Iterator[str]:
    """The catalog as text for people: the actions by category with their settings, the
    connection types, and the number of formula functions. Not a format for programs."""
    actions = sorted(catalog["actions"], key=lambda action: (str(action["category"]), str(action["name"])))
    yield f"{len(actions)} actions"
    for category, group in itertools.groupby(actions, key=lambda action: str(action["category"])):
        yield ""
        yield category
        for action in group:
            yield f"  {action['id']}  {action['name']}  ({action['kind']}, version {action['version']}, plugin {action['plugin']})"
            yield from _setting_lines(action["settings"], "    ")
    yield ""
    yield "Connection types"
    for connection_type in catalog["connection_types"]:
        yield f"  {connection_type['id']}  {connection_type['name']}  (plugin {connection_type['plugin']})"
        yield from _setting_lines(connection_type["settings"], "    ")
    yield ""
    yield f"{len(catalog['functions'])} formula functions: '{COMMAND} catalog --json' describes them, with all of the above"


# --- the parts ------------------------------------------------------------------------------


def _plugin(info: PluginInfo) -> dict[str, Any]:
    return {
        "name": info.name,
        "builtin": info.builtin,
        "path": None if info.builtin else info.path,
        "actions": list(info.actions),
        "connection_types": list(info.connectors),
        "errors": list(info.errors),
    }


def _action(action: Action) -> dict[str, Any]:
    return {
        "id": action.id,
        "name": action.name,
        "category": action.category,
        "kind": str(action.kind),
        "side_effects": action.side_effects,
        "version": action.version,
        "plugin": action.plugin,
        "help": help_text(action.description),
        "settings": [_setting(param) for param in action.params],
        "adds_columns": [_added(column) for column in action.adds_columns],
        "settings_schema": settings_schema(action.params),
    }


def _connection_type(connector: Connector) -> dict[str, Any]:
    return {
        "id": connector.id,
        "name": connector.name,
        "plugin": connector.plugin,
        "settings": [_setting(param) for param in connector.params],
    }


def _function(function: Function, curated: bool) -> dict[str, Any]:
    return {
        "name": function.name,
        "category": function.category,
        "syntax": function.syntax,
        "summary": function.summary,
        "note": function.note,
        "example": function.example,
        "aliases": list(function.aliases),
        "instead_of": list(function.instead_of),
        "curated": curated,
    }


def _added(column: AddedColumn) -> dict[str, Any]:
    return {"name": column.name, "type": column.type, "when": _values(column.when)}


def _kind(param: Param) -> str | None:
    """The kind of the setting if the catalog knows it (its class is the one of that kind, or
    derived from it), else None."""
    kind = getattr(param, "kind", None)
    cls = _KINDS.get(kind) if isinstance(kind, str) else None
    return kind if cls is not None and isinstance(param, cls) else None


def _setting(param: Param) -> dict[str, Any]:
    """The fields of every setting, then those of its kind."""
    setting = {
        "key": param.key,
        "kind": getattr(param, "kind", ""),
        "label": param.label,
        "required": bool(param.required),
        "default": _default(param),
        "help": param.help,
        "visible_if": _values(param.visible_if),
    }
    kind = _kind(param)
    if kind == "choice":
        setting["options"] = [{"value": value, "label": label} for value, label in param.options]
    if kind in ("text", "code", "expression"):
        setting["multiline"] = param.multiline
        setting["placeholder"] = param.placeholder
    if kind == "code":
        setting["language"] = param.language
    if kind == "expression":
        setting["condition"] = param.condition
        setting["creates"] = param.creates or None
    if kind in ("column", "columns"):
        setting["column_source"] = param.source
    if kind in ("int", "float"):
        setting["min"] = param.min
        setting["max"] = param.max
    if kind == "file":
        setting["file_mode"] = param.mode
        setting["file_filter"] = param.filter
    if kind == "connection":
        setting["connection_types"] = list(param.types)
    if kind == "list":
        setting["fields"] = [_setting(field) for field in param.fields]
        setting["min_items"] = param.min_items
    return setting


def _schema(param: Param) -> dict[str, Any]:
    """The schema of one setting's value; ``{}`` (any value) for a kind that the catalog doesn't know."""
    kind = _kind(param)
    if kind == "list":
        row: dict[str, Any] = {
            "type": "object",
            "properties": {field.key: _schema(field) for field in param.fields},
            "patternProperties": {"^_": {}},  # Tafl's own marks, as at the top level
            "additionalProperties": False,
        }
        if required := [field.key for field in param.fields if _must_store(field)]:
            row["required"] = required
        schema: dict[str, Any] = {"type": "array", "items": row}
        if param.min_items:
            schema["minItems"] = param.min_items
        return schema
    if kind == "choice":
        return {"enum": [value for value, _ in param.options]}
    if kind == "bool":
        return {"type": "boolean"}
    if kind in ("int", "float"):
        schema = {"type": "integer" if kind == "int" else "number"}
        if param.min is not None:
            schema["minimum"] = param.min
        if param.max is not None:
            schema["maximum"] = param.max
        return schema
    if kind in ("columns", "tables"):
        return {"type": "array", "items": {"type": "string"}}
    if kind in _TEXTS:
        return {"type": "string"}
    return {}


def _must_store(param: Param) -> bool:
    """A setting that a step must always store: always shown, and required with an empty
    default, or a list with fewer rows by default than it needs (Tafl's check says "add at least
    1 row" also for a list that isn't required)."""
    if param.visible_if:
        return False
    try:
        default = param.initial()
        if _kind(param) == "list" and len(default) < param.min_items:
            return True
        return bool(param.required) and bool(param.is_empty(default))
    except Exception:  # a plugin's own class of setting: its default is in the catalog as it is
        return False


def _default(param: Param) -> Any:
    """The value of a new step (rows of a list with all their fields)."""
    try:
        return param.initial()
    except Exception:  # a plugin's own class of setting
        return param.default


def _values(spec: Mapping[str, Any] | None) -> dict[str, list[Any]] | None:
    """A ``visible_if`` (or an added column's ``when``) as the catalog writes it: setting key ->
    the list of values, one of which it must have; None when it has no condition."""
    if not spec:
        return None
    values = {}
    for key, expected in spec.items():
        if isinstance(expected, (tuple, list)):
            values[key] = list(expected)
        elif isinstance(expected, (set, frozenset)):
            values[key] = sorted(expected, key=repr)
        else:
            values[key] = [expected]
    return values


def _plain(value: Any) -> Any:
    """``value`` as plain JSON: tuples as lists, mappings with text keys, texts and numbers as
    they are. A number that JSON can't hold (NaN, infinity) and any other value become their
    text, so that a plugin's default can't break the catalog."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return float(value) if math.isfinite(value) else str(value)
    if isinstance(value, str):
        return str(value)
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return str(value)


def _setting_lines(settings: Sequence[Mapping[str, Any]], indent: str) -> Iterator[str]:
    for setting in settings:
        details = [setting["kind"], json.dumps(setting["label"], ensure_ascii=False)]
        if setting["required"]:
            details.append("required")
        if setting["kind"] != "list":
            details.append(f"default {json.dumps(setting['default'], ensure_ascii=False)}")
        elif setting.get("min_items"):
            details.append(f"at least {setting['min_items']} row{'s' if setting['min_items'] != 1 else ''}")
        if setting["visible_if"]:
            conditions = (
                f"{key} is {' or '.join(json.dumps(value, ensure_ascii=False) for value in values)}"
                for key, values in setting["visible_if"].items()
            )
            details.append(f"shown when {' and '.join(conditions)}")
        if "options" in setting:
            details.append("options " + ", ".join(json.dumps(option["value"], ensure_ascii=False) for option in setting["options"]))
        yield f"{indent}{setting['key']}: {', '.join(details)}"
        if "fields" in setting:
            yield from _setting_lines(setting["fields"], indent + "    ")

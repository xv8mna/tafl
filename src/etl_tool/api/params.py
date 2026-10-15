"""Declarative action parameters.

An action lists its parameters. The engine uses that list to fill in defaults and to check a step
before running it; the UI builds the settings form from it. Values are stored in the project file
as plain JSON (strings, numbers, booleans, lists, objects).

Plugins use these classes through ``from etl_tool.api import P``: ``P.Text``, ``P.Column``,
``P.List(...)`` and so on.
"""

from __future__ import annotations

import copy
import difflib
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import KW_ONLY, dataclass
from typing import Any, ClassVar


class Params(dict):
    """The parameter values of one step.

    Read values as attributes (``p.column``) or as keys (``p["column"]``). Each row of a ``List``
    parameter is a ``Params`` as well.
    """

    def __getattr__(self, name: str) -> Any:
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


@dataclass(frozen=True)
class ValidationContext:
    """What parameters can check their values against. ``None`` means "unknown, don't check"."""

    columns: Sequence[str] | None = None
    table_columns: Callable[[str], Sequence[str] | None] = lambda table_id: None
    tables: Mapping[str, str] | None = None
    connections: Collection[str] | None = None

    def columns_for(self, source: str, params: Mapping[str, Any]) -> Sequence[str] | None:
        """Columns of the input table, or of the table chosen in the parameter named ``source``."""
        if source == "input":
            return self.columns
        table_id = params.get(source)
        return self.table_columns(table_id) if table_id else None


@dataclass(frozen=True)
class Param:
    """Base class of all parameter types."""

    key: str
    label: str = ""
    _: KW_ONLY
    default: Any = None
    help: str = ""
    required: bool = False
    # Show (and check) this parameter only when other parameters have these values,
    # e.g. {"mode": "query"} or {"how": ("left", "inner")}.
    visible_if: Mapping[str, Any] | None = None

    kind: ClassVar[str] = "param"

    def __post_init__(self) -> None:
        if not self.label:
            object.__setattr__(self, "label", self.key.replace("_", " ").capitalize())

    def initial(self) -> Any:
        """Value for a new step."""
        return copy.deepcopy(self.default)

    def normalize(self, value: Any) -> Any:
        """Turn a stored value into the form the action receives."""
        return self.initial() if value is None else value

    def is_empty(self, value: Any) -> bool:
        return value is None or value == "" or value == []

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        """Problems with ``value``; ``params`` are all values of the step (for cross-references)."""
        if self.required and self.is_empty(value):
            return [f"{self.label}: required"]
        return []

    def table_refs(self, value: Any) -> list[str]:
        """Ids of the tables this value refers to."""
        return []


@dataclass(frozen=True, kw_only=True)
class Text(Param):
    default: Any = ""
    multiline: bool = False
    placeholder: str = ""
    kind: ClassVar[str] = "text"

    def normalize(self, value: Any) -> Any:
        return self.initial() if value is None else str(value)


@dataclass(frozen=True, kw_only=True)
class Code(Text):
    """Multi-line code, e.g. a SQL query."""

    multiline: bool = True
    language: str = "sql"
    kind: ClassVar[str] = "code"


@dataclass(frozen=True, kw_only=True)
class Expression(Text):
    """A SQL-style formula (see ``etl_tool.api.expressions``)."""

    condition: bool = False  # True: the formula must be true/false for each row
    kind: ClassVar[str] = "expression"


@dataclass(frozen=True, kw_only=True)
class Int(Param):
    default: Any = 0
    min: int | None = None
    max: int | None = None
    kind: ClassVar[str] = "int"

    def normalize(self, value: Any) -> Any:
        if value is None or value == "":
            return self.initial()
        try:
            return int(value)
        except (TypeError, ValueError):
            return value  # check() reports it

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        if not isinstance(value, int) or isinstance(value, bool):
            return [f"{self.label}: must be a whole number"]
        return _check_range(self.label, value, self.min, self.max)


@dataclass(frozen=True, kw_only=True)
class Float(Param):
    default: Any = 0.0
    min: float | None = None
    max: float | None = None
    kind: ClassVar[str] = "float"

    def normalize(self, value: Any) -> Any:
        if value is None or value == "":
            return self.initial()
        try:
            return float(str(value).replace(",", "."))
        except (TypeError, ValueError):
            return value

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return [f"{self.label}: must be a number"]
        return _check_range(self.label, value, self.min, self.max)


@dataclass(frozen=True, kw_only=True)
class Bool(Param):
    default: Any = False
    kind: ClassVar[str] = "bool"

    def normalize(self, value: Any) -> Any:
        return self.initial() if value is None else bool(value)


@dataclass(frozen=True)
class Choice(Param):
    """One of a fixed set of values. ``options`` are values or ``(value, label)`` pairs."""

    options: Sequence[Any] = ()
    kind: ClassVar[str] = "choice"

    def __post_init__(self) -> None:
        super().__post_init__()
        pairs = tuple((o, o) if isinstance(o, str) else (o[0], o[1]) for o in self.options)
        object.__setattr__(self, "options", pairs)
        if self.default is None and pairs:
            object.__setattr__(self, "default", pairs[0][0])

    @property
    def values(self) -> list[str]:
        return [value for value, _ in self.options]

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if value not in self.values:
            return [f"{self.label}: '{value}' is not one of: {', '.join(self.values)}"]
        return []


@dataclass(frozen=True, kw_only=True)
class Column(Param):
    """One column. ``source="input"`` means the step's input table; any other value names the
    ``TableRef`` parameter whose table the column belongs to (e.g. the other table of a merge)."""

    default: Any = ""
    required: bool = True
    source: str = "input"
    kind: ClassVar[str] = "column"

    def normalize(self, value: Any) -> Any:
        return "" if value is None else str(value)

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        columns = vc.columns_for(self.source, params)
        if value and columns is not None and value not in columns:
            return [f"{self.label}: column '{value}' not found{_suggest(value, columns)}"]
        return []


@dataclass(frozen=True, kw_only=True)
class Columns(Param):
    """Several columns, in order."""

    default: Any = ()
    source: str = "input"
    kind: ClassVar[str] = "columns"

    def initial(self) -> Any:
        return list(self.default or ())

    def normalize(self, value: Any) -> Any:
        if value is None:
            return self.initial()
        if isinstance(value, str):
            return [value] if value else []
        return [str(v) for v in value]

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        columns = vc.columns_for(self.source, params)
        if columns is None:
            return []
        return [
            f"{self.label}: column '{name}' not found{_suggest(name, columns)}"
            for name in value
            if name not in columns
        ]


@dataclass(frozen=True, kw_only=True)
class File(Param):
    """A file path. ``mode`` is "open" (must exist) or "save"."""

    default: Any = ""
    required: bool = True
    mode: str = "open"
    filter: str = "All files (*.*)"
    kind: ClassVar[str] = "file"


@dataclass(frozen=True, kw_only=True)
class Folder(Param):
    default: Any = ""
    required: bool = True
    kind: ClassVar[str] = "folder"


@dataclass(frozen=True, kw_only=True)
class TableRef(Param):
    """Another table of the project (stored by id)."""

    default: Any = ""
    required: bool = True
    kind: ClassVar[str] = "table"

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        if value and vc.tables is not None and value not in vc.tables:
            return [f"{self.label}: table '{value}' does not exist"]
        return []

    def table_refs(self, value: Any) -> list[str]:
        return [value] if value else []


@dataclass(frozen=True, kw_only=True)
class TableRefs(Param):
    """Several other tables."""

    default: Any = ()
    required: bool = True
    kind: ClassVar[str] = "tables"

    def initial(self) -> Any:
        return list(self.default or ())

    def normalize(self, value: Any) -> Any:
        return self.initial() if value is None else [str(v) for v in value]

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        if vc.tables is None:
            return []
        return [f"{self.label}: table '{v}' does not exist" for v in value if v not in vc.tables]

    def table_refs(self, value: Any) -> list[str]:
        return [v for v in value if v]


@dataclass(frozen=True, kw_only=True)
class ConnectionRef(Param):
    """A database connection defined in the project (stored by name)."""

    default: Any = ""
    required: bool = True
    types: tuple[str, ...] = ()  # allowed connector ids; empty = any
    kind: ClassVar[str] = "connection"

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if problems := super().check(value, vc, params):
            return problems
        if value and vc.connections is not None and value.lower() not in {c.lower() for c in vc.connections}:
            return [f"{self.label}: connection '{value}' is not defined in this project"]
        return []


@dataclass(frozen=True)
class List(Param):
    """A list of rows; each row has the given ``fields`` (e.g. sort keys: column + order)."""

    fields: Sequence[Param] = ()
    _: KW_ONLY
    default: Any = ()
    min_items: int = 0
    kind: ClassVar[str] = "list"

    def __post_init__(self) -> None:
        super().__post_init__()
        object.__setattr__(self, "fields", tuple(self.fields))

    def initial(self) -> Any:
        return [self.new_row(row) for row in (self.default or ())]

    def new_row(self, values: Mapping[str, Any] | None = None) -> Params:
        return resolve(self.fields, values)

    def normalize(self, value: Any) -> Any:
        if value is None:
            return self.initial()
        return [self.new_row(row if isinstance(row, Mapping) else {}) for row in value]

    def check(self, value: Any, vc: ValidationContext, params: Mapping[str, Any]) -> list[str]:
        if len(value) < self.min_items:
            return [f"{self.label}: add at least {self.min_items} row{'s' if self.min_items > 1 else ''}"]
        problems = []
        for number, row in enumerate(value, start=1):
            for f in self.fields:
                if is_visible(f, row):
                    for problem in f.check(row.get(f.key), vc, params):
                        problems.append(f"{self.label}, row {number} – {problem}")
        return problems

    def table_refs(self, value: Any) -> list[str]:
        refs = []
        for row in value:
            for f in self.fields:
                refs += f.table_refs(row.get(f.key))
        return refs


# --- helpers used by the engine --------------------------------------------------------------


def resolve(spec: Sequence[Param], values: Mapping[str, Any] | None) -> Params:
    """Stored values merged over the defaults. Unknown keys are kept unchanged."""
    values = dict(values or {})
    result = Params(values)
    for param in spec:
        result[param.key] = param.normalize(values.get(param.key))
    return result


def is_visible(param: Param, values: Mapping[str, Any]) -> bool:
    if not param.visible_if:
        return True
    for key, expected in param.visible_if.items():
        actual = values.get(key)
        if isinstance(expected, (tuple, list, set, frozenset)):
            if actual not in expected:
                return False
        elif actual != expected:
            return False
    return True


def check(spec: Sequence[Param], params: Mapping[str, Any], vc: ValidationContext) -> list[str]:
    problems: list[str] = []
    for param in spec:
        if is_visible(param, params):
            problems += param.check(params.get(param.key), vc, params)
    return problems


def table_refs(spec: Sequence[Param], params: Mapping[str, Any]) -> list[str]:
    refs: list[str] = []
    for param in spec:
        if is_visible(param, params):
            refs += param.table_refs(params.get(param.key))
    return list(dict.fromkeys(refs))


def _check_range(label: str, value: float, low: float | None, high: float | None) -> list[str]:
    if low is not None and value < low:
        return [f"{label}: must be at least {low}"]
    if high is not None and value > high:
        return [f"{label}: must be at most {high}"]
    return []


def _suggest(name: str, columns: Sequence[str]) -> str:
    matches = difflib.get_close_matches(name, list(columns), n=1, cutoff=0.6)
    if not matches:
        lowered = {c.lower(): c for c in columns}
        if name.lower() in lowered:
            matches = [lowered[name.lower()]]
    return f" (did you mean '{matches[0]}'?)" if matches else ""

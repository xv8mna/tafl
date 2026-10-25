"""An index of a project for finding steps: one entry per step with the texts of its settings,
what it reads and writes outside the project, and the parameters it uses. The Find panel
searches and groups it; it has no Qt in it."""

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass, field, replace
from itertools import accumulate
from pathlib import Path
from typing import Any

import polars as pl

from tafl.api import Action, ActionError, Cancelled, Connector, Endpoint, Kind, P
from tafl.api.endpoints import CMD, DATABASE, FILE, FOLDER, IN, OUT, SQL, default_endpoints
from tafl.api.parameters import PLACE, fill_sql, fill_text

from . import parameters
from .engine import Engine
from .project import Connection, Group, Project, Step, Table

KINDS = ("in", "out", "cmd", "step")
SECTION_TITLES = {"in": "Imports", "out": "Exports", "cmd": "Commands", "step": "Other steps"}
# Keys of the values that are no setting
NOTE, SUMMARY, ACTION, TABLE, GROUP, MODE = "#note", "#summary", "#action", "#table", "#group", "#mode"
NAMES = (ACTION, TABLE)  # shown on the first line of a result already (the group only in a wide panel)
SUMMARY_LENGTH = 300  # characters of a step's summary kept (as the window shows them)

# What plugin code can raise (as in the engine): a broken step gets a plain entry
_BROKEN = (Exception, SystemExit, KeyboardInterrupt)
_DIRECTIONS = {"source": IN, "transform": IN, "export": OUT, "command": CMD}
_ENDPOINT_KINDS = (FILE, FOLDER, DATABASE, SQL)
_VERBS = {IN: "Reads", OUT: "Writes into", CMD: "Changes"}
_PROBLEMS = {"error": "Error", "warning": "Warning", "incomplete": "Not complete", "waiting": "Waiting"}
_NOT_TEXTS = (P.Choice, P.Bool, P.Int, P.Float)  # settings whose values are not searched
_SECRET = re.compile(r"password|secret|token|pwd", re.IGNORECASE)  # connection settings never shown
_EXCERPT = 200  # characters of a value on the second line of a result
_SQL_SHOWN = 60  # characters of SQL in the text of an endpoint
_DRIVE_ONLY = re.compile(r"[A-Za-z]:(?![\\/])")  # a drive without a \ after it: C:data.csv
_NO_NAME = "(no file name)"


# --- data --------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Value:
    """A text of a step to search: a setting, the note, the summary or a name."""

    label: str  # "File", "Columns, row 2 – Formula", "Formula [Gross]", "Note"; "" for the summary
    text: str  # as written, with {Name}
    now: str = ""  # with the parameters' values now; "" when the same as text or unknown
    key: str = ""  # the setting's key, or NOTE, SUMMARY, ACTION, TABLE, GROUP
    row: int = 0  # in a list setting: the row, from 1 (0: no list)
    field: str = ""  # in a list setting: the key of the field in the row


@dataclass(frozen=True)
class Location:
    """An endpoint of a step, with what the Find panel needs to show and group it."""

    endpoint: Endpoint  # as written
    now: Endpoint | None = None  # with the parameters' values now (None: the same, or unknown)
    folder: str = ""  # FILE/FOLDER: the folder as a full path ("" if unknown); DATABASE/SQL: ""
    connection: str = ""  # DATABASE/SQL: "Shop (Microsoft SQL Server: .\SQLEXPRESS, ShopDB)"; "": none chosen
    via: Endpoint | None = None  # imported=True: the endpoint of the table's folder import, as written
    via_now: Endpoint | None = None  # and with the parameters' values now (None: the same, or unknown)

    @property
    def via_shown(self) -> Endpoint | None:
        """The folder import's endpoint with the values now (as written when they are unknown)."""
        return self.via_now or self.via

    @property
    def place_key(self) -> str:
        """The same for all spellings of a folder, or for a connection."""
        shown = self.now or self.endpoint
        if shown.kind in (DATABASE, SQL):
            return "connection:" + shown.place.strip().lower()
        if shown.imported and self.via is None:
            return "imported:"
        if shown.column and not shown.place.strip():
            return "column:"
        return "path:" + os.path.normcase(self.folder or shown.place)

    @property
    def name_key(self) -> str:
        """The same for all spellings of a file name or a table, inside the place."""
        shown = self.now or self.endpoint
        if shown.kind == SQL:
            return "sql"
        if shown.kind == DATABASE:
            return shown.name.strip().lower()
        if shown.imported:
            return os.path.normcase(self.via_shown.name) if self.via is not None else ""
        if shown.column:
            return "column:" + shown.column.lower()
        return os.path.normcase(shown.name)


@dataclass(frozen=True)
class Entry:
    table_id: str
    table: str  # the table's name
    table_enabled: bool
    group_id: str  # "" = none
    group: str  # the group's name, "" = none
    group_color: str  # the group's color name, "" = none
    step_id: str
    number: int  # from 1
    action: str  # the action's name; the action id when its plugin is missing
    kind: str  # "in", "out", "cmd" or "step" (step_kind)
    enabled: bool
    summary: str
    note: str
    values: tuple[Value, ...]  # settings, the note, the summary, the names
    locations: tuple[Location, ...]
    parameters: tuple[str, ...]  # the project's parameters it uses, as the project writes their names
    now_problem: str  # why the values now are unknown (a parameter with an error), else ""
    signature: str  # what "changed since opened" compares (signatures)
    haystack: str  # all texts and now-texts, lower case, one per line
    empty: tuple[str, int, str] | None = None  # (key, row, field) of the first required setting that is empty


@dataclass
class ProjectIndex:
    entries: list[Entry]
    parameters: list[str]  # the project's parameter names, in order
    directory: Path | None  # the project's folder
    groups: list[str] = field(default_factory=list)  # the ids of the project's groups, in order

    def __post_init__(self) -> None:
        self._steps = {entry.step_id: entry for entry in self.entries}

    def entry(self, step_id: str) -> Entry | None:
        if len(self._steps) != len(self.entries):  # entries changed since
            self._steps = {entry.step_id: entry for entry in self.entries}
        return self._steps.get(step_id)


def step_kind(action: Action | None) -> str:
    """What a step of this action does outside the project: "out" (export), "cmd" (command), "in"
    (it reads files, folders or databases) or "step". It depends on the action only, so it stays
    the same while the settings are edited (the map's marks don't move)."""
    if action is None:
        return "step"
    if action.kind == Kind.EXPORT:
        return "out"
    if action.kind == Kind.COMMAND:
        return "cmd"
    outside = action.endpoints is not None or _has(action.params, (P.File, P.Folder, P.ConnectionRef))
    if action.kind == Kind.SOURCE and not _has(action.params, (P.TableRef, P.TableRefs)):
        return "in"  # a source that reads no other table reads from outside
    return "in" if outside else "step"


def _has(params: Iterable[P.Param], kinds: tuple[type, ...]) -> bool:
    """Whether a parameter (also a field of a list) is one of ``kinds``."""
    return any(isinstance(p, kinds) or (isinstance(p, P.List) and _has(tuple(p.fields), kinds)) for p in params)


# --- building ----------------------------------------------------------------------------------


def build(engine: Engine) -> ProjectIndex:
    """One entry per step of every table, in project order. The values now come from
    ``engine.parameter_values()`` (worked out once and kept, as the calculation does); the
    summaries from ``engine.describe``."""
    builder = _Builder(engine)
    project = engine.project
    entries = []
    for table in project.tables:
        group = project.group(table.group) if table.group else None
        folder_import: Location | None = None  # the first step's folder, for the files it read
        for number, step in enumerate(table.steps, start=1):
            entry = builder.entry(table, group, number, step, folder_import)
            entries.append(entry)
            if number == 1 and entry.locations:
                first = entry.locations[0]  # an import of all files in a folder ('Files in a folder' notes none)
                if first.endpoint.kind == FOLDER and first.endpoint.direction == IN and first.endpoint.folder_import:
                    folder_import = first
    return ProjectIndex(
        entries, [p.name for p in project.parameters], project.directory, [g.id for g in project.groups]
    )


def signatures(project: Project, actions: Mapping[str, Action]) -> dict[str, str]:
    """step id -> what "changed since opened" compares (``step_signature``); ``actions``: action
    id -> Action, as ``Registry.actions``."""
    return {
        step.id: step_signature(step, table, action=actions.get(step.action)) for table in project.tables for step in table.steps
    }


def changed_steps(index: ProjectIndex, baseline: Mapping[str, str]) -> set[str]:
    """The steps whose signature differs from ``baseline`` (new steps too)."""
    return {entry.step_id for entry in index.entries if baseline.get(entry.step_id) != entry.signature}


def step_signature(step: Step, table: Table, params: Mapping[str, Any] | None = None, action: Action | None = None) -> str:
    """What "changed since opened" compares for a step: the action, its version, the settings
    (``params``: instead of its settings), on/off, the note and whether its table is on. With the
    step's ``action``, the settings as the action gets them, the defaults filled in: a step saved
    with only some of its settings is the same after the settings form has written all of them."""
    params = step.params if params is None else params
    if action is not None:
        try:
            params = action.resolve(action.upgrade(params, step.version))
        except _BROKEN:  # settings that can't be read: as they are
            pass
    data = [step.action, step.version, params, step.enabled, step.note, table.enabled]
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class _Connection:
    """A connection of the project, as the index shows it."""

    name: str  # as the project writes it
    details: str  # "Microsoft SQL Server, .\SQLEXPRESS, ShopDB"
    now: str  # the details with the parameters' values now ("" when the same or unknown)
    label: str  # "Shop (Microsoft SQL Server: .\SQLEXPRESS, ShopDB)"


class _Builder:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.project = engine.project
        self.registry = engine.registry
        self.directory = engine.project.directory
        self.values = engine.parameter_values() if engine.project.parameters else {}
        self.known = {p.name.strip().lower(): p.name for p in reversed(engine.project.parameters)}
        self.connections: dict[str, _Connection] = {}
        for connection in reversed(engine.project.connections):  # the first of a name counts, as in the project
            self.connections[connection.name.lower()] = self._connection(connection)
        self.kinds: dict[str, str] = {}  # action id -> step_kind

    def entry(self, table: Table, group: Group | None, number: int, step: Step, folder_import: Location | None) -> Entry:
        action = self.registry.actions.get(step.action)
        summary = self._summary(step)
        signature = step_signature(step, table, action=action)
        if (kind := self.kinds.get(step.action)) is None:
            kind = self.kinds[step.action] = step_kind(action)
        if action is not None:
            try:
                raw = action.upgrade(step.params, step.version)
                params = action.resolve(raw)
                filled, now_problem = self._filled(action, raw, signature)
                values = self._setting_values(action.params, params, filled)
                # A step of kind "step" has no endpoints: no file, folder or connection settings
                locations = self._locations(action, params, filled, folder_import) if kind != "step" else []
                values += _mode_values(locations)
                values += _other_values(step, table, group, summary, action.name)
                entry = self._made(
                    table, group, number, step, action.name, kind, summary, signature, values, locations, now_problem
                )
                empty = _first_empty(action.params, params)
                return replace(entry, empty=empty) if empty is not None else entry
            except _BROKEN:
                self._stop_if_stopping()
        # The action is missing, or the settings can't be read: the texts of the settings as they are
        values = [Value(key, value, key=key) for key, value in step.params.items() if isinstance(value, str) and value.strip()]
        name = action.name if action is not None else step.action
        values += _other_values(step, table, group, summary, name)
        return self._made(table, group, number, step, name, kind, summary, signature, values, [], "")

    def _filled(self, action: Action, raw: dict[str, Any], signature: str) -> tuple[P.Params | None, str]:
        """The settings with the parameters' values now (None when they have no {Name}, or when
        a parameter has an error), and that error."""
        if not self.values:
            return None, ""
        # The signature has the settings as the action gets them (upgraded, with the defaults):
        # without a {Name} in it there is nothing to fill in
        if PLACE.search(signature) is None:
            return None, ""
        try:
            connector = self._connector(action, raw)
            sql = connector.fill_sql if connector is not None else fill_sql
            return action.resolve(parameters.fill(action.params, raw, self.values, sql)), ""
        except ActionError as error:  # a parameter with an error
            return None, str(error)
        except _BROKEN:
            self._stop_if_stopping()
            return None, ""

    def _made(
        self,
        table: Table,
        group: Group | None,
        number: int,
        step: Step,
        action: str,
        kind: str,
        summary: str,
        signature: str,
        values: list[Value],
        locations: list[Location],
        now_problem: str,
    ) -> Entry:
        used: dict[str, None] = {}
        for value in values if self.known else ():
            if not value.key.startswith("#") and "{" in value.text:
                for match in PLACE.finditer(value.text):
                    if (name := self.known.get(match[1].strip().lower())) is not None:
                        used.setdefault(name)
        return Entry(
            table_id=table.id,
            table=table.name,
            table_enabled=table.enabled,
            group_id=group.id if group is not None else "",
            group=group.name if group is not None else "",
            group_color=group.color if group is not None else "",
            step_id=step.id,
            number=number,
            action=action,
            kind=kind,
            enabled=step.enabled,
            summary=summary,
            note=step.note,
            values=tuple(values),
            locations=tuple(locations),
            parameters=tuple(used),
            now_problem=now_problem,
            signature=signature,
            haystack="\n".join(text for value in values for text in (value.text, value.now) if text).lower(),
        )

    def _summary(self, step: Step) -> str:
        """The step's summary on one line: line breaks (e.g. of a condition on several lines) become spaces."""
        try:
            text = " ".join(self.engine.describe(step).split())
        except _BROKEN:
            self._stop_if_stopping()
            text = step.action
        return text if len(text) <= SUMMARY_LENGTH else text[: SUMMARY_LENGTH - 1] + "…"

    # --- values

    def _setting_values(self, spec: tuple[P.Param, ...], params: P.Params, filled: P.Params | None) -> list[Value]:
        values = []
        for param in spec:
            if not P.is_visible(param, params):
                continue
            value = params.get(param.key)
            now_value = filled.get(param.key) if filled is not None else None
            if isinstance(param, P.List):
                values += self._row_values(param, value, now_value)
                continue
            text = self._text(param, value)
            if not text:
                continue
            now = self._text(param, now_value) if filled is not None else ""
            values.append(Value(param.label, text, now if now != text else "", param.key))
            if isinstance(param, P.ConnectionRef):
                connection = self.connections.get((now or text).strip().lower())
                if connection is not None:
                    values.append(Value(connection.name, connection.details, connection.now, param.key))
        return values

    def _row_values(self, param: P.List, rows: Any, now_rows: Any) -> list[Value]:
        rows = rows if isinstance(rows, list) else []
        if not (isinstance(now_rows, list) and len(now_rows) == len(rows)):
            now_rows = None
        values = []
        for number, row in enumerate(rows, start=1):
            if not isinstance(row, Mapping):
                continue
            now_row = now_rows[number - 1] if now_rows is not None else None
            for field_ in param.fields:
                if not P.is_visible(field_, row):
                    continue
                text = self._text(field_, row.get(field_.key))
                if not text:
                    continue
                now = self._text(field_, now_row.get(field_.key)) if isinstance(now_row, Mapping) else ""
                values.append(Value(_row_label(param, field_, row, number), text, now if now != text else "", param.key, number, field_.key))
        return values

    def _text(self, param: P.Param, value: Any) -> str:
        """The text of a setting's value to search, "" for none."""
        if isinstance(param, _NOT_TEXTS):
            return ""
        if isinstance(param, P.TableRef):
            return self.engine.table_name(value) if isinstance(value, str) and value.strip() else ""
        if isinstance(param, P.TableRefs | P.Columns):
            if not isinstance(value, list):
                return ""
            names = [v for v in value if isinstance(v, str) and v]
            return ", ".join(self.engine.table_name(v) for v in names) if isinstance(param, P.TableRefs) else ", ".join(names)
        return value if isinstance(value, str) and value.strip() else ""

    # --- connections

    def _connection(self, connection: Connection) -> _Connection:
        connector = self.registry.connectors.get(connection.type)
        settings = connection.settings
        texts: list[str] = []
        try:
            if connector is not None:
                shown = P.resolve(connector.params, settings)
                spec = [p for p in connector.params if P.is_visible(p, shown) and not _SECRET.search(p.key)]
                texts = [self._text(p, settings.get(p.key)) for p in spec]
                filled = parameters.fill(list(connector.params), settings, self.values) if self.values else settings
                now = [self._text(p, filled.get(p.key)) for p in spec]
            else:
                spec_keys = [k for k, v in settings.items() if isinstance(v, str) and v.strip() and not _SECRET.search(k)]
                texts = [settings[k] for k in spec_keys]
                now = [fill_text(text, self.values) for text in texts]
        except ActionError:  # a parameter with an error: the values now are unknown
            now = texts
        except _BROKEN:
            self._stop_if_stopping()
            texts, now = [], []
        kind = connector.name if connector is not None else connection.type
        details = ", ".join([kind, *(t for t in texts if t)])
        details_now = ", ".join([kind, *(t for t in now if t)])
        shown_texts = ", ".join(t for t in texts if t)
        label = f"{connection.name} ({kind}: {shown_texts})" if shown_texts else f"{connection.name} ({kind})"
        return _Connection(connection.name, details, details_now if details_now != details else "", label)

    def _connector(self, action: Action, raw: Mapping[str, Any]) -> Connector | None:
        """The database type of the step's connection (as the engine finds it): it fills the
        parameters into the SQL."""
        for param in action.params:
            if isinstance(param, P.ConnectionRef):
                definition = self.project.connection(str(raw.get(param.key) or ""))
                connector = self.registry.connectors.get(definition.type) if definition else None
                if connector is not None:
                    return connector
        return None

    # --- locations

    def _locations(
        self, action: Action, params: P.Params, filled: P.Params | None, folder_import: Location | None
    ) -> list[Location]:
        written = self._endpoints(action, params)
        now = self._endpoints(action, filled) if filled is not None else None
        if now is not None and len(now) != len(written):
            now = None
        direction = _DIRECTIONS.get(str(action.kind), IN)
        locations = []
        for position, item in enumerate(written):
            endpoint = _normal(item, direction)
            now_endpoint = _normal(now[position], direction) if now is not None else None
            if now_endpoint == endpoint:
                now_endpoint = None
            if _empty(endpoint):
                continue
            locations.append(self._location(endpoint, now_endpoint, folder_import))
        return locations

    def _endpoints(self, action: Action, params: P.Params) -> list[Endpoint]:
        try:
            return action.endpoints_of(params)
        except _BROKEN:  # sys.exit() or KeyboardInterrupt in a plugin's function
            self._stop_if_stopping()
        try:
            return default_endpoints(action, params)
        except _BROKEN:
            self._stop_if_stopping()
            return []

    def _location(self, endpoint: Endpoint, now: Endpoint | None, folder_import: Location | None) -> Location:
        shown = now or endpoint
        if endpoint.kind in (DATABASE, SQL):
            if not shown.place.strip():  # no connection chosen yet
                return Location(endpoint, now)
            connection = self.connections.get(shown.place.strip().lower())
            label = connection.label if connection is not None else f"{shown.place} (not defined in this project)"
            return Location(endpoint, now, connection=label)
        if endpoint.imported:
            if folder_import is None:
                return Location(endpoint, now)
            via, via_now = folder_import.endpoint, folder_import.now
            return Location(endpoint, now, folder=folder_import.folder, via=via, via_now=via_now)
        if endpoint.column and not shown.place.strip():
            return Location(endpoint, now)
        return Location(endpoint, now, folder=full_path(shown.place, self.directory))

    def _stop_if_stopping(self) -> None:
        """Stopping the work (Ctrl+C on the command line) stops building too."""
        if self.engine.cancel is not None and self.engine.cancel.is_set():
            raise Cancelled() from None


def full_path(text: str, directory: Path | None) -> str:
    """A folder or a file as a full path: %VARS% and ~ expanded, relative to ``directory``; ""
    is ``directory`` ("" without one). A path that still has a ``{Name}`` stays as written."""
    text = text.strip() if isinstance(text, str) else ""
    try:
        if not text:
            return os.path.normpath(directory) if directory is not None else ""
        if PLACE.search(text):
            return text
        path = os.path.expandvars(os.path.expanduser(text))
        if _DRIVE_ONLY.match(path):  # C:data.csv is no place below the project's folder (it is an error)
            return path
        if not os.path.isabs(path) and directory is not None:
            path = os.path.join(directory, path)
        return os.path.normpath(path)
    except (TypeError, ValueError, OSError):
        return text


def _row_label(param: P.List, field_: P.Param, row: Mapping[str, Any], number: int) -> str:
    """"Formula [Gross]" for a formula that creates a column, else "Columns, row 2 – Name"."""
    if isinstance(field_, P.Expression) and field_.creates:
        column = row.get(field_.creates)
        if isinstance(column, str) and column.strip():
            return f"{field_.label} [{column}]"
    return f"{param.label}, row {number} – {field_.label}"


def _mode_values(locations: list[Location]) -> list[Value]:
    """How the step reads or writes its files and tables ("to the Recycle Bin", "first sheet"):
    the list shows it, so it is found too (it comes from settings that are not searched)."""
    modes = [e.mode for location in locations for e in (location.endpoint, location.now) if e is not None and e.mode]
    return [Value("", mode, key=MODE) for mode in dict.fromkeys(modes)]


def _first_empty(spec: tuple[P.Param, ...], params: P.Params) -> tuple[str, int, str] | None:
    """(key, row, field) of the first shown setting that must be filled in and is empty: Enter in
    the Find panel goes there when nothing else is matched (a step that is not complete yet)."""
    for param in spec:
        if not P.is_visible(param, params):
            continue
        value = params.get(param.key)
        if isinstance(param, P.List):
            rows = value if isinstance(value, list) else []
            for number, row in enumerate(rows, start=1):
                if isinstance(row, Mapping):
                    for field_ in param.fields:
                        if field_.required and P.is_visible(field_, row) and field_.is_empty(row.get(field_.key)):
                            return param.key, number, field_.key
        elif param.required and param.is_empty(value):
            return param.key, 0, ""
    return None


def _other_values(step: Step, table: Table, group: Group | None, summary: str, action: str) -> list[Value]:
    """The note, the summary and the names of a step."""
    values = [Value("Note", step.note, key=NOTE)] if step.note.strip() else []
    values += [Value("", summary, key=SUMMARY), Value("Action", action, key=ACTION), Value("Table", table.name, key=TABLE)]
    if group is not None:
        values.append(Value("Group", group.name, key=GROUP))
    return values


def _normal(endpoint: Endpoint, direction: str) -> Endpoint:
    """An endpoint from a plugin with a known direction and kind, and texts in its text fields."""
    changes: dict[str, Any] = {}
    if endpoint.direction not in _VERBS:
        changes["direction"] = direction
    if endpoint.kind not in _ENDPOINT_KINDS:
        changes["kind"] = FILE
    for name in ("place", "name", "part", "mode", "setting", "column", "mode_setting"):
        value = getattr(endpoint, name)
        if not isinstance(value, str):
            changes[name] = "" if value is None else str(value)
    for name in ("imported", "folder_import"):
        if not isinstance(getattr(endpoint, name), bool):
            changes[name] = bool(getattr(endpoint, name))
    return replace(endpoint, **changes) if changes else endpoint


def _empty(endpoint: Endpoint) -> bool:
    """Nothing named yet: no place, no name, no column, not the imported files."""
    return not (endpoint.place.strip() or endpoint.name.strip() or endpoint.column or endpoint.imported)


# --- searching ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Query:
    text: str = ""
    case: bool = False  # match upper and lower case
    regex: bool = False  # the text is a regular expression
    problems: bool = False  # only steps with errors, warnings, not complete yet, waiting
    off: bool = False  # only steps that are off, or in a table that is off
    changed: bool = False  # only steps changed since the project was opened
    parameter: str | None = None  # None: no filter; "": uses any parameter; a name: uses that one

    @property
    def empty(self) -> bool:
        """No text and no filter."""
        return not self.text.strip() and not (self.problems or self.off or self.changed) and self.parameter is None


@dataclass(frozen=True)
class Line:
    """A line of a result with marked parts: (start, end, kind), kind "match", "parameter" or
    "label". Parts may overlap; "match" wins over "parameter", both over "label"."""

    text: str
    marks: tuple[tuple[int, int, str], ...] = ()


@dataclass(frozen=True)
class Hit:
    entry: Entry
    value: Value | None  # the value line 2 shows (None: the overview, the problem or the summary)
    title: Line  # "Orders · 4 · Merge with another table", matches in the names marked
    line: Line  # line 2
    status: str  # "error", "warning", "incomplete", "waiting", "off" or ""
    focus: tuple[str, int, str] | None  # (key, row, field) of the setting Enter moves the focus to
    tooltip: str


class _Finder:
    """The words of a search (or its pattern): which values they match, and where.

    A pattern is matched by Polars (the regex crate of Rust), as in the filter pane's "Pattern
    (regex)": the time it takes grows with the length of the texts only, so no pattern can make a
    search hang (Python's ``re`` can take hours for one like ``(a+)+$``). Polars works out many
    texts at once much faster than one at a time: ``prepare``, ``prepare_first`` and
    ``prepare_spans`` take the texts of a search before they are asked about."""

    def __init__(self, query: Query) -> None:
        self.case = query.case
        self.words: list[str] = []
        self.patterns: list[re.Pattern[str]] = []  # the words, for Python
        self.pattern = ""  # the regular expression, for Polars
        self._found: dict[str, bool] = {}  # text -> whether the pattern is in it
        self._first: dict[str, int | None] = {}  # text -> where the pattern's first match starts
        self._spans: dict[str, list[tuple[int, int]]] = {}  # text -> where the pattern is in it
        self._flat: dict[str, str] = {}  # text -> the text on one line (flat)
        if query.regex:
            # A flag in front: Polars takes a pattern without one as plain text when it can, and
            # then doesn't fill in the match where prepare_spans puts ${0}
            self.pattern = ("(?-i)" if query.case else "(?i)") + query.text
            self.prepare([""])  # ValueError for a pattern with a mistake
        else:
            flags = 0 if query.case else re.IGNORECASE
            self.words = query.text.split() if query.case else query.text.lower().split()
            self.patterns = [re.compile(re.escape(word), flags) for word in dict.fromkeys(self.words)]

    @property
    def regex(self) -> bool:
        return not self.words

    def prepare(self, texts: Iterable[str]) -> None:
        """For a pattern: whether it is in each of the texts, all at once."""
        if not self.regex:
            return
        new = [text for text in dict.fromkeys(texts) if text not in self._found]
        if new:
            self._found.update(zip(new, _polars(lambda texts: texts.str.contains(self.pattern), new), strict=True))

    def prepare_first(self, texts: Iterable[str]) -> None:
        """For a pattern: where its first match starts in each of the texts, all at once. That is
        all the excerpt of a long text needs: marking all matches in it takes long when the
        pattern matches nearly every character (".")."""
        if not self.regex:
            return
        new = [text for text in dict.fromkeys(texts) if text not in self._first]
        if new:
            starts = _polars(lambda texts: texts.str.find(self.pattern), new)  # in bytes of UTF-8
            self._first.update((text, _in_characters(text, at)) for text, at in zip(new, starts, strict=True))

    def prepare_spans(self, texts: Iterable[str]) -> None:
        """For a pattern: where it is in each of the texts, all at once."""
        if not self.regex:
            return
        new = []
        for text in dict.fromkeys(texts):
            if text in self._spans:
                continue
            if self._found.get(text) is False:  # no match in it (prepare): nothing to ask Polars
                self._spans[text] = []
            else:
                new.append(text)
        if not new:
            return
        start, end = _MATCH
        # Marks in a text already become another character (of the same length)
        clean = [text.translate(_NO_MATCH_MARKS) if start in text or end in text else text for text in new]
        marked = _polars(lambda texts: texts.str.replace_all(self.pattern, f"{start}${{0}}{end}"), clean)
        for text, out in zip(new, marked, strict=True):
            if start not in out:
                self._spans[text] = []
                continue
            # "before<start>match<end>text<start>match<end>…": where each part ends in the text
            ends = list(accumulate(map(len, out.replace(end, start).split(start))))
            self._spans[text] = [(a, b) for a, b in zip(ends[::2], ends[1::2]) if b > a]

    def flat(self, text: str) -> str:
        """The text on one line, as an excerpt shows it (kept: ``search`` needs it twice)."""
        if (flat := self._flat.get(text)) is None:
            flat = self._flat[text] = " ".join(text.split())
        return flat

    def _has(self, text: str) -> bool:
        if text not in self._found:
            self.prepare([text])
        return self._found[text]

    def _spans_in(self, text: str) -> list[tuple[int, int]]:
        if text not in self._spans:
            self.prepare_spans([text])
        return self._spans[text]

    def entry(self, entry: Entry) -> bool:
        """Whether the entry matches: all words, each in any of its texts; or the pattern in one."""
        if self.regex:
            return any(self._has(v.text) or (v.now and self._has(v.now)) for v in entry.values)
        if self.case:
            texts = "\n".join(text for v in entry.values for text in (v.text, v.now) if text)
            return all(word in texts for word in self.words)
        return all(word in entry.haystack for word in self.words)

    def count(self, value: Value) -> int:
        """How many of the words a value matches (the pattern: 1 or 0)."""
        if self.regex:
            return 1 if self._has(value.text) or (value.now and self._has(value.now)) else 0
        if self.case:
            return sum(1 for word in self.words if word in value.text or word in value.now)
        text, now = value.text.lower(), value.now.lower()
        return sum(1 for word in self.words if word in text or word in now)

    def first(self, text: str) -> int | None:
        """Where the first match in a text starts (of a pattern, also an empty one: as Python's
        ``re.search`` finds it)."""
        if self.regex:
            if text not in self._first:
                self.prepare_first([text])
            return self._first[text]
        starts = [m.start() for pattern in self.patterns if (m := pattern.search(text)) is not None]
        return min(starts) if starts else None

    def spans(self, text: str, start: int, end: int) -> list[tuple[int, int]]:
        """The matches in ``text[start:end]`` (for a pattern, as if that part were all the text)."""
        if self.regex:
            return [(a + start, b + start) for a, b in self._spans_in(text[start:end])]
        return [
            (m.start(), m.end()) for pattern in self.patterns for m in pattern.finditer(text, start, end) if m.end() > m.start()
        ]


_MATCH = "\ue000", "\ue001"  # put around each match by Polars (characters for private use)
_NO_MATCH_MARKS = str.maketrans(dict.fromkeys(_MATCH, "\ufffd"))  # for texts that have them already
_SURROGATE = re.compile(r"[\ud800-\udfff]")  # half of a character, which Polars doesn't take (_series)


def _in_characters(text: str, at: int | None) -> int | None:
    """A position in a text's UTF-8 bytes, as Polars gives it, in characters (None stays None)."""
    if at is None or text.isascii():
        return at
    # A lone surrogate has 3 bytes this way, as the replacement character Polars got (_series)
    return len(text.encode("utf-8", "surrogatepass")[:at].decode("utf-8", "surrogatepass"))


def _series(texts: list[str]) -> pl.Series:
    """The texts as a Polars series. Polars takes no lone surrogate (half of a character, e.g. from
    a "\\ud800" in a project file): it becomes U+FFFD, so that the positions stay the same."""
    try:
        return pl.Series(texts, dtype=pl.String)
    except UnicodeEncodeError:
        return pl.Series([_SURROGATE.sub("\ufffd", text) for text in texts], dtype=pl.String)


def _polars(work: Callable[[pl.Series], pl.Series], texts: list[str]) -> list[Any]:
    """``work`` done on the texts as a Polars series; ValueError("Not a valid pattern: …") when
    Polars can't use the pattern (also look-around and back-references, which it doesn't have)."""
    series = _series(texts)
    try:
        return work(series).to_list()
    except (pl.exceptions.PolarsError, pl.exceptions.PanicException) as error:
        message = str(error)
        found = re.search(r"^error: (.+)$", message, re.MULTILINE)  # e.g. "error: unclosed group"
        reason = found[1] if found else (message.strip().splitlines() or ["invalid"])[0].removeprefix("regex error: ")
        raise ValueError(f"Not a valid pattern: {reason.strip()}") from None


def search(
    index: ProjectIndex,
    query: Query,
    *,
    problems: Mapping[str, tuple[str, str]] | None = None,
    changed: Collection[str] = (),
) -> list[Hit]:
    """The steps that match, in project order. With an empty query: the imports, exports and
    commands. ``problems``: step id -> (kind, message), kind "error", "warning", "incomplete" or
    "waiting" (the Find panel works them out from the session's results). ``changed``: the ids of
    the steps changed since the project was opened (``changed_steps``). Raises ValueError("Not a
    valid pattern: …") for a regular expression with a mistake."""
    problems = problems or {}
    changed = changed if isinstance(changed, set | frozenset | dict) else set(changed)
    finder = _Finder(query) if query.text.strip() else None
    if finder is not None:  # a pattern: Polars looks at all texts at once
        finder.prepare(text for entry in index.entries for v in entry.values for text in (v.text, v.now) if text)
    wanted = None if query.parameter is None else query.parameter.strip().lower()
    overview_only = query.empty  # the imports, exports and commands
    found = []
    for entry in index.entries:
        if overview_only and entry.kind == "step":
            continue
        if query.problems and entry.step_id not in problems:
            continue
        if query.off and entry.enabled and entry.table_enabled:
            continue
        if query.changed and entry.step_id not in changed:
            continue
        if wanted is not None and not _uses(entry, wanted):
            continue
        if finder is not None and not finder.entry(entry):
            continue
        found.append(entry)
    shown = [_shown_value(entry, finder, wanted) for entry in found]
    if finder is not None:  # the excerpt of a long text starts near its first match
        finder.prepare_first(finder.flat(t) for v in shown if v is not None for t in (v.text, v.now) if len(t) > 80)
    drafts = [_draft(entry, value, finder, query, problems.get(entry.step_id)) for entry, value in zip(found, shown, strict=True)]
    if finder is not None:  # the matches in all lines at once
        finder.prepare_spans(piece for draft in drafts for piece in draft.pieces())
    return [draft.hit(finder) for draft in drafts]


def _uses(entry: Entry, wanted: str) -> bool:
    """Whether the entry uses the parameter (lower case), or any parameter for ""."""
    return bool(entry.parameters) if not wanted else any(name.strip().lower() == wanted for name in entry.parameters)


def _hit(entry: Entry, finder: _Finder | None, query: Query, wanted: str | None, problem: tuple[str, str] | None) -> Hit:
    return _draft(entry, _shown_value(entry, finder, wanted), finder, query, problem).hit(finder)


@dataclass(frozen=True)
class _Draft:
    """A hit before the matches in its lines are marked (``search`` finds them in all lines at once)."""

    entry: Entry
    value: Value | None
    text: str  # line 2
    labels: list[tuple[int, int]]  # in line 2
    status: str
    focus: tuple[str, int, str] | None

    def pieces(self) -> list[str]:
        """The parts of the title and of line 2 that matches are marked in."""
        return [self.entry.table, self.entry.action, *(self.text[start:end] for start, end in _gaps(self.text, self.labels))]

    def hit(self, finder: _Finder | None) -> Hit:
        line = _marked(self.text, self.labels, finder)
        return Hit(self.entry, self.value, _title(self.entry, finder), line, self.status, self.focus, tooltip(self.entry))


def _shown_value(entry: Entry, finder: _Finder | None, wanted: str | None) -> Value | None:
    """The value line 2 shows: the one that matches the most words (not a name of the title), else
    the first with the wanted parameter; None for the overview, the problem or the summary."""
    value = None
    if finder is not None:
        best = 0
        for candidate in entry.values:
            if candidate.key not in NAMES and (count := finder.count(candidate)) > best:
                value, best = candidate, count
    elif wanted is not None:
        value = next((v for v in entry.values if not v.key.startswith("#") and _has_parameter(v.text, wanted, entry)), None)
    return value


def _draft(entry: Entry, value: Value | None, finder: _Finder | None, query: Query, problem: tuple[str, str] | None) -> _Draft:
    if value is not None and value.key == MODE:  # how a file is read or written: as the list shows it
        text, labels = _overview(entry)
    elif value is not None:
        text, labels = _value_text(value, finder)
    elif query.problems and problem is not None:
        kind = f"{_PROBLEMS.get(problem[0], problem[0].capitalize())}:"
        message = str(problem[1]).strip().splitlines()
        text, labels = f"{kind} {_excerpt(message[0] if message else '', finder)}", [(0, len(kind))]
    else:
        text, labels = _overview(entry)
    if value is not None and (not value.key.startswith("#") or value.key == NOTE):
        focus: tuple[str, int, str] | None = (value.key, value.row, value.field)
    elif value is not None and value.key == MODE and (setting := _mode_setting(entry, value.text)):
        focus = (setting, 0, "")  # the setting that says how ("to the Recycle Bin": Delete)
    elif entry.empty is not None:  # not complete yet: the setting to fill in
        focus = entry.empty
    else:
        setting = next((loc.endpoint.setting for loc in entry.locations if loc.endpoint.setting), "")
        focus = (setting, 0, "") if setting else None
    status = problem[0] if problem is not None else "off" if not (entry.enabled and entry.table_enabled) else ""
    return _Draft(entry, value, text, labels, status, focus)


def _mode_setting(entry: Entry, mode: str) -> str:
    """The key of the setting that an endpoint's ``mode`` comes from, else of the setting that
    names the endpoint; "" when none has the mode."""
    for location in entry.locations:
        for endpoint in (location.endpoint, location.now):
            if endpoint is not None and endpoint.mode == mode and (setting := endpoint.mode_setting or endpoint.setting):
                return setting
    return ""


def _has_parameter(text: str, wanted: str, entry: Entry) -> bool:
    """Whether a text has the wanted parameter's {Name} (any of the step's for "")."""
    names = {name.strip().lower() for name in entry.parameters}
    for match in PLACE.finditer(text):
        name = match[1].strip().lower()
        if (name == wanted) if wanted else (name in names):
            return True
    return False


def _value_text(value: Value, finder: _Finder | None) -> tuple[str, list[tuple[int, int]]]:
    """ "File: orders_{Month}.csv   now: orders_2026-10.csv", and where the label and "now:" are."""
    text, labels = "", []
    if value.key != SUMMARY:
        label = f"{value.label}:"
        labels.append((0, len(label)))
        text = label + " "
    text += _excerpt(value.text, finder)
    if value.now:
        labels.append((len(text) + 3, len(text) + 7))
        text += "   now: " + _excerpt(value.now, finder)
    return text, labels


def _title(entry: Entry, finder: _Finder | None) -> Line:
    text = f"{entry.table} · {entry.number} · {entry.action}"
    if finder is None:
        return Line(text)
    spans = finder.spans(text, 0, len(entry.table)) + finder.spans(text, len(text) - len(entry.action), len(text))
    return Line(text, tuple(sorted((start, end, "match") for start, end in spans)))


def _marked(text: str, labels: list[tuple[int, int]], finder: _Finder | None) -> Line:
    """A line with its labels, the matches outside them and every {Name} marked."""
    marks = [(start, end, "label") for start, end in labels]
    marks += [(m.start(), m.end(), "parameter") for m in PLACE.finditer(text)]
    if finder is not None:
        marks += [(a, b, "match") for start, end in _gaps(text, labels) for a, b in finder.spans(text, start, end)]
    return Line(text, tuple(sorted(marks)))


def _gaps(text: str, labels: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(start, end) of the parts of a line outside its labels."""
    gaps, position = [], 0
    for start, end in [*sorted(labels), (len(text), len(text))]:
        if start > position:
            gaps.append((position, start))
        position = max(position, end)
    return gaps


def _excerpt(text: str, finder: _Finder | None) -> str:
    """A text on one line, at most about 200 characters, around the first match when it comes late."""
    flat = finder.flat(text) if finder is not None else " ".join(text.split())
    start = 0
    if finder is not None and len(flat) > 80 and (first := finder.first(flat)) is not None and first > 80:
        start = first - 40
    shown = flat[start : start + _EXCERPT]
    return ("…" if start else "") + shown + ("…" if start + _EXCERPT < len(flat) else "")


# --- texts for the panel -----------------------------------------------------------------------


def location_text(location: Location, *, now: bool = False) -> str:
    """One line for an endpoint, as written (or with the values now)."""
    e = location.now if now and location.now is not None else location.endpoint
    place = e.place.strip() or "the project folder"
    if e.kind == SQL:  # "?": no connection chosen yet (only spaces too, as in the tooltip)
        text = f"SQL on {e.place.strip() or '?'}: {_one_line(e.name, _SQL_SHOWN)}"
    elif e.kind == DATABASE:
        text = f"{e.name or '?'}  on  {e.place.strip() or '?'}"
    elif e.imported:
        text = "the files the import read"
        if (via := location.via_shown if now else location.via) is not None:
            text += f"  ({via.name or '*'} in {via.place.strip() or 'the project folder'})"
    elif e.column:
        text = f"files listed in column {e.column}" + (f"  below  {e.place}" if e.place.strip() else "")
    elif e.kind == FOLDER:
        text = f"{e.name}{f', sheet {e.part}' if e.part else ''}  in  {place}" if e.name else place
    else:
        text = f"{e.name or _NO_NAME}{f', sheet {e.part}' if e.part else ''}  in  {place}"
    return text + (f"  ·  {e.mode}" if e.mode else "")


def overview(entry: Entry) -> Line:
    """Line 2 of a result without a matched value: its locations, with the values now."""
    text, labels = _overview(entry)
    return _marked(text, labels, None)


def _overview(entry: Entry) -> tuple[str, list[tuple[int, int]]]:
    if not entry.locations:
        return entry.summary, []
    text = ""
    for position, location in enumerate(entry.locations):
        if position:
            before, e = entry.locations[position - 1].endpoint, location.endpoint
            moved = before.direction in (CMD, IN) and e.direction == OUT and e.kind == FOLDER  # copy, move
            text += "  →  " if moved else "; "
        text += location_text(location)
    nows = [now for location in entry.locations if (now := _now_text(location))]
    if not nows:
        return text, []
    labels = [(len(text) + 3, len(text) + 7)]
    return text + "   now: " + "; ".join(nows), labels


def _now_text(location: Location) -> str:
    """What differs in the endpoint with the values now: "orders_2026-10.csv", "… in D:\\Data",
    "output\\2026-10" for a folder itself, "sheets 2025*" for how it is read or written; for the
    files the import read, the import's files."""
    now, written = location.now, location.endpoint
    if now is None and written.imported and location.via is not None:
        now, written = location.via_now, location.via
    if now is None:
        return ""
    # How, as the line shows it after the place (the import's own isn't shown for its files)
    mode = now.mode if now.mode != written.mode and location.now is not None else ""
    if (now.name, now.part, now.place) == (written.name, written.part, written.place):
        return mode  # only how differs: not the name and place again
    folder = now.kind == FOLDER and not now.name  # the folder itself: its place, without "in"
    if now.kind == SQL:
        text = _one_line(now.name, _SQL_SHOWN) if now.name != written.name else ""
    elif now.column or now.imported or folder:
        text = ""
    else:
        text = now.name + (f", sheet {now.part}" if now.part else "")
    if now.place != written.place:
        if now.kind in (DATABASE, SQL):
            text = f"{text} on {now.place.strip() or '?'}"
        else:
            place = now.place.strip() or "the project folder"
            text = place if folder else f"{text} in {place}"
    text = text.strip()
    return f"{text}  ·  {mode}" if text and mode else text or mode


def tooltip(entry: Entry) -> str:
    """Table, step, group, every location with full paths (and now), the note, now_problem."""
    lines = [f"{entry.table}, step {entry.number}: {entry.summary}"]
    if entry.group:
        lines.append(f"Group: {entry.group}")
    for location in entry.locations:
        lines.append(f"{_VERBS.get(location.endpoint.direction, 'Reads')} {_full_text(location, written=True)}")
        if location.now is not None or (location.endpoint.imported and location.via_now is not None):
            lines.append(f"    now {_full_text(location, written=False)}")
    if entry.note.strip():
        lines.append(f"Note: {entry.note.strip()}")
    if entry.now_problem:
        lines.append(f"The values now are unknown: {entry.now_problem}")
    return "\n".join(lines)


def _full_text(location: Location, *, written: bool) -> str:
    """An endpoint with its full path, or with its connection, for the tooltip."""
    e = location.endpoint if written or location.now is None else location.now
    mode = f" ({e.mode})" if e.mode else ""
    if e.kind == SQL:
        return f"SQL on {location.connection or '?'}: {_one_line(e.name, 2 * _SQL_SHOWN)}{mode}"
    if e.kind == DATABASE:
        return f"{e.name or '?'} on {location.connection or '?'}{mode}"
    if e.imported:  # the import's folder (a full path with the values now) and its files
        if location.via is None:
            return f"the files the import read{mode}"
        via = location.via if written else location.via_shown
        same_place = location.via_now is None or location.via_now.place == location.via.place
        folder = location.folder if same_place or not written else via.place
        files = f"{_join(folder, via.name)}{mode}"
        return f"the files the import read from {files}" if written else files
    same_place = location.now is None or location.now.place == location.endpoint.place
    folder = location.folder if same_place or not written else e.place
    if e.column:
        return f"the files listed in column {e.column}" + (f" below {folder}" if folder else "") + mode
    part = f", sheet {e.part}" if e.part else ""
    return f"{_join(folder, e.name)}{part}{mode}"


def _join(folder: str, name: str) -> str:
    if folder and name:
        return folder + ("" if folder.endswith(("\\", "/", ":")) else os.sep) + name
    return folder or name


def _one_line(text: str, length: int) -> str:
    # Only the start of a long text (each result's tooltip has its SQL): on one line, it is the
    # start of all the text on one line, long enough unless it is mostly spaces
    flat = " ".join(text[: 2 * length].split())
    if len(flat) <= length and len(text) > 2 * length:
        flat = " ".join(text.split())
    return flat if len(flat) <= length else flat[: length - 1] + "…"


# --- sections ----------------------------------------------------------------------------------


@dataclass
class Section:
    key: str  # stays the same between searches (for remembering what is collapsed)
    title: str
    color: str = ""  # a map group's color name, for its icon
    tooltip: str = ""
    sections: list[Section] = field(default_factory=list)
    hits: list[Hit] = field(default_factory=list)

    @property
    def count(self) -> int:
        """The hits in it and in its sections."""
        return len(self.hits) + sum(section.count for section in self.sections)


def sections(index: ProjectIndex, hits: list[Hit], by: str) -> list[Section]:
    """The hits grouped ``by`` "kind", "table" or "place"; empty sections are left out."""
    if by == "table":
        return _by_table(index, hits)
    if by == "place":
        return _by_place(index, hits)
    found = {kind: Section(f"kind:{kind}", SECTION_TITLES[kind]) for kind in KINDS}
    for hit in hits:
        found.get(hit.entry.kind, found["step"]).hits.append(hit)
    return [section for section in found.values() if section.hits]


def _by_table(index: ProjectIndex, hits: list[Hit]) -> list[Section]:
    tables: dict[str, Section] = {}
    groups: dict[str, Section] = {}
    grouped = any(entry.group_id for entry in index.entries)
    for hit in hits:
        entry = hit.entry
        table = tables.get(entry.table_id)
        if table is None:
            title = entry.table + ("" if entry.table_enabled else " (off)")
            table = tables[entry.table_id] = Section(f"table:{entry.table_id}", title)
            if grouped:
                group = groups.get(entry.group_id)
                if group is None:
                    group = groups[entry.group_id] = (
                        Section(f"group:{entry.group_id}", entry.group, color=entry.group_color)
                        if entry.group_id
                        else Section("group:", "Not in a group")
                    )
                group.sections.append(table)
        table.hits.append(hit)
    if not grouped:
        return list(tables.values())
    order = {group_id: position for position, group_id in enumerate(index.groups)}
    last = len(order) + 1
    return sorted(groups.values(), key=lambda s: last if s.key == "group:" else order.get(s.key[6:], len(order)))


def _by_place(index: ProjectIndex, hits: list[Hit]) -> list[Section]:
    places: dict[str, tuple[tuple[int, str], Section]] = {}
    names: dict[str, Section] = {}
    nowhere = Section("place:", "No file, folder or database")
    for hit in hits:
        seen = set()
        for location in hit.entry.locations:
            place_key, name_key = location.place_key, location.name_key
            if (place_key, name_key) in seen:
                continue
            seen.add((place_key, name_key))
            if place_key not in places:
                places[place_key] = (_place_order(location, index.directory), _place_section(location, index.directory))
            place = places[place_key][1]
            if place_key == "imported:":  # no folder known: the hits right below
                place.hits.append(hit)
                continue
            key = f"name:{place_key}|{name_key}"
            if key not in names:
                names[key] = Section(key, _name_title(location))
                place.sections.append(names[key])
            names[key].hits.append(hit)
        if not seen:
            nowhere.hits.append(hit)
    found = [section for _, section in sorted(places.values(), key=lambda item: item[0])]
    for section in found:
        section.sections.sort(key=lambda s: s.title.lower())
    return found + ([nowhere] if nowhere.hits else [])


def _place_order(location: Location, directory: Path | None) -> tuple[int, str]:
    """File and folder places by title, then connections by name, then no connection chosen, then
    the "Files …" places."""
    shown = location.now or location.endpoint
    if shown.kind in (DATABASE, SQL):
        return (1, shown.place.strip().lower()) if location.connection else (2, "")
    if location.place_key in ("imported:", "column:"):
        return 2, location.place_key
    return 0, _folder_title(location.folder or shown.place, directory).lower()


def _place_section(location: Location, directory: Path | None) -> Section:
    shown = location.now or location.endpoint
    key = f"place:{location.place_key}"
    if shown.kind in (DATABASE, SQL) and not location.connection:
        return Section(key, "No connection chosen", tooltip="The step's settings don't name a connection yet")
    if shown.kind in (DATABASE, SQL):
        return Section(key, location.connection, tooltip=location.connection)
    if location.place_key == "imported:":
        return Section(key, "Files the import read", tooltip="The table doesn't start with an import of all files in a folder")
    if location.place_key == "column:":
        return Section(key, "Files named in a column", tooltip="Full paths, or paths below the project's folder")
    folder = location.folder or shown.place
    return Section(key, _folder_title(folder, directory), tooltip=folder)


def _folder_title(folder: str, directory: Path | None) -> str:
    """A folder relative to the project's folder when inside it ("data\\inbox"), "Project folder"
    for that folder itself, else the full path."""
    if not folder:
        return "Project folder"
    if directory is None:
        return folder
    home = os.path.normcase(os.path.normpath(directory))
    here = os.path.normcase(folder)
    if here == home:
        return "Project folder"
    if here.startswith(home.rstrip("\\/") + os.sep):
        return folder[len(home.rstrip("\\/")) + 1 :]
    return folder


def _name_title(location: Location) -> str:
    shown = location.now or location.endpoint
    if shown.kind == SQL:
        return "SQL"
    if shown.kind == DATABASE:
        return shown.name.strip() or "(no table)"
    if shown.imported:
        return location.via_shown.name or "*" if location.via is not None else "*"
    if shown.column:
        return f"listed in column {shown.column}"
    if shown.kind == FOLDER and not shown.name:
        return "The folder itself"
    return shown.name or _NO_NAME

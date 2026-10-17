"""Project files: tables with their steps, plus database connections. Saved as JSON."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tafl import APP_NAME
from tafl.api import Action

FORMAT = "tafl-project"
FORMAT_VERSION = 1
EXTENSION = ".tafl"


class ProjectError(Exception):
    """The project file is invalid, or a table/connection does not exist."""


@dataclass
class Step:
    action: str
    params: dict[str, Any] = field(default_factory=dict)
    id: str = ""
    version: int = 1  # version of the action that wrote ``params`` (see Action.migrate)
    enabled: bool = True
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "action": self.action, "version": self.version}
        if not self.enabled:
            data["enabled"] = False
        if self.note:
            data["note"] = self.note
        data["params"] = self.params
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Step:
        return cls(
            action=str(data["action"]),
            params=dict(data.get("params") or {}),
            id=str(data.get("id") or ""),
            version=int(data.get("version", 1)),
            enabled=bool(data.get("enabled", True)),
            note=str(data.get("note") or ""),
        )


@dataclass
class Table:
    name: str
    id: str = ""
    steps: list[Step] = field(default_factory=list)
    note: str = ""
    position: tuple[float, float] | None = None  # where the table sits on the project map
    group: str = ""  # id of the group the table belongs to ("" = none)
    enabled: bool = True  # a table that is off is not calculated, and tables using it are skipped
    filters: list[dict[str, Any]] = field(default_factory=list)  # instant filters of the data grid (ui.filters)

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "name": self.name}
        if not self.enabled:
            data["enabled"] = False
        if self.note:
            data["note"] = self.note
        if self.group:
            data["group"] = self.group
        if self.position is not None:
            data["position"] = _position_to_json(self.position)
        if self.filters:
            data["filters"] = [dict(f) for f in self.filters]
        data["steps"] = [s.to_dict() for s in self.steps]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Table:
        return cls(
            name=str(data["name"]),
            id=str(data.get("id") or ""),
            steps=[Step.from_dict(s) for s in data.get("steps") or []],
            note=str(data.get("note") or ""),
            position=_position_from_json(data.get("position")),
            group=str(data.get("group") or ""),
            enabled=bool(data.get("enabled", True)),
            filters=[dict(f) for f in data.get("filters") or [] if isinstance(f, dict) and f.get("column")],
        )


@dataclass
class Group:
    """A named, colored set of tables, shown as a frame on the project map. A collapsed group
    is drawn as one box (at ``position``) instead of its tables."""

    name: str
    id: str = ""
    color: str = "blue"  # a key of tafl.ui.theme.GROUP_COLORS
    collapsed: bool = False
    position: tuple[float, float] | None = None  # where the collapsed box sits

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"id": self.id, "name": self.name, "color": self.color}
        if self.collapsed:
            data["collapsed"] = True
        if self.position is not None:
            data["position"] = _position_to_json(self.position)
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Group:
        return cls(
            name=str(data.get("name") or "Group"),
            id=str(data.get("id") or ""),
            color=str(data.get("color") or "blue"),
            collapsed=bool(data.get("collapsed", False)),
            position=_position_from_json(data.get("position")),
        )


def _position_to_json(position: tuple[float, float]) -> list[int]:
    return [round(position[0]), round(position[1])]


def _position_from_json(value: Any) -> tuple[float, float] | None:
    if isinstance(value, list) and len(value) == 2 and all(isinstance(v, (int, float)) for v in value):
        return float(value[0]), float(value[1])
    return None


@dataclass
class Connection:
    name: str
    type: str  # connector id, e.g. "sqlserver" or "sqlite"
    settings: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "type": self.type, "settings": self.settings}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Connection:
        return cls(name=str(data["name"]), type=str(data["type"]), settings=dict(data.get("settings") or {}))


@dataclass
class Parameter:
    """A value the settings of steps use as ``{name}``: fixed, or a formula when it starts with
    ``=`` (see ``tafl.api.parameters``)."""

    name: str
    value: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "value": self.value}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Parameter:
        value = data.get("value")
        return cls(name=str(data["name"]), value="" if value is None else str(value))


@dataclass
class Project:
    name: str = "Untitled"
    tables: list[Table] = field(default_factory=list)
    connections: list[Connection] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)
    parameters: list[Parameter] = field(default_factory=list)
    path: Path | None = field(default=None, compare=False)

    @property
    def directory(self) -> Path | None:
        """Folder of the project file; relative paths in steps are relative to it."""
        return self.path.parent if self.path else None

    # --- lookup ---------------------------------------------------------------------------

    def find_table(self, ref: str) -> Table | None:
        """Table by id, or else by name (case-insensitive)."""
        for table in self.tables:
            if table.id == ref:
                return table
        lowered = ref.lower()
        for table in self.tables:
            if table.name.lower() == lowered:
                return table
        return None

    def table(self, ref: str) -> Table:
        table = self.find_table(ref)
        if table is None:
            raise ProjectError(f"There is no table '{ref}'")
        return table

    def connection(self, name: str) -> Connection | None:
        lowered = name.lower()
        return next((c for c in self.connections if c.name.lower() == lowered), None)

    def group(self, group_id: str) -> Group | None:
        return next((g for g in self.groups if g.id == group_id), None)

    def members(self, group_id: str) -> list[Table]:
        """The tables of a group, in project order."""
        return [t for t in self.tables if t.group == group_id]

    # --- editing --------------------------------------------------------------------------

    def add_table(self, name: str) -> Table:
        if self.find_table(name) is not None:
            raise ProjectError(f"A table named '{name}' already exists")
        table = Table(name=name, id=self._new_id("t"))
        self.tables.append(table)
        return table

    def add_step(
        self,
        table: Table | str,
        action: Action | str,
        params: dict[str, Any] | None = None,
        *,
        version: int | None = None,
        index: int | None = None,
    ) -> Step:
        """Add a step. Pass the ``Action`` itself so the step records the version of the action
        that its parameters are written for; with an action id, ``version`` defaults to 1."""
        target = table if isinstance(table, Table) else self.table(table)
        if isinstance(action, Action):
            action_id, version = action.id, action.version if version is None else version
        else:
            action_id, version = action, 1 if version is None else version
        step = Step(action=action_id, params=dict(params or {}), id=self._new_id("s"), version=version)
        if index is None:
            target.steps.append(step)
        else:
            target.steps.insert(index, step)
        return step

    def add_group(self, name: str, color: str = "blue") -> Group:
        group = Group(name=name, id=self._new_id("g"), color=color)
        self.groups.append(group)
        return group

    def add_connection(self, name: str, type: str, **settings: Any) -> Connection:
        if self.connection(name) is not None:
            raise ProjectError(f"A connection named '{name}' already exists")
        connection = Connection(name=name, type=type, settings=settings)
        self.connections.append(connection)
        return connection

    def _new_id(self, prefix: str) -> str:
        used = {t.id for t in self.tables} | {s.id for t in self.tables for s in t.steps} | {g.id for g in self.groups}
        numbers = [int(m.group(1)) for u in used if (m := re.fullmatch(rf"{prefix}(\d+)", u))]
        return f"{prefix}{max(numbers, default=0) + 1}"

    # --- files ----------------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "name": self.name,
            "connections": [c.to_dict() for c in self.connections],
        }
        if self.groups:
            data["groups"] = [g.to_dict() for g in self.groups]
        if self.parameters:
            data["parameters"] = [p.to_dict() for p in self.parameters]
        data["tables"] = [t.to_dict() for t in self.tables]
        return data

    @classmethod
    def from_dict(cls, data: dict[str, Any], path: Path | None = None) -> Project:
        if data.get("format") != FORMAT:
            raise ProjectError(f"This is not a project file of {APP_NAME} (they end with {EXTENSION})")
        version = int(data.get("format_version", 1))
        if version > FORMAT_VERSION:
            raise ProjectError(f"The project was saved by a newer app (format {version})")
        project = cls(
            name=str(data.get("name") or "Untitled"),
            tables=[Table.from_dict(t) for t in data.get("tables") or []],
            connections=[Connection.from_dict(c) for c in data.get("connections") or []],
            groups=[Group.from_dict(g) for g in data.get("groups") or []],
            parameters=[Parameter.from_dict(p) for p in data.get("parameters") or []],
            path=path,
        )
        project._repair_ids()
        return project

    def save(self, path: str | Path | None = None) -> Path:
        """Write the project; the file is replaced atomically, so a crash can't corrupt it."""
        target = Path(path) if path is not None else self.path
        if target is None:
            raise ProjectError("No file name given")
        target = target.resolve()
        text = json.dumps(self.to_dict(), indent=2, ensure_ascii=False)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(prefix=target.name, suffix=".tmp", dir=target.parent)
        except PermissionError:
            raise ProjectError(f"No permission to save in {target.parent}: save the project in another folder") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                f.write(text + "\n")
            os.replace(tmp, target)
        except BaseException as error:
            Path(tmp).unlink(missing_ok=True)
            if isinstance(error, PermissionError) and target.is_file() and not os.access(target, os.W_OK):
                raise ProjectError(f"{target.name} is read-only, so it can't be saved") from None
            raise
        self.path = target
        return target

    @classmethod
    def load(cls, path: str | Path) -> Project:
        file = Path(path).resolve()
        try:
            text = file.read_text(encoding="utf-8-sig")  # also with the BOM some editors write
        except UnicodeDecodeError as e:
            raise ProjectError(f"The project file {file.name} can't be read: it is not UTF-8 text") from e
        try:
            data = json.loads(text)
        except (ValueError, RecursionError) as e:  # ValueError includes json.JSONDecodeError
            raise ProjectError(f"The project file {file.name} is damaged: {e}") from e
        try:
            return cls.from_dict(data, path=file)
        except ProjectError:
            raise
        except Exception as e:  # valid JSON, but parts are missing or of the wrong kind
            problem = _content_problem(data) or f"{type(e).__name__}: {e}"
            raise ProjectError(f"The project file {file.name} is damaged: {problem}") from e

    def _repair_ids(self) -> None:
        """Give tables/steps/groups that lack an id (or share one) a fresh one; forget group
        memberships of groups that don't exist."""
        seen: set[str] = set()
        for item in [*self.groups, *self.tables, *(s for t in self.tables for s in t.steps)]:
            if not item.id or item.id in seen:
                item.id = ""
            seen.add(item.id)
        for group in self.groups:
            if not group.id:
                group.id = self._new_id("g")
        groups = {g.id for g in self.groups}
        for table in self.tables:
            if not table.id:
                table.id = self._new_id("t")
            if table.group not in groups:
                table.group = ""
            for step in table.steps:
                if not step.id:
                    step.id = self._new_id("s")


def _content_problem(data: Any) -> str | None:
    """What makes the content of a project file unreadable, in words (None if nothing is found).
    It checks what ``Project.from_dict`` needs, for the message when that failed."""
    if not isinstance(data, dict):
        return f"it holds {_show(data)} instead of a project"
    problems: list[str] = []
    if not _converts(int, data.get("format_version", 1)):
        problems.append(f'"format_version" is {_show(data["format_version"])} instead of a whole number')
    for table, where in _parts(data, "tables", "table", ("name",), "", problems):
        if not _converts(iter, table.get("filters") or []):
            problems.append(f'the "filters" of {where} are {_show(table["filters"])} instead of a list')
        for step, at in _parts(table, "steps", "step", ("action",), f" of {where}", problems):
            if not _converts(dict, step.get("params") or {}):
                problems.append(f'the "params" of {at} are {_show(step["params"])} instead of an object')
            if not _converts(int, step.get("version", 1)):
                problems.append(f'the "version" of {at} is {_show(step["version"])} instead of a whole number')
    for connection, where in _parts(data, "connections", "connection", ("name", "type"), "", problems):
        if not _converts(dict, connection.get("settings") or {}):
            problems.append(f'the "settings" of {where} are {_show(connection["settings"])} instead of an object')
    _parts(data, "groups", "group", (), "", problems)
    _parts(data, "parameters", "parameter", ("name",), "", problems)
    return problems[0] if problems else None


def _parts(
    data: dict[str, Any], key: str, kind: str, required: tuple[str, ...], owner: str, problems: list[str]
) -> list[tuple[dict[str, Any], str]]:
    """The usable objects of a list in a project file, each with words for where it is (e.g.
    'table "Orders"'). What is wrong with the list or the others goes to ``problems``."""
    items = data.get(key) or []
    if not isinstance(items, list):
        problems.append(f'"{key}"{owner} is {_show(items)} instead of a list')
        return []
    usable = []
    for number, item in enumerate(items, start=1):
        name = item.get("name") if isinstance(item, dict) else None
        where = (f'{kind} "{name}"' if isinstance(name, str) and name else f"{kind} {number}") + owner
        if not isinstance(item, dict):
            problems.append(f"{where} is {_show(item)} instead of an object")
        elif missing := [k for k in required if k not in item]:
            problems.append(f'{where} has no "{missing[0]}"')
        else:
            usable.append((item, where))
    return usable


def _converts(convert: Any, value: Any) -> bool:
    try:
        convert(value)
    except Exception:
        return False
    return True


def _show(value: Any) -> str:
    """A value of a project file as it is written there, shortened."""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= 40 else text[:39] + "…"

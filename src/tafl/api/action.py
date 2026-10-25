"""Actions: the building blocks of a table."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

import polars as pl

from .endpoints import Endpoint, default_endpoints
from .errors import ActionError
from .params import List, Param, Params, ValidationContext, check, resolve, table_refs

if TYPE_CHECKING:
    from .context import Context

RunFunction = Callable[[pl.DataFrame | None, Params, "Context"], pl.DataFrame | None]

# Parameter names that would hide dict methods when read as attributes (p.keys, p.items, ...).
_RESERVED_NAMES = frozenset(dir(dict))


class Kind(StrEnum):
    """What an action does with the table."""

    SOURCE = "source"  # creates the table; always the first step
    TRANSFORM = "transform"  # table in, table out
    EXPORT = "export"  # writes the table somewhere; the table passes through unchanged
    COMMAND = "command"  # other side effects (files, SQL commands); the table passes through


@dataclass(frozen=True, eq=False)
class Action:
    id: str
    name: str
    run: RunFunction
    category: str = "Other"
    kind: Kind = Kind.TRANSFORM
    params: tuple[Param, ...] = ()
    version: int = 1
    migrate: Callable[[dict[str, Any], int], dict[str, Any]] | None = None
    summary: Callable[[Params], str] | str | None = None
    description: str = ""
    fingerprint: Callable[[Params, Context], Any] | None = None
    checks: Callable[[Params, ValidationContext], list[str]] | None = None
    endpoints: Callable[[Params], Iterable[Endpoint]] | None = None
    plugin: str = ""  # set by the registry

    @property
    def side_effects(self) -> bool:
        """Exports and commands change things outside the project; they only run in Run mode."""
        return self.kind in (Kind.EXPORT, Kind.COMMAND)

    def resolve(self, values: Mapping[str, Any] | None) -> Params:
        """Stored values merged over the parameter defaults."""
        return resolve(self.params, values)

    def upgrade(self, values: Mapping[str, Any], from_version: int) -> dict[str, Any]:
        """Bring parameters saved by an older version of this action up to date."""
        if from_version > self.version:
            raise ActionError(
                f"This step was saved by a newer version of '{self.name}' "
                f"(v{from_version}); this app has v{self.version}."
            )
        if from_version < self.version and self.migrate is not None:
            return dict(self.migrate(dict(values), from_version))
        return dict(values)

    def check(self, params: Params, vc: ValidationContext) -> list[str]:
        """The problems of the parameters, and those that ``checks`` finds."""
        return check(self.params, params, vc) + self.own_problems(params, vc)

    def own_problems(self, params: Params, vc: ValidationContext) -> list[str]:
        """The problems that ``checks`` finds (none without it)."""
        return list(self.checks(params, vc)) if self.checks is not None else []

    def table_refs(self, params: Params) -> list[str]:
        return table_refs(self.params, params)

    def endpoints_of(self, params: Params) -> list[Endpoint]:
        """What a step with these settings reads, writes or changes outside the project: from
        ``endpoints``, else from its file, folder and connection settings. A broken ``endpoints``
        function gives the latter; what isn't an Endpoint is left out."""
        if self.endpoints is not None:
            try:
                return [endpoint for endpoint in self.endpoints(params) if isinstance(endpoint, Endpoint)]
            except Exception:
                pass
        return default_endpoints(self, params)

    def describe(self, params: Params) -> str:
        """One line for the step list, e.g. "Sort by Date, Amount"."""
        if self.summary is None:
            return self.name
        try:
            if isinstance(self.summary, str):
                text = self.summary.format_map(_Lenient(params))
            else:
                text = self.summary(params)
        except (Exception, SystemExit):  # a broken summary must never break the step list
            return self.name
        return text or self.name

    def __call__(
        self,
        df: pl.DataFrame | None = None,
        params: Mapping[str, Any] | None = None,
        ctx: Context | None = None,
    ) -> pl.DataFrame | None:
        """Run the action directly (handy in tests); missing parameters get their defaults."""
        from .context import Context

        return self.run(df, self.resolve(params), ctx if ctx is not None else Context())


def action(
    id: str,
    name: str,
    *,
    category: str = "Other",
    kind: Kind | str = Kind.TRANSFORM,
    params: Iterable[Param] = (),
    version: int = 1,
    migrate: Callable[[dict[str, Any], int], dict[str, Any]] | None = None,
    summary: Callable[[Params], str] | str | None = None,
    fingerprint: Callable[[Params, Context], Any] | None = None,
    checks: Callable[[Params, ValidationContext], list[str]] | None = None,
    endpoints: Callable[[Params], Iterable[Endpoint]] | None = None,
) -> Callable[[RunFunction], Action]:
    """Declare an action by decorating its run function: ``(table, params, ctx) -> table``.

    * ``kind="source"``: the function gets ``table=None`` and returns the new table.
    * ``kind="export"`` / ``"command"``: may return ``None``; the table then passes through.
    * ``summary``: a format string (``"Sort by {column}"``) or a function of the params,
      shown in the step list.
    * ``fingerprint``: for sources, a function returning something that changes when the source
      changes (e.g. file modification time); the cached result is then refreshed automatically.
    * ``checks``: a function ``(params, vc) -> [problems]`` for what the parameters can't check
      on their own, e.g. a choice that needs something of the table (``ValidationContext``).
      Its problems stop the step before it runs, as those of the parameters do. Exports and
      commands don't run while the project is edited: there they show as warnings.
    * ``endpoints``: a function ``(params) -> [Endpoint]``: the files, folders, database tables and
      SQL that the step reads, writes or changes (see ``tafl.api.endpoints``). Without it they come
      from the action's file, folder and connection settings.

    The function's docstring becomes the help text shown in the app.
    """
    params = tuple(params)
    _check_param_names(id, params)

    def wrap(run: RunFunction) -> Action:
        return Action(
            id=id,
            name=name,
            run=run,
            category=category,
            kind=Kind(kind),
            params=params,
            version=version,
            migrate=migrate,
            summary=summary,
            description=inspect.getdoc(run) or "",
            fingerprint=fingerprint,
            checks=checks,
            endpoints=endpoints,
        )

    return wrap


def file_fingerprint(key: str = "file") -> Callable[[Params, Context], Any]:
    """A ``fingerprint`` for sources that read the file named in parameter ``key``:
    the cached result is refreshed automatically when the file changes."""

    def fingerprint(params: Params, ctx: Context) -> Any:
        path = ctx.resolve_path(params[key])
        try:
            stat = path.stat()
        except OSError:
            return (str(path), None)
        return (str(path), stat.st_mtime_ns, stat.st_size)

    return fingerprint


def _check_param_names(action_id: str, params: tuple[Param, ...]) -> None:
    seen: set[str] = set()
    for param in params:
        if param.key in _RESERVED_NAMES or param.key.startswith("_"):
            raise ValueError(f"Action '{action_id}': parameter name '{param.key}' is reserved")
        if param.key in seen:
            raise ValueError(f"Action '{action_id}': duplicate parameter '{param.key}'")
        seen.add(param.key)
        if isinstance(param, List):
            _check_param_names(f"{action_id}.{param.key}", tuple(param.fields))


class _Lenient(dict):
    def __missing__(self, key: str) -> str:
        return ""

"""The values of a project's parameters, and filling them into the settings of a step."""

from __future__ import annotations

import datetime as dt
import threading
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from typing import Any

import polars as pl

from etl_tool.api import ActionError, P, expressions
from etl_tool.api.parameters import (
    NAME,
    PLACE,
    Failed,
    fill_formula,
    fill_sql,
    fill_text,
    fixed,
    formula_places,
    plain,
    unknown,
)
from etl_tool.api.params import Param, is_visible, resolve

from .project import Project

TIME_LIMIT = 5.0  # seconds a parameter's formula may take
# The settings that get the values as text (P.Code with a language other than SQL too)
_TEXTS = (P.Text, P.File, P.Folder, P.Column, P.Columns)

SqlFill = Callable[[str, Mapping[str, Any]], str]
SqlSpans = Callable[[str], list[tuple[int, int, str]]]


def values(project: Project, overrides: Mapping[str, str] | None = None, *, limit: float = TIME_LIMIT) -> dict[str, Any]:
    """Name -> value of each parameter, in their order: fixed values typed (a ``Fixed``),
    formulas calculated (a formula can use the parameters above it, and may take ``limit``
    seconds). ``overrides`` (e.g. from ``tafl run --set``) replace the values written in the
    project; a value that can't be worked out is a Failed."""
    given = {name.strip().lower(): value for name, value in (overrides or {}).items()}
    names = [parameter.name.strip().lower() for parameter in project.parameters]
    found: dict[str, Any] = {}
    for index, parameter in enumerate(project.parameters):
        text = given.get(names[index], parameter.value)
        found[parameter.name] = _value(text, found, names[index:], limit)
    return found


def problems(project: Project, overrides: Mapping[str, str] | None = None) -> list[str]:
    """Names that can't be used, twice the same name, and overrides for no parameter."""
    found, seen = [], set()
    for parameter in project.parameters:
        name = parameter.name
        if name != name.strip() and name.strip():
            found.append(f"The parameter name '{name}' can't be used: it starts or ends with a space")
        elif not NAME.fullmatch(name):
            found.append(f"The parameter name '{name}' can't be used: letters, digits, _ and spaces, starting with a letter")
        elif name.strip().lower() in seen:
            found.append(f"There are two parameters named '{name}'")
        seen.add(name.strip().lower())
    for name in overrides or {}:
        if name.strip().lower() not in seen:
            listed = ", ".join(p.name for p in project.parameters) or "none"
            found.append(f"The project has no parameter '{name}' (its parameters: {listed})")
    return found


def fill(params: Sequence[Param], raw: Mapping[str, Any], values: Mapping[str, Any], sql: SqlFill = fill_sql) -> dict[str, Any]:
    """The settings ``raw`` of a step with ``{Name}`` filled in where they are shown: in formulas
    typed values, in SQL typed values written by ``sql`` (e.g. ``Connector.fill_sql``), in all
    other texts the values as text. Hidden settings stay as they are."""
    if not values:
        return dict(raw)
    filled = dict(raw)
    shown = _shown(params, raw)
    for param in params:
        if param.key in filled and is_visible(param, shown):
            filled[param.key] = _fill_value(param, filled[param.key], values, sql)
    return filled


def unknown_names(
    params: Sequence[Param], raw: Mapping[str, Any], values: Mapping[str, Any], sql_spans: SqlSpans | None = None
) -> list[str]:
    """Names in ``{…}`` that are no parameter, in the shown texts, paths, column names and SQL of
    the settings ``raw`` (they are left as they are; formulas report them as an error). The
    texts, names and comments of SQL are found by ``sql_spans`` (e.g. ``Connector.sql_spans``)."""
    found = []
    for param, text in _texts(params, raw):
        sql = isinstance(param, P.Code) and param.language == "sql"
        if sql or (isinstance(param, _TEXTS) and not isinstance(param, P.Expression)):
            spans = sql_spans(text) if sql and sql_spans is not None else None
            found += unknown(text, values, sql=sql, spans=spans)
    return list(dict.fromkeys(found))


def renamed(value: Any, renames: Mapping[str, str]) -> Any:
    """A step's settings with ``{Old}`` written as ``{New}`` for each renamed parameter (old
    name -> new name; upper and lower case of the old name don't matter)."""
    if isinstance(value, dict):
        return {key: renamed(item, renames) for key, item in value.items()}
    if isinstance(value, list):
        return [renamed(item, renames) for item in value]
    if not isinstance(value, str) or "{" not in value:
        return value
    folded = {old.strip().lower(): new for old, new in renames.items()}
    return PLACE.sub(lambda m: "{" + folded[m[1].strip().lower()] + "}" if m[1].strip().lower() in folded else m[0], value)


def value_label(value: Any) -> str:
    """The word for the type of a parameter's value."""
    value = plain(value)
    if isinstance(value, bool):
        return "true/false"
    if isinstance(value, int):
        return "whole number"
    if isinstance(value, float):
        return "number"
    if isinstance(value, dt.datetime):
        return "date-time"
    if isinstance(value, dt.date):
        return "date"
    if value is None:
        return "empty"
    return "text"


def _shown(params: Sequence[Param], raw: Mapping[str, Any]) -> Mapping[str, Any]:
    """The values that decide which settings are shown: the stored ones, else the defaults."""
    try:
        return resolve(params, raw)
    except Exception:  # a broken value: the action reports it
        return raw


def _fill_value(param: Param, value: Any, values: Mapping[str, Any], sql: SqlFill) -> Any:
    if isinstance(param, P.List):
        if not isinstance(value, list):
            return value
        return [fill(param.fields, item, values, sql) if isinstance(item, dict) else item for item in value]
    if isinstance(value, list):
        return [_fill_value(param, item, values, sql) for item in value]
    if not isinstance(value, str) or "{" not in value:
        return value
    if isinstance(param, P.Expression):
        return fill_formula(value, values)
    if isinstance(param, P.Code) and param.language == "sql":
        return sql(value, values)
    if isinstance(param, _TEXTS):
        return fill_text(value, values)
    return value


def _texts(params: Sequence[Param], raw: Mapping[str, Any]) -> Iterator[tuple[Param, str]]:
    """The shown settings that are texts (also in lists and in the rows of a P.List)."""
    shown = _shown(params, raw)
    for param in params:
        value = raw.get(param.key)
        if not is_visible(param, shown):
            continue
        if isinstance(param, P.List):
            for row in value if isinstance(value, list) else ():
                if isinstance(row, dict):
                    yield from _texts(param.fields, row)
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, str) and "{" in item:
                yield param, item


def _value(text: str, earlier: Mapping[str, Any], names: Sequence[str], limit: float) -> Any:
    """The value of a parameter; ``names`` are its own (folded) name and those below it."""
    if not text.lstrip().startswith("="):
        return fixed(text)
    formula = text.lstrip()[1:]
    above = {name.strip().lower() for name in earlier}
    for _start, _end, name, _place in formula_places(formula):
        folded = name.strip().lower()
        if folded in above:
            continue
        if folded == names[0]:
            return Failed("a parameter's formula can't use itself")
        if folded in names:
            return Failed(f"{{{name}}} is below this parameter: a formula can only use the parameters above it")
    try:
        formula = fill_formula(formula, earlier)
    except ActionError as e:
        return Failed(str(e))
    return _calculated(formula, limit)


def _calculated(formula: str, limit: float) -> Any:
    """The value of a formula. One that takes longer than ``limit`` seconds is stopped: a timer
    interrupts DuckDB on this thread, again and again until the formula is over (DuckDB forgets
    an interrupt that comes between two queries)."""
    con = expressions._connection()
    lock = threading.Lock()
    running, late = True, False

    def interrupt() -> None:
        nonlocal late
        while True:
            with lock:  # never after the formula is over: it would stop the next query on this thread
                if not running:
                    return
                late = True
                con.interrupt()
            time.sleep(0.05)

    timer = threading.Timer(limit, interrupt)
    timer.daemon = True
    timer.start()
    try:
        result = expressions.evaluate(pl.DataFrame({"__one": [0]}), formula)
    except Exception as e:
        if late:
            return Failed(f"the formula took longer than {limit:g} seconds")
        message = str(e) if isinstance(e, ActionError) else f"{type(e).__name__}: {e}"
        return Failed(message.split("\nFormula:")[0].removeprefix("Formula error: "))
    finally:
        timer.cancel()
        with lock:
            running = False
    return result[0]

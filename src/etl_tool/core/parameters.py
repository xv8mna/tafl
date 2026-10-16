"""The values of a project's parameters, and filling them into the settings of a step."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping
from typing import Any

import polars as pl

from etl_tool.api import ActionError, P, expressions
from etl_tool.api.parameters import NAME, PLACE, Failed, fill_formula, fill_sql, fill_text, sql_literal, typed

from .project import Project


def values(project: Project, overrides: Mapping[str, str] | None = None) -> dict[str, Any]:
    """Name -> value of each parameter, in their order: fixed values typed, formulas calculated
    (a formula can use the parameters above it). ``overrides`` (e.g. from ``tafl run --set``)
    replace the values written in the project; a value that can't be worked out is a Failed."""
    given = {name.lower(): value for name, value in (overrides or {}).items()}
    found: dict[str, Any] = {}
    for parameter in project.parameters:
        text = given.get(parameter.name.lower(), parameter.value)
        found[parameter.name] = _value(text, found)
    return found


def problems(project: Project, overrides: Mapping[str, str] | None = None) -> list[str]:
    """Names that can't be used, twice the same name, and overrides for no parameter."""
    found, seen = [], set()
    for parameter in project.parameters:
        if not NAME.fullmatch(parameter.name):
            found.append(f"The parameter name '{parameter.name}' can't be used: letters, digits, _ and spaces, starting with a letter")
        elif parameter.name.lower() in seen:
            found.append(f"There are two parameters named '{parameter.name}'")
        seen.add(parameter.name.lower())
    for name in overrides or {}:
        if name.lower() not in seen:
            listed = ", ".join(p.name for p in project.parameters) or "none"
            found.append(f"The project has no parameter '{name}' (its parameters: {listed})")
    return found


def fill(params: list, raw: Mapping[str, Any], values: Mapping[str, Any], literal: Callable[[Any], str] = sql_literal) -> dict[str, Any]:
    """The settings ``raw`` of a step with ``{Name}`` filled in: in formulas typed values, in SQL
    typed values written by ``literal``, in all other texts the values as text."""
    if not values:
        return dict(raw)
    filled = dict(raw)
    for param in params:
        if param.key in filled:
            filled[param.key] = _fill_value(param, filled[param.key], values, literal)
    return filled


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


def _fill_value(param: Any, value: Any, values: Mapping[str, Any], literal: Callable[[Any], str]) -> Any:
    if isinstance(param, P.List):
        if not isinstance(value, list):
            return value
        return [fill(param.fields, item, values, literal) if isinstance(item, dict) else item for item in value]
    if isinstance(value, list):
        return [_fill_value(param, item, values, literal) for item in value]
    if not isinstance(value, str) or "{" not in value:
        return value
    if isinstance(param, P.Expression):
        return fill_formula(value, values)
    if isinstance(param, P.Code) and param.language == "sql":
        return fill_sql(value, values, literal)
    if isinstance(param, P.Text | P.File | P.Folder | P.Column | P.Columns):
        return fill_text(value, values)
    return value


def _value(text: str, earlier: Mapping[str, Any]) -> Any:
    if not text.lstrip().startswith("="):
        return typed(text)
    formula = text.lstrip()[1:]
    try:
        result = expressions.evaluate(pl.DataFrame({"__one": [0]}), fill_formula(formula, earlier))
    except ActionError as e:
        return Failed(str(e).split("\nFormula:")[0].removeprefix("Formula error: "))
    return result[0]

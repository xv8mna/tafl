"""Project parameters: values that the settings of steps use, written as ``{Name}``.

A parameter's value is either fixed (``2026-10-01``, ``D:\\Data``, ``500``) or a formula that
is calculated when the project is calculated (``=TODAY() - 1``). Fixed values get a type as on
import: a whole number, a decimal number, a date (``2026-10-01``), a date-time
(``2026-10-01 08:30``), otherwise text; numbers with leading zeros (``01067``) stay text.

Where ``{Name}`` is filled in decides how:

- in texts, paths and names: the value as text (dates as ``2026-10-01``);
- in formulas: a typed value (``DATE '2026-10-01'``, ``500``, ``'text'``); inside a formula's own
  quotes (``'Report {Month}'``) the text;
- in SQL: a typed value in the database's own way of writing it (``Connector.literal``), so
  quotes need no care and nothing can be smuggled into the SQL; inside quotes the text.

Only the names of parameters are filled in (upper and lower case don't matter); other braces,
such as ``\\d{3}`` in a pattern, stay as they are.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable, Mapping
from typing import Any

from .errors import ActionError

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*")
PLACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ ]*)\}")
_INTEGER = re.compile(r"[+-]?(0|[1-9]\d*)")
_DECIMAL = re.compile(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?")


class Failed:
    """A parameter whose formula has an error: using it is an error too."""

    def __init__(self, message: str) -> None:
        self.message = message

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Failed) and other.message == self.message

    def __hash__(self) -> int:
        return hash(self.message)

    def __repr__(self) -> str:
        return f"Failed({self.message!r})"


def typed(text: str) -> Any:
    """The value of a fixed parameter: a number, a date, a date-time or the text itself."""
    value = text.strip()
    if _INTEGER.fullmatch(value):
        return int(value)
    if _DECIMAL.fullmatch(value):
        return float(value)
    try:
        return dt.date.fromisoformat(value)
    except ValueError:
        pass
    if "T" in value or " " in value:
        try:
            return dt.datetime.fromisoformat(value)
        except ValueError:
            pass
    return text


def as_text(value: Any) -> str:
    """A value written into a text, a path or a name."""
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds" if value.microsecond == 0 else "microseconds")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) if abs(value) < 1e15 else repr(value)
    return "" if value is None else str(value)


def formula_literal(value: Any) -> str:
    """A value written into a formula (DuckDB)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, int | float):
        return repr(value) if isinstance(value, int) or value == value and abs(value) != float("inf") else f"'{value}'::DOUBLE"
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def sql_literal(value: Any) -> str:
    """A value written into SQL, the way most databases read it (``Connector.literal``)."""
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, dt.datetime | dt.date):
        return "'" + as_text(value) + "'"
    return "'" + str(value).replace("'", "''") + "'"


def fill_text(text: str, values: Mapping[str, Any]) -> str:
    """``{Name}`` in a text, a path or a name: the value as text."""
    return PLACE.sub(lambda m: _found(m, values, as_text), text)


def fill_formula(formula: str, values: Mapping[str, Any]) -> str:
    """``{Name}`` in a formula: a typed value; inside the formula's '…' texts the value as text;
    in "names", [columns], $$texts$$ and comments nothing changes."""
    from .expressions import tokenize  # the formula's own tokens

    skips = []
    for token in tokenize(formula):
        if token.kind == "string" and formula.startswith("'", token.start):
            skips.append((token.start, token.end, "text"))
        elif token.kind in ("string", "quoted", "column", "comment"):
            skips.append((token.start, token.end, "name"))
    return _outside(formula, values, formula_literal, skips, inside_text=True)


def fill_sql(sql: str, values: Mapping[str, Any], literal: Callable[[Any], str] = sql_literal) -> str:
    """``{Name}`` in SQL: a typed value written by ``literal``; inside '…' the value as text,
    inside "…" and […] (names) and comments nothing changes."""
    return _outside(sql, values, literal, _sql_skips(sql), inside_text=True)


def left_in_formula(formula: str) -> str | None:
    """The first ``{Name}`` left in a formula outside its texts and comments: no parameter has
    that name."""
    return next((name for _start, _end, name, in_text in formula_places(formula) if not in_text), None)


def formula_places(formula: str) -> list[tuple[int, int, str, bool]]:
    """Where a formula has ``{Name}`` that would be filled in: (start, end, name, in a '…' text).
    Those in "names", [columns], $$texts$$ and comments are not listed."""
    from .expressions import tokenize

    found, position = [], 0
    for token in tokenize(formula):
        if token.kind in ("string", "quoted", "column", "comment"):
            found += [(m.start(), m.end(), m[1], False) for m in PLACE.finditer(formula, position, token.start)]
            if token.kind == "string" and formula.startswith("'", token.start):
                found += [(m.start(), m.end(), m[1], True) for m in PLACE.finditer(formula, token.start, token.end)]
            position = token.end
    found += [(m.start(), m.end(), m[1], False) for m in PLACE.finditer(formula, position)]
    return found


def unknown(text: str, values: Mapping[str, Any]) -> list[str]:
    """Names in ``{…}`` that are no parameter (for messages)."""
    known = {name.lower() for name in values}
    return [m[1] for m in PLACE.finditer(text) if m[1].strip().lower() not in known]


def _found(match: re.Match, values: Mapping[str, Any], write: Callable[[Any], str]) -> str:
    name = match[1].strip().lower()
    for key, value in values.items():
        if key.lower() == name:
            if isinstance(value, Failed):
                raise ActionError(f"The parameter {{{key}}} has an error: {value.message}")
            return write(value)
    return match[0]


def _outside(text: str, values: Mapping[str, Any], literal: Callable[[Any], str], skips: list[tuple[int, int, str]],
             inside_text: bool = False) -> str:  # fmt: skip
    """Fill in ``{Name}`` outside the ``skips`` (start, end, kind); in skips of kind "text"
    (with ``inside_text``) the value goes in as text with doubled quotes."""
    out, position = [], 0
    for start, end, kind in skips:
        out.append(PLACE.sub(lambda m: _found(m, values, literal), text[position:start]))
        piece = text[start:end]
        if inside_text and kind == "text":
            piece = PLACE.sub(lambda m: _found(m, values, lambda v: as_text(v).replace("'", "''")), piece)
        out.append(piece)
        position = end
    out.append(PLACE.sub(lambda m: _found(m, values, literal), text[position:]))
    return "".join(out)


def _sql_skips(sql: str) -> list[tuple[int, int, str]]:
    """Texts ('…'), names ("…", […], `…`) and comments of SQL."""
    skips, i, n = [], 0, len(sql)
    closing = {"'": "'", '"': '"', "[": "]", "`": "`"}
    while i < n:
        ch = sql[i]
        if ch in closing:
            close, j = closing[ch], i + 1
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:  # a doubled closing character stands for itself
                        j += 2
                        continue
                    break
                j += 1
            skips.append((i, min(j + 1, n), "text" if ch == "'" else "name"))
            i = j + 1
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            j = n if j < 0 else j
            skips.append((i, j, "comment"))
            i = j
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            skips.append((i, j, "comment"))
            i = j
        else:
            i += 1
    return skips

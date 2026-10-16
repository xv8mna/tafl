"""Suggestions while typing a formula: column names after [, the project's parameters after {,
functions and keywords after two letters of a word. Qt-free: what to suggest at the cursor and
what to write in."""

from __future__ import annotations

import re
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

import duckdb

from tafl.api import expressions
from tafl.api.parameters import Failed, as_text

from .functions import CURATED, CURSOR, Function, more_functions
from .parameters import value_label

LIMIT = 60  # suggestions shown at most
_PARAMETER_START = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ ]*|)\Z")  # searched up to the cursor
_PARAMETER_REST = re.compile(r"[A-Za-z0-9_ ]*\}")  # the rest of the name after the cursor
# Words of SQL that have no entry in the function list, with what is written in.
_KEYWORDS = (
    ("WHEN", "WHEN ", "CASE WHEN condition THEN value ... END"),
    ("THEN", "THEN ", "CASE WHEN condition THEN value ... END"),
    ("ELSE", "ELSE ", "CASE WHEN ... ELSE value END: the value when no condition is true"),
    ("END", "END", "Ends CASE WHEN ..."),
    ("NULL", "NULL", "An empty value"),
    ("TRUE", "TRUE", "True"),
    ("FALSE", "FALSE", "False"),
    ("INTERVAL", f"INTERVAL {CURSOR} DAY", "A time span to add to a date: [Date] + INTERVAL 3 DAY (or MONTH, YEAR, HOUR, ...)"),
    ("AS", "AS ", "CAST(value AS type)"),
    ("DISTINCT", "DISTINCT ", "COUNT(DISTINCT [Customer]): each value once"),
    ("OVER", f"OVER ({CURSOR})", "Makes a window function: SUM([Amount]) OVER (PARTITION BY [Customer])"),
    ("PARTITION BY", "PARTITION BY ", "The groups of a window function"),
    ("ORDER BY", "ORDER BY ", "The order inside a window function"),
    ("ASC", "ASC", "Ascending order"),
    ("DESC", "DESC", "Descending order"),
)


@dataclass(frozen=True)
class Suggestion:
    kind: str  # "column", "parameter", "function" or "keyword"
    name: str
    detail: str  # the type of a column, how a function is written
    help: str = ""  # what it does
    insert: str = ""  # what is written in; CURSOR marks where typing goes on
    row: int | None = None  # a column made by this row of Calculate columns
    key: str = ""  # what typing is matched against, if not the name

    def __post_init__(self) -> None:
        if not self.key:
            object.__setattr__(self, "key", self.name)


@dataclass(frozen=True)
class Context:
    """What is being typed at the cursor: ``formula[start:end]`` is replaced by the suggestion;
    ``prefix`` is the part before the cursor that the suggestions must match."""

    kind: str  # "column" (after [), "parameter" (after {) or "word"
    start: int
    end: int
    prefix: str


def context_at(formula: str, cursor: int, *, forced: bool = False) -> Context | None:
    """What is being typed at ``cursor``: a [column name], or a word (from 2 letters on, or any
    length when ``forced``, e.g. Ctrl+Space). Nothing inside texts and comments, and no word
    where a type or a unit goes ([Date]::DATE, CAST(x AS INTEGER), INTERVAL 1 MONTH): Enter
    must apply the formula there."""
    tokens = expressions.tokenize(formula)
    if (parameter := _parameter_at(formula, tokens, cursor)) is not None:
        return parameter
    for index, token in enumerate(tokens):
        if not token.start < cursor <= token.end and not (token.kind == "column" and token.start < cursor and not token.closed):
            continue
        if token.kind == "column" and cursor > token.start:
            if token.closed and cursor == token.end:  # just after the ]
                break
            end = token.end if token.closed else cursor
            if "[" in formula[cursor : end - 1] or "\n" in formula[cursor:end]:
                end = cursor  # a [ typed before other text: that text stays
            return Context("column", token.start, end, formula[token.start + 1 : cursor].replace("]]", "]"))
        if token.kind == "word":
            prefix = formula[token.start : cursor]
            if forced or (len(prefix) >= 2 and not _type_or_unit(formula, tokens, index)):
                return Context("word", token.start, token.end, prefix)
            return None
        if token.kind in ("string", "comment", "quoted", "number"):
            return None
    return Context("word", cursor, cursor, "") if forced else None


def suggestions(context: Context, columns: Sequence[Suggestion], parameters: Sequence[Suggestion] = ()) -> list[Suggestion]:
    """The suggestions for what is typed, best first: exact names, then names that start with
    it, then names that contain it. After [ only columns, after { only parameters; for a word,
    functions and keywords come before columns (after them when nothing is typed yet), then
    names from SQL Server and spreadsheets that lead to what to write instead, and DuckDB's
    other functions last."""
    prefix = context.prefix.casefold()
    if context.kind == "column":
        groups = [list(columns)]
    elif context.kind == "parameter":
        groups = [list(parameters)]
    else:
        curated, hints, more = _functions()
        groups = [curated, list(columns), hints, more] if prefix else [list(columns), curated, hints, more]
    found: list[tuple[int, int, int, Suggestion]] = []
    for group, items in enumerate(groups):
        for order, item in enumerate(items):
            rank = _rank(item.key.casefold(), prefix)
            if rank is not None:
                found.append((rank, group, order, item))
    found.sort(key=lambda entry: entry[:3])
    limit = None if context.kind in ("column", "parameter") else LIMIT  # every column: they are what the step has
    return [item for *_, item in found[:limit]]


def starts_with(context: Context, suggestion: Suggestion) -> bool:
    """True if the suggestion's name starts with what is typed (not only contains it)."""
    return suggestion.key.casefold().startswith(context.prefix.casefold())


def _rank(name: str, prefix: str) -> int | None:
    """0 the same name, 1 its first word (IS NULL for is), 2 starts with it, 3 contains it."""
    if name == prefix:
        return 0
    if not prefix or name.startswith(prefix):
        return 1 if name.split(" ")[0] == prefix else 2
    return 3 if prefix in name else None


def _type_or_unit(formula: str, tokens: list[expressions.Token], index: int) -> bool:
    """A word after :: or AS (a type), or after INTERVAL and a number or text (a unit)."""
    before = expressions._previous(tokens, index)
    if before is None:
        return False
    text = formula[tokens[before].start : tokens[before].end]
    if text == "::" or expressions._fold(text) == "as":
        return True
    two_before = expressions._previous(tokens, before)
    return (
        tokens[before].kind in ("number", "string")
        and two_before is not None
        and expressions._fold(formula[tokens[two_before].start : tokens[two_before].end]) == "interval"
    )


def insertion(context: Context, suggestion: Suggestion, formula: str) -> tuple[str, int]:
    """The formula with the suggestion written in, and where the cursor goes."""
    if suggestion.kind == "column":
        text = "[" + suggestion.name.replace("]", "]]") + "]"
    elif suggestion.kind == "parameter":
        text = "{" + suggestion.name + "}"
    else:
        text = suggestion.insert or suggestion.name
        if context.prefix and context.prefix == context.prefix.lower() and context.prefix.isalpha():
            text = text.lower()  # typed in lowercase: keep it that way
        if CURSOR in text and formula[context.end :].lstrip(" ")[:1] == "(":  # the ( is there already
            text = text[: text.index("(")] if "(" in text else text
    cursor = text.find(CURSOR)
    text = text.replace(CURSOR, "")
    new = formula[: context.start] + text + formula[context.end :]
    return new, context.start + (cursor if cursor >= 0 else len(text))


def parameter_suggestions(values: Mapping[str, Any]) -> list[Suggestion]:
    """Suggestions for the project's parameters (name -> value), with their values."""
    found = []
    for name, value in values.items():
        if isinstance(value, Failed):
            found.append(Suggestion("parameter", name, "error", help=f"This parameter has an error: {value.message}"))
        else:
            shown = as_text(value)
            shown = shown if len(shown) <= 40 else shown[:40] + "…"
            found.append(Suggestion("parameter", name, f"{value_label(value)}: {shown}", help="A parameter of the project (Project → Parameters)"))
    return found


def _parameter_at(formula: str, tokens: list[expressions.Token], cursor: int) -> Context | None:
    """{ and the start of a name before the cursor, where it is filled in: also in '…' texts,
    [columns] and "names", not in comments and $$texts$$."""
    found = _PARAMETER_START.search(formula, 0, cursor)
    if found is None:
        return None
    start = found.start()
    token = next((t for t in tokens if t.start <= start < t.end), None)
    if token is not None and (token.kind == "comment" or token.kind == "string" and not formula.startswith("'", token.start)):
        return None
    rest = _PARAMETER_REST.match(formula, cursor)
    return Context("parameter", start, rest.end() if rest else cursor, found[1])


def column_suggestions(columns: Sequence[tuple[str, str, int | None]]) -> list[Suggestion]:
    """Suggestions for (name, type, row) of the columns a formula can use."""
    return [Suggestion("column", name, kind if row is None else f"{kind} (row {row})", row=row) for name, kind, row in columns]


def warm_up() -> None:
    """Load DuckDB's functions in the background, so the first list opens without a pause."""
    if _functions.cache_info().currsize == 0 and not _warming.is_set():
        _warming.set()
        threading.Thread(target=_functions, daemon=True).start()


_warming = threading.Event()


@cache
def _functions() -> tuple[list[Suggestion], list[Suggestion], list[Suggestion]]:
    """The functions of the list (and keywords) first, DuckDB's other functions after them.
    Names from SQL Server and spreadsheets lead to what to write instead (DATEADD → + INTERVAL)."""
    curated: list[Suggestion] = []
    hints: list[Suggestion] = []
    for function in CURATED:
        key = _first_word(function.name)
        if not key or key == "INTERVAL":  # operators such as || or + - * /; INTERVAL is a keyword below
            continue
        insert = function.template.strip() + (" " if function.template.endswith(" ") else "")
        insert = insert or function.name
        curated.append(Suggestion("function", function.name, function.syntax, function.summary, insert, key=key))
        for alias in function.aliases:
            curated.append(Suggestion("function", alias, function.syntax, function.summary, insert.replace(function.name, alias, 1)))
        hints += _hints(function, insert)
    for function in CURATED:
        if function.name == "+ INTERVAL":
            hints += _hints(function, "+ INTERVAL " + CURSOR + " DAY")
    curated += [Suggestion("keyword", name, detail, insert=insert) for name, insert, detail in _KEYWORDS]
    known = {s.key.casefold() for s in curated}
    more = [
        Suggestion("function", f.name, f.syntax, f.summary, f.template or f.name)
        for f in more_functions()
        if f.name.casefold() not in known
    ]
    # A name DuckDB has (NOW, MAX, IF, ...) works: it is listed as itself, not as a hint
    existing = known | _duckdb_names()
    unique: dict[str, Suggestion] = {}
    for hint in hints:
        unique.setdefault(hint.name.casefold(), hint)
    return curated, [h for name, h in unique.items() if name not in existing], more


def _hints(function: Function, insert: str) -> list[Suggestion]:
    """Names from SQL Server and spreadsheets (``instead_of``) that lead to ``function``."""
    found = []
    for other in function.instead_of:
        about = function.note if other.casefold() in function.note.casefold() else function.summary
        found.append(Suggestion("function", other, f"→ {function.syntax}", f"Instead of {other}: {about}", insert))
    return found


def _duckdb_names() -> set[str]:
    con = duckdb.connect()
    try:
        return {row[0].casefold() for row in con.sql("SELECT DISTINCT function_name FROM duckdb_functions()").fetchall()}
    finally:
        con.close()


def _first_word(name: str) -> str:
    """The word a name is found by: SUM for "SUM ... OVER", DATE for "DATE '...'"."""
    word = re.match(r"[A-Za-z_][A-Za-z0-9_ ]*", name)
    return word.group().strip() if word else ""

"""Suggestions while typing a formula: column names after [, functions and keywords after two
letters of a word. Qt-free: what to suggest at the cursor and what to write in."""

from __future__ import annotations

import re
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache

from etl_tool.api import expressions

from .functions import CURATED, CURSOR, more_functions

LIMIT = 60  # suggestions shown at most
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
    kind: str  # "column", "function" or "keyword"
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

    kind: str  # "column" (after [) or "word"
    start: int
    end: int
    prefix: str


def context_at(formula: str, cursor: int, *, forced: bool = False) -> Context | None:
    """What is being typed at ``cursor``: a [column name], or a word (from 2 letters on, or any
    length when ``forced``, e.g. Ctrl+Space). Nothing inside texts and comments."""
    tokens = expressions.tokenize(formula)
    for token in tokens:
        if not token.start < cursor <= token.end and not (token.kind == "column" and token.start < cursor and not token.closed):
            continue
        if token.kind == "column" and cursor > token.start:
            if token.closed and cursor == token.end:  # just after the ]
                break
            end = token.end if token.closed else cursor
            return Context("column", token.start, end, formula[token.start + 1 : cursor].replace("]]", "]"))
        if token.kind == "word":
            prefix = formula[token.start : cursor]
            if len(prefix) >= 2 or forced:
                return Context("word", token.start, token.end, prefix)
            return None
        if token.kind in ("string", "comment", "quoted", "number"):
            return None
    return Context("word", cursor, cursor, "") if forced else None


def suggestions(context: Context, columns: Sequence[Suggestion]) -> list[Suggestion]:
    """The suggestions for what is typed, best first: exact names, then names that start with
    it, then names that contain it. After [ only columns; for a word, functions and keywords
    come before columns (after them when nothing is typed yet), then names from SQL Server and
    spreadsheets that lead to what to write instead, and DuckDB's other functions last."""
    prefix = context.prefix.casefold()
    if context.kind == "column":
        groups = [list(columns)]
    else:
        curated, hints, more = _functions()
        groups = [curated, list(columns), hints, more] if prefix else [list(columns), curated, hints, more]
    found: list[tuple[int, int, int, Suggestion]] = []
    for group, items in enumerate(groups):
        for order, item in enumerate(items):
            name = item.key.casefold()
            if not prefix or name.startswith(prefix):
                rank = 0 if name == prefix else 1
            elif prefix in name:
                rank = 2
            else:
                continue
            found.append((rank, group, order, item))
    found.sort(key=lambda entry: entry[:3])
    return [item for *_, item in found[:LIMIT]]


def insertion(context: Context, suggestion: Suggestion, formula: str) -> tuple[str, int]:
    """The formula with the suggestion written in, and where the cursor goes."""
    if suggestion.kind == "column":
        text = "[" + suggestion.name.replace("]", "]]") + "]"
    else:
        text = suggestion.insert or suggestion.name
        if context.prefix and context.prefix == context.prefix.lower() and context.prefix.isalpha():
            text = text.lower()  # typed in lowercase: keep it that way
        if CURSOR in text and formula[context.end : context.end + 1] == "(":  # the ( is there already
            text = text[: text.index("(")] if "(" in text else text
    cursor = text.find(CURSOR)
    text = text.replace(CURSOR, "")
    new = formula[: context.start] + text + formula[context.end :]
    return new, context.start + (cursor if cursor >= 0 else len(text))


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
        for other in function.instead_of:
            help = f"{other} doesn't exist in formulas: {function.summary[:1].lower()}{function.summary[1:]}"
            hints.append(Suggestion("function", other, f"→ {function.syntax}", help, insert))
    for function in CURATED:
        if function.name == "+ INTERVAL":
            for other in function.instead_of:
                hints.append(Suggestion("function", other, f"→ {function.syntax}", function.summary, "+ INTERVAL " + CURSOR + " DAY"))
    curated += [Suggestion("keyword", name, detail, insert=insert) for name, insert, detail in _KEYWORDS]
    known = {s.key.casefold() for s in curated}
    more = [
        Suggestion("function", f.name, f.syntax, f.summary, f.template or f.name)
        for f in more_functions()
        if f.name.casefold() not in known
    ]
    return curated, hints, more


def _first_word(name: str) -> str:
    """The word a name is found by: SUM for "SUM ... OVER", DATE for "DATE '...'"."""
    word = re.match(r"[A-Za-z_][A-Za-z0-9_ ]*", name)
    return word.group().strip() if word else ""

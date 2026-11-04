"""SQL-style formulas, evaluated with DuckDB.

Formulas use DuckDB's SQL expression syntax plus conveniences for SQL Server users:

* ``[Column Name]`` refers to a column (like T-SQL); standard ``"Column Name"`` works too.
* T-SQL helpers: ``IIF(cond, a, b)``, ``ISNULL(a, b)``, ``LEN(text)``, ``GETDATE()``,
  ``CHARINDEX(find, text)``.
* Text is joined with ``||`` or ``CONCAT(a, b, ...)`` (``+`` only adds numbers).
* Window functions work: ``SUM([Amount]) OVER (PARTITION BY [Customer])``.
* Comments: ``-- to the end of the line`` and ``/* ... */``.

Examples: ``[Price] * [Quantity]``, ``UPPER(TRIM([Name]))``,
``CASE WHEN [Amount] > 1000 THEN 'big' ELSE 'small' END``, ``[Date] + 30``.
"""

from __future__ import annotations

import atexit
import difflib
import itertools
import json
import re
import shutil
import string
import tempfile
import threading
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb
import polars as pl

from .dtypes import normalize
from .errors import ActionError
from .files import _new_scratch_folder

_MACROS = (
    "CREATE MACRO iif(c, a, b) AS CASE WHEN c THEN a ELSE b END",
    "CREATE MACRO len(s) AS length(rtrim(CAST(s AS VARCHAR)))",
    "CREATE MACRO getdate() AS current_localtimestamp()",
    "CREATE MACRO charindex(find, s) AS instr(s, find)",
    # ß becomes SS, as in Clean text (DuckDB writes the rare capital ẞ)
    "CREATE MACRO upper(s) AS replace(system.main.upper(s), 'ẞ', 'SS')",
    "CREATE MACRO ucase(s) AS replace(system.main.upper(s), 'ẞ', 'SS')",
)
# Reserved words in DuckDB that cannot be macros; rewritten when called as functions.
_REWRITES = {"isnull": "coalesce"}
# SQL Server functions that DuckDB doesn't have, and what to write instead.
_TSQL_HINTS = {
    "dateadd": "add an interval instead: [Date] + INTERVAL 3 DAY (or MONTH, YEAR)",
    "eomonth": "use LAST_DAY([Date])",
    "convert": "use CAST([Value] AS VARCHAR) (or BIGINT, DOUBLE, DATE, ...)",
    "try_convert": "use TRY_CAST([Value] AS BIGINT) (or VARCHAR, DOUBLE, DATE, ...)",
    "isnumeric": "use TRY_CAST([Value] AS DOUBLE) IS NOT NULL",
    "isdate": "use TRY_CAST([Value] AS DATE) IS NOT NULL",
    "datename": "use DAYNAME([Date]) or MONTHNAME([Date])",
    "getutcdate": "use NOW() AT TIME ZONE 'UTC'",
    "replicate": "use REPEAT(text, count)",
    "newid": "use GEN_RANDOM_UUID()",
    "text": "for a date use STRFTIME([Date], '%d.%m.%Y'), for a number FORMAT('{:,.2f}', [Amount]::DOUBLE)",
    "proper": "for a capital first letter write UPPER(LEFT([Name], 1)) || LOWER(SUBSTRING([Name], 2))",
    "initcap": "for a capital first letter write UPPER(LEFT([Name], 1)) || LOWER(SUBSTRING([Name], 2))",
}
# Functions without arguments that are easily written without their parentheses.
_NO_ARGUMENTS = {"today", "now", "getdate", "random", "pi", "gen_random_uuid"}
# Functions whose first argument is a date part, and SQL Server's names for the parts.
_DATE_FUNCTIONS = {"datediff", "date_diff", "datepart", "date_part", "datetrunc", "date_trunc", "datename", "datesub", "date_sub"}
_DATE_PARTS = {
    "year": "year", "yy": "year", "yyyy": "year", "quarter": "quarter", "qq": "quarter", "q": "quarter",
    "month": "month", "mm": "month", "m": "month", "week": "week", "wk": "week", "ww": "week",
    "day": "day", "dd": "day", "d": "day", "dayofyear": "dayofyear", "dy": "dayofyear", "y": "dayofyear",
    "weekday": "dow", "dw": "dow", "hour": "hour", "hh": "hour", "minute": "minute", "mi": "minute",
    "n": "minute", "second": "second", "ss": "second", "s": "second", "millisecond": "millisecond",
    "ms": "millisecond",
}  # fmt: skip
_CLOSING = {"string": "'", "quoted": '"', "column": "]"}
_TEXT_PREFIXES = {"e": "e", "E": "e", "b": "b", "B": "b", "x": "b", "X": "b"}  # see Token.prefix
# The characters that DuckDB's own parser reads as spaces outside texts and names, before its
# scanner sees them. All other characters from U+0080 on are letters for DuckDB.
_WIDE_SPACES = "  -​  ⁠　﻿"
_LETTER = rf"(?:[A-Za-z_]|(?![{_WIDE_SPACES}])[^\x00-\x7f])"
_DIGITS = "[0-9]+(?:_[0-9]+)*+"
_OPERATORS = set("+-*/%<>=!|&^~@#`")
_SPACE = re.compile(rf"[ \t\n\r\f{_WIDE_SPACES}]+")
_WIDE_SPACE = re.compile(f"[{_WIDE_SPACES}]")
_WORD = re.compile(rf"{_LETTER}(?:{_LETTER}|[0-9$])*+")
_NUMBER = re.compile(rf"(?:{_DIGITS}\.(?!\.)(?:{_DIGITS})?|\.{_DIGITS}|{_DIGITS})(?:[eE][+-]?{_DIGITS})?")
_PLACEHOLDER = re.compile(rf"[$?]{_DIGITS}")  # $1 and ?1: one token, what follows stands on its own
_DOLLAR = re.compile(rf"\$(?:{_LETTER}(?:{_LETTER}|[0-9])*+)?\$")
_LINE_END = re.compile(r"[\r\n]")
_ASCII_LOWER = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)

_VIEW = "__input"
_ROW = "__etl_row__"
_FRAME = "__frame"  # the data of a formula that is calculated on a table (see _needs_table)
# The rows of one part of a result, as DuckDB makes them. Each part keeps room for the rows asked
# for, so DuckDB's default of a million makes a column hold 8 MB per part, however few rows it has.
_PART_ROWS = 122_880
_SAFE = {"enable_external_access": False, "autoinstall_known_extensions": False, "autoload_known_extensions": False}
# DuckDB's settings and catalog: no use in a formula, and they show paths of this computer. The
# same for functions that run or plan a text as a query (what it calls can't be seen) and for
# those that would stay in force for the formulas of the next project.
_BLOCKED = {
    "current_setting", "getvariable", "which_secret",
    "query", "query_table", "json_execute_serialized_sql", "json_serialize_plan",
    "enable_logging", "disable_logging", "truncate_duckdb_logs", "enable_profiling", "disable_profiling", "checkpoint", "force_checkpoint",
    "setseed",
}  # fmt: skip
_BLOCKED_PREFIXES = ("duckdb_", "pragma_")
# Names that are only blocked where DuckDB reads them as a table (see _reads_blocked_table): a column
# may have them, and HISTOGRAM is also a normal function. pg_settings is DuckDB's settings as a view;
# the two others read the table that a text names, which a formula can build while it runs.
_BLOCKED_TABLES = {"pg_settings", "histogram", "histogram_values"}
_SANDBOX = (
    "a formula can only use the data of the step: no files, network, extensions or settings "
    "(use an import step for files)"
)
_temp: Path | None = None
_temp_lock = threading.Lock()
_connections: dict[int, duckdb.DuckDBPyConnection] = {}  # one per thread, by its id
_connections_lock = threading.Lock()


@dataclass(frozen=True)
class Token:
    """A piece of a formula, ``formula[start:end]``.

    ``kind`` is one of: column (``[Name]``), quoted (``"Name"``), string (``'text'``,
    ``E'text'``, ``B'101'``, ``X'1F'`` or ``$$text$$``), comment, number, word (functions,
    keywords, names), operator, open, close, comma, space. ``closed`` is False for a text,
    column or comment that isn't closed. A text has the ``prefix`` "e" (with backslash escapes)
    or "b" (bits or hex: a doubled quote is the end of it and the start of another text);
    ``joined`` is True for a text that DuckDB joins to the one before it, whose prefix it has.
    """

    kind: str
    start: int
    end: int
    closed: bool = True
    prefix: str = ""
    joined: bool = False


def tokenize(formula: str) -> list[Token]:
    """Split a formula into tokens, as DuckDB reads what ``translate`` makes of it (in which a
    comment is spaces). It never fails: what isn't closed runs to the end."""
    tokens: list[Token] = []
    i, n = 0, len(formula)
    while i < n:
        ch = formula[i]
        closed, prefix, joined = True, "", False
        dollar = _DOLLAR.match(formula, i) if ch == "$" else None
        if ch == "'" or (ch in _TEXT_PREFIXES and formula.startswith("'", i + 1)):
            before = _text_to_join(formula, tokens) if ch == "'" else None
            joined = before is not None
            prefix = before.prefix if before is not None else _TEXT_PREFIXES.get(ch, "")
            kind = "string"
            end, closed = _closing(formula, i + (ch != "'") + 1, _CLOSING[kind], escapes=prefix == "e", doubled=prefix != "b")
        elif ch in '"[':
            kind = {'"': "quoted", "[": "column"}[ch]
            end, closed = _closing(formula, i + 1, _CLOSING[kind])
        elif dollar:  # $$text$$ or $tag$text$tag$
            found = formula.find(dollar.group(), dollar.end())
            closed = found >= 0
            kind, end = "string", found + len(dollar.group()) if closed else n
        elif formula.startswith("--", i):
            line_end = _LINE_END.search(formula, i)
            kind, end = "comment", line_end.start() if line_end else n
        elif formula.startswith("/*", i):
            end, closed = _comment_end(formula, i)
            kind = "comment"
        elif space := _SPACE.match(formula, i):
            kind, end = "space", space.end()
        elif number := _NUMBER.match(formula, i):
            kind, end = "number", number.end()
        elif word := _WORD.match(formula, i):
            kind, end = "word", word.end()
        elif ch in "(),":
            kind, end = {"(": "open", ")": "close", ",": "comma"}[ch], i + 1
        elif ch in _OPERATORS:
            kind, end = "operator", i + 1
            while end < n and formula[end] in _OPERATORS and not formula.startswith(("--", "/*"), end):
                end += 1
        elif placeholder := _PLACEHOLDER.match(formula, i):
            kind, end = "operator", placeholder.end()
        else:
            kind, end = "operator", i + (2 if formula.startswith(("::", ":=", ".."), i) else 1)
        tokens.append(Token(kind, i, end, closed, prefix, joined))
        i = end
    return tokens


def _text_to_join(formula: str, tokens: list[Token]) -> Token | None:
    """The text that DuckDB joins a text after ``tokens`` to: the last token that isn't a space
    or a comment, when a line ends between them. Not after a $$text$$."""
    line_end = False
    for token in reversed(tokens):
        if token.kind not in ("space", "comment"):
            return token if line_end and token.kind == "string" and formula[token.start] != "$" else None
        line_end = line_end or _LINE_END.search(formula, token.start, token.end) is not None
    return None


def column_names(formula: str) -> list[str]:
    """The column names a formula refers to (``[Name]`` and ``"Name"``), in order, once each."""
    names: list[str] = []
    for token in tokenize(formula):
        if token.kind in ("column", "quoted") and token.closed:
            name = _name(formula, token)
            if name not in names:
                names.append(name)
    return names


def translate(formula: str, *, row_number: bool = True) -> str:
    """Turn a user formula into DuckDB SQL: ``[Col]`` -> ``"Col"``, ``ISNULL(`` -> ``coalesce(``.

    Texts and identifiers in double quotes are left untouched; comments become spaces. Raises
    ActionError for a text, ``[column]``, comment or parenthesis that isn't closed. With
    ``row_number``, ``COLUMNS(*)`` leaves out the row number that keeps the rows in order.
    """
    tokens = tokenize(formula)
    problem = _unclosed(formula, tokens)
    if problem:
        raise ActionError(f"Formula error: {problem}\nFormula: {formula}")
    around = _hide_row_number(formula, tokens) if row_number else {}
    out: list[str] = []
    for index, token in enumerate(tokens):
        text = formula[token.start : token.end]
        before, after = around.get(index, ("", ""))
        out.append(before)
        if token.kind == "column":
            out.append('"' + _name(formula, token).replace('"', '""') + '"')
        elif token.kind == "comment":
            out.append(re.sub(r"[^\r\n]", " ", text))
        elif token.kind == "space":  # DuckDB's own reading of these depends on the text before them
            out.append(_WIDE_SPACE.sub(" ", text))
        elif token.kind == "word" and _is_call(tokens, index):
            out.append(_REWRITES.get(text.lower(), text))
        else:
            out.append(text)
        out.append(after)
    return "".join(out)


def _hide_row_number(formula: str, tokens: list[Token]) -> dict[int, tuple[str, str]]:
    """``COLUMNS(*)`` must not see the row number that keeps the rows in order: what to add
    before and after tokens so that it becomes ``COLUMNS(* EXCLUDE (__etl_row__))``."""
    around: dict[int, tuple[str, str]] = {}
    for index, token in enumerate(tokens):
        if token.kind != "word" or _fold(formula[token.start : token.end]) != "columns" or not _is_call(tokens, index):
            continue
        star = _next_index(tokens, _next_index(tokens, index))
        if star is None or formula[tokens[star].start : tokens[star].end] != "*":
            continue
        exclude = _next_index(tokens, star)
        if exclude is None or tokens[exclude].kind != "word" or _fold(formula[tokens[exclude].start : tokens[exclude].end]) != "exclude":
            around[star] = ("", f" EXCLUDE ({_ROW})")
            continue
        names = _next_index(tokens, exclude)
        if names is None:
            continue
        if tokens[names].kind == "open":  # EXCLUDE (a, b)
            around[names] = ("", f"{_ROW}, ")
        else:  # EXCLUDE a
            around[names] = (f"({_ROW}, ", ")")
    return around


def _needs_table(formula: str) -> bool:
    """COLUMNS with a pattern or a lambda, and a query on __input, would see the row number that
    keeps the rows in order (only COLUMNS(*) can leave it out): such a formula is calculated on
    a table, whose own row numbers (rowid) they don't see."""
    tokens = tokenize(formula)
    for index, token in enumerate(tokens):
        if token.kind != "word":
            continue
        word = _fold(formula[token.start : token.end])
        if word == _VIEW:
            return True
        if word == "columns" and _is_call(tokens, index):
            argument = _next_index(tokens, _next_index(tokens, index))
            if argument is None or formula[tokens[argument].start : tokens[argument].end] != "*":
                return True
    return False


def _needs_all_columns(formula: str) -> bool:
    """COLUMNS(*), a * among the arguments of a function, a #1 for the first column or a subquery
    on __input (also one named by a text) use columns that aren't named in the formula: they would
    give other values on fewer columns."""
    if _VIEW in _fold(formula):
        return True
    tokens = tokenize(formula)
    for index, token in enumerate(tokens):
        text = formula[token.start : token.end]
        if token.kind == "word" and _fold(text) in ("columns", _VIEW):
            return True
        if token.kind in ("column", "quoted") and token.closed and _fold(_name(formula, token)) == _VIEW:
            return True
        if token.kind == "operator" and "#" in text:
            return True
        if token.kind == "operator" and text.startswith("*"):
            before = _previous(tokens, index)
            if before is None or tokens[before].kind == "comma":
                return True
            if tokens[before].kind == "open":
                function = _previous(tokens, before)
                if function is None or _fold(formula[tokens[function].start : tokens[function].end]) != "count":
                    return True
    return False


def _with_row_number(df: pl.DataFrame, formula: str) -> pl.DataFrame:
    """The row number that keeps the rows in order, with the columns that ``formula`` uses: the
    row number makes Polars line up the parts of all columns that DuckDB gets, which copies them."""
    numbered = df.with_row_index(_ROW)
    if _needs_all_columns(formula):
        return numbered
    named = {_fold(name) for name in column_names(formula)}
    named.update(_fold(formula[t.start : t.end]) for t in tokenize(formula) if t.kind == "word")
    return pl.DataFrame([numbered.get_column(name) for name in numbered.columns if name == _ROW or _fold(name) in named])


def evaluate(df: pl.DataFrame, formula: str, *, condition: bool = False) -> pl.Series:
    """Evaluate a formula for every row; returns one value per row, in row order. With
    ``condition``, the result must be true or false."""
    if not formula or all(t.kind in ("space", "comment") for t in tokenize(formula)):
        raise ActionError("The formula is empty")
    from .parameters import left_in_formula

    if (name := left_in_formula(formula)) is not None:
        raise ActionError(f"Formula error: there is no parameter {{{name}}} (Project → Parameters lists them)\nFormula: {formula}")
    table = _needs_table(formula)
    if table and any(_fold(column) == "rowid" for column in df.columns):
        raise ActionError(
            "Formula error: COLUMNS with a pattern or a lambda, and a query on __input, don't work with a "
            "column named rowid (DuckDB keeps that name for its own row numbers): rename the column first"
            f"\nFormula: {formula}"
        )
    body = translate(formula, row_number=not table)
    _check_calls(formula)
    _check_names(formula, df.columns)
    con = _connection()
    order = "rowid" if table else _ROW
    frame, columns = _readable(df if table else _with_row_number(df, formula))
    view = not table and columns != "*"  # a view that gives columns their own types back
    try:
        try:
            if table or view:
                _register(con, frame, _FRAME)
                _clear(con, _VIEW)
                con.execute(f'CREATE TEMP {"TABLE" if table else "VIEW"} "{_VIEW}" AS SELECT {columns} FROM "{_FRAME}"')
            else:
                _register(con, frame, _VIEW)
        except duckdb.Error as e:  # e.g. interrupted
            raise ActionError(_clean_error(e, formula, df.columns)) from None
        try:
            # On lines of its own: an error on the line after the formula means it ends too early
            relation = _query(con, f"SELECT (\n{body}\n) AS value FROM {_VIEW} ORDER BY {order}", formula)
            result_type = str(relation.types[0])
            if result_type.startswith("TIMESTAMP WITH TIME ZONE"):  # local time: the grid shows no zones
                relation = _query(con, f"SELECT CAST((\n{body}\n) AS TIMESTAMP) AS value FROM {_VIEW} ORDER BY {order}", formula)
                result_type = "TIMESTAMP"
            elif result_type == "UHUGEINT":  # Arrow would take it for a signed number: from 2^127 on, a negative one
                relation = _query(con, f"SELECT CAST((\n{body}\n) AS HUGEINT) AS value FROM {_VIEW} ORDER BY {order}", formula)
                result_type = "HUGEINT"
        except duckdb.Error as e:
            raise ActionError(_clean_error(e, formula, df.columns)) from None
        problem = _unusable_result(result_type)
        if problem:
            raise ActionError(f"Formula error: {problem}\nFormula: {formula}")
        if condition and result_type != "BOOLEAN":
            raise ActionError(_not_a_condition(_type_word(result_type)))
        try:
            result = relation.pl(_PART_ROWS)
        except duckdb.Error as e:
            raise ActionError(_clean_error(e, formula, df.columns)) from None
        except Exception as e:  # e.g. a result type Polars can't take
            raise ActionError(f"Formula error: {_first_line(e)}\nFormula: {formula}") from None
        except BaseException as e:  # a crash inside Polars (Rust) is not an Exception
            if not is_panic(e):
                raise
            raise ActionError(f"Formula error: {_first_line(e)}\nFormula: {formula}") from None
    finally:
        for name in (_VIEW, _FRAME):
            try:
                con.unregister(name)
            except duckdb.Error:  # interrupted (the view may be left: the next formula drops it)
                pass
        if table or view:
            _clear(con, _VIEW)
    if result.height != df.height:
        raise ActionError(
            "The formula must give one value per row. For totals use the Aggregate action, "
            "or a window function such as SUM([Amount]) OVER ()."
        )
    return normalize(result.to_series(0))


def add_columns(df: pl.DataFrame, columns: Sequence[tuple[str, str]]) -> pl.DataFrame:
    """Add columns calculated from formulas; a column that exists is replaced in place.
    Later formulas can use the columns calculated before them."""
    for name, formula in columns:
        if not name.strip():
            raise ActionError("A new column needs a name")
        try:
            values = evaluate(df, formula)
        except ActionError as e:  # say which of several formulas is wrong
            text = str(e)
            if text.startswith("Formula error: "):
                text = f"Formula error in [{name}]: " + text.removeprefix("Formula error: ")
            else:
                text = f"[{name}]: {text}"
            raise ActionError(text) from None
        df = df.with_columns(values.alias(name))
    return df


def filter_rows(df: pl.DataFrame, condition: str, keep: bool = True) -> pl.DataFrame:
    """Keep (or remove) the rows where ``condition`` is true. Empty results count as false."""
    mask = evaluate(df, condition, condition=True)
    if mask.dtype != pl.Boolean:
        raise ActionError(_not_a_condition(f"values of type {mask.dtype}"))
    mask = mask.fill_null(False)
    return df.filter(mask if keep else ~mask)


def is_panic(error: BaseException) -> bool:
    """A crash inside Polars (Rust): such errors derive from BaseException, not Exception."""
    return type(error).__name__ == "PanicException"


def _connection() -> duckdb.DuckDBPyConnection:
    """This thread's connection. It is kept by the thread's id, not in threading.local: a thread
    of a Qt thread pool gets new thread-local data for every job, which would make a new
    connection (and temp folder) for every formula check."""
    ident = threading.get_ident()
    con = _connections.get(ident)
    if con is None:
        # A formula is calculated as soon as its project opens, also a project from someone else:
        # it may use the table, but no files, network, extensions or settings.
        con = duckdb.connect(":memory:", config={**_SAFE, "temp_directory": _temp_folder()})
        for macro in _MACROS:
            con.execute(macro)
        con.execute("SET lock_configuration = true")
        with _connections_lock:
            _connections[ident] = con
    return con


def _query(con: duckdb.DuckDBPyConnection, sql: str, formula: str) -> duckdb.DuckDBPyRelation:
    """``sql``, which holds ``formula``, once DuckDB itself has confirmed how it is read: one
    SELECT, with its texts and names where ``tokenize`` found them, that doesn't read a blocked
    table. The checks on the formula rest on that reading. Only the statement that was checked
    runs."""
    try:
        sql.encode()
        statements = con.extract_statements(sql)
    except UnicodeError:  # half of a character, in a project file or in DuckDB's message on U&'…'
        raise ActionError("Formula error: the formula holds a broken character") from None
    if len(statements) > 1 and "pivot" in _fold(formula):  # without its values, DuckDB makes two statements of it
        raise ActionError(f"Formula error: a PIVOT in a formula needs its values: ON [Column] IN (...)\nFormula: {formula}")
    if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT or not _same_reading(sql):
        raise ActionError(f"Formula error: DuckDB doesn't read this as one expression, so it isn't calculated\nFormula: {formula}")
    if _reads_blocked_table(con, sql):
        raise ActionError(f"Formula error: {_SANDBOX}\nFormula: {formula}")
    return con.sql(statements[0])


def _reads_blocked_table(con: duckdb.DuckDBPyConnection, sql: str) -> bool:
    """Whether ``sql`` reads one of _BLOCKED_TABLES, by DuckDB's own parse tree: a table can follow
    many more words than FROM and JOIN, and its name can be a text such as E'pg\\x5fsettings'.
    Without a tree the answer is yes."""
    tree = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    if tree["error"]:
        return True
    nodes = [tree]
    while nodes:  # not by recursion: a formula can nest deeper than Python goes
        node = nodes.pop()
        if isinstance(node, list):
            nodes += node
        elif isinstance(node, dict):
            nodes += node.values()
            name = ""
            if node.get("type") == "BASE_TABLE":
                name = node["table_name"]
            elif node.get("type") == "TABLE_FUNCTION":
                name = node["function"].get("function_name", "")
            if _fold(name) in _BLOCKED_TABLES:
                return True
    return False


def _same_reading(sql: str) -> bool:
    """Whether DuckDB's own tokens are those of ``tokenize``. It may only split an operator (+-
    is two operators for it), and a joined text is part of the text before it. It counts in
    bytes."""
    places = list(itertools.accumulate((len(ch.encode()) for ch in sql), initial=0))
    theirs = duckdb.tokenize(sql)
    index = 0
    for token in tokenize(sql):
        if token.kind in ("space", "comment") or token.joined:
            continue
        if index == len(theirs) or theirs[index][0] != places[token.start]:
            return False
        if (theirs[index][1] == duckdb.token_type.string_const) != (token.kind == "string"):
            return False
        index += 1
        while token.kind == "operator" and index < len(theirs) and theirs[index][0] < places[token.end]:
            index += 1
    return index == len(theirs)


def _readable(frame: pl.DataFrame) -> tuple[pl.DataFrame, str]:
    """``frame`` in types that Arrow, which hands it over to DuckDB, has, and what to select from it
    for the columns in their own types: 128-bit whole numbers go as text and become HUGEINT and
    UHUGEINT again."""
    wide = {
        name: "HUGEINT" if dtype == pl.Int128 else "UHUGEINT"
        for name, dtype in frame.schema.items()
        if dtype in (pl.Int128, pl.UInt128)
    }
    if not wide:
        return frame, "*"
    quoted = {name: '"' + name.replace('"', '""') + '"' for name in wide}
    casts = ", ".join(f"CAST({quoted[name]} AS {kind}) AS {quoted[name]}" for name, kind in wide.items())
    return frame.with_columns(frame[name].cast(pl.String) for name in wide), f"* REPLACE ({casts})"


def _register(con: duckdb.DuckDBPyConnection, frame: pl.DataFrame, name: str) -> None:
    """Make ``frame`` the view ``name``. An interrupted formula can leave a view or table of that
    name behind (and unregister then doesn't remove it): it is dropped, so that formulas on this
    thread keep working."""
    try:
        con.register(name, frame)
    except duckdb.CatalogException:
        _clear(con, name)
        con.register(name, frame)


def _clear(con: duckdb.DuckDBPyConnection, name: str) -> None:
    """Drop the view or the table ``name``, if there is one."""
    for kind in ("VIEW", "TABLE"):
        try:
            con.execute(f'DROP {kind} IF EXISTS "{name}"')
        except duckdb.CatalogException:  # the name is the other kind
            pass


def _temp_folder() -> str:
    """A folder of its own for each connection, for data that doesn't fit in memory. DuckDB lets
    a formula read its temp folder, which would otherwise be ".tmp" in the working directory."""
    global _temp
    with _temp_lock:
        if _temp is None:
            _temp = _new_scratch_folder()  # deleted at the end, or by the next process if this one is ended
            atexit.register(shutil.rmtree, _temp, ignore_errors=True)
        return tempfile.mkdtemp(dir=_temp)


def _check_calls(formula: str) -> None:
    """Refuse calls of DuckDB's settings and catalog functions, also written as "name"(...)."""
    tokens = tokenize(formula)
    for index, token in enumerate(tokens):
        if token.kind == "word":
            name = _fold(formula[token.start : token.end])
        elif token.kind in ("quoted", "column") and token.closed:
            name = _fold(_name(formula, token))
        else:
            continue
        if _is_call(tokens, index) and (name in _BLOCKED or name.startswith(_BLOCKED_PREFIXES)):
            raise ActionError(f"Formula error: {_SANDBOX}\nFormula: {formula}")


def _closing(text: str, start: int, close: str, *, escapes: bool = False, doubled: bool = True) -> tuple[int, bool]:
    """Where a text, name or column that starts before ``start`` ends. A doubled closing
    character (with ``doubled``) stands for the character itself; with ``escapes``, so does a
    character after a backslash."""
    i = start
    while i < len(text):
        if escapes and text[i] == "\\":
            i += 2
            continue
        if text[i] == close:
            if doubled and i + 1 < len(text) and text[i + 1] == close:
                i += 2
                continue
            return i + 1, True
        i += 1
    return len(text), False


def _comment_end(text: str, start: int) -> tuple[int, bool]:
    """The end of a /* comment */ starting at ``start``; comments can be nested, as in DuckDB."""
    depth, i = 0, start
    while i < len(text):
        if text.startswith("/*", i):
            depth, i = depth + 1, i + 2
        elif text.startswith("*/", i):
            depth, i = depth - 1, i + 2
            if depth == 0:
                return i, True
        else:
            i += 1
    return len(text), False


def _name(formula: str, token: Token) -> str:
    inner = formula[token.start + 1 : token.end - 1]
    return inner.replace("]]", "]") if token.kind == "column" else inner.replace('""', '"')


def _next(tokens: list[Token], index: int) -> Token | None:
    """The next token after ``index`` that isn't a space or comment."""
    following = _next_index(tokens, index)
    return tokens[following] if following is not None else None


def _next_index(tokens: list[Token], index: int | None) -> int | None:
    """The index of the next token after ``index`` that isn't a space or comment."""
    if index is None:
        return None
    for j in range(index + 1, len(tokens)):
        if tokens[j].kind not in ("space", "comment"):
            return j
    return None


def _previous(tokens: list[Token], index: int) -> int | None:
    for j in range(index - 1, -1, -1):
        if tokens[j].kind not in ("space", "comment"):
            return j
    return None


def _is_call(tokens: list[Token], index: int) -> bool:
    following = _next(tokens, index)
    return following is not None and following.kind == "open"


def _unclosed(formula: str, tokens: list[Token]) -> str | None:
    """What isn't closed: a text, a column name, a comment or a parenthesis."""
    for token in tokens:
        if token.closed:
            continue
        if token.kind == "column":
            return "a ] is missing (columns are written as [Name])"
        if token.kind == "comment":
            return "a */ is missing (comments are written as /* ... */)"
        if token.kind in ("string", "quoted"):
            dollar = _DOLLAR.match(formula, token.start)
            return f"a closing {dollar.group() if dollar else _CLOSING[token.kind]} is missing"
    depth = 0
    for token in tokens:
        depth += {"open": 1, "close": -1}.get(token.kind, 0)
        if depth < 0:
            return "there is one ) too many"
    return "a ) is missing" if depth > 0 else None


def _fold(name: str) -> str:
    """Names the way DuckDB compares them: it ignores the case of A-Z only (Ä and ä differ)."""
    return name.translate(_ASCII_LOWER)


def _check_names(formula: str, columns: Sequence[str]) -> None:
    """DuckDB ignores upper and lower case in names: columns that differ only in that can't be
    told apart (it would silently use the first one)."""
    same: dict[str, list[str]] = {}
    for column in columns:
        same.setdefault(_fold(column), []).append(column)
    if all(len(group) == 1 for group in same.values()):
        return
    tokens = tokenize(formula)
    names = column_names(formula)
    names += [formula[t.start : t.end] for i, t in enumerate(tokens) if t.kind == "word" and not _is_call(tokens, i)]
    for name in names:
        group = same.get(_fold(name), [])
        if len(group) > 1:
            listed = " and ".join(f"[{c}]" for c in group)
            raise ActionError(
                f"Formula error: the columns {listed} differ only in upper and lower case, so a formula "
                f"can't tell them apart. Rename one of them first.\nFormula: {formula}"
            )


def _unusable_result(result_type: str) -> str | None:
    """Results a column of a table can't hold (they would break the grid and exports)."""
    if "INTERVAL" in result_type:  # Polars can't take DuckDB's time spans
        return (
            "the result is a time span. For the days between two dates or date-times use "
            "DATE_DIFF('day', [From], [To]) (or 'hour', 'minute', ...); to move a date, add an interval: "
            "[Date] + INTERVAL 3 DAY."
        )
    if re.search(r"\[\d*\]$", result_type):
        return (
            "the result is a list of values. Take one of them with LIST_EXTRACT(list, 1), "
            "or join them with ARRAY_TO_STRING(list, ', ')."
        )
    if result_type.startswith(("STRUCT", "MAP", "UNION")):
        return "the result is a structure of several values. Take one of them, e.g. STRUCT_EXTRACT(value, 'name')."
    return None


def _not_a_condition(gives: str) -> str:
    return f"The condition must be true or false for every row, e.g. [Amount] > 100 (this formula gives {gives})"


def _type_word(duckdb_type: str) -> str:
    """A DuckDB type in the words the grid uses."""
    kind = duckdb_type.upper()
    if kind.startswith("VARCHAR"):
        return "text"
    if kind in ("TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT", "UHUGEINT"):
        return "an integer"
    if kind in ("FLOAT", "DOUBLE") or kind.startswith("DECIMAL"):
        return "a number"
    if kind == "DATE":
        return "a date"
    if kind.startswith("TIMESTAMP"):
        return "a date-time"
    if kind.startswith("TIME"):
        return "a time"
    return kind.lower()


def _first_line(error: BaseException) -> str:
    return (str(error).strip().splitlines() or [type(error).__name__])[0]


def _clean_error(error: Exception, formula: str, columns: Sequence[str] = ()) -> str:
    text = str(error)
    # An aggregate such as SUM([x]) without OVER (...); a formula has no GROUP BY of its own, unless
    # in a query on __input
    if "must appear in the GROUP BY clause" in text and not re.search(r"\bgroup\s+by\b", formula, re.IGNORECASE):
        return (
            "Formula error: the formula must give one value per row. For totals use the Aggregate "
            f"action, or a window function such as SUM([Amount]) OVER ().\nFormula: {formula}"
        )
    line = re.search(r"\nLINE (\d+):", text)
    closing = len(re.findall(r"\r\n|\r|\n", f"SELECT (\n{formula}\n")) + 1  # the line of the ) after the formula
    if "syntax error" in text and line and int(line.group(1)) >= closing:
        return (
            "Formula error: the formula ends too early: something is missing at its end "
            f"(e.g. a value after + or AND, or END after CASE)\nFormula: {formula}"
        )
    if isinstance(error, duckdb.PermissionException):
        return f"Formula error: {_SANDBOX}\nFormula: {formula}"
    text = re.split(r"\n\s*\nLINE \d+:|\nLINE \d+:", text)[0]
    text = text.split("Candidate functions:")[0]  # DuckDB lists dozens of variants of the function
    text = text.replace(f'"{_VIEW}".', "").replace(f"{_VIEW}.", "")
    text = re.sub(r"\s+", " ", text).strip()
    text = re.sub(r"^[A-Za-z ]+? Error: ", "", text)
    missing = re.search(r'Referenced column "(.+?)" not found in FROM clause!', text)
    if missing:  # the most common mistake: say it plainly, without DuckDB's internals
        return f"Formula error: {_missing_column(missing.group(1), formula, columns)}\nFormula: {formula}"
    if re.search(r"Function with name \*/ does not exist", text):
        return f"Formula error: there is a */ without a /* before it\nFormula: {formula}"
    function = re.search(r'(?:\w+ )?Function with name "?(\w+)"? does not exist!(?: Did you mean "(\w+)"\?)?', text)
    if function:
        return f"Formula error: {_missing_function(function.group(1), function.group(2), formula)}\nFormula: {formula}"
    extension = re.search(r'(?:\w+ )?Function with name "?(\w+)"? is not in the catalog', text)
    if extension:  # it exists in an extension of DuckDB, which formulas can't load
        return f"Formula error: {_missing_function(extension.group(1), None, formula)}\nFormula: {formula}"
    kind = re.search(r'Type with name "?([\w.]+)"? (?:does not exist|is not in the catalog)', text)
    if kind:
        return f"Formula error: there is no type {kind.group(1)} (e.g. VARCHAR, BIGINT, DOUBLE, DATE, TIMESTAMP)\nFormula: {formula}"
    too_big = re.search(r"Type UINT128 with value (\d+) can't be cast because the value is out of range for the destination type INT128", text)
    if too_big:  # a result of UHUGEINT goes as HUGEINT (see evaluate)
        return (
            f"Formula error: the whole number {int(too_big.group(1)):,} is too big (at most {2**127 - 1:,}). For a decimal "
            f"number write CAST(... AS DOUBLE)\nFormula: {formula}"
        )
    # e.g. a result beyond Int64 (an exact decimal) times itself: DuckDB's hint, a cast to a decimal
    # with a smaller scale, doesn't help a whole number
    if re.search(r"Overflow in (\w+ of (DECIMAL\(\d+\)|INT128)|HUGEINT \w+)", text):
        return (
            "Formula error: the result has more than 38 digits, more than an exact number holds. For a decimal "
            f"number (about 15 digits) write CAST(... AS DOUBLE)\nFormula: {formula}"
        )
    if "+(" in text and "VARCHAR" in text:
        text += " — to join text use || or CONCAT()"
    return f"Formula error: {text}\nFormula: {formula}"


def _missing_column(name: str, formula: str, columns: Sequence[str]) -> str:
    if name not in column_names(formula):  # written without brackets: maybe not meant as a column
        tokens = tokenize(formula)
        for index, token in enumerate(tokens):
            if token.kind != "word" or _fold(formula[token.start : token.end]) != _fold(name):
                continue
            part = _DATE_PARTS.get(name.lower())
            call = _date_function(formula, tokens, index)
            if part and call:
                return f"{name} is not a column: write the date part in quotes, {call}('{part}', ...)"
            if name.lower() in _NO_ARGUMENTS:
                return f"{name} is a function: write {name.upper()}()"
    folded = {_fold(c): c for c in columns}
    cutoff = 0.75 if len(name) <= 5 else 0.6  # short names are similar to too many others
    close = difflib.get_close_matches(_fold(name), list(folded), n=1, cutoff=cutoff)
    return f"column [{name}] not found" + (f" (did you mean [{folded[close[0]]}]?)" if close else "")


def _date_function(formula: str, tokens: list[Token], index: int) -> str | None:
    """The date function (as typed) if the word at ``index`` is its first argument."""
    before = _previous(tokens, index)
    if before is None or tokens[before].kind != "open":
        return None
    function = _previous(tokens, before)
    if function is None or tokens[function].kind != "word":
        return None
    typed = formula[tokens[function].start : tokens[function].end]
    return typed if typed.lower() in _DATE_FUNCTIONS else None


def _missing_function(name: str, suggestion: str | None, formula: str) -> str:
    typed = next(
        (formula[t.start : t.end] for t in tokenize(formula) if t.kind == "word" and formula[t.start : t.end].lower() == name.lower()),
        name.upper(),
    )
    if name.lower() in _TSQL_HINTS:
        return f"there is no function {typed}: {_TSQL_HINTS[name.lower()]}"
    needed = 0.8 if len(name) <= 5 else 0.7  # a wrong suggestion is worse than none
    if suggestion and difflib.SequenceMatcher(None, name.lower(), suggestion.lower()).ratio() >= needed:
        return f"there is no function {typed} (did you mean {suggestion.upper()}?)"
    return f"there is no function {typed}"

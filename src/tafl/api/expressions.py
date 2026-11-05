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
import contextvars
import datetime as dt
import difflib
import itertools
import json
import re
import shutil
import string
import tempfile
import threading
from collections.abc import Collection, Sequence
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
_HUGEINT_MAX = 2**127 - 1
_TOO_LONG = (
    "the result needs more than 38 digits, decimal places included, more than an exact number holds. "
    "For a decimal number (about 15 digits) write CAST(... AS DOUBLE)"
)
# DuckDB's message when a value doesn't fit into the exact decimal it brings two of them to
_DECIMAL_CAST = re.compile(r"(?:Casting value .*? to type|Could not cast value .*? to) DECIMAL\((\d+),\s*\d+\)")
# DuckDB's messages when a function or an operator gets values of a type it doesn't take (see _text_hint)
_CALL = re.compile(
    r"No function matches the given name and argument types '([^\n]+)'\. You might need"
    r"|Could not choose a best candidate function for the function call \"([^\n]+)\"\. In order"
)
_COMPARE = re.compile(r"Cannot compare values of type ([^\n]+?) and type ([^\n]+?) - an explicit cast is required")
_MIX = re.compile(r"Cannot mix values of type ([^\n]+?) and ([^\n]+?) in [^\n]+ - an explicit cast is required")
_TYPED_LITERALS = {"date", "timestamp", "timestamptz", "time", "timestamp_ns", "time_ns", "interval"}
_DATES_NOW = {"today", "now", "getdate", "current_date", "current_timestamp", "current_localtimestamp"}
_TEMPORAL = re.compile(r"(?:DATE|TIME|TIMESTAMP|INTERVAL)\b|TIME_NS|TIMESTAMP_")
_NUMERIC = re.compile(r"(?:U?(?:TINYINT|SMALLINT|INTEGER|BIGINT|HUGEINT)|FLOAT|DOUBLE|DECIMAL|BIGNUM)\b")
_ARITHMETIC = {"+", "-", "*", "/", "//", "%", "**", "^"}
_ALIASES = {"date_part": ("datepart", "extract"), "date_diff": ("datediff",), "date_trunc": ("datetrunc",), "date_sub": ("datesub",)}
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


# Within one evaluate, the tokens of each formula it splits (see tokenize)
_split: contextvars.ContextVar[dict[str, tuple[Token, ...]] | None] = contextvars.ContextVar("_split", default=None)


def tokenize(formula: str) -> list[Token]:
    """Split a formula into tokens, as DuckDB reads what ``translate`` makes of it (in which a
    comment is spaces). It never fails: what isn't closed runs to the end. Within one evaluate a
    formula is split once: its checks look at it about ten times, and the step of a filter of
    20,000 texts takes 0.13 seconds each time."""
    split = _split.get()
    if split is None:
        return _tokenize(formula)
    if (tokens := split.get(formula)) is None:
        tokens = split[formula] = tuple(_tokenize(formula))
    return list(tokens)


def _tokenize(formula: str) -> list[Token]:
    """``tokenize`` without keeping the tokens."""
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
    scope = _split.set({})
    try:
        return _evaluate(df, formula, condition=condition)
    finally:
        _split.reset(scope)


def _evaluate(df: pl.DataFrame, formula: str, *, condition: bool) -> pl.Series:
    """``evaluate``, in which each formula is split into tokens once."""
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
    exact_times = "time_ns" in {_fold(formula[t.start : t.end]) for t in tokenize(formula) if t.kind == "word"}
    frame, kinds, beyond, unsigned = _readable(df if table else _with_row_number(df, formula), exact_times=exact_times)
    tried: set[str] = set()
    while True:
        try:
            values = _calculate(con, frame, kinds, beyond, body, formula, table=table, condition=condition)
            break
        except (duckdb.Error, _TooLong) as e:
            wider = _wider(e, frame, kinds, unsigned, tried)
            if wider is None:
                if isinstance(e, _TooLong):
                    raise ActionError(f"Formula error: {_TOO_LONG}\nFormula: {formula}") from None
                raise ActionError(_clean_error(e, formula, df.columns, _texts(df), _nanosecond_times(df))) from None
            kinds = wider
    if values.len() != df.height:
        raise ActionError(
            "The formula must give one value per row. For totals use the Aggregate action, "
            "or a window function such as SUM([Amount]) OVER ()."
        )
    return normalize(values)


def _calculate(
    con: duckdb.DuckDBPyConnection,
    frame: pl.DataFrame,
    kinds: dict[str, str],
    beyond: dict[str, int],
    body: str,
    formula: str,
    *,
    table: bool,
    condition: bool,
) -> pl.Series:
    """The values of the formula (``body`` as translated) on ``frame``, its columns in the types of
    ``kinds``. DuckDB's errors, and _TooLong, are passed on: the caller may try again (see _wider)."""
    order = "rowid" if table else _ROW
    columns = _selected(frame.columns, kinds)
    view = not table and columns != "*"  # a view that gives columns their own types back
    # On lines of its own: an error on the line after the formula means it ends too early
    sql = f"SELECT (\n{body}\n) AS value FROM {_VIEW} ORDER BY {order}"
    try:
        if table or view:
            _register(con, frame, _FRAME)
            if beyond:
                _check_unsigned(con, sql, formula, frame.columns, kinds, beyond, table=table)
            _make_input(con, columns, table=table)
        else:
            _register(con, frame, _VIEW)
        relation = _query(con, sql, formula)
        result_type = str(relation.types[0])
        if result_type.startswith("TIMESTAMP WITH TIME ZONE"):  # local time: the grid shows no zones
            relation = _query(con, f"SELECT CAST((\n{body}\n) AS TIMESTAMP) AS value FROM {_VIEW} ORDER BY {order}", formula)
            result_type = "TIMESTAMP"
        elif result_type == "UHUGEINT":  # Arrow would take it for a signed number: from 2^127 on, a negative one
            relation = _query(con, f"SELECT CAST((\n{body}\n) AS HUGEINT) AS value FROM {_VIEW} ORDER BY {order}", formula)
            result_type = "HUGEINT"
        problem = _unusable_result(result_type)
        if problem:
            raise ActionError(f"Formula error: {problem}\nFormula: {formula}")
        if condition and result_type != "BOOLEAN":
            raise ActionError(_not_a_condition(_type_word(result_type)))
        try:
            result = relation.pl(_PART_ROWS)
        except duckdb.Error:
            raise
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
    values = result.to_series(0)
    if result_type.startswith("DECIMAL(") and _beyond_its_digits(values, result_type):
        raise _TooLong
    return values


class _TooLong(Exception):
    """A result of exact decimals with more digits than its type (DuckDB's SUM doesn't check them)."""


def _wider(
    error: Exception, frame: pl.DataFrame, kinds: dict[str, str], unsigned: Sequence[str], tried: set[str]
) -> dict[str, str] | None:
    """The types to try a formula again with after ``error``, once each; None when there are none.
    Exact decimals that DuckDB brings to one type, or that its SUM makes too long: each column as
    DECIMAL(38, the decimal places its values use), as long as they fit (the text of such a decimal
    loses its last zeros, 1.50 becomes 1.5: only then). Unsigned 64-bit numbers below 2^63, which go
    as BIGINT: as HUGEINT after an overflow, as an unsigned number held them."""
    if (isinstance(error, _TooLong) or _DECIMAL_CAST.search(str(error))) and "decimals" not in tried:
        tried.add("decimals")
        return _decimal_kinds(frame, kinds)
    if unsigned and "unsigned" not in tried and re.search(r"Overflow in \w+ of INT64", str(error)):
        tried.add("unsigned")
        return {**kinds, **dict.fromkeys(unsigned, "HUGEINT")}
    return None


def _texts(df: pl.DataFrame) -> list[str]:
    """The columns of text: _clean_error names them in a hint."""
    return [name for name, dtype in df.schema.items() if dtype == pl.String]


def _has_nanoseconds(times: pl.Series) -> bool:
    """Whether a time of day has digits below a microsecond."""
    return bool(((times.dt.nanosecond() % 1000) != 0).any())


def _nanosecond_times(df: pl.DataFrame) -> list[str]:
    """The columns of times of day with nanoseconds: _clean_error names them in a hint."""
    return [name for name, dtype in df.schema.items() if dtype == pl.Time and _has_nanoseconds(df[name].alias("value"))]


def _make_input(con: duckdb.DuckDBPyConnection, columns: str, *, table: bool, empty: bool = False) -> None:
    """The view (or the table) that a formula reads: ``columns`` of the frame, without rows if ``empty``."""
    _clear(con, _VIEW)
    rows = " LIMIT 0" if empty else ""
    con.execute(f'CREATE TEMP {"TABLE" if table else "VIEW"} "{_VIEW}" AS SELECT {columns} FROM "{_FRAME}"{rows}')


def _check_unsigned(
    con: duckdb.DuckDBPyConnection,
    sql: str,
    formula: str,
    names: Sequence[str],
    kinds: dict[str, str],
    beyond: dict[str, int],
    *,
    table: bool,
) -> None:
    """A column of unsigned 128-bit numbers beyond 2^127 - 1 stays UHUGEINT, which DuckDB mixes with
    signed numbers as decimal numbers (also to compare them: 2^127 + 5 = 2^127 - 1), and whose -x
    and a - x can be a big positive number: such a formula stops. Other calculations with them work
    as before: a result beyond 2^127 - 1 stops (see evaluate)."""
    tree = json.loads(con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0])
    for name, highest in beyond.items():
        too_big = (
            f"Formula error: the whole number {highest:,} in [{name}] is too big (at most {_HUGEINT_MAX:,}). "
            f"For a decimal number write CAST(... AS DOUBLE)\nFormula: {formula}"
        )
        if tree["error"] or _negates(tree, name) or _compared_with_others(tree, name, beyond):
            raise ActionError(too_big)
        try:
            _make_input(con, _selected(names, {**kinds, name: "HUGEINT"}), table=table, empty=True)
            signed = str(_query(con, sql, formula).types[0])
        except duckdb.Error:  # it doesn't work with signed numbers: what it does with these is DuckDB's to say
            continue
        _make_input(con, _selected(names, kinds), table=table, empty=True)
        unsigned = str(_query(con, sql, formula).types[0])
        if unsigned != signed and (unsigned in ("DOUBLE", "FLOAT") or unsigned.startswith("DECIMAL")):
            raise ActionError(too_big)


def _negates(tree: object, name: str) -> bool:
    """Whether DuckDB's parse tree of a formula has a -x whose x uses the column ``name`` (or all
    columns, as COLUMNS(*) does), or an a - x: DuckDB checks the subtraction of unsigned 128-bit
    numbers only for a small x (3 - (2^128 - 1) gives 4). Also written subtract(a, x) or "-"(x)."""
    nodes = [tree]
    while nodes:  # not by recursion: a formula can nest deeper than Python goes
        node = nodes.pop()
        if isinstance(node, list):
            nodes += node
        elif isinstance(node, dict):
            children = node.get("children")
            minus = node.get("class") == "FUNCTION" and _fold(str(node.get("function_name"))) in ("-", "subtract")
            if minus and isinstance(children, list) and children and _uses(children[-1], name):
                return True
            nodes += node.values()
    return False


def _compared_with_others(tree: object, name: str, alike: Collection[str]) -> bool:
    """Whether DuckDB's parse tree compares the column ``name`` with a column that isn't one of
    ``alike`` (=, <, BETWEEN, IN, ...)."""
    nodes = [tree]
    while nodes:
        node = nodes.pop()
        if isinstance(node, list):
            nodes += node
        elif isinstance(node, dict):
            sides: list[object] = []
            if node.get("class") == "COMPARISON":
                sides = [node.get("left"), node.get("right")]
            elif node.get("class") == "BETWEEN":
                sides = [node.get("input"), node.get("lower"), node.get("upper")]
            elif node.get("class") == "OPERATOR" and str(node.get("type")) in ("COMPARE_IN", "COMPARE_NOT_IN"):
                sides = list(node.get("children") or [])
            elif node.get("class") == "SUBQUERY" and node.get("child"):  # [v] IN (SELECT ...), [v] = ANY (...)
                sides = [node.get("child"), node.get("subquery")]
            for index, side in enumerate(sides):
                if _uses(side, name) and any(_uses(other, None, alike) for other in sides[:index] + sides[index + 1 :]):
                    return True
            nodes += node.values()
    return False


def _uses(tree: object, name: str | None, alike: Collection[str] = ()) -> bool:
    """Whether a part of DuckDB's parse tree uses the column ``name``, or with ``name`` None, a
    column that isn't one of ``alike`` (all columns, as COLUMNS(*) does, count as both)."""
    folded = {_fold(other) for other in alike}
    nodes = [tree]
    while nodes:
        node = nodes.pop()
        if isinstance(node, list):
            nodes += node
        elif isinstance(node, dict):
            names = node.get("column_names")
            if node.get("class") == "COLUMN_REF" and isinstance(names, list) and names:
                column = _fold(str(names[-1]))
                if (column == _fold(name)) if name is not None else (column not in folded):
                    return True
            if node.get("class") == "STAR":
                return True
            nodes += node.values()
    return False


def _beyond_its_digits(values: pl.Series, result_type: str) -> bool:
    """Whether an exact decimal has more digits than its type: DuckDB's SUM adds them up as 128-bit
    numbers without checking, so 6×10^37 twice gives 39 digits."""
    if not isinstance(values.dtype, pl.Decimal):
        return False
    digits = int(re.match(r"DECIMAL\((\d+)", result_type).group(1))
    units = values.alias("value").to_physical()  # Polars' Series methods would read a name like ^a.*$ as a pattern
    high, low = (pl.Series([limit], dtype=pl.Int128) for limit in (10**digits, -(10**digits)))
    return bool(((units >= high) | (units <= low)).any())


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


def moment(local: dt.datetime) -> pl.Series:
    """A date-time of this PC's clock (NOW() and date-time parameters give such values) as the
    moment it is, read as formulas read it: a Series of one date-time in UTC. Of a time that a
    clock change repeats it is the later one; a time that the change skips is moved on by it."""
    try:
        micros = _connection().execute("SELECT epoch_us(CAST(? AS TIMESTAMPTZ))", [local]).fetchone()[0]
    except duckdb.Error as e:  # e.g. stopped
        raise ActionError(_first_line(e)) from None
    return pl.Series([micros], dtype=pl.Int64).cast(pl.Datetime("us", "UTC"))


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


def _readable(
    frame: pl.DataFrame, *, exact_times: bool = False
) -> tuple[pl.DataFrame, dict[str, str], dict[str, int], list[str]]:
    """``frame`` in types that Arrow, which hands it over to DuckDB, has; the types that formulas
    calculate with for the columns that need another one than DuckDB gives them (see _selected);
    the unsigned 128-bit columns with numbers beyond 2^127 - 1, with their highest number; and the
    unsigned 64-bit columns that go as BIGINT (see _wider).

    128-bit whole numbers go as text and become HUGEINT and UHUGEINT again. Unsigned whole numbers
    are calculated as signed ones: DuckDB turns -x of an unsigned number into a big positive one,
    and mixes UHUGEINT with signed numbers as a decimal number. They become Int64 (no cast in
    DuckDB: fast with thousands of columns); unsigned 64-bit ones all become HUGEINT if one of them
    doesn't fit (a list of thousands of BIGINT and HUGEINT columns takes DuckDB gigabytes). Only an
    unsigned 128-bit column with a number beyond HUGEINT stays UHUGEINT (see _check_unsigned).

    ``exact_times``: the formula has TIME_NS, which DuckDB compares with a TIME to the
    microsecond (08:00:00 = TIME_NS '08:00:00.000000001'): the times of day stay TIME_NS."""
    kinds: dict[str, str] = {}
    beyond: dict[str, int] = {}
    changed: list[pl.Series] = []
    unsigned = [name for name, dtype in frame.schema.items() if dtype == pl.UInt64 and name != _ROW]
    wide = any((frame[name].alias("value") > 2**63 - 1).any() for name in unsigned)
    nanoseconds = any(_has_nanoseconds(frame[name].alias("value")) for name, dtype in frame.schema.items() if dtype == pl.Time)
    for name, dtype in frame.schema.items():
        column = frame[name].alias("value")  # Polars' Series methods would read a name like ^a.*$ as a pattern
        if dtype == pl.UInt128:
            if (column > _HUGEINT_MAX).any():
                beyond[name] = column.max()
            kinds[name] = "UHUGEINT" if name in beyond else "HUGEINT"
            changed.append(column.cast(pl.String).alias(name))
        elif dtype == pl.Int128:
            kinds[name] = "HUGEINT"
            changed.append(column.cast(pl.String).alias(name))
        elif dtype in (pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64) and name != _ROW:  # the row number only keeps the order
            if dtype == pl.UInt64 and wide:
                kinds[name] = "HUGEINT"
            else:
                changed.append(column.cast(pl.Int64).alias(name))
        elif dtype == pl.Time and not nanoseconds and not exact_times:
            # Polars' times are TIME_NS for DuckDB, which compares them with a TIME only with = and
            # IN, and adds no INTERVAL to them; TIME holds them when none has nanoseconds (all of the
            # columns, as TIME and TIME_NS can't be compared either)
            kinds[name] = "TIME"
    as_bigint = [] if wide else unsigned
    return (frame.with_columns(changed) if changed else frame), kinds, beyond, as_bigint


def _selected(columns: Sequence[str], kinds: dict[str, str]) -> str:
    """What to select from the frame that DuckDB gets: its ``columns``, those of ``kinds`` in these
    types. Each column is named: DuckDB takes long for a * REPLACE (...) of thousands of them."""
    if not kinds:
        return "*"
    quoted = ['"' + name.replace('"', '""') + '"' for name in columns]
    return ", ".join(f"CAST({q} AS {kinds[name]}) AS {q}" if name in kinds else q for name, q in zip(columns, quoted, strict=True))


def _decimal_kinds(frame: pl.DataFrame, kinds: dict[str, str]) -> dict[str, str] | None:
    """``kinds`` with each column of exact decimals as DECIMAL(38, the decimal places its values use,
    at least one of those it has), or None when none of them changes. DuckDB brings two exact
    decimals to one type with the decimal places of both and the digits of their types:
    12345678901234567890123 and a number with 18 decimal places don't fit into its 38 digits,
    1.500000000000000000 needs only one of them; DECIMAL(18,2) and DECIMAL(18,4) need more than 18."""
    wider = dict(kinds)
    for name, dtype in frame.schema.items():
        if isinstance(dtype, pl.Decimal):
            places = _decimal_places(frame[name].alias("value")) if dtype.scale else 0
            if (dtype.precision, dtype.scale) != (38, places):
                wider[name] = f"DECIMAL(38, {places})"
    return wider if wider != kinds else None


def _decimal_places(values: pl.Series) -> int:
    """The decimal places that exact decimals use, at least one: 1.50 and 2.25 use 2, 1.500 one."""
    scale = values.dtype.scale
    units = values.to_physical()  # whole numbers of the last decimal place

    def enough(places: int) -> bool:
        return bool((units % pl.Series([10 ** (scale - places)], dtype=pl.Int128) == 0).all())

    low, high = 1, scale
    while low < high:  # if some decimal places are enough, so are more
        middle = (low + high) // 2
        if enough(middle):
            high = middle
        else:
            low = middle + 1
    return low


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


def _clean_error(
    error: Exception, formula: str, columns: Sequence[str] = (), texts: Sequence[str] = (), nanos: Sequence[str] = ()
) -> str:
    """DuckDB's message in plain words. ``texts``: the columns of text, for the hint when a function
    gets text where it needs a date or a number; ``nanos``: the times of day with nanoseconds."""
    text = raw = str(error)
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
    # with a smaller scale, doesn't help a whole number. The decimal places count: 12.5 * 10 of two
    # DECIMAL(38,18) needs 36 of them. Not for DECIMAL(18): there DuckDB's hint, a bigger decimal, helps
    if re.search(r"Overflow in (\w+ of (DECIMAL\(38\)|INT128)|HUGEINT \w+)", text):
        return f"Formula error: {_TOO_LONG}\nFormula: {formula}"
    cast = _DECIMAL_CAST.search(text)
    if cast and cast.group(1) == "38":  # two exact decimals whose values need more than 38 digits together
        return f"Formula error: {_TOO_LONG}\nFormula: {formula}"
    hint = _nanosecond_hint(formula, nanos) if "TIME_NS" in text else _text_hint(text, raw, formula, texts)
    if hint:
        return f"Formula error: {hint}\nFormula: {formula}"
    return f"Formula error: {text}\nFormula: {formula}"


def _text_hint(text: str, raw: str, formula: str, texts: Sequence[str]) -> str | None:
    """For a function or an operator that gets text where it needs a date or a number: which
    columns hold text, and what to do. ``raw``: DuckDB's whole message, with the variants of the
    function it lists."""
    compare = _COMPARE.search(text) or _MIX.search(text)
    if compare:  # [Date] > DATE '2025-01-01', [Date] BETWEEN DATE ... AND ..., COALESCE([Date], DATE ...)
        kinds = [compare.group(1), compare.group(2)]
        if "VARCHAR" in kinds and any(_TEMPORAL.match(kind) for kind in kinds):
            return _needs("a date", None, _compared_texts(formula, texts) or _only_text(formula, texts))
        return None
    call = _CALL.search(text)
    if not call:
        return None
    signature = call.group(1) or call.group(2)
    name, _, arguments = signature.partition("(")
    kinds = _types(arguments.removesuffix(")"))
    if "VARCHAR" not in kinds:
        return None
    listed = raw.partition("Candidate functions:")[2]
    variants = [_types(found) for found in re.findall(r"^\s+\S+?\((.*)\) -> ", listed, re.M)]
    operator = name in _ARITHMETIC
    columns = _text_columns(formula, texts, name)
    if any(_TEMPORAL.match(kind) for kind in kinds if kind != "VARCHAR") or (
        variants and all(any(_TEMPORAL.match(kind) for kind in variant) for variant in variants)
    ):
        return _needs("a date", None if operator else _typed(formula, name), columns)
    if name == "+":
        return _needs("numbers to add", None, columns)
    if operator or (variants and all(any(_NUMERIC.match(kind) for kind in variant) for variant in variants)):
        return _needs("numbers", None if operator else _typed(formula, name), columns)
    return None


def _nanosecond_hint(formula: str, nanos: Sequence[str]) -> str:
    """For a time of day with nanoseconds (TIME_NS for DuckDB), which a formula can compare with a
    time only with = and IN, and can't add an INTERVAL to."""
    if not nanos:  # the times are TIME_NS for a formula with TIME_NS (see _readable)
        return (
            "a formula with TIME_NS gets the times of day as TIME_NS, which it compares with a TIME only by = and IN: "
            "write its times all as TIME_NS, such as TIME_NS '12:00:00'"
        )
    named = [name for name in _text_columns(formula, nanos, None)] or list(nanos)
    first = "[" + named[0].replace("]", "]]") + "]"
    subject = " and ".join("[" + name.replace("]", "]]") + "]" for name in named[:2])
    holds = "hold" if len(named) > 1 else "holds"
    return (
        f"{subject} {holds} times with nanoseconds, which a formula compares with a time only by = and IN: write "
        f"CAST({first} AS TIME), or a time as TIME_NS '12:00:00'"
    )


def _needs(what: str, function: str | None, columns: Sequence[str]) -> str:
    """The hint of _text_hint: ``function`` (as typed) needs ``what``, but ``columns`` hold text."""
    target = "DATE" if what == "a date" else "DOUBLE"
    if not columns:
        if what == "numbers to add":
            return "+ adds numbers, but gets text: to join text use || or CONCAT(); for numbers write CAST(... AS DOUBLE)"
        subject = f"{function} gets text" if function else "a text is used"
        return f"{subject} where {what} {'is' if what == 'a date' else 'are'} needed: write CAST(... AS {target})"
    names = ["[" + name.replace("]", "]]") + "]" for name in columns]  # as a formula writes them
    subject = names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]
    one = len(names) == 1
    holds, its = ("holds", "its") if one else ("hold", "their")
    kind = ("a date" if one else "dates") if what == "a date" else ("a number" if one else "numbers")
    change = f"change {its} type to {kind} before this step, or write CAST({names[0]} AS {target})"
    if what == "numbers to add":
        return f"{subject} {holds} text: to join text use || or CONCAT(); to add numbers, {change}"
    if function:
        return f"{function} needs {what}, but {subject} {holds} text: {change}"
    return f"{subject} {holds} text, not {what}: {change}"


def _compared_texts(formula: str, texts: Sequence[str]) -> list[str]:
    """The columns of text that the formula compares with a date: with a date or time written in it
    (DATE '2025-01-01'), with TODAY() and such, or with a column that holds no text."""
    tokens = tokenize(formula)
    by_name = {_fold(name): name for name in texts}

    def text_column(index: int | None) -> str | None:
        token = None if index is None else tokens[index]
        if token is not None and token.kind in ("column", "quoted") and token.closed:
            return by_name.get(_fold(_name(formula, token)))
        if token is not None and token.kind == "word" and not _is_call(tokens, index):
            return by_name.get(_fold(formula[token.start : token.end]))
        return None

    def a_date(index: int | None) -> bool:
        token = None if index is None else tokens[index]
        if token is None:
            return False
        text = _fold(formula[token.start : token.end])
        if token.kind == "string":  # the text of DATE '2025-01-01'
            before = _previous(tokens, index)
            return before is not None and _fold(formula[tokens[before].start : tokens[before].end]) in _TYPED_LITERALS
        if token.kind == "word" and text in _TYPED_LITERALS:
            following = _next(tokens, index)
            return following is not None and following.kind == "string"
        if token.kind == "word" and text in _DATES_NOW:
            return True
        if token.kind == "close":  # TODAY(): the word before its (
            opening = _matching_open(tokens, index)
            word = _previous(tokens, opening) if opening is not None else None
            return word is not None and _fold(formula[tokens[word].start : tokens[word].end]) in _DATES_NOW
        return token.kind in ("column", "quoted") and token.closed and _fold(_name(formula, token)) not in by_name

    found = []
    for index, token in enumerate(tokens):
        if token.kind == "operator" and formula[token.start : token.end] in ("=", "==", "<>", "!=", "<", ">", "<=", ">="):
            left, right = _previous(tokens, index), _next_index(tokens, index)
            found += [name for side, other in ((left, right), (right, left)) if (name := text_column(side)) and a_date(other)]
    return list(dict.fromkeys(found))


def _only_text(formula: str, texts: Sequence[str]) -> list[str]:
    """The column of text that the formula names, if it names only one."""
    named = _text_columns(formula, texts, None)
    return named if len(named) == 1 else []


def _matching_open(tokens: list[Token], index: int) -> int | None:
    """The ( of the ) at ``index``."""
    depth = 0
    for before in range(index, -1, -1):
        depth += {"close": 1, "open": -1}.get(tokens[before].kind, 0)
        if depth == 0:
            return before
    return None


def _typed(formula: str, function: str) -> str:
    """A function of DuckDB's message as the formula writes it (EXTRACT for date_part)."""
    names = {function, *_ALIASES.get(function, ())}
    tokens = tokenize(formula)
    for index, token in enumerate(tokens):
        if token.kind == "word" and _fold(formula[token.start : token.end]) in names and _is_call(tokens, index):
            return formula[token.start : token.end]
    return function.upper()


def _text_columns(formula: str, texts: Sequence[str], function: str | None) -> list[str]:
    """The columns of text that a function (DuckDB's name) or an operator gets in the formula; when
    there are none, the columns of text that the formula names."""
    tokens = tokenize(formula)
    by_name = {_fold(name): name for name in texts}  # DuckDB ignores the case of A-Z in names

    def column(index: int | None) -> str | None:
        if index is None:
            return None
        token = tokens[index]
        if token.kind in ("column", "quoted") and token.closed:
            return by_name.get(_fold(_name(formula, token)))
        if token.kind == "word" and not _is_call(tokens, index):
            following = _next(tokens, index)
            if following is None or following.kind != "string":  # not DATE '2025-01-01'
                return by_name.get(_fold(formula[token.start : token.end]))
        return None

    found: list[str | None] = []
    names = {function, *_ALIASES.get(function, ())} if function else set()
    for index, token in enumerate(tokens):
        text = formula[token.start : token.end]
        if token.kind == "operator" and text == function:
            found += [column(_previous(tokens, index)), column(_next_index(tokens, index))]
        elif token.kind == "word" and _fold(text) in names and _is_call(tokens, index):
            depth = 0
            for inner in range(_next_index(tokens, index) or index, len(tokens)):
                depth += {"open": 1, "close": -1}.get(tokens[inner].kind, 0)
                if depth == 0:
                    break
                if depth == 1:
                    found.append(column(inner))
    if not any(found):
        found = [column(index) for index in range(len(tokens))]
    return list(dict.fromkeys(name for name in found if name is not None))


def _types(arguments: str) -> list[str]:
    """The types of a list of them as DuckDB writes it: VARCHAR, DECIMAL(18,3), TIMESTAMP WITH TIME ZONE."""
    parts, depth, start = [], 0, 0
    for index, char in enumerate(arguments):
        if char in "([":
            depth += 1
        elif char in ")]":
            depth -= 1
        elif char == "," and depth == 0:
            parts.append(arguments[start:index].strip())
            start = index + 1
    if arguments[start:].strip():
        parts.append(arguments[start:].strip())
    return parts


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

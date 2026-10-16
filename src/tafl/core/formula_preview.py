"""What a formula gives on the first rows of a step's data, for the preview in the formula window."""

from __future__ import annotations

import re
import threading
import time
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Any

import polars as pl

from tafl.api import ActionError, Cancelled, expressions
from tafl.api.dtypes import type_label
from tafl.api.parameters import fill_formula, fill_text, formula_places

from .cancel import CancelEvent

SAMPLE_ROWS = 1000  # rows the formula is checked on
SHOWN_ROWS = 200  # rows the preview shows
NO_DATA = "No data to check with yet: the steps before this one have no result (an error, or still calculating)."
# Words DuckDB reserves (duckdb_keywords()), and operators: never a column without brackets.
_KEYWORDS = frozenset(
    "all analyse analyze and any array as asc asymmetric both case cast check collate column constraint "
    "create default deferrable desc describe distinct do else end except false fetch for foreign from "
    "group having in initially intersect into lambda lateral leading limit not null offset on only or "
    "order pivot pivot_longer pivot_wider placing primary qualify references returning select show some "
    "summarize symmetric table then to trailing true union unique unpivot using variadic when where "
    "window with is like ilike between".split()
)


@dataclass
class Column:
    name: str
    type: str  # the word the grid uses: text, integer, number, date, ...
    row: int | None = None  # made by this row of the same step (Calculate columns), not an input column
    written: str = ""  # the row's name as written, if a parameter is filled into it ("Sales {Year}")

    @property
    def label(self) -> str:
        """The name to write in a formula: as the row has it, so it follows the parameters."""
        return self.written or self.name


@dataclass
class Preview:
    ok: bool
    message: str  # one line for the status
    rows: pl.DataFrame | None = None  # the columns the formula uses, then the result
    columns: list[Column] = field(default_factory=list)  # what the formula can use
    values: pl.Series | None = None  # the result on the sample
    scope: str = ""  # "all 156 rows", "the first 1,000 rows"
    note: str = ""  # e.g. that window functions see only the sample
    mistake: tuple[int, int] | None = None  # where the error is: formula[start:end]
    fix: str | None = None  # what to write there instead ("did you mean")


@dataclass
class _Sample:
    """The first rows with the columns of the rows above added (kept for the next check)."""

    frame: pl.DataFrame  # the input it was made from
    earlier: tuple[tuple[str, str], ...]
    rows: int
    data: pl.DataFrame
    columns: list[Column]
    problem: str | None  # the first thing the step would reject in the rows above


_CACHE_SIZE = 100
_cache: dict[tuple[int, tuple[tuple[str, str], ...]], _Sample] = {}
_cache_frame: pl.DataFrame | None = None
_cache_lock = threading.Lock()


def preview(
    frame: pl.DataFrame | None,
    formula: str,
    earlier: Sequence[tuple[str, str]] = (),
    *,
    condition: bool = False,
    name: str = "",
    creates: bool = False,
    rows: int = SAMPLE_ROWS,
    cancel: threading.Event | None = None,
    no_data: str = "",
    parameters: Mapping[str, Any] | None = None,
) -> Preview:
    """Evaluate ``formula`` on the first ``rows`` rows of ``frame``. In Calculate columns
    (``creates``), ``earlier`` are the (name, formula) rows above and ``name`` is this row's
    name: the rows above are added first, and the names are checked as the step does. The
    ``parameters`` (name -> value) are filled in first, as the calculation does: into the
    formulas and into the names of the rows. Setting ``cancel`` stops the work (Cancelled)."""
    if frame is None:
        _forget()
        return Preview(False, no_data or NO_DATA)
    typed, written = formula, [new.strip() for new, _ in earlier]
    if parameters:
        filled = []
        for number, (new, text) in enumerate(earlier, 1):
            try:
                filled.append((fill_text(new, parameters), fill_formula(text, parameters)))
            except ActionError as e:  # a parameter with an error
                columns = _written(_prepare(frame, tuple(filled), rows, cancel).columns, written)
                return Preview(False, f"Row {number} ([{new.strip()}]) has an error: {e}", columns=columns)
        earlier = filled
        try:
            name = fill_text(name, parameters)
            formula = fill_formula(formula, parameters)
        except ActionError as e:
            columns = _written(_prepare(frame, tuple(earlier), rows, cancel).columns, written)
            return Preview(False, str(e), columns=columns, **_locate(typed, str(e)))
    with _interrupted_by(cancel):
        sample = _prepare(frame, tuple(earlier), rows, cancel)
        columns = _written(sample.columns, written)
        problem = sample.problem or _same_name(earlier, name)
        if creates and not name.strip():
            problem = problem or "This row has no column name yet: the step needs one."
        if not formula.strip():
            return Preview(problem is None, problem or "Type a formula: double-click a column or a function to insert it.", columns=columns)
        if _empty(formula):
            return Preview(False, problem or "The formula is empty: it has only comments.", columns=columns)
        used = used_columns(formula, sample.data.columns)
        try:
            values = _evaluate(sample.data, used, formula, condition, cancel)
        except ActionError as e:
            _stop_if(cancel)
            if problem:
                return Preview(False, problem, columns=columns)
            message = _message(str(e))
            return Preview(False, message, columns=columns, **_locate(typed, message))
    data = sample.data
    if used:
        base = data.select(used).head(SHOWN_ROWS)
    else:  # nothing to show beside the result: number the rows
        base = pl.DataFrame({"Row": pl.int_range(1, min(data.height, SHOWN_ROWS) + 1, eager=True)})
    result = name.strip() or ("Condition" if condition else "Result")
    while result in base.columns:
        result += " (new)"
    shown = base.with_columns(values.head(SHOWN_ROWS).alias(result))
    scope = f"all {data.height:,} rows" if frame.height <= rows else f"the first {data.height:,} rows"
    if condition:
        true, false, empty = int((values == True).sum()), int((values == False).sum()), values.null_count()  # noqa: E712
        message = f"True for {true:,}, false for {false:,}" + (f", empty for {empty:,} (counts as false)" if empty else "") + f" of {scope}"
    else:
        message = f"Result: {type_label(values.dtype)} · no errors in {scope}"
    note = "window functions see only these rows here" if frame.height > rows and _has_word(formula, "over") else ""
    if note:
        message += f" ({note})"
    return Preview(problem is None, problem or message, shown, columns, values, scope, note)


def used_columns(formula: str, columns: Sequence[str]) -> list[str]:
    """The columns a formula refers to: [Name], "Name", or a name written without brackets.
    Of names that differ only in upper and lower case, the first one is listed."""
    fold = {_fold(c): c for c in reversed(columns)}
    found: list[str] = []
    tokens = expressions.tokenize(formula)
    for index, token in enumerate(tokens):
        if token.kind in ("column", "quoted") and token.closed:
            candidate = fold.get(_fold(expressions._name(formula, token)))
        elif token.kind == "word" and _may_be_column(formula, tokens, index):
            candidate = fold.get(_fold(formula[token.start : token.end]))
        else:
            continue
        if candidate is not None and candidate not in found:
            found.append(candidate)
    return found


def _prepare(frame: pl.DataFrame, earlier: tuple[tuple[str, str], ...], rows: int, cancel: threading.Event | None) -> _Sample:
    """The sample with the rows above added, as Calculate columns does. Samples are kept for
    the next check, and each row builds on the sample of the row above it."""
    sample = _cached(frame, earlier, rows)
    if sample is not None and len(sample.earlier) == len(earlier):
        return sample
    if sample is None:
        data = frame.head(rows)
        sample = _Sample(frame, (), rows, data, [Column(c, type_label(t)) for c, t in data.schema.items()], None)
        _remember(sample)
    names = [new.strip() for new, _ in earlier]
    for number in range(len(sample.earlier) + 1, len(earlier) + 1):
        _stop_if(cancel)
        new, text = names[number - 1], earlier[number - 1][1]
        data, columns, problem = sample.data, sample.columns, sample.problem
        if not new and _empty(text):
            problem = problem or f"Row {number} is still empty: the step needs a name and a formula in every row."
        elif not new:
            problem = problem or f"Row {number} has no column name yet: the step needs one."
        elif _empty(text):
            problem = problem or f"Row {number} ([{new}]) has no formula yet."
        elif names.index(new) + 1 != number:
            problem = problem or _two_names(new, names.index(new) + 1, number)
        else:
            try:
                values = _evaluate(data, used_columns(text, data.columns), text, False, cancel)
            except ActionError as e:
                _stop_if(cancel)
                problem = problem or f"Row {number} ([{new}]) has an error: {_message(str(e))}"
            else:
                data = data.with_columns(values.alias(new))  # replaces a column of the same name in place
                columns = [c for c in columns if c.name != new] + [Column(new, type_label(data.schema[new]), number)]
        sample = _Sample(frame, earlier[:number], rows, data, columns, problem)
        _remember(sample)
    return sample


def _written(columns: list[Column], names: Sequence[str]) -> list[Column]:
    """The columns of the rows above with the names as written in the rows, where parameters
    were filled into them."""
    return [
        replace(column, written=names[column.row - 1])
        if column.row is not None and column.row <= len(names) and names[column.row - 1] != column.name
        else column
        for column in columns
    ]


def _forget() -> None:
    """Drop the kept samples: the input they were made from must not stay in memory for them."""
    global _cache_frame
    with _cache_lock:
        _cache.clear()
        _cache_frame = None


def _cached(frame: pl.DataFrame, earlier: tuple[tuple[str, str], ...], rows: int) -> _Sample | None:
    """The kept sample with the most of these rows above (only for the latest input: a big
    input must not be kept alive by its sample)."""
    global _cache_frame
    with _cache_lock:
        if _cache_frame is not frame:
            _cache.clear()
            _cache_frame = frame
        for size in range(len(earlier), -1, -1):
            found = _cache.get((rows, earlier[:size]))
            if found is not None:
                return found
    return None


def _remember(sample: _Sample) -> None:
    with _cache_lock:
        if _cache_frame is sample.frame:
            _cache[(sample.rows, sample.earlier)] = sample
            while len(_cache) > _CACHE_SIZE:
                del _cache[next(iter(_cache))]


def _locate(formula: str, message: str) -> dict:
    """Where the mistake of an error message is in the formula (``mistake``), and what to write
    there instead (``fix``), if the message tells."""
    if parameter := re.match(r"(There is no parameter|The parameter) \{(.+?)\}", message):
        name, unknown = parameter.group(2).strip().lower(), parameter.group(1) == "There is no parameter"
        for start, end, found, place in formula_places(formula):  # an unknown name is an error only as a value
            if found.strip().lower() == name and (place == "formula" or not unknown):
                return {"mistake": (start, end), "fix": None}
        return {}
    tokens = expressions.tokenize(formula)
    suggestion = re.search(r"\(did you mean (.+)\?\)$", message)
    fix = suggestion.group(1) if suggestion else None
    found: expressions.Token | None = None
    if column := re.match(r"Column \[(.+?)\] not found", message):
        name = _fold(column.group(1))
        found = next((t for i, t in enumerate(tokens) if _token_name(formula, tokens, i) == name), None)
        if fix is not None:
            fix = "[" + fix[1:-1].replace("]", "]]") + "]"
    elif function := re.match(r"There is no function (\w+)", message):
        name = _fold(function.group(1))
        found = next((t for i, t in enumerate(tokens) if _word(formula, t) == name and expressions._is_call(tokens, i)), None)
    elif part := re.match(r"(\w+) is not a column: write the date part in quotes, \w+\('(\w+)'", message):
        found = next((t for t in tokens if _word(formula, t) == _fold(part.group(1))), None)
        fix = f"'{part.group(2)}'"
    elif call := re.match(r"(\w+) is a function: write (\w+\(\))", message):
        found = next((t for i, t in enumerate(tokens) if _word(formula, t) == _fold(call.group(1)) and not expressions._is_call(tokens, i)), None)
        fix = call.group(2)
    elif message.startswith(("A ] is missing", "A */ is missing", "A closing ")):
        found = next((t for t in tokens if not t.closed), None)
        fix = None
    elif message.startswith(("A ) is missing", "There is one ) too many")):
        found = _unmatched_parenthesis(tokens)
        fix = None
    elif near := re.match(r'Syntax error at or near "(.+)"$', message):  # only when that text is there once
        same = [t for t in tokens if formula[t.start : t.end] == near.group(1)]
        found = same[0] if len(same) == 1 else None
    elif message.startswith(("Syntax error at end of input", "The formula ends too early")):
        found = next((t for t in reversed(tokens) if t.kind not in ("space", "comment")), None)
    if found is None:
        return {}
    return {"mistake": (found.start, found.end), "fix": fix}


def _token_name(formula: str, tokens: list[expressions.Token], index: int) -> str | None:
    """The folded column name a token stands for, if it can be one."""
    token = tokens[index]
    if token.kind in ("column", "quoted") and token.closed:
        return _fold(expressions._name(formula, token))
    if token.kind == "word" and not expressions._is_call(tokens, index):
        return _fold(formula[token.start : token.end])
    return None


def _unmatched_parenthesis(tokens: list[expressions.Token]) -> expressions.Token | None:
    """The ( that is never closed, or the ) that closes nothing."""
    opened: list[expressions.Token] = []
    for token in tokens:
        if token.kind == "open":
            opened.append(token)
        elif token.kind == "close":
            if not opened:
                return token
            opened.pop()
    return opened[-1] if opened else None


def _same_name(earlier: Sequence[tuple[str, str]], name: str) -> str | None:
    """The step rejects two new columns with the same name."""
    names = [new.strip() for new, _ in earlier]
    if name.strip() and name.strip() in names:
        return _two_names(name.strip(), names.index(name.strip()) + 1, len(names) + 1)
    return None


def _two_names(name: str, first: int, second: int) -> str:
    return f"Two new columns are named “{name}” (rows {first} and {second}): give each one its own name."


def _evaluate(sample: pl.DataFrame, used: list[str], formula: str, condition: bool, cancel: threading.Event | None) -> pl.Series:
    """Only the used columns go to DuckDB: registering hundreds of columns costs more than the
    formula. If that isn't enough (e.g. a misspelled name), the whole sample is used."""
    if _needs_all_columns(formula):
        return expressions.evaluate(sample, formula, condition=condition)
    folded = {_fold(u) for u in used}  # with names that differ only in case: they must give the step's error
    subset = sample.select([c for c in sample.columns if _fold(c) in folded] or sample.columns[:1])
    try:
        return expressions.evaluate(subset, formula, condition=condition)
    except ActionError:
        _stop_if(cancel)
        if subset.width == sample.width:
            raise
        return expressions.evaluate(sample, formula, condition=condition)


def _needs_all_columns(formula: str) -> bool:
    """COLUMNS(*), a * among the arguments of a function or a subquery on __input use columns
    that aren't named in the formula: they would give other values on fewer columns."""
    tokens = expressions.tokenize(formula)
    for index, token in enumerate(tokens):
        text = formula[token.start : token.end]
        if token.kind == "word" and _fold(text) in ("columns", "__input"):
            return True
        if token.kind == "operator" and text.startswith("*"):
            before = expressions._previous(tokens, index)
            if before is None or tokens[before].kind == "comma":
                return True
            if tokens[before].kind == "open":
                function = expressions._previous(tokens, before)
                if function is None or _fold(formula[tokens[function].start : tokens[function].end]) != "count":
                    return True
    return False


def _may_be_column(formula: str, tokens: list[expressions.Token], index: int) -> bool:
    """A word without brackets can be a column, unless it is a function, a keyword, a type
    (CAST(x AS DATE), DATE '2024-01-01') or the unit of an interval (INTERVAL 3 DAY)."""
    if expressions._is_call(tokens, index) or _fold(formula[tokens[index].start : tokens[index].end]) in _KEYWORDS:
        return False
    following = expressions._next(tokens, index)
    if following is not None and following.kind == "string":
        return False
    before = expressions._previous(tokens, index)
    if before is None:
        return True
    if _word(formula, tokens[before]) == "as" or formula[tokens[before].start : tokens[before].end] == "::":
        return False
    two_before = expressions._previous(tokens, before)
    return two_before is None or _word(formula, tokens[two_before]) != "interval"



def _word(formula: str, token: expressions.Token) -> str:
    return _fold(formula[token.start : token.end]) if token.kind == "word" else ""


def _has_word(formula: str, word: str) -> bool:
    return any(_word(formula, token) == word for token in expressions.tokenize(formula))


def _empty(formula: str) -> bool:
    return all(token.kind in ("space", "comment") for token in expressions.tokenize(formula))


@contextmanager
def _interrupted_by(cancel: threading.Event | None) -> Iterator[None]:
    """Setting ``cancel`` stops a query that runs in DuckDB on this thread. DuckDB forgets an
    interrupt that comes before the query starts, so it is repeated until the work is over."""
    if not isinstance(cancel, CancelEvent):
        yield
        return
    con = expressions._connection()
    lock = threading.Lock()
    running = True

    def keep_interrupting() -> None:
        while True:
            with lock:  # never after the work is over: it would stop the next query on this thread
                if not running:
                    return
                con.interrupt()
            time.sleep(0.05)

    remove = cancel.when_set(lambda: threading.Thread(target=keep_interrupting, daemon=True).start())
    try:
        yield
    finally:
        remove()
        with lock:
            running = False
    _stop_if(cancel)


def _stop_if(cancel: threading.Event | None) -> None:
    if cancel is not None and cancel.is_set():
        raise Cancelled()


def _message(error: str) -> str:
    text = error.split("\nFormula: ")[0].removeprefix("Formula error: ")
    return text[:1].upper() + text[1:]


def _fold(name: str) -> str:
    return expressions._fold(name)

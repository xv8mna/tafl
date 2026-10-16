"""Project parameters: values that the settings of steps and connections use, written as
``{Name}``.

A parameter's value is either fixed (``2026-10-01``, ``D:\\Data``, ``500``) or a formula
(``=TODAY() - 1``) that can use the parameters above it. Fixed values get a type as on import: a
whole number, a decimal number, a date (``2026-10-01``), a date-time (``2026-10-01 08:30``; one
with a time zone in UTC), otherwise text; whole numbers with leading zeros (``01067``) stay
text. A fixed value keeps the text it was written as (``Fixed``): ``2.50`` is the number 2.5 in
a formula, but ``2.50`` in a file name. Actions get their settings with ``{Name}`` filled in
(in the settings that are shown); they don't see the values themselves.

Where ``{Name}`` is filled in decides how:

- in texts, paths and names: the value as text (dates as ``2026-10-01``);
- in formulas: a typed value (``DATE '2026-10-01'``, ``500``, ``(-5)``, ``'text'``); inside a
  formula's own quotes (``'Report {Month}'``), column names (``[Sales {Year}]``) and "names" the
  text, with the closing character doubled; in comments and $$texts$$ nothing;
- in SQL: a typed value in the database's own way of writing it (``Connector.fill_sql`` and
  ``Connector.literal``); inside '…' the text with the quotes doubled. Names ("…", […], `…`),
  comments and $$texts$$ stay as they are. So a value can't end or start a text, a name or a
  comment, as long as they are found where the database finds them: ``Connector.sql_spans``
  reads them with ``sql_spans``, and a connector overrides it for a database that reads them
  differently (the built-in SQLite and SQL Server connectors do; another database may e.g.
  have backslash escapes in texts).

A typed value never joins a word, a number or a quote right next to it: a space is written
between them (``Sales_{Year}`` gives ``Sales_ 2026``, not the name ``Sales_2026``; in SQL
``5-{X}`` with -5 gives ``5- -5``, not ``5--5``, the start of a comment). In formulas a text
or a date next to a text of the formula, with only spaces, line breaks or comments between
them, is written in parentheses: DuckDB joins two texts on separate lines into one. (Two
values next to each other, ``{A}`` and on the next line ``{B}``, are joined: both are plain
texts, so that changes nothing but the text.)

Only the names of parameters are filled in (upper and lower case don't matter); other braces,
such as ``\\d{3}`` in a pattern, stay as they are.
"""

from __future__ import annotations

import bisect
import datetime as dt
import functools
import math
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .errors import ActionError

NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_ ]*")
PLACE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_ ]*)\}")
_INTEGER = re.compile(r"[+-]?(0|[1-9]\d*)")
_DECIMAL = re.compile(r"[+-]?(\d+\.\d*|\.\d+)([eE][+-]?\d+)?")
_DOLLAR = re.compile(r"\$(?:[A-Za-z_][A-Za-z0-9_]*)?\$")
# Besides letters, digits and the characters from U+0080 on (which SQLite and DuckDB read as
# part of a name): a typed value next to one would join it (? starts a numbered parameter)
_JOINING = "_.$@#:?'\"`[]{}"
_Spans = list[tuple[int, int, str]]  # (start, end, kind) of the quoted parts and comments


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


@dataclass(frozen=True)
class Fixed:
    """A fixed value with the text it was written as: in formulas and SQL the value counts, in
    texts, paths and inside quotes the text (``2.50`` stays ``2.50``, not ``2.5``)."""

    value: Any
    text: str


def typed(text: str) -> Any:
    """The value of a fixed parameter: a number, a date, a date-time or the text itself. A
    date-time with a time zone (``2026-10-01 08:30+02:00``) is in local time, without the zone,
    as NOW() and date-times with a time zone are in formulas."""
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
            stamp = dt.datetime.fromisoformat(value)
        except ValueError:
            pass
        else:
            return stamp if stamp.utcoffset() is None else stamp.astimezone().replace(tzinfo=None)
    return text


def fixed(text: str) -> Fixed:
    """A fixed parameter: its typed value, and its text (without the spaces around a number or
    a date; a text value as it is)."""
    value = typed(text)
    return Fixed(value, text if isinstance(value, str) else text.strip())


def plain(value: Any) -> Any:
    """The value itself: for a fixed value without the text it was written as."""
    return value.value if isinstance(value, Fixed) else value


def as_text(value: Any) -> str:
    """A value written into a text, a path or a name; a fixed value as it was written."""
    if isinstance(value, Fixed):
        return value.text
    if isinstance(value, dt.datetime):
        return value.isoformat(sep=" ", timespec="seconds" if value.microsecond == 0 else "microseconds")
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value)) if abs(value) < 1e15 else repr(value)
    return "" if value is None else str(value)


def formula_literal(value: Any) -> str:
    """A value written into a formula (DuckDB). A negative number is in parentheses, (-5):
    plain -5 would be -(5::UTINYINT) before ::, an error in INTERVAL -5 DAY, and an operator of
    its own after another one (2**-5)."""
    value = plain(value)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float) and not math.isfinite(value):
        return f"'{value}'::DOUBLE"
    if isinstance(value, int | float):
        number = repr(value)
        return f"({number})" if number.startswith("-") else number
    if isinstance(value, dt.datetime):
        return f"TIMESTAMP '{value.isoformat(sep=' ')}'"
    if isinstance(value, dt.date):
        return f"DATE '{value.isoformat()}'"
    return "'" + str(value).replace("'", "''") + "'"


def sql_literal(value: Any) -> str:
    """A value written into SQL, the way most databases read it (``Connector.literal``): texts
    and dates in quotes, a negative number as -5 (which also works where SQL takes only a
    constant, e.g. in SQLite's PRAGMA). An infinite number or one that is not a number is an
    ActionError."""
    value = plain(value)
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float) and not math.isfinite(value):
        kind = "a value that is not a number" if math.isnan(value) else "an infinite number"
        raise ActionError(f"SQL has no way to write {kind} ({value})")
    if isinstance(value, int | float):
        return repr(value)
    if isinstance(value, dt.datetime | dt.date):
        return "'" + as_text(value) + "'"
    return "'" + str(value).replace("'", "''") + "'"


def fill_text(text: str, values: Mapping[str, Any]) -> str:
    """``{Name}`` in a text, a path or a name: the value as text."""
    return PLACE.sub(lambda m: _found(m, values, as_text), text)


def fill_formula(formula: str, values: Mapping[str, Any]) -> str:
    """``{Name}`` in a formula: a typed value (with a space between it and a word, a number or a
    quote right next to it); inside the formula's '…' texts, "names" and [columns] the value as
    text (with the closing character doubled); in $$texts$$ and comments nothing changes.

    A value written with quotes (a text, a date) that has a text of the formula next to it, with only spaces,
    line breaks or comments between them, is in parentheses: DuckDB reads two texts on separate
    lines as one ('a'\\n'b' is 'ab'), so the value would go on the other text, also an E'…' text
    with its backslash escapes."""
    spans = _formula_spans(formula)
    comments = [(start, end) for start, end, kind in spans if kind == "" and formula.startswith(("--", "/*"), start)]

    def literal(value: Any, position: int) -> str:
        written = formula_literal(value)
        end = PLACE.match(formula, position).end()
        if (written.startswith("'") and beside(formula, position, comments, backwards=True) == "'") or (
            written.endswith("'") and beside(formula, end, comments) == "'"
        ):
            return f"({written})"
        return written

    return _fill(formula, values, literal, spans)


def fill_sql(
    sql: str,
    values: Mapping[str, Any],
    literal: Callable[[Any], str] = sql_literal,
    *,
    spans: _Spans | None = None,
    literal_at: Callable[[Any, int], str] | None = None,
) -> str:
    """``{Name}`` in SQL: a typed value written by ``literal`` (which gets the plain value), with
    a space between it and a word, a number or a quote right next to it; inside '…' the value as
    text, with the quotes doubled. In names ("…", […], `…`), comments and $$texts$$ nothing changes.
    ``spans`` are where these are, for a database that doesn't read them as ``sql_spans`` does
    by default (e.g. ``sql_spans(sql, nested_comments=False)``). ``literal_at``, if given,
    writes the typed values instead of ``literal``: it gets the plain value and the position of
    its ``{Name}`` in ``sql``, for a database that takes values in another form at some places
    (e.g. only constants as the arguments of a procedure call)."""
    found = sql_spans(sql) if spans is None else spans
    places = [(start, end, "'" if kind == "text" else "") for start, end, kind in found]
    write = literal_at or (lambda value, _position: literal(value))
    return _fill(sql, values, lambda value, position: write(plain(value), position), places)


def left_in_formula(formula: str) -> str | None:
    """The first ``{Name}`` left in a formula outside its texts, names and comments: no parameter
    has that name."""
    return next((name for _start, _end, name, place in formula_places(formula) if place == "formula"), None)


def formula_places(formula: str) -> list[tuple[int, int, str, str]]:
    """Where a formula has ``{Name}`` that would be filled in: (start, end, name, place). The
    place is "formula" (a typed value), "text" (in a '…' text) or "name" (in a [column] or a
    "name"). Those in $$texts$$ and comments are not listed."""
    found, position = [], 0
    for start, end, kind in _formula_spans(formula):
        found += [(m.start(), m.end(), m[1], "formula") for m in PLACE.finditer(formula, position, start)]
        if kind:
            place = "name" if kind in ('"', "]") else "text"
            found += [(m.start(), m.end(), m[1], place) for m in PLACE.finditer(formula, start, end)]
        position = end
    found += [(m.start(), m.end(), m[1], "formula") for m in PLACE.finditer(formula, position)]
    return found


def unknown(text: str, values: Mapping[str, Any], *, sql: bool = False, spans: _Spans | None = None) -> list[str]:
    """Names in ``{…}`` that are no parameter, where a parameter would be filled in (with
    ``sql``: not in names, comments and $$texts$$, which ``spans`` gives as in ``fill_sql``),
    once each."""
    known = {name.strip().lower() for name in values}
    found_spans = (sql_spans(text) if spans is None else spans) if sql else []
    skips = [(start, end) for start, end, kind in found_spans if kind != "text"]
    starts = [start for start, _end in skips]
    found = []
    for match in PLACE.finditer(text):
        k = bisect.bisect_right(starts, match.start()) - 1  # the last skipped part that starts before it
        if match[1].strip().lower() not in known and (k < 0 or skips[k][1] <= match.start()):
            found.append(match[1])
    return list(dict.fromkeys(found))


def sql_spans(
    sql: str,
    *,
    nested_comments: bool = True,
    dollar_quotes: bool = True,
    line_ends: str = "\r\n",
    words: re.Pattern[str] | None = None,
) -> _Spans:
    """The quoted parts and comments of SQL, in order: (start, end, kind). Kinds: "text" ('…'),
    "name" ("…", […], `…`), "comment" (-- up to the end of the line, and /* */) and "dollar"
    ($$…$$ or $tag$…$tag$). What isn't closed runs to the end.

    The options say how the database reads SQL: ``nested_comments``: /* /* */ */ is one comment
    (as in SQL Server and PostgreSQL); ``dollar_quotes``: $$…$$ and $tag$…$tag$ are texts;
    ``line_ends``: the characters that end a -- comment (a carriage return or a line feed, as
    in SQL Server, PostgreSQL and DuckDB; SQLite: only "\\n"); ``words``: a pattern for other
    words in which no quote or comment starts (e.g. SQLite's variables such as $a(x'y)), listed
    as names."""
    spans, i, n = [], 0, len(sql)
    closing = {"'": "'", '"': '"', "[": "]", "`": "`"}
    while i < n:
        ch, dollar = sql[i], None
        word = words.match(sql, i) if words is not None else None
        if dollar_quotes and ch == "$" and not (i > 0 and (sql[i - 1].isalnum() or sql[i - 1] in "_$")):  # a$b$c is a name
            dollar = _DOLLAR.match(sql, i)
        if word and word.end() > i:
            spans.append((i, word.end(), "name"))
            i = word.end()
        elif ch in closing:
            close, j = closing[ch], i + 1
            while j < n:
                if sql[j] == close:
                    if j + 1 < n and sql[j + 1] == close:  # a doubled closing character stands for itself
                        j += 2
                        continue
                    break
                j += 1
            spans.append((i, min(j + 1, n), "text" if ch == "'" else "name"))
            i = j + 1
        elif dollar:
            j = sql.find(dollar[0], dollar.end())
            j = n if j < 0 else j + len(dollar[0])
            spans.append((i, j, "dollar"))
            i = j
        elif sql.startswith("--", i):
            end = _line_end(line_ends).search(sql, i) if line_ends else None
            j = end.start() if end else n
            spans.append((i, j, "comment"))
            i = j
        elif sql.startswith("/*", i) and not nested_comments:
            j = sql.find("*/", i + 2)
            j = n if j < 0 else j + 2
            spans.append((i, j, "comment"))
            i = j
        elif sql.startswith("/*", i):
            depth, j = 0, i
            while j < n:
                if sql.startswith("/*", j):
                    depth, j = depth + 1, j + 2
                elif sql.startswith("*/", j):
                    depth, j = depth - 1, j + 2
                    if depth == 0:
                        break
                else:
                    j += 1
            spans.append((i, j, "comment"))
            i = j
        else:
            i += 1
    return spans


def beside(text: str, position: int, comments: Sequence[tuple[int, int]], *, backwards: bool = False) -> str:
    """The first character from ``position`` on (``backwards``: before it) that is no space and
    in none of the ``comments`` ((start, end), in order): what the database reads next to a value
    written there. "" at the start or the end of the text."""
    i = position
    while 0 < i if backwards else i < len(text):
        at = i - 1 if backwards else i
        k = bisect.bisect_right(comments, at, key=lambda comment: comment[0]) - 1  # the last one that starts at or before it
        if k >= 0 and comments[k][1] > at:
            i = comments[k][0] if backwards else comments[k][1]
        elif text[at].isspace():
            i = at if backwards else i + 1
        else:
            return text[at]
    return ""


@functools.cache
def _line_end(characters: str) -> re.Pattern[str]:
    return re.compile("[" + re.escape(characters) + "]")


def _found(match: re.Match, values: Mapping[str, Any], write: Callable[[Any], str]) -> str:
    name = match[1].strip().lower()
    for key, value in values.items():
        if key.strip().lower() == name:
            if isinstance(value, Failed):
                raise ActionError(f"The parameter {{{key}}} has an error: {value.message}")
            try:
                return write(value)
            except ActionError as e:
                raise ActionError(f"The parameter {{{key}}} can't be used here: {e}") from None
    return match[0]


def _fill(text: str, values: Mapping[str, Any], literal: Callable[[Any, int], str], spans: _Spans) -> str:
    """Fill in ``{Name}``: outside the ``spans`` (start, end, kind) a typed value written by
    ``literal`` (from the value and the position of its ``{Name}``), see ``_typed``; in a span
    whose kind is a closing character (', ", ]) as text with that character doubled ("e'": also
    the backslashes, for E'…' texts); in other spans not at all."""
    out, position = [], 0
    for start, end, kind in [*spans, (len(text), len(text), "")]:
        for match in PLACE.finditer(text, position, start):
            out += [text[position : match.start()], _typed(text, match, values, literal)]
            position = match.end()
        piece = text[start:end]
        if kind:
            piece = PLACE.sub(lambda m, kind=kind: _found(m, values, lambda v: _quoted(as_text(v), kind)), piece)
        out += [text[position:start], piece]
        position = end
    return "".join(out)


def _typed(text: str, match: re.Match, values: Mapping[str, Any], literal: Callable[[Any, int], str]) -> str:
    """The typed value for the ``{Name}`` at ``match``, with a space between it and a letter, a
    digit, a character from U+0080 on or one of ``_JOINING`` right before or after the
    ``{Name}`` in ``text`` (the whole text: also a quote that ends a text), which it would join
    otherwise (``Sales_{Year}``: the name Sales_2026, ``1{N}``: 15, ``e{T}``: an E'…' text), and
    between a ``-`` and a negative number (``5-{X}``: ``5--5``, the start of a comment). A
    ``{Name}`` that is no parameter stays as it is."""

    def spaced(value: Any) -> str:
        written = literal(value, match.start())
        before, after = text[match.start() - 1 : match.start()], text[match.end() : match.end() + 1]
        if _joins(before) or (before == "-" and written.startswith("-")):
            written = " " + written
        return written + " " if _joins(after) else written

    return _found(match, values, spaced)


def _joins(character: str) -> bool:
    return character.isalnum() or (character != "" and (character in _JOINING or character >= "\x80"))


def _quoted(text: str, kind: str) -> str:
    if kind == "e'":
        return text.replace("\\", "\\\\").replace("'", "''")
    return text.replace(kind, kind * 2)


def _formula_spans(formula: str) -> _Spans:
    """The quoted parts and comments of a formula: (start, end, kind), the kind being the
    closing character of a text ('), "name" (") or [column] (]), "e'" for an E'…' text (with
    backslash escapes), "" for $$texts$$ and comments."""
    from .expressions import tokenize  # the formula's own tokens

    spans = []
    previous = ""  # the kind of the last '…' text, while only spaces and comments follow it
    for token in tokenize(formula):
        if token.kind == "string" and formula.startswith("'", token.start):
            start = token.start
            before = formula[max(start - 2, 0) : start]  # E'…' unless the E ends a longer word
            escapes = before[-1:] in ("e", "E") and not (len(before) == 2 and (before[0].isalnum() or before[0] == "_"))
            # DuckDB joins a text on a later line to the one before it: after an E'…' text the
            # joined part has the backslash escapes too
            previous = "e'" if escapes or previous == "e'" else "'"
            spans.append((start, token.end, previous))
            continue
        if token.kind == "quoted":
            spans.append((token.start, token.end, '"'))
        elif token.kind == "column":
            spans.append((token.start, token.end, "]"))
        elif token.kind in ("string", "comment"):
            spans.append((token.start, token.end, ""))
        if token.kind not in ("space", "comment"):
            previous = ""
    return spans

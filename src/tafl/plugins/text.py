"""Text: replace values, clean up text, split columns."""

from __future__ import annotations

import re

import polars as pl

from tafl.api import ActionError, P, action, dtypes


@action(
    "text.replace",
    "Replace",
    category="Text",
    params=[
        P.Columns("columns", "In columns", required=True),
        P.Choice(
            "match",
            "Find",
            [("whole", "Whole cell value"), ("contains", "Text inside the value"), ("regex", "Regular expression")],
        ),
        P.List(
            "replacements",
            "Replace",
            fields=[P.Text("find", "Find"), P.Text("replace", "Replace with")],
            min_items=1,
        ),
        P.Bool("case_sensitive", "Upper/lower case matters", default=True),
    ],
    summary=lambda p: f"Replace in {', '.join(p.columns)}: "
    + ", ".join(f"'{r.find}' → '{r.replace}'" for r in p.replacements),
)
def replace(df, p, ctx):
    """Replaces values.

    Whole cell value: cells equal to "Find" get the new value; an empty "Find" matches empty
    cells, an empty "Replace with" makes cells empty. Text inside: every occurrence is
    replaced. Regular expression: "Replace with" can use $1, $2 for groups.
    """
    pairs = [(r.find, r.replace) for r in p.replacements]
    if p.match != "whole" and any(find == "" for find, _ in pairs):
        raise ActionError("'Find' can't be empty here")
    if p.match == "regex":
        for find, _ in pairs:
            _check_regex(find)
    updates = []
    for name in p.columns:
        dtype = df.schema[name]
        if p.match == "whole":
            updates.append(_replace_whole(name, dtype, pairs, p.case_sensitive, ctx))
        else:
            text = pl.col(name).cast(pl.String)
            for find, new in pairs:
                if p.match == "contains" and p.case_sensitive:
                    text = text.str.replace_all(find, new, literal=True)
                else:
                    pattern = find if p.match == "regex" else re.escape(find)
                    replacement = new if p.match == "regex" else new.replace("$", "$$")
                    flags = "" if p.case_sensitive else "(?i)"
                    text = text.str.replace_all(flags + pattern, replacement)
            updates.append(text.alias(name))
    try:
        return df.with_columns(updates)
    except pl.exceptions.ComputeError as e:
        raise ActionError(f"Invalid regular expression: {_regex_problem(e)}") from None


def _check_regex(pattern: str) -> None:
    try:
        pl.select(pl.lit("").str.contains(pattern))
    except pl.exceptions.ComputeError as e:
        raise ActionError(f"Invalid regular expression '{pattern}': {_regex_problem(e)}") from None


def _regex_problem(error: Exception) -> str:
    """The reason in an error of the regex library, e.g. "unclosed group"."""
    found = re.search(r"^error: (.+)$", str(error), re.MULTILINE)
    return found.group(1).strip() if found else str(error).splitlines()[0]


def _replace_whole(name: str, dtype: pl.DataType, pairs, case_sensitive: bool, ctx) -> pl.Expr:
    column = pl.col(name)
    typed = _typed_pairs(pairs, dtype) if dtype != pl.String else None
    if dtype != pl.String and typed is None:
        ctx.warn(f"'{name}' was converted to text because a value isn't a {dtype}")
        column, dtype = column.cast(pl.String), pl.String
    branches = None
    for index, (find, new) in enumerate(pairs):
        if dtype == pl.String:
            if find == "":
                condition = column.is_null() | (column.str.strip_chars() == "")
            elif case_sensitive:
                condition = column == find
            else:
                condition = column.str.to_lowercase() == find.lower()
            value = pl.lit(new if new != "" else None, dtype=pl.String)
        else:
            find_value, new_value = typed[index]
            condition = column.is_null() if find_value is None else column == find_value
            value = pl.lit(new_value, dtype=dtype)
        branches = pl.when(condition).then(value) if branches is None else branches.when(condition).then(value)
    return branches.otherwise(column).alias(name)


def _typed_pairs(pairs, dtype: pl.DataType):
    """Find/replace values converted to the column type, or None if one doesn't fit."""
    if not (dtype.is_numeric() or dtype in (pl.Date, pl.Datetime, pl.Boolean)):
        return None
    result = []
    for find, new in pairs:
        converted = []
        for text in (find, new):
            if text.strip() == "":
                converted.append(None)
                continue
            series = pl.Series([text.strip()])
            if isinstance(dtype, pl.Decimal):  # exact decimals: all digits count
                value = dtypes.parse_decimal(text.strip(), "," if "," in text else ".")
                if value is not None and not _fits(value, dtype):
                    value = None
            elif dtype.is_numeric():
                decimal = "," if "," in text else "."
                value = dtypes.parse_numbers(series, decimal)[0]
                if value is not None and dtype.is_integer():  # all digits, also beyond 15
                    value = dtypes.parse_integers(series, decimal)[0] if value.is_integer() else None
            elif dtype == pl.Date:
                value = dtypes.parse_dates(series)[0]
            elif dtype == pl.Datetime:
                value = dtypes.parse_datetimes(series)[0]
            else:
                value = dtypes.parse_booleans(series)[0]
            if value is None:
                return None
            converted.append(value)
        result.append(tuple(converted))
    return result


def _fits(value, dtype: pl.Decimal) -> bool:
    """Whether a Decimal column can hold ``value`` without cutting digits."""
    _, digits, exponent = value.normalize().as_tuple()
    whole = max(len(digits) + exponent, 0)  # digits before the decimal point
    return max(-exponent, 0) <= dtype.scale and whole <= (dtype.precision or 38) - dtype.scale


@action(
    "text.clean",
    "Clean text",
    category="Text",
    params=[
        P.Columns("columns", "Columns (empty = all text columns)"),
        P.Choice("trim", "Remove spaces", [("both", "at start and end"), ("start", "at the start"), ("end", "at the end"), ("none", "nowhere")]),
        P.Bool("collapse_spaces", "Replace repeated spaces with one", default=False),
        P.Bool("line_breaks", "Replace line breaks with a space", default=False),
        P.Choice("case", "Upper/lower case", [("keep", "keep"), ("upper", "UPPER CASE"), ("lower", "lower case"), ("title", "Title Case")]),
        P.Int("pad_length", "Pad to length (0 = off)", min=0, help="Adds characters at the start, e.g. 1234 → 01234"),
        P.Text("pad_char", "Padding character", default="0"),
        P.Bool("empty_to_null", "Make empty text an empty value", default=True),
    ],
    summary=lambda p: "Clean text" + (f": {', '.join(p.columns)}" if p.columns else ""),
)
def clean(df, p, ctx):
    """Tidies up text: removes surrounding or repeated spaces and line breaks, changes upper/lower
    case, pads values to a fixed length (e.g. postal codes 1234 → 01234). Selected columns that
    are not text are turned into text."""
    names = p.columns or [name for name, dtype in df.schema.items() if dtype == pl.String]
    updates = []
    for name in names:
        text = pl.col(name).cast(pl.String)
        if p.line_breaks:
            text = text.str.replace_all(r"\r\n|\r|\n", " ")
        if p.collapse_spaces:
            text = text.str.replace_all(r"[ \t ]{2,}", " ")
        text = {
            "both": text.str.strip_chars(),
            "start": text.str.strip_chars_start(),
            "end": text.str.strip_chars_end(),
            "none": text,
        }[p.trim]
        text = {
            "upper": text.str.to_uppercase().str.replace_all("ẞ", "SS", literal=True),  # as UPPER() in formulas
            "lower": text.str.to_lowercase(),
            "title": text.str.to_titlecase(),
            "keep": text,
        }[p.case]
        if p.pad_length > 0:
            if len(p.pad_char) != 1:
                raise ActionError("The padding character must be exactly one character")
            text = text.str.pad_start(p.pad_length, p.pad_char)
        if p.empty_to_null:
            text = pl.when(text == "").then(None).otherwise(text)
        updates.append(text.alias(name))
    return df.with_columns(updates)


_SEPARATORS = [
    (",", "Comma ,"),
    (";", "Semicolon ;"),
    ("space", "Space"),
    ("tab", "Tab"),
    ("line", "Line break"),
    ("other", "Other text"),
]
_SEPARATOR_SHOWN = {",": "','", ";": "';'", "space": "spaces", "tab": "tabs", "line": "line breaks"}


def _split_summary(p) -> str:
    """E.g. "Split Name at ',' into Last name, First name"; a setting that is not filled in yet
    shows as ? (the engine gives "?" for it), without quotes and without "? 1, ? 2"."""
    if p.separator == "other":
        separator = "?" if p.other == "?" else f"'{p.other}'"
    else:
        separator = _SEPARATOR_SHOWN[p.separator]
    if p.into == "rows":
        target = "rows"
    elif p.column == "?" and any(not row["name"].strip() for row in p.parts):
        target = f"{len(p.parts)} columns"
    else:
        target = ", ".join(_part_names(p))
    return f"Split {p.column} at {separator} into {target}"


@action(
    "text.split",
    "Split column",
    category="Text",
    params=[
        P.Column("column", "Column"),
        P.Choice("into", "Into", [("columns", "Columns: one new column per part"), ("rows", "Rows: one row per part")]),
        P.Choice("separator", "Separator", _SEPARATORS),
        P.Text(
            "other",
            "Separator text",
            required=True,
            visible_if={"separator": "other"},
            help="Any text, e.g. / or - (as it is, not a pattern).",
        ),
        P.Bool("trim", "Remove spaces around the parts", default=True),
        P.List(
            "parts",
            "New columns (one per part)",
            fields=[P.Text("name", "Name", placeholder="automatic")],
            default=({"name": ""}, {"name": ""}),
            min_items=2,
            visible_if={"into": "columns"},
            help="The last one gets the rest of the text. Without a name: Name 1, Name 2, … after the column.",
        ),
        P.Bool("keep", "Keep the original column", default=False, visible_if={"into": "columns"}),
    ],
    summary=_split_summary,
)
def split(df, p, ctx):
    """Splits the text of a column at a separator, into columns or into rows.

    Into columns: "Müller, Hans" becomes Müller and Hans in two new columns, which take the place of
    the column. There are as many parts as new columns: the last one gets the rest of the text
    ("Müller, Hans, Dr." gives Müller and Hans, Dr.), and missing parts stay empty. Into rows: "A;B;C"
    becomes three rows, with the other columns repeated; empty parts are left out, and a row without
    any part stays, with an empty value.

    The parts are text; a part with nothing but spaces is empty. With Space, several spaces in a row
    are one separator. A column that is not text is turned into text first: 2.5 and 2 (not 2.0),
    exact decimals with their decimal places (2.00), 2026-02-27, 2026-02-27 08:30:00 (fractions of a
    second only when there are some).
    """
    text = _as_text(p.column, df.schema[p.column])
    if df.schema[p.column] != pl.String:  # turned into text once, not for each use below
        text = pl.lit(df.select(text).to_series())
    if p.separator == "space":  # runs of spaces are one separator, and spaces at start and end make no parts
        prepared, separator = text.str.replace_all(" {2,}", " ").str.strip_chars(" "), " "
    elif p.separator == "line":  # Windows, Unix and old Mac line breaks
        prepared, separator = text.str.replace_all(r"\r\n?", "\n"), "\n"
    elif p.separator == "other":
        if p.other == "":
            raise ActionError("Separator text: required")
        prepared, separator = text, p.other
    else:
        prepared, separator = text, {"tab": "\t"}.get(p.separator, p.separator)
    if p.into == "rows":
        part = pl.element()
        if p.trim:
            part = part.str.strip_chars()
            kept = part.filter(part != "")
        else:
            kept = part.filter(part.str.strip_chars() != "")
        parts = prepared.str.split(separator).list.eval(kept)
        return df.with_columns(parts.alias(p.column)).explode(p.column, empty_as_null=True, keep_nulls=True)
    names = _part_names(p)
    _check_part_names(names, [c for c in df.columns if p.keep or c != p.column])
    # each part in a column of its own, split once (an expression would split again for each use)
    pieces = df.select(prepared.str.splitn(separator, len(names)).alias("pieces")).unnest("pieces")
    rest = pieces.columns[-1]  # what is left after the other parts
    more = _part(pl.col(rest), p.trim).str.contains(separator, literal=True).fill_null(False)  # as the column shows it
    more = pieces.select(more).to_series()
    count = more.sum()
    if count:
        example = " ".join(re.split(r"\r\n|\r|\n|\t", df.select(text.filter(more).first()).item()))
        example = example if len(example) <= 30 else example[:30] + "…"
        values = "1 value has" if count == 1 else f"{count:,} values have"
        ctx.log(f"{values} more parts than there are new columns: the last column has the rest, e.g. “{example}”")
    if p.separator in ("space", "line"):  # the rest as it is, with its own runs of spaces and line breaks
        as_it_is = _rest_as_it_is(text, pieces[rest], p.separator, len(names) - 1)
        pieces = pieces.with_columns(df.select(as_it_is.alias(rest)).to_series())
    new = pieces.select(_part(pl.col(field), p.trim).alias(name) for field, name in zip(pieces.columns, names))
    columns = []
    for name in df.columns:
        if name != p.column or p.keep:
            columns.append(df[name])
        if name == p.column:
            columns += new.get_columns()
    return pl.DataFrame(columns)


def _as_text(column: str, dtype: pl.DataType) -> pl.Expr:
    """The values of a column as text, for splitting: as Pivot names its columns (2, not 2.0;
    2026-01-01 08:30:00)."""
    if dtype.is_nested() or dtype in (pl.Binary, pl.Object, pl.Duration):
        raise ActionError(f"“{column}” holds {dtypes.type_label(dtype)} values, which can't be split")
    return dtypes.text_expr(pl.col(column), dtype)


def _part_names(p) -> list[str]:
    """The names of the new columns: as given, or Name 1, Name 2, … after the column."""
    return [row["name"].strip() or f"{p.column} {number}" for number, row in enumerate(p.parts, start=1)]


def _check_part_names(names: list[str], existing: list[str]) -> None:
    if len(names) < 2:
        raise ActionError("New columns (one per part): add at least 2 rows")
    for number, name in enumerate(names, start=1):
        first = names.index(name) + 1
        if first != number:
            raise ActionError(f"Two new columns are named “{name}” (rows {first} and {number}). Give each one its own name.")
    taken = set(existing)
    for number, name in enumerate(names, start=1):
        if name in taken:
            raise ActionError(
                f"New columns, row {number}: the table has a column named “{name}” already. "
                "Give the new column another name."
            )


def _part(part: pl.Expr, trim: bool) -> pl.Expr:
    """One part of a split value: empty (null) when it has nothing but spaces."""
    if trim:
        return part.str.strip_chars().replace("", None)
    return pl.when(part.str.strip_chars() != "").then(part)


def _rest_as_it_is(text: pl.Expr, rest: pl.Series, separator: str, before: int) -> pl.Expr:
    """At Space and Line break: the text after the first ``before`` parts and the separator after
    each, as it is. ``rest`` is the same rest of the prepared text (spaces in a row as one, \\n for
    each line break): null when there are fewer parts."""
    if separator == "line":  # the length of the rest, from a text of the same length with \n for each line break
        same_length = text.str.replace_all("\r\n", " \n", literal=True).str.replace_all("\r", "\n", literal=True)
        after = same_length.str.splitn("\n", before + 1).struct.field(f"field_{before}")
        return text.str.slice(text.str.len_chars() - after.str.len_chars())
    after = text
    while before:  # each part and the spaces after it, for at most 1,000 parts at a time (a longer pattern is too big)
        step = min(before, 1000)
        after, before = after.str.replace(rf"^ *(?:[^ ]+ +){{{step}}}", ""), before - step
    return pl.when(rest.is_not_null()).then(after)

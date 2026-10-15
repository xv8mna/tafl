"""Text: replace values, clean up text."""

from __future__ import annotations

import re

import polars as pl

from etl_tool.api import ActionError, P, action, dtypes


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
        raise ActionError(f"Invalid regular expression: {str(e).splitlines()[0]}") from None


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
            if dtype.is_numeric():
                value = dtypes.parse_numbers(series, "," if "," in text else ".")[0]
                if value is not None and dtype.is_integer():
                    value = int(value) if float(value).is_integer() else None
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
            "upper": text.str.to_uppercase(),
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

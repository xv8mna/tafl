"""Combining tables: start from another table, merge (lookup / join), append."""

from __future__ import annotations

import re

import polars as pl

from tafl.api import ActionError, P, action

_NUMBERED = re.compile(r"(.*\()(\d+)(\)\s*)")  # a suffix like " (2)"
_HOW = [
    ("lookup", "Lookup (first match, row count stays)"),
    ("left", "Left join (all rows of this table)"),
    ("inner", "Inner join (only rows with a match)"),
    ("full", "Full join (all rows of both tables)"),
    ("matching", "Keep rows with a match"),
    ("non_matching", "Keep rows without a match"),
]
_JOINS = ("left", "inner", "full")
_ADDS_COLUMNS = ("lookup", *_JOINS)
_VERBS = {
    "lookup": "Lookup",
    "left": "Left join",
    "inner": "Inner join",
    "full": "Full join",
    "matching": "Keep rows matching",
    "non_matching": "Keep rows not matching",
}


def _migrate_merge(params: dict, from_version: int) -> dict:
    """Version 1 had "lookup" (= left join) plus a first/all choice that defaulted to "first"."""
    if from_version < 2:
        multiple = params.get("multiple", "first")
        if params.get("how", "lookup") == "lookup":
            params["how"] = "left" if multiple == "all" else "lookup"
        params["multiple"] = multiple
    return params


@action(
    "table.derive",
    "Start from another table",
    category="Import",
    kind="source",
    params=[P.TableRef("table", "Table")],
    summary="Start from {table}",
)
def derive(df, p, ctx):
    """Starts this table with the result of another table, so you can branch off and
    transform it differently without touching the original."""
    return ctx.table(p.table)


@action(
    "table.merge",
    "Merge with another table",
    category="Combine",
    params=[
        P.TableRef("other", "Other table"),
        P.List(
            "match",
            "Match rows where",
            fields=[
                P.Column("column", "Column in this table"),
                P.Column("other_column", "equals column in the other table", source="other"),
            ],
            min_items=1,
        ),
        P.Choice("how", "Merge type", _HOW),
        P.Columns("columns", "Columns to add (empty = all)", source="other", visible_if={"how": _ADDS_COLUMNS}),
        P.Choice(
            "multiple",
            "If several rows of the other table match",
            [("all", "Use all of them, rows repeat (like SQL)"), ("first", "Use only the first one")],
            visible_if={"how": _JOINS},
        ),
        P.Text("suffix", "Suffix for column names that exist already", default=" (2)", visible_if={"how": _ADDS_COLUMNS}),
    ],
    version=2,
    migrate=_migrate_merge,
    summary=lambda p: f"{_VERBS.get(p.how, 'Merge')} {p.other} on " + ", ".join(m.column for m in p.match),
)
def merge(df, p, ctx):
    """Combines this table with another table by matching key columns.

    Lookup (the default, like a lookup in a spreadsheet) adds columns from the first matching row, so
    the row count never changes. Left, inner and full join work like in SQL: every matching row
    is used, so a row repeats when the other table has several matches. Keep rows with or
    without a match filters this table without adding columns. Empty keys never match. Keys of
    different types (a number and a text) are compared as text; the key columns keep their type,
    except in a full join whose other table has keys that don't fit it (2.5 for whole numbers):
    then they get a type that fits both.
    """
    other = ctx.table(p.other)
    right_on = [m.other_column for m in p.match]
    left, right, left_on, right_keys, compared = _compared_keys(df, other, [m.column for m in p.match], right_on, ctx)

    if p.how in ("matching", "non_matching"):
        keys = right.select(right_keys).unique()
        how = "semi" if p.how == "matching" else "anti"
        out = left.join(keys, left_on=left_on, right_on=right_keys, how=how, maintain_order="left")
        return out.drop([key for _, key in compared])

    columns = [c for c in (p.columns or other.columns) if c not in right_on]
    if missing := [c for c in columns if c not in other.columns]:
        raise ActionError(f"Columns not in the other table: {', '.join(missing)}")
    right = right.select([*right_keys, *columns])
    if p.how == "lookup" or p.multiple == "first":
        right = right.unique(subset=right_keys, keep="first", maintain_order=True)
    taken = {*df.columns, *columns}
    renames = {}
    for column in columns:
        if column in df.columns:
            renames[column] = _free_name(column, p.suffix, taken)
            taken.add(renames[column])
    right = right.rename(renames)
    out = left.join(
        right,
        left_on=left_on,
        right_on=right_keys,
        how={"lookup": "left", "left": "left", "inner": "inner", "full": "full"}[p.how],
        suffix=p.suffix,
        coalesce=True,
        maintain_order="left_right" if p.how == "full" else "left",
    )
    if p.how == "full":  # rows only in the other table get its key, in this table's type if it fits
        for name, key in compared:
            back = pl.col(key).cast(df.schema[name], strict=False)
            fits = pl.col(name).is_not_null() | pl.col(key).is_null() | (back.cast(out.schema[key], strict=False) == pl.col(key))
            if out.select(fits.all()).item():
                out = out.with_columns(pl.coalesce(name, back).alias(name))
            else:
                out = out.with_columns(pl.coalesce(pl.col(name).cast(out.schema[key]), key).alias(name))
    return out.drop([key for _, key in compared])


@action(
    "table.append",
    "Append other tables",
    category="Combine",
    params=[
        P.TableRefs("tables", "Tables to append below this one"),
        P.Choice(
            "columns",
            "Columns",
            [("all", "All columns of all tables (missing values stay empty)"), ("this", "Only the columns of this table")],
        ),
    ],
    summary=lambda p: "Append " + ", ".join(p.tables),
)
def append(df, p, ctx):
    """Adds the rows of other tables below the rows of this table. Columns are matched by
    name; if a column has different types in different tables, it becomes text."""
    frames = [df, *(ctx.table(t) for t in p.tables)]
    out = pl.concat(frames, how="diagonal_relaxed")
    return out.select(df.columns) if p.columns == "this" else out


def _free_name(name: str, suffix: str, taken: set[str]) -> str:
    """``name`` + ``suffix``; if that exists too, the number in the suffix counts up
    ("Net (2)" → "Net (3)"), or one is added."""
    numbered = _NUMBERED.fullmatch(suffix)
    count = int(numbered.group(2)) if numbered else 1
    candidate = name + suffix
    while candidate in taken:
        count += 1
        candidate = f"{name}{numbered.group(1)}{count}{numbered.group(3)}" if numbered else f"{name}{suffix}{count}"
    return candidate


def _compared_keys(left, right, left_on, right_on, ctx):
    """Join keys must have the same type: keys of different types are compared as numbers or
    text, in copies of the key columns, so that the columns themselves keep their type.

    Returns both tables with the copies, the key columns to join on, and the copies of this
    table's key columns as (key column, copy).
    """
    left_keys, right_keys, compared = list(left_on), list(right_on), []
    taken = {*left.columns, *right.columns}
    for index, (left_name, right_name) in enumerate(zip(left_on, right_on)):
        left_type, right_type = left.schema[left_name], right.schema[right_name]
        if left_type == right_type:
            continue
        if left_type.is_numeric() and right_type.is_numeric():
            target = pl.Int64 if left_type.is_integer() and right_type.is_integer() else pl.Float64
        elif left_type.is_temporal() and right_type.is_temporal():
            target = pl.Datetime("us")
        else:
            target = pl.String
            ctx.warn(f"'{left_name}' ({left_type}) and '{right_name}' ({right_type}) are compared as text")
        key = f"__key{index}__"
        while key in taken:
            key = f"_{key}_"
        taken.add(key)
        left = left.with_columns(pl.col(left_name).cast(target, strict=False).alias(key))
        right = right.with_columns(pl.col(right_name).cast(target, strict=False).alias(key))
        left_keys[index] = right_keys[index] = key
        compared.append((left_name, key))
    return left, right, left_keys, right_keys, compared

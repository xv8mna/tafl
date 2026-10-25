"""Combining tables: start from another table, merge (lookup / join), append."""

from __future__ import annotations

import re
from decimal import Decimal

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
    without a match filters this table without adding columns. The step tells how many rows
    matched and shows a few keys of the rows without a match. Empty keys never match. Whole
    numbers and exact decimals are compared with all their digits, also when their types differ.
    Keys of different types (a number and a text) are compared as text; the key columns keep
    their type, except in a full join whose other table has keys that don't fit it (2.5 for
    whole numbers): then they get a type that fits both.
    """
    other = ctx.table(p.other)
    pairs = list(dict.fromkeys((m.column, m.other_column) for m in p.match))  # a row given twice counts once
    right_on = [other_column for _, other_column in pairs]
    left, right, left_on, right_keys, compared = _compared_keys(df, other, [column for column, _ in pairs], right_on, ctx)
    if p.how == "full":  # a key of the other table that doesn't fit the compared type would get lost
        for name, other_name, key in compared:
            if right.select((pl.col(other_name).is_not_null() & pl.col(key).is_null()).any()).item():
                raise ActionError(_no_common_type(df, name, other, other_name, right.schema[key]))
    own =list(dict.fromkeys(column for column, _ in pairs))  # this table's key columns, for the message

    if p.how in ("matching", "non_matching"):
        keys = right.select(_once(right_keys)).unique()
        how = "semi" if p.how == "matching" else "anti"
        out = left.join(keys, left_on=left_on, right_on=right_keys, how=how, maintain_order="left")
        unmatched = df.height - out.height if p.how == "matching" else out.height
        missed = None
        if p.how == "matching" and unmatched:
            missed = _without_match(left, left_on, keys, right_keys, own) if out.height else df.select(own)
        if df.height:
            ctx.log(_feedback(p.how, df.height, unmatched, missed, other.height))
        return out.drop([key for *_, key in compared])

    columns = [c for c in (p.columns or other.columns) if c not in right_on]
    if missing := [c for c in columns if c not in other.columns]:
        raise ActionError(f"Columns not in the other table: {', '.join(missing)}")
    right = right.select(_once([*right_keys, *columns]))
    if p.how == "full" and p.multiple == "first":
        right = _first_matches(right, right_keys, left.select(_once(left_on)), left_on)
    elif p.how == "lookup" or p.multiple == "first":
        right = right.unique(subset=right_keys, keep="first", maintain_order=True)
    taken = {*left.columns, *right.columns}  # also the key columns and their copies
    renames = {}
    for column in columns:
        if column in df.columns:
            renames[column] = _free_name(column, p.suffix, taken)
            taken.add(renames[column])
    right = right.rename(renames)
    # Markers: empty in the joined rows without a row of the other table (or of this one). Real
    # columns, not pl.lit(True): the joined table would be copied to line up their parts.
    markers = []
    if p.how != "inner":
        markers.append(_free_name("__other_row__", "", {*left.columns, *right.columns}))
        right = right.with_columns(pl.repeat(True, right.height, eager=True).alias(markers[0]))
    if p.how == "full":
        markers.append(_free_name("__this_row__", "", {*left.columns, *right.columns}))
        left = left.with_columns(pl.repeat(True, left.height, eager=True).alias(markers[1]))
    out = left.join(
        right,
        left_on=left_on,
        right_on=right_keys,
        how={"lookup": "left", "left": "left", "inner": "inner", "full": "full"}[p.how],
        suffix=p.suffix,
        coalesce=True,
        maintain_order="left_right" if p.how == "full" else "left",
    )
    missed = None  # this table's key columns of its rows without a match
    if p.how != "inner":  # a row of this table without a match is there once, without a row of the other table
        unmatched = out[markers[0]].null_count()
        if 0 < unmatched < df.height:
            missed = out.select([*own, markers[0]]).filter(pl.col(markers[0]).is_null()).select(own)
    elif out.height and p.multiple == "all":  # rows repeat, so the joined rows don't tell how many matched
        missed = _without_match(left, left_on, right, right_keys, own)
        unmatched = missed.height
    else:  # each row of this table is there once at most
        unmatched = df.height - out.height
        if 0 < unmatched < df.height:
            missed = _without_match(left, left_on, right, right_keys, own)
    if unmatched == df.height:
        missed = df.select(own)
    added = out[markers[1]].null_count() if p.how == "full" else 0  # rows of the other table without a match
    out = out.drop(markers)
    if p.how == "full":  # rows only in the other table get its key, in this table's type if it fits
        for name, other_name, key in compared:
            back = _cast_fitting(pl.col(key), out.schema[key], df.schema[name])
            same = (_cast_key(back, df.schema[name], out.schema[key]) == pl.col(key)).fill_null(False)
            if out.select((pl.col(name).is_not_null() | pl.col(key).is_null() | same).all()).item():
                out = out.with_columns(pl.coalesce(name, back).alias(name))
            else:
                wide = _cast_key(pl.col(name), df.schema[name], out.schema[key])
                if out.select((pl.col(name).is_not_null() & wide.is_null()).any()).item():
                    raise ActionError(_no_common_type(df, name, other, other_name, out.schema[key]))
                out = out.with_columns(pl.coalesce(wide, key).alias(name))
    if df.height or added:  # a full join of an empty table: the rows of the other table
        ctx.log(_feedback(p.how, df.height, unmatched, missed, other.height, added))
    return out.drop([key for *_, key in compared])


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
    text, in copies of the key columns, so that the columns themselves keep their type. A column
    of the other table that is the key for two columns of this table gets a copy for the second:
    with one column for both, a full join loses one of the columns it adds.

    Returns both tables with the copies, the key columns to join on, and the compared keys as
    (key column of this table, key column of the other table, copy).
    """
    left_keys, right_keys, compared = list(left_on), list(right_on), []
    taken = {*left.columns, *right.columns}
    for index, (left_name, right_name) in enumerate(zip(left_on, right_on)):
        left_type, right_type = left.schema[left_name], right.schema[right_name]
        repeated = right_name in right_keys[:index]
        if left_type == right_type and not repeated:
            continue
        key = f"__key{index}__"
        while key in taken:
            key = f"_{key}_"
        taken.add(key)
        if left_type == right_type:  # only the other table needs the copy
            right = right.with_columns(pl.col(right_name).alias(key))
            right_keys[index] = key
            continue
        if left_type.is_numeric() and right_type.is_numeric():
            target = _number_type(left_type, right_type)
        elif isinstance(left_type, pl.Duration) and isinstance(right_type, pl.Duration):  # other units
            target = pl.Duration("ns" if "ns" in (left_type.time_unit, right_type.time_unit) else "us")
        elif left_type.is_temporal() and right_type.is_temporal():
            target = pl.Datetime("us")
        else:
            target = pl.String
        try:
            left = left.with_columns(_cast_key(pl.col(left_name), left_type, target).alias(key))
            right = right.with_columns(_cast_key(pl.col(right_name), right_type, target).alias(key))
        except pl.exceptions.InvalidOperationError:  # Polars has no such cast: a duration as text, a time as a date-time
            raise ActionError(
                f"'{left_name}' ({left_type}) and '{right_name}' ({right_type}) can't be compared: choose other "
                f"key columns, or first make one of them text with a formula such as CAST([{left_name}] AS VARCHAR)"
            ) from None
        if target == pl.String:
            ctx.warn(f"'{left_name}' ({left_type}) and '{right_name}' ({right_type}) are compared as text")
        left_keys[index] = right_keys[index] = key
        compared.append((left_name, right_name, key))
    return left, right, left_keys, right_keys, compared


def _number_type(left: pl.DataType, right: pl.DataType) -> pl.DataType:
    """The type in which number keys of two types are compared. Whole numbers and exact decimals
    keep all their digits: as Int64 when both types fit it, otherwise as a Decimal with the
    decimal places of both. A value that doesn't fit that Decimal is longer than any value of
    the other side, so it can't match. Only a decimal number (Float) makes it Float64."""
    if left.is_float() or right.is_float():
        return pl.Float64
    if _fits_int64(left) and _fits_int64(right):
        return pl.Int64
    return pl.Decimal(38, max(_scale(left), _scale(right)))


def _cast_key(column: pl.Expr, dtype: pl.DataType, target: pl.DataType) -> pl.Expr:
    """``column`` (of type ``dtype``) as ``target``, to compare keys, empty where a value doesn't
    fit. A time keeps its fractions of a second as text ("08:00:00", "08:00:00.500"): Polars' cast
    to text leaves them out."""
    if dtype == pl.Time and target == pl.String:
        return column.dt.strftime("%H:%M:%S%.f")
    return column.cast(target, strict=False)


def _cast_fitting(column: pl.Expr, dtype: pl.DataType, target: pl.DataType) -> pl.Expr:
    """``column`` (of type ``dtype``) as ``target``, empty where a value doesn't fit. Polars
    stops with an error for a decimal number (Float) too big for an exact decimal (1e9 for a
    Decimal(10, 2)), also with strict=False: those are left out first. Polars can't cast text
    to a date, date-time, time or true/false: text is read in the form that ``_cast_key`` writes
    ("2024-01-31", "2024-01-31 08:00:00.000000", "08:00:00.500", "true")."""
    if dtype.is_float() and isinstance(target, pl.Decimal):
        whole = (target.precision or 38) - target.scale  # digits before the decimal point
        below = 10.0**whole - 0.5 * 10.0**-target.scale  # values below it don't round up to more digits
        column = pl.when(column.abs() < below).then(column)
    if dtype == pl.String:
        if target == pl.Date:
            return column.str.to_date("%Y-%m-%d", strict=False)
        if isinstance(target, pl.Datetime):
            text = "%Y-%m-%d %H:%M:%S%.f" + ("%:z" if target.time_zone else "")
            return column.str.to_datetime(text, time_unit=target.time_unit, time_zone=target.time_zone, strict=False)
        if target == pl.Time:
            return column.str.to_time("%H:%M:%S%.f", strict=False)
        if target == pl.Boolean:
            return column.replace_strict({"true": True, "false": False}, default=None, return_dtype=pl.Boolean)
    return column.cast(target, strict=False)


def _fits_int64(dtype: pl.DataType) -> bool:
    if isinstance(dtype, pl.Decimal):
        return dtype.scale == 0 and dtype.precision <= 18
    return dtype in (pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32)


def _scale(dtype: pl.DataType) -> int:
    return dtype.scale if isinstance(dtype, pl.Decimal) else 0


def _no_common_type(left: pl.DataFrame, name: str, right: pl.DataFrame, other_name: str, compared: pl.DataType) -> str:
    """``compared``: the type the keys are compared in, which holds the values of neither column."""
    why = {
        pl.Decimal: ": an exact decimal holds 38 digits, decimal places included",
        pl.Duration: ": a time span in nanoseconds holds up to 292 years",
    }.get(type(compared), "")
    return (
        f"Full join: the keys '{name}' ({left.schema[name]}) and '{other_name}' ({right.schema[other_name]}) "
        f"don't fit in one column{why}"
    )


def _first_matches(right, right_keys, keys, left_on) -> pl.DataFrame:
    """The other table for a full join that uses only the first matching row: of the rows that
    match a key of this table (``keys``), only the first one per key stays. The rows without a
    match all stay, also those with the same key, and those with an empty key: they never match,
    and a full join adds every one of them."""
    row = _free_name("__row__", "", set(right.columns))
    right = right.with_row_index(row)
    again = right.select(_once([row, *right_keys])).filter(~pl.struct(right_keys).is_first_distinct())  # a key seen before
    repeated = again.join(keys, left_on=right_keys, right_on=left_on, how="semi")
    return right.filter(~pl.col(row).is_in(repeated[row].implode())).drop(row)


def _without_match(left, left_on, right, right_keys, own) -> pl.DataFrame:
    """This table's key columns ``own`` of its rows without a match, in row order (only the key
    columns are joined)."""
    keys = left.select(_once([*own, *left_on]))
    out = keys.join(right.select(_once(right_keys)), left_on=left_on, right_on=right_keys, how="anti", maintain_order="left")
    return out.select(own)


def _once(columns: list[str]) -> list[str]:
    """The columns without repeats (a column can be the key for two columns of the other table)."""
    return list(dict.fromkeys(columns))


def _feedback(how, rows, unmatched, missed, other_rows, added=0) -> str:
    """How many of this table's ``rows`` matched, with a few key values of those that didn't
    (``missed``: their key columns). ``other_rows``: the other table's rows; ``added``: those
    without a match that a full join added. The other table is not named: it may be renamed while
    the step's result is kept, and the step's summary names it."""
    matched = rows - unmatched
    if not rows:  # a full join of an empty table
        return f"This table has no rows: the {added:,} {'row' if added == 1 else 'rows'} of the other table {_were(added)} added"
    if how == "non_matching":
        why = "" if other_rows else "the other table has no rows, so "
        if rows == 1:
            return f"The row has no match: {why}it was kept" if unmatched else "The row matched, so the table is empty"
        if not matched:
            return f"None of the {rows:,} rows matched: {why}all were kept"
        if not unmatched:
            return f"All {rows:,} rows matched, so the table is empty"
        return (
            f"{unmatched:,} of {rows:,} rows without a match {_were(unmatched)} kept; "
            f"{matched:,} matched and {_were(matched)} left out"
        )
    left_out = how in ("inner", "matching")  # rows without a match are left out
    kept = f" and {_were(matched)} kept" if how == "matching" else ""
    if not unmatched:
        text = "The row matched" if rows == 1 else f"All {rows:,} rows matched{kept}"
    else:
        empty = ", so the table is empty" if left_out else ""
        sample = _sample(missed) if other_rows else "the other table has no rows"
        if rows == 1:
            text = f"The row has no match{empty}: {sample}"
        elif not matched:
            text = f"None of the {rows:,} rows matched{empty}: {sample}"
        elif left_out:
            text = f"{matched:,} of {rows:,} rows matched{kept}; "
            text += f"{unmatched:,} without a match {_were(unmatched)} left out: {sample}"
        else:
            text = f"{matched:,} of {rows:,} rows matched, {unmatched:,} without a match: {sample}"
    if added:
        text += f"; {added:,} {'row' if added == 1 else 'rows'} of the other table without a match {_were(added)} added"
    return text


def _were(count: int) -> str:
    return "was" if count == 1 else "were"


_SAMPLE_ROWS = 10_000  # the key values shown come from this many rows without a match at most


def _sample(keys: pl.DataFrame) -> str:
    """Up to three distinct values of ``keys`` (rows without a match), in row order, and how many
    other values there are: "09999, 01234, 05555 and 17 more"."""
    first = keys.head(_SAMPLE_ROWS).unique(maintain_order=True)
    rows = first.head(3).rows()
    if keys.height <= _SAMPLE_ROWS:
        distinct = first.height
    else:
        # The hashes of the rows are much faster than n_unique() for texts and several columns;
        # two keys with one 64-bit hash are too rare to matter.
        hashes = keys.hash_rows()
        distinct = hashes.n_unique()
        if len(rows) < 3 and distinct > len(rows):  # the first rows have one or two keys (sorted by key)
            rows = keys[hashes.is_first_distinct().arg_true().head(3).to_list()].rows()
    shown = [_key_text(row) for row in rows]
    if distinct > len(shown):
        return f"{', '.join(shown)} and {distinct - len(shown):,} more"
    return f"{', '.join(shown[:-1])} and {shown[-1]}" if len(shown) > 1 else shown[0]


def _key_text(values: tuple) -> str:
    """A key as text: the value, or (value 1, value 2) for several key columns."""
    if len(values) == 1:
        return _value_text(values[0])
    return f"({', '.join(_value_text(value, inside=True) for value in values)})"


def _value_text(value, inside: bool = False) -> str:
    """A value as text; ``inside`` the parentheses of a key with several columns, "empty" has none."""
    if value is None:
        return "empty" if inside else "(empty)"
    if isinstance(value, str) and not value:
        return "empty text" if inside else "(empty text)"
    # str() writes a decimal zero with 10 places as 0E-10; dates and times come out in ISO form
    text = format(value, "f") if isinstance(value, Decimal) else str(value)
    text = " ".join(text.splitlines())
    return text if len(text) <= 30 else text[:29] + "…"

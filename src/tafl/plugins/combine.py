"""Combining tables: start from another table, merge (lookup / join), append."""

from __future__ import annotations

import re
from decimal import Decimal

import polars as pl

from tafl.api import ActionError, P, action, col, cols, dtypes

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
    Keys of different types (a number and a text) are compared as text, the same on every PC (the
    decimal number 17.0 finds the text 17); the key columns keep their type, except in a full join
    whose other table has keys that don't fit it (2.5 for whole numbers): then they get a type
    that fits both. Lists and records match only keys of their own type.
    """
    other = ctx.table(p.other)
    pairs = list(dict.fromkeys((m.column, m.other_column) for m in p.match))  # a row given twice counts once
    right_on = [other_column for _, other_column in pairs]
    left, right, left_on, right_keys, compared = _compared_keys(df, other, [column for column, _ in pairs], right_on, ctx)
    if p.how == "full":  # a key of the other table that doesn't fit the compared type would get lost
        for name, other_name, key in compared:
            if right.select((col(other_name).is_not_null() & col(key).is_null()).any()).item():
                raise ActionError(_no_common_type(df, name, other, other_name, right.schema[key]))
    own =list(dict.fromkeys(column for column, _ in pairs))  # this table's key columns, for the message

    if p.how in ("matching", "non_matching"):
        keys = right.select(cols(_once(right_keys))).unique()
        how = "semi" if p.how == "matching" else "anti"
        out = left.join(keys, left_on=cols(left_on), right_on=cols(right_keys), how=how, maintain_order="left")
        unmatched = df.height - out.height if p.how == "matching" else out.height
        missed = None
        if p.how == "matching" and unmatched:
            missed = _without_match(left, left_on, keys, right_keys, own) if out.height else df.select(cols(own))
        if df.height:
            ctx.log(_feedback(p.how, df.height, unmatched, _bytes_as_text(df, missed), other.height))
        return out.drop([key for *_, key in compared])

    columns = [c for c in (p.columns or other.columns) if c not in right_on]
    if missing := [c for c in columns if c not in other.columns]:
        raise ActionError(f"Columns not in the other table: {', '.join(missing)}")
    right = right.select(cols(_once([*right_keys, *columns])))
    if p.how == "full" and p.multiple == "first":
        right = _first_matches(right, right_keys, left.select(cols(_once(left_on))), left_on)
    elif p.how == "lookup" or p.multiple == "first":
        right = right.unique(subset=cols(right_keys), keep="first", maintain_order=True)
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
        left_on=cols(left_on),
        right_on=cols(right_keys),
        how={"lookup": "left", "left": "left", "inner": "inner", "full": "full"}[p.how],
        suffix=_spare_suffix(left.columns, right.columns),
        coalesce=True,
        maintain_order="left_right" if p.how == "full" else "left",
    )
    missed = None  # this table's key columns of its rows without a match
    if p.how != "inner":  # a row of this table without a match is there once, without a row of the other table
        unmatched = out[markers[0]].null_count()
        if 0 < unmatched < df.height:
            missed = out.select(cols([*own, markers[0]])).filter(col(markers[0]).is_null()).select(cols(own))
    elif out.height and p.multiple == "all":  # rows repeat, so the joined rows don't tell how many matched
        missed = _without_match(left, left_on, right, right_keys, own)
        unmatched = missed.height
    else:  # each row of this table is there once at most
        unmatched = df.height - out.height
        if 0 < unmatched < df.height:
            missed = _without_match(left, left_on, right, right_keys, own)
    if unmatched == df.height:
        missed = df.select(cols(own))
    added = out[markers[1]].null_count() if p.how == "full" else 0  # rows of the other table without a match
    out = out.drop(markers)
    if p.how == "full":  # rows only in the other table get its key, in this table's type if it fits
        for name, other_name, key in compared:
            as_hex = _as_hex(df[name])
            dtype = out.schema[name]  # not df's: a column that is the key for two may be text after the first
            if dtype == pl.String and out.schema[key] != pl.String:  # text since an earlier key, this one not compared as text
                own = other.schema[other_name]  # the key as text in its own type: 0.1 for a Float32, a zone's local time
                text = dtypes.text_expr(_cast_fitting(col(key), out.schema[key], own), own)
                out = out.with_columns(pl.coalesce(col(name), text).alias(name))
                continue
            back = _cast_fitting(col(key), out.schema[key], dtype)
            same = (_cast_key(back, dtype, out.schema[key], as_hex) == col(key)).fill_null(False)
            if out.select((col(name).is_not_null() | col(key).is_null() | same).all()).item():
                out = out.with_columns(pl.coalesce(col(name), back).alias(name))
            else:
                wide = _cast_key(col(name), dtype, out.schema[key], as_hex)
                if out.select((col(name).is_not_null() & wide.is_null()).any()).item():
                    raise ActionError(_no_common_type(df, name, other, other_name, out.schema[key]))
                out = out.with_columns(pl.coalesce(wide, col(key)).alias(name))
    if df.height or added:  # a full join of an empty table: the rows of the other table
        ctx.log(_feedback(p.how, df.height, unmatched, _bytes_as_text(df, missed), other.height, added))
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
    """Adds the rows of other tables below the rows of this table. Columns are matched by name.
    A column with different types in the tables gets the type that Unpivot gives its value
    column: whole and decimal numbers become decimal numbers, whole numbers and exact decimals
    exact decimals, dates and date-times date-times, when every value stays as it is. Any other
    mix becomes text, and the step says so. A table in which the column has only empty values
    doesn't count. Lists and records can't be in one column with values of another type."""
    others = [ctx.table(t) for t in p.tables]
    if p.columns == "this":  # the other columns are left out, whatever their types
        others = [_only(other, df.columns) for other in others]
    out = dtypes.stack([df, *others], ctx.warn)  # as a folder import stacks its files
    return out.select(cols(df.columns)) if p.columns == "this" else out


def _only(frame: pl.DataFrame, columns: list[str]) -> pl.DataFrame:
    """``frame`` with only those of ``columns`` that it has. With none of them, its rows stay, as
    empty values of the first one: a frame without columns has no rows."""
    own = set(columns)
    kept = [c for c in frame.columns if c in own]
    if kept or not columns:
        return frame.select(cols(kept))
    return pl.repeat(None, frame.height, dtype=pl.Null, eager=True).alias(columns[0]).to_frame()


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


def _spare_suffix(left: list[str], right: list[str]) -> str:
    """A suffix for the join itself: for a moment, a full join with several keys names the key
    columns of the other table with it. The new columns have their final names by then, so it
    isn't in the result; it only must not give a name of either table, as an empty suffix does, or
    " (2)" next to a column "Region (2)"."""
    taken = {*left, *right}
    suffix = "__other__"
    while any(name + suffix in taken for name in right):
        suffix += "_"
    return suffix


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
        _check_lists(left_name, left_type, right_name, right_type)
        repeated = right_name in right_keys[:index]
        if left_type == right_type and not repeated:
            continue
        key = f"__key{index}__"
        while key in taken:
            key = f"_{key}_"
        taken.add(key)
        if left_type == right_type:  # only the other table needs the copy
            right = right.with_columns(col(right_name).alias(key))
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
        left_hex, right_hex = _as_hex(left[left_name]), _as_hex(right[right_name])
        left_label, right_label = dtypes.type_label(left_type), dtypes.type_label(right_type)
        keys = f"'{left_name}' ({left_label}) and '{right_name}' ({right_label})"
        try:
            left = left.with_columns(_cast_key(col(left_name), left_type, target, left_hex).alias(key))
            right = right.with_columns(_cast_key(col(right_name), right_type, target, right_hex).alias(key))
        except pl.exceptions.InvalidOperationError:  # Polars has no such cast: a duration as text, a time as a date-time
            raise ActionError(
                f"{keys} can't be compared: choose other key columns, or first make one of them text with a "
                f"formula such as CAST([{left_name}] AS VARCHAR)"
            ) from None
        if target == pl.String and not left_label == right_label == "text":  # categories and text: both are called text
            how = " (the binary values as hex digits, 0xFF00)" if left_hex or right_hex else ""
            ctx.warn(f"{keys} are compared as text{how}")
        left_keys[index] = right_keys[index] = key
        compared.append((left_name, right_name, key))
    return left, right, left_keys, right_keys, compared


def _check_lists(left_name: str, left_type: pl.DataType, right_name: str, right_type: pl.DataType) -> None:
    """Lists and records (from Parquet files or plugins) match only lists and records of the same
    type, value by value: they have no text to compare with other keys. A plugin's own values
    (objects) can't be compared at all."""
    for name, dtype, other in ((left_name, left_type, right_name), (right_name, right_type, left_name)):
        if dtype == pl.Object:
            raise ActionError(f"'{name}' holds object values, which can't be compared: choose other key columns")
        if dtype.is_nested() and left_type != right_type:
            raise ActionError(
                f"'{name}' holds {dtypes.type_label(dtype)} values, which can't be compared with '{other}': "
                "choose other key columns"
            )


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


def _cast_key(column: pl.Expr, dtype: pl.DataType, target: pl.DataType, as_hex: bool = False) -> pl.Expr:
    """``column`` (of type ``dtype``) as ``target``, to compare keys, empty where a value doesn't
    fit. As text, a value is written the same on every PC, as Pivot and Split column write it (see
    ``dtypes.text_expr``): 17 for 17.0, 10000000000000000 for 1e16, "2025-12-31 08:30:00" (a time
    zone's local time), "08:00:00.500".
    Polars' own text differs ("17.0", "1e16", "2025-12-31 08:30:00.000000+01:00") and leaves out
    the fractions of a second of a time. Binary values are text, or hex digits with ``as_hex`` (see
    ``_as_hex``). A time span has no cast to text: Polars stops, and so does the step."""
    if target == pl.String and dtype not in (pl.String, pl.Duration) and (dtype != pl.Binary or as_hex):
        return dtypes.text_expr(column, dtype)
    return column.cast(target, strict=False)


def _as_hex(values: pl.Series) -> bool:
    """Whether binary keys are compared with text as hex digits (0xFF00), as Join texts writes
    them: when some of them are no text. Bytes that are all text are compared as text."""
    return values.dtype == pl.Binary and not dtypes.bytes_are_text(values)


def _cast_fitting(column: pl.Expr, dtype: pl.DataType, target: pl.DataType) -> pl.Expr:
    """``column`` (of type ``dtype``) as ``target``, empty where a value doesn't fit. Polars
    stops with an error for a decimal number (Float) too big for an exact decimal (1e9 for a
    Decimal(10, 2)), also with strict=False: those are left out first. Polars can't cast text
    to a date, date-time, time or true/false: text is read in the form that ``_cast_key`` writes
    ("2024-01-31", "2024-01-31 08:00:00", "2024-01-31 08:00:00.250", "08:00:00.500", "true"). A
    date-time with a time zone is its local time there: empty when the clocks were turned back or
    forward at that time (two moments or none)."""
    if dtype.is_float() and isinstance(target, pl.Decimal):
        whole = (target.precision or 38) - target.scale  # digits before the decimal point
        below = 10.0**whole - 0.5 * 10.0**-target.scale  # values below it don't round up to more digits
        column = pl.when(column.abs() < below).then(column)
    if dtype == pl.String:
        if target == pl.Date:
            return column.str.to_date("%Y-%m-%d", strict=False)
        if isinstance(target, pl.Datetime):
            moment = column.str.to_datetime("%Y-%m-%d %H:%M:%S%.f", time_unit=target.time_unit, strict=False)
            if target.time_zone:
                moment = moment.dt.replace_time_zone(target.time_zone, ambiguous="null", non_existent="null")
            return moment
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
        f"Full join: the keys '{name}' ({dtypes.type_label(left.schema[name])}) and '{other_name}' "
        f"({dtypes.type_label(right.schema[other_name])}) don't fit in one column{why}"
    )


def _first_matches(right, right_keys, keys, left_on) -> pl.DataFrame:
    """The other table for a full join that uses only the first matching row: of the rows that
    match a key of this table (``keys``), only the first one per key stays. The rows without a
    match all stay, also those with the same key, and those with an empty key: they never match,
    and a full join adds every one of them."""
    row = _free_name("__row__", "", set(right.columns))
    right = right.with_row_index(row)
    seen = ~pl.struct(cols(right_keys)).is_first_distinct()  # a key seen before
    again = right.select(cols(_once([row, *right_keys]))).filter(seen)
    repeated = again.join(keys, left_on=cols(right_keys), right_on=cols(left_on), how="semi")
    return right.filter(~col(row).is_in(repeated[row].implode())).drop(row)


def _without_match(left, left_on, right, right_keys, own) -> pl.DataFrame:
    """This table's key columns ``own`` of its rows without a match, in row order (only the key
    columns are joined)."""
    keys = left.select(cols(_once([*own, *left_on])))
    out = keys.join(
        right.select(cols(_once(right_keys))), left_on=cols(left_on), right_on=cols(right_keys), how="anti", maintain_order="left"
    )
    return out.select(cols(own))


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
    rows = first.head(3)
    if keys.height <= _SAMPLE_ROWS:
        distinct = first.height
    else:
        # The hashes of the rows are much faster than n_unique() for texts and several columns;
        # two keys with one 64-bit hash are too rare to matter.
        hashes = keys.hash_rows()
        distinct = hashes.n_unique()
        if rows.height < 3 and distinct > rows.height:  # the first rows have one or two keys (sorted by key)
            rows = keys[hashes.is_first_distinct().arg_true().head(3).to_list()]
    shown = [_key_text(row) for row in _key_texts(rows).rows()]
    if distinct > len(shown):
        return f"{', '.join(shown)} and {distinct - len(shown):,} more"
    return f"{', '.join(shown[:-1])} and {shown[-1]}" if len(shown) > 1 else shown[0]


def _bytes_as_text(df: pl.DataFrame, keys: pl.DataFrame | None) -> pl.DataFrame | None:
    """This table's key columns ``keys`` of rows without a match, with binary values as their text
    where all bytes of the column in this table ``df`` are text, as Merge compares them with text
    keys (see ``_as_hex``). The other binary values are shown as hex digits, 0xFF00."""
    if keys is None:
        return None
    texts = [name for name, dtype in keys.schema.items() if dtype == pl.Binary and not _as_hex(df[name].alias("value"))]
    return keys.with_columns(col(name).cast(pl.String) for name in texts) if texts else keys


def _key_texts(keys: pl.DataFrame) -> pl.DataFrame:
    """The key values as text, the same on every PC, as Pivot and Split column write them (see
    ``dtypes.text_expr``): 17 and 10000000000000000 for decimal numbers, true, 0xFF00,
    08:30:00.500, 1d 2h 3m, a time zone's local time. Python's text was 17.0, 1e+16, True,
    b'\\xff\\x00', ... Lists and records, which have no such text, stay as they are."""
    return keys.select(
        col(name) if dtype.is_nested() or dtype == pl.Object else dtypes.text_expr(col(name), dtype).alias(name)
        for name, dtype in keys.schema.items()
    )


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

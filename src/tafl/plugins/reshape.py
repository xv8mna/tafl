"""Reshaping: pivot (values become columns) and unpivot (columns become rows)."""

from __future__ import annotations

from decimal import Decimal

import polars as pl

from tafl.api import ActionError, P, action, col, cols, dtypes, selector

_FUNCTIONS = [
    ("sum", "Sum"),
    ("count", "Count values (not empty)"),
    ("mean", "Average"),
    ("min", "Minimum"),
    ("max", "Maximum"),
    ("first", "First value"),
    ("last", "Last value"),
]
_FUNCTION_LABELS = dict(_FUNCTIONS)
_ORDER = [("sorted", "Sorted (A→Z, 1→9)"), ("first", "As they first appear")]
_EMPTY = "(empty)"  # the new column of the empty values
_MAX_COLUMNS = 1_000  # more is almost always the wrong column (an ID, an amount), and slow to show
_MAX_CELLS = 50_000_000  # result rows x new columns: about 400 MB as whole numbers
# Pivot makes this many new columns at a time. When Polars expects few row keys, every thread works
# out every new column apart, with up to 0.4 MB each: 5.6 GB for 1,000 new columns on 16 threads,
# however small the table and the result are. The two limits above can't see that.
_PIVOT_PIECE = 32
# Unpivot: result rows x columns, about 2 GB as whole numbers (a long table is cheaper to show than
# a wide one). It holds Pivot's widest result turned back into rows (150,000,000 cells).
_MAX_LONG_CELLS = 250_000_000
_PIECE = 2**18  # Unpivot puts this many result rows at a time in order (all at once needs twice the memory)
_NARROW = (pl.Int8, pl.Int16, pl.Int32, pl.UInt8, pl.UInt16, pl.UInt32, pl.Boolean)  # summed as Int64
_EXACT = 10**38  # the units of an exact decimal (its digits without the point) are below this
_PART = 10**19  # a sum of exact decimals is checked in two parts: see _decimal_parts
_WIDE = (pl.Int128, pl.UInt128)  # whole numbers of 128 bits (only a plugin makes them): their sums are checked in parts too
_PER_DAY = {"ns": 86_400 * 10**9, "us": 86_400 * 10**6, "ms": 86_400 * 10**3}  # durations


@action(
    "reshape.pivot",
    "Pivot",
    category="Columns",
    params=[
        P.Columns(
            "rows",
            "Row keys (empty = one total row)",
            help="One row for each combination of their values, in the order they first appear.",
        ),
        P.Column("column", "Column names from", help="Each value of this column becomes a new column."),
        P.Column("value", "Values from", help="The values that fill the new columns."),
        P.Choice("function", "Function", _FUNCTIONS, help="How the values of the rows that meet in one cell are combined."),
        P.Choice("order", "Order of the new columns", _ORDER),
    ],
    summary=lambda p: f"Pivot {p.column} into columns: {_FUNCTION_LABELS[p.function].split(' (')[0]} of {p.value}"
    + (f" by {', '.join(p.rows)}" if p.rows else ""),
    checks=lambda p, vc: _pivot_problems(p),
)
def pivot(df, p, ctx):
    """Turns the values of one column into columns, like an Excel pivot table without totals: one row
    for each combination of the row keys, one column for each value, and in each cell the values of
    the rows that meet there, added up (or counted, averaged, …).

    The new columns are named after the values: text as it is, numbers as 1234 or 1.5, dates as
    2025-12-31, true/false as true and false; empty values go into one column (empty). They are
    sorted, or come in the order they first appear; a name that exists already gets " (2)", or the
    next number that isn't the name of another value. Rows come in the order their keys first
    appear. A cell without rows stays empty, and so does a sum of only empty values. Count counts
    the values that are not empty; First and Last take the first and last value that is not
    empty (text with nothing but spaces counts here, as in Aggregate). At most
    1,000 new columns, and at most 50,000,000 cells (rows × new columns).
    """
    rows = list(dict.fromkeys(p.rows))
    names_type, value_type = df.schema[p.column], df.schema[p.value]
    _check_value_type(p.function, p.value, value_type)
    if not _can_be_text(names_type) or names_type == pl.Binary:
        raise ActionError(f"The values of “{p.column}” ({dtypes.type_label(names_type)}) can't become column names")
    if df.height == 0:
        ctx.log("The table has no rows, so there are no new columns")
        return df.select(cols(rows))

    # in the order they first appear, under a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
    distinct = df[p.column].alias("value").unique(maintain_order=True)
    empty = _is_empty(distinct)
    values = distinct.filter(~empty)
    has_empty = values.len() < distinct.len()
    count = values.len() + has_empty
    if count > _MAX_COLUMNS:
        raise ActionError(
            f"“{p.column}” has {count:,} distinct values: too many new columns (at most {_MAX_COLUMNS:,}). "
            "Choose a column with fewer values, or group them first, e.g. dates by month with a formula."
        )
    if df.height * count > _MAX_CELLS:  # the result can't have more rows than the table
        groups = df.select(pl.struct(cols(rows)).n_unique()).item() if rows else 1
        if groups * count > _MAX_CELLS:
            raise ActionError(
                f"The result would have {groups:,} rows and {count:,} new columns: {groups * count:,} cells "
                f"(at most {_MAX_CELLS:,}). Choose fewer row keys, or a column with fewer distinct values."
            )

    if p.order == "sorted":
        values = _sorted(values)
        where = values.len()  # the column of the empty values comes last
    else:  # where the first empty value is: after the values before it
        where = int(empty.arg_true()[0]) if has_empty else values.len()
    labels = _as_text(values).to_list()
    if has_empty:
        labels.insert(where, _EMPTY)
    names = _free_names(labels, rows, ctx, where if has_empty else None)
    if apart := _apart_by_spaces(names):
        (one, other), more = apart[0], len(apart) - 1
        also = f" (and {more:,} more pair{'s' if more > 1 else ''})" if more else ""
        ctx.log(
            f"The new columns “{one}” and “{other}” differ only in spaces at the start or end{also}: "
            "Clean text before this step makes them one column"
        )
    # The pivot is on the place of each new column (0, 1, 2, …): Polars is much faster with such a
    # key than with long names. The columns get their names at the end; until then the row keys
    # have working names, which can't be the text of a number.
    places = list(range(len(names)))
    old, new = values, places
    if has_empty:
        empties = distinct.filter(empty)
        old = pl.concat([values, empties])
        new = [*places[:where], *places[where + 1 :], *[where] * empties.len()]
    keys = [f"row {i}" for i in range(len(rows))]
    work = df.select(
        *(col(row).alias(key) for row, key in zip(rows, keys, strict=True)),
        col(p.column).replace_strict(old, new, return_dtype=pl.UInt32).alias("name"),
        _prepared(col(p.value), p.function, value_type).alias("value"),
    )
    summed, exact = work.schema["value"], None
    if p.function == "sum" and (summed in (pl.Int64, pl.UInt64, *_WIDE, pl.Duration) or isinstance(summed, pl.Decimal)):
        if _check_sums(work, names, p.value) and isinstance(summed, pl.Decimal):
            # Polars stops at a sum beyond 38 digits, also on the way to one that fits: their units
            # (Int128) add up without stopping, and every sum fits
            exact, work = summed, work.with_columns(pl.col("value").to_physical())
    pieces = [
        work.pivot(
            on="name",
            on_columns=pl.Series(places[start : start + _PIVOT_PIECE], dtype=pl.UInt32),
            index=keys,
            values="value",
            aggregate_function=_aggregation(p.function),
            maintain_order=True,
        )
        for start in range(0, len(places), _PIVOT_PIECE)
    ]
    out = pl.concat([pieces[0], *(piece.drop(keys) for piece in pieces[1:])], how="horizontal")  # same rows, same order
    if exact is not None:
        out = out.with_columns(_from_units(column, exact.scale) for column in cols(out.columns[len(keys) :]))
    out.columns = [*rows, *names]
    return out


def _pivot_problems(p) -> list[str]:
    rows = p.rows if isinstance(p.rows, list) else []
    problems = []
    if p.column and p.column in rows:
        problems.append(f"Row keys: “{p.column}” gives the column names, so it can't be a row key too")
    if p.value and p.value in rows and p.value != p.column:
        problems.append(f"Row keys: “{p.value}” holds the values, so it can't be a row key too")
    return problems


def _check_value_type(function: str, name: str, dtype: pl.DataType) -> None:
    """Sum needs numbers; Average also takes dates (as Aggregate). Minimum and Maximum can't compare
    lists and records (Polars gives empty cells)."""
    if function in ("min", "max") and not _can_be_text(dtype):
        raise ActionError(f"“{name}” holds {dtypes.type_label(dtype)} values, which {_FUNCTION_LABELS[function]} can't compare")
    if function not in ("sum", "mean") or dtype == pl.Null:
        return
    if dtype.is_numeric() or dtype in (pl.Boolean, pl.Duration):
        return
    if function == "mean" and dtype.is_temporal():
        return
    kind = "text" if dtype == pl.String else dtypes.with_article(f"{dtypes.type_label(dtype)} column")
    needs = "a number column" if function == "sum" else "a number or date column"
    raise ActionError(f"{_FUNCTION_LABELS[function]} needs {needs}; “{name}” is {kind}")


def _prepared(column: pl.Expr, function: str, dtype: pl.DataType) -> pl.Expr:
    """The values in the type their cells are calculated in: a sum of narrow whole numbers or of
    true/false as Int64 (a sum of Int32 would overflow), decimal numbers as Float64."""
    if function == "sum" and (dtype in _NARROW or dtype == pl.Null):
        return column.cast(pl.Int64)
    if function in ("sum", "mean") and dtype in (pl.Float32, pl.Null):
        return column.cast(pl.Float64)
    return column


def _sums_may_overflow(s: pl.Series) -> bool:
    """Whether a sum of these whole numbers (or durations) might be beyond Int64 (beyond their
    type for 128 bits), or one of exact decimals beyond 38 digits."""
    limit = _EXACT - 1 if isinstance(s.dtype, pl.Decimal) else _whole_range(s.dtype)[1] if s.dtype in _WIDE else 2**63 - 1
    s = s.to_physical()
    low, high = s.min(), s.max()
    return low is not None and max(-low, high) * s.len() > limit


def _whole_range(dtype: pl.DataType) -> tuple[int, int]:
    """The smallest and the biggest value of a type of whole numbers (of a duration: Int64)."""
    if dtype == pl.UInt64:
        return 0, 2**64 - 1
    if dtype == pl.Int128:
        return -(2**127), 2**127 - 1
    if dtype == pl.UInt128:
        return 0, 2**128 - 1
    return -(2**63), 2**63 - 1


def _check_sums(work: pl.DataFrame, names: list[str], value: str) -> bool:
    """Polars lets a sum of whole numbers or durations overflow (it gives a wrong number), and one
    of exact decimals beyond 38 digits too. So when that might be, the sum of each cell is worked
    out wider first (as Int128, or in two parts), and one beyond the type of the values stops the
    step. ``work`` has the row keys, the place of the new column ("name") and the values
    ("value"). Returns whether the sums were checked."""
    if not _sums_may_overflow(work["value"]):
        return False
    dtype = work.schema["value"]
    if isinstance(dtype, pl.Decimal):
        sums = work.group_by(pl.exclude("value"), maintain_order=True).agg(_decimal_parts(pl.col("value")).alias("value"))
        cells = zip(sums["name"], _decimal_units(sums["value"]), strict=True)
        if beyond := [(place, total) for place, total in cells if abs(total) >= _EXACT]:
            place, total = min(beyond, key=lambda cell: cell[0])  # in the first new column
            raise ActionError(
                f"The sum of “{value}” in “{names[place]}” is {Decimal(f'{total}E-{dtype.scale}'):,}: too many digits "
                f"for an exact decimal (at most 38). Change “{value}” to a decimal number before this step."
            )
        return True
    low, high = _whole_range(dtype)
    if dtype in _WIDE:  # Int128 can't hold their sums either: in two parts, as those of exact decimals
        sums = work.group_by(pl.exclude("value"), maintain_order=True).agg(_decimal_parts(pl.col("value"), dtype).alias("value"))
        cells = zip(sums["name"], _decimal_units(sums["value"]), strict=True)
        if beyond := [(place, total) for place, total in cells if not low <= total <= high]:
            place, total = min(beyond, key=lambda cell: cell[0])  # in the first new column
            raise ActionError(_too_big(value, names[place], total, low, high))
        return True
    exact = pl.col("value").to_physical().cast(pl.Int128).sum()
    sums = work.group_by(pl.exclude("value"), maintain_order=True).agg(exact)
    beyond = sums.filter((pl.col("value") < low) | (pl.col("value") > high))
    if beyond.height:
        first = beyond.sort("name", maintain_order=True).row(0, named=True)  # in the first new column
        total, name = first["value"], names[first["name"]]
        if dtype == pl.Duration:
            per_day = _PER_DAY[dtype.time_unit]
            raise ActionError(
                f"The sum of “{value}” in “{name}” is {'-' if total < 0 else ''}{abs(total) // per_day:,} days: "
                f"too long for a duration (at most {high // per_day:,} days). Change “{value}” to hours "
                f"before this step, with the formula EPOCH([{value}]) / 3600."
            )
        raise ActionError(_too_big(value, name, total, low, high))
    return True


def _too_big(value: str, name: str, total: int, low: int, high: int) -> str:
    limit = f"big for a whole number (at most {high:,})" if total > high else f"small for a whole number (at least {low:,})"
    return f"The sum of “{value}” in “{name}” is {total:,}: too {limit}. Change “{value}” to a decimal number before this step."


def _decimal_parts(column: pl.Expr, dtype: pl.DataType | None = None) -> pl.Expr:
    """A sum of exact decimals (or of whole numbers of 128 bits) as two sums that can't overflow:
    their units (the digits without the point) are split at 19 digits, and each part is added up
    as Int128."""
    units = column.to_physical()
    if dtype == pl.UInt128:  # beyond Int128: split in its own type
        part = pl.lit(_PART, pl.UInt128)
        return pl.struct((units // part).cast(pl.Int128).sum().alias("high"), (units % part).cast(pl.Int128).sum().alias("low"))
    high = units // pl.lit(_PART, pl.Int128)
    return pl.struct(high.sum().alias("high"), (units - high * pl.lit(_PART, pl.Int128)).sum().alias("low"))


def _decimal_units(parts: pl.Series) -> list[int]:
    """The units of each sum from ``_decimal_parts`` (Python's whole numbers have no limit)."""
    return [high * _PART + low for high, low in zip(parts.struct.field("high"), parts.struct.field("low"), strict=True)]


def _from_units(units: pl.Expr, scale: int) -> pl.Expr:
    """Exact decimals of this scale from their units (Int128, within 38 digits)."""
    return units.cast(pl.Decimal(38, 0)) * pl.lit(Decimal(1).scaleb(-scale))


def _apart_by_spaces(names: list[str]) -> list[tuple[str, str]]:
    """Pairs of names that differ only in spaces at their start or end (they look the same)."""
    first: dict[str, str] = {}
    pairs = []
    for name in names:
        key = name.strip()
        if key in first:
            pairs.append((first[key], name))
        else:
            first[key] = name
    return pairs


def _aggregation(function: str) -> pl.Expr:
    """The cell of each row key and new column. Polars works out cells without rows too: they
    must stay empty (its sum and count give 0 there), and so must a sum of only empty values."""
    value = pl.element()
    match function:
        case "sum":
            return pl.when(value.count() > 0).then(value.sum())
        case "count":
            return pl.when(value.len() > 0).then(value.count()).cast(pl.Int64)
        case "mean":
            return value.mean()
        case "min":
            return value.min()
        case "max":
            return value.max()
        case "first":
            return value.drop_nulls().first()
        case "last":
            return value.drop_nulls().last()
    raise ActionError(f"Unknown function '{function}'")


def _sorted(values: pl.Series) -> pl.Series:
    """Values (without empty ones) in their order: numbers by size, dates by time, false before
    true, text as Sort does (values with the same key keep their order)."""
    if values.dtype in (pl.String, pl.Categorical):
        keys = dtypes.text_sort_key(values.cast(pl.String))
        return pl.DataFrame({"value": values, "key": keys}).sort("key", maintain_order=True)["value"]
    return values.sort()


def _free_names(names: list[str], taken, ctx, empty: int | None = None) -> list[str]:
    """The names, each one free: a name that the table or an earlier name has gets " (2)", " (3)", …,
    but never the name of another value. So a value always has its own name, also on a day when
    the value that took a number isn't there. ``empty``: the place of the column (empty), which
    gets a number too when a value is named (empty)."""
    taken = set(taken)
    values = {name for place, name in enumerate(names) if place != empty}
    result = []
    for place, name in enumerate(names):
        free = name
        if name in taken or (place == empty and name in values):
            free = _free_name(name, taken | values, " ")
            ctx.log(f"Two columns would be named “{name}”: the new one is named “{free}”")
        taken.add(free)
        result.append(free)
    return result


def _free_name(name: str, taken, space: str = "") -> str:
    """``name``, or ``name (2)``, ``name (3)``, … if it is taken."""
    candidate, count = name, 1
    while candidate in taken:
        count += 1
        candidate = f"{name}{space}({count})"
    return candidate


@action(
    "reshape.unpivot",
    "Unpivot",
    category="Columns",
    params=[
        P.Choice("mode", "Turn into rows", [("these", "These columns"), ("others", "All columns except these")]),
        P.Columns("columns", "Columns", required=True),
        P.Text(
            "name",
            "New column for the names",
            default="Column",
            required=True,
            help="Holds the name of the column each value comes from.",
        ),
        P.Text("value", "New column for the values", default="Value", required=True),
        P.Bool(
            "drop_empty",
            "Leave out empty values",
            default=False,
            help="Off: each row gives one row for every column, also for an empty cell.",
        ),
    ],
    summary=lambda p: f"Unpivot {'all but ' if p.mode == 'others' else ''}{', '.join(p.columns)} → "
    f"{p.name.strip() or '?'}, {p.value.strip() or '?'}",
    checks=lambda p, vc: _unpivot_problems(p),
)
def unpivot(df, p, ctx):
    """Turns columns into rows, the opposite of Pivot: each row becomes one row for each chosen
    column, with the column's name in a new name column and its value in a new value column; the
    other columns are repeated. Example: Customer, Jan, Feb, Mar becomes Customer, Month, Amount with
    three rows per customer.

    The rows keep their order, and the values of a row come in the order of the columns. Columns of
    one type keep it. Whole and decimal numbers become decimal numbers, whole numbers and exact
    decimals exact decimals, dates and date-times date-times, when every value stays as it is
    (whole numbers beyond 9,223,372,036,854,775,807 become exact decimals). Any
    other mix, also decimal numbers with exact decimals, becomes text (numbers as 1.5, exact
    decimals as 2.00, dates as 2025-12-31), and the step says so. A column whose values are all
    empty doesn't count, whatever its type. At most 250,000,000 cells in the result (rows ×
    columns).
    """
    name, value = p.name.strip(), p.value.strip()
    chosen = list(dict.fromkeys(p.columns))
    if p.mode == "these":
        turned = chosen
    else:
        turned = [column for column in df.columns if column not in chosen]
        if not turned:
            raise ActionError("All columns stay in the table: no column is left to turn into rows")
    stay = [column for column in df.columns if column not in turned]
    for new in (name, value):
        if new in stay:
            raise ActionError(f"A column named “{new}” stays in the table: choose another name for the new column")
    schema = df.schema
    for column in turned:
        if not _can_be_text(schema[column]):
            kind = dtypes.type_label(schema[column])
            raise ActionError(f"“{column}” holds {kind} values, which can't be turned into rows")
    count = sum(df.height - df[c].null_count() for c in turned) if p.drop_empty else df.height * len(turned)
    width = len(stay) + 2  # the two new columns
    if count * width > _MAX_LONG_CELLS:
        raise ActionError(
            f"The result would have {count:,} rows and {width:,} columns: {count * width:,} cells "
            f"(at most {_MAX_LONG_CELLS:,}). Turn fewer columns into rows, or remove columns or rows before this step."
        )

    # The type of the value column: a column whose values are all empty (or left out) doesn't count
    common = dtypes.common_type([_deciding(df[c], p.drop_empty) for c in turned])
    target, counted = common.dtype, dict(zip(turned, common.counted, strict=True))
    if target == pl.String and any(counted[c] and schema[c] != pl.String for c in turned):  # a mix
        if common.lost is not None:  # digits or a date out of range
            ctx.log(f"“{value}” is text, so that no value of “{turned[common.lost]}” changes")
        else:
            ctx.log(f"“{value}” is text: the columns have different types ({_type_groups(df, [c for c in turned if counted[c]])})")
    casts = [
        (dtypes.to_common_type(col(c), schema[c], target) if counted[c] else pl.lit(None, dtype=target)).alias(c)
        for c in turned
        if schema[c] != target
    ]

    # Working names: a new column may have the name of a turned column, which is there until the end
    taken = set(df.columns)
    names = _free_name("__names__", taken)
    values = _free_name("__values__", taken)
    size = max(1, _PIECE // len(turned))  # rows of the table in one piece
    df = df.with_columns(casts)  # once: text_expr runs Python for some decimal numbers, per call
    pieces = []
    for start in range(0, max(df.height, 1), size):
        work = df.slice(start, size)
        out = work.unpivot(on=selector(turned), index=selector(stay), variable_name=names, value_name=values)
        rows, columns = work.height, len(turned)
        if rows > 1 and columns > 1:  # Polars gives all rows of one column, then of the next
            place = pl.int_range(rows * columns, dtype=pl.UInt32, eager=True)  # in the result, row by row
            out = out[(place % columns) * rows + place // columns]
        if p.drop_empty:
            out = out.filter(~_is_empty(out[values]))
        pieces.append(out)
    return pl.concat(pieces, rechunk=False).rename({names: name, values: value})


def _unpivot_problems(p) -> list[str]:
    problems = []
    for text, label in ((p.name, "New column for the names"), (p.value, "New column for the values")):
        if isinstance(text, str) and text and not text.strip():  # an empty one is reported as required already
            problems.append(f"{label}: required")
    if isinstance(p.name, str) and isinstance(p.value, str) and p.name.strip() and p.name.strip() == p.value.strip():
        problems.append("The two new columns need different names")
    return problems


def _deciding(s: pl.Series, drop_empty: bool) -> pl.Series:
    """The values of a turned column that decide the type of the value column: with "Leave out
    empty values", not its text with nothing but spaces either."""
    if drop_empty and _is_text(s.dtype):
        s = s.alias("value")  # a fixed name: Polars' Series methods would read a name like ^a.*$ as a pattern
        return s.filter(~_is_empty(s))
    return s


def _type_groups(df: pl.DataFrame, turned: list[str]) -> str:
    """The turned columns by type, in table order, e.g. "integer: Jan, Feb, Mar and 9 more; text: Note"."""
    groups: dict[str, list[str]] = {}
    chosen = set(turned)
    for column, dtype in df.schema.items():
        if column in chosen and dtype != pl.Null:
            groups.setdefault(dtypes.type_word(dtype), []).append(column)
    return "; ".join(
        f"{word}: {', '.join(names[:3])}" + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        for word, names in groups.items()
    )


# --- values as text --------------------------------------------------------------------------


def _can_be_text(dtype: pl.DataType) -> bool:
    return not (dtype.is_nested() or dtype == pl.Object)


def _is_text(dtype: pl.DataType) -> bool:
    return dtype in (pl.String, pl.Categorical) or isinstance(dtype, pl.Enum)


def _is_empty(s: pl.Series) -> pl.Series:
    """Empty values, and text with nothing but spaces (as "is empty" in Filter rows)."""
    if _is_text(s.dtype):
        text = s.cast(pl.String)
        return (text.is_null() | (text.str.strip_chars() == "")).alias(s.name)
    return s.is_null()


def _as_text(s: pl.Series) -> pl.Series:
    return s.to_frame("value").select(dtypes.text_expr(pl.col("value"), s.dtype))["value"]

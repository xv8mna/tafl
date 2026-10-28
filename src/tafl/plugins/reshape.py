"""Reshaping: pivot (values become columns) and unpivot (columns become rows)."""

from __future__ import annotations

import polars as pl

from tafl.api import ActionError, P, action, dtypes

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
# Unpivot: result rows x columns, about 2 GB as whole numbers (a long table is cheaper to show than
# a wide one). It holds Pivot's widest result turned back into rows (150,000,000 cells).
_MAX_LONG_CELLS = 250_000_000
_PIECE = 2**18  # Unpivot puts this many result rows at a time in order (all at once needs twice the memory)
_NARROW = (pl.Int8, pl.Int16, pl.Int32, pl.UInt8, pl.UInt16, pl.UInt32, pl.Boolean)  # summed as Int64
_UNITS = ("ns", "us", "ms")  # of date-times and durations, the finest first
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
        return df.select(rows)

    distinct = df[p.column].unique(maintain_order=True)  # in the order they first appear
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
        groups = df.select(pl.struct(rows).n_unique()).item() if rows else 1
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
        *(pl.col(row).alias(key) for row, key in zip(rows, keys, strict=True)),
        pl.col(p.column).replace_strict(old, new, return_dtype=pl.UInt32).alias("name"),
        _prepared(pl.col(p.value), p.function, value_type).alias("value"),
    )
    if p.function == "sum" and work.schema["value"] in (pl.Int64, pl.UInt64, pl.Duration):
        _check_sums(work, names, p.value)
    out = work.pivot(
        on="name",
        on_columns=pl.Series(places, dtype=pl.UInt32),
        index=keys,
        values="value",
        aggregate_function=_aggregation(p.function),
        maintain_order=True,
    )
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
    kind = "text" if dtype == pl.String else f"a {dtypes.type_label(dtype)} column"
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
    """Whether a sum of these whole numbers (or durations) might be beyond Int64."""
    s = s.to_physical()
    low, high = s.min(), s.max()
    return low is not None and max(-low, high) * s.len() > 2**63 - 1


def _check_sums(work: pl.DataFrame, names: list[str], value: str) -> None:
    """Polars lets a sum of whole numbers or durations overflow (it gives a wrong number). So when
    that might be, the sum of each cell is worked out as Int128 first, and one beyond the type of
    the values stops the step. ``work`` has the row keys, the place of the new column ("name") and
    the values ("value")."""
    if not _sums_may_overflow(work["value"]):
        return
    dtype = work.schema["value"]
    exact = pl.col("value").to_physical().cast(pl.Int128).sum()
    sums = work.group_by(pl.exclude("value"), maintain_order=True).agg(exact)
    low, high = (0, 2**64 - 1) if dtype == pl.UInt64 else (-(2**63), 2**63 - 1)
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
        limit = f"big for a whole number (at most {high:,})" if total > high else f"small for a whole number (at least {low:,})"
        raise ActionError(
            f"The sum of “{value}” in “{name}” is {total:,}: too {limit}. "
            f"Change “{value}” to a decimal number before this step."
        )


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
    decimals exact decimals, dates and date-times date-times, when every value stays as it is. Any
    other mix, also decimal numbers with exact decimals, becomes text (numbers as 1.5, exact
    decimals as 2.00, dates as 2025-12-31), and the step says so. At most 250,000,000 cells in the
    result (rows × columns).
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

    target, lost = _value_type(df, turned)
    if target == pl.String and any(schema[c] not in (pl.String, pl.Null) for c in turned):  # a mix
        if lost is not None:
            ctx.log(f"“{value}” is text, so that no value of “{lost}” changes")  # digits or a date out of range
        else:
            ctx.log(f"“{value}” is text: the columns have different types ({_type_groups(df, turned)})")
        casts = [dtypes.text_expr(pl.col(c), schema[c]).alias(c) for c in turned if schema[c] != pl.String]
    else:
        casts = [_cast(pl.col(c), schema[c], target).alias(c) for c in turned if schema[c] != target]

    # Working names: a new column may have the name of a turned column, which is there until the end
    taken = set(df.columns)
    names = _free_name("__names__", taken)
    values = _free_name("__values__", taken)
    size = max(1, _PIECE // len(turned))  # rows of the table in one piece
    df = df.with_columns(casts)  # once: text_expr runs Python for some decimal numbers, per call
    pieces = []
    for start in range(0, max(df.height, 1), size):
        work = df.slice(start, size)
        out = work.unpivot(on=turned, index=stay, variable_name=names, value_name=values)
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


def _value_type(df: pl.DataFrame, turned: list[str]) -> tuple[pl.DataType, str | None]:
    """The type of the new value column, and the first column whose values it would change
    (then it is text). Columns with nothing but empty values (type Null) don't count."""
    schema = df.schema
    types = list(dict.fromkeys(schema[c] for c in turned if schema[c] != pl.Null))
    if not types:
        return pl.Null, None
    if len(types) == 1:
        return types[0], None
    first_lost = None
    for common in _common_types(types):
        lost = next((c for c in turned if schema[c] not in (common, pl.Null) and not _keeps_values(df[c], common)), None)
        if lost is None:
            return common, None
        first_lost = first_lost or lost
    return pl.String, first_lost


def _common_types(types: list[pl.DataType]) -> list[pl.DataType]:
    """Types that can hold the values of all these types (if the values fit), the best first."""
    if all(t.is_integer() for t in types):
        return [pl.Int64]
    if all(t.is_integer() or t.is_float() for t in types):
        return [pl.Float64]
    if all(t.is_integer() or isinstance(t, pl.Decimal) for t in types):
        return [pl.Decimal(38, max(t.scale for t in types if isinstance(t, pl.Decimal)))]
    # Dates and date-times of one time zone, or durations: date-times (durations) in the finest
    # unit of the columns, else in a coarser one (a nanosecond one ends in the year 2262)
    times = [t for t in types if t != pl.Date]
    if all(isinstance(t, pl.Datetime) for t in times) and len({t.time_zone for t in times}) == 1:
        return [pl.Datetime(unit, times[0].time_zone) for unit in _coarser(times)]
    if all(isinstance(t, pl.Duration) for t in types):
        return [pl.Duration(unit) for unit in _coarser(types)]
    return []


def _coarser(types: list[pl.DataType]) -> tuple[str, ...]:
    """The finest unit of these date-times or durations, and the coarser ones."""
    return _UNITS[min(_UNITS.index(t.time_unit) for t in types) :]


def _keeps_values(s: pl.Series, target: pl.DataType) -> bool:
    """Whether the values of ``s`` come back unchanged from ``target``."""
    if s.dtype.is_float() and target == pl.Float64:
        return True
    if s.dtype.is_integer() and target == pl.Float64:  # all whole numbers up to 2^53 are exact
        low, high = s.min(), s.max()
        return low is None or (-(2**53) <= low and high <= 2**53)
    frame = s.to_frame("value")
    back = _cast(pl.col("value"), s.dtype, target)
    back = back.dt.date() if s.dtype == pl.Date else back.cast(s.dtype, strict=False)
    return frame.select((pl.col("value").is_null() | (back == pl.col("value")).fill_null(False)).all()).item()


def _cast(column: pl.Expr, dtype: pl.DataType, target: pl.DataType) -> pl.Expr:
    """``column`` as ``target``, empty where a value doesn't fit. A date becomes midnight of the
    date-time's time zone (a plain cast would take midnight in UTC)."""
    if dtype == pl.Date and isinstance(target, pl.Datetime) and target.time_zone:
        local = column.cast(pl.Datetime(target.time_unit))
        return local.dt.replace_time_zone(target.time_zone, ambiguous="earliest", non_existent="null")
    return column.cast(target, strict=False)


def _type_groups(df: pl.DataFrame, turned: list[str]) -> str:
    """The turned columns by type, in table order, e.g. "integer: Jan, Feb, Mar and 9 more; text: Note"."""
    groups: dict[str, list[str]] = {}
    chosen = set(turned)
    for column, dtype in df.schema.items():
        if column in chosen and dtype != pl.Null:
            groups.setdefault(_type_word(dtype), []).append(column)
    return "; ".join(
        f"{word}: {', '.join(names[:3])}" + (f" and {len(names) - 3} more" if len(names) > 3 else "")
        for word, names in groups.items()
    )


def _type_word(dtype: pl.DataType) -> str:
    """The word for a type in a message; an exact decimal and a date-time with a time zone are told apart."""
    if isinstance(dtype, pl.Decimal):
        return "exact decimal"
    if isinstance(dtype, pl.Datetime) and dtype.time_zone:
        return f"date-time ({dtype.time_zone})"
    return dtypes.type_label(dtype)


# --- values as text --------------------------------------------------------------------------


def _can_be_text(dtype: pl.DataType) -> bool:
    return not (dtype.is_nested() or dtype == pl.Object)


def _is_empty(s: pl.Series) -> pl.Series:
    """Empty values, and text with nothing but spaces (as "is empty" in Filter rows)."""
    if s.dtype in (pl.String, pl.Categorical) or isinstance(s.dtype, pl.Enum):
        text = s.cast(pl.String)
        return (text.is_null() | (text.str.strip_chars() == "")).alias(s.name)
    return s.is_null()


def _as_text(s: pl.Series) -> pl.Series:
    return s.to_frame("value").select(dtypes.text_expr(pl.col("value"), s.dtype))["value"]

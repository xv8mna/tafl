"""Calculations: formula columns, numbering rows, aggregating."""

from __future__ import annotations

import polars as pl

from tafl.api import ActionError, P, action, dtypes, expressions

_ROW = "__etl_row__"
_ORDER = [("asc", "Ascending"), ("desc", "Descending")]
_FUNCTIONS = [
    ("sum", "Sum"),
    ("mean", "Average"),
    ("min", "Minimum"),
    ("max", "Maximum"),
    ("median", "Median"),
    ("count", "Count values (not empty)"),
    ("count_distinct", "Count distinct values"),
    ("count_rows", "Count rows"),
    ("first", "First value"),
    ("last", "Last value"),
    ("join", "Join texts"),
]
_FUNCTION_LABELS = dict(_FUNCTIONS)


@action(
    "calc.columns",
    "Calculate columns",
    category="Calculate",
    params=[
        P.List(
            "columns",
            "New columns",
            fields=[
                P.Text("name", "Column name", required=True),
                P.Expression("formula", "Formula", required=True, placeholder="[Price] * [Quantity]", creates="name"),
            ],
            min_items=1,
        ),
    ],
    summary=lambda p: "Calculate " + ", ".join(c.name for c in p.columns),
)
def calculate_columns(df, p, ctx):
    """Adds columns calculated with SQL-style formulas. A column from an earlier step with the
    same name is replaced; the new columns of one step need different names. Each formula can
    use the columns calculated above it.

    Examples: [Price] * [Quantity] · ROUND([Net] * 1.19, 2) · UPPER(TRIM([Name])) ·
    [First name] || ' ' || [Last name] · IIF([Amount] > 1000, 'big', 'small') ·
    CASE WHEN [Country] = 'DE' THEN 'Germany' ELSE 'Other' END · YEAR([Date]) ·
    [Date] + 30 (days) · SUM([Amount]) OVER (PARTITION BY [Customer])
    """
    names = [c.name.strip() for c in p.columns]
    for number, name in enumerate(names, start=1):
        first = names.index(name) + 1
        if name and first != number:  # the second would silently replace the first
            raise ActionError(
                f"Two new columns are named “{name}” (rows {first} and {number}). "
                "Give each one its own name."
            )
    return expressions.add_columns(df, list(zip(names, (c.formula for c in p.columns), strict=True)))


@action(
    "calc.enumerate",
    "Number rows",
    category="Calculate",
    params=[
        P.Text("name", "New column", default="No", required=True),
        P.Choice(
            "mode",
            "Numbering",
            [("rows", "Number the rows: 1, 2, 3, …"), ("groups", "Number the groups: equal values get the same number")],
        ),
        P.Columns("group_by", "Groups (columns)"),
        P.List(
            "order_by",
            "In this order (empty = current row order)",
            fields=[P.Column("column", "Column"), P.Choice("order", "Order", _ORDER)],
        ),
        P.Int("start", "Start at", default=1),
        P.Int("step", "Step", default=1),
    ],
    summary=lambda p: f"Number {'rows' if p.mode == 'rows' else 'groups'} → {p.name}"
    + (f" per {', '.join(p.group_by)}" if p.group_by and p.mode == "rows" else "")
    + (f" by {', '.join(p.group_by)}" if p.group_by and p.mode == "groups" else ""),
)
def enumerate_rows(df, p, ctx):
    """Adds a running number.

    Number the rows: 1, 2, 3, … over the whole table, or restarting for each group (like
    ROW_NUMBER() OVER (PARTITION BY … ORDER BY …) in SQL). Number the groups: every distinct
    combination of the group columns gets its own number (like DENSE_RANK()).
    The order of the rows in the table does not change.
    """
    if p.mode == "groups" and not p.group_by:
        raise ActionError("Choose the columns that form the groups")
    partition = p.group_by if p.mode == "rows" else []
    work = df.with_row_index(_ROW)
    if p.order_by:
        text = {name for name, dtype in df.schema.items() if dtype == pl.String}  # in the order of Sort
        work = work.sort(
            [*partition, *(dtypes.text_sort_key(pl.col(o.column)) if o.column in text else o.column for o in p.order_by)],
            descending=[*([False] * len(partition)), *(o.order == "desc" for o in p.order_by)],
            nulls_last=True,
            maintain_order=True,
        )
    if p.mode == "rows":
        counter = pl.int_range(pl.len(), dtype=pl.Int64)
        if partition:
            counter = counter.over(partition)
        work = work.with_columns((counter * p.step + p.start).alias(p.name))
    else:
        groups = work.select(p.group_by).unique(maintain_order=True)
        groups = groups.with_columns((pl.int_range(pl.len(), dtype=pl.Int64) * p.step + p.start).alias(p.name))
        work = work.drop(p.name, strict=False).join(
            groups, on=p.group_by, how="left", nulls_equal=True, maintain_order="left"
        )
    return work.sort(_ROW).drop(_ROW)


@action(
    "calc.aggregate",
    "Aggregate",
    category="Calculate",
    params=[
        P.Columns("group_by", "Group by (empty = one total row)"),
        P.List(
            "calculations",
            "Calculate",
            fields=[
                P.Choice("function", "Function", _FUNCTIONS),
                P.Column("column", "Column", visible_if={"function": tuple(f for f, _ in _FUNCTIONS if f != "count_rows")}),
                P.Text("name", "Result name (empty = automatic)"),
            ],
            min_items=1,
        ),
        P.Text("separator", "Separator for 'Join texts'", default=", "),
    ],
    summary=lambda p: "Aggregate"
    + (f" by {', '.join(p.group_by)}" if p.group_by else "")
    + ": "
    + ", ".join(_result_name(c) for c in p.calculations),
)
def aggregate(df, p, ctx):
    """Groups rows with equal values in the group columns and calculates one result row per
    group, like GROUP BY in SQL. Groups appear in the order they first occur."""
    aggregations = []
    wide = []  # sums of whole numbers (durations) that might be beyond Int64: added up as Int128, checked below
    for calc in p.calculations:
        if calc.function != "count_rows" and not calc.column:
            raise ActionError(f"Choose a column for '{_FUNCTION_LABELS[calc.function]}'")
        dtype = df.schema.get(calc.column)
        _check_type(calc, dtype)
        aggregation = _aggregation(calc, p.separator)
        if calc.function == "sum" and dtype in (pl.Int64, pl.UInt64, pl.Duration) and _sums_may_overflow(df[calc.column]):
            aggregation = pl.col(calc.column).to_physical().cast(pl.Int128).sum()
            wide.append((_result_name(calc), calc.column, dtype))
        aggregations.append(aggregation.alias(_result_name(calc)))
    names = [*p.group_by, *(a.meta.output_name() for a in aggregations)]
    if duplicates := sorted({n for n in names if names.count(n) > 1}):
        raise ActionError(f"Result names used twice: {', '.join(duplicates)}")
    if p.group_by:
        out = df.group_by(p.group_by, maintain_order=True).agg(aggregations)
    else:
        out = df.select(aggregations)
    for name, column, dtype in wide:
        low, high = (0, 2**64 - 1) if dtype == pl.UInt64 else (-(2**63), 2**63 - 1)
        beyond = out[name].filter((out[name] < low) | (out[name] > high))
        if beyond.len():  # a wrong number would be worse
            total, where = beyond[0], " in a group" if p.group_by else ""
            if dtype == pl.Duration:
                per_day = {"ns": 10**9, "us": 10**6, "ms": 10**3}[dtype.time_unit] * 86_400
                raise ActionError(
                    f"“{name}” is {'-' if total < 0 else ''}{abs(total) // per_day:,} days{where}: too long for a "
                    f"duration (at most {high // per_day:,} days). Change “{column}” to hours before this step, "
                    f"with the formula EPOCH([{column}]) / 3600."
                )
            limit = f"big for a whole number (at most {high:,})" if total > high else f"small for a whole number (at least {low:,})"
            raise ActionError(
                f"“{name}” is {total:,}{where}: too {limit}. Change “{column}” to a decimal number before this step."
            )
        out = out.with_columns(pl.col(name).cast(dtype))
    return out


def _sums_may_overflow(s: pl.Series) -> bool:
    """Whether a sum of these whole numbers (or durations) might be beyond Int64 (Polars would let it
    overflow)."""
    s = s.to_physical()
    low, high = s.min(), s.max()
    return low is not None and max(-low, high) * s.len() > 2**63 - 1


def _check_type(calc, dtype: pl.DataType | None) -> None:
    """Sum needs numbers; average and median also take dates. Minimum and maximum can't compare
    lists and records (Polars gives empty values)."""
    if dtype is not None and calc.function in ("min", "max") and (dtype.is_nested() or dtype == pl.Object):
        label = _FUNCTION_LABELS[calc.function]
        raise ActionError(f"“{calc.column}” holds {dtypes.type_label(dtype)} values, which {label} can't compare")
    if dtype is None or calc.function not in ("sum", "mean", "median"):
        return
    if dtype.is_numeric() or dtype in (pl.Boolean, pl.Duration):
        return
    if calc.function != "sum" and dtype.is_temporal():
        return
    kind = "text" if dtype == pl.String else f"a {dtypes.type_label(dtype)} column"
    needs = "a number column" if calc.function == "sum" else "a number or date column"
    raise ActionError(f"{_FUNCTION_LABELS[calc.function]} needs {needs}; {calc.column} is {kind}")


def _aggregation(calc, separator: str) -> pl.Expr:
    column = pl.col(calc.column) if calc.column else None
    match calc.function:
        case "sum":
            return column.sum()
        case "mean":
            return column.mean()
        case "min":
            return column.min()
        case "max":
            return column.max()
        case "median":
            return column.median()
        case "count":
            return column.count().cast(pl.Int64)
        case "count_distinct":
            return column.drop_nulls().n_unique().cast(pl.Int64)
        case "count_rows":
            return pl.len().cast(pl.Int64)
        case "first":
            return column.drop_nulls().first()
        case "last":
            return column.drop_nulls().last()
        case "join":
            return column.cast(pl.String).drop_nulls().str.join(separator)
    raise ActionError(f"Unknown function '{calc.function}'")


def _result_name(calc) -> str:
    if calc.name.strip():
        return calc.name.strip()
    if calc.function == "count_rows":
        return "Count"
    return f"{_FUNCTION_LABELS[calc.function].split(' (')[0]} of {calc.column or '?'}"

"""Column names that Polars reads as patterns: exactly * (all columns), and a name that starts with ^
and ends with $ (a regular expression: ^a.*$ is the column abc). They come with data (a CSV
header, a value that Pivot makes a column name) and from settings, and every step takes them as
they are.

The examples come first. Then each case runs once with ordinary column names and once with one
of them (K, S, F, ...) renamed to such a name, beside the columns abc and x that the patterns
match: with the names mapped back, the table and the messages are the same (or the message of a
step that refuses the data).

Polars' own Series methods can't find a Series whose name starts with ^ and ends with $: some
steps stop there with an IndexError and change no value. Those cases are marked as expected to
stop; a column named * works in them."""

from __future__ import annotations

import datetime as dt
import os
import sqlite3
import uuid
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import openpyxl
import polars as pl
import pytest
import xlsxwriter
from polars.testing import assert_frame_equal

from tafl.api import ActionError, text_length
from tafl.api.testing import StubContext
from tafl.core import Registry, formula_preview
from tafl.plugins import databases, reshape
from tafl.ui import filters

# --- examples --------------------------------------------------------------------------------


def test_a_pivot_of_ratings_gives_columns_that_later_steps_take_by_their_names(act, tmp_path):
    ratings = tmp_path / "ratings.csv"
    ratings.write_text("Hotel;Rating;Nights\nA;*;3\nA;**;1\nB;*;2\nB;***;5\nC;**;4\n", encoding="utf-8")
    data = act("csv.import", None, {"file": str(ratings)})
    wide = act("reshape.pivot", data, {"rows": ["Hotel"], "column": "Rating", "value": "Nights", "function": "sum"})
    assert wide.rows() == [("A", 3, 1, None), ("B", 2, None, 5), ("C", None, 4, None)]
    assert wide.columns == ["Hotel", "*", "**", "***"]

    assert act("rows.sort", wide, {"by": [{"column": "*", "order": "desc"}]})["Hotel"].to_list() == ["A", "B", "C"]
    assert act("columns.select", wide, {"mode": "remove", "columns": ["*"]}).columns == ["Hotel", "**", "***"]
    filled = act("rows.fill", wide, {"columns": ["*"], "method": "value", "value": "0"})
    assert filled.rows() == [("A", 3, 1, None), ("B", 2, None, 5), ("C", 0, 4, None)]
    kept = act("rows.filter", wide, {"conditions": [{"column": "*", "operator": ">", "value": "2"}]})
    assert kept["Hotel"].to_list() == ["A"]


def test_remove_columns_removes_only_the_column_named_star(act):
    df = pl.DataFrame({"Order": [1, 2], "*": ["x", ""], "Amount": [9.5, 3.0]})
    assert act("columns.select", df, {"mode": "remove", "columns": ["*"]}).columns == ["Order", "Amount"]
    kept = act("columns.select", df, {"mode": "keep", "columns": ["*", "Order"]})
    assert kept.rows() == [("x", 1), ("", 2)] and kept.columns == ["*", "Order"]


def test_rename_files_renames_the_files_of_its_column(act, tmp_path):
    for name in ("a.txt", "b.txt", "c.txt"):
        (tmp_path / name).write_text(name, encoding="utf-8")
    table = pl.DataFrame(
        {
            "^a.*$": [str(tmp_path / "a.txt"), str(tmp_path / "b.txt")],  # the files
            "abc": [str(tmp_path / "c.txt")] * 2,  # what the name as a pattern would be
            "^x$": ["a_new.txt", "b_new.txt"],  # the new names
            "x": ["x1.txt", "x2.txt"],
        }
    )
    act("files.rename", table, {"column": "^a.*$", "new_name": "^x$"}, StubContext(mode="run"))
    assert sorted(path.name for path in tmp_path.iterdir()) == ["a_new.txt", "b_new.txt", "c.txt"]


def test_the_excel_export_writes_the_values_of_its_own_column(act, tmp_path):
    df = pl.DataFrame({"abc": [1.25, 2.5], "^a.*$": [10.5, float("inf")]})
    path = tmp_path / "out.xlsx"
    act("excel.export", df, {"file": str(path), "sheet": "S"}, StubContext(mode="run"))
    sheet = openpyxl.load_workbook(path)["S"]
    assert [[cell.value for cell in row] for row in sheet.iter_rows()] == [["abc", "^a.*$"], [1.25, 10.5], [2.5, None]]


def test_the_excel_export_writes_the_lists_and_texts_of_their_own_columns(act, tmp_path):
    df = pl.DataFrame({"abc": [10, 20], "^a.*$": [["red", "blue"], ["green"]], "^Code$": ["A1", "B2"]})
    path = tmp_path / "out.xlsx"
    act("excel.export", df, {"file": str(path), "sheet": "S"}, StubContext(mode="run"))
    cells = [[cell.value for cell in row] for row in openpyxl.load_workbook(path)["S"].iter_rows()]
    assert cells == [["abc", "^a.*$", "^Code$"], [10, "['red', 'blue']", "A1"], [20, "['green']", "B2"]]


def test_unpivot_writes_the_decimal_numbers_of_their_own_column_as_text(act):
    df = pl.DataFrame({"Region": ["N", "S"], "^Q1$": [1e16, -1.5e-7], "Note": ["ok", "late"]})
    out = act("reshape.unpivot", df, {"mode": "others", "columns": ["Region"], "name": "Field", "value": "Value"})
    assert out.rows() == [("N", "^Q1$", "10000000000000000"), ("N", "Note", "ok"), ("S", "^Q1$", "-0.00000015"), ("S", "Note", "late")]


def test_aggregate_keeps_the_group_values_beside_a_result_named_like_a_pattern(act):
    df = pl.DataFrame({"group": ["a", "a", "b"], "gx": [1.5, 2.5, 3.5], "big": [2**62, 2**62 - 5, 1]})
    calculation = {"function": "sum", "column": "big", "name": "^g.*$"}  # added up wider, then cast back
    out = act("calc.aggregate", df, {"group_by": ["gx"], "calculations": [calculation]})
    assert out.rows() == [(1.5, 2**62), (2.5, 2**62 - 5), (3.5, 1)]
    assert out.schema == pl.Schema({"gx": pl.Float64, "^g.*$": pl.Int64})


def test_clean_text_of_all_text_columns_leaves_each_column_its_own_values(act):
    df = pl.DataFrame({"abc": ["  one ", " two"], "^a.*$": [" x ", "y  "], "n": [1, 2]})
    out = act("text.clean", df, {"columns": [], "case": "upper"})
    assert out.rows() == [("ONE", "X", 1), ("TWO", "Y", 2)]


# --- the same result with such names ---------------------------------------------------------

ORDINARY = {"K": "k", "K2": "k2", "S": "s", "F": "f", "I": "info", "N": "new", "D": "d", "B": "b", "L": "l", "R": "r", "O": "o"}
PATTERNS = ("*", "^a.*$", "^x$")


@dataclass
class Run:
    """What a case gets: the column names (by role), a folder of its own and the actions."""

    names: dict[str, str]
    folder: Path
    act: Callable
    registry: Registry

    def __getitem__(self, role: str) -> str:
        return self.names[role]

    def step(self, action_id: str, df: pl.DataFrame | None, params: dict, ctx: StubContext | None = None):
        ctx = ctx or StubContext()
        return self.act(action_id, df, params, ctx), ctx

    def sqlite(self, path: Path, **options) -> StubContext:
        return StubContext(connections={"db": (self.registry.connectors["sqlite"], {"file": str(path)})}, **options)


@dataclass(frozen=True)
class Case:
    run: Callable[[Run], tuple[pl.DataFrame, StubContext]]
    roles: tuple[tuple[str, ...], ...]
    stops: frozenset[tuple[str, ...]]
    refused: bool


CASES: dict[str, Case] = {}


def case(label: str, *roles: str, stops: tuple[str, ...] = (), refused: bool = False):
    """A case for the roles (columns) it renames: "K", or "K+K2" for two columns with one name.
    ``stops``: roles whose names ^...$ stop in Polars' Series methods (IndexError). ``refused``:
    the step stops with a message (an ActionError), which must be the same."""

    def register(run):
        assert label not in CASES
        split = tuple(tuple(r.split("+")) for r in roles)
        CASES[label] = Case(run, split, frozenset(tuple(r.split("+")) for r in stops), refused)
        return run

    return register


def named(df: pl.DataFrame, run: Run) -> pl.DataFrame:
    """``df`` with the columns named by role (K, S, ...) renamed to the names of the run."""
    return df.rename({c: run[c] for c in df.columns if c in run.names})


def table(run: Run) -> pl.DataFrame:
    frame = pl.DataFrame(
        {
            "K": [1, 2, 2, 3, None, 3],
            "abc": [10, 20, 30, 40, 50, 60],
            "x": [5, 5, 6, 6, 7, 7],
            "S": ["b c", "A", None, "c d", " ", "a"],
            "F": [3.5, None, 1.5, 5.5, 2.5, 4.5],  # in another order than abc and x
        }
    )
    return named(frame, run)


def other(run: Run) -> pl.DataFrame:
    frame = pl.DataFrame(
        {"K2": [2, 3, 3, 9, None], "I": ["two", "three", "three b", "nine", "none"], "abc": [1, 2, 3, 4, 5], "x": [7, 8, 9, 10, 11]}
    )
    return named(frame, run)


def in_formula(name: str) -> str:
    return "[" + name.replace("]", "]]") + "]"


# columns

case("columns.rename K", "K")(lambda r: r.step("columns.rename", table(r), {"columns": [{"column": r["K"], "new_name": "renamed"}]}))
case("columns.rename x to N", "N")(lambda r: r.step("columns.rename", table(r), {"columns": [{"column": "x", "new_name": r["N"]}]}))
for _mode in ("keep", "remove", "front"):
    case(f"columns.select {_mode} K", "K")(lambda r, m=_mode: r.step("columns.select", table(r), {"mode": m, "columns": [r["K"]]}))
    case(f"columns.select {_mode} S and K", "K")(
        lambda r, m=_mode: r.step("columns.select", table(r), {"mode": m, "columns": [r["S"], r["K"]]})
    )
case("columns.select front x, K behind", "K")(lambda r: r.step("columns.select", table(r), {"mode": "front", "columns": ["x"]}))
case("columns.change_type K to text", "K", stops=("K",))(
    lambda r: r.step("columns.change_type", table(r), {"columns": [{"column": r["K"], "type": "text", "format": ""}]})
)

# calculate

case("calc.columns formula on K", "K")(
    lambda r: r.step("calc.columns", table(r), {"columns": [{"name": "out", "formula": f"{in_formula(r['K'])} * 2"}]})
)
case("calc.columns result N", "N")(lambda r: r.step("calc.columns", table(r), {"columns": [{"name": r["N"], "formula": "[x] + 1"}]}))
case("calc.enumerate rows per K", "K")(lambda r: r.step("calc.enumerate", table(r), {"name": "No", "mode": "rows", "group_by": [r["K"]]}))
case("calc.enumerate rows per K by F", "K", "F")(
    lambda r: r.step(
        "calc.enumerate", table(r), {"name": "No", "mode": "rows", "group_by": [r["K"]], "order_by": [{"column": r["F"], "order": "desc"}]}
    )
)
case("calc.enumerate rows by F descending", "F")(
    lambda r: r.step("calc.enumerate", table(r), {"name": "No", "mode": "rows", "order_by": [{"column": r["F"], "order": "desc"}]})
)
case("calc.enumerate rows by text S", "S", stops=("S",))(
    lambda r: r.step("calc.enumerate", table(r), {"name": "No", "mode": "rows", "order_by": [{"column": r["S"], "order": "asc"}]})
)
case("calc.enumerate groups by K", "K")(lambda r: r.step("calc.enumerate", table(r), {"name": "No", "mode": "groups", "group_by": [r["K"]]}))
case("calc.enumerate groups into N", "N")(lambda r: r.step("calc.enumerate", table(r), {"name": r["N"], "mode": "groups", "group_by": ["x"]}))
case("calc.enumerate groups into N, which is there", "N")(
    lambda r: r.step("calc.enumerate", table(r).with_columns(pl.lit(0).alias(r["N"])), {"name": r["N"], "mode": "groups", "group_by": ["x"]})
)
case("calc.aggregate by K: sum of F", "K")(
    lambda r: r.step("calc.aggregate", table(r), {"group_by": [r["K"]], "calculations": [{"function": "sum", "column": r["F"], "name": "total"}]})
)
case("calc.aggregate sum of F", "F")(
    lambda r: r.step("calc.aggregate", table(r), {"group_by": ["x"], "calculations": [{"function": "sum", "column": r["F"], "name": "total"}]})
)
case("calc.aggregate join texts of S", "S")(
    lambda r: r.step("calc.aggregate", table(r), {"group_by": ["x"], "calculations": [{"function": "join", "column": r["S"], "name": "all"}]})
)


@case("calc.aggregate wide sums of K into N", "K", "N", stops=("K",))
def _(r):
    """Group columns of decimal numbers beside a sum of whole numbers that is added up wider."""
    df = named(pl.DataFrame({"g": [1.5, 1.5, 2.5], "abc": [0.5, 0.5, 0.25], "x": [5.5, 5.5, 7.5], "K": [2**62, 2**62 - 5, 1]}), r)
    calculations = [{"function": "sum", "column": r["K"], "name": r["N"]}]
    return r.step("calc.aggregate", df, {"group_by": ["g", "abc", "x"], "calculations": calculations})


@case("calc.aggregate exact sum of F", "F", stops=("F",))
def _(r):
    big = Decimal(4 * 10**37)
    exact = pl.Series([big, -big + 7, Decimal(1)], dtype=pl.Decimal(38, 0))
    df = named(pl.DataFrame({"g": [1, 1, 2], "abc": [1, 2, 3], "x": [5, 6, 7], "F": exact}), r)
    return r.step("calc.aggregate", df, {"group_by": ["g"], "calculations": [{"function": "sum", "column": r["F"], "name": "total"}]})


# rows

for _operator, _value in (("=", "2"), (">", "1"), ("empty", ""), ("in", "1;3")):
    case(f"rows.filter K {_operator} {_value}", "K")(
        lambda r, o=_operator, v=_value: r.step("rows.filter", table(r), {"conditions": [{"column": r["K"], "operator": o, "value": v}]})
    )
case("rows.filter S contains a", "S")(
    lambda r: r.step("rows.filter", table(r), {"conditions": [{"column": r["S"], "operator": "contains", "value": "a"}]})
)
case("rows.filter_formula K > 1", "K")(lambda r: r.step("rows.filter_formula", table(r), {"condition": f"{in_formula(r['K'])} > 1"}))
case("rows.sort by K descending", "K")(lambda r: r.step("rows.sort", table(r), {"by": [{"column": r["K"], "order": "desc"}]}))
case("rows.sort by F", "F")(lambda r: r.step("rows.sort", table(r), {"by": [{"column": r["F"], "order": "asc"}]}))
case("rows.sort by text S", "S", stops=("S",))(lambda r: r.step("rows.sort", table(r), {"by": [{"column": r["S"], "order": "asc"}]}))
case("rows.dedupe K", "K")(lambda r: r.step("rows.dedupe", table(r), {"columns": [r["K"]], "keep": "last"}))
case("rows.dedupe all columns", "K")(lambda r: r.step("rows.dedupe", pl.concat([table(r), table(r)]), {"columns": []}))
case("rows.fill K down", "K")(lambda r: r.step("rows.fill", table(r), {"columns": [r["K"]], "method": "down"}))
case("rows.fill S with z", "S")(lambda r: r.step("rows.fill", table(r), {"columns": [r["S"]], "method": "value", "value": "z"}))
case("rows.fill F with 0", "F")(lambda r: r.step("rows.fill", table(r), {"columns": [r["F"]], "method": "value", "value": "0"}))

# text

case("text.replace whole values of S", "S")(
    lambda r: r.step("text.replace", table(r), {"columns": [r["S"]], "match": "whole", "replacements": [{"find": "A", "replace": "Z"}]})
)
case("text.replace in K", "K")(
    lambda r: r.step("text.replace", table(r), {"columns": [r["K"]], "match": "contains", "replacements": [{"find": "2", "replace": "9"}]})
)
case("text.clean S", "S")(lambda r: r.step("text.clean", table(r), {"columns": [r["S"]], "case": "upper"}))
case("text.clean all text columns", "S")(lambda r: r.step("text.clean", table(r), {"columns": [], "case": "upper"}))
case("text.split S into columns", "S")(
    lambda r: r.step("text.split", table(r), {"column": r["S"], "into": "columns", "separator": "space", "parts": [{"name": "p1"}, {"name": "p2"}]})
)
case("text.split S into rows", "S")(lambda r: r.step("text.split", table(r), {"column": r["S"], "into": "rows", "separator": "space"}))
case("text.split K into columns", "K")(
    lambda r: r.step("text.split", table(r), {"column": r["K"], "into": "columns", "separator": ",", "parts": [{"name": "p1"}, {"name": "p2"}]})
)
case("text.split decimal numbers F into columns", "F")(
    lambda r: r.step(
        "text.split", table(r), {"column": r["F"], "into": "columns", "separator": "other", "other": ".", "parts": [{"name": "p1"}, {"name": "p2"}]}
    )
)
case("text.split into N", "N")(
    lambda r: r.step("text.split", table(r), {"column": "s", "into": "columns", "separator": "space", "parts": [{"name": r["N"]}, {"name": "p2"}]})
)
case("text.split S into automatic names", "S")(
    lambda r: r.step("text.split", table(r), {"column": r["S"], "into": "columns", "separator": "space", "parts": [{"name": ""}, {"name": ""}]})
)

# combine


def _merge(r: Run, df: pl.DataFrame, right: pl.DataFrame, match: list[tuple[str, str]], **params):
    ctx = StubContext(tables={"o": right})
    pairs = [{"column": column, "other_column": other_column} for column, other_column in match]
    return r.step("table.merge", df, {"other": "o", "match": pairs, **params}, ctx)


def _texts(r: Run) -> pl.DataFrame:
    """The other table with its keys as text."""
    frame = pl.DataFrame({"K2": ["2", "3", "3", "9", None], "I": ["two", "three", "three b", "nine", "none"], "abc": [1, 2, 3, 4, 5]})
    return named(frame, r)


for _how in ("lookup", "left", "inner", "full", "matching", "non_matching"):
    case(f"table.merge {_how} on K and K2", "K", "K2", "K+K2")(
        lambda r, h=_how: _merge(r, table(r), other(r), [(r["K"], r["K2"])], how=h, multiple="all")
    )
for _how in ("matching", "left"):
    case(f"table.merge {_how}, no row matches", "K", "K2")(
        lambda r, h=_how: _merge(r, table(r), named(pl.DataFrame({"K2": [100, 200], "abc": [1, 2]}), r), [(r["K"], r["K2"])], how=h)
    )
case("table.merge on K and x, both with K2", "K2")(
    lambda r: _merge(r, table(r), other(r), [("k", r["K2"]), ("x", r["K2"])], how="full", multiple="all")
)
case("table.merge left, the other column I", "I")(lambda r: _merge(r, table(r), other(r), [("k", "k2")], how="left", multiple="all"))
case("table.merge lookup of the column I", "I")(lambda r: _merge(r, table(r), other(r), [("k", "k2")], how="lookup", columns=[r["I"]]))
case("table.merge full on K and text K2", "K", "K2", "K+K2")(lambda r: _merge(r, table(r), _texts(r), [(r["K"], r["K2"])], how="full"))
case("table.merge full, first match, two keys", "K", "K2", "K+K2")(
    lambda r: _merge(
        r, table(r), other(r).with_columns(x=pl.Series([5, 6, 6, 7, 7])), [(r["K"], r["K2"]), ("x", "x")], how="full", multiple="first"
    )
)
case("table.append only the columns of this table", "K")(
    lambda r: r.step("table.append", table(r), {"tables": ["o"], "columns": "this"}, StubContext(tables={"o": table(r).head(2)}))
)
case("table.append all columns", "K")(
    lambda r: r.step("table.append", table(r), {"tables": ["o"], "columns": "all"}, StubContext(tables={"o": other(r)}))
)


def _other_types(r: Run) -> dict[str, pl.DataFrame]:
    """Tables whose column K has decimal numbers (o) and texts (p): K gets a type that fits all."""
    return {"o": named(pl.DataFrame({"K": [4.5], "x": [9]}), r), "p": named(pl.DataFrame({"K": ["n/a"], "abc": [7]}), r)}


for _tables in (["o"], ["o", "p"]):
    for _columns in ("all", "this"):
        case(f"table.append {'+'.join(_tables)} ({_columns} columns), K of other types", "K")(
            lambda r, t=_tables, c=_columns: r.step(
                "table.append", table(r), {"tables": t, "columns": c}, StubContext(tables=_other_types(r))
            )
        )


@case("parquet.import of a folder, K of other types in the files", "K")
def _(r):
    named(pl.DataFrame({"K": [1, 2], "abc": [1.5, 2.5]}), r).write_parquet(r.folder / "a.parquet")
    named(pl.DataFrame({"K": ["n/a"], "x": ["b"]}), r).write_parquet(r.folder / "b.parquet")
    return r.step("parquet.import", None, {"source": "folder", "folder": str(r.folder), "file_column": True})


# reshape

case("reshape.pivot rows K", "K")(lambda r: r.step("reshape.pivot", table(r), {"rows": [r["K"]], "column": "s", "value": "f", "function": "sum"}))
case("reshape.pivot names from S", "S", stops=("S",))(
    lambda r: r.step("reshape.pivot", table(r), {"rows": ["x"], "column": r["S"], "value": "f", "function": "sum"})
)
case("reshape.pivot values of F", "F")(lambda r: r.step("reshape.pivot", table(r), {"rows": ["x"], "column": "s", "value": r["F"], "function": "sum"}))
case("reshape.pivot of no rows, rows K", "K")(
    lambda r: r.step("reshape.pivot", table(r).clear(), {"rows": [r["K"]], "column": "s", "value": "f", "function": "sum"})
)


@case("reshape.pivot of a big table, rows K", "K")
def _(r):
    """K has few values, abc and x many: the cells are counted by K."""
    df = named(pl.DataFrame({"K": [i % 3 for i in range(30)], "abc": range(30), "x": range(30), "s": [f"v{i % 10}" for i in range(30)]}), r)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(reshape, "_MAX_CELLS", 100)
        return r.step("reshape.pivot", df, {"rows": [r["K"]], "column": "s", "value": "x", "function": "sum"})


@case("reshape.pivot exact sums of F", "F")
def _(r):
    big = Decimal(4 * 10**37)
    exact = pl.Series([big, -big + 7, Decimal(1), Decimal(2)], dtype=pl.Decimal(38, 0))
    df = named(pl.DataFrame({"g": [1, 1, 2, 2], "abc": [1, 2, 3, 4], "x": ["p", "p", "p", "q"], "F": exact}), r)
    return r.step("reshape.pivot", df, {"rows": ["g"], "column": "x", "value": r["F"], "function": "sum"})


case("reshape.unpivot F and K", "K")(
    lambda r: r.step("reshape.unpivot", table(r), {"mode": "these", "columns": [r["F"], r["K"]], "name": "Column", "value": "Value"})
)
case("reshape.unpivot all but S", "K")(
    lambda r: r.step("reshape.unpivot", table(r), {"mode": "others", "columns": [r["S"]], "name": "Column", "value": "Value"})
)
case("reshape.unpivot F, K stays", "K")(
    lambda r: r.step("reshape.unpivot", table(r), {"mode": "these", "columns": ["f"], "name": "Column", "value": "Value"})
)
case("reshape.unpivot K and S as text", "K")(
    lambda r: r.step("reshape.unpivot", table(r), {"mode": "these", "columns": [r["K"], r["S"]], "name": "Column", "value": "Value"})
)
case("reshape.unpivot F and S as text", "F")(
    lambda r: r.step("reshape.unpivot", table(r), {"mode": "these", "columns": [r["F"], r["S"]], "name": "Column", "value": "Value"})
)
case("reshape.unpivot into N", "N")(
    lambda r: r.step("reshape.unpivot", table(r), {"mode": "these", "columns": ["f", "abc"], "name": r["N"], "value": "Value"})
)

# files


@case("files.rename the files in K to the names in S", "K", "S")
def _(r):
    for name in ("a.txt", "b.txt", "c.txt"):
        (r.folder / name).write_text(name, encoding="utf-8")
    files = {"K": [str(r.folder / "a.txt"), str(r.folder / "b.txt")], "abc": [str(r.folder / "c.txt")] * 2}
    df = named(pl.DataFrame({**files, "x": ["x1.txt", "x2.txt"], "S": ["a_new.txt", "b_new.txt"]}), r)
    _, ctx = r.step("files.rename", df, {"column": r["K"], "new_name": r["S"]}, StubContext(mode="run"))
    return pl.DataFrame({"file": sorted(path.name for path in r.folder.iterdir())}), ctx


@case("files.copy the files in K", "K")
def _(r):
    (r.folder / "in").mkdir()
    for name in ("a.txt", "c.txt"):
        (r.folder / "in" / name).write_text(name, encoding="utf-8")
    df = named(pl.DataFrame({"K": [str(r.folder / "in" / "a.txt")], "abc": [str(r.folder / "in" / "c.txt")]}), r)
    _, ctx = r.step("files.copy", df, {"column": r["K"], "target": str(r.folder / "out")}, StubContext(mode="run"))
    return pl.DataFrame({"file": sorted(path.name for path in (r.folder / "out").iterdir())}), ctx


# exports


def _for_export(r: Run) -> pl.DataFrame:
    """Decimal numbers (F), date-times with a time zone (D) and binary values (B)."""
    zoned = pl.Series("D", [dt.datetime(2025, 1, 1, 8, 30), dt.datetime(2025, 6, 1, 9, 0)]).dt.replace_time_zone("Europe/Berlin")
    frame = pl.DataFrame({"abc": [1.25, 2.5], "x": [3.0, 4.0], "F": [10.5, float("inf")], "D": zoned, "B": [b"\x00\xff", b"\x01"]})
    return named(frame, r)


def _cells(path: Path, formats: bool = False) -> pl.DataFrame:
    """The cells of sheet S as text: their values, and with ``formats`` their number formats too."""
    rows = list(openpyxl.load_workbook(path)["S"].iter_rows())
    shown = [[repr((cell.value, cell.number_format) if formats else cell.value) for cell in row] for row in rows]
    return pl.DataFrame(shown, schema=[f"column {i}" for i in range(len(shown[0]))], orient="row")


@dataclass(frozen=True)
class _Own:
    """A value of a plugin's own type."""

    text: str

    def __str__(self) -> str:
        return f"own {self.text}"


def _for_excel(r: Run) -> pl.DataFrame:
    """Texts (S), lists (L), records (R) and values of a plugin's own type (O): Polars' write_excel
    takes the last three by their names."""
    frame = pl.DataFrame(
        [
            pl.Series("abc", [10, 20]),
            pl.Series("x", [30, 40]),
            pl.Series("S", ["A1", None]),
            pl.Series("L", [["red", "blue"], None]),
            pl.Series("R", [{"size": 1}, {"size": 2}]),
            pl.Series("O", [_Own("a"), None], dtype=pl.Object),
        ]
    )
    return named(frame, r)


for _role in ("F", "D", "B"):

    @case(f"excel.export {_role}", _role)
    def _(r):
        path = r.folder / "out.xlsx"
        _, ctx = r.step("excel.export", _for_export(r), {"file": str(path), "sheet": "S"}, StubContext(mode="run"))
        return _cells(path), ctx

    @case(f"csv.export {_role}", _role, stops=("F",) if _role == "F" else ())
    def _(r):
        path = r.folder / "out.csv"
        params = {"file": str(path), "delimiter": ";", "encoding": "utf-8", "decimal": "."}
        _, ctx = r.step("csv.export", _for_export(r), params, StubContext(mode="run"))
        return pl.DataFrame({"line": path.read_text(encoding="utf-8").splitlines()}), ctx


@case("excel.export with a format for F", "F")
def _(r):
    path = r.folder / "out.xlsx"
    df = named(pl.DataFrame({"abc": [1.25], "x": [2.5], "F": [10.5]}), r)
    params = {"file": str(path), "sheet": "S", "column_formats": [{"column": r["F"], "format": "0.000"}]}
    _, ctx = r.step("excel.export", df, params, StubContext(mode="run"))
    return _cells(path, formats=True), ctx


for _role in ("S", "L", "R", "O"):

    @case(f"excel.export {_role} beside lists and records", _role)
    def _(r):
        path = r.folder / "out.xlsx"
        _, ctx = r.step("excel.export", _for_excel(r), {"file": str(path), "sheet": "S"}, StubContext(mode="run"))
        return _cells(path), ctx


def _database(r: Run, *exports: tuple[pl.DataFrame, dict]) -> tuple[pl.DataFrame, StubContext]:
    path = r.folder / "out.db"
    ctx = r.sqlite(path, mode="run")
    for df, params in exports:
        r.act("db.export", df, {"connection": "db", "table": "t", **params}, ctx)
    with closing(sqlite3.connect(path)) as con:
        names = [row[1] for row in con.execute("SELECT * FROM pragma_table_info('t')")]
        rows = con.execute("SELECT * FROM t ORDER BY rowid").fetchall()
    return pl.DataFrame({"names": [repr(names)] * len(rows), "row": [repr(row) for row in rows]}), ctx


for _role in ("F", "D", "K"):

    @case(f"db.export to SQLite with {_role}", _role)
    def _(r):
        frame = pl.DataFrame(
            {
                "K": [1, 2],
                "abc": [Decimal("1.50"), Decimal("2.25")],
                "x": [True, False],
                "F": [1.5, float("nan")],
                "D": [dt.date(2025, 1, 1), dt.date(2025, 1, 2)],
            }
        )
        return _database(r, (named(frame, r), {}))


for _mode in ("update", "upsert", "replace_keys", "delete"):

    @case(f"db.export to SQLite: {_mode} by K", "K")
    def _(r, mode=_mode):
        first = named(pl.DataFrame({"K": [1, 2, 3], "abc": [10, 20, 30], "x": [5, 6, 7]}), r)
        changed = named(pl.DataFrame({"K": [2, 4], "abc": [99, 40], "x": [7, 8]}), r)
        return _database(r, (first, {}), (changed, {"mode": mode, "key_columns": [r["K"]]}))


@case("db.export to SQLite: update by K, which has a value twice", "K", refused=True)
def _(r):
    first = named(pl.DataFrame({"K": [1, 2], "abc": [10, 20], "x": [5, 6]}), r)
    changed = named(pl.DataFrame({"K": [2, 2], "abc": [98, 99], "x": [7, 8]}), r)  # which row would win?
    return _database(r, (first, {}), (changed, {"mode": "update", "key_columns": [r["K"]]}))


# imports


def _csv(r: Run) -> Path:
    path = r.folder / "in.csv"
    header = ";".join([r["K"], "abc", "x", r["S"], r["F"]])
    path.write_text(f"{header}\n1;1,5;a;31.12.2025;1.234,50 €\n2;2,5;b;01.01.2026;12,5\n2;3;c;;7\n", encoding="utf-8")
    return path


def _workbook(r: Run) -> Path:
    path = r.folder / "in.xlsx"
    book = xlsxwriter.Workbook(str(path))
    sheet, stamp = book.add_worksheet("Sheet1"), book.add_format({"num_format": "yyyy-mm-dd hh:mm"})
    sheet.write_row(0, 0, [r["K"], "abc", "x", r["S"], r["F"]])
    rows = [(1, 1.5, "a", "p", dt.datetime(2025, 1, 1, 8)), (2, 2.5, "b", None, None), (2, 3.0, "c", "q", dt.datetime(2025, 6, 1, 8, 30))]
    for number, values in enumerate(rows, 1):
        for column, value in enumerate(values):
            if isinstance(value, dt.datetime):
                sheet.write_datetime(number, column, value, stamp)
            elif value is not None:
                sheet.write(number, column, value)
    book.close()
    return path


def _parquet(r: Run) -> Path:
    path = r.folder / "in.parquet"
    frame = pl.DataFrame(
        {
            "K": pl.Series([1, 2, 2], dtype=pl.Int32),
            "abc": [1.5, 2.5, 3.0],
            "x": ["a", "b", "c"],
            "S": pl.Series([1, 2, None], dtype=pl.Decimal(10, 0)),
            "F": pl.Series([1.5, None, 2.0], dtype=pl.Float32),
        }
    )
    named(frame, r).write_parquet(path)
    return path


def _sqlite_table(r: Run) -> Path:
    path = r.folder / "in.db"

    def quoted(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    with closing(sqlite3.connect(path)) as con, con:
        con.execute(f"CREATE TABLE t ({quoted(r['K'])} INTEGER, abc REAL, x TEXT, {quoted(r['S'])} TEXT, {quoted(r['F'])} NUMERIC)")
        rows = [(1, 1.5, "a", "2025-01-31", 1.25), (2, None, "b", "x", 3), (None, 2.5, "c", None, None)]
        con.executemany("INSERT INTO t VALUES (?, ?, ?, ?, ?)", rows)
    return path


for _role in ("K", "S", "F"):
    case(f"csv.import header {_role}", _role, stops=(_role,))(lambda r: r.step("csv.import", None, {"file": str(_csv(r))}))
    case(f"excel.import header {_role}", _role, stops=("K", "F"))(lambda r: r.step("excel.import", None, {"file": str(_workbook(r))}))
    case(f"parquet.import column {_role}", _role)(lambda r: r.step("parquet.import", None, {"file": str(_parquet(r))}))
    case(f"db.import column {_role} with detected types", _role, stops=("S",))(
        lambda r: r.step("db.import", None, {"connection": "db", "mode": "table", "table": "t", "detect_types": True}, r.sqlite(_sqlite_table(r)))
    )
case("csv.import without detected types", "K", "S", "F")(lambda r: r.step("csv.import", None, {"file": str(_csv(r)), "detect_types": False}))
case("db.import without detected types", "K", "S", "F")(
    lambda r: r.step("db.import", None, {"connection": "db", "mode": "table", "table": "t"}, r.sqlite(_sqlite_table(r)))
)

# instant filters and the formula check of the window


def _filtered(r: Run, f: filters.Filter):
    return filters.apply(table(r), [f]), StubContext()


def _offered(r: Run, f: filters.Filter):
    found = filters.facet(table(r), [f], 0)
    assert found is not None
    values, rows = found
    return values.sort("value", nulls_last=True).with_columns(rows=pl.lit(rows)), StubContext()


case("filter values of K", "K")(lambda r: _filtered(r, filters.Filter(column=r["K"], values=[2, 3])))
case("filter values offered for K", "K")(lambda r: _offered(r, filters.Filter(column=r["K"])))
case("filter S by a pattern", "S")(lambda r: _filtered(r, filters.Filter(column=r["S"], values=[filters.MATCH], facet="regex", options={"pattern": "^[a-c]"})))
case("filter F by ranges", "F")(lambda r: _filtered(r, filters.Filter(column=r["F"], values=[1], facet="ranges", options={"thresholds": [2, 4]})))
case("filter lengths offered for S", "S")(lambda r: _offered(r, filters.Filter(column=r["S"], facet="length")))


def _preview(r: Run, formula: str):
    checked = formula_preview.preview(table(r), formula)
    ctx = StubContext()
    ctx.messages.append(("ok" if checked.ok else "error", checked.message))
    return (checked.rows if checked.rows is not None else pl.DataFrame()), ctx


case("formula check of K and x", "K")(lambda r: _preview(r, f"{in_formula(r['K'])} * 2 + [x]"))
case("formula check of K", "K")(lambda r: _preview(r, f"{in_formula(r['K'])} * 2"))


# --- running the cases -----------------------------------------------------------------------


@dataclass(frozen=True)
class Done:
    frame: pl.DataFrame
    messages: list[tuple[str, str]]


@dataclass(frozen=True)
class Refused:
    message: str


def _outcome(case: Case, run: Run) -> Done | Refused:
    """What the case gives: a table and the messages of the step, or the message of a refused step."""
    run.folder.mkdir()
    try:
        frame, ctx = case.run(run)
    except ActionError as error:
        if not case.refused:
            raise
        return Refused(str(error).replace(str(run.folder), "<folder>"))
    return Done(frame, [(level, text.replace(str(run.folder), "<folder>")) for level, text in ctx.messages])


def _mapped_back(outcome: Done | Refused, name: str, ordinary: str) -> Done | Refused:
    """The outcome with the name ``name`` written as ``ordinary``: in column names, texts and messages."""
    if isinstance(outcome, Refused):
        return Refused(outcome.message.replace(name, ordinary))
    frame = outcome.frame.rename({c: c.replace(name, ordinary) for c in outcome.frame.columns})
    texts = [c for c, dtype in frame.schema.items() if dtype == pl.String]
    if texts:  # e.g. Unpivot's column of names
        frame = frame.with_columns(
            pl.Series(c, [v if v is None else v.replace(name, ordinary) for v in frame[c].to_list()], dtype=pl.String) for c in texts
        )
    return Done(frame, [(level, text.replace(name, ordinary)) for level, text in outcome.messages])


def _cases():
    for label, found in CASES.items():
        for role in found.roles:
            for pattern in PATTERNS:
                marks = ()
                if role in found.stops and pattern != "*":
                    reason = "Polars' Series methods can't find a Series named ^...$: the step stops, no value changes"
                    marks = (pytest.mark.xfail(raises=IndexError, strict=False, reason=reason),)
                yield pytest.param(label, role, pattern, marks=marks, id=f"{label} | {'+'.join(role)}={pattern}")


_ORDINARY_OUTCOMES: dict[tuple[str, tuple[str, ...]], Done | Refused] = {}


@pytest.mark.parametrize(("label", "role", "pattern"), list(_cases()))
def test_a_name_like_a_pattern_gives_the_same_result(act, registry, tmp_path, label, role, pattern):
    found = CASES[label]
    ordinary = ORDINARY[role[0]]  # two roles get one name
    key = (label, role)
    if key not in _ORDINARY_OUTCOMES:
        run = Run(ORDINARY | {r: ordinary for r in role}, tmp_path / "ordinary", act, registry)
        try:
            _ORDINARY_OUTCOMES[key] = _outcome(found, run)
        except Exception as error:  # the case itself must work: an expected stop must not hide that
            pytest.fail(f"With ordinary names: {type(error).__name__}: {error}")
    expected = _ORDINARY_OUTCOMES[key]
    assert isinstance(expected, Refused) == found.refused
    run = Run(ORDINARY | {r: pattern for r in role}, tmp_path / "named", act, registry)
    got = _mapped_back(_outcome(found, run), pattern, ordinary)
    if isinstance(expected, Refused):
        assert got == expected
        return
    assert_frame_equal(got.frame, expected.frame, check_exact=True)
    assert got.messages == expected.messages


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("role", ["D", "Z"])
def test_the_sqlserver_export_fits_the_values_of_their_own_columns(registry, role, pattern):
    """Date-times for a datetime2(0) column cut to the second (D), and date-times with a time zone
    in their local time (Z), beside abc, a datetime2(7) column whose values stay as they are."""
    connector = registry.connectors["sqlserver"]
    ctx = StubContext(mode="run", connections={"server": (connector, {"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"})})
    stamp = dt.datetime(2026, 1, 2, 8, 30, 15, 600000)
    frame = pl.DataFrame(
        {
            "K": [1, 2],
            "abc": [stamp, stamp],
            "x": [5, 6],
            "D": [stamp, stamp + dt.timedelta(hours=1)],
            "Z": pl.Series([stamp, stamp]).dt.replace_time_zone("Europe/Berlin"),
        }
    )

    def exported(names: dict[str, str]) -> list[tuple]:
        table = f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"
        quoted = {c: "[" + names.get(c, c).replace("]", "]]") + "]" for c in frame.columns}
        types = {"K": "int", "abc": "datetime2(7)", "x": "int", "D": "datetime2(0)", "Z": "datetime2(7)"}
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {table} ({', '.join(f'{quoted[c]} {types[c]}' for c in frame.columns)})")
        try:
            data = frame.rename({c: names[c] for c in frame.columns if c in names})
            registry.actions["db.export"](data, {"connection": "server", "table": table}, ctx)
            with ctx.database("server") as db:
                return db.read(f"SELECT * FROM {table} ORDER BY {quoted['K']}").rows()
        finally:
            with ctx.database("server") as db:
                db.execute(f"DROP TABLE {table}")

    expected = exported({})
    assert expected == [
        (1, stamp, 5, dt.datetime(2026, 1, 2, 8, 30, 15), stamp),
        (2, stamp, 6, dt.datetime(2026, 1, 2, 9, 30, 15), stamp),
    ]
    assert exported({role: pattern}) == expected


@pytest.mark.parametrize("name", [*PATTERNS, "Amount"])
def test_the_sqlserver_export_checks_and_fits_the_values_of_their_own_columns(registry, name):
    """Without a server: what the SQL Server export does with the values before it writes them,
    for a column beside abc (which ^a.*$ would find). Polars' Series methods can't find a Series
    named ^...$: these stopped with an IndexError."""
    connector = registry.connectors["sqlserver"]

    def frame(*values, dtype=None) -> pl.DataFrame:
        return pl.DataFrame([pl.Series("abc", [1] * len(values)), pl.Series(name, values, dtype=dtype)])

    def fitted(df: pl.DataFrame, kind: str, finding: tuple[str, ...] = ()) -> list:
        return databases._fit_lengths(df, {"abc": "abc", name: name}, {name: kind}, "t", list(finding))[name].to_list()

    exact = frame(Decimal("1.005"), Decimal("-2.665"), dtype=pl.Decimal(10, 3))  # rounded as SQL Server rounds
    assert connector.fit_frame(exact, {name: "decimal(10,2)"})[name].to_list() == [Decimal("1.01"), Decimal("-2.67")]
    assert connector.prepare_frame(frame(1.5, float("nan")))[name].to_list() == [1.5, None]
    with pytest.raises(ActionError, match="has an infinite value"):
        connector.prepare_frame(frame(1.5, float("inf")))
    databases._check_whole_numbers(frame(1.0, 2.0), {"abc": "abc", name: name}, {name: "int"}, "t")
    with pytest.raises(ActionError, match=r"holds whole numbers \(int\), but .* has decimals \(e\.g\. 2\.5\)"):
        databases._check_whole_numbers(frame(1.0, 2.5), {"abc": "abc", name: name}, {name: "int"}, "t")
    assert fitted(frame("AB  ", "CD", None), "nvarchar(3)") == ["AB ", "CD", None]  # spaces cut as SQL Server cuts them
    assert fitted(frame("ABCD", "AB"), "nvarchar(3)", finding=(name,)) == ["AB"]  # a key that finds no row
    with pytest.raises(ActionError, match="holds texts of up to 3 characters"):
        fitted(frame("ABCD"), "nvarchar(3)")
    with pytest.raises(ActionError, match="holds values of up to 1 bytes"):
        fitted(frame(b"\x01\x02"), "varbinary(1)")
    texts = frame("a" * 60)[name]
    assert text_length(texts) == 100 and connector.sql_type(texts) == "NVARCHAR(100)"
    pyodbc = pytest.importorskip("pyodbc")  # a temporary table: texts for an int column, binary values of another type
    assert connector.input_sizes(frame("5"), {"abc": "INT", name: "INT"}, temporary=True)[1] == (pyodbc.SQL_WVARCHAR, 50, 0)
    assert connector.input_sizes(frame(b"\x01", b"\x02\x03"), {"abc": "INT"}, temporary=True)[1] == (pyodbc.SQL_VARBINARY, 2, 0)


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
@pytest.mark.parametrize("pattern", PATTERNS)
@pytest.mark.parametrize("role", ["F", "E", "S", "B", "W"])
def test_the_sqlserver_export_writes_the_values_of_their_own_columns(registry, role, pattern):
    """Decimal numbers for a float column (F), exact decimals with more decimal places than their
    column (E), texts with spaces at the end for an nvarchar(3) column (S), binary values (B) and
    whole decimal numbers for an int column (W): into a table, into a new table and by key."""
    connector = registry.connectors["sqlserver"]
    ctx = StubContext(mode="run", connections={"server": (connector, {"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"})})
    frame = pl.DataFrame(
        {
            "K": [1, 2],
            "abc": ["p", "q"],
            "x": [5, 6],
            "F": [1.5, 2.25],
            "E": pl.Series([Decimal("1.005"), Decimal("-2.665")], dtype=pl.Decimal(10, 3)),
            "S": ["AB  ", "CD"],
            "B": [b"\x01", b"\x02\x03"],
            "W": [3.0, 4.0],
        }
    )
    types = {"K": "int", "abc": "nvarchar(max)", "x": "int", "F": "float", "E": "decimal(10,2)", "S": "nvarchar(3)", "B": "varbinary(10)", "W": "int"}

    def exported(names: dict[str, str]) -> list[list[tuple]]:
        data = frame.rename(names)
        quoted = {c: "[" + names.get(c, c).replace("]", "]]") + "]" for c in frame.columns}
        tables = []
        for mode in ("insert", "create", "update"):
            table = f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"
            try:
                params = {"connection": "server", "table": table}
                if mode != "create":
                    with ctx.database("server") as db:
                        db.execute(f"CREATE TABLE {table} ({', '.join(f'{quoted[c]} {types[c]}' for c in frame.columns)})")
                if mode == "update":  # first the values of the other row
                    registry.actions["db.export"](data.with_columns(K=pl.Series([2, 1])), params, ctx)
                    params |= {"mode": "update", "key_columns": ["K"]}
                registry.actions["db.export"](data, params, ctx)
                with ctx.database("server") as db:
                    tables.append(db.read(f"SELECT * FROM {table} ORDER BY K").rows())
            finally:
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")
        return tables

    expected = exported({})  # the import reads decimals of 15 digits or fewer as decimal numbers
    fitted = [(1, "p", 5, 1.5, 1.01, "AB ", b"\x01", 3), (2, "q", 6, 2.25, -2.67, "CD", b"\x02\x03", 4)]
    assert expected[0] == expected[2] == fitted
    assert expected[1] == [(1, "p", 5, 1.5, 1.005, "AB  ", b"\x01", 3.0), (2, "q", 6, 2.25, -2.665, "CD", b"\x02\x03", 4.0)]
    assert exported({role: pattern}) == expected

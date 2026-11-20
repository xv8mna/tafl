import datetime as dt
import subprocess
import sys
import threading
from decimal import Decimal

import polars as pl
import pytest

import tafl.plugins.text as text_plugin
from tafl.api import ActionError, Cancelled, Context, expressions
from tafl.api.parameters import DateTimeText
from tafl.api.testing import StubContext
from tafl.core import Engine, Parameter, Project


def replace(act, df, columns, pairs, ctx=None, **options):
    params = {"columns": columns, "replacements": [{"find": f, "replace": r} for f, r in pairs], **options}
    return act("text.replace", df, params, ctx)


def test_replace_whole_values(act):
    df = pl.DataFrame({"s": ["open", "OPEN", "done", None, " "]})
    out = replace(act, df, ["s"], [("open", "o"), ("", "missing"), ("done", "")], case_sensitive=False)
    assert out["s"].to_list() == ["o", "o", None, "missing", "missing"]


def test_replace_inside_text_and_regex(act):
    df = pl.DataFrame({"s": ["a-b-A", "Price: $5"]})
    assert replace(act, df, ["s"], [("a", "x")], match="contains")["s"].to_list() == ["x-b-A", "Price: $5"]
    assert replace(act, df, ["s"], [("a", "$")], match="contains", case_sensitive=False)["s"].to_list() == ["$-b-$", "Price: $5"]
    regex = replace(act, df, ["s"], [(r"(\w)-(\w)", "$2+$1")], match="regex")
    assert regex["s"].to_list() == ["b+a-A", "Price: $5"]
    with pytest.raises(ActionError, match="Invalid regular expression"):
        replace(act, df, ["s"], [("(", "x")], match="regex")


CODE = r"^([A-Z]+)([0-9]+)$"


@pytest.mark.parametrize(
    ("replace_with", "expected"),
    [
        ("$1_$2", "PA_1004"),  # before: 1004 (the library read $1_ as a group named "1_")
        ("$1a$2", "PAa1004"),
        ("$1-$2", "PA-1004"),  # as before
        ("${1}_${2}", "PA_1004"),
        ("${01}${+2}", "PA1004"),  # as the library reads them
        ("$0!", "PA1004!"),
        ("$$1", "$1"),  # $$ is a $ sign
        ("US$ $2", "US$ 1004"),  # a $ that names no group too
        ("${1", "${1"),
        ("$", "$"),
        (r"\1-\2", r"\1-\2"),  # a backslash is a normal character, as before
        (r"\0!", r"\0!"),
        (r"\\1", r"\\1"),
        (r"$1\\2", r"PA\\2"),
        (r"C:\data\$2", r"C:\data\1004"),
        (r"D:\Archiv\2026\$1", r"D:\Archiv\2026\PA"),
        (r"\\server\$1", r"\\server\PA"),
        ("\\", "\\"),
    ],
)
def test_replace_with_the_groups_of_a_regular_expression(act, replace_with, expected):
    df = pl.DataFrame({"c": ["PA1004", "x"]})
    out = replace(act, df, ["c"], [(CODE, replace_with)], match="regex")
    assert out["c"].to_list() == [expected, "x"]
    upper = replace(act, df, ["c"], [(CODE.lower(), replace_with)], match="regex", case_sensitive=False)
    assert upper["c"].to_list() == [expected, "x"]


def test_replace_with_named_groups(act):
    df = pl.DataFrame({"c": ["PA1004"]})
    named = r"^(?P<L>[A-Z]+)(?P<N>[0-9]+)$"
    for replace_with, expected in [("${L}_${N}", "PA_1004"), ("$L-$N", "PA-1004"), ("$2/$1", "1004/PA"), (r"\2_$1", r"\2_PA")]:
        assert replace(act, df, ["c"], [(named, replace_with)], match="regex")["c"].to_list() == [expected]
    with pytest.raises(ActionError) as raised:  # the name is the longest one: L_, as before
        replace(act, df, ["c"], [(named, "$L_$N")], match="regex")
    assert str(raised.value) == r"Replace with '$L_$N': the regular expression '" + named + "' has no group named 'L_'. For a $ sign, write $$"


@pytest.mark.parametrize(
    ("replace_with", "problem"),
    [
        ("$3", "has no group 3. For a $ sign, write $$"),
        ("${3}", "has no group 3"),
        ("Price: $amount", "has no group named 'amount'. For a $ sign, write $$"),
        ("${amount}", "has no group named 'amount'"),
    ],
)
def test_a_group_that_the_expression_doesnt_have_stops_replace(act, replace_with, problem):
    """The regex library wrote nothing for it: "Price: $amount" would lose the amount."""
    ctx = StubContext()
    with pytest.raises(ActionError) as raised:
        replace(act, pl.DataFrame({"c": ["PA1004"]}), ["c"], [("x", "y"), (CODE, replace_with)], ctx, match="regex")
    assert str(raised.value) == f"Replace with '{replace_with}': the regular expression '{CODE}' {problem}"
    assert ctx.warnings == []


@pytest.mark.parametrize(
    ("pairs", "warning", "written"),
    [
        ([(CODE, r"\1-\2")], r"Replace with '\1-\2': \1 is the text \1; group 1 is written $1", r"\1-\2"),
        ([(CODE, r"$1/\2"), (CODE, r"\1")], r"Replace with '$1/\2': \2 is the text \2; group 2 is written $2", r"PA/\2"),
        ([(CODE, r"D:\Archiv\2026\$1")], None, r"D:\Archiv\2026\PA"),  # no group 2026: a path
        ([(CODE, r"\0 and \3")], None, r"\0 and \3"),  # group 0 is the whole value, and there is no group 3
        ([(CODE, "$1-$2")], None, "PA-1004"),
    ],
)
def test_a_backslash_before_the_number_of_a_group_gives_a_warning(act, pairs, warning, written):
    """A backslash is a normal character in "Replace with", as before: \\1 is the text \\1. Where
    the expression has that group, the step says once how a group is written; the values stay."""
    ctx = StubContext()
    out = replace(act, pl.DataFrame({"c": ["PA1004"]}), ["c"], pairs, ctx, match="regex")
    assert ctx.warnings == ([warning] if warning else []) and out["c"].to_list() == [written]
    for match in ("whole", "contains"):  # they read no groups: no warning
        quiet = StubContext()
        replace(act, pl.DataFrame({"c": ["PA1004"]}), ["c"], [("PA1004", r"\1")], quiet, match=match)
        assert quiet.warnings == []


def test_only_a_regular_expression_has_groups(act):
    df = pl.DataFrame({"c": ["PA1004"]})
    for match, case_sensitive in (("whole", True), ("whole", False), ("contains", True), ("contains", False)):
        out = replace(act, df, ["c"], [("PA1004", r"$1_\1$$\\2")], match=match, case_sensitive=case_sensitive)
        assert out["c"].to_list() == [r"$1_\1$$\\2"]


def test_replace_in_number_columns(act):
    df = pl.DataFrame({"n": [0, 1, None]})
    assert replace(act, df, ["n"], [("0", "100"), ("", "-1")])["n"].to_list() == [100, 1, -1]
    ctx = StubContext()
    out = replace(act, df, ["n"], [("0", "zero")], ctx)
    assert out["n"].to_list() == ["zero", "1", None]
    assert ctx.warnings == ["'n' was converted to text because 'zero' isn't an integer"]


KEEPS = "'v' was converted to text: Replace keeps the type only of numbers, dates, date-times and true/false values"


@pytest.mark.parametrize(
    ("values", "pairs", "warning"),
    [
        pytest.param([1.5, 2.0], [("1,5", "x")], "'v' was converted to text because 'x' isn't a number", id="number"),
        pytest.param([dt.date(2025, 1, 2)], [("02.01.2025", "soon")], "'v' was converted to text because 'soon' isn't a date", id="date"),
        pytest.param([dt.datetime(2025, 1, 2, 8)], [("x", "")], "'v' was converted to text because 'x' isn't a date-time", id="date-time"),
        pytest.param([True, None], [("ja", "maybe")], "'v' was converted to text because 'maybe' isn't true or false", id="true/false"),
        pytest.param([dt.time(8)], [("08:00:00", "09:00:00")], KEEPS, id="time"),
        pytest.param([dt.timedelta(hours=1)], [("1h", "2h")], KEEPS, id="time span"),
        pytest.param([b"a"], [("a", "b")], KEEPS, id="binary"),
        pytest.param(pl.Series([None, None], dtype=pl.Null), [("", "x")], KEEPS, id="empty"),
        pytest.param(pl.Series(["a"], dtype=pl.Categorical), [("a", "b")], None, id="categories"),
    ],
)
def test_replace_of_whole_values_says_why_a_column_became_text(act, values, pairs, warning):
    """In plain words, with the value that isn't one of the column's type. Before: "'v' was
    converted to text because a value isn't a Time", also when Find and Replace with were times:
    Replace keeps no time in its type. The type of categories is called text: no warning."""
    ctx = StubContext()
    out = replace(act, pl.DataFrame({"v": values}), ["v"], pairs, ctx)
    assert out.schema["v"] == pl.String and ctx.warnings == ([warning] if warning else [])


def test_clean_text(act):
    df = pl.DataFrame({"s": ["  müller  gmbh ", "a\nb", "   "], "zip": [1067, 80331, None]})
    out = act(
        "text.clean",
        df,
        {"columns": ["s"], "collapse_spaces": True, "line_breaks": True, "case": "title"},
    )
    assert out["s"].to_list() == ["Müller Gmbh", "A B", None]
    padded = act("text.clean", df, {"columns": ["zip"], "pad_length": 5})
    assert padded["zip"].to_list() == ["01067", "80331", None]


def test_clean_text_of_a_column_named_twice_cleans_it_once(act):
    """Only a project file written by hand or by a program can name a column twice: the result is
    that of the name once. Before, the step stopped with a technical message."""
    df = pl.DataFrame({"s": ["  müller  gmbh ", "a\nb", "   "], "zip": [1067, 80331, None]})
    params = {"collapse_spaces": True, "line_breaks": True, "case": "title", "pad_length": 12, "pad_char": "."}
    twice = act("text.clean", df, {"columns": ["s", "zip", "s"], **params})
    assert twice.equals(act("text.clean", df, {"columns": ["s", "zip"], **params}))
    assert twice["s"].to_list() == [".Müller Gmbh", ".........A B", "............"]


def test_invalid_regular_expressions_say_why(act):
    df = pl.DataFrame({"s": ["abc"]})
    with pytest.raises(ActionError, match=r"^Invalid regular expression '\(abc': unclosed group$"):
        replace(act, df, ["s"], [("a", "b"), ("(abc", "x")], match="regex")
    with pytest.raises(ActionError, match=r"'a\{2,1\}': invalid repetition count range"):
        replace(act, df, ["s"], [("a{2,1}", "x")], match="regex", case_sensitive=False)


def test_replace_typed_values(act):
    df = pl.DataFrame({"d": [dt.date(2025, 12, 31), None], "n": [1234567890123456789, 1234567890123456768]})
    out = replace(act, df, ["d"], [("31.12.25", "01.01.26")])
    assert out["d"].to_list() == [dt.date(2026, 1, 1), None]
    out = replace(act, df, ["n"], [("1234567890123456789", "1")])
    assert out["n"].to_list() == [1, 1234567890123456768]


def test_a_typed_number_is_read_as_in_filter_rows_and_fill(act):
    """Replace read 1.000 as 1, with the point as the decimal separator, while Filter rows and Fill
    read it as 1000; and 1,000.50 was no number, so the column became text. Now Replace reads typed
    numbers as they do: with both separators the last one is the decimal one; a point alone splits
    thousands when groups of three digits follow it (1.000), else it is the decimal point (1.5)."""
    df = pl.DataFrame(
        {
            "N": [1, 1000, None],
            "F": [1.0, 1000.5, 1.5],
            "E": pl.Series([Decimal("1"), Decimal("1000"), None], dtype=pl.Decimal(10, 2)),
        }
    )
    assert replace(act, df, ["N"], [("1.000", "7")])["N"].to_list() == [1, 7, None]
    assert replace(act, df, ["E"], [("1.000", "7")])["E"].to_list() == [Decimal("1"), Decimal("7"), None]
    ctx = StubContext()
    assert replace(act, df, ["F"], [("1,000.50", "2"), ("1.5", "3")], ctx)["F"].to_list() == [1.0, 2.0, 3.0]
    assert replace(act, df, ["F"], [("1.0000", "9")], ctx)["F"].to_list() == [9.0, 1000.5, 1.5]
    assert ctx.warnings == []
    kept = act("rows.filter", df, {"conditions": [{"column": "N", "operator": "=", "value": "1.000"}]})
    filled = act("rows.fill", df, {"columns": ["N"], "method": "value", "value": "1.000"})
    assert kept["N"].to_list() == [1000] and filled["N"].to_list() == [1, 1000, 1000]


@pytest.mark.parametrize(
    ("typed", "number"),
    [("-€1.000", -1000.0), ("-$1.000", -1000.0), ("€1.000", 1000.0), ("1.000 €", 1000.0), ("1.000-", -1000.0), ("-1.000 €", -1000.0)],
)
def test_a_typed_number_with_a_currency_sign_or_a_minus_at_the_end_is_read_the_same_everywhere(act, typed, number):
    """The separator was decided on the text as typed, so that Filter rows, Fill and Replace read
    these as ±1 (1.000 alone is 1000), while Change type (Comma) read them as ±1000; 0.6.4 said
    "'-€1.000' is not a number". All read them as ±1000 now."""
    df = pl.DataFrame({"Amount": [-1000.0, -1.0, 1.0, 1000.0, None]})
    kept = act("rows.filter", df, {"conditions": [{"column": "Amount", "operator": "=", "value": typed}]})
    assert kept["Amount"].to_list() == [number]
    assert act("rows.fill", df, {"columns": ["Amount"], "method": "value", "value": typed})["Amount"][4] == number
    replaced = replace(act, df, ["Amount"], [(typed, "99")])["Amount"].to_list()
    assert [a for a, b in zip(df["Amount"].to_list(), replaced) if b == 99] == [number]
    changed = act("columns.change_type", pl.DataFrame({"T": [typed]}), {"columns": [{"column": "T", "type": "number", "format": ""}]})
    assert changed["T"].to_list() == [number]


def test_replace_exact_decimals(act):
    ids = [Decimal("12345678901234567890.1234"), Decimal("12345678901234567890.1235"), Decimal("1.1000")]
    df = pl.DataFrame({"n": pl.Series(ids, dtype=pl.Decimal(30, 4))})
    out = replace(act, df, ["n"], [("12345678901234567890,1234", "7,5"), ("1.1", "")])
    assert out["n"].to_list() == [Decimal("7.5"), ids[1], None] and out.schema["n"] == pl.Decimal(30, 4)
    ctx = StubContext()  # a value with more decimal places than the column holds isn't cut
    out = replace(act, df, ["n"], [("1,1", "1,12345")], ctx)
    assert out["n"].to_list() == ["12345678901234567890.1234", "12345678901234567890.1235", "1.1000"]  # as numbers in text
    assert ctx.warnings == ["'n' was converted to text because '1,12345' has more digits than the column's exact decimals hold"]


def test_upper_case_writes_sharp_s_as_ss_like_formulas(act):
    df = pl.DataFrame({"s": ["Größe", "ẞ"]})
    out = act("text.clean", df, {"columns": ["s"], "case": "upper"})
    assert out["s"].to_list() == ["GRÖSSE", "SS"]
    formula = act("calc.columns", df, {"columns": [{"name": "s", "formula": "UPPER([s])"}]})
    assert formula["s"].to_list() == out["s"].to_list()


AS_TEXT = [  # values of other types, and their text, the same on every PC (as Pivot and Split column write it)
    pytest.param(pl.Series([17.0, 2.5, 1e16, -0.0, None]), ["17", "2.5", "10000000000000000", "0", None], id="decimal numbers"),
    pytest.param(pl.Series([0.1, 2.0], dtype=pl.Float32), ["0.1", "2"], id="Float32"),
    pytest.param(
        pl.Series([dt.datetime(2025, 12, 31, 8, 30), dt.datetime(2025, 12, 31, 8, 30, 0, 500000)]),
        ["2025-12-31 08:30:00", "2025-12-31 08:30:00.500"],
        id="date-times",
    ),
    pytest.param(
        pl.Series([dt.datetime(2025, 7, 1, 9, 30)], dtype=pl.Datetime("ns")).dt.replace_time_zone("Europe/Berlin"),
        ["2025-07-01 09:30:00"],
        id="time zone",
    ),
    pytest.param(pl.Series([dt.time(8, 30, 0, 500000), dt.time(8, 30)]), ["08:30:00.500", "08:30:00"], id="times"),
    pytest.param(pl.Series([dt.timedelta(days=1, hours=2, minutes=3), dt.timedelta(seconds=1.5)]), ["1d 2h 3m", "1s 500ms"], id="time spans"),
    pytest.param(pl.Series([b"abc", b"\x00\xff", None]), ["0x616263", "0x00FF", None], id="bytes that are no text"),
    pytest.param(pl.Series([b"abc", "ä".encode()]), ["abc", "ä"], id="bytes that are text"),
    pytest.param(pl.Series([Decimal("2.50"), None], dtype=pl.Decimal(10, 2)), ["2.50", None], id="exact decimals"),
    pytest.param(pl.Series([dt.date(2025, 12, 31)]), ["2025-12-31"], id="dates"),
]


@pytest.mark.parametrize("name", ["v", "*", "^a.*$"])
@pytest.mark.parametrize(("values", "shown"), AS_TEXT)
def test_clean_text_and_replace_make_other_types_the_same_text_on_every_pc(act, values, shown, name):
    """The column becomes text, as before, now the same text on every PC, as Pivot and Split
    column write it: 17, not 17.0; 1e16 as 10000000000000000; 08:30:00.500 (Polars' text left out
    the fractions of a time); a date-time without .000000 and its zone's local time without
    +02:00. Time spans and bytes that are no text stopped the step with a technical error."""
    df = pl.DataFrame({name: values, "abc": [0] * values.len()})  # abc: the column that ^a.*$ would find
    cleaned = act("text.clean", df, {"columns": [name], "trim": "none", "empty_to_null": False})
    assert cleaned.schema[name] == pl.String and cleaned[name].to_list() == shown
    for match in ("contains", "regex"):
        out = replace(act, df, [name], [("§", "-")], match=match)  # finds nothing
        assert out.schema[name] == pl.String and out[name].to_list() == shown
    out = replace(act, df, [name], [(r"^", ">")], match="regex")
    assert out[name].to_list() == [None if text is None else ">" + text for text in shown]
    ctx = StubContext()
    out = replace(act, df, [name], [("x", "y")], ctx)  # "x" is no value of the column's type: the column becomes text
    assert out.schema[name] == pl.String and out[name].to_list() == shown and len(ctx.warnings) == 1
    assert out["abc"].to_list() == [0] * values.len()


def test_replace_of_a_whole_value_finds_a_time_with_its_fractions_of_a_second(act):
    df = pl.DataFrame({"t": [dt.time(8, 30, 0, 500000), dt.time(8, 30)]})
    out = replace(act, df, ["t"], [("08:30:00.500", "half past")])  # a time column becomes text, as before
    assert out["t"].to_list() == ["half past", "08:30:00"]  # before, both times were 08:30:00


@pytest.mark.parametrize(
    ("values", "label"),
    [
        pytest.param([[1, 2], None], "list", id="lists"),
        pytest.param(pl.Series([[1, 2]], dtype=pl.Array(pl.Int64, 2)), "list", id="arrays"),
        pytest.param([{"x": 1, "y": "a"}], "record", id="records"),
        pytest.param(pl.Series([object()], dtype=pl.Object), "object", id="objects"),
    ],
)
def test_clean_text_and_replace_of_lists_and_records_stop_the_step(act, values, label):
    """Before: "cannot cast List type", a record written without the names of its fields ({1,"a"}),
    or "Invalid regular expression: cannot cast 'Object' type"."""
    df = pl.DataFrame({"Tags": values})
    with pytest.raises(ActionError) as raised:
        act("text.clean", df, {"columns": ["Tags"]})
    assert str(raised.value) == f"“Tags” holds {label} values, which Clean text can't change"
    for match in ("whole", "contains", "regex"):
        ctx = StubContext()
        with pytest.raises(ActionError) as raised:
            replace(act, df, ["Tags"], [("1", "2")], ctx, match=match)
        assert str(raised.value) == f"“Tags” holds {label} values, which Replace can't change"
        assert ctx.warnings == []


_WIDE = """
import time
import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Engine, Parameter, Project
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
df = pl.DataFrame({f"N {i}": [i + 0.5, 2.0, None] for i in range(4001)})
params = {"columns": df.columns, "match": "contains", "replacements": [{"find": ".5", "replace": ",5"}]}
start = time.perf_counter()
out = registry.actions["text.replace"](df, params, StubContext())
assert out.row(0)[:2] == ("0,5", "1,5") and out.row(1)[:2] == ("2", "2") and out.row(2)[0] is None, out
assert set(out.schema.values()) == {pl.String}
cleaned = registry.actions["text.clean"](df, {"columns": df.columns, "pad_length": 4}, StubContext())
assert cleaned.row(0)[:2] == ("00.5", "01.5") and cleaned.row(1)[:2] == ("0002", "0002"), cleaned
whole = pl.DataFrame({f"W {i}": [1, 1000, None] for i in range(4001)})  # 1.000 is one thousand, as in Filter rows
params = {"columns": whole.columns, "match": "whole", "replacements": [{"find": "1.000", "replace": "7"}]}
begin = time.perf_counter()
out = registry.actions["text.replace"](whole, params, StubContext())
assert time.perf_counter() - begin < 5, time.perf_counter() - begin  # 22 s when each column read the values again
assert out.row(0) == (1,) * 4001 and out.row(1) == (7,) * 4001 and set(out.schema.values()) == {pl.Int64}, out
print(f"{time.perf_counter() - start:.2f} s")
"""


def test_replace_and_clean_text_of_a_wide_table_of_numbers():
    """4,000 decimal-number columns become text (17, not 17.0), each with its own expression: in a
    process of its own, so that a stack overflow shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _WIDE], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-500:]}"


_MANY_ROWS = r"""
import polars as pl
from PySide6.QtCore import QCoreApplication, QRunnable, QThreadPool

from tafl.core import Registry

registry = Registry()
registry.load_builtin()
replace = registry.actions["text.replace"]
# Row 3,999 finds what row 1 made: the rows apply one after the other, across the groups
inside = [{"find": f"<k{i}>", "replace": f"[{i}]"} for i in range(3999)] + [{"find": "[1]", "replace": "one"}]
pattern = [{"find": f"<k({i})>", "replace": "[$1]"} for i in range(3999)] + [{"find": r"\[1\]", "replace": "one"}]
whole = [{"find": f"k{i}", "replace": f"v{i}"} for i in range(4000)] + [{"find": "k1", "replace": "later"}]
texts = pl.DataFrame({"T": ["<k1>", "a <k3998> b", "<K2><k0>", None]})
values = pl.DataFrame({"T": ["k1", "K2", "x", "k3999", None]})
cases = [
    ("contains", True, inside, texts, ["one", "a [3998] b", "<K2>[0]", None]),
    ("contains", False, inside, texts, ["one", "a [3998] b", "[2][0]", None]),
    ("regex", True, pattern, texts, ["one", "a [3998] b", "<K2>[0]", None]),
    ("regex", False, pattern, texts, ["one", "a [3998] b", "[2][0]", None]),
    ("whole", True, whole, values, ["v1", "K2", "x", "v3999", None]),  # the first row that finds a value counts
    ("whole", False, whole, values, ["v1", "v2", "x", "v3999", None]),
]
found = []


class Job(QRunnable):  # as the window calculates a step: in a thread of a pool, with its smaller stack
    def run(self):
        for match, case_sensitive, rows, df, expected in cases:
            print(match, case_sensitive, flush=True)
            params = {"columns": ["T"], "match": match, "case_sensitive": case_sensitive, "replacements": rows}
            found.append((replace(df, params)["T"].to_list(), expected))


app = QCoreApplication([])
pool = QThreadPool()
pool.start(Job())
pool.waitForDone()
assert len(found) == len(cases) and all(got == expected for got, expected in found), found
print("ok")
"""


def test_replace_with_thousands_of_rows_in_each_mode():
    """4,000 rows of Replace in a thread of a pool, as the window calculates a step: before, one
    expression for all rows overflowed the stack from about 450 rows (Text inside, Regular
    expression) or 1,200 (Whole cell value), and the program ended. In a process of its own."""
    done = subprocess.run([sys.executable, "-c", _MANY_ROWS], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and done.stdout.endswith("ok\n"), f"exit code {done.returncode:#x}: {done.stdout[-300:]} {done.stderr[-500:]}"


_ONE_EXPRESSION = r"""
import json
import random

import polars as pl

import tafl.plugins.text as text_plugin
from tafl.api import ActionError
from tafl.api.testing import StubContext
from tafl.core import Engine, Parameter, Project
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
replace = registry.actions["text.replace"]
rng = random.Random(17)
ALPHABET = "aAbB$1 -é"
REGEX = [
    (r"(a)(b)", ["$2$1", "${1}x", "$$", "[$1]"]),
    (r"(?P<x>a)b", ["${x}", "$x-", "$$x"]),
    (r"a+", ["A", "$$", "", "aa"]),
    (r"[ab]$", ["!", "$$1"]),
    (r"(a|b)(\$)", ["$2$1", "${2}"]),
    (r"é", ["e", "ée"]),
]
# Well below the sizes at which one expression overflowed the stack on Windows: 450 rows (Text
# inside, Regular expression) with Python 3.12 and 3.14; 1,200 (Whole cell value) with 3.14, 750
# with 3.12
SIZES = {
    "contains": [99, 100, 101, 201, 250],
    "regex": [99, 100, 101, 201, 250],
    "whole": [99, 100, 101, 201, 500],
    "numbers": [101],  # each row is read as numbers: slow
}


def word(low=0, high=6):
    return "".join(rng.choice(ALPHABET) for _ in range(rng.randint(low, high)))


def case(mode, case_sensitive, size):
    if mode == "numbers":
        df = pl.DataFrame({"I": [rng.choice([None, *range(25), 1000]) for _ in range(40)], "F": [rng.choice([None, 0.5, 2.5, 1.0, 1000.0]) for _ in range(40)]})
        texts = ["", " ", "1", "2", "7", "1.000", "2,5", "0,5", "24", "abc"]
        rows = [{"find": rng.choice(texts), "replace": rng.choice(["", "9", "5", "1.000", "x"])} for _ in range(size)]
        return df, {"columns": ["I", "F"], "match": "whole", "replacements": rows}
    df = pl.DataFrame({name: [None if rng.random() < 0.1 else word(0, 8) for _ in range(40)] for name in ("s", "t")}, schema={"s": pl.String, "t": pl.String})
    values = [value for value in df["s"] if value is not None]
    rows = []
    for _ in range(size):  # what replaces is never longer than what it finds: no text grows row by row
        if mode == "regex" and rng.random() < 0.6:
            find, replacements = rng.choice(REGEX)
            rows.append({"find": find, "replace": rng.choice(replacements)})
        elif mode == "whole":  # the same value found by rows of different groups: the first one counts
            find = rng.choice(values) if rng.random() < 0.5 else ("" if rng.random() < 0.1 else word(0, 4))
            rows.append({"find": find, "replace": "" if rng.random() < 0.15 else word()})
        else:
            find = word(1, 3)
            rows.append({"find": find, "replace": word(0, len(find)).replace("$", "$$" if mode == "regex" else "$")})
    if mode != "whole":  # row 1 makes text that the last row finds; "Replace with" has a $ sign
        rows[0], rows[-1] = {"find": "aB", "replace": "b$"}, {"find": "b$" if mode == "contains" else r"b\$", "replace": "1"}
    return df, {"columns": ["s", "t"], "match": mode, "case_sensitive": case_sensitive, "replacements": rows}


def run(df, params):
    ctx = StubContext()
    try:
        out = replace(df, params, ctx)
        return {"schema": str(out.schema), "rows": out.rows(), "warnings": ctx.warnings}
    except ActionError as e:
        return {"error": str(e)}


checked, grouping = 0, text_plugin._ROWS_AT_ONCE
assert grouping < 99, grouping  # every case has several groups
for mode, sizes in SIZES.items():
    for case_sensitive in (True,) if mode == "numbers" else (True, False):
        for size in sizes:
            df, params = case(mode, case_sensitive, size)
            grouped = run(df, params)
            text_plugin._ROWS_AT_ONCE = 10**9  # all rows in one expression, as before
            expected = run(df, params)
            text_plugin._ROWS_AT_ONCE = grouping
            assert grouped == expected, json.dumps([mode, case_sensitive, size, grouped, expected], default=str)[:2000]
            assert "error" not in grouped, grouped
            checked += 1
print(f"ok {checked}")
"""


def test_replace_in_groups_of_rows_gives_what_one_expression_gave():
    """An oracle: random rows (seeded) in each mode, also case-insensitive, give the same values,
    types, warnings and errors in groups of rows as with all rows in one expression, as Replace
    worked before. The cases stay below the sizes at which one expression overflowed the stack.
    In a process of its own."""
    done = subprocess.run([sys.executable, "-c", _ONE_EXPRESSION], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0 and done.stdout.startswith("ok "), f"exit code {done.returncode:#x}: {done.stdout[-300:]} {done.stderr[-2000:]}"


def test_replace_of_whole_values_in_groups_keeps_the_columns_named_like_its_own(act):
    """The rows of later groups hold their result in a column of their own, whose name the table
    doesn't have: the columns "A (replaced)" and "A (replaced) 2" stay as they are."""
    df = pl.DataFrame({"A": ["k1", "k11", "x"], "A (replaced)": ["r1", "r2", "r3"], "A (replaced) 2": ["s1", "s2", "s3"], "B": [1, 2, 3]})
    out = replace(act, df, ["A"], [(f"k{i}", f"v{i}") for i in range(12)], match="whole")  # 12 rows: two groups
    assert out.columns == df.columns and out["A"].to_list() == ["v1", "v11", "x"]
    assert out.drop("A").equals(df.drop("A"))


@pytest.mark.parametrize(("match", "count"), [("contains", 100), ("regex", 100), ("whole", 100), ("whole", 15)])
def test_a_stop_takes_effect_at_the_next_group_of_rows(act, monkeypatch, match, count):
    """Many rows of Replace can take a minute (100,000 rows of a regular expression): a Stop
    doesn't wait for the end of the step, but stops before the next group of rows."""
    cancel, groups = threading.Event(), []
    changed = text_plugin._changed

    def first_group_and_stop(df, updates):
        groups.append(len(updates))
        cancel.set()  # Stop while the first group is calculated
        return changed(df, updates)

    monkeypatch.setattr(text_plugin, "_changed", first_group_and_stop)
    rows = [{"find": f"k{i}", "replace": "x"} for i in range(count)]  # 15: two groups
    with pytest.raises(Cancelled):
        act("text.replace", pl.DataFrame({"T": ["k1"]}), {"columns": ["T"], "match": match, "replacements": rows}, Context(cancel=cancel))
    assert groups == [1]  # the next group didn't start


NUMBERS = [(str(i), str(i + 1)) for i in range(50)]


@pytest.mark.parametrize(
    ("reading", "values", "match", "pairs"),
    [
        ("_typed_pairs", [1, 2], "whole", NUMBERS),  # numbers: about 8 ms a row
        ("_typed_pairs", [dt.datetime(2026, 1, 1)], "whole", [(f"2026-01-01 08:{i:02}", "2027-01-01") for i in range(50)]),
        ("_made_text", [1, 2], "whole", [*NUMBERS[:-1], ("x", "1")]),  # which row made the column text
        ("_check_regex", ["k1"], "regex", [(f"^k{i}$", "v") for i in range(50)]),
        ("_replacement", ["k1"], "regex", [(f"^k{i}$", "v") for i in range(50)]),
        ("_backslash_group", ["k1"], "regex", [(f"^k{i}$", "v") for i in range(50)]),
    ],
)
def test_a_stop_takes_effect_while_replace_reads_its_rows(act, monkeypatch, reading, values, match, pairs):
    """Before its first group, Replace reads all rows: as values of the column's type, or as
    regular expressions. Thousands of them take half a minute (3,000 rows of numbers took 21 s):
    a Stop during this doesn't wait for the rest of the rows."""
    cancel, calls = threading.Event(), []
    read = getattr(text_plugin, reading)

    def stop_while_reading(*args):
        calls.append("called")
        cancel.set()  # Stop while the rows are read
        result = read(*args)
        calls.append("returned")
        return result

    monkeypatch.setattr(text_plugin, reading, stop_while_reading)
    rows = [{"find": find, "replace": new} for find, new in pairs]
    with pytest.raises(Cancelled):
        act("text.replace", pl.DataFrame({"T": values}), {"columns": ["T"], "match": match, "replacements": rows}, Context(cancel=cancel))
    once_for_all_rows = reading in ("_typed_pairs", "_made_text")  # else once for each row
    assert calls == (["called"] if once_for_all_rows else ["called", "returned"])  # the next row wasn't read


def test_a_stop_takes_effect_while_replace_reads_the_rows_for_a_second_type_of_column(act, monkeypatch):
    """Columns of two types in "In columns" read the rows once for each type, here for a date
    column, then for a number column: a Stop while the rows are read for the second type doesn't
    wait for the rest of them either."""
    cancel, calls = threading.Event(), []
    read = text_plugin._typed_pairs

    def stop_in_the_second_reading(pairs, dtype):
        calls.append(f"read for {dtype}")
        if sum(call.startswith("read") for call in calls) == 2:
            cancel.set()  # Stop while the rows are read for the second type
        result = read(pairs, dtype)
        calls.append("returned")
        return result

    monkeypatch.setattr(text_plugin, "_typed_pairs", stop_in_the_second_reading)
    rows = [{"find": find, "replace": new} for find, new in NUMBERS]
    df = pl.DataFrame({"D": [dt.date(2026, 1, 1)], "N": [1]})
    with pytest.raises(Cancelled):
        act("text.replace", df, {"columns": ["D", "N"], "match": "whole", "replacements": rows}, Context(cancel=cancel))
    assert calls == ["read for Date", "returned", "read for Int64"]  # the second reading stopped at its next row


ZONED = pl.Series([dt.datetime(2026, 1, 1, 7, 30), dt.datetime(2026, 7, 1, 7, 30), None]).dt.replace_time_zone("UTC")


@pytest.mark.parametrize("unit", ["ms", "us", "ns"])
@pytest.mark.parametrize(
    ("zone", "shown", "other"),
    [
        ("Europe/Berlin", ["2026-01-01 08:30", "01.07.2026 09:30"], "2026-01-01 07:30"),  # other: the time in UTC
        ("UTC", ["2026-01-01 07:30", "01.07.2026 07:30"], "2026-01-01 08:30"),  # other: the time in Berlin
        ("America/New_York", ["2026-01-01 02:30", "2026-07-01 03:30"], "2026-01-01 07:30"),
    ],
)
def test_replace_of_a_whole_date_time_in_a_column_with_a_time_zone_finds_the_time_as_shown(act, zone, shown, other, unit):
    """A date-time in Find and Replace with is the local time of the column's time zone, as the grid
    shows its values, as in Filter rows. Before, Find stopped the step with a technical message."""
    df = pl.DataFrame({"D": ZONED.dt.convert_time_zone(zone).dt.cast_time_unit(unit), "N": [1, 2, 3]})
    out = replace(act, df, ["D"], [(shown[0], "2026-02-01 10:00"), (shown[1], "31.12.2026"), ("", "2026-12-24 18:00")], match="whole")
    assert out.schema == df.schema  # the same type, time zone and unit
    local = out["D"].dt.replace_time_zone(None).to_list()
    assert local == [dt.datetime(2026, 2, 1, 10), dt.datetime(2026, 12, 31), dt.datetime(2026, 12, 24, 18)]
    assert replace(act, df, ["D"], [(other, "2026-02-01 10:00")], match="whole").equals(df)  # another time: no change
    assert replace(act, df, ["D"], [("2300-01-01 10:00", "2026-02-01")], match="whole").equals(df)  # beyond nanoseconds: none
    assert replace(act, df, ["D"], [(shown[0], "")], match="whole")["D"].to_list() == [None, *df["D"].to_list()[1:]]


@pytest.mark.parametrize(
    ("pairs", "problem"),
    [
        ([("2026-03-29 02:30", "2027-01-01")], "Find: '2026-03-29 02:30' doesn't happen in the time zone of column 'D' (Europe/Berlin): a clock change skips it"),
        ([("2026-10-25 02:30", "2027-01-01")], "Find: '2026-10-25 02:30' happens twice in the time zone of column 'D' (Europe/Berlin): a clock change repeats it"),
        ([("", "29.03.2026 02:30")], "Replace with: '29.03.2026 02:30' doesn't happen in the time zone of column 'D' (Europe/Berlin): a clock change skips it"),
        ([("", " 2026-10-25 02:59:59 ")], "Replace with: '2026-10-25 02:59:59' happens twice in the time zone of column 'D' (Europe/Berlin): a clock change repeats it"),
        ([("2026-01-01 08:30", "2026-01-02")] * 12 + [("2026-01-02", "2026-03-29 02:00")], "Replace with: '2026-03-29 02:00' doesn't happen"),
    ],
)
def test_replace_of_a_whole_date_time_at_a_clock_change_stops_with_a_message(act, pairs, problem):
    """A time that a clock change skips (spring) or repeats (autumn) in the column's time zone is
    not one moment: the step stops with a message, as in Filter rows. Before, a Find stopped with a
    technical message, a new value that the clock skips too, and one that it repeats became the
    first of the two times without a message. In a column in UTC, the same times are just times."""
    df = pl.DataFrame({"D": ZONED.dt.convert_time_zone("Europe/Berlin"), "U": ZONED})
    with pytest.raises(ActionError) as error:
        replace(act, df, ["U", "D"], pairs, match="whole")
    assert str(error.value).startswith(problem)
    times = {"2026-03-29 02:30", "2026-10-25 02:30", "2026-03-29 02:00", "2026-10-25 02:59:59"}
    assert replace(act, df, ["U"], [(t, "2026-10-25 02:30") for t in times], match="whole")["U"].equals(ZONED)  # no such time in U
    assert replace(act, df, ["D"], [("2026-03-29 03:00", "2026-10-25 03:00")], match="whole").equals(df)  # after the clock changes
    ctx = StubContext()  # a Replace with that is no date-time: the column becomes text, and Find is a text
    assert replace(act, df, ["D"], [("2026-03-29 02:30", "x")], ctx, match="whole")["D"].dtype == pl.String
    assert ctx.warnings == ["'D' was converted to text because 'x' isn't a date-time"]


@pytest.mark.parametrize("find", ["300", "-1", "2"])
def test_replace_of_a_whole_number_compares_as_before_in_a_narrow_column(act, find):
    """Only a column with a time zone compares in its own type: a Find of 300 or -1 doesn't stop
    the step in a column of small whole numbers (0 to 255), it finds nothing, as before."""
    df = pl.DataFrame({"n": pl.Series([1, 2, 200], dtype=pl.UInt8)})
    out = replace(act, df, ["n"], [(find, "5")], match="whole")
    assert out.schema == df.schema and out["n"].to_list() == ([1, 5, 200] if find == "2" else [1, 2, 200])


def replaced_rows(out: pl.DataFrame) -> list[int]:
    """The numbers N of the rows whose D Replace made 2000-01-01."""
    return [n for n, d in zip(out["N"], out["D"], strict=True) if d is not None and d.year == 2000]


@pytest.mark.parametrize(
    "local",  # on this PC's clock: winter, summer with microseconds, and the times of Berlin's clock changes
    [dt.datetime(2026, 1, 1, 8, 30), dt.datetime(2026, 7, 1, 10, 0, 0, 123_457), dt.datetime(2026, 10, 25, 2, 30), dt.datetime(2026, 3, 29, 2, 30)],
)
@pytest.mark.parametrize("unit", ["us", "ns"])
@pytest.mark.parametrize("zone", ["UTC", "Europe/Berlin", "America/New_York"])
def test_replace_of_a_whole_date_time_with_a_date_time_parameter_alone_is_its_moment(act, zone, unit, local):
    """A date-time parameter alone in Find or Replace with, such as {Now}, is the moment that it
    holds (a time of this PC's clock), as Filter rows and Fill empty values read it, without a
    check of the clock change: it is one moment. The same digits typed are the local time of the
    column's zone, as the grid shows it. Before, a Find stopped with a technical message, and
    Replace with wrote the digits as the local time of the column's zone: in a column whose zone
    isn't this PC's, another moment."""
    value = DateTimeText(local.isoformat(sep=" "), local)  # the setting "{Now}", filled in
    moment = expressions.moment(local).dt.convert_time_zone(zone).dt.cast_time_unit(unit)
    as_shown = pl.Series([local]).dt.replace_time_zone(zone, ambiguous="earliest", non_existent="null").dt.cast_time_unit(unit)
    other = pl.Series([dt.datetime(2020, 1, 1)]).dt.replace_time_zone(zone).dt.cast_time_unit(unit)
    df = pl.DataFrame({"D": pl.concat([moment, as_shown, other, pl.Series([None], dtype=moment.dtype)]), "N": [1, 2, 3, 4]})
    kept = act("rows.filter", df, {"conditions": [{"column": "D", "operator": "=", "value": value}]})["N"].to_list()
    assert replaced_rows(replace(act, df, ["D"], [(value, "2000-01-01")], match="whole")) == kept and 1 in kept
    filled = act("rows.fill", df, {"columns": ["D"], "method": "value", "value": value})
    assert replace(act, df, ["D"], [("", value)], match="whole").equals(filled)
    typed = [(str(value), "2000-01-01")]
    if zone == "Europe/Berlin" and local.month in (3, 10):
        with pytest.raises(ActionError, match="a clock change"):
            replace(act, df, ["D"], typed, match="whole")
    else:
        shown = [n for n, d in zip(df["N"], df["D"], strict=True) if d is not None and d == as_shown[0]]
        assert replaced_rows(replace(act, df, ["D"], typed, match="whole")) == shown and 2 in shown


@pytest.mark.parametrize("value", ["=TIMESTAMP '2026-01-01 08:30:00'", "2026-01-01 08:30"])
def test_a_date_time_parameter_of_a_project_in_replace_is_the_moment_that_filter_rows_finds(registry, tmp_path, value):
    """Through the engine: {Cut} alone in a row of Replace is filled in as a date-time parameter
    (a formula or a fixed value), as in Filter rows and Fill empty values."""
    moment = expressions.moment(dt.datetime(2026, 1, 1, 8, 30))  # 08:30 of this PC's clock, in UTC
    digits = pl.Series([dt.datetime(2026, 1, 1, 8, 30)]).dt.replace_time_zone("UTC")  # 08:30 in UTC
    utc = pl.concat([moment, digits, pl.Series([None], dtype=moment.dtype)])
    pl.DataFrame({"D": utc, "N": [1, 2, 3]}).write_parquet(tmp_path / "times.parquet")
    project = Project(name="Cut", parameters=[Parameter("Cut", value)])
    project.path = tmp_path / "cut.tafl"
    steps = {
        "Filter": ("rows.filter", {"conditions": [{"column": "D", "operator": "=", "value": "{Cut}"}]}),
        "Find": ("text.replace", {"columns": ["D"], "match": "whole", "replacements": [{"find": "{Cut}", "replace": "2000-01-01"}]}),
        "Fill": ("rows.fill", {"columns": ["D"], "method": "value", "value": "{Cut}"}),
        "New": ("text.replace", {"columns": ["D"], "match": "whole", "replacements": [{"find": "", "replace": "{Cut}"}]}),
    }
    for name, (action_id, params) in steps.items():
        table = project.add_table(name)
        project.add_step(table, "parquet.import", {"file": "times.parquet"})
        project.add_step(table, action_id, params)
    engine = Engine(project, registry)
    results = {name: engine.evaluate(name).df for name in steps}
    kept = results["Filter"]["N"].to_list()  # the moment, and 08:30 in UTC too on a PC in UTC
    assert replaced_rows(results["Find"]) == kept and 1 in kept
    assert results["New"].equals(results["Fill"]) and results["New"]["D"][2] == moment[0]


def test_replace_with_a_date_time_parameter_in_a_column_without_a_time_zone_reads_its_digits(act):
    """Without a time zone, a date-time parameter alone is its date and time, as before and as in
    Filter rows."""
    local = dt.datetime(2026, 7, 1, 10, 0, 0, 123_457)
    value = DateTimeText(local.isoformat(sep=" "), local)
    df = pl.DataFrame({"D": [local, dt.datetime(2020, 1, 1), None], "N": [1, 2, 3]})
    assert replaced_rows(replace(act, df, ["D"], [(value, "2000-01-01")], match="whole")) == [1]
    assert act("rows.filter", df, {"conditions": [{"column": "D", "operator": "=", "value": value}]})["N"].to_list() == [1]
    assert replace(act, df, ["D"], [("", value)], match="whole")["D"].to_list() == [local, dt.datetime(2020, 1, 1), local]


@pytest.mark.parametrize(
    ("match", "pairs"),
    [("whole", [("a", "ab")]), ("contains", [("a", "ab")]), ("regex", [("^(a)", "${1}b")])],  # twice would give "abb"
)
def test_replace_changes_a_column_named_twice_once(act, match, pairs):
    """A column twice in "In columns" (only a project file written by hand or by a program can
    have it) is changed once. Before, the step stopped with a message about a regular expression."""
    df = pl.DataFrame({"A": ["a", "b"], "B": ["a", "b"]})
    out = replace(act, df, ["A", "B", "A"], pairs, match=match)
    assert out.columns == ["A", "B"] and out["A"].to_list() == out["B"].to_list() == ["ab", "b"]
    ctx = StubContext()
    out = replace(act, pl.DataFrame({"N": [1, 2]}), ["N", "N"], [("1", "x")], ctx, match="whole")
    assert out["N"].to_list() == ["x", "2"] and len(ctx.warnings) == 1  # 'N' was converted to text …, once

import datetime as dt
import subprocess
import sys
from decimal import Decimal

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.testing import StubContext


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
        (r"\1-\2", "PA-1004"),  # before: \1-\2
        (r"\2\1", "1004PA"),
        ("$1-$2", "PA-1004"),  # as before
        ("${1}_${2}", "PA_1004"),
        ("${01}${+2}", "PA1004"),  # as the library reads them
        ("$0!", "PA1004!"),
        (r"\0!", "PA1004!"),
        ("$$1", "$1"),  # $$ is a $ sign
        ("US$ $2", "US$ 1004"),  # a $ that names no group too
        ("${1", "${1"),
        ("$", "$"),
        (r"\\1", r"\1"),  # a backslash before a digit
        (r"$1\\2", r"PA\2"),
        (r"C:\data\$2", r"C:\data\1004"),  # other backslashes are text, as before
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
    for replace_with, expected in [("${L}_${N}", "PA_1004"), ("$L-$N", "PA-1004"), ("$2/$1", "1004/PA"), (r"\2_\1", "1004_PA")]:
        assert replace(act, df, ["c"], [(named, replace_with)], match="regex")["c"].to_list() == [expected]
    with pytest.raises(ActionError) as raised:  # the name is the longest one: L_, as before
        replace(act, df, ["c"], [(named, "$L_$N")], match="regex")
    assert str(raised.value) == r"Replace with '$L_$N': the regular expression '" + named + "' has no group named 'L_'. For a $ sign, write $$"


@pytest.mark.parametrize(
    ("replace_with", "problem"),
    [
        ("$3", "has no group 3. For a $ sign, write $$"),
        ("${3}", "has no group 3"),
        (r"D:\Archiv\2026\$1", r"has no group 2026. For a backslash before a digit, write two: \\2026"),
        ("Price: $amount", "has no group named 'amount'. For a $ sign, write $$"),
        ("${amount}", "has no group named 'amount'"),
    ],
)
def test_a_group_that_the_expression_doesnt_have_stops_replace(act, replace_with, problem):
    """The regex library wrote nothing for it: D:\\Archiv\\2026\\$1 would lose a part of the path."""
    ctx = StubContext()
    with pytest.raises(ActionError) as raised:
        replace(act, pl.DataFrame({"c": ["PA1004"]}), ["c"], [("x", "y"), (CODE, replace_with)], ctx, match="regex")
    assert str(raised.value) == f"Replace with '{replace_with}': the regular expression '{CODE}' {problem}"
    assert ctx.warnings == []


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
    Replace keeps no time in its type. Categories are text in the grid: no warning."""
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


AS_SHOWN = [  # values of other types, and their text as the grid shows it
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
@pytest.mark.parametrize(("values", "shown"), AS_SHOWN)
def test_clean_text_and_replace_change_other_types_as_the_grid_shows_them(act, values, shown, name):
    """The column becomes text, as before, now with the grid's text: 17, not 17.0; 1e16 as
    10000000000000000; 08:30:00.500 (Polars' text left out the fractions of a time); a date-time
    without .000000 and its zone's local time without +02:00. Time spans and bytes that are no
    text stopped the step with a technical error."""
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


def test_replace_of_a_whole_value_finds_the_text_of_the_grid(act):
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
print(f"{time.perf_counter() - start:.2f} s")
"""


def test_replace_and_clean_text_of_a_wide_table_of_numbers():
    """4,000 decimal-number columns become text as the grid shows them, each with its own
    expression: in a process of its own, so that a stack overflow shows as a failed test."""
    done = subprocess.run([sys.executable, "-c", _WIDE], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-500:]}"

"""Split column: the text of a column into new columns, or into rows."""

import datetime as dt
import time
from decimal import Decimal

import polars as pl
import pytest

from tafl.api import ActionError, ValidationContext
from tafl.api.testing import StubContext
from tafl.core import Engine, Project, Status

NAMES = pl.DataFrame({"Id": [1, 2, 3], "Name": ["Müller, Hans", "Schmidt, Eva", "Weber, Jo"], "City": ["Bonn", "Ulm", "Kiel"]})


def split(act, df, column, ctx=None, names=("", ""), **options):
    params = {"column": column, "parts": [{"name": name} for name in names], **options}
    return act("text.split", df, params, ctx)


def parts(out, *columns):
    return [list(row) for row in out.select(columns).rows()]


# --- the action and its settings ---------------------------------------------------------------


def test_identity_defaults_and_help(registry):
    action = registry.actions["text.split"]
    assert (action.name, action.category, action.kind, action.version) == ("Split column", "Text", "transform", 1)
    assert action.resolve({}) == {
        "column": "",
        "into": "columns",
        "separator": ",",
        "other": "",
        "trim": True,
        "parts": [{"name": ""}, {"name": ""}],
        "keep": False,
    }
    assert action.description.startswith("Splits the text of a column at a separator, into columns or into rows.\n\n")
    assert "\"Müller, Hans, Dr.\" gives Müller and Hans, Dr." in action.description
    labels = [label for _, label in action.params[2].options]
    assert labels == ["Comma ,", "Semicolon ;", "Space", "Tab", "Line break", "Other text"]


def test_problems_before_the_run(registry):
    action = registry.actions["text.split"]
    vc = ValidationContext(columns=["Id", "Name"])
    assert action.check(action.resolve({"column": "Name"}), vc) == []
    assert action.check(action.resolve({}), vc) == ["Column: required"]
    assert action.check(action.resolve({"column": "Nme"}), vc) == ["Column: column 'Nme' not found (did you mean 'Name'?)"]
    other = action.resolve({"column": "Name", "separator": "other"})
    assert action.check(other, vc) == ["Separator text: required"]
    one = action.resolve({"column": "Name", "parts": [{"name": "A"}]})
    assert action.check(one, vc) == ["New columns (one per part): add at least 2 rows"]
    one["into"] = "rows"  # the new columns don't count when splitting into rows
    assert action.check(one, vc) == []
    assert action.check(action.resolve({"column": "Name", "separator": "|"}), vc) == [
        "Separator: '|' is not one of: ,, ;, space, tab, line, other"
    ]


def test_summaries(registry):
    action = registry.actions["text.split"]

    def summary(**values):
        return action.describe(action.resolve({"column": "Name", **values}))

    assert summary(parts=[{"name": "Last name"}, {"name": " First name "}]) == "Split Name at ',' into Last name, First name"
    assert summary(separator=";") == "Split Name at ';' into Name 1, Name 2"
    assert summary(separator="space", parts=[{}, {"name": "Rest"}, {}]) == "Split Name at spaces into Name 1, Rest, Name 3"
    assert summary(separator="tab") == "Split Name at tabs into Name 1, Name 2"
    assert summary(separator="line", into="rows") == "Split Name at line breaks into rows"
    assert summary(separator="other", other="-") == "Split Name at '-' into Name 1, Name 2"
    assert summary(separator="other", other=" / ", into="rows") == "Split Name at ' / ' into rows"
    assert summary(separator=";", into="rows") == "Split Name at ';' into rows"
    assert summary(separator="|") == "Split column"  # a broken value gives the name


def test_summaries_of_a_new_step_show_what_is_missing(registry):
    project = Project(name="Test")
    table = project.add_table("Orders")
    engine = Engine(project, registry)
    new = project.add_step(table, registry.actions["text.split"], {})
    assert engine.describe(new) == "Split ? at ',' into 2 columns"
    named = project.add_step(table, registry.actions["text.split"], {"parts": [{"name": "A"}, {"name": "B"}]})
    assert engine.describe(named) == "Split ? at ',' into A, B"
    other = project.add_step(table, registry.actions["text.split"], {"column": "Code", "separator": "other", "into": "rows"})
    assert engine.describe(other) == "Split Code at ? into rows"  # not at a question mark
    question = project.add_step(table, registry.actions["text.split"], {"column": "Url", "separator": "other", "other": "/"})
    assert engine.describe(question) == "Split Url at '/' into Url 1, Url 2"


# --- into columns ------------------------------------------------------------------------------


def test_into_columns_in_place(act):
    out = split(act, NAMES, "Name", names=("Last name", "First name"))
    assert out.columns == ["Id", "Last name", "First name", "City"]
    assert out.schema["Last name"] == out.schema["First name"] == pl.String
    assert parts(out, "Last name", "First name") == [["Müller", "Hans"], ["Schmidt", "Eva"], ["Weber", "Jo"]]
    assert out["Id"].to_list() == [1, 2, 3] and out["City"].to_list() == ["Bonn", "Ulm", "Kiel"]


def test_more_parts_go_into_the_last_column_with_a_message(act):
    df = pl.DataFrame({"Name": ["Müller, Hans, Dr.", "Weber, Jo", "a,b,c,d"]})
    ctx = StubContext()
    out = split(act, df, "Name", ctx)
    assert parts(out, "Name 1", "Name 2") == [["Müller", "Hans, Dr."], ["Weber", "Jo"], ["a", "b,c,d"]]
    assert ctx.messages == [
        ("info", "2 values have more parts than there are new columns: the last column has the rest, e.g. “Müller, Hans, Dr.”")
    ]
    ctx = StubContext()
    out = split(act, df, "Name", ctx, names=("", "", "", ""))
    assert parts(out, "Name 1", "Name 2", "Name 3", "Name 4") == [["Müller", "Hans", "Dr.", None], ["Weber", "Jo", None, None], ["a", "b", "c", "d"]]
    assert ctx.messages == []


def test_the_example_of_more_parts_is_short_and_on_one_line(act):
    df = pl.DataFrame({"Notes": ["one", "first line\r\nsecond\nthird\rfourth, and more", "x, y"]})
    ctx = StubContext()
    split(act, df, "Notes", ctx, separator="line")
    assert ctx.messages == [
        ("info", "1 value has more parts than there are new columns: the last column has the rest, e.g. “first line second third fourth…”")
    ]
    many = pl.DataFrame({"Code": ["a-b-c"] * 1500 + ["a-b"]})
    ctx = StubContext()
    out = split(act, many, "Code", ctx, separator="other", other="-")
    assert out["Code 2"][0] == "b-c" and out["Code 2"][-1] == "b"
    assert ctx.messages == [("info", "1,500 values have more parts than there are new columns: the last column has the rest, e.g. “a-b-c”")]
    tabs = pl.DataFrame({"T": ["a\tb", "tab\tsep\tx"]})
    ctx = StubContext()
    split(act, tabs, "T", ctx, separator="tab")
    assert ctx.messages == [("info", "1 value has more parts than there are new columns: the last column has the rest, e.g. “tab sep x”")]


def test_a_line_break_or_tab_at_the_end_is_not_one_more_part(act):
    """As into rows: with the spaces around the parts removed, there is nothing after it."""
    df = pl.DataFrame({"Notes": ["Title\r\nBody\r\n", "a\r\nb\n"], "T": ["a\tb\t", "x\ty"]})
    ctx = StubContext()
    out = split(act, df, "Notes", ctx, separator="line")
    assert parts(out, "Notes 1", "Notes 2") == [["Title", "Body"], ["a", "b"]] and ctx.messages == []
    assert split(act, df, "Notes", separator="line", into="rows")["Notes"].to_list() == ["Title", "Body", "a", "b"]
    ctx = StubContext()
    out = split(act, df, "T", ctx, separator="tab")
    assert parts(out, "T 1", "T 2") == [["a", "b"], ["x", "y"]] and ctx.messages == []
    ctx = StubContext()  # the spaces stay: the last column shows the line break
    out = split(act, df, "Notes", ctx, separator="line", trim=False)
    assert out["Notes 2"].to_list() == ["Body\r\n", "b\n"]
    assert ctx.messages == [
        ("info", "2 values have more parts than there are new columns: the last column has the rest, e.g. “Title Body ”")
    ]


def test_values_of_nothing_but_spaces_have_no_parts_to_report(act):
    df = pl.DataFrame({"S": ["   ", "a b", "  ", "x y z"]})
    ctx = StubContext()
    out = split(act, df, "S", ctx, separator="other", other=" ")
    assert parts(out, "S 1", "S 2") == [[None, None], ["a", "b"], [None, None], ["x", "y z"]]
    assert ctx.messages == [("info", "1 value has more parts than there are new columns: the last column has the rest, e.g. “x y z”")]
    ctx = StubContext()
    out = split(act, pl.DataFrame({"S": [",,,", "a,b,,"]}), "S", ctx)
    assert parts(out, "S 1", "S 2") == [[None, ",,"], ["a", "b,,"]]  # the rest is there as it is
    assert ctx.messages[0][1].startswith("2 values have more parts")


def test_fewer_parts_and_empty_values_give_empty_values(act):
    df = pl.DataFrame({"Name": ["Schmidt", None, "", "   ", ", Hans", "Weber,", " , "]})
    out = split(act, df, "Name", names=("Last", "First", "Title"))
    assert parts(out, "Last", "First", "Title") == [
        ["Schmidt", None, None],
        [None, None, None],
        [None, None, None],
        [None, None, None],
        [None, "Hans", None],
        ["Weber", None, None],
        [None, None, None],
    ]


def test_spaces_around_the_parts(act):
    df = pl.DataFrame({"Name": ["  Müller ,  Hans  ", "a,   ", "\tb\t,c"]})
    trimmed = split(act, df, "Name")
    assert parts(trimmed, "Name 1", "Name 2") == [["Müller", "Hans"], ["a", None], ["b", "c"]]
    kept = split(act, df, "Name", trim=False)
    assert parts(kept, "Name 1", "Name 2") == [["  Müller ", "  Hans  "], ["a", None], ["\tb\t", "c"]]


@pytest.mark.parametrize(
    ("separator", "other", "value", "expected"),
    [
        (",", "", "a,b;c d", ["a", "b;c d"]),
        (";", "", "a,b;c d", ["a,b", "c d"]),
        ("space", "", "  Hans   Peter  Müller ", ["Hans", "Peter  Müller"]),
        ("tab", "", "a\tb c\td", ["a", "b c\td"]),
        ("line", "", "a\r\nb\nc", ["a", "b\nc"]),
        ("line", "", "a\r\nb\r\nc", ["a", "b\r\nc"]),
        ("line", "", "a\rb\rc", ["a", "b\rc"]),
        ("line", "", "a\rb", ["a", "b"]),
        ("line", "", "a\nb", ["a", "b"]),
        ("other", ".", "www.example.com", ["www", "example.com"]),
        ("other", "+", "1+2+3", ["1", "2+3"]),
        ("other", " - ", "Berlin - Mitte - Nord", ["Berlin", "Mitte - Nord"]),
        ("other", "|", "a|b", ["a", "b"]),
        ("other", "\\", "C:\\Data\\in", ["C:", "Data\\in"]),
        ("other", "$1", "x$1y", ["x", "y"]),
    ],
)
def test_separators(act, separator, other, value, expected):
    out = split(act, pl.DataFrame({"t": [value]}), "t", separator=separator, other=other)
    assert out.row(0) == tuple(expected)


def test_space_parts_without_trim(act):
    """With Space, runs of spaces are one separator and spaces at start and end make no parts,
    also without removing spaces around the parts."""
    out = split(act, pl.DataFrame({"t": ["  Hans   Peter  "]}), "t", separator="space", trim=False, names=("", "", ""))
    assert out.row(0) == ("Hans", "Peter", None)


@pytest.mark.parametrize("trim", [True, False])
def test_the_last_column_gets_the_rest_as_it_is(act, trim):
    """With Space and Line break too, only the separators between the parts before it count as one
    (or as a line break): the rest keeps its own spaces and line breaks."""
    df = pl.DataFrame(
        {
            "Notes": ["Subject\r\nDear Sir,\r\nthe invoice ...", "Subject\rold Mac\rtext", "a\r\n\r\nb\r\nc", "a\r\nb", None],
            "Words": ["Main Street   12   b", "  Hans   Peter  Müller  ", "a  b   c  d\r\n e", "a  b", "   "],
        }
    )
    lines = split(act, df, "Notes", separator="line", trim=trim, names=("", "", ""))
    assert parts(lines, "Notes 1", "Notes 2", "Notes 3") == [
        ["Subject", "Dear Sir,", "the invoice ..."],
        ["Subject", "old Mac", "text"],
        ["a", None, "b\r\nc"],
        ["a", "b", None],
        [None, None, None],
    ]
    assert split(act, df, "Notes", separator="line", trim=trim)["Notes 2"].to_list() == [
        "Dear Sir,\r\nthe invoice ...", "old Mac\rtext", "\r\nb\r\nc" if not trim else "b\r\nc", "b", None,
    ]
    words = split(act, df, "Words", separator="space", trim=trim, names=("", "", ""))
    assert parts(words, "Words 1", "Words 2", "Words 3") == [
        ["Main", "Street", "12   b"],
        ["Hans", "Peter", "Müller" if trim else "Müller  "],
        ["a", "b", "c  d\r\n e"],
        ["a", "b", None],
        [None, None, None],
    ]
    assert split(act, df, "Words", separator="space", trim=trim)["Words 2"].to_list() == [
        "Street   12   b", "Peter  Müller" if trim else "Peter  Müller  ", "b   c  d\r\n e", "b", None,
    ]


def test_names_automatic_or_given(act):
    out = split(act, NAMES, "Name", names=(" Last ", "", ""))
    assert out.columns == ["Id", "Last", "Name 2", "Name 3", "City"]
    same = split(act, NAMES, "Name", names=("Name", "First"))  # the split column goes away: its name is free
    assert same.columns == ["Id", "Name", "First", "City"] and same["Name"].to_list() == ["Müller", "Schmidt", "Weber"]


def test_keep_the_original_column(act):
    out = split(act, NAMES, "Name", keep=True)
    assert out.columns == ["Id", "Name", "Name 1", "Name 2", "City"]
    assert out["Name"].equals(NAMES["Name"]) and out["Name 2"].to_list() == ["Hans", "Eva", "Jo"]
    last = split(act, NAMES.select("Id", "Name"), "Name", keep=True)
    assert last.columns == ["Id", "Name", "Name 1", "Name 2"]


def test_names_that_collide(act):
    with pytest.raises(ActionError) as raised:
        split(act, NAMES, "Name", names=("A", "B", " A"))
    assert str(raised.value) == "Two new columns are named “A” (rows 1 and 3). Give each one its own name."
    with pytest.raises(ActionError) as raised:
        split(act, NAMES, "Name", names=("Name 2", ""))  # the second one is named Name 2 too
    assert str(raised.value) == "Two new columns are named “Name 2” (rows 1 and 2). Give each one its own name."
    with pytest.raises(ActionError) as raised:
        split(act, NAMES, "Name", names=("Last", "City"))
    assert str(raised.value) == "New columns, row 2: the table has a column named “City” already. Give the new column another name."
    with pytest.raises(ActionError) as raised:
        split(act, NAMES, "Name", names=("Name", "First"), keep=True)
    assert str(raised.value) == "New columns, row 1: the table has a column named “Name” already. Give the new column another name."
    with pytest.raises(ActionError) as raised:
        split(act, NAMES.with_columns(pl.lit(1).alias("Name 1")), "Name")
    assert str(raised.value) == "New columns, row 1: the table has a column named “Name 1” already. Give the new column another name."


def test_other_types_are_split_as_text(act):
    df = pl.DataFrame(
        {
            "Number": [1234, -5, None],
            "Date": [dt.date(2025, 12, 31), dt.date(2026, 1, 5), None],
            "Decimal": pl.Series([Decimal("5.25"), Decimal("-0.50"), None], dtype=pl.Decimal(10, 2)),
            "Time": [dt.time(8, 30, 0, 500000), dt.time(17, 5), None],
            "Code": pl.Series(["a-b", "c", None], dtype=pl.Categorical),
        }
    )
    number = split(act, df, "Number", separator="other", other="3")
    assert parts(number, "Number 1", "Number 2") == [["12", "4"], ["-5", None], [None, None]]
    date = split(act, df, "Date", separator="other", other="-", names=("Year", "Month", "Day"))
    assert parts(date, "Year", "Month", "Day") == [["2025", "12", "31"], ["2026", "01", "05"], [None, None, None]]
    decimal = split(act, df, "Decimal", separator="other", other=".")
    assert parts(decimal, "Decimal 1", "Decimal 2") == [["5", "25"], ["-0", "50"], [None, None]]
    times = split(act, df, "Time", separator="other", other=":", names=("h", "m", "s"))
    assert parts(times, "h", "m", "s") == [["08", "30", "00.500"], ["17", "05", "00"], [None, None, None]]
    code = split(act, df, "Code", separator="other", other="-", into="rows")
    assert code["Code"].to_list() == ["a", "b", "c", None] and code.schema["Code"] == pl.String
    empty = split(act, pl.DataFrame({"x": [None, None]}), "x")  # a column with nothing in it
    assert parts(empty, "x 1", "x 2") == [[None, None], [None, None]] and empty.schema["x 1"] == pl.String


def test_date_times_and_decimal_numbers_are_split_as_pivot_names_them(act):
    """Fractions of a second only when there are some, no time zone offset, 2 and not 2.0, no
    exponent, every digit that tells a decimal number apart."""
    moments = pl.Series([dt.datetime(2026, 1, 1, 8, 30), dt.datetime(2026, 1, 1, 8, 30, 0, 500_000), None])
    for column in (moments, moments.dt.replace_time_zone("Europe/Berlin"), moments.cast(pl.Datetime("ns"))):
        out = split(act, column.to_frame("When"), "When", separator="space", names=("Date", "Time"))
        assert parts(out, "Date", "Time") == [["2026-01-01", "08:30:00"], ["2026-01-01", "08:30:00.500"], [None, None]]
    nanos = moments.head(1).cast(pl.Datetime("ns")).dt.offset_by("1ns")
    assert parts(split(act, nanos.to_frame("W"), "W", separator="other", other="."), "W 1", "W 2") == [
        ["2026-01-01 08:30:00", "000000001"]
    ]
    numbers = pl.DataFrame({"F": [2.0, 2.5, 1e16, 0.1 + 0.2, -0.0, 1e-7, None]})
    out = split(act, numbers, "F", separator="other", other=".")
    assert parts(out, "F 1", "F 2") == [
        ["2", None], ["2", "5"], ["10000000000000000", None], ["0", "30000000000000004"], ["0", None], ["0", "0000001"],
        [None, None],
    ]
    floats = pl.DataFrame({"F": pl.Series([0.1, 2.0], dtype=pl.Float32)})
    assert parts(split(act, floats, "F", separator="other", other="."), "F 1", "F 2") == [["0", "1"], ["2", None]]
    rows = split(act, numbers.head(2), "F", separator="other", other=".", into="rows")
    assert rows["F"].to_list() == ["2", "2", "5"]
    for into in ("columns", "rows"):  # without rows
        assert split(act, numbers.clear(), "F", separator="other", other=".", into=into, keep=True).height == 0


@pytest.mark.parametrize(
    ("series", "label"),
    [
        (pl.Series("v", [[1, 2], None]), "list(int64)"),
        (pl.Series("v", [{"a": 1}, None]), "struct({'a': int64})"),
        (pl.Series("v", [dt.timedelta(hours=2), None]), "duration"),
        (pl.Series("v", [b"a,b", None]), "binary"),
    ],
)
def test_values_that_cant_be_split(act, series, label):
    for into in ("columns", "rows"):
        with pytest.raises(ActionError) as raised:
            split(act, series.to_frame(), "v", into=into)
        assert str(raised.value) == f"“v” holds {label} values, which can't be split"


def test_empty_table_into_columns(act):
    empty = NAMES.clear()
    ctx = StubContext()
    out = split(act, empty, "Name", ctx, names=("Last", ""), keep=True)
    assert out.height == 0 and out.columns == ["Id", "Name", "Last", "Name 2", "City"]
    assert out.schema["Last"] == out.schema["Name 2"] == pl.String and out.schema["Id"] == pl.Int64
    assert ctx.messages == []
    for separator in ("space", "line"):
        out = split(act, empty, "Name", separator=separator)
        assert out.height == 0 and out.columns == ["Id", "Name 1", "Name 2", "City"]


# --- into rows ---------------------------------------------------------------------------------


def test_into_rows(act):
    df = pl.DataFrame(
        {
            "Id": [1, 2],
            "Tags": ["A;B;C", "x"],
            "Day": [dt.date(2025, 1, 1), dt.date(2025, 1, 2)],
            "Amount": [1.5, None],
        }
    )
    out = split(act, df, "Tags", separator=";", into="rows")
    assert out.columns == df.columns and out.schema == {**df.schema, "Tags": pl.String}
    assert out.rows() == [
        (1, "A", dt.date(2025, 1, 1), 1.5),
        (1, "B", dt.date(2025, 1, 1), 1.5),
        (1, "C", dt.date(2025, 1, 1), 1.5),
        (2, "x", dt.date(2025, 1, 2), None),
    ]


def test_into_rows_leaves_out_empty_parts_but_never_a_row(act):
    df = pl.DataFrame({"Id": [1, 2, 3, 4, 5, 6], "Tags": [" a ; ;b;", None, "", ";;", "  ", "c"]})
    out = split(act, df, "Tags", separator=";", into="rows")
    assert out.rows() == [(1, "a"), (1, "b"), (2, None), (3, None), (4, None), (5, None), (6, "c")]
    kept = split(act, df, "Tags", separator=";", into="rows", trim=False)
    assert kept.rows() == [(1, " a "), (1, "b"), (2, None), (3, None), (4, None), (5, None), (6, "c")]


def test_into_rows_at_line_breaks_and_spaces(act):
    df = pl.DataFrame({"Id": [1, 2], "Notes": ["first\r\nsecond\n\nthird\r", "  x   y "]})
    lines = split(act, df.head(1), "Notes", separator="line", into="rows")
    assert lines["Notes"].to_list() == ["first", "second", "third"]
    words = split(act, df.tail(1), "Notes", separator="space", into="rows", trim=False)
    assert words["Notes"].to_list() == ["x", "y"]


def test_into_rows_with_many_parts(act):
    df = pl.DataFrame({"Id": [1, 2], "Codes": [",".join(f"c{i}" for i in range(10_000)), "z"]})
    out = split(act, df, "Codes", into="rows")
    assert out.height == 10_001 and out["Codes"][9_999] == "c9999" and out["Id"].to_list()[-2:] == [1, 2]


def test_empty_table_into_rows(act):
    out = split(act, NAMES.clear(), "Name", into="rows")
    assert out.height == 0 and out.schema == NAMES.schema
    numbers = split(act, pl.DataFrame({"n": pl.Series([], dtype=pl.Int64)}), "n", into="rows")
    assert numbers.height == 0 and numbers.schema == {"n": pl.String}


# --- in a project ------------------------------------------------------------------------------


def test_in_a_project(registry, tmp_path):
    (tmp_path / "people.csv").write_text("Id;Name\n1;Müller, Hans, Dr.\n2;Weber, Jo\n", encoding="utf-8")
    project = Project(name="Test")
    project.path = tmp_path / "test.tafl"
    people = project.add_table("People")
    project.add_step(people, "csv.import", {"file": "people.csv"})
    step = project.add_step(people, "text.split", {"column": "Name", "parts": [{"name": "Last name"}, {"name": "First name"}]})
    engine = Engine(project, registry)
    result = engine.evaluate("People")
    assert result.steps[1].status == Status.OK
    assert result.df.rows() == [(1, "Müller", "Hans, Dr."), (2, "Weber", "Jo")]
    assert result.steps[1].messages == [
        ("info", "1 value has more parts than there are new columns: the last column has the rest, e.g. “Müller, Hans, Dr.”")
    ]
    assert engine.describe(step) == "Split Name at ',' into Last name, First name"


# --- speed -------------------------------------------------------------------------------------


def _best(run, rounds=3) -> float:
    times = []
    for _ in range(rounds):
        start = time.perf_counter()
        run()
        times.append(time.perf_counter() - start)
    return min(times)


@pytest.mark.parametrize("separator", [",", "space"])
def test_a_million_rows_into_columns(act, separator):
    """The step stays close to a plain Polars split (the bound is generous)."""
    rows = 1_000_000
    df = pl.select(
        Id=pl.int_range(rows),
        Name=pl.format("{}, {}", pl.int_range(rows).cast(pl.String), (pl.int_range(rows) * 7).cast(pl.String)),
    )
    names = pl.col("Name").str.splitn(",", 2).struct.rename_fields(["Last", "First"])
    plain = _best(lambda: df.with_columns(names.alias("Name")).unnest("Name"))
    params = {"column": "Name", "separator": separator, "parts": [{"name": "Last"}, {"name": "First"}]}
    step = _best(lambda: act("text.split", df, params))
    out = act("text.split", df, params)
    assert out.columns == ["Id", "Last", "First"] and out.row(1) == ((1, "1", "7") if separator == "," else (1, "1,", "7"))
    assert step < 3 * plain + 0.2, (step, plain)


def test_many_new_columns_without_removing_spaces(act):
    """The value is split once for all the parts: the time grows with the number of new columns,
    not with its square."""
    rows, count = 100_000, 60
    df = pl.select(Codes=pl.concat_str([pl.int_range(rows).cast(pl.String)] * count, separator=";"))
    names = [f"c{i}" for i in range(count)]
    plain = _best(lambda: df.select(pl.col("Codes").str.splitn(";", count).struct.rename_fields(names)).unnest("Codes"))
    params = {"column": "Codes", "separator": ";", "trim": False, "parts": [{"name": name} for name in names]}
    step = _best(lambda: act("text.split", df, params))
    out = act("text.split", df, params)
    assert out.columns == names and out.row(2) == ("2",) * count
    assert step < 3 * plain + 0.2, (step, plain)


@pytest.mark.parametrize(("separator", "joint"), [("space", "  "), ("line", "\r\n")])
def test_the_rest_after_thousands_of_parts(act, separator, joint):
    """More parts than one pattern of Polars can skip (about 9,500 for a space): a piece at a time."""
    count = 12_000
    df = pl.DataFrame({"t": [joint.join(f"w{i}" for i in range(count + 2))] * 3})
    params = {"column": "t", "separator": separator, "parts": [{"name": f"c{i}"} for i in range(count)]}
    start = time.perf_counter()
    out = act("text.split", df, params)
    assert time.perf_counter() - start < 5
    assert out.width == count and out.row(2)[-2:] == ("w11998", f"w11999{joint}w12000{joint}w12001")


def test_a_million_rows_into_rows(act):
    rows = 1_000_000
    df = pl.select(Id=pl.int_range(rows), Tags=pl.format("{};x; y ", pl.int_range(rows).cast(pl.String)))
    plain = _best(lambda: df.with_columns(pl.col("Tags").str.split(";")).explode("Tags"))
    params = {"column": "Tags", "separator": ";", "into": "rows"}
    step = _best(lambda: act("text.split", df, params))
    out = act("text.split", df, params)
    assert out.height == 3 * rows and out.row(2) == (0, "y")
    assert step < 3 * plain + 0.2, (step, plain)

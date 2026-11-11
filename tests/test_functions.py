"""The described list of formula functions."""

import datetime as dt
import statistics

import polars as pl
import pytest

from tafl.api import expressions
from tafl.core import functions
from tafl.core.functions import CURATED, CURSOR, MORE

SAMPLE = pl.DataFrame(
    {
        "Name": ["Anna ", "bob", None],
        "First": ["Anna", "Bob", "Carl"],
        "Last": ["Ek", None, "Li"],
        "Amount": [10, 2000, 150],
        "Net": [8.4, 1680.0, 126.0],
        "Price": [1.5, 2.25, 3.0],
        "Date": [dt.date(2025, 1, 31), dt.date(2025, 6, 30), dt.date(2024, 2, 29)],
        "Phone": ["089 1234", None, "+49-30"],
        "Mobile": [None, "0170 1", None],
        "Country": ["DE", "FR", "AT"],
        "Zip": ["01067", "80331", "10115"],
        "Code": ["A-123", "B-7", "007"],
        "Path": ["2025\\01\\x.csv", "2024\\12\\y.csv", "z.csv"],
        "File": ["orders_2025-01-02.csv", "x.txt", "y.csv"],
        "Street": ["Main 1", None, "High 2"],
        "City": ["Berlin", "Paris", None],
        "Customer": ["C1", "C2", "C1"],
    }
)


@pytest.mark.parametrize("function", [f for f in CURATED if f.example], ids=lambda f: f.name)
def test_every_example_works(function):
    expressions.evaluate(SAMPLE, function.example)


def test_moving_average_standard_deviation_and_change_in_percent_give_what_their_notes_say():
    """The new entries of "Groups and order": the first rows, empty values and 0 as the notes say."""
    df = pl.DataFrame({"Date": [dt.date(2025, 1, day) for day in range(1, 8)], "Amount": [64, 80, 0, 50, None, 120, 60]})
    entries = {f.name: f for f in CURATED}
    moving = expressions.evaluate(df, entries["Moving average"].example).to_list()
    assert moving == pytest.approx([64, 72, 48, 130 / 3, 25, 85, 90])  # the first rows: of the rows there are
    spread = expressions.evaluate(df, entries["STDDEV_SAMP ... OVER"].example).to_list()
    assert spread == pytest.approx([statistics.stdev([64, 80, 0, 50, 120, 60])] * 7)  # empty values don't count
    last = expressions.evaluate(df, "STDDEV_SAMP([Amount]) OVER (ORDER BY [Date] ROWS BETWEEN 2 PRECEDING AND CURRENT ROW)")
    assert last[0] is None and last[1] == pytest.approx(statistics.stdev([64, 80]))  # one value: empty
    change = expressions.evaluate(df, entries["Change in %"].example).to_list()
    assert change == [None, 25.0, -100.0, None, None, None, -50.0]  # first row, after 0, empty, after empty
    assert [f.name for f in functions.search("moving")][:1] == ["Moving average"]
    assert functions.search("stdev")[0].name == "STDDEV_SAMP ... OVER"
    assert "Change in %" in [f.name for f in functions.search("percent")]


def test_the_list_is_complete_and_consistent():
    names = [f.name for f in functions.all_functions()]
    assert len(names) == len(set(names))
    assert all(f.syntax and f.summary and f.category in functions.CATEGORIES for f in functions.all_functions())
    assert all(f.template.count(CURSOR) <= 1 for f in functions.all_functions())
    helpers = {f.name for f in CURATED if f.helper}
    assert helpers == {"IIF", "ISNULL", "LEN", "CHARINDEX", "GETDATE"}  # every helper Tafl adds is described
    assert functions.CURATED[0].template == f"IIF({CURSOR}, , )"
    assert next(f for f in CURATED if f.name == "UPPER").template == f"UPPER({CURSOR})"  # from the syntax
    assert next(f for f in CURATED if f.name == "+ - * /").template == ""  # nothing to insert


def test_duckdb_adds_its_other_functions():
    more = functions.more_functions()
    assert len(more) > 300 and all(f.category == MORE and f.name.isupper() for f in more)
    curated = {f.name for f in CURATED}
    assert not curated & {f.name for f in more}
    assert "SUM" not in {f.name for f in more}  # covered by "SUM ... OVER"
    avg = next(f for f in more if f.name == "AVG")
    assert "OVER" in avg.note  # aggregates need OVER in a formula


def test_search():
    assert functions.search("round")[0].name == "ROUND"
    assert [f.name for f in functions.search("sum")][:2] == ["SUM ... OVER", "Running total"]  # curated first
    assert functions.search("month name")[0].name == "MONTHNAME"  # every word must match
    assert functions.search("zzz") == []
    dates = functions.search("", functions.DATES)
    assert dates and all(f.category == functions.DATES for f in dates)


def test_names_from_sql_server_and_spreadsheets_are_found():
    assert functions.search("dateadd")[0].name == "+ INTERVAL"
    assert functions.search("datepart")[0].name == "DATE_PART"  # also written DATEPART
    assert functions.search("newid")[0].name == "GEN_RANDOM_UUID"
    assert functions.search("eomonth")[0].name == "LAST_DAY"
    assert functions.search("round")[1].name in ("CEIL", "FLOOR", "TRUNC")  # names and summaries before notes


def test_duckdbs_list_is_cleaned_up():
    more = {f.name: f for f in functions.more_functions()}
    assert not {"SLEEP_MS", "CURRENT_QUERY", "SUM_NO_OVERFLOW", "NEXTVAL", "DATEPART"} & set(more)
    assert more["MAKE_TIMESTAMP"].syntax.count(",") == 5  # the overload with all parameters
    assert all("[" not in f.syntax and not f.summary.endswith(" ") for f in more.values())
    assert more["FMOD"].summary == "No description from DuckDB"  # listed although undocumented

"""Exporting into a database table (all modes) and running SQL commands."""

import datetime as dt
import decimal
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid

import polars as pl
import pytest

from tafl.api import ActionError, Cancelled
from tafl.api.testing import StubContext
from tafl.core import Engine, Project

DATA = pl.DataFrame(
    {
        "OrderNo": [1, 2, 3],
        "Customer": ["Müller", "Bäcker", None],
        "Net": [10.5, float("nan"), 3.0],
        "Day": [dt.date(2025, 1, 1), dt.date(2025, 1, 1), dt.date(2025, 1, 2)],
        "Paid": [True, False, None],
    }
)


@pytest.fixture
def db(tmp_path, registry):
    """export(df, **params) into a SQLite file; rows(sql) reads it back."""
    path = tmp_path / "shop.db"
    ctx = StubContext(mode="run", connections={"shop": (registry.connectors["sqlite"], {"file": str(path)})})

    class Shop:
        context = ctx

        def export(self, df, **params):
            return registry.actions["db.export"](df, {"connection": "shop", "table": "orders", **params}, ctx)

        def command(self, sql):
            return registry.actions["db.command"](DATA, {"connection": "shop", "sql": sql}, ctx)

        def rows(self, sql="SELECT * FROM orders ORDER BY OrderNo"):
            with sqlite3.connect(path) as con:
                return con.execute(sql).fetchall()

    return Shop()


def test_add_rows_creates_the_table(db):
    assert db.export(DATA) is DATA  # the table passes through
    assert db.rows("SELECT name, type FROM pragma_table_info('orders')") == [
        ("OrderNo", "INTEGER"), ("Customer", "TEXT"), ("Net", "REAL"), ("Day", "TEXT"), ("Paid", "INTEGER"),
    ]
    assert db.rows() == [(1, "Müller", 10.5, "2025-01-01", 1), (2, "Bäcker", None, "2025-01-01", 0), (3, None, 3.0, "2025-01-02", None)]
    db.export(DATA)
    assert len(db.rows()) == 6
    assert db.context.messages[-1] == ("info", "orders: added 3 rows")


def test_replace_all_rows(db):
    db.export(DATA)
    db.export(DATA.head(1), mode="replace")
    assert db.rows() == [(1, "Müller", 10.5, "2025-01-01", 1)]
    assert db.context.messages[-1] == ("info", "orders: deleted 3 rows, added 1 row")


def test_replace_the_rows_of_the_same_days(db):
    db.export(DATA)
    reload = pl.DataFrame({"OrderNo": [7, 8], "Day": [dt.date(2025, 1, 2), dt.date(2025, 1, 2)]})
    db.export(reload, mode="replace_keys", key_columns=["Day"])
    assert [(r[0], r[3]) for r in db.rows()] == [(1, "2025-01-01"), (2, "2025-01-01"), (7, "2025-01-02"), (8, "2025-01-02")]
    empty_day = pl.DataFrame({"OrderNo": [9], "Day": [None]}, schema={"OrderNo": pl.Int64, "Day": pl.Date})
    db.export(empty_day, mode="replace_keys", key_columns=["Day"])
    db.export(empty_day, mode="replace_keys", key_columns=["Day"])  # empty matches empty: replaced, not doubled
    assert db.rows("SELECT COUNT(*) FROM orders WHERE Day IS NULL") == [(1,)]


def test_delete_where_a_condition_is_true_then_add(db):
    db.export(DATA)
    db.export(DATA.tail(1), mode="replace_where", condition="Day = '2025-01-02'")
    assert len(db.rows()) == 3 and db.context.messages[-1] == ("info", "orders: deleted 1 row, added 1 row")


def test_update_and_upsert_by_key(db):
    db.export(DATA)
    changes = pl.DataFrame({"OrderNo": [1, 4], "Net": [99.0, 4.0]})
    db.export(changes, mode="update", key_columns=["OrderNo"])
    assert db.rows("SELECT OrderNo, Net, Customer FROM orders ORDER BY OrderNo") == [
        (1, 99.0, "Müller"), (2, None, "Bäcker"), (3, 3.0, None),  # only Net changes; 4 is not there
    ]
    db.export(changes.with_columns(pl.col("Net") + 1), mode="upsert", key_columns=["OrderNo"])
    assert db.rows("SELECT OrderNo, Net FROM orders ORDER BY OrderNo") == [(1, 100.0), (2, None), (3, 3.0), (4, 5.0)]
    assert db.context.messages[-1] == ("info", "orders: updated 1 row, added 1 row")


def test_delete_rows_with_the_same_keys(db):
    db.export(DATA)
    db.export(DATA.filter(pl.col("OrderNo") != 2), mode="delete", key_columns=["OrderNo"])
    assert [r[0] for r in db.rows()] == [2]


def test_mistakes_are_explained(db):
    with pytest.raises(ActionError, match="Table orders not found"):
        db.export(DATA, mode="update", key_columns=["OrderNo"])
    with pytest.raises(ActionError, match="Table orders not found"):
        db.export(DATA, create=False)
    db.export(DATA)
    with pytest.raises(ActionError, match="don't identify the rows: 1 key value occurs more than once"):
        db.export(DATA, mode="upsert", key_columns=["Day"])
    with pytest.raises(ActionError, match="Choose the key columns"):
        db.export(DATA, mode="update", key_columns=[])
    extra = DATA.with_columns(pl.lit("x").alias("Remark"))
    with pytest.raises(ActionError, match="has no column Remark"):
        db.export(extra)
    assert len(db.rows()) == 3  # the failed exports changed nothing (one transaction)
    db.export(extra, extra_columns="skip")
    assert len(db.rows()) == 6


def test_the_example_of_a_repeated_key_is_the_same_every_time(db):
    """The first key value in the order of the rows that occurs more than once (before, a random one)."""
    db.export(DATA)
    keys = ["00001", *(f"{100 + i % 40:05d}" for i in range(400))]  # 00001 once, 40 values ten times
    data = pl.DataFrame({"OrderNo": range(401), "Customer": keys, "Day": [dt.date(2025, 1, 1 + i % 3) for i in range(401)]})
    texts = set()
    for _ in range(20):
        for key_columns in (["Customer"], ["Customer", "Day"]):
            with pytest.raises(ActionError) as error:
                db.export(data, mode="update", key_columns=key_columns)
            texts.add(str(error.value))
    assert sorted(texts) == [
        "The key columns don't identify the rows: 120 key values occur more than once "
        "(e.g. Customer = 00100, Day = 2025-01-02). Remove the duplicates first, or choose more key columns.",
        "The key columns don't identify the rows: 40 key values occur more than once (e.g. Customer = 00100). "
        "Remove the duplicates first, or choose more key columns.",
    ]
    with pytest.raises(ActionError, match=r"2 key values occur more than once \(e\.g\. OrderNo = 1\)"):  # not 2, seen again first
        db.export(pl.DataFrame({"OrderNo": [1, 2, 2, 1]}), mode="update", key_columns=["OrderNo"])


def test_a_key_column_named_len(tmp_path):
    """Polars names the count of a group "len": a key column of that name must not clash with it."""
    from tafl.plugins.databases import _check_unique

    _check_unique(pl.DataFrame({"len": [1, 2], "_len_": [1, 1]}), ["len", "_len_"])  # unique: no error
    with pytest.raises(ActionError, match=r"1 key value occurs more than once \(e\.g\. len = 2\)"):
        _check_unique(pl.DataFrame({"len": [1, 2, 2]}), ["len"])


def test_a_failed_export_changes_nothing(db):
    db.export(DATA)
    with pytest.raises(ActionError):
        db.export(DATA, mode="replace_where", condition="no_such_column = 1")
    assert len(db.rows()) == 3


def test_a_failed_export_creates_no_table(db):
    with pytest.raises(ActionError):
        db.export(DATA, mode="replace_where", condition="no_such_column = 1")
    assert db.rows("SELECT name FROM sqlite_master WHERE name = 'orders'") == []


def test_no_permission_is_not_a_missing_table():
    """Only "no such table" means the export creates the table; other errors are shown."""
    from tafl.api.database import Connector, Database

    class Cursor:
        def __init__(self, error):
            self.error = error

        def execute(self, sql):
            raise self.error

        def close(self):
            pass

    def columns(error):
        raw = type("Raw", (), {"cursor": lambda self: Cursor(error)})()
        return Database(Connector(), raw).columns("orders")

    assert columns(Exception("no such table: orders")) is None
    assert columns(Exception('relation "orders" does not exist')) is None
    with pytest.raises(ActionError, match="permission was denied"):
        columns(Exception("The SELECT permission was denied on the object 'orders'"))
    assert columns(Exception("Relation »orders« existiert nicht")) is None  # unknown words: as before, missing


def test_a_view_whose_table_is_gone_is_not_a_missing_table(db):
    db.export(DATA)
    db.command("CREATE VIEW broken AS SELECT * FROM orders; DROP TABLE orders")
    with pytest.raises(ActionError, match="no such table: main.orders"):  # not "view broken already exists"
        db.export(DATA, table="broken")


def test_names_with_doubled_quotes():
    from tafl.api import split_name

    assert split_name('"my ""big"" table"') == ['my "big" table']
    assert split_name("[dbo].[a]]b]") == ["dbo", "a]b"]
    assert split_name("dbo.Orders") == ["dbo", "Orders"]


def test_a_temporary_sqlite_table_is_refused(db):
    with pytest.raises(ActionError, match="temporary table"):
        db.export(DATA, table="temp.orders")


def test_messages_of_the_sql_server_driver():
    """The ODBC driver finds some bad values before the server; its own (localized) text names no
    column or value."""
    from tafl.core import Registry

    connector = Registry.default().connectors["sqlserver"]
    driver = "[22018] [Microsoft][ODBC Driver 17 for SQL Server]Ung�ltiger Zeichenwert (0) (SQLExecute)"
    assert connector.error_message(Exception("22018", driver)) == (
        "A value doesn't fit its column in the database: a value can't be turned into the column's type "
        "(e.g. text in a number or date column)"
    )
    assert "a text is too long" in connector.error_message(Exception("String data, right truncation: length 24 buffer 20", "HY000"))
    server = "[22018] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Conversion failed when converting the nvarchar value 'x' to data type int. (245) (SQLExecDirectW)"
    assert connector.error_message(Exception("22018", server)) == "Conversion failed when converting the nvarchar value 'x' to data type int."


def test_an_empty_table_can_be_left_out(db, registry, tmp_path):
    """With "Don't export an empty table", no rows mean no change: no file, no deleted rows."""
    empty = DATA.clear()
    db.export(empty, skip_empty=True)
    assert not (tmp_path / "shop.db").exists()
    assert db.context.messages == [("info", "Not exported: the table is empty")]
    db.export(DATA)
    db.export(empty, mode="replace", skip_empty=True)
    db.export(pl.DataFrame(), mode="replace", skip_empty=True)  # no columns either: nothing to do
    assert len(db.rows()) == 3 and db.context.messages[-1] == ("info", "Not exported: the table is empty")
    db.export(empty, mode="replace")  # without it, as before: all rows are deleted
    assert db.rows() == [] and db.context.messages[-1] == ("info", "orders: deleted 3 rows")
    assert registry.actions["db.export"].upgrade({"connection": "shop", "table": "orders"}, 1)["skip_empty"] is False


def test_created_table_is_reported_only_when_the_export_succeeds(db):
    with pytest.raises(ActionError):
        db.export(DATA, mode="replace_where", condition="no_such_column = 1")
    assert db.context.messages == []  # the table was created, then rolled back
    db.export(DATA)
    assert db.context.messages == [("info", "Created table orders"), ("info", "orders: added 3 rows")]


def test_date_times_with_a_time_zone_keep_their_local_time(db):
    """As in the Excel export, and the same in SQL Server: 08:30 in Berlin stays 08:30."""
    at = pl.Series("At", [dt.datetime(2026, 10, 1, 8, 30), dt.datetime(2026, 7, 1, 8, 30, 0, 500)])
    db.export(pl.DataFrame({"OrderNo": [1, 2], "At": at.dt.replace_time_zone("Europe/Berlin")}))
    assert db.rows("SELECT At FROM orders ORDER BY OrderNo") == [("2026-10-01 08:30:00",), ("2026-07-01 08:30:00.000500",)]


def test_sqlite_writes_each_date_time_the_same_way_in_every_export(db):
    """Whole seconds never get .000000 because another value of the export has a fraction:
    otherwise the keys of a later export with other values would find no row."""
    day = [dt.datetime(2026, 10, 2, hour) for hour in (8, 9, 10)]
    db.export(pl.DataFrame({"OrderNo": [1, 2, 3], "At": day}))
    later = pl.DataFrame({"OrderNo": [4, 5, 6, 7], "At": [*day, dt.datetime(2026, 10, 2, 11, 0, 0, 500000)]})
    db.export(later, mode="upsert", key_columns=["At"])
    assert db.context.messages[-1] == ("info", "orders: updated 3 rows, added 1 row")
    assert db.rows("SELECT OrderNo, At FROM orders ORDER BY At") == [
        (4, "2026-10-02 08:00:00"), (5, "2026-10-02 09:00:00"), (6, "2026-10-02 10:00:00"), (7, "2026-10-02 11:00:00.500000"),
    ]  # fmt: skip
    db.export(later.reverse(), mode="delete", key_columns=["At"])
    assert db.rows("SELECT COUNT(*) FROM orders") == [(0,)]


def test_sqlite_keeps_the_fractions_of_a_second(db, registry):
    """A time 08:30:00.5 was stored as 08:30:00, and a date-time lost its nanoseconds: different
    values became one, also as keys. Each value has one text: whole seconds without a fraction,
    a fraction of whole microseconds with six digits (as date-times always had), one with
    nanoseconds with nine; the same in a column of microseconds and one of nanoseconds."""
    second = 1_735_720_200 * 10**9  # 2025-01-01 08:30:00
    stamps = [second + 1, second, second + 500_000_000, second + 1_500]
    clocks = [30_600 * 10**9 + 500_000_000, 30_600 * 10**9, 30_600 * 10**9 + 1, 30_600 * 10**9 + 1_000]
    data = pl.DataFrame({
        "OrderNo": [1, 2, 3, 4],
        "Stamp": pl.Series(stamps, dtype=pl.Int64).cast(pl.Datetime("ns")),
        "Clock": pl.Series(clocks, dtype=pl.Int64).cast(pl.Time),
    })  # fmt: skip
    stored = [
        (1, "2025-01-01 08:30:00.000000001", "08:30:00.500000"),
        (2, "2025-01-01 08:30:00", "08:30:00"),
        (3, "2025-01-01 08:30:00.500000", "08:30:00.000000001"),
        (4, "2025-01-01 08:30:00.000001500", "08:30:00.000001"),
    ]
    db.export(data)
    assert db.rows("SELECT OrderNo, Stamp, Clock FROM orders ORDER BY OrderNo") == stored
    for key in ("Stamp", "Clock"):  # each key finds its own row, and no other
        db.export(data.with_columns(OrderNo=pl.col("OrderNo") * 10), mode="update", key_columns=[key])
        assert db.context.messages[-1] == ("info", "orders: updated 4 rows"), key
        assert db.rows("SELECT OrderNo, Stamp, Clock FROM orders ORDER BY OrderNo") == [(n * 10, s, c) for n, s, c in stored], key
        db.export(data, mode="replace_keys", key_columns=[key])
        assert db.rows("SELECT OrderNo, Stamp, Clock FROM orders ORDER BY OrderNo") == stored, key
        db.export(data.head(2), mode="delete", key_columns=[key])
        assert db.rows("SELECT OrderNo FROM orders ORDER BY OrderNo") == [(3,), (4,)], key
        db.export(data.head(2))
    later = data.with_columns(pl.col("Stamp").cast(pl.Datetime("us")), OrderNo=pl.col("OrderNo") + 100)
    db.export(later.slice(1, 2), mode="upsert", key_columns=["Stamp"])  # 08:30:00 and .5 as microseconds
    assert db.context.messages[-1] == ("info", "orders: updated 2 rows")


def test_sqlite_date_times_come_back_with_their_nanoseconds(db, registry):
    """The export writes nanoseconds with nine digits; the import with "Detect numbers and dates"
    read them as microseconds and cut them (08:30:00.000000001 was 08:30:00)."""
    second = 1_735_720_200 * 10**9  # 2025-01-01 08:30:00
    stamps = pl.Series("Stamp", [second + 1, second, second + 500_000_000, None], dtype=pl.Int64).cast(pl.Datetime("ns"))
    db.export(pl.DataFrame({"OrderNo": [1, 2, 3, 4], "Stamp": stamps}))
    back = registry.actions["db.import"](None, {"connection": "shop", "table": "orders", "detect_types": True}, db.context)
    assert back.schema["Stamp"] == pl.Datetime("ns") and back["Stamp"].to_list() == stamps.to_list()
    assert back["Stamp"].cast(pl.Int64).to_list() == [second + 1, second, second + 500_000_000, None]


def test_sqlite_says_what_it_cant_store(db):
    """The driver's words were the message: "Error binding parameter 1: type 'datetime.timedelta'
    is not supported", "Python int too large to convert to SQLite INTEGER", and SQLite's own "too
    many columns on orders". Nothing is written, and a new table isn't made."""
    big = pl.Series("Id", [2**64 - 1, 5], dtype=pl.UInt64)
    cases = [
        (pl.DataFrame({"OrderNo": [1], "Span": [dt.timedelta(hours=1)]}),
         r"^SQLite can't store time spans: Span has them\. Leave this column out before this step \(Keep, remove or reorder columns\)\.$"),
        (pl.DataFrame({"Tags": [["a"]], "OrderNo": [1], "Pair": [{"x": 1}]}),
         r"^SQLite can't store lists or records: Tags and Pair have them\. Leave these columns out before this step"),
        (pl.DataFrame({"OrderNo": [1, 2], "Id": big}),
         r"^SQLite stores whole numbers from -9,223,372,036,854,775,808 to 9,223,372,036,854,775,807, and Id has others "
         r"\(e\.g\. 18446744073709551615\)\. Change Id to text before this step \(Change type / format\)"),
        (pl.DataFrame({"OrderNo": [1, 2], "Id": pl.Series([-(2**70), 5], dtype=pl.Int128)}), r"and Id has others \(e\.g\. -1180591620717411303424\)"),
        (pl.DataFrame({f"c{i}": [i] for i in range(2_001)}), r"^The table has 2,001 columns, but a SQLite table holds at most 2,000: leave some out"),
    ]  # fmt: skip
    for data, message in cases:
        with pytest.raises(ActionError, match=message):
            db.export(data)
        assert db.rows("SELECT name FROM sqlite_master") == []
    db.export(pl.DataFrame({"OrderNo": [1], "Id": pl.Series([2**63 - 1], dtype=pl.UInt64), "Span": ["text"]}))  # these fit
    for data, message in (cases[0], cases[2]):  # into an existing table
        with pytest.raises(ActionError, match=message):
            db.export(data, extra_columns="skip")
    db.export(cases[1][0], extra_columns="skip")  # columns that are left out don't count
    assert db.rows() == [(1, 2**63 - 1, "text"), (1, None, None)]
    db.export(pl.DataFrame({f"c{i}": [i] for i in range(2_000)}), table="wide")  # SQLite's most
    assert db.rows("SELECT c1999 FROM wide") == [(1999,)]


def test_delete_by_keys_looks_only_at_the_keys(db, tmp_path):
    """Delete writes only the key values: time spans or whole numbers beyond 64 bits beside the
    keys don't stop it, as they never did. As keys, they do."""
    with sqlite3.connect(tmp_path / "shop.db") as con:
        con.executescript("CREATE TABLE orders (OrderNo INTEGER, Span TEXT, Id INTEGER); INSERT INTO orders VALUES (1, 'a', 1), (2, 'b', 2)")
    con.close()
    data = pl.DataFrame({"OrderNo": [1], "Span": [dt.timedelta(hours=1)], "Id": pl.Series([2**64 - 1], dtype=pl.UInt64)})
    db.export(data, mode="delete", key_columns=["OrderNo"])
    assert db.rows("SELECT OrderNo FROM orders") == [(2,)] and db.context.messages[-1] == ("info", "orders: deleted 1 row")
    with pytest.raises(ActionError, match=r"^SQLite can't store time spans: Span has them"):
        db.export(data, mode="delete", key_columns=["Span"])
    with pytest.raises(ActionError, match=r"^SQLite stores whole numbers .* and Id has others \(e\.g\. 18446744073709551615\)"):
        db.export(data, mode="delete", key_columns=["Id"])


def test_sqlite_finds_rows_by_hundreds_of_key_columns(db):
    """SQLite stopped with "Expression tree is too large (maximum depth 1000)": in a delete with
    500 key columns, in every mode with 999. Empty key values still match empty ones."""
    keys = [f"k{i}" for i in range(999)]
    first = pl.DataFrame({key: [1, 2, None] for key in keys} | {"Net": [1, 2, 3]})
    db.export(first)
    modes = {"update": "updated 3 rows", "upsert": "updated 3 rows", "replace_keys": "deleted 3 rows, added 3 rows", "delete": "deleted 3 rows"}
    for mode, message in modes.items():
        db.export(first.with_columns(Net=pl.col("Net") * 10), mode=mode, key_columns=keys)
        assert db.context.messages[-1] == ("info", f"orders: {message}"), mode
        assert db.rows("SELECT Net FROM orders ORDER BY Net") == ([] if mode == "delete" else [(10,), (20,), (30,)]), mode
    pairs = pl.DataFrame({"a": [1, None, 1, None], "b": [None, None, 2, 2], "Net": [1, 2, 3, 4]})
    db.export(pairs, table="pairs")
    db.export(pairs.with_columns(Net=pl.col("Net") * 10), table="pairs", mode="upsert", key_columns=["a", "b"])
    assert db.context.messages[-1] == ("info", "pairs: updated 4 rows")
    assert db.rows("SELECT a, b, Net FROM pairs ORDER BY Net") == [(1, None, 10), (None, None, 20), (1, 2, 30), (None, 2, 40)]


_WIDE_SQLITE = """
import datetime as dt
import sqlite3
import sys

import polars as pl
from tafl.api import ActionError
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
ctx = StubContext(mode="run", connections={"db": (registry.connectors["sqlite"], {"file": sys.argv[1]})})
kinds = [[dt.datetime(2025, 1, 1, 8, 30, 0, 500000)], [dt.time(8, 30, 0, 500000)], pl.Series([5], dtype=pl.UInt64), ["a"]]
df = pl.DataFrame({f"c{i}": kinds[i % 4] for i in range(2_000)})  # the most a SQLite table holds
export = registry.actions["db.export"]
export(df, {"connection": "db", "table": "wide"}, ctx)
export(df, {"connection": "db", "table": "wide", "mode": "upsert", "key_columns": df.columns[:-1]}, ctx)
assert ctx.messages[-1] == ("info", "wide: updated 1 row"), ctx.messages
row = sqlite3.connect(sys.argv[1]).execute("SELECT c0, c1, c2, c3 FROM wide").fetchall()
assert row == [("2025-01-01 08:30:00.500000", "08:30:00.500000", 5, "a")], row
for data, message in (
    (pl.DataFrame({f"c{i}": [i] for i in range(4_001)}), "The table has 4,001 columns, but a SQLite table holds at most 2,000"),
    (pl.DataFrame({f"s{i}": [dt.timedelta(1)] for i in range(1_999)}), "s0, s1, s2, s3, s4 and 1,994 more columns have them"),
):
    try:
        export(data, {"connection": "db", "table": "other"}, ctx)
    except ActionError as error:
        assert message in str(error), error
    else:
        raise AssertionError("no message")
"""


def test_sqlite_takes_a_table_of_its_most_columns(tmp_path):
    """2,000 columns, the most a SQLite table holds, also with 1,999 key columns, and more columns
    or time spans in all columns give their message. The export goes through every column, and a
    deep Polars expression would end the process: in a process of its own, so that such an end shows
    as a failed test."""
    done = subprocess.run([sys.executable, "-c", _WIDE_SQLITE, str(tmp_path / "wide.db")], capture_output=True, text=True, timeout=300)
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


def test_sqlite_exact_decimals_keep_their_digits(db, registry):
    """Exact decimals (e.g. from Parquet or SQL Server) go to SQLite as texts with all their
    digits. A new table gets INTEGER or REAL for them when that keeps every value of the type,
    else TEXT, and reads them back so; SQLite turns the texts into numbers for its columns of
    numbers, also when keys compare."""
    D = decimal.Decimal
    data = pl.DataFrame({
        "OrderNo": [1, 2],
        "Amount": pl.Series([D("1.005"), D("-12.300")], dtype=pl.Decimal(10, 3)),
        "Id": pl.Series([D("123456789012345678"), D("-5")], dtype=pl.Decimal(18, 0)),
        "Big": pl.Series([D("1" * 38), D("1")], dtype=pl.Decimal(38, 0)),
        "Long": pl.Series([D("12345678901234.5678"), D("0.1000")], dtype=pl.Decimal(18, 4)),
    })  # fmt: skip
    db.export(data)
    assert db.rows("SELECT name, type FROM pragma_table_info('orders')") == [
        ("OrderNo", "INTEGER"), ("Amount", "REAL"), ("Id", "INTEGER"), ("Big", "TEXT"), ("Long", "TEXT"),
    ]  # fmt: skip
    stored = [(1, 1.005, 123456789012345678, "1" * 38, "12345678901234.5678"), (2, -12.3, -5, "1", "0.1000")]
    assert db.rows() == stored
    imported = registry.actions["db.import"](None, {"connection": "shop", "table": "orders"}, db.context)
    assert imported.schema == {"OrderNo": pl.Int64, "Amount": pl.Float64, "Id": pl.Int64, "Big": pl.String, "Long": pl.String}
    assert imported.rows() == stored
    for key in ("Amount", "Id", "Big", "Long"):
        db.export(data.with_columns(OrderNo=pl.col("OrderNo") + 10), mode="upsert", key_columns=[key])
        assert db.context.messages[-1] == ("info", "orders: updated 2 rows"), key
    db.export(data, mode="delete", key_columns=["Amount", "Long"])
    assert db.rows() == []


def test_sqlite_stores_exact_decimals_as_it_stores_their_texts(db, tmp_path):
    """Into an existing table: as SQLite stores the same texts (a number for a column of
    numbers, the text for a TEXT column or one without a type), in every mode, and keys of
    each of these columns find the rows."""
    D = decimal.Decimal
    kinds = {"R": "REAL", "N": "NUMERIC", "I": "INTEGER", "D": "DECIMAL(10,2)", "T": "TEXT", "B": ""}
    texts = ["1.5000", "2.0000", "12345678901234.5678", "-0.0500"]
    data = pl.DataFrame({"OrderNo": [1, 2, 3, 4], **{c: pl.Series([D(t) for t in texts], dtype=pl.Decimal(18, 4)) for c in kinds}})
    columns = ", ".join(f"{c} {kind}" for c, kind in kinds.items())
    with sqlite3.connect(tmp_path / "shop.db") as con:
        con.executescript(f"CREATE TABLE orders (OrderNo INTEGER, {columns}); CREATE TABLE texts (OrderNo INTEGER, {columns})")
        con.executemany("INSERT INTO texts VALUES (?, ?, ?, ?, ?, ?, ?)", [(n, *[t] * len(kinds)) for n, t in enumerate(texts, 1)])
    con.close()
    stored = "SELECT OrderNo, " + ", ".join(f"{c}, typeof({c})" for c in kinds) + " FROM {} ORDER BY OrderNo"
    expected = db.rows(stored.format("texts"))
    assert expected[1] == (2, 2.0, "real", 2, "integer", 2, "integer", 2, "integer", "2.0000", "text", "2.0000", "text")
    db.export(data)
    assert db.rows(stored.format("orders")) == expected
    modes = {"update": "updated 4 rows", "replace_keys": "deleted 4 rows, added 4 rows", "delete": "deleted 4 rows", "upsert": "added 4 rows"}
    for key in kinds:
        for mode, message in modes.items():
            db.export(data, mode=mode, key_columns=[key])
            assert db.context.messages[-1] == ("info", f"orders: {message}"), (key, mode)
            assert db.rows(stored.format("orders")) == ([] if mode == "delete" else expected), (key, mode)


def test_rows_are_written_with_every_digit_of_their_decimals():
    """pyodbc's fast_executemany converts decimals in the thread's decimal context, whose 28 digits
    would round a numeric(38,0) ID (…789012345678 would end in …790000000000)."""
    from tafl.api.database import Connector, Database

    seen = []

    class Cursor:
        def executemany(self, sql, rows):
            seen.extend(+value for row in rows for value in row)  # + rounds to the context's digits

        def close(self):
            pass

    raw = type("Raw", (), {"cursor": lambda self: Cursor()})()
    values = [decimal.Decimal("12345678901234567890123456789012345678"), decimal.Decimal("1234567890123456789012345678.0123456789")]
    assert Database(Connector(), raw).execute_many("INSERT INTO t VALUES (?)", [(v,) for v in values]) == 2
    assert seen == values
    assert decimal.getcontext().prec == 28  # only while the rows are written


def test_sqlite_commands_run_in_one_transaction(db):
    """BEGIN would fail, and a COMMIT would keep what came before an error: both are refused."""
    db.export(DATA)
    for sql in ("BEGIN; DELETE FROM orders; COMMIT;", "DELETE FROM orders WHERE OrderNo = 1; COMMIT; DELETE FROM nope"):
        with pytest.raises(ActionError, match="runs the command in one transaction.*leave out BEGIN, COMMIT and ROLLBACK"):
            db.command(sql)
        assert len(db.rows()) == 3
    db.command("SAVEPOINT s; DELETE FROM orders WHERE OrderNo = 1; RELEASE s")  # savepoints are fine
    assert len(db.rows()) == 2 and db.context.messages[-1] == ("info", "Done: 1 row changed")


def test_a_failed_command_changes_nothing(db):
    db.export(DATA)
    with pytest.raises(ActionError, match="no such table"):
        db.command("DELETE FROM orders; DELETE FROM nope")
    assert len(db.rows()) == 3


def test_sql_commands(db):
    db.export(DATA)
    assert db.command("DELETE FROM orders WHERE OrderNo = 1; UPDATE orders SET Net = 0") is DATA
    assert db.rows("SELECT OrderNo, Net FROM orders ORDER BY OrderNo") == [(2, 0.0), (3, 0.0)]
    assert db.context.messages[-1] == ("info", "Done: 3 rows changed")
    with pytest.raises(ActionError, match="no such table"):
        db.command("DELETE FROM nope")


def test_updating_many_rows_stays_fast(db):
    """Matching rows by key once took minutes in SQLite (every row compared with every row)."""
    many = pl.DataFrame({"OrderNo": pl.int_range(0, 50_000, eager=True)}).with_columns(Net=pl.col("OrderNo") / 10)
    db.export(many)
    started = time.perf_counter()
    db.export(many.with_columns(pl.col("OrderNo") + 25_000), mode="upsert", key_columns=["OrderNo"])
    db.export(many.head(10_000), mode="delete", key_columns=["OrderNo"])
    assert time.perf_counter() - started < 10
    assert db.rows("SELECT COUNT(*) FROM orders") == [(65_000,)]


def test_exports_run_only_when_the_project_is_run(registry, tmp_path):
    (tmp_path / "in.csv").write_text("OrderNo;Net\n1;2,5\n", encoding="utf-8")
    project = Project(name="Export")
    project.path = tmp_path / "export.tafl"
    project.add_connection("shop", "sqlite", file="shop.db")
    table = project.add_table("Orders")
    project.add_step(table, registry.actions["csv.import"], {"file": "in.csv"})
    project.add_step(table, registry.actions["db.export"], {"connection": "shop", "table": "orders"})
    engine = Engine(project, registry)
    assert engine.evaluate(table.id).ok and not (tmp_path / "shop.db").exists()  # preview: nothing written
    results = engine.run()
    assert results[table.id].ok, results[table.id].first_error
    with sqlite3.connect(tmp_path / "shop.db") as con:
        assert con.execute("SELECT * FROM orders").fetchall() == [(1, 2.5)]


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_export(registry):
    """Writes into a new table in tempdb and removes it again."""
    connector = registry.connectors["sqlserver"]
    settings = {"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"}
    ctx = StubContext(mode="run", connections={"server": (connector, settings)})
    name = f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"
    new = f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"  # for a first export that fails
    long_text = "x" * 5000
    data = DATA.with_columns(
        pl.Series("Stamp", [dt.datetime(2025, 1, 1, 8, 30, 15, 123456)] * 3),
        pl.Series("Memo", [long_text, "kurz", None]),
        pl.Series("Big", [1, 2, 3_000_000_000]),
    )

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def read(sql):
        with ctx.database("server") as db:
            return db.read(sql)

    try:
        export(data)
        types = read(
            f"SELECT COLUMN_NAME, DATA_TYPE, CHARACTER_MAXIMUM_LENGTH FROM INFORMATION_SCHEMA.COLUMNS "
            f"WHERE TABLE_NAME = '{name.split('.')[1]}' ORDER BY ORDINAL_POSITION"
        ).rows()
        assert types == [
            ("OrderNo", "int", None), ("Customer", "nvarchar", 50), ("Net", "float", None), ("Day", "date", None),
            ("Paid", "bit", None), ("Stamp", "datetime2", None), ("Memo", "nvarchar", -1), ("Big", "bigint", None),
        ]
        rows = read(f"SELECT * FROM {name} ORDER BY OrderNo")
        assert rows["Customer"].to_list() == ["Müller", "Bäcker", None] and rows["Net"].to_list() == [10.5, None, 3.0]
        assert rows["Memo"][0] == long_text and rows["Stamp"][0] == dt.datetime(2025, 1, 1, 8, 30, 15, 123456)
        assert rows["Day"][0] == dt.date(2025, 1, 1) and rows["Paid"].to_list() == [True, False, None]

        export(pl.DataFrame({"OrderNo": [9], "Day": [dt.date(2025, 1, 2)]}), mode="replace_keys", key_columns=["Day"])
        export(pl.DataFrame({"OrderNo": [1, 10], "Net": [99.0, 1.0], "Customer": ["Neu", "Neu"]}), mode="upsert", key_columns=["OrderNo"])
        export(pl.DataFrame({"OrderNo": [2]}), mode="delete", key_columns=["OrderNo"])
        rows = read(f"SELECT OrderNo, Net, Customer FROM {name} ORDER BY OrderNo").rows()
        assert rows == [(1, 99.0, "Neu"), (9, None, None), (10, 1.0, "Neu")]

        registry.actions["db.command"](data, {"connection": "server", "sql": f"UPDATE {name} SET Net = 0\nGO\nDELETE FROM {name} WHERE OrderNo = 10"}, ctx)
        assert read(f"SELECT SUM(Net) AS s, COUNT(*) AS n FROM {name}").rows() == [(0.0, 2)]
        assert ctx.messages[-1] == ("info", "Done: 4 rows changed")

        # A failed export changes nothing: all rows are deleted, then a value doesn't fit its column
        with pytest.raises(ActionError, match="doesn't fit its column"):
            export(pl.DataFrame({"OrderNo": ["not a number"]}), mode="replace")
        with pytest.raises(ActionError):
            export(data, mode="replace_where", condition="no_such_column = 1")
        assert read(f"SELECT SUM(Net) AS s, COUNT(*) AS n FROM {name}").rows() == [(0.0, 2)]
        # A failed first export leaves no table behind (SQL Server's CREATE TABLE is rolled back too)
        with pytest.raises(ActionError):
            export(data, table=new, mode="replace_where", condition="no_such_column = 1")
        assert read(f"SELECT OBJECT_ID('{new}') AS id").rows() == [(None,)]

        # Decimals into a whole-number column would be cut off by the database: refused
        with pytest.raises(ActionError, match="holds whole numbers .* decimals .* 1.5"):
            export(pl.DataFrame({"OrderNo": [1.5, 2.0]}))
        export(pl.DataFrame({"OrderNo": [11.0, None]}))  # whole numbers stored as decimals are fine

        # An error in a later statement of a batch fails the command, and nothing is kept
        command = registry.actions["db.command"]
        for sql in (f"DELETE FROM {name};\nSELECT 1/0;", f"DELETE FROM {name}; RAISERROR('check failed', 16, 1);"):
            with pytest.raises(ActionError, match="Divide by zero|check failed"):
                command(data, {"connection": "server", "sql": sql}, ctx)
            assert read(f"SELECT COUNT(*) AS n FROM {name}").rows() == [(4,)]
        command(data, {"connection": "server", "sql": f"UPDATE {name} SET Net = 1; UPDATE {name} SET Net = 2"}, ctx)
        assert ctx.messages[-1] == ("info", "Done: 8 rows changed")  # both statements counted
    finally:
        with ctx.database("server") as db:
            for table in (name, new):
                db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")


def _sqlserver():
    """A context with the connection "server" to tempdb, and a new table name there."""
    from tafl.core import Registry

    connector = Registry.default().connectors["sqlserver"]
    settings = {"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"}
    return StubContext(mode="run", connections={"server": (connector, settings)}), f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_keyed_modes_store_values_as_add_does(registry):
    """The keyed modes send the rows into a #temporary table first, whose columns the ODBC driver
    can't look up. Long texts, parts of a second and characters outside the Windows code page
    must arrive as they are: otherwise keys find the wrong rows (the decoy: what they became)."""
    ctx, name = _sqlserver()
    long_text = "Łódź ẞ " + "x" * 5000
    data = pl.DataFrame({
        "Stamp": [dt.datetime(2026, 10, 2, 8, 0, 0, 500000)],
        "City": ["Łódź"],
        "Memo": [long_text],
        "At": [dt.datetime(2026, 10, 2, 23, 59, 59, 123456)],
        "Clock": [dt.time(8, 30, 15, 123456)],
        "Bytes": [b"\x00\xff"],
        "Price": pl.Series([decimal.Decimal("12.34")], dtype=pl.Decimal(10, 2)),
        "N": [2],
    })  # fmt: skip
    new = (dt.datetime(2026, 10, 2, 8, 0, 0, 500000), "Łódź", long_text, dt.datetime(2026, 10, 2, 23, 59, 59, 123456),
           dt.time(8, 30, 15, 123456), b"\x00\xff", 12.34, 2)  # fmt: skip
    decoy = (dt.datetime(2026, 10, 2, 8, 0), "Lódz", "decoy", None, None, None, None, 1)
    expected = {
        "update": ([decoy, new], "updated 1 row"),
        "upsert": ([decoy, new], "updated 1 row"),
        "replace_keys": ([decoy, new], "deleted 1 row, added 1 row"),
        "delete": ([decoy], "deleted 1 row"),
    }
    columns = "Stamp datetime2(3), City nvarchar(50), Memo nvarchar(max), At datetime2(7), Clock time(7), "
    columns += "Bytes varbinary(max), Price decimal(10, 2), N int"
    try:
        for key in ("Stamp", "City"):
            for mode, (rows, message) in expected.items():
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                    db.execute(f"CREATE TABLE {name} ({columns})")
                    db.execute(f"INSERT INTO {name} (Stamp, City, Memo, N) VALUES "
                               "('2026-10-02 08:00:00.500', N'Łódź', N'old', 1), ('2026-10-02 08:00:00', N'Lódz', N'decoy', 1)")  # fmt: skip
                registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": [key]}, ctx)
                with ctx.database("server") as db:
                    assert db.read(f"SELECT * FROM {name} ORDER BY N").rows() == rows, (key, mode)
                assert ctx.messages[-1] == ("info", f"{name}: {message}"), (key, mode)
        # A datetime holds steps of 1/300 s: .123 is .1233333 there, and a key read from it must find it
        with ctx.database("server") as db:
            db.execute(f"DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (Stamp datetime, Minute smalldatetime, N int)")
            db.execute(f"INSERT INTO {name} VALUES ('2026-10-02T08:00:00.123', '2026-10-02T08:00:29', 1), "
                       "('2026-10-02T08:00:00.500', '2026-10-02T08:01:00', 1)")  # fmt: skip
            stored = db.read(f"SELECT Stamp, Minute FROM {name}")
        assert stored["Stamp"].to_list() == [dt.datetime(2026, 10, 2, 8, 0, 0, 123000), dt.datetime(2026, 10, 2, 8, 0, 0, 500000)]
        for key, value in (("Stamp", 2), ("Minute", 3)):
            changes = stored.with_columns(N=pl.lit(value), Minute=pl.col("Minute").dt.offset_by("29s"))
            export = {"connection": "server", "table": name, "mode": "update", "key_columns": [key]}
            registry.actions["db.export"](changes.drop("Minute" if key == "Stamp" else "Stamp"), export, ctx)
            assert ctx.messages[-1] == ("info", f"{name}: updated 2 rows"), key
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_cuts_digits_below_a_microsecond_with_a_warning(registry):
    """The driver passes fractions of a second to the microsecond: 08:30:00.000000001 became
    08:30:00 without a word, also as a key (an update gave two rows the values of one). A column
    of fewer digits cuts more: the warning says what that column holds."""
    ctx, name = _sqlserver()
    second = 1_735_720_200 * 10**9  # 2025-01-01 08:30:00
    data = pl.DataFrame({
        "N": [1, 2, 3],
        "Stamp": pl.Series([second + 1, second + 100, second + 1_000], dtype=pl.Int64).cast(pl.Datetime("ns")),
        "Clock": pl.Series([30_600 * 10**9 + 1, 30_600 * 10**9, 30_600 * 10**9 + 2_000], dtype=pl.Int64).cast(pl.Time),
    })  # fmt: skip

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    try:
        export(data)
        assert ctx.warnings == [
            "Stamp: 2 values were cut to whole microseconds (e.g. 2025-01-01 08:30:00.000000001 is 2025-01-01 08:30:00): "
            "SQL Server's driver passes no smaller parts of a second",
            "Clock: 1 value was cut to whole microseconds (e.g. 08:30:00.000000001 is 08:30:00): SQL Server's driver passes "
            "no smaller parts of a second",
        ]
        with ctx.database("server") as db:
            rows = db.read(f"SELECT N, CONVERT(nvarchar(30), Stamp, 121) AS S, CONVERT(nvarchar(30), Clock) AS C FROM {name} ORDER BY N")
        assert rows.rows() == [
            (1, "2025-01-01 08:30:00.0000000", "08:30:00.0000000"),
            (2, "2025-01-01 08:30:00.0000000", "08:30:00.0000000"),
            (3, "2025-01-01 08:30:00.0000010", "08:30:00.0000020"),
        ]
        message = r"1 key value occurs more than once \(e\.g\. Stamp = 2025-01-01 08:30:00\)"
        with pytest.raises(ActionError, match=message):  # as the column holds them, rows 1 and 2 have one key
            export(data.with_columns(N=pl.col("N") * 10), mode="update", key_columns=["Stamp"])
        export(data.with_columns(N=pl.col("N") * 10).tail(1), mode="update", key_columns=["Stamp"])
        assert ctx.messages[-1] == ("info", f"{name}: updated 1 row") and len(ctx.warnings) == 2
        with ctx.database("server") as db:
            db.execute(f"DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (N int, Stamp datetime2(3), Clock time(6))")
        export(data)
        assert ctx.warnings[2:] == [
            f"Stamp: 3 values were cut to milliseconds (e.g. 2025-01-01 08:30:00.000000001 is 2025-01-01 08:30:00): the column "
            f"Stamp of {name} holds 3 digits of a second (datetime2(3))",
            f"Clock: 1 value was cut to whole microseconds (e.g. 08:30:00.000000001 is 08:30:00): the column Clock of {name} "
            "holds 6 digits of a second (time(6))",
        ]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_says_what_it_cant_store(registry):
    """Polars' and the driver's words were the message: "casting from Duration('μs') to String not
    supported", "Unknown object type dict during describe", "int too big to convert". A column of
    decimals takes whole numbers beyond a bigint, as before."""
    ctx, name = _sqlserver()

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def exists():
        with ctx.database("server") as db:
            return db.read(f"SELECT OBJECT_ID('{name}') AS id")["id"][0] is not None

    big = pl.Series("Id", [2**64 - 1, 5], dtype=pl.UInt64)
    try:
        for data, message in (
            (pl.DataFrame({"N": [1], "Span": [dt.timedelta(hours=1)]}), r"^SQL Server can't store time spans: Span has them\. Leave this"),
            (pl.DataFrame({"Tags": [["a"]], "Pair": [{"x": 1}]}), r"^SQL Server can't store lists or records: Tags and Pair have them"),
        ):  # fmt: skip
            with pytest.raises(ActionError, match=message):
                export(data)
            assert not exists()
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int, Id decimal(38, 0), Span nvarchar(50))")
        with pytest.raises(ActionError, match=r"^SQL Server can't store time spans: Span has them"):
            export(pl.DataFrame({"N": [1], "Span": [dt.timedelta(hours=1)]}))
        export(pl.DataFrame({"N": [1, 2], "Id": big}))
        with ctx.database("server") as db:
            assert db.read(f"SELECT CONVERT(nvarchar(40), Id) AS Id FROM {name} ORDER BY N")["Id"].to_list() == [str(2**64 - 1), "5"]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_new_table_holds_whole_numbers_beyond_a_bigint(registry):
    """A new table got a bigint column for them, and the export stopped with a message about that
    column of a table that wasn't made. Now the column is a decimal that holds every value exactly:
    decimal(20, 0) for UInt64, decimal(38, 0) for 128 bits; keys find the rows."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({
        "N": [1, 2],
        "Id": pl.Series([2**64 - 1, 5], dtype=pl.UInt64),
        "Big": pl.Series([-(10**37), 2**100], dtype=pl.Int128),
        "Small": pl.Series([7, 8], dtype=pl.UInt64),  # within a bigint: as before
    })  # fmt: skip
    try:
        registry.actions["db.export"](data, {"connection": "server", "table": name}, ctx)
        with ctx.database("server") as db:
            types = db.read(
                "SELECT COLUMN_NAME, DATA_TYPE, NUMERIC_PRECISION FROM INFORMATION_SCHEMA.COLUMNS "
                f"WHERE TABLE_NAME = '{name.split('.')[1]}' ORDER BY ORDINAL_POSITION"
            ).rows()
            stored = db.read(f"SELECT CONVERT(nvarchar(45), Id) AS Id, CONVERT(nvarchar(45), Big) AS Big FROM {name} ORDER BY N").rows()
        assert types == [("N", "int", 10), ("Id", "decimal", 20), ("Big", "decimal", 38), ("Small", "int", 10)]
        assert stored == [(str(2**64 - 1), str(-(10**37))), ("5", str(2**100))]
        changed = data.with_columns(N=pl.col("N") + 10)
        registry.actions["db.export"](changed, {"connection": "server", "table": name, "mode": "update", "key_columns": ["Id"]}, ctx)
        assert ctx.messages[-1] == ("info", f"{name}: updated 2 rows")
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_text_and_ntext_columns_take_texts(registry):
    """Every export of texts into a column of the old types text or ntext said "There is not enough
    memory for this": the driver gave them a size of 2 GB, and that much memory was taken for each
    value. They go as Unicode of any length now, as for nvarchar(max), which the server converts
    (a text column keeps the characters of its code page, as a varchar column does)."""
    ctx, name = _sqlserver()
    long_text = "y" * 5000
    data = pl.DataFrame({"N": [1, 2, 3, 4], "T": ["abc", "Łódź", long_text, None], "U": ["abc", "Łódź", long_text, None]})
    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int, T text, U ntext)")
            as_varchar = db.read("SELECT CONVERT(varchar(20), N'Łódź') AS t")["t"][0]  # as the server converts it
        registry.actions["db.export"](data, {"connection": "server", "table": name}, ctx)
        with ctx.database("server") as db:
            rows = db.read(f"SELECT N, CAST(T AS varchar(max)) AS T, CAST(U AS nvarchar(max)) AS U FROM {name} ORDER BY N").rows()
        assert rows == [(1, "abc", "abc"), (2, as_varchar, "Łódź"), (3, long_text, long_text), (4, None, None)]
        changed = data.with_columns(T=pl.lit("neu"), U=pl.lit("neu"))
        registry.actions["db.export"](changed, {"connection": "server", "table": name, "mode": "update", "key_columns": ["N"]}, ctx)
        assert ctx.messages[-1] == ("info", f"{name}: updated 4 rows")
        with ctx.database("server") as db:
            assert db.read(f"SELECT DISTINCT CAST(T AS varchar(10)) AS T, CAST(U AS nvarchar(10)) AS U FROM {name}").rows() == [("neu", "neu")]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_keyed_modes_write_text_and_ntext_columns_as_add_does(registry):
    """An update or upsert of numbers, dates or true/false into a text or ntext column stopped with
    "Operand type clash: int is incompatible with text". The keyed modes write the text that Add
    the rows writes into such a column."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({
        "N": [1, 2], "I": [1, -5], "F": [1.5, None], "D": [dt.date(2025, 12, 31), None], "B": [True, False], "S": ["Łódź", "x"],
    })  # fmt: skip
    read = f"SELECT N, {', '.join(f'CAST({c} AS nvarchar(max)) AS {c}' for c in 'IFDBS')} FROM {name} ORDER BY N"

    def export(**params):
        registry.actions["db.export"](data, {"connection": "server", "table": name} | params, ctx)

    try:
        for kind in ("text", "ntext"):
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (N int, {', '.join(f'{c} {kind}' for c in 'IFDBS')})")
            export()
            with ctx.database("server") as db:
                added = db.read(read).rows()
            assert added[0][:5] == (1, "1", "1.5", "2025-12-31", "1"), kind
            for mode in ("update", "upsert"):
                with ctx.database("server") as db:
                    db.execute(f"DELETE FROM {name}")
                    if mode == "update":
                        db.execute(f"INSERT INTO {name} (N) VALUES (1), (2)")
                export(mode=mode, key_columns=["N"])
                with ctx.database("server") as db:
                    assert db.read(read).rows() == added, (kind, mode)
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_text_and_ntext_columns_take_every_type_in_every_mode(registry):
    """Add the rows and the replace modes stopped with "There is not enough memory for this" when
    categories went into a text or ntext column, or numbers, dates and true/false whose first value
    was empty (the driver gave such a column 2 GB for each value). They are written as Update and
    Upsert write them."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({
        "N": [1, 2], "C": pl.Series(["b", "a"], dtype=pl.Categorical), "E": pl.Series(["y", "x"], dtype=pl.Enum(["x", "y"])),
        "I": [None, 5], "F": [None, 1.5], "D": [None, dt.date(2025, 12, 31)], "B": [None, True], "T": [None, dt.datetime(2025, 1, 2, 8, 30)],
    })  # fmt: skip
    columns = data.columns[1:]
    read = f"SELECT N, {', '.join(f'CAST({c} AS nvarchar(max)) AS {c}' for c in columns)} FROM {name} ORDER BY N"
    expected = [(1, "b", "y", None, None, None, None, None), (2, "a", "x", "5", "1.5", "2025-12-31", "1", "2025-01-02 08:30:00.000")]
    try:
        for kind in ("text", "ntext"):
            for mode in ("insert", "replace", "replace_where", "replace_keys", "update", "upsert"):
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                    db.execute(f"CREATE TABLE {name} (N int, {', '.join(f'{c} {kind}' for c in columns)})")
                    if mode == "update":
                        db.execute(f"INSERT INTO {name} (N) VALUES (1), (2)")
                params = {"connection": "server", "table": name, "mode": mode, "key_columns": ["N"], "condition": "1 = 1"}
                registry.actions["db.export"](data, params, ctx)
                with ctx.database("server") as db:
                    assert db.read(read).rows() == expected, (kind, mode)
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_categories_keep_their_characters_in_char_and_varchar_columns(registry):
    """Categories (e.g. a Parquet file's dictionary column) went into char and varchar columns in the
    code page of this PC in Add the rows and the replace modes, without a word: Łódź was Lódz in a
    Polish column and 日本 was ?? in a UTF-8 one (Update wrote them right), also with categories of a
    name of their own (Polars' pl.Categories). They go as texts go, and a category too long in bytes for a
    UTF-8 column stops the export."""
    ctx, name = _sqlserver()
    texts = ["Łódź", "日本"]
    named = [pl.Categorical(pl.Categories("tafl_test")), pl.Categorical(pl.Categories("tafl_test_8", physical=pl.UInt8))]
    read = f"SELECT CAST(T AS nvarchar(20)) AS T FROM {name} ORDER BY N"

    def export(df, mode):
        registry.actions["db.export"](df, {"connection": "server", "table": name, "mode": mode, "key_columns": ["N"]}, ctx)

    try:
        for kind, expected in (
            ("varchar(20) COLLATE Polish_CI_AS", ["Łódź", "??"]),
            ("char(4) COLLATE Polish_CI_AS", ["Łódź", "??  "]),
            ("varchar(20) COLLATE Latin1_General_100_CI_AS_SC_UTF8", texts),
        ):
            for dtype in (pl.Categorical, pl.Enum(texts), *named):
                for mode in ("insert", "replace_keys", "update"):
                    with ctx.database("server") as db:
                        db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                        db.execute(f"CREATE TABLE {name} (N int, T {kind})")
                        if mode == "update":
                            db.execute(f"INSERT INTO {name} (N) VALUES (1), (2)")
                    export(pl.DataFrame({"N": [1, 2], "T": pl.Series(texts, dtype=dtype)}), mode)
                    with ctx.database("server") as db:
                        assert db.read(read)["T"].to_list() == expected, (kind, dtype, mode)
        with ctx.database("server") as db:
            db.execute(f"DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (N int, T varchar(5) COLLATE Latin1_General_100_CI_AS_SC_UTF8)")
        for dtype in (pl.Categorical, *named):
            with pytest.raises(ActionError, match="truncated|too long"):  # 4 characters, 8 bytes in UTF-8: not cut
                export(pl.DataFrame({"N": [1], "T": pl.Series(["ääää"], dtype=dtype)}), "insert")
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_text_columns_get_binary_values_as_hex_text(registry):
    """Binary values for a column of text were written as their bytes read as characters, without a
    word: b"ab" was 扡 in an nvarchar or ntext column, ab in a varchar one, and ? there in Update
    and Upsert (into a text column, Add said "There is not enough memory"). Every mode writes them
    as hex text, as the CSV export does, and binary keys find the rows that hold that text."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({"N": [1, 2], "B": [b"\xff\x00", b"ab"]})
    read = f"SELECT N, CAST(B AS nvarchar(max)) AS B FROM {name} ORDER BY N"

    def export(df, mode, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name, "mode": mode} | params, ctx)

    try:
        for kind in ("varchar(20)", "nvarchar(20)", "char(6)", "nchar(6)", "varchar(max)", "nvarchar(max)", "text", "ntext"):
            for mode in ("insert", "replace", "replace_where", "replace_keys", "update", "upsert"):
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                    db.execute(f"CREATE TABLE {name} (N int, B {kind})")
                    if mode == "update":
                        db.execute(f"INSERT INTO {name} (N) VALUES (1), (2)")
                export(data, mode, key_columns=["N"], condition="1 = 1")
                with ctx.database("server") as db:
                    assert db.read(read).rows() == [(1, "0xFF00"), (2, "0x6162")], (kind, mode)
            if kind in ("text", "ntext"):
                continue  # SQL Server compares no values of these types: no keys
            export(data.with_columns(N=pl.lit(3)).head(1), "update", key_columns=["B"])
            assert ctx.messages[-1] == ("info", f"{name}: updated 1 row"), kind
            export(data.select("B").tail(1), "delete", key_columns=["B"])
            assert ctx.messages[-1] == ("info", f"{name}: deleted 1 row"), kind
            with ctx.database("server") as db:
                assert db.read(read).rows() == [(3, "0xFF00")], kind
        with ctx.database("server") as db:
            db.execute(f"DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (N int, B varchar(5))")
        message = (
            rf"^The column B of {name} holds texts of up to 5 characters \(varchar\(5\)\), but B has binary values whose hex text "
            r"is longer than this column holds \(e\.g\. 0xFF00, 6 characters\)\. Make the column longer in the database\.$"
        )
        with pytest.raises(ActionError, match=message):
            export(data, "insert")
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


_WIDE_SQLSERVER = """
import sys

import polars as pl
from tafl.api.testing import StubContext
from tafl.core import Registry

registry = Registry()
registry.load_builtin()
name = sys.argv[2]
ctx = StubContext(mode="run", connections={"server": (registry.connectors["sqlserver"], {"server": sys.argv[1], "database": "tempdb"})})
kinds = [[b"ab"], pl.Series([None], dtype=pl.Int64), pl.Series(["a"], dtype=pl.Categorical), pl.Series([None], dtype=pl.Date)]
df = pl.DataFrame({"N": [1], **{f"c{i}": kinds[i % 4] for i in range(4_100)}})
types = ["text", "ntext", "varchar(20)", "nvarchar(max)", "varchar(max)"]
with ctx.database("server") as db:
    db.execute(f"CREATE TABLE {name} (N int, {', '.join(f'c{i} {types[i % 5]}' for i in range(1_023))})")
try:
    for mode in ("insert", "replace_keys"):
        params = {"connection": "server", "table": name, "mode": mode, "key_columns": ["N"], "extra_columns": "skip"}
        registry.actions["db.export"](df, params, ctx)
    assert ctx.messages[-1] == ("info", f"{name}: deleted 1 row, added 1 row"), ctx.messages
    with ctx.database("server") as db:
        picked = "CAST(c0 AS varchar(10)) AS a, c1, c2, c3, CAST(c5 AS varchar(10)) AS b, CAST(c6 AS nvarchar(10)) AS c"
        row = db.read(f"SELECT {picked} FROM {name}").rows()
    assert row == [("0x6162", None, "a", None, None, "a")], row
finally:
    with ctx.database("server") as db:
        db.execute(f"DROP TABLE {name}")
"""


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_takes_a_table_of_its_most_columns():
    """4,100 columns, of which the 1,024 of the table (the most a SQL Server table holds) are
    written: binary values, categories and empty values of other types into text, ntext and other
    text columns. The export goes through every column: in a process of its own, so that a deep
    Polars expression that ends the process shows as a failed test."""
    name = _sqlserver()[1]
    done = subprocess.run(
        [sys.executable, "-c", _WIDE_SQLSERVER, os.environ["TAFL_TEST_SQLSERVER"], name], capture_output=True, text=True, timeout=300
    )
    assert done.returncode == 0, f"exit code {done.returncode:#x}: {done.stderr[-800:]}"


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_whole_numbers_of_more_than_38_digits(registry):
    """A new table got decimal(38, 0) for them, and the export then said "Make the column wider …,
    e.g. decimal(38, 0)", although 38 digits are the most (the table was rolled back). It stops
    before it makes the table, and says to keep them as text; so for such a column of a table."""
    ctx, name = _sqlserver()
    more = r"SQL Server's decimals hold at most 38 digits, and Id has whole numbers with more \(e\.g\. {}\)\. Keep them in a text column"

    def export(df):
        registry.actions["db.export"](df, {"connection": "server", "table": name}, ctx)

    try:
        for values, dtype, example in (([2**127 - 1, 5], pl.Int128, 2**127 - 1), ([-(2**127), 5], pl.Int128, -(2**127)), ([2**128 - 1], pl.UInt128, 2**128 - 1)):
            with pytest.raises(ActionError, match="^" + more.format(example)):
                export(pl.DataFrame({"Id": pl.Series(values, dtype=dtype)}))
            with ctx.database("server") as db:
                assert db.read(f"SELECT OBJECT_ID('{name}') AS id")["id"][0] is None  # no table made
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (Id decimal(38, 0))")
        with pytest.raises(ActionError, match=rf"^The column Id of {name} \(decimal\(38,0\)\): " + more.format(2**127 - 1)):
            export(pl.DataFrame({"Id": pl.Series([2**127 - 1], dtype=pl.Int128)}))
        export(pl.DataFrame({"Id": pl.Series([10**38 - 1], dtype=pl.Int128)}))  # 38 digits fit
        with ctx.database("server") as db:
            db.execute(f"DROP TABLE {name}")
        export(pl.DataFrame({"Id": [str(2**127 - 1)]}))  # as text, as the message says: a column of text
        with ctx.database("server") as db:
            assert db.read(f"SELECT Id FROM {name}")["Id"].to_list() == [str(2**127 - 1)]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_decimal_columns_name_the_longest_number_and_a_way_that_works(registry):
    """For -(10^38) and 10^19 the example was 10000000000000000000, which has 20 digits, and for a
    decimal(38, 0) column the advice "change Id to text" failed on the next run (a text too long
    for its column). The example has the most digits, and the advice says to change the column."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({"Id": pl.Series([-(10**38), 10**19], dtype=pl.Int128)})

    def export(df):
        registry.actions["db.export"](df, {"connection": "server", "table": name}, ctx)

    try:
        for kind in ("decimal(38,0)", "decimal(10,0)"):
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (Id {kind})")
            message = (
                rf"^The column Id of {name} \({re.escape(kind)}\): SQL Server's decimals hold at most 38 digits, and Id has whole numbers "
                r"with more \(e\.g\. -100000000000000000000000000000000000000\)\. Keep them in a text column: change the column's "
                r"type in the database to text, e\.g\. varchar\(40\), and Id to text before this step \(Change type / format\)\.$"
            )
            with pytest.raises(ActionError, match=message):
                export(data)
        with pytest.raises(ActionError, match=r"up to 10 digits before the decimal point \(decimal\(10,0\)\), but Id has longer ones \(e\.g\. 10000000000000000000\)"):
            export(pl.DataFrame({"Id": pl.Series([10**19, 5], dtype=pl.Int128)}))  # the longest: 20 digits, not 5
        with ctx.database("server") as db:  # the advice works
            db.execute(f"ALTER TABLE {name} ALTER COLUMN Id varchar(40)")
        export(data.with_columns(pl.col("Id").cast(pl.String)))
        with ctx.database("server") as db:
            assert sorted(db.read(f"SELECT Id FROM {name}")["Id"].to_list()) == sorted([str(-(10**38)), str(10**19)])
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_every_column_type_gets_the_text_advice_beyond_38_digits(registry):
    """An int, bigint, float, real or money column was told "e.g. to decimal(38, 0)" for numbers of
    more than 38 digits, which failed on the next run. Every type gets the advice of a text column,
    and that works."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({"Id": pl.Series([-(10**38), 10**19], dtype=pl.Int128)})
    try:
        for kind in ("int", "bigint", "float", "real", "money", "decimal(10,0)"):
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (Id {kind})")
            message = (
                rf"^The column Id of {name} \({re.escape(kind)}\): SQL Server's decimals hold at most 38 digits, and Id has whole "
                r"numbers with more \(e\.g\. -100000000000000000000000000000000000000\)\. Keep them in a text column: change the "
                r"column's type in the database to text, e\.g\. varchar\(40\), and Id to text before this step"
            )
            with pytest.raises(ActionError, match=message):
                registry.actions["db.export"](data, {"connection": "server", "table": name}, ctx)
            with ctx.database("server") as db:
                db.execute(f"ALTER TABLE {name} ALTER COLUMN Id varchar(40)")  # as the message says
            registry.actions["db.export"](data, {"connection": "server", "table": name}, ctx)
            with ctx.database("server") as db:
                assert sorted(db.read(f"SELECT Id FROM {name}")["Id"].to_list()) == sorted([str(-(10**38)), str(10**19)]), kind
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_whole_numbers_beyond_a_bigint_into_other_columns(registry):
    """The driver's words were the message ("int too big to convert", or a text about the TDS
    protocol for a decimal(19, 0)). A text column gets the digits, a decimal column with enough
    digits the exact value; a shorter text, a narrower decimal, float, real and other types stop
    with a message that names the column and its type."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({"N": [1, 2], "Id": pl.Series([2**64 - 1, 5], dtype=pl.UInt64)})
    stored = {
        "nvarchar(50)": [str(2**64 - 1), "5"], "varchar(30)": [str(2**64 - 1), "5"], "nvarchar(max)": [str(2**64 - 1), "5"],
        "decimal(20, 0)": [str(2**64 - 1), "5"], "decimal(38, 2)": [f"{2**64 - 1}.00", "5.00"],
    }  # fmt: skip
    refused = {
        "nvarchar(10)": r"holds texts of up to 10 characters \(nvarchar\(10\)\), but Id has longer ones \(e\.g\. “18446744073709551615”",
        "decimal(19, 0)": r"holds numbers of up to 19 digits before the decimal point \(decimal\(19,0\)\), but Id has longer ones "
        r"\(e\.g\. 18446744073709551615\)",
        "float": r"holds decimal numbers \(float\), which keep about 15 digits, but Id has whole numbers with more \(e\.g\. "
        r"18446744073709551615\), which it would round",
        "real": r"holds decimal numbers \(real\), which keep about 7 digits",
        "sql_variant": r"^The column Id of \S+ \(sql_variant\) can't take whole numbers outside -9,223,372,036,854,775,808 to "
        r"9,223,372,036,854,775,807, and Id has some \(e\.g\. 18446744073709551615\)",
        "money": r"\(money\) can't take whole numbers outside",
    }

    def export(**params):
        registry.actions["db.export"](data, {"connection": "server", "table": name} | params, ctx)

    try:
        for kind in [*stored, *refused]:
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (N int, Id {kind})")
            if kind in refused:
                with pytest.raises(ActionError, match=refused[kind]):
                    export()
                continue
            export()
            with ctx.database("server") as db:
                assert db.read(f"SELECT CONVERT(nvarchar(45), Id) AS Id FROM {name} ORDER BY N")["Id"].to_list() == stored[kind], kind
        for kind in ("decimal(20, 0)", "nvarchar(50)"):  # keys of such columns find the rows
            with ctx.database("server") as db:
                db.execute(f"DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (N int, Id {kind})")
            export()
            export(mode="upsert", key_columns=["Id"])
            assert ctx.messages[-1] == ("info", f"{name}: updated 2 rows"), kind
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_columns_that_hold_no_numbers_get_no_advice_to_change_them(registry):
    """For whole numbers beyond 64 bits, a sql_variant column was told to become decimal(38, 0) or
    varchar(40), and a date, time, uniqueidentifier, xml or binary column to become decimal(38, 0),
    which SQL Server refuses. A sql_variant column takes them as text, as its message says; the others
    say that they can't take whole numbers, whatever their number of digits."""
    ctx, name = _sqlserver()
    frames = [pl.DataFrame({"Id": pl.Series(values, dtype=pl.Int128), "V": [7, 8]}) for values in ([10**19, 5], [10**38, 5])]

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def table(kind):
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (Id {kind}, V int)")

    try:
        for kind in ("date", "time", "datetime2", "datetimeoffset", "datetime", "uniqueidentifier", "xml", "binary(16)", "varbinary(50)"):
            table(kind)
            for df in frames:
                message = rf"^The column Id of {name} \(\S+\) can't take whole numbers, and Id has some \(e\.g\. {df['Id'][0]}\)\.$"
                with pytest.raises(ActionError, match=message):
                    export(df)
        table("sql_variant")
        for df in frames:
            with pytest.raises(ActionError, match=(
                rf"^The column Id of {name} \(sql_variant\) can't take whole numbers outside -9,223,372,036,854,775,808 to "
                rf"9,223,372,036,854,775,807, and Id has some \(e\.g\. {df['Id'][0]}\)\. Change Id to text before this step "
                r"\(Change type / format\)\.$"
            )):  # fmt: skip
                export(df)
            export(df.with_columns(pl.col("Id").cast(pl.String)))  # as the message says
            export(df.with_columns(pl.col("Id").cast(pl.String), V=pl.lit(9)), mode="update", key_columns=["Id"])
        with ctx.database("server") as db:
            stored = db.read(f"SELECT CAST(Id AS nvarchar(40)) AS Id, V FROM {name} ORDER BY V, Id").rows()
        assert stored == [("10000000000000000000", 9), ("100000000000000000000000000000000000000", 9), ("5", 9), ("5", 9)]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_columns_of_whole_numbers_name_their_own_range(registry):
    """The message gave the range of a bigint for an int, smallint, tinyint or bit column, and a
    value beyond the column but within a bigint (2^40 into an int) got the driver's message, which
    names no column. Each column gives its own range, and values within it are written."""
    ctx, name = _sqlserver()
    big, beyond_int = pl.Series("Id", [2**64 - 1, 1], dtype=pl.UInt64), pl.Series("Id", [2**40, 1])
    cases = [
        ("bit", beyond_int, r"holds 0 and 1 \(bit\), but Id has others \(e\.g\. 1099511627776\)\. Change the column's type in the "
         r"database, e\.g\. to bigint\."),
        ("bit", pl.Series("Id", [0, 2]), r"holds 0 and 1 \(bit\), but Id has others \(e\.g\. 2\)\. .* e\.g\. to int\."),
        ("tinyint", pl.Series("Id", [-1, 5]), r"holds whole numbers from 0 to 255 \(tinyint\), but Id has others \(e\.g\. -1\)"),
        ("smallint", beyond_int, r"holds whole numbers from -32,768 to 32,767 \(smallint\), but Id has others \(e\.g\. 1099511627776\)"),
        ("int", big, r"holds whole numbers from -2,147,483,648 to 2,147,483,647 \(int\), but Id has others \(e\.g\. "
         r"18446744073709551615\)\. Change the column's type in the database, e\.g\. to decimal\(20, 0\)\."),
        ("bigint", big, r"holds whole numbers from -9,223,372,036,854,775,808 to 9,223,372,036,854,775,807 \(bigint\), but Id has others"),
    ]  # fmt: skip
    fits = {"bit": [0, 1], "tinyint": [0, 255], "smallint": [-32768, 32767], "int": [-(2**31), 2**31 - 1], "bigint": [2**63 - 1, 1]}
    try:
        for kind, values, message in cases:
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (Id {kind})")
            with pytest.raises(ActionError, match=rf"^The column Id of {name} {message}"):
                registry.actions["db.export"](pl.DataFrame([values]), {"connection": "server", "table": name}, ctx)
            registry.actions["db.export"](pl.DataFrame({"Id": fits[kind]}), {"connection": "server", "table": name}, ctx)
            with ctx.database("server") as db:
                assert sorted(db.read(f"SELECT CAST(Id AS bigint) AS Id FROM {name}")["Id"].to_list()) == sorted(fits[kind]), kind
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_delete_by_keys_looks_only_at_the_keys(registry):
    """Delete writes only the key values: decimals beside the keys, for a column of whole numbers,
    stopped it ("… holds whole numbers (int), but Price has decimals (e.g. 1.5) …"). As a key, and
    in the modes that write the column, they still stop the export."""
    ctx, name = _sqlserver()

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    data = pl.DataFrame({"N": [1], "Price": [1.5]})
    message = r"holds whole numbers \(int\), but Price has decimals \(e\.g\. 1\.5\)"
    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int, Price int)")
            db.execute(f"INSERT INTO {name} VALUES (1, 10), (2, 20)")
        export(data, mode="delete", key_columns=["N"])
        assert ctx.messages[-1] == ("info", f"{name}: deleted 1 row")
        for params in ({"mode": "delete", "key_columns": ["Price"]}, {"mode": "update", "key_columns": ["N"]}, {}):
            with pytest.raises(ActionError, match=message):
                export(data, **params)
        with ctx.database("server") as db:
            assert db.read(f"SELECT N, Price FROM {name}").rows() == [(2, 20)]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_finds_rows_by_more_key_columns_than_an_index_holds(registry):
    """An index holds 32 columns (16 before SQL Server 2016): with more keys, the export stopped
    with "The index 'tafl_stage_…_keys' on table '#tafl_stage_…' has 40 columns in the key list".
    The rows find each other without the index, also by empty keys."""
    ctx, name = _sqlserver()
    keys = [f"k{i}" for i in range(40)]
    first = pl.DataFrame({key: [1, 2, None] for key in keys} | {"Net": [1, 2, 3]})
    modes = {"update": "updated 3 rows", "upsert": "updated 3 rows", "replace_keys": "deleted 3 rows, added 3 rows", "delete": "deleted 3 rows"}
    try:
        registry.actions["db.export"](first, {"connection": "server", "table": name}, ctx)
        for mode, message in modes.items():
            changed = first.with_columns(Net=pl.col("Net") * 10)
            registry.actions["db.export"](changed, {"connection": "server", "table": name, "mode": mode, "key_columns": keys}, ctx)
            assert ctx.messages[-1] == ("info", f"{name}: {message}"), mode
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_export_mistakes_are_explained(registry):
    ctx, name = _sqlserver()
    new = f"{name}_new"

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def command(sql):
        registry.actions["db.command"](DATA, {"connection": "server", "sql": sql}, ctx)

    try:
        # A #temporary table would disappear with the connection
        for table in ("#tafl_orders", "##tafl_orders", "tempdb..#tafl_orders"):
            with pytest.raises(ActionError, match="would be a temporary table"):
                export(DATA, table=table)
        # SQL Server has no infinity; a failed first export leaves no table, and says it made none
        with pytest.raises(ActionError, match=r"^The column Net has an infinite value .* IIF\(ISINF\(\[Net\]\), NULL, \[Net\]\)\.$"):
            export(DATA.with_columns(pl.Series("Net", [1.0, float("-inf"), None])), table=new)
        with ctx.database("server") as db:
            assert db.read(f"SELECT OBJECT_ID('{new}') AS id").rows() == [(None,)]
        assert not any("Created" in text for _level, text in ctx.messages)
        # A text too long for its column: the column, with an example
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (OrderNo int PRIMARY KEY, Customer nvarchar(5), Note nchar(3))")
        too_long = (
            rf"^The column Customer of {name} holds texts of up to 5 characters \(nvarchar\(5\)\), but Customer has "
            r"longer ones \(e.g. “Müller”, 6 characters\)\. Shorten them first, e.g. LEFT\(\[Customer\], 5\), or make "
            r"the column longer in the database\.$"
        )
        orders = DATA.select("OrderNo", "Customer")
        for mode in ("insert", "upsert", "replace_keys"):
            with pytest.raises(ActionError, match=too_long):
                export(orders, mode=mode, key_columns=["OrderNo"])
        with pytest.raises(ActionError, match=r"holds texts of up to 3 characters \(nchar\(3\)\), but Note .*“a😀b”, 4"):
            export(pl.DataFrame({"OrderNo": [1], "Note": ["a😀b"]}))  # an emoji counts twice
        export(pl.DataFrame({"OrderNo": [1], "Note": ["😀b"]}))
        export(pl.DataFrame({"OrderNo": [1], "Customer": ["Bäcker"]}), mode="delete", key_columns=["Customer"])  # finds rows only
        # The duplicate key value stays in the message, which says everything once
        with pytest.raises(ActionError) as error:
            export(orders.with_columns(Customer=pl.lit("A")))
        assert str(error.value).endswith(f"Cannot insert duplicate key in object '{name}'. The duplicate key value is (1).")
        export(orders.head(2).with_columns(Customer=pl.lit("A")), mode="replace")
        with pytest.raises(ActionError) as error:
            export(orders.with_columns(Customer=pl.lit("A")))
        assert str(error.value).endswith("The duplicate key value is (1). 1 more like it.")
        # Date-times with a time zone: the local time, as in the Excel export and in SQLite
        at = pl.Series("At", [dt.datetime(2026, 10, 1, 8, 30)]).dt.replace_time_zone("Europe/Berlin")
        price = pl.Series("Price", [decimal.Decimal("12.345")], dtype=pl.Decimal(12, 3))
        export(pl.DataFrame({"OrderNo": [7], "At": at, "Price": price}), table=new)
        with ctx.database("server") as db:
            assert db.read(f"SELECT At FROM {new}")["At"].to_list() == [dt.datetime(2026, 10, 1, 8, 30)]
            assert db.read(f"SELECT DATA_TYPE AS t, NUMERIC_PRECISION AS p, NUMERIC_SCALE AS s FROM INFORMATION_SCHEMA.COLUMNS "
                           f"WHERE TABLE_NAME = '{new.split('.')[1]}' AND COLUMN_NAME = 'Price'").rows() == [("decimal", 12, 3)]  # fmt: skip
        assert ctx.messages[-2:] == [("info", f"Created table {new}"), ("info", f"{new}: added 1 row")]
        # GO with a repeat count and a comment, as in SSMS
        command(f"INSERT INTO {new} (OrderNo) VALUES (8)\nGO -- next\nUPDATE {new} SET OrderNo = OrderNo + 1\nGO 2")
        assert ctx.messages[-1] == ("info", "Done: 5 rows changed")
        command(f"DELETE FROM {new} WHERE OrderNo = 9")
        assert ctx.messages[-1] == ("info", "Done: 1 row changed")
    finally:
        with ctx.database("server") as db:
            for table in (name, new):
                db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")


_SQLSERVER = pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
_BERLIN = pl.Series([dt.datetime(2026, 7, 1, 8, 30, 0, 500000), dt.datetime(2026, 1, 1, 8, 30)]).dt.replace_time_zone("Europe/Berlin")
# The type of a column, and key values for it: Add stores them in the column's own way (Łódź is
# Lódz in a varchar, 08:00:01.5 is 08:00:01 in a datetime2(0)), and the keyed modes must find them
_KEYS = [
    ("varchar(50)", ["Łódź", "Praha č", "Müller", "straße", None]),
    ("nvarchar(50)", ["Łódź", "東京", "😀 smile", None]),
    ("varchar(50) COLLATE Latin1_General_CS_AS", ["Łódź", "a", "A"]),
    ("nvarchar(50) COLLATE Latin1_General_BIN2", ["Łódź", "a", "A"]),
    ("varchar(50) COLLATE Latin1_General_100_CI_AS_SC_UTF8", ["Łódź", "Praha č"]),
    ("varchar(50) COLLATE Latin1_General_CI_AS", ["Łódź", "Gdańsk", "€ 5", "Ελλάδα"]),
    ("varchar(50) COLLATE Polish_CI_AS", ["Łódź", "Gdańsk", "Kraków", "Ελλάδα"]),
    ("char(10) COLLATE Greek_CI_AS", ["Ελλάδα", "Łódź"]),
    ("varchar(max) COLLATE Polish_CI_AS", ["Łódź " + "x" * 9000, "b"]),
    ("char(5)", ["AB      ", "x"]),  # the spaces that don't fit are cut, as SQL Server does
    ("datetime2(0)", [dt.datetime(2026, 10, 2, 8), dt.datetime(2026, 10, 2, 8, 0, 1, 500000), dt.datetime(2026, 10, 2, 8, 0, 2, 250000)]),
    ("datetime2(3)", [dt.datetime(2026, 10, 2, 8, 0, 1, 123456), None]),
    ("datetime2(7)", [dt.datetime(2026, 10, 2, 8, 0, 1, 123456)]),
    ("datetime", [dt.datetime(2026, 10, 2, 8, 0, 1, 998500), dt.datetime(2026, 10, 2, 8, 0, 1, 123456)]),
    ("smalldatetime", [dt.datetime(2026, 10, 2, 8, 0, 29, 999000), dt.datetime(2026, 10, 2, 8, 0, 30)]),
    ("time(0)", [dt.time(8, 0, 1, 500000), dt.time(8, 0, 2)]),
    ("time(3)", [dt.time(8, 0, 1, 123456)]),
    ("date", [dt.datetime(2026, 10, 2, 8, 0, 1), dt.datetime(2026, 10, 3)]),
    ("datetimeoffset(0)", [dt.datetime(2026, 7, 1, 8, 30, 0, 500000), dt.datetime(2026, 1, 1, 8, 30)]),
    ("datetimeoffset(7)", _BERLIN),
    ("datetimeoffset(3)", ["2026-10-01T08:30:00.123456+02:00", "2026-10-01T08:30:00Z", "2026-10-01 09:30:00"]),
    ("varbinary(10)", [b"\x00\xff", b"\x01"]),
    ("varbinary(max)", [b"\x00\xff" * 5000, b"\x01"]),
    ("nvarchar(1000)", ["ä" * 900, "b" * 900]),  # too long for an index on the key
    ("nvarchar(max)", ["ä" * 2000, "b"]),
    # Values of another type than the column's, as in 0.2.0
    ("int", [1, 2, None]),
    ("int", ["1", "2", "003"]),
    ("int", [1.0, 2.0]),
    ("varchar(10)", [1, 2, 3]),
    ("bigint", [9007199254740993, -9223372036854775808]),
    ("bit", [True, False]),
    ("datetime2(0)", [dt.date(2026, 1, 1), dt.date(2026, 1, 2)]),
    ("decimal(18,2)", [1.1, 2.25, 0.07]),
    ("decimal(10,2)", pl.Series([decimal.Decimal("1.50"), decimal.Decimal("2.255")], dtype=pl.Decimal(10, 3))),
    ("uniqueidentifier", ["6F9619FF-8B86-D011-B42D-00C04FC964FF"]),
]


@_SQLSERVER
@pytest.mark.parametrize(("kind", "values"), _KEYS, ids=[f"{i} {kind}" for i, (kind, _values) in enumerate(_KEYS)])
def test_sqlserver_keys_find_the_rows_that_add_stored(registry, kind, values):
    """The keyed modes stage the rows in a #temporary table with the types and collations of the
    table's own columns: the keys find the rows that Add stored, whatever Add made of them, and
    every mode stores the values as Add does."""
    ctx, name = _sqlserver()
    keys = values if isinstance(values, pl.Series) else pl.Series(values, strict=False)
    data = pl.DataFrame({"K": keys, "V": keys})
    n, s = data.height, "s" if data.height != 1 else ""
    expected = {
        "update": f"updated {n} row{s}",
        "upsert": f"updated {n} row{s}",
        "replace_keys": f"deleted {n} row{s}, added {n} row{s}",
        "delete": f"deleted {n} row{s}",
    }

    def export(note, **params):
        registry.actions["db.export"](data.with_columns(N=pl.lit(note)), {"connection": "server", "table": name} | params, ctx)

    def rows():
        with ctx.database("server") as db:
            return sorted(db.read(f"SELECT K, V, N FROM {name}").rows(), key=repr)

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K {kind}, V {kind}, N nvarchar(10))")
        export("old")
        stored = rows()
        assert len(stored) == n
        with ctx.database("server") as db:
            db.execute(f"SELECT * INTO {name}_added FROM {name}")
        for mode, message in expected.items():
            with ctx.database("server") as db:
                db.execute(f"DELETE FROM {name}; INSERT INTO {name} SELECT * FROM {name}_added")
            export("new", mode=mode, key_columns=["K"])
            assert _logged(ctx) == ("info", f"{name}: {message}"), mode  # warnings of cut digits come after it
            assert rows() == ([] if mode == "delete" else sorted([(k, v, "new") for k, v, _note in stored], key=repr)), mode
        with ctx.database("server") as db:
            db.execute(f"DELETE FROM {name}")
        export("old", mode="upsert", key_columns=["K"])  # the new rows come from the stage
        assert rows() == stored
    finally:
        with ctx.database("server") as db:
            for table in (name, f"{name}_added"):
                db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")


@_SQLSERVER
def test_sqlserver_keys_must_be_unique_as_the_column_holds_them(registry):
    """08:00:01.2 and 08:00:01.7 are the same second in a datetime2(0) column: updating by them
    wouldn't say which row wins."""
    ctx, name = _sqlserver()
    seconds = [dt.datetime(2026, 10, 2, 8, 0, 1, 200000), dt.datetime(2026, 10, 2, 8, 0, 1, 700000)]
    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K datetime2(0), Day date, V int)")
        for key, values in (("K", seconds), ("Day", [dt.datetime(2026, 10, 2, 8), dt.datetime(2026, 10, 2, 9)])):
            for mode in ("update", "upsert"):
                with pytest.raises(ActionError, match=rf"don't identify the rows: 1 key value occurs more than once \(e.g. {key} = 2026-10-02"):
                    registry.actions["db.export"](
                        pl.DataFrame({key: values, "V": [1, 2]}), {"connection": "server", "table": name, "mode": mode, "key_columns": [key]}, ctx
                    )
        registry.actions["db.export"](pl.DataFrame({"K": seconds[:1], "V": [1]}), {"connection": "server", "table": name}, ctx)
        with ctx.database("server") as db:
            assert db.read(f"SELECT K FROM {name}").rows() == [(dt.datetime(2026, 10, 2, 8, 0, 1),)]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_datetimeoffset_columns_get_the_zones_offset(registry):
    """A date-time with a time zone keeps its zone's offset, one without a zone gets the offset of
    this PC's time zone on its day (from the driver), in every mode; imported values (the moments,
    date-times in UTC) find their rows by key."""
    ctx, name = _sqlserver()
    copy = f"{name}_copy"
    utc = pl.Series([dt.datetime(2026, 7, 1, 6, 30, 0, 500000), dt.datetime(2026, 1, 1, 7, 30), dt.datetime(2026, 7, 1, 6, 30)])
    data = pl.DataFrame({"N": [1, 2, 3], "At": utc.dt.replace_time_zone("UTC")})
    berlin = data.with_columns(pl.col("At").dt.convert_time_zone("Europe/Berlin"))  # the same moments
    naive = dt.datetime(2026, 7, 1, 8, 30)
    local = naive.astimezone().strftime("%z")
    local = f"{local[:3]}:{local[3:]}"

    def export(df, table=name, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": table} | params, ctx)

    def stored(table=name):  # as SQL Server writes them, with their offsets
        with ctx.database("server") as db:
            return db.read(f"SELECT N, CONVERT(nvarchar(40), At) AS At FROM {table} ORDER BY N").rows()

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int, At datetimeoffset(3))")
            db.execute(f"CREATE TABLE {copy} (N int, At datetimeoffset(3))")
        for mode in ("insert", "upsert"):
            with ctx.database("server") as db:
                db.execute(f"DELETE FROM {name}")
            export(data, mode=mode, key_columns=["N"])
            assert stored() == [
                (1, "2026-07-01 06:30:00.500 +00:00"), (2, "2026-01-01 07:30:00.000 +00:00"), (3, "2026-07-01 06:30:00.000 +00:00"),
            ], mode  # fmt: skip
        export(berlin, mode="update", key_columns=["N"])
        assert stored() == [
            (1, "2026-07-01 08:30:00.500 +02:00"), (2, "2026-01-01 08:30:00.000 +01:00"), (3, "2026-07-01 08:30:00.000 +02:00"),
        ]  # fmt: skip
        export(data.drop("N"), mode="delete", key_columns=["At"])  # the same moments in another zone
        assert ctx.messages[-1] == ("info", f"{name}: deleted 3 rows")
        export(pl.DataFrame({"N": [4], "At": [naive]}), mode="upsert", key_columns=["N"])
        export(pl.DataFrame({"N": [5], "At": [naive]}))
        assert stored() == [(4, f"2026-07-01 08:30:00.000 {local}"), (5, f"2026-07-01 08:30:00.000 {local}")]
        # An import gives the moments, date-times in UTC: by key they find their rows
        imported = registry.actions["db.import"](None, {"connection": "server", "table": name}, ctx)
        moment = naive.astimezone().astimezone(dt.UTC)
        assert imported.schema["At"] == pl.Datetime("us", "UTC") and imported["At"].to_list() == [moment] * 2
        export(imported, table=copy)
        export(imported, table=copy, mode="upsert", key_columns=["At", "N"])
        assert ctx.messages[-1] == ("info", f"{copy}: nothing changed")  # the keys found their rows
        export(imported.with_columns(N=pl.col("N") + 10), table=copy, mode="upsert", key_columns=["At", "N"])
        utc = moment.strftime("%Y-%m-%d %H:%M:%S.000 +00:00")
        assert stored(copy) == [(4, utc), (5, utc), (14, utc), (15, utc)]  # the same moments, written in UTC
        export(imported.head(1).with_columns(N=pl.lit(24)), mode="update", key_columns=["At"])  # both rows have this moment
        assert ctx.messages[-1] == ("info", f"{name}: updated 2 rows")
    finally:
        with ctx.database("server") as db:
            for table in (name, copy):
                db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")


@_SQLSERVER
def test_sqlserver_spaces_that_dont_fit_are_cut(registry):
    """SQL Server leaves out the spaces at the end of a text that don't fit its column (and
    ignores them when it compares texts): so does the export, in every mode. Other values that
    are too long are refused with the column and an example."""
    ctx, name = _sqlserver()

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def stored():
        with ctx.database("server") as db:
            return db.read(f"SELECT * FROM {name} ORDER BY Id").rows()

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (Id int, Code nvarchar(5), Fixed char(5), Short varchar(3), Data varbinary(2))")
        export(pl.DataFrame({"Id": [1, 2], "Code": ["AB      ", "ABCDE  "], "Fixed": ["x      ", None], "Short": ["é   ", "ab"]}))
        assert stored() == [(1, "AB   ", "x    ", "é  ", None), (2, "ABCDE", None, "ab", None)]
        export(pl.DataFrame({"Id": [1, 3], "Code": ["CD        ", "😀😀  "]}), mode="upsert", key_columns=["Id"])
        assert ctx.messages[-1] == ("info", f"{name}: updated 1 row, added 1 row")
        assert [row[:2] for row in stored()] == [(1, "CD   "), (2, "ABCDE"), (3, "😀😀 ")]  # an emoji takes two
        export(pl.DataFrame({"Code": ["CD          ", "ABCDEFG"], "Short": ["new", "too long"]}), mode="update", key_columns=["Code"])
        assert ctx.messages[-1] == ("info", f"{name}: updated 1 row")  # ABCDEFG can't be in the column: left out
        assert stored()[0][:4] == (1, "CD   ", "x    ", "new")
        too_long = r"^The column Code .* up to 5 characters \(nvarchar\(5\)\), but Code has longer ones \(e.g. “ABCDEF  ”, 8 characters\)"
        with pytest.raises(ActionError, match=too_long):
            export(pl.DataFrame({"Id": [4], "Code": ["ABCDEF  "]}))
        too_long = r"^The column Data .* up to 2 bytes \(varbinary\(2\)\), but Data has longer ones \(e.g. 0x000102, 3 bytes\)\. Shorten"
        with pytest.raises(ActionError, match=too_long):
            export(pl.DataFrame({"Id": [4], "Data": [b"\x00\x01\x02"]}), mode="upsert", key_columns=["Id"])
        export(pl.DataFrame({"Data": [b"\x00\x01\x02"]}), mode="delete", key_columns=["Data"])  # can't be in the column
        assert ctx.messages[-1] == ("info", f"{name}: nothing changed") and len(stored()) == 3
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


_KEYED_MODES = ("update", "upsert", "replace_keys", "delete")


def _logged(ctx) -> tuple[str, str]:
    """The last message of a step that is no warning: an export says what it changed, then warns."""
    return next(message for message in reversed(ctx.messages) if message[0] != "warning")


def _keyed_message(mode: str, n: int) -> str:
    rows = f"{n} row{'s' if n != 1 else ''}"
    return {"update": f"updated {rows}", "upsert": f"updated {rows}", "replace_keys": f"deleted {rows}, added {rows}",
            "delete": f"deleted {rows}"}[mode]  # fmt: skip


@_SQLSERVER
def test_sqlserver_varchar_columns_get_texts_as_the_server_converts_them(registry):
    """Texts for char and varchar columns arrive as the server converts N'…' to the column's
    collation, in every mode: Łódź stays Łódź in a Polish or UTF-8 column and is Lódz in a
    Latin1 one, and keys find the rows the database stored itself. Before, the driver converted
    them to this PC's code page first (Łódź was Lódz in every varchar column)."""
    ctx, name = _sqlserver()
    texts = ["Łódź", "Gdańsk", "Kraków", "Ελλάδα", "東京", "Müller €"]
    data = pl.DataFrame({"K": texts, "V": texts, "C": texts})
    literals = ", ".join(f"(N'{t}', N'{t}', N'{t}', N'sql')" for t in texts)

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def stored(note):
        with ctx.database("server") as db:
            return sorted(db.read(f"SELECT K, V, C FROM {name} WHERE N = N'{note}'").rows())

    try:
        for collation in ("Polish_CI_AS", "Latin1_General_100_CI_AS_SC_UTF8", "Latin1_General_CI_AS"):
            columns = f"K varchar(30) COLLATE {collation}, V varchar(max) COLLATE {collation}, C char(20) COLLATE {collation}"
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} ({columns}, N nvarchar(10))")
                db.execute(f"INSERT INTO {name} VALUES {literals}")
            by_server = stored("sql")
            lodz = "Lódz" if collation == "Latin1_General_CI_AS" else "Łódź"
            assert (lodz, lodz, lodz) in [(k, v, c.rstrip()) for k, v, c in by_server], collation
            export(data.with_columns(N=pl.lit("add")))
            assert stored("add") == by_server, collation
            for mode in _KEYED_MODES:
                with ctx.database("server") as db:
                    db.execute(f"DELETE FROM {name}; INSERT INTO {name} VALUES {literals}")
                export(data.with_columns(N=pl.lit("new")), mode=mode, key_columns=["K"])
                assert ctx.messages[-1] == ("info", f"{name}: {_keyed_message(mode, len(texts))}"), (collation, mode)
                assert stored("sql") == [] and stored("new") == ([] if mode == "delete" else by_server), (collation, mode)
        # Too long in bytes for a UTF-8 column (äöü takes 6): refused with the column and an example,
        # in every mode. Before, the server's message came through, in keyed modes naming the stage
        too_long = (
            rf"^The column T of {name} holds texts of up to 5 bytes in UTF-8 \(varchar\(5\)\), but T has longer ones "
            r"\(e\.g\. “äöü”, 6 bytes: letters such as ä take two\)\. Shorten them first, or make the column longer "
            r"in the database\.$"
        )
        with ctx.database("server") as db:
            db.execute(f"DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (K int, T varchar(5) COLLATE Latin1_General_100_CI_AS_SC_UTF8, "
                       "F char(4) COLLATE Latin1_General_100_CI_AS_SC_UTF8)")  # fmt: skip
            db.execute(f"INSERT INTO {name} VALUES (1, 'old', 'ab')")
        for mode in ("insert", "update", "upsert", "replace_keys"):
            with pytest.raises(ActionError, match=too_long):
                export(pl.DataFrame({"K": [1], "T": ["äöü"]}), mode=mode, key_columns=["K"])
        with pytest.raises(ActionError, match=r"holds texts of up to 4 bytes in UTF-8 \(char\(4\)\), but F .*“€ab”, 5 bytes"):
            export(pl.DataFrame({"K": [1], "F": ["€ab"]}), mode="upsert", key_columns=["K"])  # € takes three
        export(pl.DataFrame({"K": [1], "T": ["äöx"]}), mode="update", key_columns=["K"])  # 5 bytes fit
        export(pl.DataFrame({"K": [1], "T": ["äö    "], "F": ["ä  "]}), mode="upsert", key_columns=["K"])  # spaces that don't fit are cut
        export(pl.DataFrame({"T": ["äöü", "äö"]}), mode="delete", key_columns=["T"])  # äöü can't be in the column: left out
        assert ctx.messages[-1] == ("info", f"{name}: deleted 1 row")
        export(pl.DataFrame({"K": [1], "T": ["äöx"], "F": ["ä  "]}))
        with ctx.database("server") as db:
            assert db.read(f"SELECT K, T, F FROM {name}").rows() == [(1, "äöx", "ä  ")]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_rounds_decimals_as_the_server_does(registry):
    """1.005 is 1.01 in a decimal(10,2), as SQL Server's own CAST makes it (half away from zero),
    in every mode: keys find the rows the server rounded itself, and nothing is added twice."""
    ctx, name = _sqlserver()
    texts = ["1.005", "-0.005", "2.675", "0.125", "1.015", "1.004"]
    data = pl.DataFrame({"K": pl.Series([decimal.Decimal(t) for t in texts], dtype=pl.Decimal(10, 3))})
    by_sql = f"INSERT INTO {name} VALUES " + ", ".join(f"(CAST({t} AS decimal(10,3)), {t}, N'sql')" for t in texts)

    def stored(note):
        with ctx.database("server") as db:
            return sorted(db.read(f"SELECT CAST(K AS varchar(20)) AS K, CAST(V AS varchar(20)) AS V FROM {name} WHERE N = N'{note}'").rows())

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K decimal(10,2), V decimal(10,2), N nvarchar(10))")
            db.execute(by_sql)
        by_server = stored("sql")
        assert by_server == sorted((v, v) for v in ("1.01", "-0.01", "2.68", "0.13", "1.02", "1.00"))
        export(data.with_columns(V=pl.col("K"), N=pl.lit("add")))
        assert stored("add") == by_server
        for mode in _KEYED_MODES:
            with ctx.database("server") as db:
                db.execute(f"DELETE FROM {name}; {by_sql}")
            export(data.with_columns(V=pl.col("K"), N=pl.lit("new")), mode=mode, key_columns=["K"])
            assert ctx.messages[-1] == ("info", f"{name}: {_keyed_message(mode, len(texts))}"), mode
            assert stored("sql") == [] and stored("new") == ([] if mode == "delete" else by_server), mode
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_writes_every_digit_of_38_digit_decimals(registry):
    """numeric(38,0) IDs and decimal(38,10) amounts keep all their digits, in every mode (the
    driver converted them with 28 digits): imported and written back by key, they find their
    rows, and an upsert adds nothing."""
    ctx, name = _sqlserver()
    copy = f"{name}_copy"
    D = decimal.Decimal
    ids = ["12345678901234567890123456789012345678", "-99999999999999999999999999999999999999", "12345678901234567890123456789012345677"]
    amounts = ["1234567890123456789012345678.0123456789", "-9999999999999999999999999999.9999999999", "0.0000000001"]
    data = pl.DataFrame({
        "Id": pl.Series([D(v) for v in ids], dtype=pl.Decimal(38, 0)),
        "Amount": pl.Series([D(v) for v in amounts], dtype=pl.Decimal(38, 10)),
        "N": [1, 2, 3],
    })  # fmt: skip
    expected = [(i, a, n) for i, a, n in zip(ids, amounts, [1, 2, 3])]

    def export(df, table=name, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": table} | params, ctx)

    def stored(table=name):
        with ctx.database("server") as db:
            return db.read(f"SELECT CAST(Id AS varchar(50)) AS Id, CAST(Amount AS varchar(50)) AS Amount, N FROM {table} ORDER BY N").rows()

    try:
        with ctx.database("server") as db:
            for table in (name, copy):
                db.execute(f"CREATE TABLE {table} (Id numeric(38,0), Amount decimal(38,10), N int)")
        export(data)
        assert stored() == expected
        for key in ("Id", "Amount"):
            for mode in _KEYED_MODES:
                with ctx.database("server") as db:
                    db.execute(f"DELETE FROM {name}")
                export(data)
                export(data.with_columns(N=pl.col("N") + 10), mode=mode, key_columns=[key])
                assert ctx.messages[-1] == ("info", f"{name}: {_keyed_message(mode, 3)}"), (key, mode)
                assert stored() == ([] if mode == "delete" else [(i, a, n + 10) for i, a, n in expected]), (key, mode)
        # Imported (as exact decimals) and written back by key
        export(data, mode="replace")
        imported = registry.actions["db.import"](None, {"connection": "server", "table": name}, ctx)
        assert imported.schema == {"Id": pl.Decimal(38, 0), "Amount": pl.Decimal(38, 10), "N": pl.Int64}
        export(imported, table=copy)
        export(imported, table=copy, mode="upsert", key_columns=["Id"])
        assert ctx.messages[-1] == ("info", f"{copy}: updated 3 rows")
        export(imported, table=copy, mode="delete", key_columns=["Amount"])
        assert ctx.messages[-1] == ("info", f"{copy}: deleted 3 rows")
    finally:
        with ctx.database("server") as db:
            for table in (name, copy):
                db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")


@_SQLSERVER
@pytest.mark.parametrize(
    ("columns", "values"),
    [
        ({"s": "xml"}, ["<a/>"]),
        ({"x": "xml"}, ["<a/>"]),
        ({"s": "geography"}, ["POINT (8.5 47.4)"]),
        ({"x": "hierarchyid"}, ["/1/2/"]),
        ({"tafl_target": "xml", "TAFL_STAGE": "xml"}, ["<a/>"]),  # named like the names in the statements
    ],
    ids=["s xml", "x xml", "s geography", "x hierarchyid", "named like the names in the statements"],
)
def test_sqlserver_keyed_modes_take_xml_and_clr_columns_of_any_name(registry, columns, values):
    """The statements of the keyed modes named the table x and the stage s: an xml, geography or
    hierarchyid column named s or x made that name ambiguous ("Ambiguous column name 's'"), since
    s.[K] could also be the member K of such a column."""
    ctx, name = _sqlserver()
    definitions = ", ".join(f"[{column}] {kind}" for column, kind in columns.items())
    data = pl.DataFrame({"K": [1, 2], **{column: values * 2 for column in columns}})
    shown = ", ".join(f"CAST([{column}] AS nvarchar(100)) AS [{column}]" for column in columns)
    try:
        for mode, message in (("update", "updated 1 row"), ("upsert", "updated 1 row, added 1 row")):
            with ctx.database("server") as db:
                db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                db.execute(f"CREATE TABLE {name} (K int, {definitions})")
                db.execute(f"INSERT INTO {name} (K) VALUES (1)")
            registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": ["K"]}, ctx)
            assert ctx.messages[-1] == ("info", f"{name}: {message}"), mode
            with ctx.database("server") as db:
                stored = db.read(f"SELECT K, {shown} FROM {name} ORDER BY K").rows()
            assert stored == [(k, *values * len(columns)) for k in ([1] if mode == "update" else [1, 2])], mode
        registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": "delete", "key_columns": ["K"]}, ctx)
        assert ctx.messages[-1] == ("info", f"{name}: deleted 2 rows")
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_commands_run_in_one_transaction(registry):
    """BEGIN TRAN without COMMIT said "Done: 1 row changed" and stored nothing; after a COMMIT
    inside the command, the rest ran without a transaction, and a failed command kept its rows.
    Now such a command changes nothing, and a procedure that ends or begins a transaction (SQL
    Server's error 266) gets a message that says what that means."""
    ctx, name = _sqlserver()
    proc = f"{name}_commits"

    def command(sql):
        registry.actions["db.command"](DATA, {"connection": "server", "sql": sql}, ctx)

    def rows():
        with ctx.database("server") as db:
            return sorted(db.read(f"SELECT N FROM {name}")["N"].to_list())

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int)")
        for sql in (
            f"BEGIN TRAN; INSERT INTO {name} VALUES (1);",
            f"INSERT INTO {name} VALUES (2); COMMIT; INSERT INTO {name} VALUES (3); SELECT 1/0;",
            f"INSERT INTO {name} VALUES (5); ROLLBACK;",
        ):
            with pytest.raises(ActionError, match=r"in one transaction, so that a failed command changes nothing: leave out BEGIN TRAN"):
                command(sql)
            assert rows() == [], sql
        command(f"CREATE PROCEDURE {proc} AS BEGIN INSERT INTO {name} VALUES (100); COMMIT; END")  # runs when it is called
        ended = r"^A procedure or an EXEC in the command ended its transaction \(with COMMIT or ROLLBACK\), so the step stopped: "
        with pytest.raises(ActionError, match=ended + r"what the command changed before may be kept, and so is what the rest"):
            command(f"INSERT INTO {name} VALUES (10)\nEXEC {proc}\nINSERT INTO {name} VALUES (11)")
        assert rows() == [10, 11, 100]  # SQL Server committed them: the message says so
        began = r"^A procedure or an EXEC in the command began a transaction and didn't end it, so the step stopped and its changes"
        with pytest.raises(ActionError, match=began):
            command(f"INSERT INTO {name} VALUES (12); EXEC('BEGIN TRAN')")
        assert rows() == [10, 11, 100]
        command(f"INSERT INTO {name} VALUES (13)")
        assert ctx.messages[-1] == ("info", "Done: 1 row changed") and rows() == [10, 11, 13, 100]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{proc}') IS NOT NULL DROP PROCEDURE {proc}")
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_a_synonym_is_written_as_its_table(registry):
    """The columns of a synonym were read by its own name, which has none: by a datetime2(0) key,
    Update said "nothing changed" and Upsert added a duplicate, and a key of a case-sensitive
    collation stopped with "Cannot resolve the collation conflict". A synonym now has the columns
    of the table it stands for, also in another database; one for a table of another server stops
    with a plain message."""
    ctx, name = _sqlserver()
    connector = registry.connectors["sqlserver"]
    base, short = f"{name}_base", f"{name}_short"
    synonym, far, other_database, other_server = f"{name}_syn", f"{name}_far", f"{name}_master", f"{name}_server"
    made = [synonym, far, other_database, other_server, short]

    def export(df, table, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": table} | params, ctx)

    def stored():
        with ctx.database("server") as db:
            return db.read(f"SELECT K, D, C, V FROM {base} ORDER BY K, V").rows()

    old = (1, dt.datetime(2026, 10, 1, 8), "abc", 0)
    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {base} (K int, D datetime2(0), C varchar(10) COLLATE Latin1_General_CS_AS, V int)")
            db.execute(f"CREATE SYNONYM {synonym} FOR {base}")
            db.execute(f"CREATE SYNONYM {short} FOR [{base.split('.')[1]}]")  # a base without its schema
            db.execute(f"CREATE SYNONYM {far} FOR tempdb.{base}")
            db.execute(f"CREATE SYNONYM {other_database} FOR master.dbo.spt_values")
            db.execute(f"CREATE SYNONYM {other_server} FOR ElsewhereServer.Sales.dbo.Orders")
            table_types = connector.column_types(db.raw, base)
            assert table_types == {"K": "int", "D": "datetime2(0)", "C": "varchar(10)", "V": "int"}
            for target in (synonym, short, far, f"tempdb.{short}"):
                assert connector.column_types(db.raw, target) == table_types, target
                assert connector.stage_types(db.raw, target) == connector.stage_types(db.raw, base), target
            assert {"name", "number", "type"} <= set(connector.column_types(db.raw, other_database))
        by_second = pl.DataFrame({"D": [dt.datetime(2026, 10, 1, 8, 0, 0, 400000)], "V": [5]})
        for target in (synonym, f"tempdb.{far}"):
            for mode, message, rows in (
                ("update", "updated 1 row", [(1, dt.datetime(2026, 10, 1, 8), "abc", 5)]),
                ("upsert", "updated 1 row", [(1, dt.datetime(2026, 10, 1, 8), "abc", 5)]),
                ("delete", "deleted 1 row", []),
            ):
                with ctx.database("server") as db:
                    db.execute(f"DELETE FROM {base}; INSERT INTO {base} VALUES (1, '2026-10-01 08:00:00', 'abc', 0)")
                export(by_second, target, mode=mode, key_columns=["D"])
                assert _logged(ctx) == ("info", f"{target}: {message}") and stored() == rows, (target, mode)
            with ctx.database("server") as db:
                db.execute(f"DELETE FROM {base}; INSERT INTO {base} VALUES (1, '2026-10-01 08:00:00', 'abc', 0)")
            export(pl.DataFrame({"C": ["abc", "ABC"], "V": [7, 8]}), target, mode="update", key_columns=["C"])  # case-sensitive
            assert ctx.messages[-1] == ("info", f"{target}: updated 1 row") and stored() == [(*old[:3], 7)], target
        with ctx.database("server") as db:  # (no such server here: the export would stop when it looks for the table)
            with pytest.raises(ActionError) as error:
                connector.column_types(db.raw, other_server)
            assert str(error.value) == (
                f"{other_server} is a synonym for [ElsewhereServer].[Sales].[dbo].[Orders], a table of another server: an "
                "export reads the types of the columns it writes into, and it can read them only on the connection's "
                "server. Use a connection to that server."
            )
            assert connector.read_table_sql(db.raw, other_server) == f"SELECT * FROM {db.qualified(other_server)}"  # an import reads it as it is
    finally:
        with ctx.database("server") as db:
            for made_name in made:
                db.execute(f"IF OBJECT_ID('{made_name}', 'SN') IS NOT NULL DROP SYNONYM {made_name}")
            db.execute(f"IF OBJECT_ID('{base}') IS NOT NULL DROP TABLE {base}")


@_SQLSERVER
def test_sqlserver_stop_ends_a_running_statement(registry):
    """Stop (and Ctrl+C in tafl run) waited for a SQL Server statement to end: a query that gave no
    row yet, a command, an export that waited for a lock, which then even kept its rows. Now the
    statement is cancelled, the step ends as stopped, and its transaction rolls back."""
    ctx, name = _sqlserver()
    waiting = "WAITFOR DELAY '00:00:20'"
    cases = {
        "import": [("db.import", {"mode": "query", "query": f"{waiting}; SELECT 1 AS N"})],
        "command": [("db.import", {"mode": "query", "query": "SELECT 1 AS N"}), ("db.command", {"sql": f"INSERT INTO {name} VALUES (1); {waiting}"})],
        "export that waits for a lock": [("db.import", {"mode": "query", "query": "SELECT 7 AS N"}), ("db.export", {"table": name})],
    }
    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int)")
        for case, steps in cases.items():
            project = Project(name="Stop")
            project.add_connection("server", "sqlserver", server=os.environ["TAFL_TEST_SQLSERVER"], database="tempdb")
            table = project.add_table("Stopped")
            for action, params in steps:
                project.add_step(table, action, {"connection": "server", **params})
            holder = ctx.database("server")  # another program that holds the table
            holder.execute(f"INSERT INTO {name} WITH (TABLOCKX) VALUES (99)" if "lock" in case else "SELECT 1")
            try:
                cancel = threading.Event()
                threading.Timer(0.5, cancel.set).start()
                start = time.perf_counter()
                with pytest.raises(Cancelled):
                    Engine(project, registry).run([table.id], cancel=cancel)
                assert time.perf_counter() - start < 5, case
            finally:
                holder.rollback()
                holder.close()
            with ctx.database("server") as db:
                assert db.read(f"SELECT COUNT(*) AS n FROM {name}")["n"].to_list() == [0], case  # rolled back
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
@pytest.mark.parametrize(
    ("kind", "keys", "shown"),
    [
        ("nvarchar(10)", ["x", "ABC", "abc", "y", "Abc"], "ABC"),  # the database's collation: case-insensitive
        ("nvarchar(10) COLLATE Latin1_General_CI_AI", ["e", "é"], "e"),
        ("nvarchar(10)", ["a", "a "], "a"),  # spaces at the end don't count
        ("datetime", [dt.datetime(2026, 10, 1, 8, 0, 0, 1000), dt.datetime(2026, 10, 1, 8)], "2026-10-01 08:00:00.001000"),
        ("smalldatetime", [dt.datetime(2026, 10, 1, 8, 0, 10), dt.datetime(2026, 10, 1, 8, 0, 20)], "2026-10-01 08:00:10"),
        ("decimal(10,2)", [1.001, 1.004], "1.001"),
        ("uniqueidentifier", ["6f9619ff-8b86-d011-b42d-00c04fc964ff", "6F9619FF-8B86-D011-B42D-00C04FC964FF"],
         "6f9619ff-8b86-d011-b42d-00c04fc964ff"),  # fmt: skip
    ],
    ids=["case", "accents", "spaces at the end", "datetime", "smalldatetime", "float into decimal", "GUID"],
)
def test_sqlserver_keys_must_be_unique_as_the_column_compares_them(registry, kind, keys, shown):
    """Keys that differ in Tafl but are one key value for SQL Server were taken as different:
    Upsert added both rows, and the next Update gave them both the values of one row."""
    ctx, name = _sqlserver()
    data = pl.DataFrame({"K": keys, "V": range(len(keys))})
    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K {kind}, V int)")
        for mode in ("upsert", "update"):
            with pytest.raises(ActionError) as error:
                registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": ["K"]}, ctx)
            assert str(error.value) == (
                f"The key columns don't identify the rows: 1 key value occurs more than once (e.g. K = {shown}). Remove the "
                "duplicates first, or choose more key columns."
            ), mode
        with ctx.database("server") as db:
            assert db.read(f"SELECT COUNT(*) AS n FROM {name}")["n"].to_list() == [0]
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_the_example_of_keys_equal_as_compared_is_the_first_in_the_data(registry):
    """As for keys equal in Tafl: the first key value in the order of the rows that occurs more
    than once, the same in every run; several key columns together."""
    ctx, name = _sqlserver()

    def export(df):
        with pytest.raises(ActionError) as error:
            registry.actions["db.export"](df, {"connection": "server", "table": name, "mode": "update", "key_columns": list(df.columns[:-1])}, ctx)
        return str(error.value)

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K nvarchar(10), N int, V int)")
        many = [f"k{i}" for i in range(3000)]  # more rows than one batch of the driver
        keys = [*many, "b", "a", "B", "A"]
        for _ in range(3):
            assert export(pl.DataFrame({"K": keys, "V": range(len(keys))})) == (
                "The key columns don't identify the rows: 2 key values occur more than once (e.g. K = b). Remove the "
                "duplicates first, or choose more key columns."
            )
        two = pl.DataFrame({"K": ["abc", "abd", "ABC", "ABC"], "N": [1, 1, 2, 1], "V": [1, 2, 3, 4]})
        assert "1 key value occurs more than once (e.g. K = abc, N = 1)" in export(two)
        registry.actions["db.export"](two.head(3), {"connection": "server", "table": name, "mode": "upsert", "key_columns": ["K", "N"]}, ctx)
        assert ctx.messages[-1] == ("info", f"{name}: added 3 rows")
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_exact_decimals_get_the_checks_of_whole_numbers_in_every_mode(registry):
    """Exact decimals (Decimal) stopped with the driver's words, which named no column: "Converting
    decimal loses precision" for 1.50 into an int, bigint, nvarchar or varchar column, a long text
    about the TDS protocol for 12345.67 into a decimal(5,2), "A value doesn't fit its column…" for
    12345678901 into an int. Now they have the checks of whole numbers, the same in every mode, and
    a text column gets the same text on every PC."""
    ctx, name = _sqlserver()
    D = decimal.Decimal
    stored_as = {
        "int": (["2.00", "-3.00"], pl.Decimal(10, 2), ["2", "-3"]),
        "nvarchar(10)": (["1.50", "-0.05"], pl.Decimal(10, 2), ["1.50", "-0.05"]),
        "varchar(10)": (["1.50", "100"], pl.Decimal(10, 2), ["1.50", "100.00"]),
        "decimal(5,2)": (["1.505", "-999.994"], pl.Decimal(10, 3), ["1.51", "-999.99"]),
    }
    refused = {
        "int": (["1.50"], pl.Decimal(10, 2), r"holds whole numbers \(int\), but V has decimals \(e\.g\. 1\.50\): they would be cut off"),
        "bigint": (["12345678901234567890"], pl.Decimal(20, 0), r"holds whole numbers from .* \(bigint\), but V has others \(e\.g\. 12345678901234567890\)"),
        "nvarchar(3)": (["1234.50"], pl.Decimal(10, 2), r"holds texts of up to 3 characters \(nvarchar\(3\)\), but V has longer ones \(e\.g\. “1234\.50”"),
        "decimal(5,2)": (["12345.67"], pl.Decimal(10, 2), r"holds numbers of up to 3 digits before the decimal point \(decimal\(5,2\)\), but V"),
    }

    def export(values, dtype, mode):
        data = pl.DataFrame({"K": range(1, len(values) + 1), "V": pl.Series([D(v) for v in values], dtype=dtype)})
        registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": ["K"]}, ctx)

    def reset(kind):
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
            db.execute(f"CREATE TABLE {name} (K int, V {kind})")
            db.execute(f"INSERT INTO {name} (K) VALUES (1), (2)")

    try:
        for kind, (values, dtype, expected) in stored_as.items():
            for mode in ("insert", "upsert", "update"):
                reset(kind)
                export(values, dtype, mode)
                with ctx.database("server") as db:
                    stored = db.read(f"SELECT CAST(V AS nvarchar(40)) AS V FROM {name} WHERE V IS NOT NULL ORDER BY K")["V"].to_list()
                assert stored == expected, (kind, mode)
        for kind, (values, dtype, message) in refused.items():
            for mode in ("insert", "upsert", "update"):
                reset(kind)
                with pytest.raises(ActionError, match=rf"^The column V of {re.escape(name)} {message}"):
                    export(values, dtype, mode)
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_texts_for_date_columns_are_read_one_way_in_every_mode(registry):
    """Add refused 01.10.2026 08:00 for a datetime column (the driver reads ISO only), while Update
    and Upsert stored 10 January (the server read the text with its session's date format), and an
    empty text was 1900-01-01 there. Now every mode reads texts as Change type / format does."""
    ctx, name = _sqlserver()
    texts = {
        "date": (["01.10.2026", "2026-10-02", ""], ["2026-10-01", "2026-10-02", None]),
        "datetime": (["01.10.2026 08:00", "2026-10-02 08:30:15.5", "  "], ["2026-10-01 08:00:00.000", "2026-10-02 08:30:15.500", None]),
        "datetime2(3)": (["01.10.2026 08:00:15", "31.12.25 23:59", None], ["2026-10-01 08:00:15.000", "2025-12-31 23:59:00.000", None]),
        "smalldatetime": (["01.10.2026 08:00", "2026-10-02 08:30", ""], ["2026-10-01 08:00:00.000", "2026-10-02 08:30:00.000", None]),
        "time(3)": (["08:00", "8:30:15.25", ""], ["08:00:00.000", "08:30:15.250", None]),
        "datetimeoffset(3)": (["01.10.2026 08:00 +02:00", "2026-10-02T08:30:00Z", ""], ["2026-10-01 08:00:00.000 +02:00", "2026-10-02 08:30:00.000 +00:00", None]),
    }

    def stored():
        with ctx.database("server") as db:
            return db.read(f"SELECT K, CONVERT(nvarchar(40), V, 121) AS V FROM {name} ORDER BY K").rows()

    try:
        for kind, (values, expected) in texts.items():
            data = pl.DataFrame({"K": [1, 2, 3], "V": values})
            for mode in ("insert", "upsert", "update", "replace_keys"):
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                    db.execute(f"CREATE TABLE {name} (K int, V {kind})")
                    if mode in ("update", "replace_keys"):
                        db.execute(f"INSERT INTO {name} (K) VALUES (1), (2), (3)")
                registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": ["K"]}, ctx)
                assert stored() == list(zip([1, 2, 3], expected)), (kind, mode)
            # a key of texts finds the rows that SQL Server stored from the same dates
            registry.actions["db.export"](data.select("V").head(2), {"connection": "server", "table": name, "mode": "delete", "key_columns": ["V"]}, ctx)
            assert ctx.messages[-1] == ("info", f"{name}: deleted 2 rows"), kind
        with pytest.raises(ActionError, match=rf"^The column V of {re.escape(name)} holds dates and time.*e\.g\. “12/31/2025”"):
            registry.actions["db.export"](pl.DataFrame({"K": [1], "V": ["12/31/2025"]}), {"connection": "server", "table": name}, ctx)
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_digits_of_a_second_beyond_the_column_are_cut_in_every_mode_with_a_warning(registry):
    """time(0) and datetime2(0) with 08:00:00.5: Add refused the text with a wrong message ("a text
    is too long for its column"), Update and Upsert stored 08:00:01, and a date-time was cut to
    08:00:00 without a word. Now every mode stores 08:00:00 and says so."""
    ctx, name = _sqlserver()
    cases = {
        "time(0)": (["08:00:00.5", "08:00:02"], ["08:00:00", "08:00:02"], "'08:00:00.5' is 08:00:00"),
        "datetime2(0)": (
            [dt.datetime(2026, 10, 1, 8, 0, 0, 500000), dt.datetime(2026, 10, 1, 8, 0, 2)],
            ["2026-10-01 08:00:00", "2026-10-01 08:00:02"],
            "2026-10-01 08:00:00.500 is 2026-10-01 08:00:00",
        ),
        "datetimeoffset(1)": (
            ["2026-10-01 08:00:00.25 +02:00", "2026-10-01T08:00:02Z"],
            ["2026-10-01 08:00:00.2 +02:00", "2026-10-01 08:00:02.0 +00:00"],
            "'2026-10-01 08:00:00.25 +02:00' is 2026-10-01 08:00:00.2 +02:00",
        ),
    }
    try:
        for kind, (values, expected, example) in cases.items():
            for mode in ("insert", "upsert", "update", "replace_keys"):
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                    db.execute(f"CREATE TABLE {name} (K int, V {kind})")
                    db.execute(f"INSERT INTO {name} (K) VALUES (1), (2)")
                before = len(ctx.warnings)
                data = pl.DataFrame({"K": [1, 2], "V": values})
                registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": ["K"]}, ctx)
                with ctx.database("server") as db:
                    stored = db.read(f"SELECT CONVERT(nvarchar(40), V, 121) AS V FROM {name} WHERE V IS NOT NULL ORDER BY K")["V"].to_list()
                assert stored == expected, (kind, mode)
                assert ctx.warnings[before:] == [
                    f"V: 1 value was cut to {'whole seconds' if kind != 'datetimeoffset(1)' else '1 digit of a second'} (e.g. {example}): "
                    f"the column V of {name} holds {'no parts of a second' if kind != 'datetimeoffset(1)' else '1 digit of a second'} ({kind})"
                ], (kind, mode)
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
@pytest.mark.parametrize("kind", ["datetime2(7)", "time(7)", "datetimeoffset(7)"])
def test_sqlserver_keys_of_seven_digits_of_a_second_find_their_rows(registry, kind):
    """The driver reads such values to the microsecond: imported and written back by key, 20 of
    200 rows were found (those whose 7th digit was 0), and Upsert added the other 180 again. The
    import now says that a 7th digit was cut, and the keyed modes compare these keys to the
    microsecond. A datetimeoffset column is imported as date-times in UTC (it was text)."""
    ctx, name = _sqlserver()
    start = {"datetime2(7)": "CAST('2026-10-01 08:00:00' AS datetime2(7))", "time(7)": "CAST('08:00:00' AS time(7))",
             "datetimeoffset(7)": "CAST('2026-10-01 08:00:00 +02:00' AS datetimeoffset(7))"}[kind]  # fmt: skip

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    def count():
        with ctx.database("server") as db:
            return db.read(f"SELECT COUNT(*) AS n, SUM(V) AS v FROM {name}").row(0)

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K {kind}, V int)")
            db.execute(  # 200 rows, seconds apart; the 100-ns digit is 0 in every 10th row
                f"WITH n AS (SELECT TOP 200 ROW_NUMBER() OVER (ORDER BY (SELECT 1)) AS i FROM sys.all_objects) "
                f"INSERT INTO {name} SELECT DATEADD(NANOSECOND, (i * 1234567 % 10000000) * 100, DATEADD(SECOND, i, {start})), 0 FROM n"
            )
        imported = registry.actions["db.import"](None, {"connection": "server", "table": name}, ctx)
        assert imported.schema["K"] == (pl.Time if kind == "time(7)" else pl.Datetime("us", "UTC" if "offset" in kind else None))
        assert ctx.warnings[-1].startswith(
            "K: the column holds 7 digits of a second, but SQL Server's driver reads only 6 (to the microsecond), so a 7th digit is "
            "cut (e.g. in "
        )
        export(imported.with_columns(V=pl.lit(1)), mode="upsert", key_columns=["K"])
        assert _logged(ctx) == ("info", f"{name}: updated 200 rows") and count() == (200, 200)
        export(imported.with_columns(V=pl.lit(2)), mode="update", key_columns=["K"])
        assert _logged(ctx) == ("info", f"{name}: updated 200 rows") and count() == (200, 400)
        export(imported.head(50), mode="replace_keys", key_columns=["K"])
        assert _logged(ctx) == ("info", f"{name}: deleted 50 rows, added 50 rows") and count() == (200, 300)
        export(imported, mode="delete", key_columns=["K"])
        assert _logged(ctx) == ("info", f"{name}: deleted 200 rows") and count() == (0, None)
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_a_key_of_microseconds_that_meets_two_rows_stops_an_update(registry):
    """Compared to the microsecond, a key can meet two rows that differ only in the 7th digit:
    Update and Upsert would give both the values of one row of the data, so they stop; Delete
    deletes both, as the data asks. A text with a 7th digit finds only its own value."""
    ctx, name = _sqlserver()

    def export(df, **params):
        registry.actions["db.export"](df, {"connection": "server", "table": name} | params, ctx)

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (K datetime2(7), T datetimeoffset(7), V int)")
            db.execute(
                f"INSERT INTO {name} VALUES ('2026-10-01 08:00:01.1234567', '2026-10-01 08:00:01.1234567 +00:00', 0), "
                "('2026-10-01 08:00:01.1234568', '2026-10-01 08:00:01.1234568 +00:00', 0), "
                "('2026-10-01 08:00:02.0000001', '2026-10-01 08:00:02 +00:00', 0)"
            )
        data = pl.DataFrame({"K": [dt.datetime(2026, 10, 1, 8, 0, 2), dt.datetime(2026, 10, 1, 8, 0, 1, 123456)], "V": [1, 2]})
        for mode in ("update", "upsert"):
            with pytest.raises(ActionError) as error:
                export(data, mode=mode, key_columns=["K"])
            assert str(error.value) == (
                f"The key columns don't identify the rows of {name}: 1 key value finds rows that differ only in the 7th digit of a "
                "second of K (e.g. K = 2026-10-01 08:00:01.123456). SQL Server's driver passes date-times only to the microsecond, "
                "so Tafl compares these keys to the microsecond. Choose more key columns."
            ), mode
        export(data.head(1), mode="update", key_columns=["K"])  # 08:00:02.0000001: one row
        assert ctx.messages[-1] == ("info", f"{name}: updated 1 row")
        export(pl.DataFrame({"T": ["2026-10-01 08:00:01.1234568 +00:00"], "V": [3]}), mode="update", key_columns=["T"])
        assert ctx.messages[-1] == ("info", f"{name}: updated 1 row")  # its own 7th digit
        export(data.tail(1), mode="delete", key_columns=["K"])
        assert ctx.messages[-1] == ("info", f"{name}: deleted 2 rows")
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")


@_SQLSERVER
def test_sqlserver_ctrl_c_in_tafl_run_ends_a_running_statement(tmp_path, capsys):
    """Ctrl+C on the command line sets the stop at once, also while the statement runs in the
    driver: the run ends as stopped (exit code 130), not when the statement is done."""
    import signal

    from tafl import cli

    project = Project(name="Stop")
    project.add_connection("server", "sqlserver", server=os.environ["TAFL_TEST_SQLSERVER"], database="tempdb")
    table = project.add_table("Waits")
    project.add_step(table, "db.import", {"connection": "server", "mode": "query", "query": "WAITFOR DELAY '00:00:20'; SELECT 1 AS N"})
    path = project.save(tmp_path / "stop.tafl")
    before = signal.signal(signal.SIGINT, lambda *_: None)  # never a Ctrl+C for pytest itself
    try:
        threading.Timer(0.5, signal.raise_signal, (signal.SIGINT,)).start()
        started = time.perf_counter()
        assert cli.main(["run", str(path)]) == 130
        assert time.perf_counter() - started < 5
    finally:
        signal.signal(signal.SIGINT, before)
    output = capsys.readouterr()
    assert "  · 1. Import SQL query from server   (stopped)" in output.out and output.err.endswith("Error: Stopped\n")


@_SQLSERVER
def test_sqlserver_iso_8601_texts_for_date_columns_are_read_one_way_in_every_mode(registry):
    """ISO 8601 means the same in every language: such texts worked in 0.6.4 when SQL Server read
    them (Update and Upsert), mostly not in Add, whose driver reads only 2026-10-01 08:30:00. Now
    every mode reads them; an offset is left out for a column without one (its local time)."""
    ctx, name = _sqlserver()
    texts = ["2026-10-01T08:30", "20261001", "20261001 08:30:15", "2026-10-01T08:30:15Z", "2026-10-01T08:30:15+02:00"]
    expected = {
        "date": ["2026-10-01"] * 5,
        "datetime": ["2026-10-01 08:30:00.000", "2026-10-01 00:00:00.000", *["2026-10-01 08:30:15.000"] * 3],
        "datetime2(0)": ["2026-10-01 08:30:00", "2026-10-01 00:00:00", *["2026-10-01 08:30:15"] * 3],
        "time(0)": ["08:30:00", "00:00:00", *["08:30:15"] * 3],
        "datetimeoffset(0)": [None, None, None, "2026-10-01 08:30:15 +00:00", "2026-10-01 08:30:15 +02:00"],  # None: this PC's offset
    }
    data = pl.DataFrame({"K": range(1, 6), "V": texts})
    try:
        for kind, values in expected.items():
            stored_by = {}
            for mode in ("insert", "upsert"):
                with ctx.database("server") as db:
                    db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")
                    db.execute(f"CREATE TABLE {name} (K int, V {kind})")
                registry.actions["db.export"](data, {"connection": "server", "table": name, "mode": mode, "key_columns": ["K"]}, ctx)
                with ctx.database("server") as db:
                    stored_by[mode] = db.read(f"SELECT CONVERT(nvarchar(40), V, 121) AS V FROM {name} ORDER BY K")["V"].to_list()
            assert stored_by["insert"] == stored_by["upsert"], kind
            assert [v for v, e in zip(stored_by["insert"], values) if e is not None] == [e for e in values if e is not None], kind
    finally:
        with ctx.database("server") as db:
            db.execute(f"IF OBJECT_ID('{name}') IS NOT NULL DROP TABLE {name}")

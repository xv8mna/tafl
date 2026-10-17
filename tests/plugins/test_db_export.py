"""Exporting into a database table (all modes) and running SQL commands."""

import datetime as dt
import decimal
import os
import sqlite3
import time
import uuid

import polars as pl
import pytest

from tafl.api import ActionError
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
            assert ctx.messages[-1] == ("info", f"{name}: {message}"), mode
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
    this PC's time zone on its day (from the driver), in every mode; imported values (texts such
    as 2026-10-01T08:30:00+02:00) go back as they were."""
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

    def stored(table=name):
        with ctx.database("server") as db:
            return db.read(f"SELECT N, At FROM {table} ORDER BY N").rows()

    try:
        with ctx.database("server") as db:
            db.execute(f"CREATE TABLE {name} (N int, At datetimeoffset(3))")
            db.execute(f"CREATE TABLE {copy} (N int, At datetimeoffset(3))")
        for mode in ("insert", "upsert"):
            with ctx.database("server") as db:
                db.execute(f"DELETE FROM {name}")
            export(data, mode=mode, key_columns=["N"])
            assert stored() == [
                (1, "2026-07-01T06:30:00.500000+00:00"), (2, "2026-01-01T07:30:00+00:00"), (3, "2026-07-01T06:30:00+00:00"),
            ], mode  # fmt: skip
        export(berlin, mode="update", key_columns=["N"])
        assert stored() == [
            (1, "2026-07-01T08:30:00.500000+02:00"), (2, "2026-01-01T08:30:00+01:00"), (3, "2026-07-01T08:30:00+02:00"),
        ]  # fmt: skip
        export(data.drop("N"), mode="delete", key_columns=["At"])  # the same moments in another zone
        assert ctx.messages[-1] == ("info", f"{name}: deleted 3 rows")
        export(pl.DataFrame({"N": [4], "At": [naive]}), mode="upsert", key_columns=["N"])
        export(pl.DataFrame({"N": [5], "At": [naive]}))
        assert stored() == [(4, f"2026-07-01T08:30:00{local}"), (5, f"2026-07-01T08:30:00{local}")]
        # An import gives texts with the offset: they go back unchanged, by Add and by key
        imported = registry.actions["db.import"](None, {"connection": "server", "table": name}, ctx)
        assert imported["At"].to_list() == [f"2026-07-01T08:30:00{local}"] * 2
        export(imported, table=copy)
        export(imported, table=copy, mode="upsert", key_columns=["At", "N"])
        assert ctx.messages[-1] == ("info", f"{copy}: nothing changed")  # the keys found their rows
        export(imported.with_columns(N=pl.col("N") + 10), table=copy, mode="upsert", key_columns=["At", "N"])
        assert stored(copy) == [*stored(), *[(n + 10, at) for n, at in stored()]]
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

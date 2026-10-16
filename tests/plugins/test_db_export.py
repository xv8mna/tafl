"""Exporting into a database table (all modes) and running SQL commands."""

import datetime as dt
import os
import sqlite3
import time
import uuid

import polars as pl
import pytest

from etl_tool.api import ActionError
from etl_tool.api.testing import StubContext
from etl_tool.core import Engine, Project

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
    from etl_tool.api.database import Connector, Database

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
    finally:
        with ctx.database("server") as db:
            for table in (name, new):
                db.execute(f"IF OBJECT_ID('{table}') IS NOT NULL DROP TABLE {table}")

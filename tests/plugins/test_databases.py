import datetime as dt
import os
import sqlite3

import polars as pl
import pytest

from etl_tool.core import Engine, Project


@pytest.fixture
def project(tmp_path) -> Project:
    con = sqlite3.connect(tmp_path / "data.db")
    con.executescript(
        """
        CREATE TABLE orders (id INTEGER, customer TEXT, amount REAL, day TEXT);
        INSERT INTO orders VALUES (1, 'A', 10.5, '2025-01-01'), (2, 'B', NULL, '2025-01-02');
        CREATE TABLE messy (value);
        INSERT INTO messy VALUES (1), ('two'), (NULL);
        """
    )
    con.commit()
    con.close()
    project = Project(name="Db")
    project.path = tmp_path / "db.tafl"
    project.add_connection("db", "sqlite", file="data.db")
    return project


def import_table(project, registry, **params):
    table = project.add_table(f"T{len(project.tables) + 1}")
    project.add_step(table, "db.import", {"connection": "db", **params})
    return Engine(project, registry).evaluate(table.id)


def test_import_table_and_query(project, registry):
    result = import_table(project, registry, table="orders")
    assert result.df.to_dicts() == [
        {"id": 1, "customer": "A", "amount": 10.5, "day": "2025-01-01"},
        {"id": 2, "customer": "B", "amount": None, "day": "2025-01-02"},
    ]
    query = import_table(project, registry, mode="query", query="SELECT customer, id * 10 AS big FROM orders", detect_types=True)
    assert query.df.to_dicts() == [{"customer": "A", "big": 10}, {"customer": "B", "big": 20}]


def test_detect_types_turns_iso_text_into_dates(project, registry):
    result = import_table(project, registry, table="main.orders", detect_types=True)
    assert result.df["day"].to_list() == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]


def test_column_with_mixed_values_becomes_text(project, registry):
    assert import_table(project, registry, table="messy").df["value"].to_list() == ["1", "two", None]


def test_errors(project, registry):
    assert "no such table: nope" in import_table(project, registry, table="nope").first_error
    assert "connection 'other' is not defined" in import_table(project, registry, connection="other", table="x").first_error
    project.connections[0].settings["file"] = "missing.db"
    assert "SQLite file not found" in import_table(project, registry, table="orders").first_error


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver(registry, tmp_path):
    project = Project(name="SqlServer")
    project.add_connection("server", "sqlserver", server=os.environ["TAFL_TEST_SQLSERVER"], database="master")
    table = project.add_table("Databases")
    project.add_step(
        table,
        "db.import",
        {
            "connection": "server",
            "mode": "query",
            "query": "SELECT name, database_id, create_date, CAST(1.5 AS decimal(10,2)) AS dec, "
            "CAST('2025-01-02' AS date) AS d FROM sys.databases",
        },
    )
    result = Engine(project, registry).evaluate(table.id)
    assert result.ok, result.first_error
    assert "master" in result.df["name"].to_list()
    assert result.df.schema == {
        "name": pl.String,
        "database_id": pl.Int64,
        "create_date": pl.Datetime("us"),
        "dec": pl.Float64,
        "d": pl.Date,
    }
    project.tables[0].steps[0].params["query"] = "SELECT * FROM dbo.does_not_exist"
    assert Engine(project, registry).evaluate(table.id).first_error == "Invalid object name 'dbo.does_not_exist'."

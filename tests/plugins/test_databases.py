import datetime as dt
import decimal
import os
import sqlite3
import uuid

import polars as pl
import pytest

from tafl.api import ActionError, Context
from tafl.api.parameters import Fixed
from tafl.core import Engine, Parameter, Project


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


def test_an_import_never_changes_the_database(project, registry, tmp_path):
    """Imports run while editing (e.g. when someone else's project is opened): a query that
    changes data must not change it."""
    deleted = import_table(project, registry, mode="query", query="DELETE FROM orders RETURNING id")
    assert deleted.df["id"].to_list() == [1, 2]
    con = sqlite3.connect(tmp_path / "data.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM orders").fetchone() == (2,)
    finally:
        con.close()


def test_detect_types_turns_iso_text_into_dates(project, registry):
    result = import_table(project, registry, table="main.orders", detect_types=True)
    assert result.df["day"].to_list() == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]


def test_detect_types_says_why_long_numbers_stay_text(project, registry):
    query = "SELECT '98765432109876543210' AS id, '12' AS n"
    result = import_table(project, registry, mode="query", query=query, detect_types=True)
    assert result.df.schema == pl.Schema({"id": pl.String, "n": pl.Int64})
    assert result.steps[0].messages == [
        ("warning", "id stays text: its numbers are too long to be kept exactly, e.g. '98765432109876543210'")
    ]


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
    batch = "CREATE TABLE #t (n INT); INSERT INTO #t VALUES (1), (2); SELECT n * 10 AS ten FROM #t"
    project.tables[0].steps[0].params["query"] = batch  # without SET NOCOUNT ON: row counts come first
    assert Engine(project, registry).evaluate(table.id).df["ten"].to_list() == [10, 20]
    project.tables[0].steps[0].params["query"] = "SELECT 1 AS a INTO #u; SELECT CAST('x' AS int) AS b"
    assert "Conversion failed" in Engine(project, registry).evaluate(table.id).first_error  # not an empty table


def test_sqlserver_writes_parameters_as_its_own_literals(registry):
    literal = registry.connectors["sqlserver"].literal
    assert literal(dt.date(2026, 10, 15)) == "CAST('2026-10-15' AS date)"  # whatever the server's language
    assert literal(dt.datetime(2026, 10, 15, 8, 30)) == "CAST('2026-10-15T08:30:00.000000' AS datetime2)"
    assert literal("Weiß O'Brien") == "N'Weiß O''Brien'"
    assert [literal(v) for v in (5, 2.5, True, None, -5, -2.5)] == ["5", "2.5", "1", "NULL", "-5", "-2.5"]
    assert registry.connectors["sqlite"].literal(dt.date(2026, 10, 15)) == "'2026-10-15'"
    fill = registry.connectors["sqlserver"].fill_sql
    values = {"Day": Fixed(dt.date(2026, 10, 1), "2026-10-01"), "Rate": Fixed(2.5, "2.50")}
    assert fill("d = {Day} AND r = {Rate} AND t = N'{Day} {Rate}'", values) == (
        "d = CAST('2026-10-01' AS date) AND r = 2.5 AND t = N'2026-10-01 2.50'"
    )


_EXEC = {
    "EXEC dbo.P {Day}": True,
    "exec dbo.P @a = 1, @Day = {Day}": True,
    "EXEC @rc = dbo.P @Day = {Day}": True,
    "INSERT INTO t EXEC dbo.P {Day}": True,
    "EXECUTE sp_executesql N'SELECT @Day', N'@Day date', @Day = {Day}": True,
    "EXEC [dbo].[P] -5, 'x', N'y', 0x1F, 1.5e3, DEFAULT, @v OUTPUT, @w OUT, {N}, {Day}": True,
    "EXEC db..P /* a comment */ {N},\n-- another one\n{Day}": True,
    'EXEC "dbo"."P" @a = [x], @Day = {Day} WITH RECOMPILE': True,
    "EXEC ('SELECT ?', {Day}) AT Server": True,
    "EXECUTE (N'SELECT ?, ?, ?' + @more, @v OUTPUT, {N}, {Day}) AT Server": True,
    "EXEC Out {Day}": True,  # a procedure named Out
    "EXEC @rc = Output {Day}": True,
    "EXEC dbo.P;1 {Day}": True,  # numbered procedures
    "EXEC dbo.P; 1 @Day = {Day}": True,
    "SELECT a, {Day}": False,
    "SELECT {N}, {Day}": False,
    "SELECT * FROM t WHERE x = {Day}": False,
    "DECLARE @d date = {Day}": False,
    "SET @d = {Day}": False,
    "EXEC('SELECT ' + {Day})": False,
    "EXEC (N'SELECT ' + {Day}, 1) AT Server": False,  # a part of the command
    "EXEC (N'SELECT 1') {Day}": False,
    "SELECT * FROM t WHERE x IN ('a', {Day})": False,
    "SELECT COALESCE(@a, {Day})": False,
    "EXEC dbo.P 1\nSELECT {Day}": False,
    "EXEC dbo.P 1; SELECT a, {Day}": False,
    "EXEC dbo.P @a = 1 + {Day}": False,
    "EXEC {Day}": False,
    "{Day}": False,
}


@pytest.mark.parametrize(("sql", "argument"), _EXEC.items())
def test_sqlserver_writes_the_arguments_of_exec_as_constants(registry, sql, argument):
    """SQL Server takes only constants and variables as the arguments of EXEC: there a date is
    '20261001' (read the same in every language setting), elsewhere a typed CAST."""
    fill = registry.connectors["sqlserver"].fill_sql
    filled = fill(sql, {"Day": dt.date(2026, 10, 1), "N": -5})
    written = "'20261001'" if argument else "CAST('2026-10-01' AS date)"
    assert filled == sql.replace("{Day}", written).replace("{N}", "-5")


def test_sqlserver_exec_arguments_by_type(registry):
    fill = registry.connectors["sqlserver"].fill_sql
    values = {
        "Day": dt.date(2026, 10, 1),
        "Stamp": dt.datetime(2026, 10, 1, 8, 30),
        "Milli": dt.datetime(2026, 10, 1, 8, 30, 15, 120000),
        "Micro": dt.datetime(2026, 10, 1, 8, 30, 15, 123456),
        "Name": "O'Brien",
        "N": -5,
        "Rate": 2.5,
        "Yes": True,
        "None": None,
    }
    sql = "EXEC dbo.P {Day}, {Stamp}, {Milli}, {Micro}, {Name}, {N}, {Rate}, {Yes}, {None}; SELECT {Stamp}, {N}"
    assert fill(sql, values) == (
        "EXEC dbo.P '20261001', '2026-10-01T08:30:00.000', '2026-10-01T08:30:15.120', '2026-10-01T08:30:15.123456', "
        "N'O''Brien', -5, 2.5, 1, NULL; SELECT CAST('2026-10-01T08:30:00.000000' AS datetime2), -5"
    )


def test_sqlserver_writes_a_negative_number_in_parentheses_after_times_and_divide(registry):
    """T-SQL ranks a minus sign below * / % and ~: 200 * -10 / 100 would be 200 * -(10 / 100) = 0."""
    fill = registry.connectors["sqlserver"].fill_sql
    sql = "SELECT 200 * {X} / 100, 10 / {X}, 7 % {X}, ~{X}, 2 */* c */ {X}, 2 *-- c\n{X}, 5 - {X}, {X} * 2, -{X}"
    assert fill(sql, {"X": -10}) == (
        "SELECT 200 * (-10) / 100, 10 / (-10), 7 % (-10), ~(-10), 2 */* c */ (-10), 2 *-- c\n(-10), 5 - -10, -10 * 2, - -10"
    )
    assert fill(sql, {"X": 10}) == sql.replace("{X}", "10")
    assert fill("EXEC dbo.P {X}; SELECT 2 * {X}", {"X": -10}) == "EXEC dbo.P -10; SELECT 2 * (-10)"


def test_each_database_reads_the_sql_its_own_way(registry):
    sql = "-- a\rb'c' /* d /* e */ f */"
    assert registry.connectors["sqlserver"].sql_spans(sql) == [(0, 4, "comment"), (6, 9, "text"), (10, 27, "comment")]
    assert registry.connectors["sqlite"].sql_spans(sql) == [(0, 27, "comment")]
    assert registry.connectors["sqlite"].sql_spans("/* d /* e */ 'f' */") == [(0, 12, "comment"), (13, 16, "text")]


def test_sqlite_takes_a_negative_number_where_it_needs_a_constant(registry):
    fill = registry.connectors["sqlite"].fill_sql
    con = sqlite3.connect(":memory:")
    try:
        con.execute(fill("PRAGMA cache_size = {Neg}", {"Neg": -500}))
        assert con.execute("PRAGMA cache_size").fetchone() == (-500,)
        assert con.execute(fill("SELECT 5-{Neg} AS a", {"Neg": -5})).fetchall() == [(10,)]
        # In SQLite only a line feed ends a -- comment: after a carriage return {X} is still in it
        cursor = con.execute(fill("SELECT 1 AS a -- note\r{X}\n, 2 AS b", {"X": "\n, 3 AS injected --"}))
        assert [d[0] for d in cursor.description] == ["a", "b"]
    finally:
        con.close()


def test_a_negative_value_after_a_minus(project, registry, tmp_path):
    """5-{Offset} with Offset = -5 must not become 5--5: the rest of the line would be a comment."""
    con = sqlite3.connect(tmp_path / "data.db")
    con.executescript("CREATE TABLE t (Version INTEGER, Region TEXT); INSERT INTO t VALUES (10, 'EU'), (10, 'US'), (5, 'EU'), (5, 'US');")
    con.close()
    project.parameters = [Parameter("Offset", "-5")]
    table = project.add_table("Delete")
    project.add_step(table, "db.import", {"connection": "db", "mode": "query", "query": "SELECT 5-{Offset} AS v, -{Offset} AS n"})
    project.add_step(table, "db.command", {"connection": "db", "sql": "DELETE FROM t WHERE Version = 5-{Offset} AND Region = 'EU'"})
    result = Engine(project, registry).run([table.id])[table.id]
    assert result.ok, result.first_error
    assert result.df.to_dicts() == [{"v": 10, "n": 5}]
    con = sqlite3.connect(tmp_path / "data.db")
    try:
        assert sorted(con.execute("SELECT * FROM t").fetchall()) == [(5, "EU"), (5, "US"), (10, "US")]
    finally:
        con.close()


def test_sqlite_values_never_change_the_sql(project, registry, tmp_path):
    """SQLite's own reading: /* */ comments don't nest, and a quote inside a variable such as
    $a(') starts no text. Read otherwise, these values would add a column or delete all rows."""
    fill = registry.connectors["sqlite"].fill_sql
    con = sqlite3.connect(":memory:")
    try:
        sql = fill("SELECT /* a /* b */ ' */ , {X} -- ' AS q", {"X": " AS injected --"})
        assert [d[0] for d in con.execute(sql).description] == ["q"]
    finally:
        con.close()
    con = sqlite3.connect(tmp_path / "data.db")
    con.executescript("CREATE TABLE t (x INTEGER); INSERT INTO t VALUES (1), (2);")
    con.close()
    project.parameters = [Parameter("X", "1 OR 1 = 1")]
    table = project.add_table("Delete")
    project.add_step(table, "db.import", {"connection": "db", "table": "orders"})
    project.add_step(table, "db.command", {"connection": "db", "sql": "DELETE FROM t WHERE x = $a(') OR x = {X} -- ')"})
    result = Engine(project, registry).run([table.id])[table.id]
    assert result.ok, result.first_error
    con = sqlite3.connect(tmp_path / "data.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM t").fetchone() == (2,)
    finally:
        con.close()


def test_sqlserver_explains_the_drivers_own_syntax_errors(registry):
    """The ODBC driver reads {…} as an escape before the server sees the SQL; its own error
    says nothing about that."""
    error_message = registry.connectors["sqlserver"].error_message
    driver = "[42000] [Microsoft][ODBC Driver 17 for SQL Server]Syntax error, permission violation, or other nonspecific error"
    assert error_message(Exception("42000", f"{driver} (0) (SQLExecDirectW)")) == (
        "The ODBC driver couldn't read the SQL (Syntax error, permission violation, or other nonspecific error). "
        "It takes {…} outside texts and comments for an ODBC escape such as {fn …}; for the driver a comment "
        "ends at its first */, also in a comment inside another comment."
    )
    server = "[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Incorrect syntax near ','. (102) (SQLExecDirectW)"
    assert error_message(Exception("42000", server)) == "Incorrect syntax near ','."


class _Recorder:
    """A connection that keeps the batches it is given."""

    def __init__(self):
        self.batches = []
        self.rowcount = -1

    def cursor(self):
        return self

    def execute(self, sql):
        self.batches.append(sql)
        self.rowcount = 1

    def nextset(self):
        return False

    def close(self):
        pass


def test_sqlserver_batches_end_only_at_go_lines_outside_texts_and_comments(registry):
    connector = registry.connectors["sqlserver"]
    values = {"Text": "a\nGO\nb", "Note": "*/\nGO\n/*"}
    sql = "INSERT INTO #t VALUES ({Text})\ngo ;\r\nSELECT 1 /* x /* y */\nGO\n */\n  GO  \nSELECT '{Note}', [\nGO\n] -- GO\nGO"
    conn = _Recorder()
    assert connector.run_script(conn, connector.fill_sql(sql, values)) == 3
    assert [batch.strip() for batch in conn.batches] == [
        "INSERT INTO #t VALUES (N'a\nGO\nb')",
        "SELECT 1 /* x /* y */\nGO\n */",
        "SELECT '*/\nGO\n/*', [\nGO\n] -- GO",
    ]
    conn = _Recorder()  # a carriage return ends a -- comment; $a$ is no quote in T-SQL
    connector.run_script(conn, "SELECT 1 -- x\r'\nGO\n'\nGO\nDECLARE @$a$ int; SELECT '$a$'\nGO\nSELECT 2 -- $a$")
    assert [batch.strip() for batch in conn.batches] == ["SELECT 1 -- x\r'\nGO\n'", "DECLARE @$a$ int; SELECT '$a$'", "SELECT 2 -- $a$"]


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_reads_parameters_back(registry):
    project = Project(name="SqlServer", parameters=[
        Parameter("Day", "2026-10-15"),
        Parameter("Stamp", "2026-10-15 08:30:15.123456"),
        Parameter("Name", "Weiß O'Brien ✓"),
        Parameter("Count", "3000000000"),
        Parameter("Rate", "2.5"),
    ])  # fmt: skip
    project.add_connection("server", "sqlserver", server=os.environ["TAFL_TEST_SQLSERVER"], database="tempdb")
    table = project.add_table("Values")
    query = "SELECT {Day} AS d, {Stamp} AS s, {Name} AS n, {Count} AS c, {Rate} AS r, '{Name}' AS t"
    project.add_step(table, "db.import", {"connection": "server", "mode": "query", "query": query})
    result = Engine(project, registry).evaluate(table.id)
    assert result.ok, result.first_error
    assert result.df.to_dicts() == [{
        "d": dt.date(2026, 10, 15),
        "s": dt.datetime(2026, 10, 15, 8, 30, 15, 123456),
        "n": "Weiß O'Brien ✓",
        "c": 3_000_000_000,
        "r": 2.5,
        "t": "Weiß O'Brien ?",  # inside '...' the text is written as it is: not an N'' text
    }]  # fmt: skip


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_values_never_change_the_sql(registry):
    project = Project(name="SqlServer", parameters=[
        Parameter("Offset", "-5"),
        Parameter("Text", "a\nGO\nb"),
        Parameter("Note", "x */ , 2 AS injected /* y"),
    ])  # fmt: skip
    project.add_connection("server", "sqlserver", server=os.environ["TAFL_TEST_SQLSERVER"], database="tempdb")
    table = project.add_table("Values")
    query = "SELECT 5-{Offset} AS v, -{Offset} AS n /* a /* b */ c */, {Text} AS t, {Note} AS note"
    project.add_step(table, "db.import", {"connection": "server", "mode": "query", "query": query})
    script = "CREATE TABLE #g (t nvarchar(100));\nINSERT INTO #g VALUES ({Text});\nGO\nINSERT INTO #g SELECT t FROM #g WHERE t = {Text};"
    project.add_step(table, "db.command", {"connection": "server", "sql": script})
    result = Engine(project, registry).run([table.id])[table.id]
    assert result.ok, result.first_error
    assert result.df.to_dicts() == [{"v": 10, "n": 5, "t": "a\nGO\nb", "note": "x */ , 2 AS injected /* y"}]
    assert result.steps[1].messages == [("info", "Done: 2 rows changed")]  # one batch would have failed


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_reads_texts_and_comments_as_the_server_does(registry):
    """Nested comments, $ in names and a carriage return that ends a -- comment: read another
    way, the value would add a column."""
    project = Project(name="SqlServer", parameters=[Parameter("X", " 1 AS injected --"), Parameter("Day", "2026-10-01")])
    project.add_connection("server", "sqlserver", server=os.environ["TAFL_TEST_SQLSERVER"], database="tempdb")
    table = project.add_table("Values")
    project.add_step(table, "db.import", {"connection": "server", "mode": "query", "query": ""})
    queries = {
        "SELECT 1 AS w /* a /* b */ ' */ , {X} AS q -- '": ["w", "q"],
        "DECLARE @$a$ nvarchar(20) = ' @$a$ '; SELECT @$a$ AS w, {X} AS q -- '": ["w", "q"],
        "SELECT 1 AS w -- note\r, ' \n, {X} AS q -- '": ["w", "Column 2"],
    }
    for query, columns in queries.items():
        table.steps[0].params["query"] = query
        result = Engine(project, registry).evaluate(table.id)
        assert result.ok, result.first_error
        assert result.df.columns == columns
    table.steps[0].params["query"] = "SELECT 1 AS w /* a /* b */ {Day} */"  # the driver ends the comment at */
    assert "ODBC escape" in Engine(project, registry).evaluate(table.id).first_error


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_exec_arguments_in_every_language(registry):
    """The arguments of EXEC are constants that every language setting reads the same; a CAST
    there would be a syntax error. The control column shows that the setting was in force."""
    connector = registry.connectors["sqlserver"]
    values = {
        "Day": dt.date(2026, 10, 1),
        "Milli": dt.datetime(2026, 10, 1, 8, 30, 15, 120000),
        "Micro": dt.datetime(2026, 10, 1, 8, 30, 15, 123456),
        "N": -5,
        "Name": "Weiß O'Brien",
    }
    columns = "d date, dt datetime, sd smalldatetime, t datetime2, s datetime, n int, x nvarchar(50)"
    selected = "SELECT @d AS d, @dt AS dt, @sd AS sd, @t AS t, @s AS s, @n AS n, @x AS x, CAST('2026-10-02' AS datetime) AS control"
    arguments = "{Day}, {Day}, {Day}, {Micro}, {Milli}, {N}, {Name}"
    named = "@d = {Day}, @dt = {Day}, @sd = {Day}, @t = {Micro}, @s = {Milli}, @n = {N}, @x = {Name}"
    calls = [
        f"EXEC #p {arguments}",
        f"EXECUTE #p {named}",
        "DECLARE @rc int; EXEC @rc = #p {Day}, {Day}, {Day}, @t = {Micro}, @s = {Milli}, @n = {N}, @x = {Name}",
        f"DECLARE @r TABLE ({columns}, control datetime); INSERT INTO @r EXEC #p {arguments}; SELECT * FROM @r",
        f"EXEC sp_executesql N'{selected.replace(chr(39), chr(39) * 2)}', N'@{columns.replace(', ', ', @')}', {named}",
    ]
    expected = (
        dt.date(2026, 10, 1),
        dt.datetime(2026, 10, 1),
        dt.datetime(2026, 10, 1),
        dt.datetime(2026, 10, 1, 8, 30, 15, 123456),
        dt.datetime(2026, 10, 1, 8, 30, 15, 120000),
        -5,
        "Weiß O'Brien",
    )
    settings = {
        "SET LANGUAGE us_english;": dt.datetime(2026, 10, 2),
        "SET LANGUAGE Deutsch;": dt.datetime(2026, 2, 10),
        "SET LANGUAGE british;": dt.datetime(2026, 2, 10),
        "SET LANGUAGE us_english; SET DATEFORMAT dmy;": dt.datetime(2026, 2, 10),
    }
    with connector.open({"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"}, Context()) as db:
        db.execute(f"CREATE PROCEDURE #p @{columns.replace(', ', ', @')} AS {selected}")
        for setting, control in settings.items():
            for call in calls:
                row = db.read(connector.fill_sql(f"{setting} {call}", values)).row(0)
                assert row == (*expected, control), (setting, call)
            # Elsewhere a typed CAST: the columns are a date and a datetime2
            typed = db.read(connector.fill_sql(f"{setting} SELECT {{Day}} AS d, {{Micro}} AS t WHERE {{Day}} = '20261001'", values))
            assert typed.schema == {"d": pl.Date, "t": pl.Datetime("us")}
            assert typed.row(0) == (expected[0], expected[3])
            # A value for dynamic SQL goes in as a parameter of it
            dynamic = "DECLARE @Day date = {Day}; EXEC sp_executesql N'SELECT @Day AS d', N'@Day date', @Day = @Day"
            assert db.read(connector.fill_sql(f"{setting} {dynamic}", values)).row(0) == (expected[0],)
        # A text parameter gets the constant; inside quotes the date as it is written everywhere else
        db.execute("CREATE PROCEDURE #label @Label nvarchar(30) AS SELECT @Label AS label")
        assert db.read(connector.fill_sql("EXEC #label @Label = {Day}", values)).row(0) == ("20261001",)
        assert db.read(connector.fill_sql("EXEC #label @Label = '{Day}'", values)).row(0) == ("2026-10-01",)
        # The arguments of EXEC (…) AT a linked server: a CAST there would be a syntax error
        with pytest.raises(ActionError, match="tafl_no_such_server"):
            db.read(connector.fill_sql("EXEC ('SELECT ?, ?', {Day}, {N}) AT tafl_no_such_server", values))
        db.rollback()


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_arithmetic_with_a_negative_value(registry):
    """The same results as with the value given as a parameter of the query."""
    connector = registry.connectors["sqlserver"]
    terms = ["200 * {X} / 100", "200 + 200 * {X} / 100", "10 / {X} * 2", "100 / {X} / 2", "7 % {X} * 2", "~{X} * 2", "5-{X} * 2"]
    columns = ", ".join(f"{term} AS c{i}" for i, term in enumerate(terms))
    with connector.open({"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"}, Context()) as db:
        for x in (-10, -5, -1, 3):
            bound = db.read("SELECT " + columns.replace("{X}", "?"), [x] * len(terms)).row(0)
            assert db.read(connector.fill_sql("SELECT " + columns, {"X": x})).row(0) == bound, x
        assert db.read(connector.fill_sql("SELECT 200 * {X} / 100 AS a", {"X": -10})).row(0) == (-20,)


def test_sqlite_import_attaches_no_database_while_the_project_is_looked_at(project, registry, tmp_path, monkeypatch):
    """An ATTACH creates a missing file: while a project is only looked at, imports change nothing."""
    monkeypatch.chdir(tmp_path)
    made = tmp_path / "made.db"
    table = project.add_table("Attach")
    project.add_step(table, "db.import", {"connection": "db", "mode": "query", "query": f"ATTACH DATABASE '{made}' AS o"})
    result = Engine(project, registry).evaluate(table.id)
    assert result.first_error == "An import can ATTACH a database only when the project is run (it could create a file)"
    assert not made.exists()
    assert Engine(project, registry).run([table.id])[table.id].ok and made.exists()


def test_a_value_the_driver_cant_read_names_its_column():
    """A driver's output converter gives an ActionError for a value it can't read."""
    from tafl.api import make_series

    with pytest.raises(ActionError, match=r"^Convert Value first, e.g. CAST\(\[Value\] AS text\)$"):
        make_series("Value", [None, 1, ActionError("Convert {column} first, e.g. CAST([{column}] AS text)")])


def test_decimals_keep_their_digits():
    """Different IDs never become equal: whole numbers become Int64 when they fit, decimals
    Float64 when it keeps them as they are; otherwise they stay exact (Decimal)."""
    from tafl.api import make_series

    D = decimal.Decimal
    ids = make_series("Id", [D("1234567890123456789"), D("1234567890123456788"), None], pl.Decimal(19, 0))
    assert ids.dtype == pl.Int64 and ids.to_list() == [1234567890123456789, 1234567890123456788, None]
    longer = make_series("Id", [D("12345678901234567891"), D("12345678901234567892")], pl.Decimal(20, 0))
    assert longer.dtype == pl.Decimal(20, 0) and longer.to_list() == [D("12345678901234567891"), D("12345678901234567892")]
    amounts = make_series("Amount", [D("12345678901234.5678"), D("0.1000")], pl.Decimal(18, 4))
    assert amounts.dtype == pl.Decimal(18, 4) and amounts.to_list() == [D("12345678901234.5678"), D("0.1000")]
    assert make_series("Rate", [D("1.10"), D("123.45000000000000000000")], pl.Decimal(38, 20)).to_list() == [1.1, 123.45]
    assert make_series("Rate", [D("1.5"), None]).dtype == pl.Float64  # a driver that gives no type
    assert make_series("Rate", [D("0.12345678901234567"), None]).dtype == pl.Decimal(38, 17)
    assert make_series("N", [D("5")], pl.Int64).to_list() == [5]
    huge = make_series("Huge", [D("1" * 40), D("1.5")])  # more than the 38 digits of a Decimal
    assert huge.to_list() == ["1" * 40, "1.5"]


def test_sqlserver_fits_values_to_their_columns(registry):
    """As the driver does with date-times: times cut to their column's digits, date-times for a
    date column their date, offsets for datetimeoffset in the driver's form, decimals rounded."""
    fit = registry.connectors["sqlserver"].fit_frame
    df = pl.DataFrame({
        "Time": [dt.time(8, 0, 1, 567891)],
        "Second": [dt.datetime(2026, 10, 2, 8, 30, 1, 999999)],
        "Day": [dt.datetime(2026, 10, 2, 8, 30)],
        "Zoned": pl.Series([dt.datetime(2026, 7, 1, 8, 30, 0, 123456)]).dt.replace_time_zone("Europe/Berlin"),
        "Text": ["2026-10-01T08:30:00.1234567+02:00"],
        "Utc": ["2026-10-01T08:30:00.5Z"],
        "Price": pl.Series([decimal.Decimal("-2.675")], dtype=pl.Decimal(38, 8)),
        "Rate": pl.Series([decimal.Decimal("1.5")], dtype=pl.Decimal(38, 8)),
        "Other": ["x"],
    })  # fmt: skip
    types = {
        "Time": "time(3)", "Second": "datetime2(0)", "Day": "date", "Zoned": "datetimeoffset(2)",
        "Text": "datetimeoffset(3)", "Utc": "datetimeoffset(0)", "Price": "decimal(10,2)", "Rate": "float",
    }  # fmt: skip
    assert fit(df, types).row(0) == (
        dt.time(8, 0, 1, 567000), dt.datetime(2026, 10, 2, 8, 30, 1), dt.date(2026, 10, 2), "2026-07-01 08:30:00.12 +02:00",
        "2026-10-01 08:30:00.123 +02:00", "2026-10-01 08:30:00 +00:00", decimal.Decimal("-2.68"), 1.5, "x",
    )  # fmt: skip
    assert fit(df, {}).equals(df)


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_import_keeps_the_digits_of_numbers(registry):
    """numeric(19, 0) and longer IDs: whole numbers while they fit, else Decimal, as amounts
    with more digits than a Float64 keeps. They go back into the database as they were."""
    settings = {"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"}
    name = f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"
    columns = "n19 numeric(19,0), n20 numeric(20,0), few numeric(20,0), d184 decimal(18,4), d102 decimal(10,2), m money, n3820 numeric(38,20)"
    project = Project(name="SqlServer")
    project.add_connection("server", "sqlserver", **settings)
    table = project.add_table("Numbers")
    project.add_step(table, "db.import", {"connection": "server", "table": name})
    project.add_step(table, "db.export", {"connection": "server", "table": f"{name}_copy"})
    with registry.connectors["sqlserver"].open(settings, Context()) as db:
        db.execute(f"CREATE TABLE {name} ({columns})")
        db.execute(f"CREATE TABLE {name}_copy ({columns})")
        db.execute(
            f"INSERT INTO {name} VALUES (1234567890123456789, 12345678901234567891, 5, 12345678901234.5678, 1.5, "
            "922337203685477.5807, 1.23456789012345678901), (1234567890123456788, 12345678901234567892, 6, 0.1, 2.25, 1, 0.5)"
        )
    try:
        result = Engine(project, registry).evaluate(table.id)
        assert result.ok, result.first_error
        assert result.df.schema == {
            "n19": pl.Int64, "n20": pl.Decimal(20, 0), "few": pl.Int64, "d184": pl.Decimal(18, 4), "d102": pl.Float64,
            "m": pl.Decimal(19, 4), "n3820": pl.Decimal(38, 20),
        }  # fmt: skip
        assert result.df["n19"].n_unique() == 2 and result.df["n20"].n_unique() == 2
        assert result.df["n20"].to_list() == [decimal.Decimal("12345678901234567891"), decimal.Decimal("12345678901234567892")]
        assert result.df["d184"].to_list() == [decimal.Decimal("12345678901234.5678"), decimal.Decimal("0.1000")]
        results = Engine(project, registry).run([table.id])
        assert results[table.id].ok, results[table.id].first_error
        with registry.connectors["sqlserver"].open(settings, Context()) as db:
            values = "SELECT CONCAT_WS(';', n19, n20, few, d184, d102, m, n3820) AS r FROM {} ORDER BY few"
            assert db.read(values.format(f"{name}_copy")).rows() == db.read(values.format(name)).rows()
    finally:
        with registry.connectors["sqlserver"].open(settings, Context()) as db:
            for t in (name, f"{name}_copy"):
                db.execute(f"IF OBJECT_ID('{t}') IS NOT NULL DROP TABLE {t}")


def test_text_sizes_count_utf16_code_units():
    """SQL Server counts an emoji as two characters: NVARCHAR(50) can't hold 50 of them."""
    from tafl.api import text_length

    assert text_length(pl.Series(["a" * 50, None])) == 50
    assert text_length(pl.Series(["😀" * 25 + "a"])) == 100


_SERVER = "[23000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]"


def test_sqlserver_messages_keep_their_values_and_say_things_once(registry):
    """The driver's records: without the prefixes and error numbers, the duplicate key value kept,
    each message once (the driver repeats some), and the same error for more rows counted."""
    error_message = registry.connectors["sqlserver"].error_message
    duplicate = (
        f"{_SERVER}Violation of PRIMARY KEY constraint 'PK__t__C3907C74'. Cannot insert duplicate key in object "
        "'dbo.t'. The duplicate key value is ({value}). (2627)"
    )
    terminated = f"{_SERVER}The statement has been terminated. (3621)"
    records = [duplicate.format(value=1) + " (SQLExecute)", terminated, duplicate.format(value=2), terminated]
    assert error_message(Exception("23000", "; ".join(records))) == (
        "Violation of PRIMARY KEY constraint 'PK__t__C3907C74'. Cannot insert duplicate key in object 'dbo.t'. "
        "The duplicate key value is (1). 1 more like it."
    )
    nulls = (
        f"{_SERVER}Cannot insert the value NULL into column 'Txt', table 'tempdb.dbo.t'; column does not allow nulls. "
        f"INSERT fails. (515) (SQLExecDirectW); {terminated}"
    )
    assert error_message(Exception("23000", nulls)) == (
        "Cannot insert the value NULL into column 'Txt', table 'tempdb.dbo.t'; column does not allow nulls. INSERT fails."
    )
    login = (
        "[28000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Login failed for user 'PC\\me'. (18456) "
        "(SQLDriverConnect); [28000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Cannot open database "
        '"x" requested by the login. The login failed. (4060)'
    )
    twice = f"{login}; {login.replace(' (SQLDriverConnect)', '')}"
    assert error_message(Exception("28000", twice)) == (
        "Login failed for user 'PC\\me'. Cannot open database \"x\" requested by the login. The login failed."
    )
    driver = "[08001] [Microsoft][ODBC Driver 17 for SQL Server]"
    server = (
        f"{driver}SQL Server Network Interfaces: Fehler beim Suchen des angegebenen Servers/der angegebenen Instanz "
        f"[xFFFFFFFF].  (-1) (SQLDriverConnect); {driver}Anmeldungstimeout abgelaufen (0); {driver}Netzwerkbezogener "
        "oder instanzspezifischer Fehler beim Herstellen einer Verbindung mit SQL Server. (-1)"
    )
    assert error_message(Exception("08001", server)) == (
        "SQL Server Network Interfaces: Fehler beim Suchen des angegebenen Servers/der angegebenen Instanz [xFFFFFFFF]. "
        "Anmeldungstimeout abgelaufen. Netzwerkbezogener oder instanzspezifischer Fehler beim Herstellen einer "
        "Verbindung mit SQL Server."
    )
    raised = "[42000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]check (1) failed; really (50000) (SQLExecDirectW)"
    assert error_message(Exception("42000", raised)) == "check (1) failed; really"


def test_sqlserver_uses_windows_own_driver_only_when_chosen(registry, monkeypatch):
    """The driver "SQL Server" that comes with Windows reads dates and times as text."""
    pyodbc = pytest.importorskip("pyodbc")
    connector = registry.connectors["sqlserver"]
    used = []

    class Connection:
        def add_output_converter(self, sql_type, convert):
            pass

    def connect(text, **options):
        used.append(text)
        return Connection()

    def open_with(drivers, driver=""):
        monkeypatch.setattr(pyodbc, "drivers", lambda: drivers)
        connector.open({"server": ".", "database": "db", "driver": driver}, Context())
        return used[-1].split(";")[0]

    monkeypatch.setattr(pyodbc, "connect", connect)
    windows = ["SQL Server", "Microsoft Access Driver (*.mdb, *.accdb)"]
    with pytest.raises(ActionError) as error:
        open_with(windows)
    assert str(error.value) == (
        "No suitable SQL Server ODBC driver is installed: install 'ODBC Driver 18 for SQL Server' (or 17) from "
        "Microsoft. The driver 'SQL Server' that comes with Windows is too old: it reads dates and times as text "
        "(to use it anyway, enter it as the connection's ODBC driver)."
    )
    with pytest.raises(ActionError, match=r"^No suitable SQL Server ODBC driver is installed: install .* Microsoft\.$"):
        open_with([])
    assert open_with(windows, "SQL Server") == "DRIVER={SQL Server}"
    assert open_with([*windows, "ODBC Driver 17 for SQL Server"]) == "DRIVER={ODBC Driver 17 for SQL Server}"
    with pytest.raises(ActionError) as error:
        open_with(windows, "ODBC Driver 99 for SQL Server")
    assert str(error.value) == (
        "The ODBC driver 'ODBC Driver 99 for SQL Server' is not installed. Installed for SQL Server: 'SQL Server'"
    )


def test_sqlserver_go_with_a_repeat_count_and_a_comment(registry):
    """As in SSMS: GO 3 runs the batch before it three times, and a -- comment may follow GO."""
    connector = registry.connectors["sqlserver"]
    conn = _Recorder()
    sql = "INSERT INTO #t VALUES (1)\nGO 3\nSELECT 2\ngo -- the next one\r\nSELECT 3 -- GO 5\nGO 2 -- twice\nSELECT 4"
    assert connector.run_script(conn, sql) == 7
    assert [batch.strip() for batch in conn.batches] == [
        *["INSERT INTO #t VALUES (1)"] * 3, "SELECT 2", *["SELECT 3 -- GO 5"] * 2, "SELECT 4",
    ]  # fmt: skip


@pytest.mark.skipif(not os.environ.get("TAFL_TEST_SQLSERVER"), reason="set TAFL_TEST_SQLSERVER (e.g. .\\SQLEXPRESS)")
def test_sqlserver_reads_sql_variant_and_clr_columns(registry):
    """pyodbc can't read them: a whole table gets them as text, a query stops with a hint."""
    connector = registry.connectors["sqlserver"]
    settings = {"server": os.environ["TAFL_TEST_SQLSERVER"], "database": "tempdb"}
    name = f"dbo.tafl_test_{uuid.uuid4().hex[:8]}"
    values = {
        "CAST(5 AS int)": "5",
        "CAST(3000000000 AS bigint)": "3000000000",
        "CAST(1.23456789012 AS float)": "1.23456789012",
        "CAST(12.3456 AS money)": "12.3456",
        "CAST(12.345 AS decimal(10, 3))": "12.345",
        "N'Łódź'": "Łódź",
        "CAST('2026-10-15' AS date)": "2026-10-15",
        "CAST('2026-10-15T08:30:15.123' AS datetime)": "2026-10-15 08:30:15.123",
        "CAST('2026-10-15 08:30:15.1234567' AS datetime2)": "2026-10-15 08:30:15.1234567",
        "CAST('08:30:15.1234567' AS time)": "08:30:15.1234567",
        "CAST(0x01FF AS varbinary(10))": "0x01FF",
        "CAST(1 AS bit)": "1",
        "N''": "",
        "NULL": None,
    }
    with connector.open(settings, Context()) as db:
        db.execute(f"CREATE TABLE {name} (n int, v sql_variant, g geography, h hierarchyid)")
        for n, value in enumerate(values):
            db.execute(f"INSERT INTO {name} (n, v) VALUES ({n}, {value})")
        db.execute(f"UPDATE {name} SET g = geography::Point(47.4, 8.5, 4326), h = hierarchyid::Parse('/1/2/') WHERE n = 0")
    try:
        project = Project(name="SqlServer")
        project.add_connection("server", "sqlserver", **settings)
        table = project.add_table("Variant")
        project.add_step(table, "db.import", {"connection": "server", "table": name})
        result = Engine(project, registry).evaluate(table.id)
        assert result.ok, result.first_error
        rows = result.df.sort("n")
        assert rows.schema == {"n": pl.Int64, "v": pl.String, "g": pl.String, "h": pl.String}
        assert rows["v"].to_list() == list(values.values())
        assert rows.row(0) == (0, "5", "POINT (8.5 47.4)", "/1/2/")
        queries = {
            f"SELECT n, v FROM {name}": "The column v is a sql_variant, which can't be read as it is: convert it to text "
            "in the query, e.g. CAST([v] AS nvarchar(4000)) AS [v]. Importing the whole table converts it by itself.",
            f"SELECT h, n FROM {name}": "The column h has a type such as geography or hierarchyid, which can't be read "
            "as it is: convert it to text in the query, e.g. CAST([h] AS nvarchar(max)) AS [h]. Importing the whole "
            "table converts it by itself.",
        }
        for query, message in queries.items():
            table.steps[0].params.update(mode="query", query=query)
            assert Engine(project, registry).evaluate(table.id).first_error == message
        table.steps[0].params["query"] = f"SELECT CAST(v AS nvarchar(4000)) AS v FROM {name} WHERE n = 5"
        assert Engine(project, registry).evaluate(table.id).df["v"].to_list() == ["Łódź"]
        # A value of a later row that the server can't convert: its message, not the driver's error
        query = (
            "SELECT CAST(CASE WHEN n = 9000 THEN 'x' ELSE '1' END AS int) AS v FROM (SELECT TOP 10000 ROW_NUMBER() "
            "OVER (ORDER BY (SELECT NULL)) AS n FROM sys.all_objects AS a CROSS JOIN sys.all_objects AS b) AS t"
        )
        table.steps[0].params["query"] = query
        assert Engine(project, registry).evaluate(table.id).first_error == (
            "Conversion failed when converting the varchar value 'x' to data type int."
        )
    finally:
        with connector.open(settings, Context()) as db:
            db.execute(f"DROP TABLE {name}")

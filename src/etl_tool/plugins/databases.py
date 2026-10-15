"""Databases: SQL Server and SQLite connections, and importing a table or a query."""

from __future__ import annotations

import contextlib
import datetime as dt
import decimal
import re
import sqlite3
import struct
import uuid

import polars as pl

from etl_tool.api import APP_NAME, ActionError, Connector, P, action, connector, dtypes, text_length

_SQLSERVER_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
    "SQL Server",
)
_PYTHON_TYPES = {
    str: pl.String,
    int: pl.Int64,
    float: pl.Float64,
    bool: pl.Boolean,
    dt.datetime: pl.Datetime("us"),
    dt.date: pl.Date,
    dt.time: pl.Time,
    bytes: pl.Binary,
    bytearray: pl.Binary,
}


@connector(
    "sqlserver",
    "Microsoft SQL Server",
    params=[
        P.Text("server", "Server", default=r".\SQLEXPRESS", required=True),
        P.Text("database", "Database", required=True),
        P.Text("driver", "ODBC driver", placeholder="empty = newest installed"),
        P.Bool("trust_certificate", "Trust the server certificate", default=True),
    ],
)
class SqlServer(Connector):
    """SQL Server with your Windows login (no password stored)."""

    def connect(self, settings, ctx, *, create=False):
        import pyodbc

        driver = settings.driver or next((d for d in _SQLSERVER_DRIVERS if d in pyodbc.drivers()), "")
        if not driver:
            raise ActionError("No SQL Server ODBC driver is installed (get 'ODBC Driver 18 for SQL Server' from Microsoft)")
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={settings.server}",
            f"DATABASE={settings.database}",
            "Trusted_Connection=yes",
            f"APP={APP_NAME}",
        ]
        if settings.trust_certificate:
            parts.append("TrustServerCertificate=yes")
        try:
            connection = pyodbc.connect(";".join(parts), autocommit=False, timeout=15)
        except pyodbc.Error as e:
            raise ActionError(f"Could not connect to SQL Server '{settings.server}': {self.error_message(e)}") from None
        connection.add_output_converter(-155, _datetimeoffset)  # DATETIMEOFFSET: not supported by pyodbc
        return connection

    def quote(self, identifier):
        return "[" + identifier.replace("]", "]]") + "]"

    def list_tables(self, conn):
        cursor = conn.cursor()
        cursor.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_SCHEMA, TABLE_NAME")
        return [f"{schema}.{name}" for schema, name in cursor.fetchall()]

    def dtype_for(self, description):
        type_code, precision, scale = description[1], description[4], description[5]
        if type_code is decimal.Decimal:
            return pl.Int64 if scale == 0 and (precision or 0) <= 18 else pl.Float64
        return _PYTHON_TYPES.get(type_code)

    def error_message(self, error):
        # pyodbc: ('42S02', "[42S02] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid object
        # name 'x'. (208) (SQLExecDirectW)") -> "Invalid object name 'x'."
        text = str(error.args[1]) if len(error.args) > 1 else str(error)
        text = re.sub(r"\[[^\]]*\]", "", text)
        text = re.sub(r"\s*\(\d+\)\s*(\(SQL\w+\))?", " ", text)
        return re.sub(r"\s+", " ", text).strip(" ;") or str(error)

    def sql_type(self, series, temporary=False):
        dtype = series.dtype
        if dtype.is_integer():
            low, high = series.min(), series.max()
            return "INT" if low is None or (-(2**31) <= low and high < 2**31) else "BIGINT"
        if dtype.is_float():
            return "FLOAT"
        if dtype == pl.Boolean:
            return "BIT"
        if dtype == pl.Date:
            return "DATE"
        if dtype == pl.Datetime:
            return "DATETIME2"
        if dtype == pl.Time:
            return "TIME"
        if dtype == pl.Binary:
            return "VARBINARY(MAX)"
        text = f"NVARCHAR({text_length(series) or 'MAX'})"
        # tempdb may have another collation than the database: compare with the database's own
        return f"{text} COLLATE DATABASE_DEFAULT" if temporary else text

    def temp_table(self, name):
        return "CREATE TABLE", "#" + name

    def prepare_cursor(self, cursor):
        cursor.fast_executemany = True  # sends the rows in batches instead of one by one

    def prepare_frame(self, df):
        df = super().prepare_frame(df)
        zoned = [c for c, t in df.schema.items() if isinstance(t, pl.Datetime) and t.time_zone]
        return df.with_columns(pl.col(c).dt.convert_time_zone("UTC").dt.replace_time_zone(None) for c in zoned)

    def update_sql(self, table, stage, columns, keys):
        sets = ", ".join(f"x.{self.quote(c)} = s.{self.quote(c)}" for c in columns)
        return f"UPDATE x SET {sets} FROM {table} AS x INNER JOIN {stage} AS s ON {self.matching(keys)}"

    def delete_sql(self, table, stage, keys):
        return f"DELETE x FROM {table} AS x WHERE EXISTS (SELECT 1 FROM {stage} AS s WHERE {self.matching(keys)})"

    def run_script(self, conn, sql):
        """T-SQL; batches separated by GO lines (like in SSMS) run one after the other."""
        total, counted = 0, False
        cursor = conn.cursor()
        try:
            for batch in re.split(r"^\s*GO\s*;?\s*$", sql, flags=re.IGNORECASE | re.MULTILINE):
                if batch.strip():
                    cursor.execute(batch)
                    if cursor.rowcount >= 0:
                        total, counted = total + cursor.rowcount, True
        finally:
            cursor.close()
        return total if counted else -1


@connector(
    "sqlite",
    "SQLite",
    params=[P.File("file", "Database file", filter="SQLite databases (*.db *.sqlite *.sqlite3 *.db3);;All files (*.*)")],
)
class SQLite(Connector):
    """A SQLite database file."""

    def connect(self, settings, ctx, *, create=False):
        path = ctx.resolve_path(settings.file)
        if create:
            path.parent.mkdir(parents=True, exist_ok=True)
        elif not path.is_file():
            raise ActionError(f"SQLite file not found: {path}")
        return sqlite3.connect(path, autocommit=False)  # one transaction, also for CREATE TABLE and scripts

    def list_tables(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [name for (name,) in rows]

    def sql_type(self, series, temporary=False):
        dtype = series.dtype
        if dtype.is_integer() or dtype == pl.Boolean:
            return "INTEGER"
        if dtype.is_float():
            return "REAL"
        if dtype == pl.Binary:
            return "BLOB"
        return "TEXT"  # also dates: SQLite keeps them as ISO text (2025-01-31)

    def same(self, left, right):
        return f"{left} IS {right}"

    def temp_table(self, name):
        return "CREATE TEMP TABLE", name

    def prepare_frame(self, df):
        """Dates and times as ISO text, the way SQLite stores them (2025-01-31 08:30:00)."""
        df = super().prepare_frame(df)
        changes = []
        for name, dtype in df.schema.items():
            column = pl.col(name)
            if dtype == pl.Date:
                changes.append(column.dt.strftime("%Y-%m-%d"))
            elif dtype == pl.Datetime:
                fraction = bool((df[name].dt.microsecond() != 0).any())
                changes.append(column.dt.strftime("%Y-%m-%d %H:%M:%S" + ("%.6f" if fraction else "")))
            elif dtype == pl.Time:
                changes.append(column.cast(pl.String))
        return df.with_columns(changes)

    def insert_missing_sql(self, table, stage, columns, keys):
        # NOT EXISTS would scan the whole target table once per new row (no index there):
        # instead one pass over the target finds the staged rows that match.
        names = ", ".join(self.quote(c) for c in columns)
        picked = ", ".join(f"s.{self.quote(c)}" for c in columns)
        matched = f"SELECT m.rowid FROM {table} AS x JOIN {stage} AS m ON {self.matching(keys, 'x', 'm')}"
        return f"INSERT INTO {table} ({names}) SELECT {picked} FROM {stage} AS s WHERE s.rowid NOT IN ({matched})"

    def index_sql(self, stage, keys):
        return f"CREATE INDEX temp.{stage}_keys ON {stage} ({', '.join(self.quote(k) for k in keys)})"

    def run_script(self, conn, sql):
        """Several statements separated by ; are allowed."""
        before = conn.total_changes
        conn.executescript(sql)
        return conn.total_changes - before


@action(
    "db.import",
    "Import from database",
    category="Import",
    kind="source",
    params=[
        P.ConnectionRef("connection", "Connection"),
        P.Choice("mode", "Read", [("table", "A table or view"), ("query", "A SQL query")]),
        P.Text("table", "Table", required=True, placeholder="dbo.Orders", visible_if={"mode": "table"}),
        P.Code("query", "SQL query", required=True, visible_if={"mode": "query"}),
        P.Bool("detect_types", "Detect numbers and dates in text columns", default=False),
    ],
    summary=lambda p: f"Import {p.table if p.mode == 'table' else 'SQL query'} from {p.connection}",
)
def import_database(df, p, ctx):
    """Reads a whole table or view, or the result of a SQL query written in the database's own
    SQL dialect (T-SQL for SQL Server). What the query changes is rolled back, because imports
    also run while editing, e.g. when a project is opened (a SQL Server query that commits by
    itself keeps its changes)."""
    with ctx.database(p.connection) as db:
        try:
            out = db.read_table(p.table) if p.mode == "table" else db.read(p.query)
        finally:
            with contextlib.suppress(Exception):  # e.g. a driver in autocommit mode: nothing to roll back
                db.rollback()
    if p.detect_types:
        out = dtypes.infer_types(out, decimal=".")
    return out


_EXPORT_MODES = [
    ("insert", "Add the rows"),
    ("replace", "Replace all rows (delete them all, then add)"),
    ("replace_keys", "Replace the rows with the same key values (e.g. the same dates)"),
    ("replace_where", "Delete the rows where a condition is true, then add"),
    ("update", "Update the rows with the same key values"),
    ("upsert", "Update the rows with the same key values, add the others"),
    ("delete", "Delete the rows with the same key values"),
]
_KEYED = ("replace_keys", "update", "upsert", "delete")
_ADDS = ("insert", "replace", "replace_keys", "replace_where", "upsert")
_EXPORT_VERBS = {
    "insert": "Add rows to",
    "replace": "Replace all rows of",
    "replace_keys": "Replace rows by {keys} in",
    "replace_where": "Replace rows where … in",
    "update": "Update rows by {keys} in",
    "upsert": "Update or add rows by {keys} in",
    "delete": "Delete rows by {keys} in",
}


def _export_summary(p) -> str:
    verb = _EXPORT_VERBS.get(p.mode, "Export to").format(keys=", ".join(p.key_columns) or "?")
    return f"{verb} {p.table or '?'} ({p.connection or '?'})"


@action(
    "db.export",
    "Export to database",
    category="Export",
    kind="export",
    params=[
        P.ConnectionRef("connection", "Connection"),
        P.Text("table", "Table", required=True, placeholder="dbo.Orders"),
        P.Choice("mode", "What to do", _EXPORT_MODES),
        P.Columns(
            "key_columns",
            "Key columns",
            required=True,
            visible_if={"mode": _KEYED},
            help="The columns that identify a row, e.g. OrderNo, or Date to replace whole days",
        ),
        P.Code(
            "condition",
            "Delete the rows where",
            required=True,
            visible_if={"mode": "replace_where"},
            placeholder="[Date] = CAST(GETDATE() AS date)",
            help="A condition in the database's own SQL (T-SQL for SQL Server)",
        ),
        P.Bool("create", "Create the table if it doesn't exist", default=True, visible_if={"mode": _ADDS}),
        P.Choice(
            "extra_columns",
            "Columns the database table doesn't have",
            [("error", "Stop with an error"), ("skip", "Leave them out")],
        ),
    ],
    summary=_export_summary,
)
def export_database(df, p, ctx):
    """Writes the table into a database table, in one transaction: if anything fails, the
    database stays as it was.

    Add the rows, replace all of them, or replace just the rows with the same key values (e.g.
    the dates in the data: reloading a day replaces that day). Update rows by key columns, or
    update them and add the new ones. Columns are matched by name; a missing table is created
    with types that fit the data. Runs only when the project is run.
    """
    if df.width == 0:
        raise ActionError("Nothing to export: the table has no columns")
    with ctx.database(p.connection, create=True) as db:  # a SQLite file is created if needed
        table = db.qualified(p.table)
        existing = db.columns(p.table)
        if existing is None:
            if p.mode not in _ADDS or not p.create:
                raise ActionError(f"Table {p.table} not found in {p.connection}")
            definitions = ", ".join(f"{db.quote(c)} {db.connector.sql_type(df[c])}" for c in df.columns)
            db.execute(f"CREATE TABLE {table} ({definitions})")
            ctx.log(f"Created table {p.table}")
            existing = list(df.columns)
        names = _target_names(df.columns, existing, p)
        data = db.connector.prepare_frame(df.select(list(names)))
        keys = _key_names(p, names) if p.mode in _KEYED else []
        if p.mode in ("update", "upsert"):
            _check_unique(df, p.key_columns)

        added = deleted = updated = 0
        if p.mode == "replace":
            deleted = db.execute(f"DELETE FROM {table}")
        elif p.mode == "replace_where":
            deleted = db.execute(f"DELETE FROM {table} WHERE {p.condition}")
        elif p.mode in ("replace_keys", "delete"):
            stage = _stage(db, data.select([c for c, n in names.items() if n in keys]).unique(), names, keys, ctx)
            deleted = db.execute(db.connector.delete_sql(table, stage, keys))
        elif p.mode in ("update", "upsert"):
            stage = _stage(db, data, names, keys, ctx)
            others = [n for n in names.values() if n not in keys]
            if others:
                updated = db.execute(db.connector.update_sql(table, stage, others, keys))
            if p.mode == "upsert":
                added = db.execute(db.connector.insert_missing_sql(table, stage, list(names.values()), keys))
        if p.mode in ("insert", "replace", "replace_keys", "replace_where"):
            added = _insert(db, table, data, list(names.values()), ctx)
    parts = [f"{what} {count:,} row{'s' if count != 1 else ''}" for what, count in (
        ("deleted", deleted), ("updated", updated), ("added", added)) if count > 0]
    ctx.log(f"{p.table}: " + (", ".join(parts) if parts else "nothing changed"))
    return df


def _target_names(columns: list[str], existing: list[str], p) -> dict[str, str]:
    """Data column -> column of the database table (names compared without case)."""
    by_name = {name.lower(): name for name in existing}
    names = {c: by_name[c.lower()] for c in columns if c.lower() in by_name}
    missing = [c for c in columns if c.lower() not in by_name]
    if missing and p.extra_columns == "error":
        listed = ", ".join(missing)
        raise ActionError(
            f"The table {p.table} has no column {listed}. Add the column in the database, rename or remove "
            f"it here, or choose “Leave them out”."
        )
    if not names:
        raise ActionError(f"None of the columns are in the table {p.table}")
    return names


def _key_names(p, names: dict[str, str]) -> list[str]:
    if not p.key_columns:
        raise ActionError("Choose the key columns: the columns that identify a row")
    missing = [k for k in p.key_columns if k not in names]
    if missing:
        raise ActionError(f"The key column {', '.join(missing)} is not in the table {p.table}")
    return [names[k] for k in p.key_columns]


def _check_unique(df: pl.DataFrame, keys: list[str]) -> None:
    """Updating needs one row per key value: otherwise it isn't clear which row wins."""
    repeated = df.group_by(keys).len().filter(pl.col("len") > 1)
    if repeated.height:
        example = ", ".join(f"{k} = {repeated[k][0]}" for k in keys)
        values = "1 key value occurs" if repeated.height == 1 else f"{repeated.height:,} key values occur"
        raise ActionError(
            f"The key columns don't identify the rows: {values} more than once "
            f"(e.g. {example}). Remove the duplicates first, or choose more key columns."
        )


def _stage(db, data: pl.DataFrame, names: dict[str, str], keys: list[str], ctx) -> str:
    """A temporary table with the data, named like the columns of the database table and
    indexed on the key columns (then matching rows takes a moment, not minutes)."""
    create, stage = db.connector.temp_table(f"tafl_stage_{uuid.uuid4().hex[:8]}")
    columns = list(data.columns)
    definitions = ", ".join(f"{db.quote(names[c])} {db.connector.sql_type(data[c], temporary=True)}" for c in columns)
    db.execute(f"{create} {stage} ({definitions})")
    _insert(db, stage, data, [names[c] for c in columns], ctx)
    db.execute(db.connector.index_sql(stage, keys))
    return stage


def _insert(db, table: str, data: pl.DataFrame, names: list[str], ctx) -> int:
    if data.height == 0:
        return 0
    columns = ", ".join(db.quote(n) for n in names)
    marks = ", ".join("?" for _ in names)
    return db.execute_many(f"INSERT INTO {table} ({columns}) VALUES ({marks})", data.iter_rows(), between=ctx.check_cancelled)


@action(
    "db.command",
    "Run SQL command",
    category="Export",
    kind="command",
    params=[
        P.ConnectionRef("connection", "Connection"),
        P.Code("sql", "SQL", required=True, placeholder="EXEC dbo.RefreshReport", help="The database's own SQL (T-SQL for SQL Server)"),
    ],
    summary=lambda p: f"Run SQL on {p.connection or '?'}: " + (" ".join(p.sql.split())[:40] or "?"),
)
def run_sql(df, p, ctx):
    """Runs SQL in a database, e.g. a stored procedure after an export, or DELETE / UPDATE
    statements. SQL Server: separate batches with GO lines, as in SSMS. Runs only when the
    project is run; if it fails, its changes are rolled back. The table passes through unchanged."""
    with ctx.database(p.connection) as db:
        try:
            count = db.connector.run_script(db.raw, p.sql)
        except ActionError:
            raise
        except Exception as e:
            raise ActionError(db.connector.error_message(e)) from e
    ctx.log("Done" + (f": {count:,} rows changed" if count >= 0 else ""))
    return df


def _datetimeoffset(value: bytes) -> str:
    year, month, day, hour, minute, second, nanos, tz_hour, tz_minute = struct.unpack("<6hI2h", value)
    stamp = dt.datetime(year, month, day, hour, minute, second, nanos // 1000)
    tz = dt.timezone(dt.timedelta(hours=tz_hour, minutes=tz_minute))
    return stamp.replace(tzinfo=tz).isoformat()

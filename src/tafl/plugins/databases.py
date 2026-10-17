"""Databases: SQL Server and SQLite connections, and importing a table or a query."""

from __future__ import annotations

import bisect
import contextlib
import datetime as dt
import decimal
import re
import sqlite3
import struct
import uuid

import polars as pl

from tafl.api import (
    APP_NAME,
    ActionError,
    Connector,
    Mode,
    P,
    action,
    all_results,
    connector,
    dtypes,
    parameters,
    split_name,
    text_length,
)

# Chosen when the connection names no driver. Not "SQL Server", which comes with Windows: it is
# too old for date and time columns (it reads them as text).
_SQLSERVER_DRIVERS = (
    "ODBC Driver 18 for SQL Server",
    "ODBC Driver 17 for SQL Server",
    "ODBC Driver 13 for SQL Server",
    "SQL Server Native Client 11.0",
)
_GET_DRIVER = "install 'ODBC Driver 18 for SQL Server' (or 17) from Microsoft"
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


# What the ODBC driver's SQLSTATE means when it finds a bad value before the server does
_VALUE_ERRORS = {
    "22001": "a text is too long for its column",
    "22003": "a number is too large for its column, or has too many digits",
    "22007": "a date or time isn't valid for its column",
    "22008": "a date or time is outside what its column can hold",
    "22018": "a value can't be turned into the column's type (e.g. text in a number or date column)",
}

# How each database reads texts, names and comments (parameters.sql_spans), so that a parameter's
# value is filled in where the database expects it. SQL Server: /* */ comments nest, $ is part of
# names (@a$b$) and money ($5), a carriage return also ends a -- comment.
_SQLSERVER_SQL = {"dollar_quotes": False, "line_ends": "\r\n"}
# SQLite: /* */ comments don't nest, only a line feed ends a -- comment, and a variable ($name,
# @name, :name, #name) can end in (…), where a quote starts no text: $a(x'y) is one variable.
_SQLITE_SQL = {
    "nested_comments": False,
    "dollar_quotes": False,
    "line_ends": "\n",
    "words": re.compile(
        r"(?:(?<![A-Za-z0-9_$\x80-\U0010ffff])\$|[@:#])(?:[A-Za-z0-9_$\x80-\U0010ffff]|::)+(?:\([^ \t\n\v\f\r)]*\)?)?"
    ),
}
# T-SQL ranks a minus sign below * / % and ~: a negative number after one of them is written in
# parentheses (2 * -5 / 10 would be 2 * -(5 / 10)). Elsewhere it stays -5, which also works where
# only a constant may be written.
_BINDS_TIGHTER = ("*", "/", "%", "~")


@connector(
    "sqlserver",
    "Microsoft SQL Server",
    params=[
        P.Text("server", "Server", default=r".\SQLEXPRESS", required=True),
        P.Text("database", "Database", required=True),
        P.Text("driver", "ODBC driver", placeholder="empty = the newest suitable one"),
        P.Bool("trust_certificate", "Trust the server certificate", default=True),
    ],
)
class SqlServer(Connector):
    """SQL Server with your Windows login (no password stored)."""

    def connect(self, settings, ctx, *, create=False):
        import pyodbc

        installed = pyodbc.drivers()
        driver = (settings.driver or "").strip()
        if driver and driver.lower() not in (d.lower() for d in installed):
            others = ", ".join(f"'{d}'" for d in installed if "sql server" in d.lower())
            hint = f"Installed for SQL Server: {others}" if others else f"To get one, {_GET_DRIVER}"
            raise ActionError(f"The ODBC driver '{driver}' is not installed. {hint}")
        driver = driver or next((d for d in _SQLSERVER_DRIVERS if d in installed), "")
        if not driver:
            old = (
                " The driver 'SQL Server' that comes with Windows is too old: it reads dates and times as text "
                "(to use it anyway, enter it as the connection's ODBC driver)."
                if "SQL Server" in installed
                else ""
            )
            raise ActionError(f"No suitable SQL Server ODBC driver is installed: {_GET_DRIVER}.{old}")
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
        # pyodbc misreads a sql_variant (NULL as an empty text, whole numbers not at all) and can't
        # read CLR types: a query stops with a hint, read_table_sql converts them to text
        connection.add_output_converter(-150, lambda value: _SQL_VARIANT)
        connection.add_output_converter(-151, lambda value: _CLR_TYPE)  # geography, geometry, hierarchyid
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
        # name 'x'. (208) (SQLExecDirectW)") -> "Invalid object name 'x'." With fast_executemany the
        # message comes first: ('String data, right truncation: length 24 buffer 20', 'HY000').
        parts = [str(arg) for arg in error.args]
        state = next((part for part in parts if re.fullmatch(r"[0-9A-Z]{5}", part)), "")
        text = next((part for part in parts if not re.fullmatch(r"[0-9A-Z]{5}", part)), str(error))
        by_driver = "[SQL Server]" not in text  # the driver found it before the server
        if by_driver and (state in _VALUE_ERRORS or "right truncation" in text):
            # Its (localized) text names no column or value
            meaning = _VALUE_ERRORS.get(state, _VALUE_ERRORS["22001"])
            return f"A value doesn't fit its column in the database: {meaning}"
        message = _diagnostics(text) or str(error)
        if by_driver and state == "42000":
            # The driver's own syntax error: it reads {…} as ODBC escapes before the server sees the SQL
            return (
                f"The ODBC driver couldn't read the SQL ({message}). It takes {{…}} outside texts and comments "
                "for an ODBC escape such as {fn …}; for the driver a comment ends at its first */, also in a "
                "comment inside another comment."
            )
        return message

    def literal(self, value):
        # Dates as ISO text in a CAST: unambiguous whatever the server's language
        if isinstance(value, dt.datetime):
            return f"CAST('{value.isoformat(timespec='microseconds')}' AS datetime2)"
        if isinstance(value, dt.date):
            return f"CAST('{value.isoformat()}' AS date)"
        if isinstance(value, str):
            return "N'" + value.replace("'", "''") + "'"
        return super().literal(value)

    def _exec_literal(self, value):
        """``value`` as an argument of EXEC, where SQL Server takes only constants and variables
        (no CAST): a date as '20261001' and a date-time as '2026-10-01T08:30:00.000', which every
        language setting reads the same, also for a datetime or smalldatetime parameter (six
        digits for parts of a millisecond: those two don't take them)."""
        if isinstance(value, dt.datetime):
            whole = value.microsecond % 1000 == 0
            return "'" + value.isoformat(timespec="milliseconds" if whole else "microseconds") + "'"
        if isinstance(value, dt.date):
            return "'" + value.isoformat().replace("-", "") + "'"
        return self.literal(value)

    def sql_spans(self, sql):
        return parameters.sql_spans(sql, **_SQLSERVER_SQL)

    def fill_sql(self, sql, values):
        spans = self.sql_spans(sql)
        arguments = _exec_arguments(sql, spans)
        comments = [(start, end) for start, end, kind in spans if kind == "comment"]

        def literal_at(value, position):
            if position in arguments:
                return self._exec_literal(value)
            written = self.literal(value)
            if written.startswith("-") and parameters.beside(sql, position, comments, backwards=True) in _BINDS_TIGHTER:
                return f"({written})"
            return written

        return parameters.fill_sql(sql, values, spans=spans, literal_at=literal_at)

    def missing_table(self, error, table):
        # 42S02: base table or view not found; (4413): a view whose own tables are missing
        return bool(error.args) and error.args[0] == "42S02" and "(4413)" not in str(error)

    def _columns(self, conn, table):
        """(name, type, is a CLR type, size in bytes or -1 for max) of each column of a table or
        view, from the catalog of its database; the type of an alias type is the one it is based on."""
        parts = split_name(table)
        if len(parts) > 3:  # on a linked server
            return []
        catalog = f"{self.quote(parts[0])}.sys" if len(parts) == 3 else "sys"
        cursor = conn.cursor()
        try:
            cursor.execute(
                f"SELECT c.name, COALESCE(TYPE_NAME(c.system_type_id), t.name), t.is_assembly_type, c.max_length "
                f"FROM {catalog}.columns AS c JOIN {catalog}.types AS t ON t.user_type_id = c.user_type_id "
                "WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id",
                self.qualified(table),
            )
            return [(name, kind.lower(), bool(clr), size) for name, kind, clr, size in cursor.fetchall()]
        finally:
            cursor.close()

    def column_types(self, conn, table):
        types = {}
        for name, kind, _clr, size in self._columns(conn, table):
            if kind in ("char", "varchar", "nchar", "nvarchar", "binary", "varbinary"):
                kind += "(max)" if size == -1 else f"({size // 2 if kind.startswith('n') else size})"
            types[name] = kind
        return types

    def read_table_sql(self, conn, table):
        try:
            columns = self._columns(conn, table)
        except Exception:  # e.g. no access to the catalog of another database: read it as it is
            columns = []
        if not any(kind == "sql_variant" or clr for _name, kind, clr, _size in columns):
            return super().read_table_sql(conn, table)
        picked = []
        for name, kind, clr, _size in columns:
            column = self.quote(name)
            if kind == "sql_variant":
                picked.append(f"{_VARIANT_TEXT.format(column=column)} AS {column}")
            elif clr:
                picked.append(f"CAST({column} AS nvarchar(max)) AS {column}")  # e.g. POINT (8.5 47.4), /1/2/
            else:
                picked.append(column)
        return f"SELECT {', '.join(picked)} FROM {self.qualified(table)}"

    def sql_type(self, series, temporary=False):
        dtype = series.dtype
        if dtype.is_integer():
            low, high = series.min(), series.max()
            return "INT" if low is None or (-(2**31) <= low and high < 2**31) else "BIGINT"
        if dtype.is_float():
            return "FLOAT"
        if isinstance(dtype, pl.Decimal):
            return f"DECIMAL({min(dtype.precision or 38, 38)}, {dtype.scale})"
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

    def input_sizes(self, df, temporary=False):
        # The driver can't look up the columns of a #temporary table: it would send texts as 255
        # characters in the Windows code page and date-times without fractions of a second.
        # Into the database's own tables the driver's own choice fits the column.
        if not temporary:
            return None
        import pyodbc

        sizes = []
        for name, dtype in df.schema.items():
            if dtype.is_integer():
                sizes.append((pyodbc.SQL_BIGINT, 0, 0))
            elif dtype.is_float():
                sizes.append((pyodbc.SQL_DOUBLE, 0, 0))
            elif isinstance(dtype, pl.Decimal):
                sizes.append((pyodbc.SQL_DECIMAL, min(dtype.precision or 38, 38), dtype.scale))
            elif dtype == pl.Boolean:
                sizes.append((pyodbc.SQL_BIT, 0, 0))
            elif dtype == pl.Date:
                sizes.append((pyodbc.SQL_TYPE_DATE, 10, 0))
            elif dtype == pl.Datetime:
                sizes.append((pyodbc.SQL_TYPE_TIMESTAMP, 27, 7))
            elif dtype == pl.Time:
                sizes.append((pyodbc.SQL_SS_TIME2, 16, 7))
            elif dtype == pl.Binary:
                longest = df[name].bin.size().max() or 1
                sizes.append((pyodbc.SQL_VARBINARY, longest if longest <= 8000 else 0, 0))  # 0: as long as needed
            else:
                sizes.append((pyodbc.SQL_WVARCHAR, text_length(df[name]), 0))
        return sizes

    def prepare_frame(self, df):
        """Also: date-times with a time zone keep their local time and lose the zone, as in the
        Excel export (08:30 in Berlin stays 08:30); infinite numbers are refused."""
        df = super().prepare_frame(df)
        for name, dtype in df.schema.items():
            if dtype.is_float() and df[name].is_infinite().any():
                raise ActionError(
                    f"The column {name} has an infinite value (e.g. from a division by zero), which SQL Server can't store. "
                    f"Replace it first, e.g. with IIF(ISINF([{name}]), NULL, [{name}])."
                )
        zoned = [c for c, t in df.schema.items() if isinstance(t, pl.Datetime) and t.time_zone]
        return df.with_columns(pl.col(c).dt.replace_time_zone(None) for c in zoned)

    def update_sql(self, table, stage, columns, keys):
        sets = ", ".join(f"x.{self.quote(c)} = s.{self.quote(c)}" for c in columns)
        return f"UPDATE x SET {sets} FROM {table} AS x INNER JOIN {stage} AS s ON {self.matching(keys)}"

    def delete_sql(self, table, stage, keys):
        return f"DELETE x FROM {table} AS x WHERE EXISTS (SELECT 1 FROM {stage} AS s WHERE {self.matching(keys)})"

    def run_script(self, conn, sql):
        """T-SQL; batches separated by GO lines (like in SSMS) run one after the other, and
        ``GO 3`` runs the batch before it three times."""
        total, counted = 0, False
        cursor = conn.cursor()
        try:
            for batch, count in _batches(sql):
                if batch.strip():
                    for _ in range(count):
                        cursor.execute(batch)
                        rows = all_results(cursor)  # an error in a later statement of the batch shows up here
                        if rows >= 0:
                            total, counted = total + rows, True
        finally:
            cursor.close()
        return total if counted else -1


# As in SSMS: GO, a repeat count (GO 3) and a -- comment may follow
_GO = re.compile(r"^[ \t]*GO(?:[ \t]+([1-9][0-9]*))?[ \t]*;?[ \t]*(?:--[^\r\n]*)?\r?$", re.IGNORECASE | re.MULTILINE)


def _batches(sql: str) -> list[tuple[str, int]]:
    """T-SQL split at its GO lines, with how many times each batch runs; a GO line inside a text
    or a comment doesn't count."""
    quoted = parameters.sql_spans(sql, **_SQLSERVER_SQL)
    starts = [begin for begin, _end, _kind in quoted]
    batches, start = [], 0
    for line in _GO.finditer(sql):
        k = bisect.bisect_right(starts, line.start()) - 1  # the last text or comment that starts before it
        if k < 0 or quoted[k][1] <= line.start():
            batches.append((sql[start : line.start()], int(line[1] or 1)))
            start = line.end()
    batches.append((sql[start:], 1))
    return batches


# SQL Server's messages come in records joined by "; [", each with the driver's prefix and the
# error's number: "[23000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]… (2627) (SQLExecute)"
_RECORDS = re.compile(r";\s*(?=\[[0-9A-Z]{5}\])")
_PREFIX = re.compile(r"^\s*\[[0-9A-Z]{5}\]\s*\[[^\]]*\]\[[^\]]*\](?:\[SQL Server\])?")
_NUMBER = re.compile(r"\s*\((-?\d+)\)(?:\s*\(SQL\w+\))?\s*$")
_TERMINATED = 3621  # "The statement has been terminated."


def _diagnostics(text: str) -> str:
    """The records of a SQL Server error message without the driver's prefixes and the error
    numbers, each once. The same error for many rows (a duplicate key value per row) is shown
    once, with how many more there are."""
    shown: list[str] = []
    first: dict[int, int] = {}  # the number of a server error -> where it is in shown
    more: dict[int, int] = {}  # where in shown -> how many more records like it
    for record in _RECORDS.split(text):
        prefix = _PREFIX.match(record)
        from_server = prefix is not None and prefix[0].endswith("[SQL Server]")
        record = record[prefix.end() :] if prefix else record
        number = _NUMBER.search(record)
        native = int(number[1]) if number else 0
        record = re.sub(r"\s+", " ", record[: number.start()] if number else record).strip()
        if not record or native == _TERMINATED or record in shown:
            continue
        if from_server and native > 0:
            if native in first:
                more[first[native]] = more.get(first[native], 0) + 1
                continue
            first[native] = len(shown)
        shown.append(record)
    for index, count in more.items():
        shown[index] = f"{_sentence(shown[index])} {count:,} more like it."
    return " ".join([*(_sentence(record) for record in shown[:-1]), *shown[-1:]])


def _sentence(text: str) -> str:
    return text if text.endswith((".", "!", "?")) else text + "."


# A sql_variant as text: dates and times in ISO form, numbers without losing digits, binary
# values as 0x… (CONVERT on its own would give "Oct 15 2026 8:30AM" and six digits of a float)
_VARIANT_TEXT = " ".join(
    """CASE CAST(SQL_VARIANT_PROPERTY({column}, 'BaseType') AS sysname)
    WHEN 'float' THEN FORMAT(CAST({column} AS float), 'R', 'en-US')
    WHEN 'real' THEN FORMAT(CAST({column} AS real), 'R', 'en-US')
    WHEN 'money' THEN CONVERT(nvarchar(30), CAST({column} AS money), 2)
    WHEN 'smallmoney' THEN CONVERT(nvarchar(30), CAST({column} AS money), 2)
    WHEN 'date' THEN CONVERT(nvarchar(10), CAST({column} AS date), 23)
    WHEN 'time' THEN CONVERT(nvarchar(16), CAST({column} AS time))
    WHEN 'datetime' THEN CONVERT(nvarchar(23), CAST({column} AS datetime), 121)
    WHEN 'smalldatetime' THEN CONVERT(nvarchar(19), CAST({column} AS smalldatetime), 120)
    WHEN 'datetime2' THEN CONVERT(nvarchar(27), CAST({column} AS datetime2), 121)
    WHEN 'datetimeoffset' THEN CONVERT(nvarchar(34), CAST({column} AS datetimeoffset))
    WHEN 'binary' THEN CONVERT(nvarchar(max), CAST({column} AS varbinary(8000)), 1)
    WHEN 'varbinary' THEN CONVERT(nvarchar(max), CAST({column} AS varbinary(8000)), 1)
    ELSE CONVERT(nvarchar(4000), {column}) END""".split()
)
# What a query gets for the values pyodbc can't read: the import stops with this
_SQL_VARIANT = ActionError(
    "The column {column} is a sql_variant, which can't be read as it is: convert it to text in the query, "
    "e.g. CAST([{column}] AS nvarchar(4000)) AS [{column}]. Importing the whole table converts it by itself."
)
_CLR_TYPE = ActionError(
    "The column {column} has a type such as geography or hierarchyid, which can't be read as it is: convert it "
    "to text in the query, e.g. CAST([{column}] AS nvarchar(max)) AS [{column}]. Importing the whole table "
    "converts it by itself."
)


# T-SQL between its texts, names and comments: spaces, values ({Name}, numbers, also signed, 0x…
# and $5), @variables, words (also #temporary names) and other characters one by one
_TSQL_TOKEN = re.compile(
    r"(?P<space>\s+)"
    rf"|(?P<value>{parameters.PLACE.pattern}|[+-]?\$?(?:0[xX][0-9A-Fa-f]*|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?))"
    r"|(?P<variable>@[\w@$#]*)"
    r"|(?P<word>(?:[^\W\d]|#)[\w@$#]*)"
    r"|(?P<other>.)",
    re.DOTALL,
)
_WORDS = {"EXEC": "exec", "EXECUTE": "exec", "OUTPUT": "output", "OUT": "output"}
# The arguments of EXEC, read backwards from a value: what was just read, and the kind of the
# token before it, give what is read next. EXEC [@status =] procedure [@parameter =] value
# [OUTPUT], … and EXEC (command [+ …], value [OUTPUT], …) AT server, where the values after the
# first comma are the arguments.
_EXEC_BACKWARDS = {
    "value": {"=": "equals", ",": "comma", "name": "procedure", "variable": "procedure", "(": "open", "+": "plus"},
    "equals": {"variable": "argument"},
    "argument": {",": "comma", "name": "procedure", "variable": "procedure"},
    "comma": {"value": "value", "variable": "value", "name": "value", "output": "output"},
    "output": {"variable": "value"},
    "procedure": {"exec": "found", "=": "status"},
    "status": {"variable": "return"},
    "return": {"exec": "found"},
    "plus": {"value": "command", "variable": "command"},
    "command": {"+": "plus", "(": "open"},
    "open": {"exec": "found"},
}


def _exec_arguments(sql: str, spans: list[tuple[int, int, str]]) -> set[int]:
    """The positions of the ``{Name}`` that SQL Server reads as arguments of EXEC (EXECUTE), as in
    ``EXEC dbo.P {Day}``, ``EXEC @rc = dbo.P @a = 1, @Day = {Day}``, ``INSERT INTO t EXEC …`` or
    ``EXEC ('SELECT ?', {Day}) AT Server``: there it takes only constants and variables.
    ``spans``: the texts, names and comments."""
    if "EXEC" not in sql.upper():
        return set()
    tokens = _tsql_tokens(sql, spans)
    found: dict[int, bool] = {}  # the index of each {Name} among the tokens: a value there is an argument?
    arguments = set()
    for index, (kind, start) in enumerate(tokens):
        if kind == "value" and sql[start] == "{":
            found[index] = _in_exec(tokens, index, found)
            # Right after "EXEC (" or a "+" it is (a part of) the command, not an argument
            if found[index] and tokens[index - 1][0] not in ("(", "+"):
                arguments.add(start)
    return arguments


def _in_exec(tokens: list[tuple[str, int]], index: int, found: dict[int, bool]) -> bool:
    """Whether a value at ``tokens[index]`` is an argument of EXEC, read backwards from it. From
    an earlier value on, the answer is that value's: ``found`` has those of the {Name} before."""
    state = "value"
    for before in range(index - 1, -1, -1):
        state = _EXEC_BACKWARDS[state].get(tokens[before][0])
        if state == "value" and before in found:
            return found[before]
        if state is None or state == "found":
            return state == "found"
    return False


def _tsql_tokens(sql: str, spans: list[tuple[int, int, str]]) -> list[tuple[str, int]]:
    """T-SQL in tokens, (kind, start), as SQL Server reads it: comments and spaces are left out.
    Kinds: "value" (a number, a text, N'…', a {Name}), "variable", "exec" (EXEC, EXECUTE),
    "output" (OUTPUT, OUT), "name" (other words and names; dbo.[P] is one, and so is the
    procedure in EXEC Out or EXEC dbo.P;1) and other characters as themselves."""
    tokens: list[tuple[str, int]] = []
    position = 0

    def name(kind: str, start: int) -> None:  # after a name and dots: one name
        dots = len(tokens)
        while dots > 0 and tokens[dots - 1][0] == ".":
            dots -= 1
        if dots < len(tokens) and dots > 0 and tokens[dots - 1][0] in ("name", "exec", "output"):
            tokens[dots - 1 :] = [("name", tokens[dots - 1][1])]
        else:
            tokens.append((kind, start))

    def procedure_after(count: int) -> bool:  # the first count tokens end with EXEC or EXEC @status =
        kinds = [kind for kind, _start in tokens[max(count - 3, 0) : count]]
        return kinds[-1:] == ["exec"] or kinds == ["exec", "variable", "="]

    for start, end, kind in [*spans, (len(sql), len(sql), "comment")]:
        for match in _TSQL_TOKEN.finditer(sql, position, start):
            if match.lastgroup == "word":
                word = _WORDS.get(match[0].upper(), "name")
                name("name" if word == "output" and procedure_after(len(tokens)) else word, match.start())
            elif match.lastgroup == "value" and match[0].isdigit() and len(tokens) > 1 and tokens[-1][0] == ";" and (
                tokens[-2][0] == "name" and procedure_after(len(tokens) - 2)
            ):
                tokens.pop()  # EXEC dbo.P;1: the procedure dbo.P numbered 1
            elif match.lastgroup != "space":
                tokens.append((match[0] if match.lastgroup == "other" else match.lastgroup, match.start()))
        if kind == "text":
            if tokens and tokens[-1] == ("name", start - 1) and sql[start - 1] in "Nn":
                tokens.pop()  # N'…'
            tokens.append(("value", start))
        elif kind == "name":
            name("name", start)
        position = end
    return tokens


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
        connection = sqlite3.connect(path, autocommit=False)  # one transaction, also for CREATE TABLE and scripts
        if ctx.mode == Mode.PREVIEW:  # only imports run: an ATTACH would create a missing file
            connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        return connection

    def error_message(self, error):
        text = str(error)
        if text == "too many attached databases - max 0":
            return "An import can ATTACH a database only when the project is run (it could create a file)"
        return text

    def list_tables(self, conn):
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
        return [name for (name,) in rows]

    def sql_spans(self, sql):
        return parameters.sql_spans(sql, **_SQLITE_SQL)

    def missing_table(self, error, table):
        # Only this table: "no such table: main.x" from a view on a dropped table x is no reason to create it
        missing = re.search(r"no such table: (?:\w+\.)?(.+)$", str(error))
        return missing is not None and missing[1].lower() == split_name(table)[-1].lower()

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
        """Dates and times as ISO text, the way SQLite stores them (2025-01-31 08:30:00); a
        date-time with a time zone in that zone's local time, without the zone."""
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
        """Several statements separated by ; are allowed, but not BEGIN, COMMIT and ROLLBACK: the
        command runs in one transaction (a COMMIT in it would keep what came before an error)."""

        def authorize(action, *_names):
            return sqlite3.SQLITE_DENY if action == sqlite3.SQLITE_TRANSACTION else sqlite3.SQLITE_OK

        before = conn.total_changes
        conn.set_authorizer(authorize)
        try:
            conn.executescript(sql)
        except sqlite3.DatabaseError as e:
            if str(e) != "not authorized":
                raise
            raise ActionError(
                f"{APP_NAME} runs the command in one transaction, so that a failed command changes nothing: "
                "leave out BEGIN, COMMIT and ROLLBACK"
            ) from None
        finally:
            conn.set_authorizer(None)
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
        out = dtypes.infer_types(out, decimal=".", warn=ctx.warn)
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


def _migrate_export(params: dict, from_version: int) -> dict:
    """Version 1 also exported an empty table."""
    if from_version < 2:
        params.setdefault("skip_empty", False)
    return params


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
        P.Bool(
            "skip_empty",
            "Don't export an empty table",
            default=False,
            help="When the table has no rows, nothing in the database changes (Replace all rows would delete them all)",
        ),
    ],
    version=2,
    migrate=_migrate_export,
    summary=_export_summary,
)
def export_database(df, p, ctx):
    """Writes the table into a database table, in one transaction: if anything fails, the
    database stays as it was.

    Add the rows, replace all of them, or replace just the rows with the same key values (e.g.
    the dates in the data: reloading a day replaces that day). Update rows by key columns, or
    update them and add the new ones. Columns are matched by name; a missing table is created
    with types that fit the data. Date-times with a time zone are written in that zone's local
    time, without the zone (08:30 in Berlin stays 08:30), as in the Excel export. Runs only when
    the project is run.
    """
    if p.skip_empty and df.height == 0:
        ctx.log("Not exported: the table is empty")
        return df
    if df.width == 0:
        raise ActionError("Nothing to export: the table has no columns")
    created = False
    with ctx.database(p.connection, create=True) as db:  # a SQLite file is created if needed
        if _temporary(db.connector.id, split_name(p.table)):
            raise ActionError(f"{p.table} would be a temporary table: it disappears when the export ends. Choose another name.")
        table = db.qualified(p.table)
        existing = db.columns(p.table)
        if existing is None:
            if p.mode not in _ADDS or not p.create:
                raise ActionError(f"Table {p.table} not found in {p.connection}")
            definitions = ", ".join(f"{db.quote(c)} {db.connector.sql_type(df[c])}" for c in df.columns)
            db.execute(f"CREATE TABLE {table} ({definitions})")
            created, existing = True, list(df.columns)
        names = _target_names(df.columns, existing, p)
        types = db.connector.column_types(db.raw, p.table)
        _check_whole_numbers(df, names, types, p.table)
        if p.mode != "delete":  # it writes nothing
            finding = p.key_columns if p.mode == "update" else []  # key values that only find rows
            _check_text_lengths(df, {c: n for c, n in names.items() if c not in finding}, types, p.table)
        data = db.connector.prepare_frame(df.select(list(names)))
        keys = _key_names(p, names) if p.mode in _KEYED else []
        if p.mode in ("update", "upsert"):
            _check_unique(df, p.key_columns)

        added = deleted = updated = 0
        if p.mode == "replace":
            # Not TRUNCATE: it would reset IDENTITY numbering, needs more rights and fails with
            # foreign keys. Deleting 1M rows takes about 1 s; adding them again about 9 s.
            deleted = db.execute(f"DELETE FROM {table}")
        elif p.mode == "replace_where":
            deleted = db.execute(f"DELETE FROM {table} WHERE {p.condition}")
        elif p.mode in ("replace_keys", "delete"):
            stage = _stage(db, data.select([c for c, n in names.items() if n in keys]).unique(), names, keys, types, ctx)
            deleted = db.execute(db.connector.delete_sql(table, stage, keys))
        elif p.mode in ("update", "upsert"):
            stage = _stage(db, data, names, keys, types, ctx)
            others = [n for n in names.values() if n not in keys]
            if others:
                updated = db.execute(db.connector.update_sql(table, stage, others, keys))
            if p.mode == "upsert":
                added = db.execute(db.connector.insert_missing_sql(table, stage, list(names.values()), keys))
        if p.mode in ("insert", "replace", "replace_keys", "replace_where"):
            added = _insert(db, table, data, list(names.values()), ctx)
    if created:  # only now: a failed export creates no table
        ctx.log(f"Created table {p.table}")
    parts = [f"{what} {count:,} row{'s' if count != 1 else ''}" for what, count in (
        ("deleted", deleted), ("updated", updated), ("added", added)) if count > 0]
    ctx.log(f"{p.table}: " + (", ".join(parts) if parts else "nothing changed"))
    return df


def _temporary(connector_id: str, parts: list[str]) -> bool:
    """A table that disappears with the connection: temp.x in SQLite, #x (and ##x) in SQL Server."""
    if connector_id == "sqlite":
        return len(parts) > 1 and parts[0].lower() == "temp"
    return connector_id == "sqlserver" and bool(parts) and parts[-1].startswith("#")


_WHOLE_NUMBER_TYPES = {"tinyint", "smallint", "int", "integer", "bigint", "bit"}


def _check_whole_numbers(df: pl.DataFrame, names: dict[str, str], types: dict[str, str], table: str) -> None:
    """The database would cut off decimals that go into a whole-number column (1.5 -> 1) without
    a word: refuse instead."""
    kinds = {name.lower(): kind for name, kind in types.items()}
    for source, target in names.items():
        column = df[source]
        if column.dtype.is_float() and kinds.get(target.lower()) in _WHOLE_NUMBER_TYPES:
            values = column.drop_nulls().drop_nans()
            if values.len() and (values != values.round(0)).any():
                example = values.filter(values != values.round(0))[0]
                raise ActionError(
                    f"The column {target} of {table} holds whole numbers ({kinds[target.lower()]}), but {source} has "
                    f"decimals (e.g. {example}): they would be cut off. Round them first, e.g. ROUND([{source}]), "
                    "or change the column's type in the database."
                )


_TEXT_TYPE = re.compile(r"(n?)(?:var)?char\((\d+)\)")


def _check_text_lengths(df: pl.DataFrame, names: dict[str, str], types: dict[str, str], table: str) -> None:
    """A text too long for its column stops the export with a message that names no column:
    refuse with the column and an example first."""
    kinds = {name.lower(): kind for name, kind in types.items()}
    for source, target in names.items():
        kind = kinds.get(target.lower(), "")
        size = _TEXT_TYPE.fullmatch(kind)
        if size is None or df[source].dtype != pl.String:
            continue
        texts = df[source]
        # nvarchar counts UTF-16 code units: an emoji takes two
        lengths = texts.str.len_chars() + (texts.str.count_matches(r"[\x{10000}-\x{10FFFF}]") if size[1] else 0)
        too_long = lengths > int(size[2])
        if too_long.any():
            example, length = texts.filter(too_long)[0], lengths.filter(too_long)[0]
            shown = example if len(example) <= 40 else example[:40] + "…"
            raise ActionError(
                f"The column {target} of {table} holds texts of up to {int(size[2]):,} characters ({kind}), but "
                f"{source} has longer ones (e.g. “{shown}”, {length:,} characters). Shorten them first, e.g. "
                f"LEFT([{source}], {size[2]}), or make the column longer in the database."
            )


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


def _stage(db, data: pl.DataFrame, names: dict[str, str], keys: list[str], types: dict[str, str], ctx) -> str:
    """A temporary table with the data, named like the columns of the database table and
    indexed on the key columns (then matching rows takes a moment, not minutes). A date-time
    for a datetime or smalldatetime column (SQL Server) is stored as one, rounded to its steps
    as it was in the database table (.123 is .1233333 there): otherwise it wouldn't match."""
    kinds = {name.lower(): kind for name, kind in types.items()}

    def column_type(column: str) -> str:
        kind = kinds.get(names[column].lower())
        if data[column].dtype == pl.Datetime and kind in ("datetime", "smalldatetime"):
            return kind.upper()
        return db.connector.sql_type(data[column], temporary=True)

    create, stage = db.connector.temp_table(f"tafl_stage_{uuid.uuid4().hex[:8]}")
    columns = list(data.columns)
    definitions = ", ".join(f"{db.quote(names[c])} {column_type(c)}" for c in columns)
    db.execute(f"{create} {stage} ({definitions})")
    _insert(db, stage, data, [names[c] for c in columns], ctx, temporary=True)
    db.execute(db.connector.index_sql(stage, keys))
    return stage


def _insert(db, table: str, data: pl.DataFrame, names: list[str], ctx, temporary: bool = False) -> int:
    if data.height == 0:
        return 0
    columns = ", ".join(db.quote(n) for n in names)
    marks = ", ".join("?" for _ in names)
    sizes = db.connector.input_sizes(data, temporary)
    sql = f"INSERT INTO {table} ({columns}) VALUES ({marks})"
    return db.execute_many(sql, data.iter_rows(), between=ctx.check_cancelled, sizes=sizes)


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
    statements. SQL Server: separate batches with GO lines, as in SSMS (GO 3 runs a batch three
    times). Runs only when the project is run, in one transaction: if it fails, its changes are
    rolled back (so leave out BEGIN and COMMIT). The table passes through unchanged."""
    with ctx.database(p.connection) as db:
        try:
            count = db.connector.run_script(db.raw, p.sql)
        except ActionError:
            raise
        except Exception as e:
            raise ActionError(db.connector.error_message(e)) from e
    ctx.log("Done" + (f": {count:,} row{'s' if count != 1 else ''} changed" if count >= 0 else ""))
    return df


def _datetimeoffset(value: bytes) -> str:
    year, month, day, hour, minute, second, nanos, tz_hour, tz_minute = struct.unpack("<6hI2h", value)
    stamp = dt.datetime(year, month, day, hour, minute, second, nanos // 1000)
    tz = dt.timezone(dt.timedelta(hours=tz_hour, minutes=tz_minute))
    return stamp.replace(tzinfo=tz).isoformat()

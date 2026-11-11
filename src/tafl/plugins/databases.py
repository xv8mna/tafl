"""Databases: SQL Server and SQLite connections, and importing a table or a query."""

from __future__ import annotations

import bisect
import contextlib
import datetime as dt
import decimal
import functools
import re
import sqlite3
import struct
import threading
import unicodedata
import uuid
import weakref
from pathlib import Path
from typing import NamedTuple
from urllib.parse import quote

import polars as pl

from tafl.api import (
    APP_NAME,
    ActionError,
    Cancelled,
    Connector,
    Endpoint,
    Mode,
    P,
    action,
    all_results,
    col,
    cols,
    connector,
    detection,
    dtypes,
    endpoints,
    parameters,
    split_name,
    text_length,
    unique_names,
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
# A value must stay one value in the connection text: a keyword of the driver in a project's setting
# can write a file (SAVEFILE) or lead the login to another server (Address). A server name needs none
# of these characters; a database name goes in braces, with each } doubled.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_NOT_IN_A_SERVER = re.compile(rf"[;={{}}]|{_CONTROL.pattern}")
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

        if _NOT_IN_A_SERVER.search(settings.server):
            raise ActionError(
                f"A server name can't hold ; = {{ }} or control characters: {settings.server!r}. "
                r"Write it like .\SQLEXPRESS, host,1433 or host\instance."
            )
        if _CONTROL.search(settings.database):
            raise ActionError(f"A database name can't hold control characters: {settings.database!r}")
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
        database = settings.database.strip().replace("}", "}}")
        parts = [
            f"DRIVER={{{driver}}}",
            f"SERVER={settings.server}",
            f"DATABASE={{{database}}}",
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
        return _Stoppable(connection, ctx)

    def quote(self, identifier):
        return "[" + identifier.replace("]", "]]") + "]"

    def list_tables(self, conn):
        cursor = conn.cursor()
        cursor.execute("SELECT TABLE_SCHEMA, TABLE_NAME FROM INFORMATION_SCHEMA.TABLES ORDER BY TABLE_SCHEMA, TABLE_NAME")
        return [f"{schema}.{name}" for schema, name in cursor.fetchall()]

    def dtype_for(self, description):
        type_code, precision, scale = description[1], description[4], description[5]
        if type_code is decimal.Decimal:  # decimal, numeric, money: make_series keeps their digits
            if scale == 0 and (precision or 0) <= 18:
                return pl.Int64
            return pl.Decimal(min(precision or 38, 38), scale or 0)
        # datetimeoffset, which pyodbc takes for text: date-times in UTC (_datetimeoffset). With
        # digits of a second only it has a scale; without, it is described as a text of 26
        # characters: the values tell (import_database makes an empty such column text again).
        if type_code is str and scale:
            return pl.Datetime("us", "UTC")
        if type_code is str and precision == _DATETIMEOFFSET_SIZE:
            return None
        return _PYTHON_TYPES.get(type_code)

    def error_message(self, error):
        # pyodbc: ('42S02', "[42S02] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]Invalid object
        # name 'x'. (208) (SQLExecDirectW)") -> "Invalid object name 'x'." With fast_executemany the
        # message comes first: ('String data, right truncation: length 24 buffer 20', 'HY000').
        parts = [str(arg) for arg in error.args]
        state = next((part for part in parts if re.fullmatch(r"[0-9A-Z]{5}", part)), "")
        text = next((part for part in parts if not re.fullmatch(r"[0-9A-Z]{5}", part)), str(error))
        if state == _CANCELLED:  # only Stop cancels a statement (_Stoppable); the driver's text is in the language of Windows
            return "Stopped: SQL Server's statement was ended before it was done"
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

    def _columns(self, conn, table, by_key: bool = False) -> list[_Column]:
        """The columns of a table or view, from the catalog of its database; for a synonym, those
        of the table or view it stands for (also in another database of the server, also written
        db..table). A synonym for a table of another server has none here, as a table there (its
        columns can't be read on this one), and is an ActionError ``by_key``: the keyed modes need
        their types. So is a synonym whose columns this server can't tell (e.g. its database is
        gone). SQL Server's PARSENAME gives the parts of the synonym's base, also empty ones."""
        parts = split_name(table)
        if len(parts) > 3:  # on a linked server
            return []
        target = self.qualified(table)
        cursor = conn.cursor()
        try:
            catalog = f"{self.quote(parts[0])}.sys" if len(parts) == 3 else "sys"
            cursor.execute(
                f"SELECT base_object_name, PARSENAME(base_object_name, 4), PARSENAME(base_object_name, 3), "
                f"PARSENAME(base_object_name, 2), PARSENAME(base_object_name, 1) FROM {catalog}.synonyms "
                "WHERE object_id = OBJECT_ID(?)",
                target,
            )
            synonym = cursor.fetchone()
            if synonym is not None:
                base, server, database, schema, name = synonym
                if server is not None and by_key:
                    raise ActionError(
                        f"{table} is a synonym for {base}, a table of another server: to find rows by key, the "
                        "export needs the types of its columns, and it can read them only on the connection's server. "
                        "Use a connection to that server."
                    )
                if server is not None:  # written as in 0.6.4, without the types of its columns
                    return []
                if database is None and len(parts) == 3:  # in the synonym's database
                    database = parts[0]
                schema = self.quote(schema) if schema else ""  # db..table: the user's default schema there
                target = f"{self.quote(database)}.{schema}.{self.quote(name)}" if database else f"{schema}.{self.quote(name)}".lstrip(".")
                catalog = f"{self.quote(database)}.sys" if database else catalog
            query = (
                f"SELECT c.name, COALESCE(TYPE_NAME(c.system_type_id), t.name), t.is_assembly_type, c.max_length, "
                f"c.precision, c.scale, c.collation_name FROM {catalog}.columns AS c JOIN {catalog}.types AS t "
                "ON t.user_type_id = c.user_type_id WHERE c.object_id = OBJECT_ID(?) ORDER BY c.column_id"
            )
            try:
                cursor.execute(query, target)
            except Exception as e:  # e.g. the synonym's database is gone or can't be read
                if synonym is None:
                    raise
                if by_key:
                    raise ActionError(
                        f"{table} is a synonym for {synonym[0]}, whose columns can't be read ({self.error_message(e)}): to "
                        "find rows by key, the export needs their types."
                    ) from e
                return []  # written as in 0.6.4: SQL Server says what is wrong
            return [_Column(name, kind.lower(), bool(clr), *rest) for name, kind, clr, *rest in cursor.fetchall()]
        finally:
            cursor.close()

    def column_types(self, conn, table):
        """E.g. int, nvarchar(50), decimal(10,2), datetime2(0). A char or varchar column of a
        UTF-8 collation, whose length counts bytes, has it after the type: varchar(50) collate
        latin1_general_100_ci_as_sc_utf8."""
        types = {}
        for c in self._columns(conn, table):
            kind = c.kind
            if kind in _LENGTH_TYPES:
                kind += "(max)" if c.size == -1 else f"({c.size // 2 if kind.startswith('n') else c.size})"
                if kind.startswith(_CODE_PAGE_TYPES) and (c.collation or "").lower().endswith("_utf8"):
                    kind += f" collate {c.collation.lower()}"
            elif kind in ("decimal", "numeric"):
                kind += f"({c.precision},{c.scale})"
            elif kind in _FRACTION_TYPES:
                kind += f"({c.scale})"
            types[c.name] = kind
        return types

    def stage_types(self, conn, table):
        """The type of each column (an alias type as the one it is based on; texts with their
        collation), except for types the stage can't have or the driver can't send as they are
        (sql_variant, xml, CLR types such as geography, image, rowversion). A text or ntext column
        gets varchar(max) or nvarchar(max) with its collation: numbers, dates and true/false
        arrive there as the text the driver writes into the column itself (an int column of the
        stage could not be assigned to it: "Operand type clash: int is incompatible with text")."""
        types = {}
        for c in self._columns(conn, table, by_key=True):
            if c.clr:
                continue
            if c.kind in _LONG_TEXT_TYPES:
                collation = f" COLLATE {c.collation}" if c.collation else ""
                types[c.name] = f"{'NVARCHAR' if c.kind == 'ntext' else 'VARCHAR'}(MAX){collation}"
            elif c.kind in _PLAIN_TYPES:
                types[c.name] = c.kind.upper()
            elif c.kind in ("decimal", "numeric"):
                types[c.name] = f"{c.kind.upper()}({c.precision}, {c.scale})"
            elif c.kind in _FRACTION_TYPES:
                types[c.name] = f"{c.kind.upper()}({c.scale})"
            elif c.kind in _LENGTH_TYPES:
                length = "MAX" if c.size == -1 else c.size // 2 if c.kind.startswith("n") else c.size
                collation = f" COLLATE {c.collation}" if c.collation else ""
                types[c.name] = f"{c.kind.upper()}({length}){collation}"
        return types

    def read_table_sql(self, conn, table):
        try:
            columns = self._columns(conn, table)
        except Exception:  # e.g. no access to the catalog of another database: read it as it is
            columns = []
        if not any(c.kind == "sql_variant" or c.clr for c in columns):
            return super().read_table_sql(conn, table)
        picked = []
        for c in columns:
            column = self.quote(c.name)
            if c.kind == "sql_variant":
                picked.append(f"{_VARIANT_TEXT.format(column=column)} AS {column}")
            elif c.clr:
                picked.append(f"CAST({column} AS nvarchar(max)) AS {column}")  # e.g. POINT (8.5 47.4), /1/2/
            else:
                picked.append(column)
        return f"SELECT {', '.join(picked)} FROM {self.qualified(table)}"

    def sql_type(self, series, temporary=False):
        dtype = series.dtype
        if dtype.is_integer():
            low, high = series.min(), series.max()
            if low is None or (-(2**31) <= low and high < 2**31):
                return "INT"
            if _BIGINT[0] <= low and high <= _BIGINT[1]:
                return "BIGINT"
            # beyond a bigint (UInt64 from Parquet, 128 bits from a plugin): a decimal holds them exactly
            if len(str(max(abs(low), abs(high)))) > _MOST_DIGITS:  # beyond every decimal: before the table is made
                raise ActionError(_beyond_decimals(series.name, high if abs(high) >= abs(low) else low))
            return "DECIMAL(20, 0)" if dtype == pl.UInt64 else "DECIMAL(38, 0)"
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

    def input_sizes(self, df, types=None, temporary=False):
        # Texts for char and varchar columns go as Unicode, in every mode (_binding): the server
        # converts them to the column's code page, as it converts N'…' (Łódź stays Łódź in a
        # Polish or UTF-8 column, and is Lódz in a Latin1 one), and refuses those too long for
        # the column. Bound as varchar, the driver would convert them to this PC's code page on
        # the way, whatever the column's collation, and a text too long in bytes for a UTF-8
        # column would be cut without a word.
        if types is None:
            return None
        if not temporary:  # into the database's own tables the driver's own choice fits the other columns
            texts = [None] * df.width
            for index, (name, dtype) in enumerate(df.schema.items()):
                kind = _kind(types.get(name, ""))[0]
                text = dtype == pl.String or isinstance(dtype, (pl.Categorical, pl.Enum))  # categories too, also named ones
                # text and ntext: values of every type, as for the stage below (left to the driver,
                # categories, and numbers or dates whose first value is empty, took 2 GB per value)
                if (text and kind in _CODE_PAGE_TYPES) or kind in _LONG_TEXT_TYPES:
                    texts[index] = _binding(types[name])
            return texts if any(texts) else None
        # The driver can't look up the columns of a #temporary table: it would send texts as 255
        # characters in the Windows code page and date-times without fractions of a second.
        # Bound as the driver binds the same type in a table of the database (and texts as
        # above), a value arrives as it does there (e.g. a datetime2(0) without its fraction).
        # Texts for numbers, dates and other types go as texts, which the server converts, as
        # before.
        codes = _odbc_types()
        as_text = {codes[kind] for kind in ("char", "varchar", "nchar", "nvarchar", "datetimeoffset")}
        sizes = []
        for name, dtype in df.schema.items():
            binding = _binding(types.get(name, ""))
            if binding is None or (dtype == pl.String and binding[0] not in as_text):
                binding = _data_binding(df[name])
            sizes.append(binding)
        return sizes

    def fit_frame(self, df, types):
        """Values as their columns hold them, the same in every mode: date-times and times cut to
        the digits of a second their datetime2(n), datetimeoffset(n) or time(n) column holds, as
        the driver cuts date-times (08:00:01.5 is 08:00:01 in time(0)); date-times for a date
        column: their date. For a datetimeoffset column, a date-time with a time zone gets its
        zone's offset (08:30 in Berlin in summer: 08:30 +02:00; the driver gives one without a
        zone the offset of this PC's time zone on its day), and a text the form the driver reads
        (2026-10-01T08:30:00+02:00, as imports give it, is 2026-10-01 08:30:00 +02:00). Decimals
        (Decimal) are rounded to the decimal places of a decimal column, half away from zero as
        SQL Server rounds (1.005 is 1.01 in a decimal(10,2)), and are floats for a float column.
        The driver takes fractions of a second to the microsecond: digits below it are cut here
        too, as the driver would cut them (``_cut_seconds`` tells about the digits that are cut)."""
        changes = []
        for name, dtype in df.schema.items():
            kind, size, scale = _kind(types.get(name, ""))
            column = col(name)
            moment = isinstance(dtype, pl.Datetime)
            if moment and dtype.time_zone and kind == "datetimeoffset":
                changes.append(_offset_text(column, size))
            elif moment and kind in ("datetime2", "datetimeoffset") and size < 6:
                changes.append(column.dt.truncate(f"{10 ** (6 - size)}us"))
            elif moment and kind == "date":
                changes.append(column.dt.date())
            elif dtype == pl.Time and kind == "time" and size < 7:
                step = 10 ** (9 - size)  # nanoseconds
                changes.append((column.cast(pl.Int64) // step * step).cast(pl.Time))
            elif moment and dtype.time_unit == "ns":
                changes.append(column.dt.truncate("1us"))
            elif dtype == pl.Time:
                changes.append((column.cast(pl.Int64) // 1000 * 1000).cast(pl.Time))
            elif dtype == pl.String and kind == "datetimeoffset":
                changes.append(_driver_offset_text(column, size))
            elif isinstance(dtype, pl.Decimal) and kind in ("float", "real"):
                changes.append(column.cast(pl.Float64))
            elif isinstance(dtype, pl.Decimal) and kind in _DECIMAL_TYPES and dtype.scale > scale:
                changes.append(_round_half_away(df[name], scale))
        return df.with_columns(changes)

    def prepare_frame(self, df):
        """Also: date-times with a time zone keep their local time and lose the zone, as in the
        Excel export (08:30 in Berlin stays 08:30), unless ``fit_frame`` gave them their offset
        for a datetimeoffset column; infinite numbers are refused."""
        df = super().prepare_frame(df)
        for name, dtype in df.schema.items():
            if dtype.is_float() and df[name].alias("value").is_infinite().any():  # a fixed name, as in _round_half_away
                raise ActionError(
                    f"The column {name} has an infinite value (e.g. from a division by zero), which SQL Server can't store. "
                    f"Replace it first, e.g. with IIF(ISINF([{name}]), NULL, [{name}])."
                )
        zoned = [c for c, t in df.schema.items() if isinstance(t, pl.Datetime) and t.time_zone]
        return df.with_columns(col(c).dt.replace_time_zone(None) for c in zoned)

    def matching(self, keys, left=None, right=None, kinds=None):
        """The table's and the stage's names in the statement are ``_aliases`` of the key columns
        when none are given. ``kinds``: the types of the table's columns (``column_types``). A key
        of a column with 7 digits of a second (datetime2(7), time(7), datetimeoffset(7)) is
        compared to the microsecond: the driver gives and takes date-times only to the
        microsecond, so a key that an import read from such a column finds its row (a key with a
        7th digit, from a text for a datetimeoffset column, finds only its own value)."""
        if left is None or right is None:
            left, right = (self.quote(alias) for alias in _aliases(keys))
        kinds = {name.lower(): kind for name, kind in (kinds or {}).items()}
        matched = []
        for key in keys:
            x, s = f"{left}.{self.quote(key)}", f"{right}.{self.quote(key)}"
            if _seven_digits(kinds.get(key.lower(), "")):  # in the form of same(): SQL Server joins such rows by hashing
                matched.append(self.same(_microsecond(x), _microsecond(s)))
                matched.append(f"(DATEPART(NANOSECOND, {s}) % 1000 = 0 OR {x} = {s} OR {s} IS NULL)")
            else:
                matched.append(self.same(x, s))
        return " AND ".join(matched)

    def update_sql(self, table, stage, columns, keys, kinds=None):
        x, s = (self.quote(alias) for alias in _aliases([*columns, *keys]))
        sets = ", ".join(f"{x}.{self.quote(c)} = {s}.{self.quote(c)}" for c in columns)
        return f"UPDATE {x} SET {sets} FROM {table} AS {x} INNER JOIN {stage} AS {s} ON {self.matching(keys, x, s, kinds)}"

    def delete_sql(self, table, stage, keys, kinds=None):
        x, s = (self.quote(alias) for alias in _aliases(keys))
        return f"DELETE {x} FROM {table} AS {x} WHERE EXISTS (SELECT 1 FROM {stage} AS {s} WHERE {self.matching(keys, x, s, kinds)})"

    def insert_missing_sql(self, table, stage, columns, keys, kinds=None):
        x, s = (self.quote(alias) for alias in _aliases([*columns, *keys]))
        names = ", ".join(self.quote(c) for c in columns)
        picked = ", ".join(f"{s}.{self.quote(c)}" for c in columns)
        return (
            f"INSERT INTO {table} ({names}) SELECT {picked} FROM {stage} AS {s} "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} AS {x} WHERE {self.matching(keys, x, s, kinds)})"
        )

    def index_sql(self, stage, keys):
        # Only when SQL Server can index the keys: none of them (n)varchar(max), varbinary(max),
        # xml, text, ntext, image or a CLR type, at most 1,700 bytes together and at most 32 of
        # them (900 bytes and 16 keys before SQL Server 2016). Without the index, the rows still
        # find each other. The index is made by EXEC: SQL Server checks the number of its columns
        # when it reads the statement, also one that the IF leaves out.
        listed = ", ".join("N'" + key.replace("'", "''") + "'" for key in keys)
        columns = f"FROM tempdb.sys.columns WHERE object_id = OBJECT_ID(N'tempdb..{stage}') AND name IN ({listed})"
        newer = "CAST(SERVERPROPERTY('ProductMajorVersion') AS int) >= 13"
        create = super().index_sql(stage, keys).replace("'", "''")
        return (
            f"IF NOT EXISTS (SELECT 1 {columns} AND (max_length = -1 OR system_type_id IN (34, 35, 99, 240, 241))) "
            f"AND (SELECT SUM(max_length) {columns}) <= CASE WHEN {newer} THEN 1700 ELSE 900 END "
            f"AND (SELECT COUNT(*) {columns}) <= CASE WHEN {newer} THEN 32 ELSE 16 END EXEC (N'{create}')"
        )

    def run_script(self, conn, sql):
        """T-SQL; batches separated by GO lines (like in SSMS) run one after the other, and
        ``GO 3`` runs the batch before it three times. The command runs in one transaction, its
        own BEGIN TRAN … COMMIT inside it, also across batches, and a CATCH with ROLLBACK and THROW
        work as in SSMS. Each batch ends with a check of the open transactions in the same request
        (a later request would find a new one, which the driver begins): a COMMIT or ROLLBACK that
        ended the transaction of the step stops it with a plain message (what ran up to there may
        be stored, and the rest of that batch ran without a transaction); so does a transaction
        that the command began and didn't end, after its last batch (the step's rollback undoes
        it all). Not appended to a batch that must be alone (CREATE PROCEDURE …). A procedure or
        an EXEC that ends the transaction or begins one of its own: SQL Server reports it (error
        266), and the message says what that means."""
        total, counted = 0, False
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT @@TRANCOUNT")
            open_at_start = cursor.fetchone()[0]
            check = _TRANSACTION_CHECK.format(count=int(open_at_start))
            for start, end, count in _batch_spans(sql):
                batch = sql[start:end]
                if batch.strip():
                    sent = batch if _alone_in_batch(batch) else batch + check
                    for _ in range(count):
                        try:
                            cursor.execute(sent)
                            rows = all_results(cursor)  # an error in a later statement of the batch shows up here
                        except Exception as e:
                            line = sql.count("\n", 0, start + len(batch.rstrip())) + 1  # where the batch ends
                            if (message := _transaction_count_message(e, line, batch)) is not None:
                                raise ActionError(message) from e
                            raise
                        if rows >= 0:
                            total, counted = total + rows, True
            cursor.execute("SELECT @@TRANCOUNT")
            if cursor.fetchone()[0] > open_at_start:
                raise ActionError(
                    "The command began a transaction and didn't end it (BEGIN TRAN without COMMIT), so the step stopped "
                    f"and its changes were undone. Add the COMMIT, or leave out the BEGIN TRAN: {APP_NAME} runs the command "
                    "in one transaction."
                )
        finally:
            cursor.close()
        return total if counted else -1


def _seven_digits(sql_type: str) -> bool:
    """Whether a SQL Server column of this type holds 7 digits of a second (100 nanoseconds)."""
    kind, size, _scale = _kind(sql_type)
    return kind in _FRACTION_TYPES and size == 7


def _microsecond(value: str) -> str:
    """T-SQL for a datetime2, time or datetimeoffset value cut to the microsecond."""
    return f"DATEADD(NANOSECOND, -(DATEPART(NANOSECOND, {value}) % 1000), {value})"


def _aliases(names) -> tuple[str, str]:
    """The names of the table and of the stage (its #temporary table) in the statements of the
    keyed modes: none of the column names there. An xml or CLR column (geography, hierarchyid)
    named like one of them would make that name ambiguous ("Ambiguous column name 's'"): s.[K]
    could also be the member K of such a column s. Names are compared as SQL Server may compare
    them, without regard to case, accents or width."""
    taken = {_folded(name) for name in names}
    number = 1
    while True:
        suffix = str(number) if number > 1 else ""
        aliases = (f"tafl_target{suffix}", f"tafl_stage{suffix}")
        if not taken.intersection(aliases):
            return aliases
        number += 1


def _folded(name: str) -> str:
    """A name as a case-, accent- and width-insensitive collation compares it."""
    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch)).casefold()


_CANCELLED = "HY008"  # the driver's state for a statement that cursor.cancel() ended
_STOP_CHECK_SECONDS = 0.05  # how often Stop is looked at while a statement runs


class _Stoppable:
    """A connection to SQL Server whose running statements Stop (and Ctrl+C in ``tafl run``)
    ends: a query that gives no row yet, a statement that waits for a lock, a long command. While
    one of its cursors is in a call of the driver (``execute``, ``executemany``, a fetch or
    ``nextset``: ``_StoppableCursor``), a thread looks at Stop and cancels the statement there
    (``cursor.cancel()``, the call that ODBC allows from another thread). The call then fails
    (``_CANCELLED``), the step ends as stopped, and an export's or a command's transaction is
    rolled back. Everything else is the connection's own."""

    def __init__(self, connection, ctx) -> None:
        self._connection = connection
        self._ctx = ctx
        self._lock = threading.Lock()  # a cancel is never at the same time as the end of the call
        self._running: list = []  # the cursors (of the driver) in a call of the driver
        self._closed = threading.Event()
        self._watching = False
        self.described = None  # cursor.description after the last execute or nextset (import_database reads it)

    def cursor(self):
        return _StoppableCursor(self._connection.cursor(), self)

    def close(self) -> None:
        self._closed.set()
        self._connection.close()

    def __getattr__(self, name):
        if name.startswith("_"):  # its own, not yet set (e.g. while it is made)
            raise AttributeError(name)
        return getattr(self._connection, name)

    def call(self, cursor, method, *args):
        """``method(*args)``, a call of the driver for ``cursor``, which Stop can end."""
        with self._lock:
            self._running.append(cursor)
            if not self._watching:
                self._watching = True
                threading.Thread(target=_watch, args=(weakref.ref(self),), name="SQL Server stop", daemon=True).start()
        try:
            return method(*args)
        finally:
            with self._lock:
                self._running.remove(cursor)

    def cancel_running(self) -> bool:
        """Cancel the statements in a call of the driver when the step is stopped; False when the
        connection is closed."""
        if self._closed.is_set():
            return False
        try:
            self._ctx.check_cancelled()
        except Cancelled:
            with self._lock:
                for cursor in self._running:
                    with contextlib.suppress(Exception):  # e.g. one that just ended
                        cursor.cancel()
        return True


def _watch(connection: weakref.ref) -> None:
    """Look at Stop while the connection is open (and not forgotten)."""
    while (owner := connection()) is not None and owner.cancel_running():
        closed = owner._closed
        del owner  # the connection is not kept for the thread
        if closed.wait(_STOP_CHECK_SECONDS):
            return


class _StoppableCursor:
    """A cursor of ``_Stoppable``: the calls that wait for SQL Server go through ``call``,
    everything else is the cursor's own."""

    __slots__ = ("_cursor", "_owner")

    def __init__(self, cursor, owner: _Stoppable) -> None:
        object.__setattr__(self, "_cursor", cursor)
        object.__setattr__(self, "_owner", owner)

    def execute(self, *args):
        self._owner.call(self._cursor, self._cursor.execute, *args)
        self._owner.described = self._cursor.description
        return self

    def executemany(self, *args):
        self._owner.call(self._cursor, self._cursor.executemany, *args)
        return self

    def fetchone(self):
        return self._owner.call(self._cursor, self._cursor.fetchone)

    def fetchmany(self, *args):
        return self._owner.call(self._cursor, self._cursor.fetchmany, *args)

    def fetchall(self):
        return self._owner.call(self._cursor, self._cursor.fetchall)

    def nextset(self):
        found = self._owner.call(self._cursor, self._cursor.nextset)
        self._owner.described = self._cursor.description if found else self._owner.described
        return found

    def __iter__(self):
        return self

    def __next__(self):
        if (row := self.fetchone()) is None:
            raise StopIteration
        return row

    def __enter__(self):
        self._cursor.__enter__()
        return self

    def __exit__(self, *problem):
        return self._cursor.__exit__(*problem)

    def __getattr__(self, name):
        if name.startswith("_"):  # its own, not yet set (e.g. while it is made)
            raise AttributeError(name)
        return getattr(self._cursor, name)

    def __setattr__(self, name, value):  # e.g. fast_executemany
        setattr(self._cursor, name, value)


class _Column(NamedTuple):
    """A column of a SQL Server table, from the catalog of its database."""

    name: str
    kind: str  # in lower case, e.g. nvarchar; for an alias type the type it is based on
    clr: bool  # a CLR type such as geography or hierarchyid
    size: int  # in bytes, -1 for max
    precision: int
    scale: int
    collation: str | None


_LENGTH_TYPES = ("char", "varchar", "nchar", "nvarchar", "binary", "varbinary")
_FRACTION_TYPES = ("datetime2", "time", "datetimeoffset")
_DECIMAL_TYPES = ("decimal", "numeric", "money", "smallmoney")
_PLAIN_TYPES = (
    "bit", "tinyint", "smallint", "int", "bigint", "float", "real", "money", "smallmoney", "date", "datetime",
    "smalldatetime", "uniqueidentifier",
)  # fmt: skip
# The size and digits the driver gives a parameter of these types (with n digits of a second, the
# types with fractions are 1 + n longer)
_FIXED_SIZES = {
    "bit": (1, 0), "tinyint": (3, 0), "smallint": (5, 0), "int": (10, 0), "bigint": (19, 0), "float": (53, 0),
    "real": (24, 0), "money": (19, 4), "smallmoney": (10, 4), "date": (10, 0), "datetime": (23, 3),
    "smalldatetime": (16, 0), "uniqueidentifier": (36, 0),
}  # fmt: skip
_FRACTION_SIZES = {"datetime2": 19, "time": 8, "datetimeoffset": 26}
_SQL_SS_TIMESTAMPOFFSET = -155  # the driver's own type for datetimeoffset (pyodbc has no name for it)
_DATETIMEOFFSET_SIZE = 26  # the size of a datetimeoffset(0) in cursor.description (1 + n more with n digits)
_KIND = re.compile(r"\s*([a-z0-9]+)\s*(?:\(\s*(\d+|max)\s*(?:,\s*(\d+)\s*)?\))?", re.IGNORECASE)


def _kind(sql_type: str) -> tuple[str, int, int]:
    """A SQL Server type in parts: name in lower case, size or precision (0 for max), scale.
    DECIMAL(10, 2) is ("decimal", 10, 2), datetime2(0) ("datetime2", 0, 0); datetime2, time
    and datetimeoffset without a size have 7 digits, money and smallmoney 4 decimal places."""
    match = _KIND.match(sql_type)
    if match is None:
        return "", 0, 0
    kind = match[1].lower()
    if match[2] is None:
        size = 7 if kind in _FRACTION_TYPES else 0
    else:
        size = 0 if match[2].lower() == "max" else int(match[2])
    return kind, size, 4 if kind in ("money", "smallmoney") else int(match[3] or 0)


@functools.cache
def _odbc_types() -> dict[str, int]:
    """The ODBC type the driver gives a parameter for a column of each SQL Server type, except
    for char and varchar: their texts go as Unicode (nchar, nvarchar), which the server converts
    to the column's code page."""
    import pyodbc

    return {
        "bit": pyodbc.SQL_BIT, "tinyint": pyodbc.SQL_TINYINT, "smallint": pyodbc.SQL_SMALLINT,
        "int": pyodbc.SQL_INTEGER, "bigint": pyodbc.SQL_BIGINT, "float": pyodbc.SQL_FLOAT, "real": pyodbc.SQL_REAL,
        "decimal": pyodbc.SQL_DECIMAL, "numeric": pyodbc.SQL_NUMERIC, "money": pyodbc.SQL_DECIMAL,
        "smallmoney": pyodbc.SQL_DECIMAL, "date": pyodbc.SQL_TYPE_DATE, "datetime": pyodbc.SQL_TYPE_TIMESTAMP,
        "smalldatetime": pyodbc.SQL_TYPE_TIMESTAMP, "datetime2": pyodbc.SQL_TYPE_TIMESTAMP,
        "time": pyodbc.SQL_SS_TIME2, "datetimeoffset": _SQL_SS_TIMESTAMPOFFSET, "uniqueidentifier": pyodbc.SQL_GUID,
        "char": pyodbc.SQL_WCHAR, "varchar": pyodbc.SQL_WVARCHAR, "nchar": pyodbc.SQL_WCHAR,
        "nvarchar": pyodbc.SQL_WVARCHAR, "binary": pyodbc.SQL_BINARY, "varbinary": pyodbc.SQL_VARBINARY,
        "text": pyodbc.SQL_WVARCHAR, "ntext": pyodbc.SQL_WVARCHAR,  # as nvarchar(max): see _LONG_TEXT_TYPES
    }  # fmt: skip


_CODE_PAGE_TYPES = ("char", "varchar")  # texts in the code page of the column's collation
# The old types of long texts. The driver describes them with a size of 2 GB, and with
# fast_executemany pyodbc then takes that much memory for each value ("There is not enough memory"):
# values of every type for them go as Unicode texts of any length, as for nvarchar(max), which the
# server converts.
_LONG_TEXT_TYPES = ("text", "ntext")
_UNICODE_LIMIT = 4000  # the longest nchar or nvarchar parameter; a longer one is nvarchar(max)


def _binding(sql_type: str) -> tuple[int, int, int] | None:
    """The type, size and digits of a parameter for a column of this SQL Server type
    (``setinputsizes``): bound so, a value is written as the driver writes it into such a
    column of a table, e.g. a datetime2(0) without its fraction; texts for a char or varchar
    column go as Unicode, which the server converts to the column's code page (and refuses
    when too long for it). None for other types."""
    kind, size, scale = _kind(sql_type)
    code = _odbc_types().get(kind)
    if code is None:
        return None
    if kind in _FRACTION_SIZES:
        return code, _FRACTION_SIZES[kind] + (size + 1 if size else 0), size
    if kind in _FIXED_SIZES:
        return code, *_FIXED_SIZES[kind]
    if kind in ("decimal", "numeric"):
        return code, size or 18, scale
    if kind in _CODE_PAGE_TYPES and size > _UNICODE_LIMIT:  # e.g. varchar(8000)
        return _odbc_types()["nvarchar"], 0, 0
    return code, size, 0  # texts and binary values; 0: max


def _round_half_away(series: pl.Series, scale: int) -> pl.Series:
    """Decimals rounded to ``scale`` decimal places, half away from zero, as SQL Server rounds
    them for a decimal column (1.005 is 1.01, -0.005 is -0.01; a cast would round half to even)."""
    try:  # under a fixed name: Polars' Series methods read a name like ^a.*$ as a pattern that finds nothing
        rounded = series.alias("value").round(scale, mode="half_away_from_zero")
        return rounded.cast(pl.Decimal(38, scale)).alias(series.name)
    except pl.exceptions.ComputeError:  # 38 digits that a carry would make 39 before the cast
        exact = decimal.Context(prec=100, rounding=decimal.ROUND_HALF_UP)  # ties away from zero
        step = decimal.Decimal(1).scaleb(-scale, context=exact)
        values = [None if v is None else v.quantize(step, context=exact) for v in series.to_list()]
        try:
            return pl.Series(series.name, values, dtype=pl.Decimal(38, scale))
        except Exception:  # too large for the column after all: SQL Server says so
            return series


def _cut_seconds(
    df: pl.DataFrame,
    types: dict[str, str],
    texts: dict[str, tuple[int, str, int]] | None = None,
    names: dict[str, str] | None = None,
    table: str = "",
) -> list[str]:
    """The warning for the columns of ``df`` whose values lose digits of a second in their SQL
    Server column, the same in every mode (``fit_frame`` cuts them as the driver does): one
    warning, with how many values and an example for each of the first 3 columns, then how many
    more columns there are: a datetime2, time or datetimeoffset column holds as many digits as its type says
    (datetime2(0) whole seconds, 08:00:00.5 is 08:00:00 there), a datetime column 3 (in steps of
    1/300 s, to which SQL Server rounds them), and the driver passes no digits below a microsecond
    (a column of 7 digits holds them). ``texts``: for the columns read from texts
    (``_read_date_texts``), how many of them had such digits, the first one and its row. ``names``:
    the columns of the ``table``, by column of ``df``. The values of all other columns are looked
    at in one pass."""
    texts = texts or {}
    held = {name: digits for name, dtype in df.schema.items() if (digits := _second_digits(types.get(name, ""), dtype == pl.String))}
    by_value = [
        n for n, dtype in df.schema.items() if n in held and n not in texts and (isinstance(dtype, pl.Datetime) or dtype == pl.Time)
    ]
    counted = {name: (count, "", -1) for name, count in zip(by_value, df.select(
        (col(name).dt.nanosecond() % 10 ** (9 - held[name][0]) != 0).fill_null(False).sum() for name in by_value
    ).row(0) if by_value else ())}  # fmt: skip
    found = counted | {name: texts[name] for name in held if name in texts}
    cut = [name for name in df.columns if name in found and found[name][0]]  # in the order of the columns
    entries = []
    for name in cut[:_CUT_SHOWN]:  # an example for the first ones only: one warning says it all
        count, shown, row = found[name]
        kept, size = held[name]
        values = df[name].alias("value")  # a fixed name, as in _round_half_away
        if row < 0:  # the first value with such digits
            row = int((values.dt.nanosecond() % 10 ** (9 - kept) != 0).fill_null(False).arg_true()[0])
            shown = pl.select(dtypes.text_expr(pl.lit(values.slice(row, 1)), values.dtype)).item()
        else:
            shown = f"'{shown}'"
        written = _digits_of_a_second(values.slice(row, 1), kept)
        written = written if written.dtype == pl.String else pl.select(dtypes.text_expr(pl.lit(written), written.dtype)).to_series()
        driver = kept == 6 and size == 7
        entries.append((name, count, _CUT_TO.get(kept, f"{kept} digits of a second"), f"{shown} is {written[0]}", driver, kept))
    if not entries:
        return []
    if len(cut) == 1:
        name, count, unit, example, driver, kept = entries[0]
        values_were = "1 value was" if count == 1 else f"{count:,} values were"
        if driver:
            why = "SQL Server's driver passes no smaller parts of a second"
        else:
            holds = "no parts of a second" if kept == 0 else f"{kept} digit{'s' if kept != 1 else ''} of a second"
            why = f"the column {(names or {}).get(name, name)} of {table} holds {holds} ({types[name]})"
        return [f"{name}: {values_were} cut to {unit} (e.g. {example}): {why}"]
    listed = [
        f"{name}: {count:,} value{'s' if count != 1 else ''} to {unit} (e.g. {example}; "
        + ("SQL Server's driver passes no smaller parts of a second" if driver else types[name])
        + ")"
        for name, count, unit, example, driver, _kept in entries
    ]
    more = len(cut) - len(entries)
    if more:
        listed.append(f"and {more:,} more column{'s' if more != 1 else ''}")
    return [f"Parts of a second that their columns don't hold were cut in {len(cut):,} columns: " + "; ".join(listed) + "."]


_CUT_TO = {0: "whole seconds", 1: "1 digit of a second", 3: "milliseconds", 6: "whole microseconds"}
_CUT_SHOWN = 3  # columns that the warning about cut digits of a second shows, each with an example
# The digits of a second in a text that the rule of Change type reads: after seconds of one or two
# digits (08:30:15.25, 8:5:3.25)
_FRACTION = r":[0-9]{1,2}\.([0-9]+)"


def _second_digits(sql_type: str, text: bool) -> tuple[int, int] | None:
    """The digits of a second that a value keeps in a SQL Server column of this type, and those the
    column holds, or None for a column that holds no digits of its own (date, and datetime and
    smalldatetime, whose steps of 1/300 s and whole minutes are their documented rule: no warning
    there). The driver passes date-times and times to the microsecond; a ``text`` (for a
    datetimeoffset column, and for datetime2(7) and time(7), ``_sent_as_text``) keeps all 7."""
    base, size, _scale = _kind(sql_type)
    if base not in _FRACTION_TYPES:
        return None
    return (size if text else min(size, 6)), size


def _sent_as_text(base: str, size: int) -> bool:
    """Whether a text for a column of this type goes to SQL Server as a text (``_read_date_texts``
    checks and writes it): for datetimeoffset, to keep its offset, and for datetime2(7) and time(7),
    to keep the 7th digit of a second, which the driver doesn't pass in a date-time or time."""
    return base == "datetimeoffset" or (base in ("datetime2", "time") and size == 7)


def _seven_digit_texts(read: pl.Series, texts: pl.Series, base: str) -> pl.Series:
    """Date-times or times that were read from ``texts``, for a datetime2(7) or time(7) column,
    written as the text the driver and the server read the same in every language
    (2026-10-01 08:30:15.1234567, 08:30:15.1234567: SQL Server reads year-month-day for datetime2
    whatever its date format; the driver refuses a T there), with the digits of a second of the
    text, up to 7."""
    digits = texts.str.strip_chars().str.extract(_FRACTION, 1).fill_null("").str.slice(0, 7).str.pad_end(7, "0")
    whole = read.dt.strftime("%H:%M:%S" if base == "time" else "%Y-%m-%d %H:%M:%S")
    return pl.select(pl.lit(whole) + "." + pl.lit(digits)).to_series().alias(read.name)


def _digits_of_a_second(values: pl.Series, digits: int) -> pl.Series:
    """Date-times, times or texts of datetimeoffset values (``_offset_texts``) cut to ``digits``
    digits of a second."""
    if values.dtype == pl.String:
        return values.str.replace(rf"(:[0-9]{{2}}\.[0-9]{{{digits}}})[0-9]*", "${1}").str.replace(r":([0-9]{2})\.(\s|$)", ":${1}${2}")
    step = 10 ** (9 - digits)  # nanoseconds
    if values.dtype == pl.Time:
        return (values.cast(pl.Int64) // step * step).cast(pl.Time)
    return values.dt.truncate(f"{step}ns") if values.dtype.time_unit == "ns" else values.dt.truncate(f"{max(step // 1000, 1)}us")


def _data_binding(series: pl.Series) -> tuple[int, int, int]:
    """The type, size and digits for the driver that fit the values of ``series``."""
    import pyodbc

    dtype = series.dtype
    if dtype.is_integer():
        return pyodbc.SQL_BIGINT, 0, 0
    if dtype.is_float():
        return pyodbc.SQL_DOUBLE, 0, 0
    if isinstance(dtype, pl.Decimal):
        return pyodbc.SQL_DECIMAL, min(dtype.precision or 38, 38), dtype.scale
    if dtype == pl.Boolean:
        return pyodbc.SQL_BIT, 0, 0
    if dtype == pl.Date:
        return pyodbc.SQL_TYPE_DATE, 10, 0
    if dtype == pl.Datetime:
        return pyodbc.SQL_TYPE_TIMESTAMP, 27, 7
    if dtype == pl.Time:
        return pyodbc.SQL_SS_TIME2, 16, 7
    if dtype == pl.Binary:
        longest = series.alias("value").bin.size().max() or 1  # a fixed name, as in _round_half_away
        return pyodbc.SQL_VARBINARY, longest if longest <= 8000 else 0, 0  # 0: as long as needed
    return pyodbc.SQL_WVARCHAR, text_length(series), 0


def _offset_text(column: pl.Expr, digits: int) -> pl.Expr:
    """Date-times with a time zone as the text the driver reads for a datetimeoffset column
    with ``digits`` digits of a second, with their zone's offset: 2026-07-01 08:30:00.5 +02:00."""
    parts = [column.dt.strftime("%Y-%m-%d %H:%M:%S")]
    if digits:
        parts += [pl.lit("."), column.dt.strftime("%6f").str.slice(0, digits)]
    return pl.concat_str([*parts, pl.lit(" "), column.dt.strftime("%:z")])


def _driver_offset_text(column: pl.Expr, digits: int) -> pl.Expr:
    """Texts for a datetimeoffset column in the form the driver reads: 2026-10-01T08:30:00+02:00
    (as imports give them) is 2026-10-01 08:30:00 +02:00, Z is +00:00, and digits of a second
    the column doesn't hold are cut, as the driver cuts them from date-times."""
    text = column.str.strip_chars().str.replace(r"^(\d{4}-\d{2}-\d{2})T", "${1} ")
    text = text.str.replace(r"(\d)\s*Z$", "${1} +00:00").str.replace(r"(\d)([+-]\d{2}:\d{2})$", "${1} ${2}")
    time = r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})"
    if digits:
        return text.str.replace(time + rf"(\.\d{{0,{digits}}})\d*", "${1}${2}")
    return text.str.replace(time + r"\.\d*", "${1}")


# As in SSMS: GO, a repeat count (GO 3) and a -- comment may follow
_GO = re.compile(r"^[ \t]*GO(?:[ \t]+([1-9][0-9]*))?[ \t]*;?[ \t]*(?:--[^\r\n]*)?\r?$", re.IGNORECASE | re.MULTILINE)


def _batches(sql: str) -> list[tuple[str, int]]:
    """T-SQL split at its GO lines, with how many times each batch runs; a GO line inside a text
    or a comment doesn't count."""
    return [(sql[start:end], count) for start, end, count in _batch_spans(sql)]


def _batch_spans(sql: str) -> list[tuple[int, int, int]]:
    """The batches of ``_batches`` as (start, end, count)."""
    quoted = parameters.sql_spans(sql, **_SQLSERVER_SQL)
    starts = [begin for begin, _end, _kind in quoted]
    batches, start = [], 0
    for line in _GO.finditer(sql):
        k = bisect.bisect_right(starts, line.start()) - 1  # the last text or comment that starts before it
        if k < 0 or quoted[k][1] <= line.start():
            batches.append((start, line.start(), int(line[1] or 1)))
            start = line.end()
    batches.append((start, len(sql), 1))
    return batches


def _transaction_count_message(error: Exception, line: int = 0, batch: str = "") -> str | None:
    """A plain message for the check that ``run_script`` appends to a batch (the ``batch``, which
    ends in ``line``, ended the transaction of the step with COMMIT or ROLLBACK), and for SQL
    Server's error 266 (a procedure, or an EXEC of a text, ended the command's transaction, or
    began one of its own and didn't end it, which the step's rollback then undoes); None for other
    errors. After a COMMIT that ended it, what came before is stored; a ROLLBACK ends every open
    transaction, also one that the command began itself, and undoes what the step's transaction
    held. Either way, the rest of that batch ran without a transaction. The batch's own words
    tell which one it was (``_ending_words``); with both, or neither, the message says both."""
    text = " ".join(str(arg) for arg in error.args)
    for record in _RECORDS.split(text):  # a procedure's first: the batch's own check comes after it
        if not re.search(r"\(266\)", record):
            continue
        counts = [int(number) for number in re.findall(r"=\s*(\d+)", record)]
        if len(counts) < 2:
            break
        if counts[1] < counts[0]:
            return (
                "A procedure or an EXEC in the command ended its transaction (with COMMIT or ROLLBACK), so the step "
                "stopped: what the command changed before may be kept, and so is what the rest of that batch changed. "
                f"{APP_NAME} runs the command in one transaction, so that a failed command changes nothing: leave COMMIT "
                "and ROLLBACK out of the procedures and texts that it runs, too."
            )
        return (
            "A procedure or an EXEC in the command began a transaction and didn't end it, so the step stopped and its "
            f"changes were undone. {APP_NAME} runs the command in one transaction: leave BEGIN TRAN and COMMIT out of "
            "the procedures and texts that it runs, too."
        )
    if _TRANSACTION_ENDED not in text:
        return None
    words = _ending_words(batch)
    if words == {"COMMIT"}:
        return (
            f"The command ended the transaction of the step with a COMMIT that has no BEGIN TRAN of its own (in the batch "
            f"that ends in line {line}), so the step stopped. What the command changed before the COMMIT is stored, and "
            f"the statements after it in that batch ran without a transaction. {APP_NAME} runs the command in one "
            "transaction, so that a failed command changes nothing: end only the transactions that the command begins "
            "itself."
        )
    if words == {"ROLLBACK"}:
        return (
            f"The command ended the transaction of the step with a ROLLBACK (in the batch that ends in line {line}), so "
            "the step stopped. A ROLLBACK ends every open transaction, also the step's, and undoes what the command "
            "changed in it up to there; the statements after it in that batch ran without a transaction. To undo only a "
            "part of the command, use SAVE TRAN name and ROLLBACK TRAN name."
        )
    return (
        f"The command ended the transaction of the step with a COMMIT or ROLLBACK (in the batch that ends in line {line}), "
        "so the step stopped. A COMMIT without a BEGIN TRAN of the command stores what the command changed before it; "
        "a ROLLBACK ends every open transaction, also the step's, and undoes it. The statements after it in that batch "
        f"ran without a transaction. {APP_NAME} runs the command in one transaction: end only the transactions that the "
        "command begins itself, and to undo only a part of it, use SAVE TRAN name and ROLLBACK TRAN name."
    )


def _ending_words(batch: str) -> set[str]:
    """COMMIT and ROLLBACK, as far as statements of ``batch`` hold them (texts, comments and [names]
    don't count; both are reserved words, so they are no other names)."""
    tokens = _tsql_tokens(batch, parameters.sql_spans(batch, **_SQLSERVER_SQL))
    found = {match[0].upper() for kind, start in tokens if kind == "name" and (match := _WORD.match(batch, start))}
    return found & {"COMMIT", "ROLLBACK"}


# What each batch of a command ends with: SQL Server stops the batch when the transaction of the
# step has ended (_transaction_count_message knows it by its text). The ; ends the batch's last
# statement, also one written without it.
_TRANSACTION_ENDED = "The transaction of the step ended in this batch."
_TRANSACTION_CHECK = "\n;IF @@TRANCOUNT < {count} THROW 50000, N'" + _TRANSACTION_ENDED + "', 1;"
# Statements that must be alone in their batch: nothing can follow them there
_ALONE = ("PROC", "PROCEDURE", "FUNCTION", "TRIGGER", "VIEW", "RULE", "DEFAULT", "SCHEMA")
_WORD = re.compile(r"(?:[^\W\d]|#)[\w@$#]*")


def _alone_in_batch(batch: str) -> bool:
    """Whether ``batch`` creates or alters a procedure, function, trigger, view, rule, default or
    schema: such a statement must be alone in its batch (texts and comments before it don't count)."""
    tokens = _tsql_tokens(batch, parameters.sql_spans(batch, **_SQLSERVER_SQL))
    words = [(found[0].upper() if kind == "name" and (found := _WORD.match(batch, start)) else "") for kind, start in tokens[:4]]
    if words[:1] == ["CREATE"] and words[1:3] == ["OR", "ALTER"]:
        return words[3:4] != [] and words[3] in _ALONE
    return words[:1] in (["CREATE"], ["ALTER"]) and words[1:2] != [] and words[1] in _ALONE


# SQL Server's messages come in records joined by "; [", each with the driver's prefix and the
# error's number: "[23000] [Microsoft][ODBC Driver 17 for SQL Server][SQL Server]… (2627) (SQLExecute)"
_RECORDS = re.compile(r";\s*(?=\[[0-9A-Z]{5}\])")
_PREFIX = re.compile(r"^\s*\[[0-9A-Z]{5}\]\s*\[[^\]]*\]\[[^\]]*\](?:\[SQL Server\])?")
_NUMBER = re.compile(r"\s*\((-?\d+)\)(?:\s*\(SQL\w+\))?\s*$")
_TERMINATED = 3621  # "The statement has been terminated."
# How SQL Server names a #temporary table in tempdb: its name, underscores and a number
_TEMPORARY_NAME = re.compile(r"(#[^\s'\"\]]*?)_{8,}[0-9A-F]{12}(?![0-9A-Za-z_])")


def _diagnostics(text: str) -> str:
    """The records of a SQL Server error message without the driver's prefixes and the error
    numbers, each once. The same error for many rows (a duplicate key value per row) is shown
    once, with how many more there are. A #temporary table is named as it was created."""
    text = _TEMPORARY_NAME.sub(r"\1", text)
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
        _stoppable(connection, ctx)
        return connection

    def connect_read_only(self, settings, ctx):
        """Nothing is written, also not by the recovery after an interrupted change, which SQLite
        starts on the first read (a journal that came with the file can name any file to delete).
        A WAL database is read with its -wal file, which stays as it is. An empty file with a -wal
        file beside it stops here: for SQLite that -wal file belongs to no database, and it deletes
        it on the first read, also from a read-only connection. No ATTACH: SQLite opens an
        attached file for writing, also from a read-only connection. Only PRAGMAs that read."""
        path = ctx.resolve_path(settings.file)
        if not path.is_file():
            raise ActionError(f"SQLite file not found: {path}")
        wal = Path(f"{path}-wal")
        if path.stat().st_size == 0 and wal.exists():
            raise ActionError(
                f"This SQLite file is empty, and the file {wal.name} lies beside it. SQLite would delete that file on "
                f"the first read, so {APP_NAME} doesn't open the database. Put the complete database file back, or move "
                f"{wal.name} to another folder."
            )
        connection = sqlite3.connect(_read_only_uri(path), uri=True, autocommit=False)
        connection.setlimit(sqlite3.SQLITE_LIMIT_ATTACHED, 0)
        connection.set_authorizer(_authorize_reading)
        _stoppable(connection, ctx)
        return connection

    def error_message(self, error):
        text = str(error)
        code = getattr(error, "sqlite_errorname", "")
        if text == "too many attached databases - max 0":
            return "An import can't ATTACH a database"
        if code in ("SQLITE_READONLY_ROLLBACK", "SQLITE_READONLY_RECOVERY"):
            return (
                f"This SQLite database was left unfinished: a program stopped during a change. {APP_NAME} only reads "
                "it and doesn't repair it. To repair it, open it once in its own program, then calculate again. "
                f"If {APP_NAME} is the program that writes this file, run a step that writes to it once: "
                "Export to database or Run SQL command."
            )
        if code == "SQLITE_READONLY":
            return "This SQLite database can't be changed: the file is read-only, or this step only reads"
        if code == "SQLITE_AUTH":
            return "An import can use only PRAGMAs that read, such as table_info"
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
        if isinstance(dtype, pl.Decimal):  # written as texts, which SQLite turns into numbers for such columns
            precision = dtype.precision or 38
            if dtype.scale == 0 and precision <= 18:
                return "INTEGER"
            return "REAL" if precision <= 15 else "TEXT"  # REAL keeps 15 digits, TEXT all of them
        if dtype == pl.Binary:
            return "BLOB"
        return "TEXT"  # also dates: SQLite keeps them as ISO text (2025-01-31)

    def same(self, left, right):
        return f"{left} IS {right}"

    def matching(self, keys, left="x", right="s"):
        """Several keys as row values, (x.a, x.b) IS (s.a, s.b): SQLite compares them value by
        value, as a chain of x.a IS s.a AND …, and uses the index on them as well. It reads such a
        chain as a tree as deep as there are keys, and stops at 1,000 levels (in a DELETE already at
        500 keys)."""
        if len(keys) == 1:
            return super().matching(keys, left, right)
        return "({}) IS ({})".format(*(", ".join(f"{side}.{self.quote(k)}" for k in keys) for side in (left, right)))

    def temp_table(self, name):
        return "CREATE TEMP TABLE", name

    def prepare_frame(self, df):
        """Dates and times as ISO text, the way SQLite stores them (2025-01-31 08:30:00, with
        six digits for a fraction of a second: 08:30:00.500000, and nine for one with
        nanoseconds: 08:30:00.000000001); a date-time with a time zone in that zone's local time,
        without the zone. Exact decimals (Decimal) as text with all their digits (1.50): SQLite
        turns it into a number for a column of numbers (REAL, INTEGER, NUMERIC, DECIMAL(10,2)),
        as it does with every text that is a number, also when keys compare, and keeps it for a
        TEXT column. Each value gets the same text in every export, whatever the other values
        and the column's unit are, so that keys find their rows."""
        df = super().prepare_frame(df)
        changes = []
        for name, dtype in df.schema.items():
            column = col(name)
            if isinstance(dtype, pl.Decimal):
                changes.append(column.cast(pl.String))
            elif dtype == pl.Date:
                changes.append(column.dt.strftime("%Y-%m-%d"))
            elif dtype == pl.Datetime:
                changes.append(_iso_text(column, "%Y-%m-%d %H:%M:%S"))
            elif dtype == pl.Time:
                changes.append(_iso_text(column, "%H:%M:%S"))
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


def _iso_text(column: pl.Expr, seconds: str) -> pl.Expr:
    """A date-time or time as SQLite keeps it, in the strftime form ``seconds`` (up to the whole
    seconds): without a fraction for whole seconds (08:30:00), with six digits for one of whole
    microseconds (08:30:00.500000, as SQLite exports have always written date-times, so that keys
    find those rows), with nine for one with nanoseconds (08:30:00.000000001)."""
    nanoseconds = column.dt.nanosecond()  # the whole fraction of the second
    return (
        pl.when(nanoseconds == 0)
        .then(column.dt.strftime(seconds))
        .when(nanoseconds % 1000 == 0)
        .then(column.dt.strftime(seconds + "%.6f"))
        .otherwise(column.dt.strftime(seconds + "%.9f"))
    )


# Steps of SQLite's own program. Mostly that is some microseconds, but a row takes only a few
# steps, and one step can make a value of megabytes.
_STEPS_BETWEEN_CHECKS = 1_000


def _stoppable(connection: sqlite3.Connection, ctx) -> None:
    """Stop ends a query or a command that runs, also one that gives no row for a long time: then
    the step ends as stopped, and its transaction is rolled back."""

    def stopped() -> bool:
        try:
            ctx.check_cancelled()
        except Cancelled:
            return True
        return False

    connection.set_progress_handler(stopped, _STEPS_BETWEEN_CHECKS)


# The PRAGMAs of a read-only connection. All others stay out, also those of later versions of SQLite:
# they can act on the whole program, which a rollback doesn't undo (hard_heap_limit, soft_heap_limit,
# threads), on files (temp_store_directory, data_store_directory, wal_checkpoint) or on the connection.
_READING_PRAGMAS = frozenset({
    "application_id", "auto_vacuum", "collation_list", "compile_options", "data_version", "database_list", "encoding",
    "foreign_key_check", "foreign_key_list", "freelist_count", "function_list", "index_info", "index_list", "index_xinfo",
    "integrity_check", "journal_mode", "module_list", "page_count", "page_size", "pragma_list", "quick_check",
    "schema_version", "table_info", "table_list", "table_xinfo", "user_version",
})  # fmt: skip
# Those that an argument tells what to read. For the others it would be a new value.
_PRAGMAS_WITH_ARGUMENT = frozenset({
    "foreign_key_check", "foreign_key_list", "index_info", "index_list", "index_xinfo", "integrity_check", "quick_check",
    "table_info", "table_list", "table_xinfo",
})  # fmt: skip


def _authorize_reading(action, name, argument, *_where):
    """SQLite asks this for each part of a statement. A PRAGMA as a table (pragma_table_info('x'))
    comes by here too, when it runs."""
    if action != sqlite3.SQLITE_PRAGMA:
        return sqlite3.SQLITE_OK
    allowed = _READING_PRAGMAS if argument is None else _PRAGMAS_WITH_ARGUMENT
    return sqlite3.SQLITE_OK if name.lower() in allowed else sqlite3.SQLITE_DENY


def _read_only_uri(path: Path) -> str:
    """The URI that opens ``path`` for reading only. %xx stands for all that a URI reads itself
    (%, #, ?). SQLite takes the name after file:// for a server and refuses it, so a share
    (//server/share) goes after an empty one."""
    name = path.absolute().as_posix()
    return f"file://{quote(name if name.startswith('/') else '/' + name)}?mode=ro"


def _migrate_import(params: dict, from_version: int) -> dict:
    """Version 1 detected numbers with a decimal point only, and no amounts or percentages: a step
    saved by it keeps that (Point), so that it gives the same results as before, also to steps
    after it that changed such texts themselves."""
    if from_version < 2:
        params.setdefault("decimal", ".")
    return params


def _import_endpoints(p) -> list[Endpoint]:
    if p.mode == "table":
        found = Endpoint(endpoints.IN, endpoints.DATABASE, place=p.connection, name=p.table, setting="table")
    else:
        found = Endpoint(endpoints.IN, endpoints.SQL, place=p.connection, name=p.query, setting="query")
    return [found] if found.place.strip() or found.name.strip() else []


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
        P.Choice(
            "decimal",
            "Decimal separator",
            [("auto", "Detect automatically"), (".", "Point: 1,234.56"), (",", "Comma: 1.234,56")],
            visible_if={"detect_types": True},
            help="Detect automatically: each column's own, as the CSV import detects it, also in amounts (1.234,50 €) "
            "and percentages (12,5%). Point or Comma: numbers and dates with that separator, no amounts or percentages.",
        ),
    ],
    version=2,
    migrate=_migrate_import,
    summary=lambda p: f"Import {p.table if p.mode == 'table' else 'SQL query'} from {p.connection}",
    endpoints=_import_endpoints,
)
def import_database(df, p, ctx):
    """Reads a whole table or view, or the result of a SQL query written in the database's own
    SQL dialect (T-SQL for SQL Server). Imports also run while editing, e.g. when a project is
    opened, so they change nothing: a SQLite file is opened for reading only, and what a query
    changes in SQL Server is rolled back (a query that commits by itself keeps its changes).
    Numbers keep all their digits: a column with numbers of more digits than a decimal number
    keeps (about 15), or with whole numbers too big for a whole-number column, stays a column of
    exact decimals. A SQL Server datetimeoffset column becomes date-times in UTC, the moments it
    holds; SQL Server's driver reads date-times to the microsecond, and for a column with 7
    digits of a second the step says that a 7th digit is cut. "Detect numbers and dates in text
    columns" finds them in text, with the
    decimal separator of each column as the CSV import does (also in amounts and percentages),
    or only numbers and dates with a chosen one; steps saved before it could be chosen use the
    point, as they did."""

    def between():  # there may be no end of rows: a view can make them up
        ctx.check_cancelled()
        ctx.check_memory()

    with ctx.database(p.connection, read_only=True) as db:
        try:
            out = db.read_table(p.table, between) if p.mode == "table" else db.read(p.query, between=between)
        finally:
            with contextlib.suppress(Exception):  # e.g. a driver in autocommit mode: nothing to roll back
                db.rollback()
        described = getattr(db.raw, "described", None)  # SQL Server (_Stoppable): the columns of what was read
    if described:
        out, warnings = _as_described(out, described)
        for warning in warnings:
            ctx.warn(warning)
    if not p.detect_types:
        return out
    if p.decimal == "auto":
        return _detect_types(out, ctx)
    return dtypes.infer_types(out, decimal=p.decimal, warn=ctx.warn)  # only numbers and dates, as version 1 with a point


def _as_described(df: pl.DataFrame, description) -> tuple[pl.DataFrame, list[str]]:
    """A SQL Server import with what ``cursor.description`` tells about its columns: a column of
    texts that has no value is text, as before (a datetimeoffset(0) column is described as a text
    of 26 characters, so ``dtype_for`` lets the values decide); a column of 7 digits of a second
    (datetime2(7), time(7), datetimeoffset(7)) gets a warning when its values have parts of a
    second: the driver reads them only to the microsecond, so a 7th digit, if any, is cut."""
    names = unique_names([str(d[0]) if d[0] else f"Column {i}" for i, d in enumerate(description, 1)])
    changes, cut = [], {}  # the columns of 7 digits with parts of a second: an example of each
    for name, described in zip(names, description):
        if name not in df.columns:
            continue
        column, type_code, scale = df[name].alias("value"), described[1], described[5]  # a fixed name, as in _round_half_away
        if column.dtype == pl.Null and type_code is str:
            changes.append(column.cast(pl.String).alias(name))
        elif scale == 7 and (isinstance(column.dtype, pl.Datetime) or column.dtype == pl.Time):
            fraction = (column.dt.nanosecond() != 0).fill_null(False)
            if fraction.any():
                cut[name] = pl.select(dtypes.text_expr(pl.lit(column.filter(fraction).head(1)), column.dtype)).item()
    if not cut:
        return (df.with_columns(changes) if changes else df), []
    listed, first = list(cut), next(iter(cut))
    if len(listed) > 5:
        shown = ", ".join(listed[:5]) + f" and {len(listed) - 5:,} more"
    else:
        shown = " and ".join([", ".join(listed[:-1]), listed[-1]]) if len(listed) > 1 else first
    example = cut[first] if len(listed) == 1 else f"{first}: {cut[first]}"
    warning = (
        f"{shown}: the column{'s hold' if len(listed) > 1 else ' holds'} 7 digits of a second, but SQL Server's driver reads "
        f"only 6 (to the microsecond), so a 7th digit, if any, is cut (e.g. in {example}). Export to database finds such rows by "
        "these keys: it compares them to the microsecond."
    )
    return (df.with_columns(changes) if changes else df), [warning]


def _detect_types(df: pl.DataFrame, ctx) -> pl.DataFrame:
    """Numbers and dates in the text columns, as the CSV import finds them, also amounts
    (1.234,50 €) and percentages (12,5%). A column whose values show a decimal comma (123,40)
    has one, and the log names it; the others have a decimal point (1.000 is one)."""
    _all, own = detection.guess_decimal(df, ".")
    typed = detection.detect_types(df, {name: own.get(name, ".") for name in df.columns}, ctx.warn)
    schema = typed.schema  # once: each call builds the types of all columns again
    commas = [name for name, decimal in own.items() if decimal == "," and schema[name].is_numeric()]
    if commas:
        more = f" and {len(commas) - 5:,} more" if len(commas) > 5 else ""
        ctx.log(f"Decimal comma in {', '.join(commas[:5])}{more}")
    return typed


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


# What each mode does to the database table, in a few words (the mode of its endpoint)
_EXPORT_WORDS = {
    "insert": "add rows",
    "replace": "replace all rows",
    "replace_keys": "replace rows by {keys}",
    "replace_where": "replace rows where …",
    "update": "update rows by {keys}",
    "upsert": "update or add rows by {keys}",
    "delete": "delete rows by {keys}",
}


def _export_endpoints(p) -> list[Endpoint]:
    if not (p.connection.strip() or p.table.strip()):
        return []
    mode = _EXPORT_WORDS.get(p.mode, "").format(keys=", ".join(p.key_columns) or "?")
    how = "mode" if mode else ""
    return [
        Endpoint(
            endpoints.OUT, endpoints.DATABASE, place=p.connection, name=p.table, mode=mode, setting="table", mode_setting=how
        )
    ]


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
    endpoints=_export_endpoints,
)
def export_database(df, p, ctx):
    """Writes the table into a database table, in one transaction: if anything fails, the
    database stays as it was.

    Add the rows, replace all of them, or replace just the rows with the same key values (e.g.
    the dates in the data: reloading a day replaces that day). Update rows by key columns, or
    update them and add the new ones. Columns are matched by name; a missing table is created
    with types that fit the data. Values are stored as their columns hold them, the same way in
    every mode (08:00:01.5 is 08:00:01 in a SQL Server datetime2(0) column, and the step says how
    many values were cut; spaces at the end of a text that don't fit are left out), and key values
    compare as their columns do (e.g. with their collation). On SQL Server, a text for a date or
    time column is read as Change type / format reads it without a format (31.12.2025 08:30,
    2025-12-31): a text it can't read stops the export. Date-times with a time zone are written
    in that zone's local time, without the zone (08:30 in Berlin stays 08:30), as in the Excel
    export; into a SQL Server datetimeoffset column with the zone's offset (08:30 +02:00), and
    there a date-time without a zone gets the offset of this PC's time zone on its day. Binary
    values for a SQL Server column of text are hex text (0x0102FF), as the CSV export writes
    them. Runs only when the project is run.
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
            _check_storable(df, db)
            definitions = ", ".join(f"{db.quote(c)} {db.connector.sql_type(df[c])}" for c in df.columns)
            db.execute(f"CREATE TABLE {table} ({definitions})")
            created, existing = True, list(df.columns)
        names = _target_names(df.columns, existing, p)
        types = db.connector.column_types(db.raw, p.table)
        keys = _key_names(p, names) if p.mode in _KEYED else []
        data = df.select(cols(names))
        if p.mode == "delete":  # it writes nothing: only the key values count
            data = data.select(cols(c for c, n in names.items() if n in keys))
        _check_whole_numbers(data, names, types, p.table)
        _check_storable(data, db)
        data = _fit_decimals(data, names, types, p.table, db.connector.id)
        data = _fit_whole_numbers(data, names, types, p.table, db.connector.id)
        data, hexed = _binary_texts(data, names, types, db.connector.id)
        finding = keys if p.mode in ("update", "delete") else []  # key values that only find rows
        data = _fit_lengths(data, names, types, p.table, finding, hexed)
        # After the rows that _fit_lengths leaves out: the cut digits of a second are counted, with
        # their rows, in the rows that are written
        data, read = _read_date_texts(data, names, types, p.table, db.connector.id)
        kinds = {name.lower(): kind for name, kind in types.items()}
        fitted = {c: kinds[names[c].lower()] for c in data.columns if names[c].lower() in kinds}
        cut = _cut_seconds(data, fitted, read, names, p.table) if db.connector.id == "sqlserver" else []
        data = db.connector.fit_frame(data, fitted)
        if p.mode in ("update", "upsert"):
            _check_unique(data, p.key_columns)  # as stored: 08:00:01.2 and .7 are one second in datetime2(0)
        fitted_data = data
        data = db.connector.prepare_frame(data)

        added = deleted = updated = 0
        by_type = {"kinds": types} if db.connector.id == "sqlserver" else {}  # how SQL Server compares the keys
        if p.mode == "replace":
            # Not TRUNCATE: it would reset IDENTITY numbering, needs more rights and fails with
            # foreign keys. Deleting 1M rows takes about 1 s; adding them again about 9 s.
            deleted = db.execute(f"DELETE FROM {table}")
        elif p.mode == "replace_where":
            deleted = db.execute(f"DELETE FROM {table} WHERE {p.condition}")
        elif p.mode in ("replace_keys", "delete"):
            staged = data.select(cols(c for c, n in names.items() if n in keys)).unique()
            stage = _stage(db, staged, names, keys, db.connector.stage_types(db.raw, p.table), ctx)
            deleted = db.execute(db.connector.delete_sql(table, stage, keys, **by_type))
        elif p.mode in ("update", "upsert"):
            sqlserver = db.connector.id == "sqlserver"
            # The number of each row, for the examples of the checks: not in a stage that would get more
            # columns than a table can have (the checks then show a key as the stage holds it)
            row = _unused("tafl_row", names.values()) if sqlserver and data.width < _MOST_COLUMNS else None
            stage = _stage(db, data, names, keys, db.connector.stage_types(db.raw, p.table), ctx, row)
            if sqlserver:  # as SQL Server compares them: abc and ABC are one key in a CI column
                _check_unique_as_compared(db, stage, row, fitted_data, [names[k] for k in p.key_columns], p.key_columns)
                _check_seventh_digits(db, table, stage, row, fitted_data, names, p.key_columns, types, p.table)
            others = [n for n in names.values() if n not in keys]
            if others:
                updated = db.execute(db.connector.update_sql(table, stage, others, keys, **by_type))
            if p.mode == "upsert":
                added = db.execute(db.connector.insert_missing_sql(table, stage, list(names.values()), keys, **by_type))
        if p.mode in ("insert", "replace", "replace_keys", "replace_where"):
            added = _insert(db, table, data, list(names.values()), ctx, fitted)
    if created:  # only now: a failed export creates no table
        ctx.log(f"Created table {p.table}")
    parts = [f"{what} {count:,} row{'s' if count != 1 else ''}" for what, count in (
        ("deleted", deleted), ("updated", updated), ("added", added)) if count > 0]
    ctx.log(f"{p.table}: " + (", ".join(parts) if parts else "nothing changed"))
    for warning in cut:
        ctx.warn(warning)
    return df


def _temporary(connector_id: str, parts: list[str]) -> bool:
    """A table that disappears with the connection: temp.x in SQLite, #x (and ##x) in SQL Server."""
    if connector_id == "sqlite":
        return len(parts) > 1 and parts[0].lower() == "temp"
    return connector_id == "sqlserver" and bool(parts) and parts[-1].startswith("#")


_WHOLE_NUMBER_TYPES = {"tinyint", "smallint", "int", "integer", "bigint", "bit"}


def _check_whole_numbers(df: pl.DataFrame, names: dict[str, str], types: dict[str, str], table: str) -> None:
    """The database would cut off decimals that go into a whole-number column (1.5 -> 1) without
    a word: refuse instead, also for exact decimals (1.50). Only the columns of ``df``, those the
    export writes."""
    kinds = {name.lower(): kind for name, kind in types.items()}
    for source in df.columns:
        target, column = names[source], df[source].alias("value")  # a fixed name, as in _round_half_away
        exact = isinstance(column.dtype, pl.Decimal)
        if (column.dtype.is_float() or exact) and kinds.get(target.lower()) in _WHOLE_NUMBER_TYPES:
            values = column.drop_nulls() if exact else column.drop_nulls().drop_nans()
            if values.len() and (values != values.round(0)).any():
                example = values.filter(values != values.round(0))[0]
                raise ActionError(
                    f"The column {target} of {table} holds whole numbers ({kinds[target.lower()]}), but {source} has "
                    f"decimals (e.g. {example}): they would be cut off. Round them first, e.g. ROUND([{source}]), "
                    "or change the column's type in the database."
                )


# The databases of the built-in connectors, as messages name them. A plugin's connector decides
# for itself what its database can store.
_BUILT_IN = {"sqlite": "SQLite", "sqlserver": "SQL Server"}
_KINDS = ("time spans", "lists", "records", "values of a plugin's own type")
_BIGINT = (-(2**63), 2**63 - 1)
_BIGINT_RANGE = f"from {_BIGINT[0]:,} to {_BIGINT[1]:,}"


def _check_storable(df: pl.DataFrame, db) -> None:
    """What SQLite and SQL Server can't hold stops the export with a plain message (their drivers
    stop with a technical one): columns of time spans, lists, records and values of a plugin's
    own type, and in SQLite more columns than a table holds."""
    database = _BUILT_IN.get(db.connector.id)
    if database is None:
        return
    if db.connector.id == "sqlite" and df.width > (most := db.raw.getlimit(sqlite3.SQLITE_LIMIT_COLUMN)):
        raise ActionError(
            f"The table has {df.width:,} columns, but a SQLite table holds at most {most:,}: leave some out before "
            "this step (Keep, remove or reorder columns)"
        )
    kinds = {name: kind for name, dtype in df.schema.items() if (kind := _kind_of(dtype))}
    if kinds:
        names, more = list(kinds), len(kinds) - 5
        shown = names[:5] + ([f"{more:,} more column{'s' if more > 1 else ''}"] if more > 0 else [])
        raise ActionError(
            f"{database} can't store {_listed([k for k in _KINDS if k in kinds.values()], 'or')}: {_listed(shown, 'and')} "
            f"{'has' if len(names) == 1 else 'have'} them. Leave {'this column' if len(names) == 1 else 'these columns'} "
            "out before this step (Keep, remove or reorder columns)."
        )


def _kind_of(dtype: pl.DataType) -> str:
    """The words for a type that the built-in databases can't store, or "" for one they can."""
    if dtype == pl.Duration:
        return _KINDS[0]
    if dtype.is_object():
        return _KINDS[3]
    if dtype.is_nested():
        return _KINDS[2] if isinstance(dtype, pl.Struct) else _KINDS[1]
    return ""


def _listed(words: list[str], last: str) -> str:
    return words[0] if len(words) == 1 else ", ".join(words[:-1]) + f" {last} " + words[-1]


def _fit_whole_numbers(df: pl.DataFrame, names: dict[str, str], types: dict[str, str], table: str, connector_id: str) -> pl.DataFrame:
    """Whole numbers for the columns they go into. A SQL Server column of whole numbers takes those
    of its own range (bit 0 and 1, tinyint 0 to 255, …): others stop the export with a plain
    message (the driver's names no column). Whole numbers beyond 64 bits (UInt64 from Parquet, 128
    bits from a plugin) the drivers can't pass as numbers ("Python int too large to convert to
    SQLite INTEGER", "int too big to convert"): SQLite holds none of them; in SQL Server, a text
    column gets their digits as text (its length is checked as for other texts), a decimal column
    with enough digits gets them exactly, and other columns stop the export with a plain message:
    float and real (they would round them), a decimal with fewer digits, money, sql_variant (it takes
    them as text) and columns of other things than numbers, such as dates (no advice to change them)."""
    kinds = {name.lower(): kind for name, kind in types.items()}
    changes = []
    for source in df.columns:
        target, column = names[source], df[source].alias("value")  # a fixed name, as in _round_half_away
        if not column.dtype.is_integer() or connector_id not in _BUILT_IN:
            continue
        kind = kinds.get(target.lower(), "")
        base, size, scale = _kind(kind)
        whole = connector_id == "sqlserver" and base in _WHOLE_RANGES  # a column of whole numbers: its own range
        if not whole and column.dtype not in (pl.UInt64, pl.Int128, pl.UInt128):
            continue  # within 64 bits: the driver passes them, and the database converts them
        least, most = _WHOLE_RANGES[base] if whole else _BIGINT
        low, high = column.min(), column.max()  # Python's whole numbers: exact
        if high is None or least <= low <= high <= most:
            continue
        example = high if high > most else low
        if connector_id == "sqlite":
            raise ActionError(
                f"SQLite stores whole numbers {_BIGINT_RANGE}, and {source} has others (e.g. {example}). Change {source} "
                "to text before this step (Change type / format): a TEXT column keeps all their digits."
            )
        if kind == "sql_variant":  # SQL Server can't change it to another type, but it takes their digits as text
            raise ActionError(
                f"The column {target} of {table} ({kind}) can't take whole numbers outside {_BIGINT[0]:,} to {_BIGINT[1]:,}, "
                f"and {source} has some (e.g. {example}). Change {source} to text before this step (Change type / format)."
            )
        if base in _NO_NUMBER_KINDS:
            raise ActionError(f"The column {target} of {table} ({kind}) can't take whole numbers, and {source} has some (e.g. {example}).")
        if base not in _TEXT_KINDS and max(len(str(abs(low))), len(str(abs(high)))) > _MOST_DIGITS:
            # beyond every decimal: whatever the column's type, only a text column takes them
            longest = low if len(str(abs(low))) >= len(str(abs(high))) else high
            raise ActionError(f"The column {target} of {table} ({kind}): " + _beyond_decimals(source, longest, existing=True))
        if whole:
            held = "0 and 1" if base == "bit" else f"whole numbers from {least:,} to {most:,}"
            raise ActionError(
                f"The column {target} of {table} holds {held} ({kind}), but {source} has others (e.g. {example}). Change "
                f"the column's type in the database, e.g. to {_fitting_whole_type(low, high, column.dtype)}."
            )
        if base in _TEXT_KINDS:
            changes.append(column.cast(pl.String).alias(source))  # the digits of each number
            continue
        if base in ("decimal", "numeric"):
            digits = max(len(str(abs(low))), len(str(abs(high))))
            if digits <= size - scale:
                continue  # the column holds them exactly
            example = low if len(str(abs(low))) >= len(str(abs(high))) else high  # the one with the most digits
            raise ActionError(
                f"The column {target} of {table} holds numbers of up to {size - scale} digits before the decimal point "
                f"({kind}), but {source} has longer ones (e.g. {example}). Make the column wider in the database, e.g. "
                "decimal(38, 0)."
            )
        if base in ("float", "real"):
            raise ActionError(
                f"The column {target} of {table} holds decimal numbers ({kind}), which keep about {15 if base == 'float' else 7} "
                f"digits, but {source} has whole numbers with more (e.g. {example}), which it would round. Change the "
                "column's type in the database, e.g. to decimal(38, 0)."
            )
        raise ActionError(
            f"The column {target} of {table} ({kind}) can't take whole numbers outside {_BIGINT[0]:,} to {_BIGINT[1]:,}, "
            f"and {source} has some (e.g. {example}). Change the column's type in the database, e.g. to decimal(38, 0)."
        )
    return df.with_columns(changes) if changes else df


def _fit_decimals(df: pl.DataFrame, names: dict[str, str], types: dict[str, str], table: str, connector_id: str) -> pl.DataFrame:
    """Exact decimals (Decimal) for the SQL Server columns they go into, with the checks whole
    numbers have (the driver's messages named no column: "Converting decimal loses precision",
    or a long text about the TDS protocol): for a column of whole numbers they become whole
    numbers (``_check_whole_numbers`` refused those with decimal places before), so that
    ``_fit_whole_numbers`` checks their range; a decimal or numeric column takes those with at most
    as many digits before the point as it holds, after they are rounded to its decimal places
    (999.995 is 1000.00 in a decimal(5,2)), money and smallmoney those of their range; a column of
    text gets the same text on every PC (1.50), whose length ``_fit_lengths`` checks. Other
    columns are left as they were."""
    if connector_id != "sqlserver":
        return df
    kinds = {name.lower(): kind for name, kind in types.items()}
    changes = []
    for source, dtype in df.schema.items():
        if not isinstance(dtype, pl.Decimal):
            continue
        target, column = names[source], df[source].alias("value")  # a fixed name, as in _round_half_away
        kind = kinds.get(target.lower(), "")
        base, size, scale = _kind(kind)
        if base in _WHOLE_RANGES:  # whole numbers here (others were refused): within 128 bits for 38 digits
            whole = pl.Int64 if (dtype.precision or 38) - dtype.scale <= 18 else pl.Int128
            changes.append(column.cast(whole).alias(source))
        elif base in _TEXT_KINDS:
            changes.append(pl.select(dtypes.text_expr(pl.lit(column), dtype)).to_series().alias(source))
        elif base in ("decimal", "numeric", "money", "smallmoney"):
            rounded = _round_half_away(column, scale) if dtype.scale > scale else column
            low, high = rounded.min(), rounded.max()
            if high is None:
                continue
            if base in ("money", "smallmoney"):
                least, most = _MONEY_RANGES[base]
                if least <= low and high <= most:
                    continue
                example = column[rounded.arg_max() if high > most else rounded.arg_min()]
                raise ActionError(
                    f"The column {target} of {table} holds numbers from {least:,} to {most:,} ({kind}), but {source} has "
                    f"others (e.g. {example}). Change the column's type in the database, e.g. to decimal(38, 4)."
                )
            digits = size - scale  # before the point
            if max(_digits_before_point(low), _digits_before_point(high)) <= digits:
                continue
            example = column[rounded.abs().arg_max()]
            held = "numbers between -1 and 1"
            if digits:
                held = f"numbers of up to {digits} digit{'s' if digits != 1 else ''} before the decimal point"
            raise ActionError(
                f"The column {target} of {table} holds {held} ({kind}), but {source} has "
                f"{'longer ones' if digits else 'others'} (e.g. {example}). Make the column wider in the database, e.g. "
                f"decimal(38, {scale})."
            )
    return df.with_columns(changes) if changes else df


_DATE_KINDS = ("date", "datetime", "datetime2", "smalldatetime", "time", "datetimeoffset")
# For the message about texts that are no date or time: what the column holds, what the texts are
# not, how to write them
_DATE_TEXTS = {
    "date": ("dates", "no date", "31.12.2025, 2025-12-31 or 20251231, or change them to dates first (Change type / format, with "
             "their format)"),
    "time": ("times", "no time", "08:30, 08:30:15 or 08:30:15.250"),
    "datetimeoffset": (
        "dates and times with their offset from UTC", "no date and time", "31.12.2025 08:30 +01:00 or 2025-12-31T08:30:00+01:00"
    ),
    "": ("dates and times", "no date and time", "31.12.2025 08:30 or 2025-12-31T08:30:00, or change them to date-times first "
         "(Change type / format, with their format)"),
}  # fmt: skip
# ISO 8601 beside the rule of Change type: a date (2026-10-01, also 20261001 after a replace), with a
# time after T or a space (08:30, 08:30:15, 08:30:15.1234567), and maybe an offset (Z, +02:00)
_ISO = r"^([0-9]{4}-[0-9]{2}-[0-9]{2})(?:[T ]([0-9]{2}:[0-9]{2})(:[0-9]{2}(?:\.[0-9]{1,9})?)?)?\s*(?:Z|[+-][0-9]{2}(?::?[0-9]{2})?)?$"


def _iso_datetimes(texts: pl.Series) -> pl.Series:
    """Texts in a form of ISO 8601, which means the same in every language, as date-times to the
    microsecond: a date (2026-10-01 or 20261001), maybe with a time after T or a space (with or
    without seconds, up to 9 digits of a second); an offset after it (Z, +02:00) is left out, so
    the date-time is the local time that the text names. The export reads them beside the rule
    of Change type, which doesn't read all of them (2026-10-01T08:30, 20261001): CSV detection
    must not take 20261001 for a date. Empty for other texts, also for a time without a date."""
    text = texts.str.strip_chars().str.replace(r"^([0-9]{4})([0-9]{2})([0-9]{2})([T ].*)?$", "${1}-${2}-${3}${4}")
    parts = text.str.extract_groups(_ISO).struct.unnest()  # the columns "1", "2" and "3"
    joined = parts.select(pl.col("1") + " " + pl.col("2").fill_null("00:00") + pl.col("3").fill_null(":00")).to_series()
    return joined.str.to_datetime("%Y-%m-%d %H:%M:%S%.f", strict=False, time_unit="us").alias(texts.name)


def _or_else(first: pl.Series, other: pl.Series) -> pl.Series:
    """``first``, and ``other`` where it is empty."""
    return first.zip_with(first.is_not_null(), other)


def _on_1900(times: pl.Series) -> pl.Series:
    """Times as date-times on 1 January 1900 (to the microsecond), the date that SQL Server gives a
    time alone in a datetime, smalldatetime or datetime2 column: CAST('08:30' AS datetime) is
    1900-01-01 08:30. Tables made before SQL Server had a time type keep times so."""
    return pl.select(pl.lit(dt.date(1900, 1, 1)).dt.combine(pl.lit(times), time_unit="us")).to_series().alias(times.name)


def _read_date_texts(
    df: pl.DataFrame, names: dict[str, str], types: dict[str, str], table: str, connector_id: str
) -> tuple[pl.DataFrame, dict[str, tuple[int, str, int]]]:
    """Texts for SQL Server date and time columns, read in every mode with the rule of Change type
    / format without a format (31.12.2025, 2025-12-31, with or without a time; 08:30:15.250 for a
    time; a datetimeoffset column also takes an offset after the time, +01:00 or Z), and as ISO
    8601 (``_iso_datetimes``: 2026-10-01T08:30, 20261001; an offset in it is left out for a column
    without one; a time column takes the time of a date and time). A time alone (08:30,
    08:30:15.5) for a datetime, smalldatetime or datetime2 column is on 1 January 1900, as SQL
    Server converts it (``_on_1900``: old tables keep times so, and Update and Upsert wrote them so
    before). Forms whose meaning depends on the language or the date format (12/31/2025, 08:30 PM)
    are no dates here. Before, the
    driver read them in Add (ISO only), while the keyed modes sent them as texts that the server
    read with its session's date format: 01.10.2026 was 10 January there. Dates, date-times and
    times are then written as such (for a datetimeoffset column: as a text the driver reads, with
    the offset as it was); empty texts are empty values, as with Change type; a text that the rule
    can't read stops the export with a message that names the column and the text. Returns the
    data and, for the columns read so whose column holds parts of a second, how many texts have
    more digits of a second than it keeps, the first of them and its row (``_cut_seconds`` warns of
    them: the texts themselves are not kept)."""
    if connector_id != "sqlserver":
        return df, {}
    kinds = {name.lower(): kind for name, kind in types.items()}
    changes, texts = [], {}
    for source, dtype in df.schema.items():
        if not (dtype == pl.String or isinstance(dtype, (pl.Categorical, pl.Enum))):
            continue
        target = names[source]
        kind = kinds.get(target.lower(), "")
        base = _kind(kind)[0]
        if base not in _DATE_KINDS:
            continue
        text = df[source].cast(pl.String).alias("value")  # a fixed name, as in _round_half_away
        if base == "date":
            read = _or_else(dtypes.parse_dates(text), _iso_datetimes(text).dt.date())
        elif base == "time":  # the time of a date and time in ISO 8601 too (of a date: 00:00)
            read = _or_else(dtypes.parse_times(text), _iso_datetimes(text).dt.time())
        elif base == "datetimeoffset":
            read = _offset_texts(text)
        else:  # datetime, smalldatetime, datetime2: a time alone is on 1 January 1900, as SQL Server converts it
            read = _or_else(_or_else(dtypes.parse_datetimes(text), _iso_datetimes(text)), _on_1900(dtypes.parse_times(text)))
        failed =(text.is_not_null() & read.is_null() & (text.str.strip_chars() != "")).fill_null(False)
        if count := int(failed.sum()):
            held, no_such, forms = _DATE_TEXTS.get(base, _DATE_TEXTS[""])
            more = f"; {count:,} texts like it" if count > 1 else ""
            raise ActionError(
                f"The column {target} of {table} holds {held} ({kind}), but {source} has texts that are {no_such} "
                f"(e.g. “{text.filter(failed)[0]}”{more}). Write them as {forms}."
            )
        size = _kind(kind)[1]
        if base != "datetimeoffset" and _sent_as_text(base, size):  # all 7 digits of a second
            read = _seven_digit_texts(read, text, base)
        changes.append(read.alias(source))
        if digits := _second_digits(kind, _sent_as_text(base, size)):
            cut = text.str.contains(rf":[0-9]{{1,2}}\.[0-9]{{{digits[0]}}}[0-9]*[1-9]").fill_null(False)  # as _FRACTION
            first = int(cut.arg_true()[0]) if (count := int(cut.sum())) else -1
            texts[source] = (count, text[first].strip() if count else "", first)
    return df.with_columns(changes), texts


# An offset from UTC after a time: Z, +01:00, +0100 or +01
_ZONE = r"(Z|[+-][0-9]{2}(?::?[0-9]{2})?)$"
_TIME_BEFORE_ZONE = r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2}(?:\.[0-9]+)?)?\s*"


def _offset_texts(texts: pl.Series) -> pl.Series:
    """Texts for a datetimeoffset column as the driver reads them in every mode: the date and
    time read as Change type reads them, with the digits of a second as they were, and the offset
    after them as it was (2026-10-01 08:30:00.1234567 +02:00); one without an offset gets none
    (the driver gives it the offset of this PC's time zone on its day). Empty where the rule
    can't read a text or its offset is beyond what SQL Server holds (±14:00)."""
    frame = pl.DataFrame({"text": texts.str.strip_chars()})
    text, zone = pl.col("text"), pl.col("zone")
    found = text.str.extract(_TIME_BEFORE_ZONE + _ZONE, 1)
    moment = pl.when(found.is_not_null()).then(text.str.replace(r"\s*" + _ZONE, "")).otherwise(text)
    frame = frame.with_columns(found.alias("zone"), moment.alias("moment"))
    moments = frame["moment"].alias("value")
    frame = frame.with_columns(_or_else(dtypes.parse_datetimes(moments), _iso_datetimes(moments)).alias("read"))
    hours = zone.str.slice(1, 2).cast(pl.Int32, strict=False)
    minutes = zone.str.replace(r"^[+-][0-9]{2}:?", "").cast(pl.Int32, strict=False).fill_null(0)
    offset = (
        pl.when(zone == "Z")
        .then(pl.lit(" +00:00"))
        .when((minutes < 60) & (hours * 60 + minutes <= 14 * 60))
        .then(pl.lit(" ") + zone.str.slice(0, 3) + pl.lit(":") + minutes.cast(pl.String).str.zfill(2))
    )
    fraction = pl.col("moment").str.extract(r":[0-9]{2}(\.[0-9]+)$", 1).fill_null("")
    written = pl.col("read").dt.strftime("%Y-%m-%d %H:%M:%S") + fraction
    return frame.select(pl.when(zone.is_null()).then(written).otherwise(written + offset).alias(texts.name)).to_series()


# What SQL Server's money and smallmoney columns hold
_MONEY_RANGES = {
    "money": (decimal.Decimal("-922337203685477.5808"), decimal.Decimal("922337203685477.5807")),
    "smallmoney": (decimal.Decimal("-214748.3648"), decimal.Decimal("214748.3647")),
}


def _digits_before_point(value: decimal.Decimal) -> int:
    """The digits of a decimal before its point: 0 for 0.5, 3 for -123.45."""
    whole = abs(int(value))
    return len(str(whole)) if whole else 0


_TEXT_KINDS = ("char", "varchar", "nchar", "nvarchar", "text", "ntext")
# Columns of other things than numbers (dates and times, ids, xml, binary values, …): SQL Server changes
# none of them to a type of numbers (binary and varbinary only while they are empty)
_NO_NUMBER_KINDS = (
    "date", "time", "datetime", "datetime2", "smalldatetime", "datetimeoffset", "uniqueidentifier", "xml", "binary", "varbinary",
    "image", "timestamp", "hierarchyid", "geography", "geometry",
)  # fmt: skip
_MOST_DIGITS = 38  # of a SQL Server decimal


def _beyond_decimals(source: str, example: int, existing: bool = False) -> str:
    """The message for whole numbers of more digits than any SQL Server decimal holds. A new table
    gets a column of text for them when the data's column is text; an ``existing`` column must be
    changed to text in the database too (a text for its decimal column would be too long)."""
    keep = f"change the column's type in the database to text, e.g. varchar(40), and {source}" if existing else f"change {source}"
    return (
        f"SQL Server's decimals hold at most {_MOST_DIGITS} digits, and {source} has whole numbers with more (e.g. "
        f"{example}). Keep them in a text column: {keep} to text before this step (Change type / format)."
    )


# What SQL Server's columns of whole numbers hold (the driver passes only these to them, also to bit)
_WHOLE_RANGES = {"bit": (0, 1), "tinyint": (0, 255), "smallint": (-(2**15), 2**15 - 1), "int": (-(2**31), 2**31 - 1), "bigint": _BIGINT}


def _fitting_whole_type(low: int, high: int, dtype: pl.DataType) -> str:
    """A SQL Server type for whole numbers from ``low`` to ``high``, as a new table gets it."""
    for kind in ("int", "bigint"):
        if _WHOLE_RANGES[kind][0] <= low and high <= _WHOLE_RANGES[kind][1]:
            return kind
    return "decimal(20, 0)" if dtype == pl.UInt64 else "decimal(38, 0)"


def _binary_texts(
    df: pl.DataFrame, names: dict[str, str], types: dict[str, str], connector_id: str
) -> tuple[pl.DataFrame, frozenset[str]]:
    """Binary values for a SQL Server column of text (char, varchar, nchar, nvarchar, text or
    ntext) as hex text, 0x0102FF, as the CSV export writes them, in every mode: the driver would
    pass their bytes as the characters of a text, without a word (b"ab" was 扡 in an nvarchar
    column). Also the names of the columns of ``df`` changed so."""
    if connector_id != "sqlserver":
        return df, frozenset()
    kinds = {name.lower(): kind for name, kind in types.items()}
    hexed = [
        c for c, dtype in df.schema.items() if dtype == pl.Binary and _kind(kinds.get(names[c].lower(), ""))[0] in _TEXT_KINDS
    ]
    if hexed:
        df = df.with_columns((pl.lit("0x") + col(c).bin.encode("hex").str.to_uppercase()).alias(c) for c in hexed)
    return df, frozenset(hexed)


_TEXT_TYPE = re.compile(r"(n?)(?:var)?char\((\d+)\)( collate \S+_utf8)?")  # a UTF-8 varchar counts bytes
_BINARY_TYPE = re.compile(r"(?:var)?binary\((\d+)\)")
_ASTRAL = r"[\x{10000}-\x{10FFFF}]"  # outside the Basic Multilingual Plane, e.g. an emoji


def _fit_lengths(
    df: pl.DataFrame,
    names: dict[str, str],
    types: dict[str, str],
    table: str,
    finding: list[str],
    hexed: frozenset[str] = frozenset(),
) -> pl.DataFrame:
    """Texts and binary values for columns of a limited length, such as nvarchar(50). Spaces
    at the end of a text that don't fit are cut, as SQL Server does (it also leaves them out
    when it compares texts). A value that is still too long would stop the export with a
    message that names no column: refuse with the column and an example first. In the key
    columns ``finding`` (columns of the table), whose values only find rows, such a row is left
    out instead: no row of the table can have that value. ``hexed``: the columns whose binary
    values became hex text (``_binary_texts``)."""
    kinds = {name.lower(): kind for name, kind in types.items()}
    changes, unmatched = [], pl.repeat(False, df.height, eager=True)
    for source in sorted(df.columns, key=lambda c: names[c] not in finding):  # the rows left out first
        target, column = names[source], df[source].alias("value")  # a fixed name, as in _round_half_away
        kind = kinds.get(target.lower(), "")
        text, binary = _TEXT_TYPE.fullmatch(kind), _BINARY_TYPE.fullmatch(kind)
        if text and source in hexed:  # hex digits: one character each (a byte, a UTF-16 unit), no spaces
            size, lengths, unit = int(text[2]), column.str.len_bytes(), "characters"
        elif text and column.dtype == pl.String:
            size, wide, utf8 = int(text[2]), bool(text[1]), bool(text[3])
            lengths = _text_lengths(column, wide, utf8)
            kept = _text_lengths(column.str.strip_chars_end(" "), wide, utf8)
            cut = ((lengths > size) & (kept <= size)).fill_null(False)
            if cut.any():  # the whole text and as many spaces as fit (a space takes one)
                keep = (column.str.len_chars().cast(pl.Int64) - (lengths.cast(pl.Int64) - size)).clip(0)
                column = column.str.slice(0, keep).zip_with(cut, column)
                changes.append(column.alias(source))
                lengths = _text_lengths(column, wide, utf8)
            unit = "bytes" if utf8 else "characters"
        elif binary and column.dtype == pl.Binary:
            size, lengths, unit = int(binary[1]), column.bin.size(), "bytes"
        else:
            continue
        too_long = (lengths > size).fill_null(False)
        if target in finding:
            unmatched = unmatched | too_long
            continue
        too_long = too_long & ~unmatched
        if not too_long.any():
            continue
        example, length = column.filter(too_long)[0], lengths.filter(too_long)[0]
        if binary:
            raise ActionError(
                f"The column {target} of {table} holds values of up to {size:,} bytes ({kind}), but {source} has "
                f"longer ones (e.g. 0x{example[:20].hex().upper()}{'…' if len(example) > 20 else ''}, {length:,} bytes). "
                "Shorten them first, or make the column longer in the database."
            )
        shown = example if len(example) <= 40 else example[:40] + "…"
        if source in hexed:  # hex digits take a character, also a byte in UTF-8
            raise ActionError(
                f"The column {target} of {table} holds texts of up to {size:,} characters ({kind.split(' collate ')[0]}), but "
                f"{source} has binary values whose hex text is longer than this column holds (e.g. {shown}, {length:,} "
                "characters). Make the column longer in the database."
            )
        if unit == "bytes":  # a varchar of a UTF-8 collation
            raise ActionError(
                f"The column {target} of {table} holds texts of up to {size:,} bytes in UTF-8 ({kind.split(' collate ')[0]}), "
                f"but {source} has longer ones (e.g. “{shown}”, {length:,} bytes: letters such as ä take two). Shorten "
                "them first, or make the column longer in the database."
            )
        raise ActionError(
            f"The column {target} of {table} holds texts of up to {size:,} characters ({kind}), but {source} has "
            f"longer ones (e.g. “{shown}”, {length:,} characters). Shorten them first, e.g. LEFT([{source}], {size}), "
            "or make the column longer in the database."
        )
    return df.with_columns(changes).filter(~unmatched)


def _text_lengths(texts: pl.Series, wide: bool, utf8: bool) -> pl.Series:
    """The lengths of texts as SQL Server counts them: an nchar or nvarchar column UTF-16 code
    units (an emoji takes two), a char or varchar of a UTF-8 collation bytes (ä takes two), other
    char and varchar columns characters."""
    if utf8:
        return texts.str.len_bytes()
    return texts.str.len_chars() + texts.str.count_matches(_ASTRAL) if wide else texts.str.len_chars()


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
    """Updating needs one row per key value: otherwise it isn't clear which row wins. The example
    is the first key value in the order of the rows that occurs more than once (for 1, 2, 2, 1
    that is 1), the same in every run."""
    count = "len"
    while count in keys:  # a key column may have the name
        count = f"_{count}_"
    repeated = df.group_by(cols(keys), maintain_order=True).len(name=count).filter(col(count) > 1)
    if repeated.height:
        _not_unique(repeated.height, [(k, repeated[k][0]) for k in keys])


def _not_unique(count: int, example: list[tuple[str, object]]) -> None:
    """The message of ``_check_unique``: ``count`` key values occur more than once, e.g. the
    values of ``example`` (column, value)."""
    shown = ", ".join(f"{name} = {value}" for name, value in example)
    values = "1 key value occurs" if count == 1 else f"{count:,} key values occur"
    raise ActionError(
        f"The key columns don't identify the rows: {values} more than once "
        f"(e.g. {shown}). Remove the duplicates first, or choose more key columns."
    )


def _check_unique_as_compared(db, stage: str, row: str | None, data: pl.DataFrame, columns: list[str], keys: list[str]) -> None:
    """``_check_unique`` for SQL Server, as the table's columns compare the keys: the rows are in
    ``stage`` (``_stage``), with their number in the column ``row``, and the keys in its
    ``columns`` (of ``keys`` in ``data``). Keys that differ in the data can be one key value there:
    abc and ABC in a column of a case-insensitive collation, e and é in an accent-insensitive one,
    a and "a " (spaces at the end don't count), 08:00:00.001 and 08:00:00 in a datetime (steps of
    1/300 s), 1.001 and 1.004 in a decimal(10,2), a GUID in upper and lower case. The example is
    the data's first row of a key value that occurs more than once, as ``_check_unique`` gives it;
    without ``row`` (a stage of the most columns a table can have), such a key value as the stage
    holds it."""
    grouped = ", ".join(db.quote(c) for c in columns)
    if row is None:
        found = db.read(f"SELECT TOP 1 COUNT(*) OVER (), {grouped} FROM {stage} GROUP BY {grouped} HAVING COUNT(*) > 1")
        if found.height:
            values = found.row(0)
            _not_unique(int(values[0]), list(zip(keys, values[1:])))
        return
    found = db.read(
        f"SELECT TOP 1 COUNT(*) OVER () AS repeated, MIN({db.quote(row)}) AS first_row FROM {stage} "
        f"GROUP BY {grouped} HAVING COUNT(*) > 1 ORDER BY MIN({db.quote(row)})"
    )
    if found.height:
        first = int(found["first_row"][0])
        _not_unique(int(found["repeated"][0]), [(k, data[k][first]) for k in keys])


def _check_seventh_digits(
    db, table: str, stage: str, row: str, data: pl.DataFrame, names: dict[str, str], keys: list[str], types: dict[str, str], name: str
) -> None:
    """Keys of a column with 7 digits of a second are compared to the microsecond (``matching``):
    updating needs one row of the table per key there too. Two rows that differ only in the 7th
    digit (08:00:01.1234567 and 08:00:01.1234568) would both get the values of one row of the data:
    refuse, with the data's first such key as the example; without ``row`` (a stage of the most
    columns a table can have, or of the distinct keys of Delete and Replace by keys), such a key as
    the stage holds it. ``table``: quoted; ``name``: as given."""
    kinds = {column.lower(): kind for column, kind in types.items()}
    seven = [names[k] for k in keys if _seven_digits(kinds.get(names[k].lower(), ""))]
    if not seven:
        return
    x, s = (db.quote(alias) for alias in _aliases([*names.values(), *([row] if row else [])]))
    differ = " OR ".join(f"MIN({x}.{db.quote(c)}) <> MAX({x}.{db.quote(c)})" for c in seven)
    joined = f"FROM {stage} AS {s} INNER JOIN {table} AS {x} ON {db.connector.matching([names[k] for k in keys], x, s, types)}"
    if row is None:
        grouped = ", ".join(f"{s}.{db.quote(names[k])}" for k in keys)
        found = db.read(f"SELECT TOP 1 COUNT(*) OVER (), {grouped} {joined} GROUP BY {grouped} HAVING {differ}")
        if not found.height:
            return
        count, *example = found.row(0)
        shown = ", ".join(f"{k} = {value}" for k, value in zip(keys, example))
    else:
        found = db.read(
            f"SELECT TOP 1 COUNT(*) OVER () AS keys, {s}.{db.quote(row)} AS first_row {joined} GROUP BY {s}.{db.quote(row)} "
            f"HAVING {differ} ORDER BY {s}.{db.quote(row)}"
        )
        if not found.height:
            return
        first, count = int(found["first_row"][0]), int(found["keys"][0])
        shown = ", ".join(f"{k} = {data[k][first]}" for k in keys)
    values = "1 key value finds" if count == 1 else f"{count:,} key values find"
    columns = ", ".join(k for k in keys if names[k] in seven)
    raise ActionError(
        f"The key columns don't identify the rows of {name}: {values} rows that differ only in the 7th digit of a second "
        f"of {columns} (e.g. {shown}). SQL Server's driver passes date-times only to the microsecond, so {APP_NAME} "
        "compares these keys to the microsecond. Choose more key columns."
    )


_MOST_COLUMNS = 1024  # the most columns a SQL Server table can have, a #temporary one too


def _unused(name: str, names) -> str:
    """``name``, or with a number after it (name2, name3, …): no name of ``names``, also as SQL
    Server may compare them (``_folded``)."""
    taken = {_folded(n) for n in names}
    number, candidate = 1, name
    while _folded(candidate) in taken:
        number += 1
        candidate = f"{name}{number}"
    return candidate


def _stage(
    db, data: pl.DataFrame, names: dict[str, str], keys: list[str], own: dict[str, str], ctx, row: str | None = None
) -> str:
    """A temporary table with the data, named like the columns of the database table and
    indexed on the key columns (then matching rows takes a moment, not minutes). Its columns
    have the types of the table's own columns where the database tells them (``own``, from
    ``stage_types``): values arrive there as they would in the table (a datetime holds steps of
    1/300 s, a varchar only the characters of its code page), and keys compare as the table's
    columns do. Otherwise they have types that fit the data. ``row`` (SQL Server): a column of
    that name gets the number of each row (0, 1, …), in the order of the data."""
    own = {name.lower(): kind for name, kind in own.items()}
    create, stage = db.connector.temp_table(f"tafl_stage_{uuid.uuid4().hex[:8]}")
    kinds = {c: own.get(names[c].lower()) or db.connector.sql_type(data[c], temporary=True) for c in data.columns}
    columns = [names[c] for c in data.columns]
    if row is not None:  # sent with the rows: an IDENTITY column made adding them half as slow again
        data, kinds, columns = data.with_row_index(row), {row: "BIGINT", **kinds}, [row, *columns]
    definitions = ", ".join(f"{db.quote(name)} {kind}" for name, kind in zip(columns, kinds.values()))
    db.execute(f"{create} {stage} ({definitions})")
    _insert(db, stage, data, columns, ctx, kinds, temporary=True)
    db.execute(db.connector.index_sql(stage, keys))
    return stage


def _insert(
    db, table: str, data: pl.DataFrame, names: list[str], ctx, types: dict[str, str], temporary: bool = False
) -> int:
    """``types``: those of the columns of ``table`` (a temporary one: ``temporary``), by column
    of ``data``."""
    if data.height == 0:
        return 0
    columns = ", ".join(db.quote(n) for n in names)
    marks = ", ".join("?" for _ in names)
    sizes = db.connector.input_sizes(data, types, temporary=temporary)
    sql = f"INSERT INTO {table} ({columns}) VALUES ({marks})"
    return db.execute_many(sql, data.iter_rows(), between=ctx.check_cancelled, sizes=sizes)


def _command_endpoints(p) -> list[Endpoint]:
    if not (p.connection.strip() or p.sql.strip()):
        return []
    return [Endpoint(endpoints.CMD, endpoints.SQL, place=p.connection, name=p.sql, setting="sql")]


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
    endpoints=_command_endpoints,
)
def run_sql(df, p, ctx):
    """Runs SQL in a database, e.g. a stored procedure after an export, or DELETE / UPDATE
    statements. SQL Server: separate batches with GO lines, as in SSMS (GO 3 runs a batch three
    times). Runs only when the project is run, in one transaction: if it fails, its changes are
    rolled back. SQLite refuses BEGIN, COMMIT and ROLLBACK; on SQL Server, a BEGIN TRAN … COMMIT
    of the command's own works, and a COMMIT or ROLLBACK that ends the transaction of the step
    stops it with a message. Stop ends a statement that runs. The table passes through unchanged."""
    with ctx.database(p.connection) as db:
        count = db.run_script(p.sql)
    ctx.log("Done" + (f": {count:,} row{'s' if count != 1 else ''} changed" if count >= 0 else ""))
    return df


def _datetimeoffset(value: bytes) -> dt.datetime | ActionError:
    """A datetimeoffset value (the driver's struct) as the moment it stands for, a date-time in
    UTC, to the microsecond as other date-times (2026-10-01 08:30 +02:00 is 06:30 in UTC); an
    ActionError (it stops the import) for a moment that a date-time can't hold, before the year 1
    or after 9999 in UTC (0001-01-01 08:00 +14:00)."""
    year, month, day, hour, minute, second, nanos, tz_hour, tz_minute = struct.unpack("<6hI2h", value)
    stamp = dt.datetime(year, month, day, hour, minute, second, nanos // 1000)
    try:
        return (stamp - dt.timedelta(hours=tz_hour, minutes=tz_minute)).replace(tzinfo=dt.UTC)
    except OverflowError:
        sign = "-" if tz_hour < 0 or tz_minute < 0 else "+"
        written = f"{stamp:%Y-%m-%d %H:%M:%S} {sign}{abs(tz_hour):02}:{abs(tz_minute):02}"
        return ActionError(
            f"The column {{column}} has a date and time ({written}) whose moment in UTC is before the year 1 or after "
            f"9999, which {APP_NAME} can't hold as a date-time. Read the column as text in a query, e.g. "
            "CONVERT(nvarchar(34), [{column}]) AS [{column}]."
        )

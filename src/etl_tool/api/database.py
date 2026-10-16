"""Database connections: connector plugins and the open-connection wrapper actions use."""

from __future__ import annotations

import datetime as dt
import decimal
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import polars as pl

from .errors import ActionError
from .params import Param, Params, resolve

if TYPE_CHECKING:
    from .context import Context

DtypeFor = Callable[[Sequence[Any]], "pl.DataType | None"]

# "No such table" in the words of SQLite, SQL Server, PostgreSQL, MySQL, Oracle, DuckDB, ...
_MISSING_TABLE = re.compile(
    r"no such table|invalid object name|table or view does not exist|(table|relation|view)\b.*\b(does not|doesn't) exist"
    r"|unknown table|table .* not found",
    re.IGNORECASE,
)
# Errors that certainly don't mean a missing table
_OTHER_ERROR = re.compile(r"permission|denied|not authori[sz]ed|privilege|access|locked|timeout|connection|login", re.IGNORECASE)


class Connector:
    """A database type such as SQL Server or SQLite. Subclass it and decorate with ``@connector``.

    ``params`` are the connection settings shown when a connection is defined in a project.
    """

    id: str = ""
    name: str = ""
    params: tuple[Param, ...] = ()
    plugin: str = ""  # set by the registry

    def connect(self, settings: Params, ctx: Context, *, create: bool = False) -> Any:
        """Return an open DB-API 2 connection (autocommit off)."""
        raise NotImplementedError

    def quote(self, identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    def qualified(self, name: str) -> str:
        """Quote a possibly schema-qualified name: ``dbo.Orders`` -> ``"dbo"."Orders"``."""
        return ".".join(self.quote(part) for part in split_name(name))

    def list_tables(self, conn: Any) -> list[str]:
        return []

    def dtype_for(self, description: Sequence[Any]) -> pl.DataType | None:
        """Polars type for one ``cursor.description`` entry; ``None`` = infer from the values."""
        return None

    def error_message(self, error: Exception) -> str:
        """Readable text for a driver error."""
        return str(error)

    def missing_table(self, error: Exception, table: str) -> bool:
        """True if ``error``, from reading ``table``, means that the table doesn't exist, rather
        than e.g. a missing permission. The default knows the messages of the common databases;
        other errors count as a missing table unless they speak of permissions, locks or the
        connection."""
        text = str(error)
        return bool(_MISSING_TABLE.search(text)) or not _OTHER_ERROR.search(text)

    # --- writing (used by exports; override what your database does differently) ---------------

    def sql_type(self, series: pl.Series, temporary: bool = False) -> str:
        """The column type for ``CREATE TABLE`` that fits the values of ``series``."""
        dtype = series.dtype
        if dtype.is_integer():
            return "BIGINT"
        if dtype.is_float() or isinstance(dtype, pl.Decimal):
            return "DOUBLE PRECISION"
        if dtype == pl.Boolean:
            return "BOOLEAN"
        if dtype == pl.Date:
            return "DATE"
        if dtype == pl.Datetime:
            return "TIMESTAMP"
        if dtype == pl.Time:
            return "TIME"
        if dtype == pl.Binary:
            return "BLOB"
        return f"VARCHAR({text_length(series)})"

    def same(self, left: str, right: str) -> str:
        """SQL that is true when two values are equal; two empty values count as equal."""
        return f"({left} = {right} OR ({left} IS NULL AND {right} IS NULL))"

    def temp_table(self, name: str) -> tuple[str, str]:
        """``CREATE`` keywords and the name of a table that disappears with the connection."""
        return "CREATE TEMPORARY TABLE", name

    def prepare_frame(self, df: pl.DataFrame) -> pl.DataFrame:
        """The data in a form the driver can write: here, NaN becomes an empty value."""
        return df.with_columns(pl.col(c).fill_nan(None) for c, t in df.schema.items() if t.is_float())

    def prepare_cursor(self, cursor: Any) -> None:
        """Called before writing many rows with one cursor (e.g. to switch on bulk mode)."""

    def matching(self, keys: Sequence[str], left: str = "x", right: str = "s") -> str:
        """Rows ``left`` and ``right`` have the same values in the key columns (empty = empty)."""
        return " AND ".join(self.same(f"{left}.{self.quote(k)}", f"{right}.{self.quote(k)}") for k in keys)

    def update_sql(self, table: str, stage: str, columns: Sequence[str], keys: Sequence[str]) -> str:
        """``UPDATE``: the rows of ``table`` that match a row of ``stage`` get its ``columns``."""
        sets = ", ".join(f"{self.quote(c)} = s.{self.quote(c)}" for c in columns)
        return f"UPDATE {table} AS x SET {sets} FROM {stage} AS s WHERE {self.matching(keys)}"

    def delete_sql(self, table: str, stage: str, keys: Sequence[str]) -> str:
        """``DELETE``: the rows of ``table`` that match a row of ``stage``."""
        return f"DELETE FROM {table} AS x WHERE EXISTS (SELECT 1 FROM {stage} AS s WHERE {self.matching(keys)})"

    def insert_missing_sql(self, table: str, stage: str, columns: Sequence[str], keys: Sequence[str]) -> str:
        """``INSERT``: the rows of ``stage`` that match no row of ``table``."""
        names = ", ".join(self.quote(c) for c in columns)
        picked = ", ".join(f"s.{self.quote(c)}" for c in columns)
        return (
            f"INSERT INTO {table} ({names}) SELECT {picked} FROM {stage} AS s "
            f"WHERE NOT EXISTS (SELECT 1 FROM {table} AS x WHERE {self.matching(keys)})"
        )

    def index_sql(self, stage: str, keys: Sequence[str]) -> str:
        """An index on the key columns of a temporary table, so matching rows is fast."""
        return f"CREATE INDEX {stage.lstrip('#')}_keys ON {stage} ({', '.join(self.quote(k) for k in keys)})"

    def run_script(self, conn: Any, sql: str) -> int:
        """Run SQL written by the user (possibly several statements); returns the affected rows
        or -1."""
        cursor = conn.cursor()
        try:
            cursor.execute(sql)
            return all_results(cursor)
        finally:
            cursor.close()

    def column_types(self, conn: Any, table: str) -> dict[str, str]:
        """The type of each column of an existing table, in lower case (e.g. ``"int"``), as far as
        the database tells; the default tells nothing."""
        return {}

    def open(self, settings: Mapping[str, Any], ctx: Context, *, create: bool = False) -> Database:
        params = resolve(self.params, settings)
        try:
            raw = self.connect(params, ctx, create=create)
        except ActionError:
            raise
        except Exception as e:
            raise ActionError(f"Could not connect to {self.name}: {e}") from e
        return Database(self, raw)


def connector(id: str, name: str, *, params: Sequence[Param] = ()) -> Callable[[type], Connector]:
    """Declare a database type by decorating a ``Connector`` subclass."""

    def wrap(cls: type) -> Connector:
        instance = cls()
        instance.id, instance.name, instance.params = id, name, tuple(params)
        return instance

    return wrap


class Database:
    """An open connection, handed to actions by ``ctx.database(name)``.

    Use it as a context manager: changes are committed when the block ends without an error,
    rolled back otherwise, and the connection is closed either way.
    """

    def __init__(self, connector: Connector, raw: Any) -> None:
        self.connector = connector
        self.raw = raw

    def read(self, sql: str, params: Sequence[Any] = ()) -> pl.DataFrame:
        """The rows of the first result that has columns: a SQL Server batch reports the row
        counts of its INSERTs into temporary tables first (unless it says SET NOCOUNT ON)."""
        cursor = self.raw.cursor()
        try:
            self._execute(cursor, sql, params)
            while cursor.description is None and self._next_result(cursor):
                pass
            return frame_from_cursor(cursor, self.connector.dtype_for)
        finally:
            cursor.close()

    def read_table(self, name: str) -> pl.DataFrame:
        return self.read(f"SELECT * FROM {self.qualified(name)}")

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a statement; returns the number of affected rows (-1 if unknown)."""
        cursor = self.raw.cursor()
        try:
            self._execute(cursor, sql, params)
            return cursor.rowcount
        finally:
            cursor.close()

    def _next_result(self, cursor: Any) -> bool:
        """Move to the next result of a batch (DB-API ``nextset``, which not every driver has).
        An error in a later statement of the batch shows up here."""
        nextset = getattr(cursor, "nextset", None)
        if nextset is None:
            return False
        try:
            return bool(nextset())
        except Exception as e:
            if type(e).__name__ == "NotSupportedError":
                return False
            raise ActionError(self.connector.error_message(e)) from e

    def _execute(self, cursor: Any, sql: str, params: Sequence[Any]) -> None:
        try:
            if params:
                cursor.execute(sql, tuple(params))
            else:
                cursor.execute(sql)
        except Exception as e:
            raise ActionError(self.connector.error_message(e)) from e

    def execute_many(
        self,
        sql: str,
        rows: Iterable[Sequence[Any]],
        batch_size: int = 10_000,
        between: Callable[[], None] | None = None,
    ) -> int:
        """Run one statement for many rows of values, in batches; ``between`` is called after
        each batch (e.g. ``ctx.check_cancelled``). Returns the number of rows."""
        cursor = self.raw.cursor()
        self.connector.prepare_cursor(cursor)
        total = 0
        try:
            batch: list[Sequence[Any]] = []
            for row in rows:
                batch.append(tuple(row))
                if len(batch) >= batch_size:
                    total += self._many(cursor, sql, batch)
                    batch = []
                    if between is not None:
                        between()
            if batch:
                total += self._many(cursor, sql, batch)
        finally:
            cursor.close()
        return total

    def _many(self, cursor: Any, sql: str, rows: list[Sequence[Any]]) -> int:
        try:
            cursor.executemany(sql, rows)
        except Exception as e:
            raise ActionError(self.connector.error_message(e)) from e
        return len(rows)

    def columns(self, table: str) -> list[str] | None:
        """The column names of a table, or None if there is no such table. Other errors (e.g. no
        permission to read it) are reported as they are."""
        cursor = self.raw.cursor()
        try:
            cursor.execute(f"SELECT * FROM {self.qualified(table)} WHERE 1 = 0")
            return [str(d[0]) for d in cursor.description or ()]
        except Exception as e:
            if self.connector.missing_table(e, table):
                return None
            raise ActionError(self.connector.error_message(e)) from e
        finally:
            cursor.close()

    def tables(self) -> list[str]:
        return self.connector.list_tables(self.raw)

    def quote(self, identifier: str) -> str:
        return self.connector.quote(identifier)

    def qualified(self, name: str) -> str:
        return self.connector.qualified(name)

    def commit(self) -> None:
        self.raw.commit()

    def rollback(self) -> None:
        self.raw.rollback()

    def close(self) -> None:
        self.raw.close()

    def __enter__(self) -> Database:
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.raw.close()


def frame_from_cursor(cursor: Any, dtype_for: DtypeFor | None = None, batch_size: int = 50_000) -> pl.DataFrame:
    """Read all rows of an executed DB-API cursor into a DataFrame."""
    description = cursor.description
    if not description:
        return pl.DataFrame()
    names = unique_names([str(d[0]) if d[0] else f"Column {i}" for i, d in enumerate(description, 1)])
    columns: list[list[Any]] = [[] for _ in names]
    while rows := cursor.fetchmany(batch_size):
        for index, values in enumerate(zip(*rows)):
            columns[index].extend(values)
    return pl.DataFrame(
        [
            make_series(name, values, dtype_for(d) if dtype_for else None)
            for name, values, d in zip(names, columns, description)
        ]
    )


def make_series(name: str, values: list[Any], dtype: pl.DataType | None = None) -> pl.Series:
    """Build a Series from Python values as a database driver returns them.

    Decimals become Float64 (or Int64 when asked for); columns holding several kinds of values
    (possible in SQLite) become text instead of losing values.
    """
    kinds = {type(v) for v in values if v is not None}
    if decimal.Decimal in kinds:
        convert = int if dtype == pl.Int64 else float
        values = [None if v is None else convert(v) for v in values]
        dtype = dtype if dtype == pl.Int64 else pl.Float64
    elif kinds & {bytes, bytearray, memoryview}:
        values = [None if v is None else bytes(v) for v in values]
        dtype = pl.Binary
    elif len(kinds) > 1 and not kinds <= {int, float} and not kinds <= {dt.date, dt.datetime}:
        values = [None if v is None else str(v) for v in values]
        dtype = pl.String
    elif kinds and not kinds <= _NATIVE:
        values = [None if v is None else str(v) for v in values]  # e.g. UUID
        dtype = pl.String
    try:
        return pl.Series(name, values, dtype=dtype, strict=False)
    except Exception:
        return pl.Series(name, [None if v is None else str(v) for v in values], dtype=pl.String)


_NATIVE = {str, int, float, bool, dt.date, dt.datetime, dt.time, dt.timedelta}


def text_length(series: pl.Series) -> int:
    """A text column's size for ``CREATE TABLE``: the longest value, with room to grow
    (50, 100, 255, 1000 or 4000 characters; 0 when longer, for "as long as needed")."""
    longest = series.cast(pl.String).str.len_chars().max() or 0
    for size in (50, 100, 255, 1000, 4000):
        if longest <= size:
            return size
    return 0


def unique_names(names: Sequence[str]) -> list[str]:
    """Make column names unique: ``id, id`` -> ``id, id (2)``."""
    seen: set[str] = set()
    result = []
    for name in names:
        candidate, number = name, 1
        while candidate in seen:
            number += 1
            candidate = f"{name} ({number})"
        seen.add(candidate)
        result.append(candidate)
    return result


def all_results(cursor: Any) -> int:
    """Go through every result of a statement batch: a driver may report the error of a later
    statement only when its result is reached. Returns the rows changed in all of them, or -1."""
    total, counted = 0, False
    while True:
        if cursor.rowcount >= 0:
            total, counted = total + cursor.rowcount, True
        nextset = getattr(cursor, "nextset", None)
        try:
            if nextset is None or not nextset():
                break
        except Exception as e:
            if type(e).__name__ == "NotSupportedError":
                break
            raise
    return total if counted else -1


def split_name(name: str) -> list[str]:
    """Split ``[dbo].[My Table]`` / ``"main"."t"`` / ``dbo.Orders`` into its unquoted parts; a
    doubled closing character inside quotes (``"a""b"``, ``[a]]b]``) stands for itself."""
    parts, current, quote = [], [], None
    closing = {"[": "]", '"': '"', "`": "`"}
    text = name.strip()
    index = 0
    while index < len(text):
        ch = text[index]
        index += 1
        if quote:
            if ch == closing[quote] and text[index : index + 1] == ch:
                current.append(ch)
                index += 1
            elif ch == closing[quote]:
                quote = None
            else:
                current.append(ch)
            continue
        if ch in closing:
            quote = ch
        elif ch == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    parts.append("".join(current))
    return [p.strip() for p in parts if p.strip()]

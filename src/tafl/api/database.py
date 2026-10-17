"""Database connections: connector plugins and the open-connection wrapper actions use."""

from __future__ import annotations

import datetime as dt
import decimal
import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import polars as pl

from .errors import ActionError
from .parameters import fill_sql, sql_literal, sql_spans
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

    def literal(self, value: Any) -> str:
        """``value`` written into SQL (for parameters: ``WHERE Day = {Day}``). The default suits
        most databases: texts in single quotes, dates as '2026-10-01'."""
        return sql_literal(value)

    def sql_spans(self, sql: str) -> list[tuple[int, int, str]]:
        """Where ``sql`` has its texts, names, comments and $$texts$$, as this database reads
        them: (start, end, kind), see ``parameters.sql_spans`` (here with its defaults).
        ``fill_sql`` fills in the parameters by them, and the warnings about a ``{Name}`` that
        is no parameter use them. Override it for a database that reads them differently, e.g.
        ``parameters.sql_spans(sql, nested_comments=False)``: a value written where the database
        doesn't expect it could change the SQL."""
        return sql_spans(sql)

    def fill_sql(self, sql: str, values: Mapping[str, Any]) -> str:
        """``sql`` with the parameters' ``{Name}`` filled in (values: name -> value; a fixed
        value is a ``parameters.Fixed``: ``parameters.plain`` gives its value, ``as_text`` its
        text), for the SQL settings of the steps that use this connection: written by
        ``literal`` (with a space between it and a word, a number or a quote right next to it),
        and as text inside '…' with the quotes doubled; names, comments and $$texts$$ (found by
        ``sql_spans``) stay as they are. A database that takes values in another form at some
        places overrides it and gives ``parameters.fill_sql`` a ``literal_at`` instead of
        ``literal``: it gets the plain value and the position of its ``{Name}`` in ``sql`` (the
        built-in SQL Server connector writes the arguments of EXEC as constants that way)."""
        return fill_sql(sql, values, self.literal, spans=self.sql_spans(sql))

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

    def fit_frame(self, df: pl.DataFrame, types: Mapping[str, str]) -> pl.DataFrame:
        """The data fitted to the columns of an existing table, before ``prepare_frame``:
        ``types`` has the type of each column of ``df`` there, as ``column_types`` tells it (a
        column it leaves out has no known type). For example, times cut to the precision of
        their columns, as the driver does with date-times, so that every export mode stores the
        same values. The default changes nothing."""
        return df

    def prepare_frame(self, df: pl.DataFrame) -> pl.DataFrame:
        """The data in a form the driver can write: here, NaN becomes an empty value."""
        return df.with_columns(pl.col(c).fill_nan(None) for c, t in df.schema.items() if t.is_float())

    def prepare_cursor(self, cursor: Any) -> None:
        """Called before writing many rows with one cursor (e.g. to switch on bulk mode)."""

    def input_sizes(self, df: pl.DataFrame, types: Mapping[str, str] | None = None) -> Sequence[Any] | None:
        """The type and size of each column's values for the driver (DB-API ``setinputsizes``)
        when the rows of ``df`` are written, or None to let the driver find them. ``types``:
        when the rows go into a table made by ``temp_table``, whose columns a driver may not be
        able to look up, the type each column of ``df`` has there, as its ``CREATE`` gave it."""
        return None

    def stage_types(self, conn: Any, table: str) -> dict[str, str]:
        """The column types for a temporary table (``temp_table``) that holds rows for the
        existing ``table`` while the export matches them by key: by column name, the types of
        the table's own columns, so that values arrive there as in the table itself and keys
        compare as its columns do (e.g. with their collation). A column it leaves out gets
        ``sql_type(series, temporary=True)``; the default leaves out all."""
        return {}

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
        """An index on the key columns of a temporary table, so matching rows is fast. It may
        also be SQL that leaves the index out when the database can't index those columns."""
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
        """The type of each column of an existing table, in lower case (e.g. ``"int"``,
        ``"nvarchar(50)"`` or ``"datetime2(0)"``), as far as the database tells; the default
        tells nothing."""
        return {}

    def read_table_sql(self, conn: Any, table: str) -> str:
        """The SQL that reads a whole table or view: ``SELECT *`` from it. Override it to convert
        columns the driver can't read."""
        return f"SELECT * FROM {self.qualified(table)}"

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
            try:
                return frame_from_cursor(cursor, self.connector.dtype_for)
            except ActionError:
                raise
            except Exception as e:  # e.g. a value of a later row that the server can't convert
                raise ActionError(self.connector.error_message(e)) from e
        finally:
            cursor.close()

    def read_table(self, name: str) -> pl.DataFrame:
        return self.read(self.connector.read_table_sql(self.raw, name))

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
        sizes: Sequence[Any] | None = None,
    ) -> int:
        """Run one statement for many rows of values, in batches; ``between`` is called after
        each batch (e.g. ``ctx.check_cancelled``). ``sizes``: the type and size of each value for
        the driver (``setinputsizes``), e.g. from ``connector.input_sizes``. Returns the number
        of rows."""
        cursor = self.raw.cursor()
        total = 0
        try:
            self.connector.prepare_cursor(cursor)
            if sizes is not None:
                cursor.setinputsizes(sizes)
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

    Decimals keep all their digits, so that different IDs never become equal: whole numbers
    (when ``dtype`` is Int64 or a Decimal without decimal places) become Int64 when every value
    fits, other decimals Float64 when it keeps every value as it is; else they stay Decimal
    (``dtype`` when it is one), or text beyond its 38 digits. Columns holding several kinds of
    values (possible in SQLite) become text instead of losing values. A value that is an
    ``ActionError`` (what a driver's output converter gives for a value it can't read) stops
    the reading with its message, where ``{column}`` stands for the column's name.
    """
    kinds = {type(v) for v in values if v is not None}
    if any(issubclass(kind, ActionError) for kind in kinds):
        problem = next(v for v in values if isinstance(v, ActionError))
        raise ActionError(str(problem).replace("{column}", name))
    if decimal.Decimal in kinds:
        return _decimals(name, values, dtype)
    if kinds & {bytes, bytearray, memoryview}:
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


def _decimals(name: str, values: list[Any], dtype: pl.DataType | None) -> pl.Series:
    """``make_series`` for decimal values (and the numbers among them)."""
    numbers = [v if v is None or isinstance(v, decimal.Decimal) else decimal.Decimal(str(v)) for v in values]
    exact = dtype if isinstance(dtype, pl.Decimal) else None
    if dtype == pl.Int64 or (exact is not None and exact.scale == 0):
        whole = [None if v is None else int(v) for v in numbers]
        if all(v is None or -(2**63) <= v < 2**63 for v in whole):
            return pl.Series(name, whole, dtype=pl.Int64)
    else:
        floats = [None if v is None else float(v) for v in numbers]
        short = exact is not None and (exact.precision or 38) <= 15  # then every value fits
        if short or all(_float_keeps(v, f) for v, f in zip(numbers, floats)):
            return pl.Series(name, floats, dtype=pl.Float64)
    try:
        return pl.Series(name, numbers, dtype=exact)
    except Exception:  # more than the 38 digits a Decimal holds
        return pl.Series(name, [None if v is None else str(v) for v in numbers], dtype=pl.String)


def _float_keeps(value: decimal.Decimal | None, number: float | None) -> bool:
    """Whether the Float64 ``number`` is the decimal ``value`` as it is (15 digits always fit)."""
    if value is None or not value.is_finite() or len(value.as_tuple().digits) <= 15:
        return True
    return decimal.Decimal(repr(number)) == value


def text_length(series: pl.Series) -> int:
    """A text column's size for ``CREATE TABLE``: the longest value, with room to grow
    (50, 100, 255, 1000 or 4000 characters; 0 when longer, for "as long as needed"). A
    character outside the Basic Multilingual Plane, such as an emoji, counts twice: SQL Server
    counts the UTF-16 code units."""
    texts = series.cast(pl.String)
    longest = (texts.str.len_chars() + texts.str.count_matches(r"[\x{10000}-\x{10FFFF}]")).max() or 0
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

"""Parquet files: the column-based file format of data lakes."""

from __future__ import annotations

from pathlib import Path

import polars as pl
import pyarrow as pa
import pyarrow.parquet as pq

from tafl.api import (
    ActionError,
    action,
    combine,
    dtypes,
    files,
    source_endpoints,
    source_files,
    source_params,
    source_summary,
    sources_fingerprint,
)

_MAX_DIGITS = 38  # of a decimal number that Polars keeps exactly (Decimal)


def _migrate_import(params: dict, from_version: int) -> dict:
    """Version 2 can go on without files in the folder."""
    if from_version < 2:
        params.setdefault("no_files", "error")
    return params


@action(
    "parquet.import",
    "Import Parquet files",
    category="Import",
    kind="source",
    params=source_params("Parquet files (*.parquet *.pq);;All files (*.*)", "*.parquet;*.pq"),
    version=2,
    migrate=_migrate_import,
    summary=lambda p: f"Import {source_summary(p)}",
    fingerprint=sources_fingerprint,
    endpoints=source_endpoints,
)
def import_parquet(df, p, ctx):
    """Reads a Parquet file, or all Parquet files of a folder (and its subfolders) into one
    table. Parquet is the compact, column-based format of data lakes (written by Spark,
    Databricks, pandas, DuckDB, ...); column types are stored in the file, so nothing has to
    be detected. Decimal columns keep all their digits: those without decimal places become
    whole numbers when every value fits, the others stay exact decimals; numbers of more than
    38 digits become text. Files are matched by column names."""
    files, folder = source_files(p, ctx)
    parts = []
    for path in files:
        ctx.check_cancelled()
        parts.append((path, None, _read(path, ctx.warn)))
    if len(files) > 1:
        ctx.log(f"{len(files)} files")
    return combine(parts, p, folder, prepare=_standard_types)


def _standard_types(data: pl.DataFrame) -> pl.DataFrame:
    return data.with_columns([dtypes.normalize(data[name], exact=True) for name in data.columns])


def _read(path: Path, warn) -> pl.DataFrame:
    try:
        return pl.read_parquet(path)
    except Exception as e:
        if isinstance(e, OSError) or "(os error " in str(e):  # e.g. open in a program that doesn't let others read it
            raise files.read_error(e, path) from None
        error = ActionError(f"Could not read {path.name}: {str(e).splitlines()[0]}")
    try:  # Polars can't read decimals of more than 38 digits: pyarrow can
        wide = [f.name for f in pq.read_schema(path) if pa.types.is_decimal(f.type) and f.type.precision > _MAX_DIGITS]
        table = pq.read_table(path) if wide else None
    except Exception:
        table = None
    if table is None:
        raise error from None
    for name in wide:
        i = table.schema.get_field_index(name)
        column = table.column(i)
        try:  # the values may be short enough
            column = column.cast(pa.decimal128(_MAX_DIGITS, column.type.scale))
        except (pa.ArrowInvalid, ValueError):
            column = column.cast(pa.string())
            longest = max((v for v in column.to_pylist() if v is not None), key=len, default="")
            warn(f"{name} becomes text: its numbers are too long to be kept exactly, e.g. '{longest}'")
        table = table.set_column(i, name, column)
    return pl.from_arrow(table)

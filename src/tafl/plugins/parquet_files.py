"""Parquet files: the column-based file format of data lakes."""

from __future__ import annotations

import polars as pl

from tafl.api import (
    ActionError,
    action,
    combine,
    dtypes,
    source_files,
    source_params,
    source_summary,
    sources_fingerprint,
)


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
)
def import_parquet(df, p, ctx):
    """Reads a Parquet file, or all Parquet files of a folder (and its subfolders) into one
    table. Parquet is the compact, column-based format of data lakes (written by Spark,
    Databricks, pandas, DuckDB, ...); column types are stored in the file, so nothing has to
    be detected. Decimal columns become numbers as in the other imports (whole numbers when
    they have no decimal places). Files are matched by column names."""
    files, folder = source_files(p, ctx)
    parts = []
    for path in files:
        ctx.check_cancelled()
        try:
            data = pl.read_parquet(path)
        except Exception as e:
            raise ActionError(f"Could not read {path.name}: {str(e).splitlines()[0]}") from None
        parts.append((path, None, data.with_columns([dtypes.normalize(data[c]) for c in data.columns])))
    if len(files) > 1:
        ctx.log(f"{len(files)} files")
    return combine(parts, p, folder)

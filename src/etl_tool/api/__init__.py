"""The plugin API: the only part of ``etl_tool`` that plugins may import.

Everything exported here is a stable contract. Changes must stay backwards compatible while
``API_VERSION`` stays the same. A plugin can declare the API version it needs (e.g.
``REQUIRES_API = 2``): apps with an older API don't load it and say why.

A minimal plugin::

    import polars as pl
    from etl_tool.api import P, action

    @action("example.upper", "Upper-case a column", category="Text",
            params=[P.Column("column", "Column")],
            summary="Upper-case {column}")
    def upper(df, p, ctx):
        \"\"\"Turns all text in the column into capital letters.\"\"\"
        return df.with_columns(pl.col(p.column).str.to_uppercase())
"""

from etl_tool import APP_NAME

from . import dtypes, expressions, files, parameters, xlsx
from . import params as P
from .action import Action, Kind, action, file_fingerprint
from .context import Context, Mode
from .database import (
    Connector,
    Database,
    all_results,
    connector,
    frame_from_cursor,
    make_series,
    split_name,
    text_length,
    unique_names,
)
from .errors import ActionError, Cancelled, EtlError
from .files import combine, find_files, source_files, source_params, source_summary, sources_fingerprint
from .params import Params, ValidationContext

API_VERSION = 1

__all__ = [
    "APP_NAME",
    "API_VERSION",
    "Action",
    "ActionError",
    "Cancelled",
    "Connector",
    "Context",
    "Database",
    "EtlError",
    "Kind",
    "Mode",
    "P",
    "Params",
    "ValidationContext",
    "action",
    "combine",
    "all_results",
    "connector",
    "dtypes",
    "expressions",
    "file_fingerprint",
    "files",
    "find_files",
    "frame_from_cursor",
    "make_series",
    "parameters",
    "source_files",
    "source_params",
    "source_summary",
    "split_name",
    "sources_fingerprint",
    "text_length",
    "unique_names",
    "xlsx",
]

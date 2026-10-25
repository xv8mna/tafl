"""What a step reads, writes or changes outside the project: files, folders, database tables and
SQL. The project map marks the steps that do, and the Find panel lists them, also by folder or
connection. An action tells its endpoints with ``endpoints=`` (a function of its settings);
without it they come from its file, folder and connection settings (``default_endpoints``).
The directions (IN, OUT, CMD) and kinds (FILE, FOLDER, DATABASE, SQL) are in this module::

    from tafl.api import P, action, file_endpoint
    from tafl.api.endpoints import OUT

    @action("my.export", "Export my file", kind="export",
            params=[P.File("file", "File", mode="save")],
            endpoints=lambda p: [file_endpoint(p.file, OUT, setting="file")])
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import params as P

if TYPE_CHECKING:
    from .action import Action

IN, OUT, CMD = "in", "out", "cmd"
FILE, FOLDER, DATABASE, SQL = "file", "folder", "database", "sql"

# The direction of the endpoints that an action's settings name, by the action's kind
_DIRECTIONS = {"source": IN, "transform": IN, "export": OUT, "command": CMD}
_DRIVE = re.compile(r"[A-Za-z]:")


@dataclass(frozen=True)
class Endpoint:
    """A file, a folder, a database table or SQL that a step reads, writes or changes."""

    direction: str  # IN: reads from it; OUT: writes into it; CMD: changes it otherwise
    #                 (deletes, moves away, renames, creates a folder, runs SQL)
    kind: str  # FILE, FOLDER, DATABASE (a table or view) or SQL (a query or a command)
    place: str = ""  # FILE/FOLDER: the folder as written ("" = the project's folder);
    #                  DATABASE/SQL: the name of the connection
    name: str = ""  # FILE: the file name; FOLDER: the file names (orders_*.csv), "" = the folder
    #                 itself; DATABASE: the table (dbo.Orders); SQL: the SQL text
    part: str = ""  # a part of the file: the sheet of a workbook
    mode: str = ""  # how, in a few words: "replace only this sheet", "with subfolders", "add rows"
    setting: str = ""  # the key of the setting that names it (the Find panel moves the focus there)
    column: str = ""  # the files are listed in this column of the table (place: the folder the
    #                   paths are below, "" = full paths or below the project's folder)
    imported: bool = False  # the files are those the table's import read from a folder
    mode_setting: str = ""  # the key of the setting that ``mode`` comes from ("" = ``setting``)
    folder_import: bool = False  # FOLDER: an import of all files in it that notes the files it
    #                              read (``source_files`` does): a later step's ``imported`` files


def file_endpoint(
    path: str, direction: str, *, part: str = "", mode: str = "", setting: str = "", mode_setting: str = ""
) -> Endpoint:
    """A FILE endpoint for a path as written: what comes before the last / or \\ is the place,
    the rest the name ("{Folder}\\orders.csv": place "{Folder}", name "orders.csv"; "orders.csv":
    place ""; "C:orders.csv": place "C:")."""
    text = path.strip() if isinstance(path, str) else ""
    cut = max(text.rfind("/"), text.rfind("\\"))
    place, name = (text[:cut], text[cut + 1 :]) if cut >= 0 else ("", text)
    if cut >= 0 and (not place or place.endswith(":")):  # \orders.csv, C:\orders.csv: the top folder
        place = text[: cut + 1]
    elif cut < 0 and _DRIVE.match(text):  # C:orders.csv: on drive C, not in the project's folder
        place, name = text[:2], text[2:]
    return Endpoint(
        direction, FILE, place=place, name=name, part=part, mode=mode, setting=setting, mode_setting=mode_setting
    )


def default_endpoints(action: Action, params: Mapping[str, Any]) -> list[Endpoint]:
    """The endpoints of an action that doesn't tell its own: one for each shown file and folder
    setting (also in the rows of a list), and one for each connection: its SQL when the action has
    a shown SQL setting with text, else the database. They read for imports and other steps, write
    for exports and change something for commands. An action without such settings has none.

    An import with the settings of ``source_params`` gets the folder of ``source_endpoints``: its
    file names, and that it is an import of all files in the folder (``folder_import``), as if it
    said ``endpoints=source_endpoints`` (imports written before ``endpoints=`` existed)."""
    direction = _DIRECTIONS.get(str(action.kind), IN)
    folder_import = _source_import(action)
    sql = None  # (key, text) of the first shown SQL setting with text
    for param in action.params:
        if isinstance(param, P.Code) and param.language == "sql" and P.is_visible(param, params):
            if code := _text(params.get(param.key)):
                sql = param.key, code
                break
    found: list[Endpoint] = []
    for param in action.params:
        if not P.is_visible(param, params):
            continue
        value = params.get(param.key)
        if isinstance(param, P.List):
            for row in value if isinstance(value, list) else ():
                if isinstance(row, Mapping):
                    found += [
                        endpoint
                        for field in param.fields
                        if P.is_visible(field, row)
                        if (endpoint := _path_endpoint(field, row.get(field.key), direction, param.key)) is not None
                    ]
        elif isinstance(param, P.ConnectionRef):
            if connection := _text(value):
                if sql is not None:
                    found.append(Endpoint(direction, SQL, place=connection, name=sql[1], setting=sql[0]))
                else:
                    found.append(Endpoint(direction, DATABASE, place=connection, setting=param.key))
        elif folder_import and param.key == "folder":  # shown: all files in a folder are read
            from .files import source_endpoints  # (files.py imports this module)

            found += source_endpoints(params)
        elif (endpoint := _path_endpoint(param, value, direction, param.key)) is not None:
            found.append(endpoint)
    return found


def _source_import(action: Action) -> bool:
    """Whether the action is an import with the settings of ``source_params``: its folder is shown
    for "All files in a folder" only, with the file names beside it."""
    if str(action.kind) != "source":
        return False
    params = {param.key: param for param in action.params}
    folder, source = params.get("folder"), params.get("source")
    return (
        isinstance(source, P.Choice)
        and isinstance(folder, P.Folder)
        and folder.visible_if == {"source": "folder"}
        and isinstance(params.get("pattern"), P.Text)
    )


def _path_endpoint(param: P.Param, value: Any, direction: str, setting: str) -> Endpoint | None:
    text = _text(value)
    if not text:
        return None
    if isinstance(param, P.File):
        return file_endpoint(text, direction, setting=setting)
    if isinstance(param, P.Folder):
        return Endpoint(direction, FOLDER, place=text, setting=setting)
    return None


def _text(value: Any) -> str:
    """A setting's text without the spaces around it; "" for no text."""
    return value.strip() if isinstance(value, str) else ""

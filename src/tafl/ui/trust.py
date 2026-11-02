"""Which project files Tafl knows. A project it doesn't know opens in protected view: it can be read,
but nothing is calculated, and none of its files or databases is touched, until it is trusted.

The list is kept in the settings of this computer, not in the project file: a project can't answer
for itself. A value on the list stands for the file (``Project.digest``) in its folder: a file that
was changed elsewhere is unknown again, and so is a copy in another folder, where the project's
relative paths point to other files. No widgets here."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from PySide6.QtCore import QSettings

LIMIT = 1_000  # values kept, newest first
_VALUES = "trusted_projects"
_PATHS = "trusted_paths"  # the recent list when protected view came: see ``first_start``


def is_known(digest: str, path: str | Path) -> bool:
    """Whether the file with this SHA-256 value at ``path`` opens as always. A path that
    ``first_start`` kept counts once: its value is known from then on, and the path goes."""
    key = _path_key(path)
    paths = _read(_PATHS)
    if key in paths:
        _write(_PATHS, [other for other in paths if other != key])
    elif _value(digest, path) not in _read(_VALUES):
        return False
    add(digest, path)
    return True


def add(digest: str, path: str | Path) -> bool:
    """The file with this SHA-256 value at ``path`` is known from now on. Returns whether its value
    is new on the list."""
    value = _value(digest, path)
    known = _read(_VALUES)
    _write(_VALUES, [value, *(other for other in known if other != value)][:LIMIT])
    return value not in known


def drop(digest: str, path: str | Path) -> None:
    """The file with this SHA-256 value at ``path`` is known no more."""
    value = _value(digest, path)
    _write(_VALUES, [other for other in _read(_VALUES) if other != value])


def forget_path(path: str | Path) -> None:
    """What ``first_start`` kept of ``path`` no longer holds: another file is there now."""
    key = _path_key(path)
    _write(_PATHS, [other for other in _read(_PATHS) if other != key])


def first_start() -> None:
    """The projects of the recent list were opened before there was a protected view, so they
    don't ask: their paths are copied once, when the app starts for the first time with it. No
    file is read here."""
    settings = QSettings()
    if not settings.contains(_PATHS):
        recent = settings.value("recent", [])
        recent = [recent] if isinstance(recent, str) else list(recent or [])
        _write(_PATHS, [_path_key(path) for path in recent])


def _value(digest: str, path: str | Path) -> str:
    folder = os.path.dirname(_path_key(path))
    return hashlib.sha256(f"{digest}\n{folder}".encode("utf-8", "surrogatepass")).hexdigest()


def _path_key(path: str | Path) -> str:
    return os.path.normcase(os.path.normpath(path))


def _read(key: str) -> list[str]:
    try:
        found = json.loads(str(QSettings().value(key, "[]")))
    except ValueError:
        return []
    return [item for item in found if isinstance(item, str)] if isinstance(found, list) else []


def _write(key: str, values: list[str]) -> None:
    QSettings().setValue(key, json.dumps(values))

"""Which project files Tafl knows. A project it doesn't know opens in protected view: it can be read,
but nothing is calculated, and none of its files or databases is touched, until it is trusted.

The list is kept in the settings of this computer, not in the project file: a project can't answer
for itself. It holds SHA-256 values of files (``Project.digest``), so a file that was changed
elsewhere is unknown again. No widgets here."""

from __future__ import annotations

import json
import os
from pathlib import Path

from PySide6.QtCore import QSettings

LIMIT = 1_000  # values kept, newest first
_DIGESTS = "trusted_projects"
_PATHS = "trusted_paths"  # the recent list when protected view came: see ``first_start``


def is_known(digest: str, path: str | Path) -> bool:
    """Whether the file with this SHA-256 value at ``path`` opens as always. A path that
    ``first_start`` kept counts once: its value is known from then on, and the path goes."""
    key = _path_key(path)
    paths = _read(_PATHS)
    if key in paths:
        _write(_PATHS, [other for other in paths if other != key])
    elif digest not in _read(_DIGESTS):
        return False
    add(digest)
    return True


def add(digest: str) -> None:
    """The file with this SHA-256 value is known from now on."""
    _write(_DIGESTS, [digest, *(other for other in _read(_DIGESTS) if other != digest)][:LIMIT])


def first_start() -> None:
    """The projects of the recent list were opened before there was a protected view, so they
    don't ask: their paths are copied once, when the app starts for the first time with it. No
    file is read here."""
    settings = QSettings()
    if not settings.contains(_PATHS):
        recent = settings.value("recent", [])
        recent = [recent] if isinstance(recent, str) else list(recent or [])
        _write(_PATHS, [_path_key(path) for path in recent])


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

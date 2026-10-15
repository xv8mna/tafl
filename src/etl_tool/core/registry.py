"""Finding and loading plugins.

Built-in plugins live in ``etl_tool.plugins``. More plugins are loaded from the folders listed in
the ``TAFL_PLUGINS`` environment variable and from ``%APPDATA%\\Tafl\\plugins`` (the variable and
folder of the app's earlier name are read too): every ``*.py`` file, and every folder with an
``__init__.py``. A plugin that fails to load is recorded with its error; everything else keeps
working.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import pkgutil
import re
import sys
import traceback
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path
from types import ModuleType

from etl_tool import APP_FOLDER
from etl_tool.api import API_VERSION, Action, Connector

ENV_VAR = "TAFL_PLUGINS"


@dataclass
class PluginInfo:
    name: str
    path: str
    builtin: bool
    actions: list[str] = field(default_factory=list)
    connectors: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


class Registry:
    """All actions and connectors provided by the loaded plugins."""

    def __init__(self) -> None:
        self.actions: dict[str, Action] = {}
        self.connectors: dict[str, Connector] = {}
        self.plugins: list[PluginInfo] = []

    @classmethod
    def default(cls, extra_folders: Iterable[str | Path] = ()) -> Registry:
        """Built-in plugins plus the user's plugin folders."""
        registry = cls()
        registry.load_builtin()
        for folder in [*default_plugin_folders(), *map(Path, extra_folders)]:
            registry.load_folder(folder)
        return registry

    def load_builtin(self) -> None:
        from etl_tool import plugins as package

        for module in sorted(pkgutil.iter_modules(package.__path__), key=lambda m: m.name):
            name = f"{package.__name__}.{module.name}"
            self._load(module.name, name, builtin=True, importer=lambda name=name: importlib.import_module(name))

    def load_folder(self, folder: str | Path) -> None:
        folder = Path(folder)
        if not folder.is_dir():
            return
        for path in sorted(folder.iterdir()):
            if path.name.startswith(("_", ".")):
                continue
            if path.suffix == ".py":
                file, search = path, None
            elif path.is_dir() and (path / "__init__.py").is_file():
                file, search = path / "__init__.py", [str(path)]
            else:
                continue
            module_name = "etl_tool_ext_" + re.sub(r"\W", "_", path.stem)
            self._load(
                path.stem,
                str(path),
                builtin=False,
                importer=lambda m=module_name, f=file, s=search: _import_file(m, f, s),
            )

    def add_module(self, module: ModuleType, info: PluginInfo | None = None) -> PluginInfo:
        """Register the actions and connectors defined at the top level of ``module``."""
        if info is None:
            info = PluginInfo(module.__name__, getattr(module, "__file__", "") or "", builtin=False)
            self.plugins.append(info)
        required = getattr(module, "REQUIRES_API", API_VERSION)
        if required > API_VERSION:
            info.errors.append(f"Needs plugin API version {required}; this app has {API_VERSION}")
            return info
        for value in vars(module).values():
            if isinstance(value, Action):
                self._add_action(value, info)
            elif isinstance(value, Connector):
                self._add_connector(value, info)
        return info

    def by_category(self) -> dict[str, list[Action]]:
        groups: dict[str, list[Action]] = {}
        for item in sorted(self.actions.values(), key=lambda a: (a.category, a.name)):
            groups.setdefault(item.category, []).append(item)
        return groups

    @property
    def errors(self) -> list[tuple[str, str]]:
        return [(p.name, e) for p in self.plugins for e in p.errors]

    def _load(self, name: str, path: str, *, builtin: bool, importer: Callable[[], ModuleType]) -> None:
        info = PluginInfo(name, path, builtin)
        self.plugins.append(info)
        try:
            module = importer()
        except Exception:
            info.errors.append("Could not load:\n" + traceback.format_exc(limit=-4).strip())
            return
        self.add_module(module, info)

    def _add_action(self, action: Action, info: PluginInfo) -> None:
        existing = self.actions.get(action.id)
        if existing is not None:
            if existing.run is not action.run:
                info.errors.append(f"Action id '{action.id}' is already used by plugin '{existing.plugin}'")
            return
        self.actions[action.id] = replace(action, plugin=info.name)
        info.actions.append(action.id)

    def _add_connector(self, connector: Connector, info: PluginInfo) -> None:
        existing = self.connectors.get(connector.id)
        if existing is not None:
            if existing is not connector:
                info.errors.append(f"Connection type '{connector.id}' is already used by plugin '{existing.plugin}'")
            return
        connector.plugin = info.name
        self.connectors[connector.id] = connector
        info.connectors.append(connector.id)


def default_plugin_folders() -> list[Path]:
    folders = [Path(p) for p in os.environ.get(ENV_VAR, "").split(os.pathsep) if p.strip()]
    if appdata := os.environ.get("APPDATA"):
        folders.append(Path(appdata) / APP_FOLDER / "plugins")
    return folders


def _import_file(module_name: str, file: Path, search: list[str] | None) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, file, submodule_search_locations=search)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot import {file}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module

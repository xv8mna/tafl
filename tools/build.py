"""Build the Windows download and check it: dist\\Tafl (tafl-app.exe: the window, tafl.exe: the
command line, the libraries, their licenses and the demo) and dist\\Tafl-<version>-windows.zip.

Build in a fresh virtual environment with Python 3.14, so that only what Tafl needs goes in:

    py -3.14 -m venv .venv-build
    .venv-build\\Scripts\\python -m pip install --require-hashes -r requirements-build.lock
    .venv-build\\Scripts\\python -m pip install --no-deps --no-build-isolation .
    .venv-build\\Scripts\\python tools\\build.py

The download is built from committed files only: the build stops when files in src, packaging,
tools or examples have changes that are not committed, unless --dirty is given (for trying
things out).
"""

from __future__ import annotations

import argparse
import ast
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib import metadata
from pathlib import Path, PurePath

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP = DIST / "Tafl"
WORK = ROOT / "build"
QT_NOTICES = ROOT / "packaging" / "licenses" / "Qt-third-party.txt"
PYTHON_NOTICES = ROOT / "packaging" / "licenses" / "Python" / "ACKNOWLEDGEMENTS.txt"

# Tools for building, not part of the download. PyInstaller is not one of them: its start-up code
# and run-time hooks are in both programs.
BUILD_TOOLS = {"pip", "pyinstaller-hooks-contrib", "setuptools", "altgraph", "pefile", "pywin32-ctypes", "packaging",
               "tafl"}  # fmt: skip
# Notes for libraries whose license files don't cover all their code
LICENSE_NOTES = {
    "shiboken6": "Shiboken6 includes code from Python under the PSF License Agreement: see ..\\Python\\LICENSE.txt.\n",
}
# What the examples write when they are run
EXAMPLE_OUTPUTS = {
    "demo.tafl": ["output/orders.xlsx", "output/orders.csv", "output/revenue.xlsx"],
    "excel-check.tafl": ["output/excel-check.xlsx"],
}
# A plugin that imports what plugins may use; both programs must load it
CHECK_PLUGIN = '''\
import configparser, email.mime.text, html.parser, logging.handlers, smtplib, tomllib, xml.etree.ElementTree
import datetime as dt
import zoneinfo

import polars as pl
from tafl.api.testing import StubContext  # the plugin API, with its helpers for tests

zoneinfo.ZoneInfo("Europe/Berlin")  # from the tzdata package: Windows has no time zone files
moments = pl.Series([dt.datetime(2026, 1, 15, 12), dt.datetime(2026, 7, 15, 12)]).dt.replace_time_zone("UTC")
offsets = [moment.utcoffset() for moment in moments.dt.convert_time_zone("Europe/Berlin").to_list()]
if offsets != [dt.timedelta(hours=1), dt.timedelta(hours=2)]:
    raise RuntimeError(f"Europe/Berlin: {offsets}")
'''


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Windows download and check it.")
    parser.add_argument("--dirty", action="store_true", help="build with changes that are not committed (to try things out)")
    if not parser.parse_args().dirty:
        _committed()
    from tafl import __version__

    versions = _versions()
    _check_python_notices()
    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(WORK, ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "tafl.spec", "--noconfirm", "--distpath", str(DIST), "--workpath", str(WORK)],
        cwd=ROOT / "packaging",
        check=True,
    )
    _check_sources()
    _check_qt_notices()
    _add_files(__version__, versions)
    _check(__version__)
    archive = _zip(__version__)
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"\n{APP}: {size / 2**20:.0f} MB\n{archive}: {archive.stat().st_size / 2**20:.0f} MB")


def _git(*arguments: str) -> str:
    return subprocess.run(["git", *arguments], cwd=ROOT, capture_output=True, encoding="utf-8", check=True).stdout


def _committed() -> None:
    """The download is what is committed: CI checkouts are clean, a local folder may not be."""
    changes = _git("status", "--porcelain", "--untracked-files=all", "--", "src", "packaging", "tools", "examples")
    if changes:
        raise SystemExit(f"Not committed:\n{changes}\nCommit first, or build with --dirty to try things out.")


def _versions() -> dict[str, str]:
    """The versions for the README's licenses part: of PyInstaller, of Qt, PySide6 and Shiboken6
    with the links to their source, and of lxml with the libiconv it includes."""
    import PySide6
    import shiboken6
    from lxml import etree
    from PySide6.QtCore import qVersion

    qt = qVersion()
    if not QT_NOTICES.read_text(encoding="utf-8").startswith(f"Qt {qt}:"):
        raise SystemExit(f"{QT_NOTICES.relative_to(ROOT)} is not for Qt {qt}: update it with tools\\qt_notices.py")
    iconv = getattr(etree, "ICONV_COMPILED_VERSION", (0, 0))
    if not any(iconv):  # the README says that lxml includes it, under the LGPL-2.1
        raise SystemExit("lxml has no libiconv any more: update the part about it in packaging\\README.txt")
    return {
        "pyinstaller": metadata.version("pyinstaller"),
        "qt": qt,
        "pyside": PySide6.__version__,
        "shiboken": shiboken6.__version__,
        # The archive keeps every Qt version; Qt for Python keeps them in its releases folder
        "qt_source": f"https://download.qt.io/archive/qt/{qt.rsplit('.', 1)[0]}/{qt}/single/",
        "pyside_source": f"https://download.qt.io/official_releases/QtForPython/pyside6/PySide6-{PySide6.__version__}-src/",
        "lxml": metadata.version("lxml"),
        "iconv": ".".join(map(str, iconv)),
    }


def _check_python_notices() -> None:
    """The notices for the code of other projects in Python are for the Python of the build. They
    change with Python's version (3.14, 3.15), seldom with its bug-fix releases (3.14.x)."""
    built_with, file = platform.python_version(), PYTHON_NOTICES.relative_to(ROOT)
    written_for = PYTHON_NOTICES.read_text(encoding="utf-8").split(":", 1)[0].removeprefix("Python ")
    if written_for.rsplit(".", 1)[0] != built_with.rsplit(".", 1)[0]:
        raise SystemExit(f"{file} is for Python {written_for}: update it with tools\\python_notices.py")
    if written_for != built_with:
        print(f"Note: {file} is for Python {written_for}, the build uses {built_with}")


def _check_qt_notices() -> None:
    """Qt-third-party.txt names the Qt DLLs it has the notices for: exactly the ones in the download."""
    header = QT_NOTICES.read_text(encoding="utf-8").split("\nContents\n", 1)[0]
    named = {name.rsplit("\\", 1)[-1] for name in re.findall(r"[\w\\]+\.dll", header)}
    qt = APP / "_internal" / "PySide6"
    shipped = {file.name for file in [*qt.glob("Qt6*.dll"), *(qt / "plugins").rglob("*.dll")]}
    if named != shipped:
        only_named, only_shipped = ", ".join(sorted(named - shipped)) or "-", ", ".join(sorted(shipped - named)) or "-"
        raise SystemExit(
            f"{QT_NOTICES.relative_to(ROOT)} is not for the Qt DLLs of the download (not in the download: {only_named}; "
            f"not in the file: {only_shipped}): update it with tools\\qt_notices.py"
        )


def _check_sources() -> None:
    """Everything in the download comes from Python, the build environment, Tafl's source or the
    build itself, and nothing from other programs on the build machine's PATH."""
    (entries,) = ast.literal_eval((WORK / "tafl" / "COLLECT-00.toc").read_text(encoding="utf-8"))
    allowed = [Path(folder).resolve() for folder in (sys.base_prefix, sys.prefix, ROOT / "src", WORK)]
    foreign = [
        f"  {destination}  from  {source}"
        for destination, source, _ in entries
        if source and not any(Path(source).resolve().is_relative_to(folder) for folder in allowed)
    ]
    if foreign:
        raise SystemExit("Files from outside Python, the build environment and src:\n" + "\n".join(foreign))


def _add_files(version: str, versions: dict[str, str]) -> None:
    """The README, the licenses of Tafl and of everything included, and the examples."""
    text = (ROOT / "packaging" / "README.txt").read_text(encoding="utf-8")
    for key, value in {"version": version, **versions}.items():
        text = text.replace(f"{{{key}}}", value)
    title, _, rest = text.split("\n", 2)
    text = f"{title}\n{'=' * len(title)}\n{rest}"  # the underline as long as the title with its version
    (APP / "README.txt").write_text(text, encoding="utf-8", newline="\r\n")
    shutil.copyfile(ROOT / "LICENSE", APP / "LICENSE.txt")
    licenses = APP / "licenses"
    licenses.mkdir()
    for notice in (ROOT / "packaging" / "licenses").glob("*.txt"):  # the LGPLs, the GPL, Qt's third-party notices
        shutil.copyfile(notice, licenses / notice.name)
    python = Path(sys.base_prefix) / "LICENSE.txt"
    if not python.is_file():
        raise SystemExit(f"Python's license is not in {python}")
    (licenses / "Python").mkdir()
    shutil.copyfile(python, licenses / "Python" / "LICENSE.txt")
    shutil.copyfile(PYTHON_NOTICES, licenses / "Python" / PYTHON_NOTICES.name)
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name.lower() in BUILD_TOOLS:
            continue
        target = licenses / f"{name}-{dist.version}"
        copied = 0
        for file in dist.files or ():
            source = Path(dist.locate_file(file))
            if _is_license(file.name) and source.is_file():
                (target / _license_path(file)).parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target / _license_path(file))
                copied += 1
        if not copied:
            terms = dist.metadata.get("License-Expression") or dist.metadata.get("License") or "see the project"
            text = f"{name} {dist.version}: {terms}\n"
            if "LGPL-3.0" in terms:
                text += "This download includes it under the LGPL-3.0: see ..\\LGPL-3.0.txt and ..\\GPL-3.0.txt.\n"
            target.mkdir(parents=True)
            (target / "LICENSE.txt").write_text(text, encoding="utf-8", newline="\r\n")
        if note := LICENSE_NOTES.get(name.lower()):
            (target / "NOTE.txt").write_text(note, encoding="utf-8", newline="\r\n")
    for file in _git("ls-files", "-z", "--", "examples").split("\0"):
        path = Path(file)
        if path.suffix == ".tafl" or path.parts[1:2] == ("data",):  # the projects and their data
            (APP / path).parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / path, APP / path)


def _is_license(name: str) -> bool:
    """A license file, but not Qt's commercial license (PySide6 and Shiboken6 ship only that one,
    while the download uses them under the LGPL)."""
    name = name.lower()
    commercial = name.startswith("licenseref-qt-commercial")
    return not commercial and any(word in name for word in ("license", "copying", "notice"))


def _license_path(file: PurePath) -> PurePath:
    """Where a license file goes in the library's folder: e.g. LICENSE from
    <name>.dist-info\\licenses\\LICENSE, and the license of a part with its folders. A file
    installed outside site-packages (..\\..\\share\\... or a full path) only with its name."""
    parts = file.parts
    if ".." in parts or file.anchor:
        return PurePath(file.name)
    if parts[0].endswith(".dist-info"):
        parts = parts[2:] if parts[1:2] == ("licenses",) else parts[1:]
    return PurePath(*parts)


def _check(version: str) -> None:
    """Both programs start and load the plugins and a plugin that imports what plugins may use. The
    command line runs the examples; the window opens the demo and calculates all its tables. All in
    a temporary folder, without the settings and plugins of the build machine."""
    cli, window = APP / "tafl.exe", APP / "tafl-app.exe"
    started = time.perf_counter()
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        log = temp / "Local" / "Tafl" / "tafl.log"
        windows = os.environ.get("SystemRoot", r"C:\Windows")
        environment = {key: value for key, value in os.environ.items() if not key.startswith(("TAFL_", "PYTHON", "QT_"))}
        environment |= {
            "APPDATA": str(temp / "Roaming"),
            "LOCALAPPDATA": str(temp / "Local"),
            "PATH": f"{windows}\\System32;{windows}",  # no DLLs from other programs
            "PYTHONIOENCODING": "utf-8",  # the output in UTF-8: this also checks that tafl.exe applies it
        }
        plugins = temp / "plugins"
        plugins.mkdir()
        (plugins / "check_imports.py").write_text(CHECK_PLUGIN, encoding="utf-8")
        with_plugin = environment | {"TAFL_PLUGINS": str(plugins)}
        examples = temp / "examples"
        shutil.copytree(APP / "examples", examples)

        shown = _run(cli, "--version", env=environment).strip()
        if shown != f"Tafl {version}":
            raise SystemExit(f"tafl.exe --version: {shown}")
        listed = _run(cli, "plugins", env=with_plugin)
        if "ERROR" in listed or not all(name in listed for name in ("excel.export", "calc.columns", "check_imports")):
            raise SystemExit(f"tafl.exe plugins:\n{listed}")
        _run(cli, "run", examples / "demo.tafl", "--preview", env=environment)
        if (examples / "output").exists():
            raise SystemExit("tafl.exe run --preview wrote files")
        for project, outputs in EXAMPLE_OUTPUTS.items():
            for _ in range(2):  # the second time into the files of the first
                _run(cli, "run", examples / project, env=environment)
            missing = [output for output in outputs if not (examples / output).is_file()]
            if missing:
                raise SystemExit(f"tafl.exe run {project} didn't write {', '.join(missing)}")
        table = _run(cli, "show", examples / "demo.tafl", "--table", "Customers", env=environment)
        if "Müller" not in table or "│" not in table:
            raise SystemExit(f"tafl.exe show, with PYTHONIOENCODING=utf-8:\n{table}")
        checked = time.perf_counter()
        check = with_plugin | {"TAFL_STARTUP_CHECK": "1", "QT_QPA_PLATFORM": "offscreen"}
        _run(window, examples / "demo.tafl", env=check, timeout=120, log=log)
        print(
            f"checked in {checked - started:.1f} s: tafl.exe --version, plugins (with a plugin that imports the standard "
            f"library), run --preview, run of both examples, show in UTF-8; tafl-app.exe opened the demo with that "
            f"plugin and calculated its tables in {time.perf_counter() - checked:.1f} s"
        )


def _run(program: Path, *arguments: str | Path, env: dict[str, str], timeout: int = 300, log: Path | None = None) -> str:
    """Run a program of the download; on an error stop with its output (and the log)."""
    try:
        result = subprocess.run([program, *arguments], env=env, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired as e:
        ended, output = f"didn't end within {timeout} s", (e.stdout or b"") + (e.stderr or b"")
    else:
        if result.returncode == 0:
            return result.stdout.decode("utf-8", "replace")
        ended, output = f"ended with exit code {result.returncode}", result.stdout + result.stderr
    message = f"{program.name} {' '.join(map(str, arguments))} {ended}:\n{output.decode('utf-8', 'replace')}"
    if log is not None:  # tafl-app.exe: 3 for a table or plugin error (logged), 4 for an unexpected error
        logged = log.read_text(encoding="utf-8", errors="replace")[-20000:] if log.is_file() else "(no log file)"
        message += f"\n{log}:\n{logged}"
    raise SystemExit(message)


def _zip(version: str) -> Path:
    archive = DIST / f"Tafl-{version}-windows.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for file in sorted(APP.rglob("*")):
            if file.is_file():
                out.write(file, Path("Tafl") / file.relative_to(APP))
    return archive


if __name__ == "__main__":
    main()

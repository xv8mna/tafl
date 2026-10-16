"""Build the Windows download and check it: dist\\Tafl (tafl-app.exe: the window, tafl.exe: the
command line, the libraries, their licenses and the demo) and dist\\Tafl-<version>-windows.zip.

Build in a fresh virtual environment, so that only what Tafl needs goes in:

    py -m venv .venv-build
    .venv-build\\Scripts\\python -m pip install -r requirements-build.lock
    .venv-build\\Scripts\\python -m pip install --no-deps .
    .venv-build\\Scripts\\python tools\\build.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DIST = ROOT / "dist"
APP = DIST / "Tafl"


def main() -> None:
    from etl_tool import __version__

    shutil.rmtree(DIST, ignore_errors=True)
    shutil.rmtree(ROOT / "build", ignore_errors=True)
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "tafl.spec", "--noconfirm", "--distpath", str(DIST), "--workpath", str(ROOT / "build")],
        cwd=ROOT / "packaging",
        check=True,
    )
    _add_files(__version__)
    _check(__version__)
    archive = _zip(__version__)
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file())
    print(f"\n{APP}: {size / 2**20:.0f} MB\n{archive}: {archive.stat().st_size / 2**20:.0f} MB")


def _add_files(version: str) -> None:
    """The README, the licenses of Tafl and of everything included, and the demo."""
    text = (ROOT / "packaging" / "README.txt").read_text(encoding="utf-8").replace("{version}", version)
    (APP / "README.txt").write_text(text, encoding="utf-8", newline="\r\n")
    shutil.copyfile(ROOT / "LICENSE", APP / "LICENSE.txt")
    licenses = APP / "licenses"
    licenses.mkdir()
    for text in (ROOT / "packaging" / "licenses").glob("*.txt"):  # Qt and PySide6: LGPL-3.0, which builds on GPL-3.0
        shutil.copyfile(text, licenses / text.name)
    python = Path(sys.base_prefix) / "LICENSE.txt"
    if python.is_file():
        (licenses / "Python").mkdir(parents=True)
        shutil.copyfile(python, licenses / "Python" / "LICENSE.txt")
    for dist in metadata.distributions():
        name = dist.metadata["Name"]
        if name.lower() in ("pip", "pyinstaller", "pyinstaller-hooks-contrib", "setuptools", "altgraph", "pefile",
                            "pywin32-ctypes", "packaging", "tafl"):  # fmt: skip
            continue  # tools for building, not part of the download
        files = [f for f in dist.files or () if "license" in f.name.lower() or "copying" in f.name.lower() or "notice" in f.name.lower()]
        target = licenses / f"{name}-{dist.version}"
        for file in files:
            source = Path(dist.locate_file(file))
            if source.is_file():
                target.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target / source.name)
        if not files:
            target.mkdir(parents=True, exist_ok=True)
            (target / "LICENSE.txt").write_text(f"{name} {dist.version}: {dist.metadata.get('License') or dist.metadata.get('License-Expression') or 'see the project'}\n", encoding="utf-8")
    examples = APP / "examples"
    (examples / "data").mkdir(parents=True)
    for project in ("demo.tafl", "excel-check.tafl"):
        shutil.copyfile(ROOT / "examples" / project, examples / project)
    for file in (ROOT / "examples" / "data").iterdir():
        if file.is_file():
            shutil.copyfile(file, examples / "data" / file.name)


def _check(version: str) -> None:
    """The command line answers and runs the demo; the window starts, opens it and calculates."""
    cli, window = APP / "tafl.exe", APP / "tafl-app.exe"
    shown = subprocess.run([cli, "--version"], capture_output=True, text=True, check=True).stdout.strip()
    assert shown == f"Tafl {version}", shown
    actions = subprocess.run([cli, "plugins"], capture_output=True, text=True, check=True).stdout
    assert "excel.export" in actions and "calc.columns" in actions and "failed" not in actions.lower(), actions
    with tempfile.TemporaryDirectory() as folder:
        demo = Path(folder) / "examples"
        shutil.copytree(APP / "examples", demo)
        subprocess.run([cli, "run", demo / "demo.tafl", "--preview"], check=True, capture_output=True, timeout=300)
        environment = {**os.environ, "TAFL_STARTUP_CHECK": "1", "QT_QPA_PLATFORM": "offscreen", "LOCALAPPDATA": folder}
        started = time.perf_counter()
        result = subprocess.run([window, demo / "demo.tafl"], env=environment, timeout=120)
        assert result.returncode == 0, f"tafl-app.exe ended with {result.returncode}: see {folder}\\Tafl\\tafl.log"
        print(f"checked: tafl.exe --version, plugins, run --preview; tafl-app.exe opened the demo in {time.perf_counter() - started:.1f} s")


def _zip(version: str) -> Path:
    archive = DIST / f"Tafl-{version}-windows.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as out:
        for file in sorted(APP.rglob("*")):
            if file.is_file():
                out.write(file, Path("Tafl") / file.relative_to(APP))
    return archive


if __name__ == "__main__":
    main()

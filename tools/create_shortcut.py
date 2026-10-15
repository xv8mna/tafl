"""Create a desktop shortcut that starts the app without a console window (Windows only).
It also writes the icon file src/etl_tool/ui/app.ico, drawn by the app's theme.

    .venv\\Scripts\\python tools\\create_shortcut.py
"""

from __future__ import annotations

import os
import struct
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QIODevice  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402

from etl_tool import APP_COMMAND, APP_NAME, OLD_APP_NAMES  # noqa: E402
from etl_tool.ui import theme  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ICON = ROOT / "src" / "etl_tool" / "ui" / "app.ico"
LAUNCHER = Path(sys.executable).with_name(f"{APP_COMMAND}.exe")


def write_icon(path: Path, sizes: tuple[int, ...] = (16, 24, 32, 48, 64, 128, 256)) -> None:
    """An .ico with one PNG image per size (sharp at every size Windows uses)."""
    images = []
    for size in sizes:
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        theme.app_pixmap(size).save(buffer, "PNG")
        images.append((size, bytes(buffer.data())))
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, data = b"", b""
    for size, png in images:
        entries += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), offset + len(data))
        data += png
    path.write_bytes(header + entries + data)


def create_shortcut(target: Path, icon: Path, working_dir: Path) -> Path:
    script = (
        "$desktop = [Environment]::GetFolderPath('Desktop'); "
        f"$link = Join-Path $desktop '{APP_NAME}.lnk'; "
        "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($link); "
        f"$s.TargetPath = '{target}'; "
        f"$s.WorkingDirectory = '{working_dir}'; "
        f"$s.IconLocation = '{icon},0'; "
        f"$s.Description = '{APP_NAME}'; "
        "$s.Save(); "
        "Write-Output $link"
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=True
    )
    return Path(result.stdout.strip())


def main() -> None:
    if sys.platform != "win32":
        sys.exit("Desktop shortcuts are made for Windows only")
    if not LAUNCHER.exists():
        sys.exit(f"{LAUNCHER} is missing: run  pip install -e .  first")
    app = QGuiApplication([])  # noqa: F841 - QPixmap needs an application object
    write_icon(ICON)
    link = create_shortcut(LAUNCHER, ICON, ROOT)
    print(f"Icon:     {ICON}\nShortcut: {link}\nStarts:   {LAUNCHER}")
    for old in OLD_APP_NAMES:
        if (stale := link.with_name(f"{old}.lnk")).exists():
            print(f"The shortcut {stale} (the app's old name) no longer works: you can delete it.")


if __name__ == "__main__":
    main()

"""Write packaging\\licenses\\Qt-third-party.txt: the notices for the code of other projects that is
built into the Qt DLLs of the Windows download.

    python tools\\qt_notices.py qtbase-everywhere-src-<version>.tar.xz

The archive is the source code of Qt's base module, for the Qt version PySide6 is built on, from
https://download.qt.io/archive/qt/<major.minor>/<version>/submodules/. Names, versions, copyright
lines and license texts come from its qt_attribution.json and license files. Which parts are
built into which DLL (PARTS) is worked out by hand from Qt's build files: check it when Qt's
version changes or the download gets other Qt DLLs (tools\\build.py compares them with DLLS).
"""

from __future__ import annotations

import json
import posixpath
import sys
import tarfile
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packaging" / "licenses" / "Qt-third-party.txt"

# The Qt DLLs of the download (in _internal\PySide6), as packaging\tafl.spec leaves them
DLLS = [
    "Qt6Core.dll", "Qt6Gui.dll", "Qt6Svg.dll", "Qt6Widgets.dll", "plugins\\iconengines\\qsvgicon.dll",
    "plugins\\platforms\\qminimal.dll", "plugins\\platforms\\qoffscreen.dll", "plugins\\platforms\\qwindows.dll",
]  # fmt: skip
# The code of other projects in them: (DLL, folder of the qt_attribution.json, its Id). Left out:
# parts for other systems (forkfd, Cocoa, xcb, Wayland, Android, WebAssembly), Pixman (only for
# ARM), FreeType's own zlib (Qt builds FreeType with its zlib), the Adobe Glyph List (not linked
# in), and what only other modules and plugins use (libjpeg, SQLite, libpsl, Qt Test).
PARTS = [
    ("Qt6Core.dll", "src/corelib/mimetypes/3rdparty", "tika-mimetypes"),
    ("Qt6Core.dll", "src/3rdparty/blake2", "blake2"),
    ("Qt6Core.dll", "src/3rdparty/zlib", "zlib"),
    ("Qt6Core.dll", "src/3rdparty/easing", "easing"),
    ("Qt6Core.dll", "src/3rdparty/double-conversion", "doubleconversion"),
    ("Qt6Core.dll", "src/3rdparty/md4", "md4"),
    ("Qt6Core.dll", "src/3rdparty/md5", "md5"),
    ("Qt6Core.dll", "src/3rdparty/pcre2", "pcre2"),
    ("Qt6Core.dll", "src/3rdparty/pcre2", "pcre2-sljit"),
    ("Qt6Core.dll", "src/3rdparty/sha1", "sha1"),
    ("Qt6Core.dll", "src/3rdparty/sha3", "sha3_keccak"),
    ("Qt6Core.dll", "src/3rdparty/sha3", "sha3_endian"),
    ("Qt6Core.dll", "src/3rdparty/rfc6234", "rfc6234"),
    ("Qt6Core.dll", "src/3rdparty/siphash", "siphash"),
    ("Qt6Core.dll", "src/3rdparty/tinycbor", "tinycbor"),
    ("Qt6Core.dll", "src/corelib/global", "tlexpected"),
    ("Qt6Core.dll", "src/corelib/text", "unicode-character-database"),
    ("Qt6Core.dll", "src/corelib/text", "unicode-cldr"),
    ("Qt6Gui.dll", "src/gui/painting", "grayraster"),
    ("Qt6Gui.dll", "src/3rdparty/D3D12MemoryAllocator", "d3d12memoryallocator"),
    ("Qt6Gui.dll", "src/3rdparty/emoji-segmenter", "emoji-segmenter"),
    ("Qt6Gui.dll", "src/3rdparty/freetype", "freetype"),
    ("Qt6Gui.dll", "src/3rdparty/freetype", "freetype-bdf"),
    ("Qt6Gui.dll", "src/3rdparty/freetype", "freetype-pcf"),
    ("Qt6Gui.dll", "src/3rdparty/harfbuzz-ng", "harfbuzz-ng"),
    ("Qt6Gui.dll", "src/3rdparty/libpng", "libpng"),
    ("Qt6Gui.dll", "src/3rdparty/md4c", "md4c"),
    ("Qt6Gui.dll", "src/gui/rhi", "rhi-miniengine-d3d12-mipmap"),
    ("Qt6Gui.dll", "src/gui/opengl", "opengl-es2-headers"),
    ("Qt6Gui.dll", "src/gui/opengl", "opengl-headers"),
    ("Qt6Gui.dll", "src/gui/painting", "smooth-scaling-algorithm"),
    ("Qt6Gui.dll", "src/3rdparty/icc", "icc-srgb-color-profile"),
    ("Qt6Gui.dll", "src/gui/vulkan", "vulkan-xml-spec"),
    ("Qt6Gui.dll", "src/3rdparty/VulkanMemoryAllocator", "vulkanmemoryallocator"),
    ("Qt6Gui.dll", "util/gradientgen", "webgradients"),
    ("Qt6Gui.dll", "src/gui/painting", "xserverhelper"),
    ("Qt6Gui.dll", "src/gui/util", "xsvg"),
    ("plugins\\platforms\\qwindows.dll", "src/3rdparty/wintab", "wintab"),
]
# Names for licenses whose name in qt_attribution.json is older than their text
LICENSE_NAMES = {"Unicode-3.0": "Unicode License v3"}
RULE = "=" * 96


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    version, files = _read(Path(sys.argv[1]))
    sections = sorted((_section(dll, folder, ident, files) for dll, folder, ident in PARTS), key=_order)
    out = [_header(version), "Contents", "--------"]
    for dll in DLLS:
        numbered = [f"  {n}. {s['name']}" for n, s in enumerate(sections, 1) if s["dll"] == dll]
        out += ["", dll, *numbered] if numbered else []
    out.append("")
    printed: dict[str, tuple[int, str]] = {}
    for n, s in enumerate(sections, 1):
        out += ["", RULE, f"{n}. {s['name']}", RULE, "", f"In: {s['dll']}", f"License: {s['license']}", "Copyright:"]
        out += [f"  {line.strip()}" for line in s["copyright"] if line.strip()]
        out.append("")
        if s["text"] in printed:  # e.g. the CC0 of several hash functions: printed once
            first, name = printed[s["text"]]
            out.append(f"License text: the same as in section {first} ({name}).")
        else:
            printed[s["text"]] = (n, s["short"])
            out += ["License text:", "", s["text"]]
        out.append("")
    OUTPUT.write_text("\n".join(out).rstrip("\n") + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} for Qt {version}: {len(sections)} parts")


def _read(archive: Path) -> tuple[str, dict[str, bytes]]:
    """The Qt version and the attribution and license files of the archive (without unpacking it)."""
    files, version = {}, ""
    with tarfile.open(archive) as tar:
        for member in tar:
            top, _, path = member.name.partition("/")
            version = version or top.rsplit("-", 1)[-1]  # qtbase-everywhere-src-6.12.0
            name = path.upper()
            if member.isfile() and (path.endswith("qt_attribution.json") or "LICEN" in name or "COPYING" in name):
                files[path] = tar.extractfile(member).read()
    return version, files


def _section(dll: str, folder: str, ident: str, files: dict[str, bytes]) -> dict:
    def text(path: str) -> str:
        return files[path].decode("utf-8-sig").replace("\r\n", "\n").strip("\n")

    entries = json.loads(text(f"{folder}/qt_attribution.json"), strict=False)
    entry = next(e for e in (entries if isinstance(entries, list) else [entries]) if e["Id"] == ident)
    license_file = entry.get("LicenseFile") or entry.get("LicenseFiles")
    if isinstance(license_file, list):
        raise SystemExit(f"{ident}: several license files")
    # Without a file of its own, the text is Qt's copy of the license (as Qt's documentation does)
    path = posixpath.normpath(f"{folder}/{license_file}") if license_file else f"LICENSES/{entry['LicenseId']}.txt"
    copyright = entry["Copyright"]
    return {
        "dll": dll,
        "short": entry["Name"],
        "name": entry["Name"] + (f", version {entry['Version']}" if entry.get("Version") else ""),
        "license": LICENSE_NAMES.get(entry.get("LicenseId"), entry["License"]),
        "copyright": copyright if isinstance(copyright, list) else copyright.split("\n"),
        "text": text(path),
    }


def _order(section: dict) -> tuple[int, str]:
    return DLLS.index(section["dll"]), section["short"].casefold()


def _header(version: str) -> str:
    title = f"Qt {version}: code from other projects"
    without = [dll.rsplit("\\", 1)[-1] for dll in DLLS if all(dll != part[0] for part in PARTS)]
    paragraphs = [
        f"Qt {version} includes code from other projects. This file has their notices, for the parts that are "
        f"built into the Qt DLLs of this download, in the folder _internal\\PySide6: {_words(DLLS)}. Qt itself is "
        "under the LGPL-3.0: see LGPL-3.0.txt.",
        f"{_words(without)} have no such code of their own.",
        f"Names, versions, copyright lines and license texts are as Qt {version} records them in its source code "
        "(its qt_attribution.json and license files). A license text that several parts share is printed once; "
        "the later sections refer back to it.",
    ]
    filled = [textwrap.fill(p, 96, break_long_words=False, break_on_hyphens=False) for p in paragraphs]
    return "\n\n".join([f"{title}\n{'=' * len(title)}", *filled]) + "\n"


def _words(names: list[str]) -> str:
    return ", ".join(names[:-1]) + " and " + names[-1]


if __name__ == "__main__":
    main()

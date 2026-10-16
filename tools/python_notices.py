"""Write packaging\\licenses\\Python\\ACKNOWLEDGEMENTS.txt: the notices for the code of other
projects that Python includes (LICENSE.txt of Python's installation doesn't have all of them).

    python tools\\python_notices.py <version> license.rst

license.rst is Doc/license.rst of that Python version, e.g. from
https://github.com/python/cpython/blob/v3.14.5/Doc/license.rst. Its part "Licenses and
Acknowledgements for Incorporated Software" goes into the file, as plain text.
"""

from __future__ import annotations

import re
import sys
import textwrap
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "packaging" / "licenses" / "Python" / "ACKNOWLEDGEMENTS.txt"
PART = "Licenses and Acknowledgements for Incorporated Software"


def main() -> None:
    if len(sys.argv) != 3:
        raise SystemExit(__doc__)
    version, source = sys.argv[1], Path(sys.argv[2])
    lines = source.read_text(encoding="utf-8").replace("\r\n", "\n").split("\n")
    start = lines.index(PART) + 2  # after the title and its underline
    text = "\n".join(_plain(line) for line in lines[start:] if not line.startswith(".. "))
    text = re.sub(r"\n{3,}", "\n\n\n", text).strip("\n")
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(f"{_header(version)}\n\n{text}\n", encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT} for Python {version}")


def _plain(line: str) -> str:
    """A line of reStructuredText as plain text: no roles, links and literal markers."""
    line = re.sub(r":[\w:]+:`!?([^`<]*?)\s*(<[^>]*>)?`", r"\1", line)  # :mod:`random`, :file:`x.c`
    line = re.sub(r"`([^`<]+?)\s*<([^>]+)>`_", r"\1 (\2)", line)  # `uvloop <https://...>`_
    line = re.sub(r"``(.+?)``", r"\1", line)
    if line.endswith(" ::"):  # the text before a literal block
        return line[:-3]
    return line[:-1] if line.endswith("::") else line.rstrip()


def _header(version: str) -> str:
    title = f"Python {version}: code from other projects"
    paragraph = (
        f"Python includes code from other projects. This file has their notices, as the documentation of Python "
        f'{version} lists them under "{PART}". Python itself is under the PSF License Agreement: see LICENSE.txt, '
        "which also has the notices for the libraries of Python's Windows build (e.g. bzip2, libffi, OpenSSL and "
        "Zstandard)."
    )
    return f"{title}\n{'=' * len(title)}\n\n{textwrap.fill(paragraph, 96)}"


if __name__ == "__main__":
    main()

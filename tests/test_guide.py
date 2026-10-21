"""The user guide: docs/guide.md with its pictures in docs/guide, and Guide.html of the download,
which tools/build.py makes from it."""

import base64
import importlib.util
import re
import struct
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "guide.md"

_spec = importlib.util.spec_from_file_location("tafl_build", ROOT / "tools" / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

PICTURE = re.compile(r"!\[((?:\\.|[^\[\]\\]|\[[^\]]*\])*)\]\(([^)\s]+)\)")  # descriptions may have [Net] or \[
LINK = re.compile(r"(?<!!)\[[^\]]*\]\(([^)\s]+)\)")


def _without_code(text: str) -> str:
    return re.sub(r"^```.*?^```", "", text, flags=re.M | re.S)


def _anchors(markdown_file: Path) -> list[str]:
    headings = re.findall(r"^#{1,6} (.+)$", _without_code(markdown_file.read_text(encoding="utf-8")), re.M)
    return [build.anchor(heading) for heading in headings]


def test_every_picture_of_the_guide_is_there_and_used():
    text = GUIDE.read_text(encoding="utf-8")
    shown = PICTURE.findall(text)
    assert len(shown) > 40
    assert all(alt.strip() for alt, _ in shown), "every picture needs a description"
    used = {(GUIDE.parent / path).resolve() for _, path in shown}
    assert all(path.is_file() for path in used), sorted(str(p) for p in used if not p.is_file())
    files = {path.resolve() for path in (ROOT / "docs" / "guide").iterdir()}
    assert files - used == set(), "pictures that the guide doesn't show"


def test_links_of_the_guide_lead_somewhere():
    anchors = _anchors(GUIDE)
    assert len(anchors) == len(set(anchors)), "two headings with the same anchor"
    for link in LINK.findall(_without_code(GUIDE.read_text(encoding="utf-8"))):
        path, _, fragment = link.partition("#")
        if link.startswith(("http://", "https://")):
            continue
        target = (GUIDE.parent / path).resolve() if path else GUIDE
        assert target.is_file(), link
        assert target.is_relative_to(ROOT), link
        if fragment:
            assert fragment in _anchors(target), link


def test_anchors_are_those_of_github():
    assert build.anchor("2. Your first project step by step") == "2-your-first-project-step-by-step"
    assert build.anchor("Imports: the first step of a table") == "imports-the-first-step-of-a-table"
    assert build.anchor("Export to CSV / text file") == "export-to-csv--text-file"
    assert build.anchor("Größe und Maße") == "größe-und-maße"


def test_guide_html_embeds_the_pictures_and_links_to_github(tmp_path):
    pytest.importorskip("markdown")
    (tmp_path / "docs" / "pictures").mkdir(parents=True)
    picture = b"\x89PNG\r\n\x1a\n" + bytes(range(40))  # embedded as the file holds it
    (tmp_path / "docs" / "pictures" / "dot.png").write_bytes(picture)
    window = b"\x89PNG\r\n\x1a\n\x00\x00\x00\x0dIHDR" + struct.pack(">II", 1280, 800) + bytes(20)
    (tmp_path / "docs" / "pictures" / "window.png").write_bytes(window)
    (tmp_path / "README.md").write_text("# Readme\n", encoding="utf-8")
    guide = tmp_path / "docs" / "guide.md"
    guide.write_text(
        "# A guide\n\n## Contents\n\n- [1. Start](#1-start)\n- [2. More](#2-more)\n\n## 1. Start\n\n"
        "1. First\n\n    ![A dot](pictures/dot.png)\n\nA paragraph.\n\n3. Third, see the [README](../README.md#install) and "
        "[the start](#1-start).\n\n### The window\n\n![The window](pictures/window.png)\n\n### Run it\n\n## 2. More\n",
        encoding="utf-8",
    )
    html = build.guide_html(guide, root=tmp_path)
    assert "<title>A guide</title>" in html
    assert f'src="data:image/png;base64,{base64.b64encode(picture).decode()}"' in html
    assert 'alt="A dot"' in html
    assert 'id="1-start"' in html and 'href="#1-start"' in html
    assert 'href="https://github.com/xv8mna/tafl/blob/main/README.md#install"' in html
    assert '<ol start="3">' in html
    assert 'href="#contents"' in html  # the link back to the contents
    # the contents list the chapters with their sections
    contents = html[html.index('id="contents"') : html.index('id="1-start"')]
    assert re.findall(r'href="#([^"]+)"', contents) == ["1-start", "the-window", "run-it", "2-more"]
    # a picture wider than the text may be wider than the text column; the dot's size isn't known
    images = {re.search(r'alt="([^"]*)"', tag)[1]: tag for tag in re.findall(r"<img [^>]*>", html)}
    assert all(f in images["The window"] for f in ('class="wide"', 'width="1280"', 'height="800"'))
    assert "class=" not in images["A dot"] and "width=" not in images["A dot"]

    guide.write_text("# A guide\n\n![Missing](pictures/missing.png)\n", encoding="utf-8")
    with pytest.raises(SystemExit, match="missing.png"):
        build.guide_html(guide, root=tmp_path)


def test_the_guide_as_html():
    pytest.importorskip("markdown")
    html = build.guide_html(GUIDE)
    text = GUIDE.read_text(encoding="utf-8")
    sources = re.findall(r'<img [^>]*?src="([^"]*)"', html)
    assert len(sources) == len(PICTURE.findall(text))
    assert all(source.startswith("data:image/png;base64,") for source in sources)
    ids = set(re.findall(r' id="([^"]+)"', html))
    assert set(re.findall(r'href="#([^"]+)"', html)) <= ids
    assert not re.findall(r'href="(?!#|https://)[^"]*"', html), "links to files that are not in the download"

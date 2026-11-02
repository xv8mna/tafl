import datetime as dt
import random
from decimal import Decimal

import polars as pl
from PySide6.QtCore import QLocale, Qt

from tafl.ui import frame_model
from tafl.ui.frame_model import DisplayFormat, FrameModel, _start_of_text
from tafl.ui.labels import LONG_TIP_LINES, tooltip_text

start = _start_of_text

DF = pl.DataFrame(
    {
        "t": ["a", None],
        "i": [1, 2],
        "f": [1234.5, None],
        "d": [dt.date(2025, 12, 31), None],
        "ts": [dt.datetime(2025, 1, 2, 3, 4, 5), None],
        "b": [True, False],
    }
)


def model_for(locale: QLocale) -> FrameModel:
    model = FrameModel()
    model.display = DisplayFormat(locale)
    model.set_frame(DF)
    return model


def test_values_follow_the_regional_settings(qapp):
    german = model_for(QLocale(QLocale.Language.German, QLocale.Country.Germany))
    assert [german.data(german.index(0, c)) for c in range(6)] == ["a", "1", "1234,5", "31.12.2025", "02.01.2025 03:04:05", "true"]
    english = model_for(QLocale(QLocale.Language.English, QLocale.Country.UnitedKingdom))
    assert english.data(english.index(0, 2)) == "1234.5"
    assert english.data(english.index(0, 3)) == "31/12/2025"


def test_exact_decimals_show_every_digit_with_the_regional_separator(qapp):
    values = pl.Series("n", [Decimal("12345678901234.5678"), Decimal("1.1000"), Decimal("-0.0001")], pl.Decimal(18, 4))
    for language, country, separator in (("German", "Germany", ","), ("English", "UnitedKingdom", ".")):
        model = FrameModel()
        model.display = DisplayFormat(QLocale(getattr(QLocale.Language, language), getattr(QLocale.Country, country)))
        model.set_frame(values.to_frame())
        shown = [model.data(model.index(row, 0)) for row in range(3)]
        assert shown == [t.replace(".", separator) for t in ("12345678901234.5678", "1.1000", "-0.0001")]
        assert model.data(model.index(0, 0), Qt.ItemDataRole.TextAlignmentRole) & Qt.AlignmentFlag.AlignRight


def test_empty_cells_headers_and_alignment(qapp):
    model = model_for(QLocale(QLocale.Language.English, QLocale.Country.UnitedKingdom))
    assert model.rowCount() == 2 and model.columnCount() == 6
    assert model.data(model.index(1, 0)) == ""
    assert model.headerData(1, Qt.Orientation.Horizontal) == "i\ninteger"
    assert model.headerData(1, Qt.Orientation.Vertical) == "2"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole) & Qt.AlignmentFlag.AlignRight


def test_a_big_binary_cell_shows_its_first_bytes(qapp):
    """As text a byte takes up to 4 characters, and the grid lays out the text at each paint."""
    frame = pl.DataFrame({"B": [b"\xff" * 3, b"\xff" * 1_000_000]}, schema={"B": pl.Binary})
    model = FrameModel()
    model.set_frame(frame)
    assert model.data(model.index(0, 0)) == str(b"\xff" * 3)
    text = model.data(model.index(1, 0))
    assert text.startswith("b'\\xff") and text.endswith("…") and len(text) < 2_100
    tip = model.data(model.index(1, 0), Qt.ItemDataRole.ToolTipRole)
    assert 0 < len(tip) < 3_000 and "…" in tip


def test_a_big_list_or_record_shows_its_first_characters(qapp):
    """They can hold cells of any size, and Qt lays out the text at each paint."""
    big = b"\xff" * 1_000
    frame = pl.DataFrame(
        {"L": [[big], [b"a"]], "S": [{"x": big}, {"x": b"a"}]},
        schema={"L": pl.List(pl.Binary), "S": pl.Struct({"x": pl.Binary})},
    )
    model = FrameModel()
    model.set_frame(frame)
    for column in (0, 1):
        text = model.data(model.index(0, column))
        assert len(text) == 501 and text.endswith("…")
    assert model.data(model.index(1, 0)) == "[b'a']"


def test_the_time_for_the_start_of_a_list_or_a_record_does_not_grow_with_its_size(qapp):
    """The text of all of a list with a million values took seconds, at each paint, and so did a
    record with many fields."""
    shown = []

    class Value:
        def __repr__(self):
            shown.append(1)
            return "v"

    assert _start_of_text([Value()] * 1_000_000) == "[" + ", ".join(["v"] * 167) + "…"
    assert len(shown) < 1_000
    shown.clear()
    assert _start_of_text({"x": [[Value()] * 1_000] * 1_000}).startswith("{'x': [[v, v")
    assert len(shown) < 1_000
    shown.clear()
    values = "[" + ", ".join(["v"] * 1_000) + "]"
    text = "{" + ", ".join(f"'f{i}': {values}" for i in range(1_000)) + "}"
    assert _start_of_text({f"f{i}": [Value()] * 1_000 for i in range(1_000)}) == text[:500] + "…"
    assert len(shown) < 1_000


def test_the_start_of_a_list_or_a_record_is_the_start_of_all_of_its_text(qapp):
    """Texts and bytes in it are cut too: their quotes must stay as the whole value has them."""
    rng = random.Random(7)
    pieces = ["a", "'", '"', "\\", "\n", "\t", "é", "€", "\x00"]

    def value(depth, small):
        kind = rng.choice(["str", "bytes", "number", "none", "list", "dict"] if depth < 3 else ["str", "bytes", "number"])
        if kind in ("str", "bytes"):
            size = rng.choice([0, 1, 3, 40] if small else [0, 1, 40, 499, 500, 501, 520, 1_200])
            text = "".join(rng.choice(pieces) for _ in range(size))
            return text if kind == "str" else text.encode()
        if kind == "number":
            return rng.choice([1, -2.5, float("nan"), 10**30, Decimal("1.50"), dt.date(2025, 1, 2)])
        if kind == "list":
            count = rng.choice([0, 1, 2, 5] if small else [0, 1, 2, 30, 300])
            return [value(depth + 1, small or count > 2) for _ in range(count)]
        count = rng.choice([1, 3] if small else [1, 3, 60])
        return None if kind == "none" else {f"f{i}": value(depth + 1, small or count > 3) for i in range(count)}

    def whole():
        count = rng.choice([1, 5, 200])
        return rng.choice([[value(1, count > 5) for _ in range(count)], {"a": value(1, False), "b": value(1, False)}])

    # The quotes only after the cut: random texts have both early
    quoted = [["a" * 600 + "'"], ["a" * 600 + '"'], ["a" * 600 + "'\""], [b"a" * 600 + b"'"], {"x": "a" * 4_500 + "'"}]
    # Records that end at the cut after their first field, and records of many small fields
    records = [{"a": "x" * 491, "b": 1}, {"a": "x" * 3_991, "b": 1}, {f"f{i}": i for i in range(1_000)}]
    for cell in [*quoted, *records, *(whole() for _ in range(400))]:
        text = str(cell)
        for most in (500, 4_000):
            assert _start_of_text(cell, most) == (text if len(text) <= most else text[:most] + "…")


def test_the_tooltip_of_a_list_takes_only_the_part_that_it_shows(qapp, monkeypatch):
    frame = pl.DataFrame({"L": [list(range(100_000))]})
    model = FrameModel()
    model.set_frame(frame)
    parts = []
    monkeypatch.setattr(frame_model, "_start_of_text", lambda value, most=500: parts.append(most) or start(value, most))
    tip = model.data(model.index(0, 0), Qt.ItemDataRole.ToolTipRole)
    assert tip == tooltip_text(str(frame["L"][0].to_list()), LONG_TIP_LINES) and 100 * LONG_TIP_LINES in parts


def test_large_frames_are_read_in_blocks(qapp):
    model = FrameModel()
    model.set_frame(pl.DataFrame({"n": range(100_000)}))
    assert model.rowCount() == 100_000
    assert model.data(model.index(99_999, 0)) == "99999"
    assert model.data(model.index(12_345, 0)) == "12345"

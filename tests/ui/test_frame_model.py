import datetime as dt

import polars as pl
from PySide6.QtCore import QLocale, Qt

from tafl.ui.frame_model import DisplayFormat, FrameModel

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


def test_empty_cells_headers_and_alignment(qapp):
    model = model_for(QLocale(QLocale.Language.English, QLocale.Country.UnitedKingdom))
    assert model.rowCount() == 2 and model.columnCount() == 6
    assert model.data(model.index(1, 0)) == ""
    assert model.headerData(1, Qt.Orientation.Horizontal) == "i\ninteger"
    assert model.headerData(1, Qt.Orientation.Vertical) == "2"
    assert model.data(model.index(0, 1), Qt.ItemDataRole.TextAlignmentRole) & Qt.AlignmentFlag.AlignRight


def test_large_frames_are_read_in_blocks(qapp):
    model = FrameModel()
    model.set_frame(pl.DataFrame({"n": range(100_000)}))
    assert model.rowCount() == 100_000
    assert model.data(model.index(99_999, 0)) == "99999"
    assert model.data(model.index(12_345, 0)) == "12345"

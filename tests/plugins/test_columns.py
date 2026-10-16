import datetime as dt

import polars as pl
import pytest

from tafl.api import ActionError
from tafl.api.testing import StubContext


def test_rename(act, people):
    out = act("columns.rename", people, {"columns": [{"column": "Name", "new_name": "Customer"}]})
    assert out.columns == ["Customer", "City", "Age", "Score"]
    with pytest.raises(ActionError, match="exist twice: City"):
        act("columns.rename", people, {"columns": [{"column": "Name", "new_name": "City"}]})


@pytest.mark.parametrize(
    ("mode", "expected"),
    [("keep", ["Age", "Name"]), ("remove", ["City", "Score"]), ("front", ["Age", "Name", "City", "Score"])],
)
def test_select_columns(act, people, mode, expected):
    assert act("columns.select", people, {"mode": mode, "columns": ["Age", "Name"]}).columns == expected


def change(act, df, column, type_, fmt="", ctx=None, **options):
    return act(
        "columns.change_type", df, {"columns": [{"column": column, "type": type_, "format": fmt}], **options}, ctx
    )[column]


def test_text_to_numbers_and_dates(act):
    df = pl.DataFrame({"n": ["1.234,5", "7", "x"], "d": ["31.12.2025", "1.1.2025", "soon"], "b": ["ja", "nein", "?"]})
    ctx = StubContext()
    assert change(act, df, "n", "number", ctx=ctx).to_list() == [1234.5, 7.0, None]
    assert ctx.warnings == ["n: 1 value(s) could not be converted to Decimal number, e.g. 'x' (now empty)"]
    assert change(act, df, "n", "integer").to_list() == [1235, 7, None]  # rounds like Excel
    assert change(act, df, "d", "date").to_list() == [dt.date(2025, 12, 31), dt.date(2025, 1, 1), None]
    assert change(act, df, "d", "date", "d.m.yyyy").to_list()[1] == dt.date(2025, 1, 1)
    assert change(act, df, "b", "boolean").to_list() == [True, False, None]


def test_conversion_errors_can_stop(act):
    with pytest.raises(ActionError, match="could not be converted"):
        change(act, pl.DataFrame({"n": ["x"]}), "n", "number", on_error="fail")


def test_numbers_and_dates_to_text(act):
    df = pl.DataFrame({"f": [1234.567, None], "zip": [1067, 80331], "d": [dt.date(2025, 3, 9), None]})
    assert change(act, df, "f", "text", "#,##0.00").to_list() == ["1.234,57", None]
    assert change(act, df, "f", "text", "0.0", decimal=".").to_list() == ["1234.6", None]
    assert change(act, df, "zip", "text", "00000").to_list() == ["01067", "80331"]
    assert change(act, df, "d", "text").to_list() == ["09.03.2025", None]
    assert change(act, df, "d", "text", "yyyy-mm").to_list() == ["2025-03", None]


def test_same_column_converted_twice(act):
    df = pl.DataFrame({"n": ["1234,5"]})
    params = {"columns": [{"column": "n", "type": "number"}, {"column": "n", "type": "text", "format": "#,##0.00"}]}
    assert act("columns.change_type", df, params)["n"].to_list() == ["1.234,50"]


def test_excel_day_numbers_to_dates(act):
    df = pl.DataFrame({"serial": [45658, 45659]})
    assert change(act, df, "serial", "date").to_list() == [dt.date(2025, 1, 1), dt.date(2025, 1, 2)]

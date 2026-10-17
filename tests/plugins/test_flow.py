import polars as pl
import pytest

from tafl.api import Params, SkipRest, TaflError, ValidationContext
from tafl.api.testing import StubContext

EMPTY = pl.DataFrame({"Id": [], "Name": []}, schema={"Id": pl.Int64, "Name": pl.String})


def test_this_table_with_rows_passes_through(act, people):
    out = act("flow.skip_if_empty", people, {})
    assert out.equals(people)


def test_this_table_without_rows_skips_the_rest(act):
    with pytest.raises(SkipRest) as raised:
        act("flow.skip_if_empty", EMPTY, {"which": "this"})
    assert raised.value.message == "The table is empty"
    assert raised.value.df is None  # the table keeps the data going into the step
    assert isinstance(raised.value, TaflError)


def test_another_table(act, people):
    ctx = StubContext(tables={"t2": EMPTY, "t3": people})
    with pytest.raises(SkipRest, match="The table “t2” is empty"):
        act("flow.skip_if_empty", people, {"which": "other", "table": "t2"}, ctx=ctx)
    assert act("flow.skip_if_empty", EMPTY, {"which": "other", "table": "t3"}, ctx=ctx).equals(EMPTY)


def test_settings_and_summary(registry):
    action = registry.actions["flow.skip_if_empty"]
    assert action.category == "Rows" and action.version == 1
    assert action.describe(action.resolve({})) == "Skip the rest if the table is empty"
    assert action.describe(Params(which="other", table="Orders")) == "Skip the rest if Orders is empty"
    assert action.describe(Params(which="other", table="")) == "Skip the rest if another table is empty"
    assert action.table_refs(action.resolve({"table": "t2"})) == []  # only used for "another table"
    assert action.table_refs(action.resolve({"which": "other", "table": "t2"})) == ["t2"]
    assert action.check(action.resolve({"which": "other"}), ValidationContext()) == ["Table: required"]

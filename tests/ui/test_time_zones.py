"""Date-times with a time zone. Python on Windows reads time zones from the tzdata package:
without it, the grid and the filter pane fail on these columns."""

import datetime as dt

import polars as pl
import pytest

from tafl.core import Project

WINTER, SUMMER = dt.datetime(2026, 1, 15, 12), dt.datetime(2026, 7, 15, 12)  # in UTC
MOMENTS = [t.replace(tzinfo=dt.UTC) for t in (WINTER, SUMMER, WINTER)]


@pytest.fixture
def times(tmp_path, registry):
    """A project that imports a Parquet file with the same moments in Berlin time and in UTC."""
    utc = pl.Series([WINTER, SUMMER, WINTER]).dt.replace_time_zone("UTC")
    data = pl.DataFrame({"Berlin": utc.dt.convert_time_zone("Europe/Berlin"), "UTC": utc})
    data.write_parquet(tmp_path / "times.parquet")
    project = Project(name="Times")
    project.add_step(project.add_table("Times"), registry.actions["parquet.import"], {"file": "times.parquet"})
    return project.save(tmp_path / "times.tafl")


def test_the_grid_shows_date_times_with_a_time_zone(window, settle, session, times):
    window.open_path(times)
    settle(session)
    model = window.data_panel.model
    assert model.frame.schema["Berlin"] == pl.Datetime("us", "Europe/Berlin")
    berlin, utc = model.frame.columns.index("Berlin"), model.frame.columns.index("UTC")
    assert [model.value(row, berlin) for row in range(3)] == MOMENTS  # the same moments
    assert [model.value(row, utc) for row in range(3)] == MOMENTS
    assert [model.value(row, berlin).utcoffset() for row in range(2)] == [dt.timedelta(hours=1), dt.timedelta(hours=2)]
    shown = [model.index(row, column).data() for column in (berlin, utc) for row in range(2)]
    assert [text[-8:] for text in shown] == ["13:00:00", "14:00:00", "12:00:00", "12:00:00"]  # in the column's zone


def test_the_filter_pane_lists_date_times_with_a_time_zone(window, settle, session, times):
    window.open_path(times)
    settle(session)
    panel = window.data_panel
    panel.filter_by("Berlin")
    panel.filter_by("UTC")
    berlin, utc = panel.filter_pane.cards
    listed = {row[2]: row[1] for row in berlin.model.rows}  # the stored value and its count
    assert listed == {"2026-01-15T13:00:00+01:00": 2, "2026-07-15T14:00:00+02:00": 1}
    listed = {row[2]: row[1] for row in utc.model.rows}
    assert listed == {"2026-01-15T12:00:00+00:00": 2, "2026-07-15T12:00:00+00:00": 1}

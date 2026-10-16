"""Instant filters (the filter pane next to the grid)."""

import datetime as dt
import time

import polars as pl
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from tafl.api.testing import StubContext
from tafl.core import Project
from tafl.ui import app
from tafl.ui.filter_pane import FilterPane
from tafl.ui.filters import (
    Filter,
    apply,
    facet,
    facets_for,
    formula,
    parse_thresholds,
    range_labels,
    to_json,
    unusable,
)

DATA = pl.DataFrame(
    {
        "City": ["Berlin", "Hamburg", None, "Berlin", "Köln", ""],
        "Net": [10.5, 20.0, 5.0, None, 7.25, 1.0],
        "Date": [dt.date(2025, 1, 1), dt.date(2025, 1, 2), None, dt.date(2025, 1, 1), dt.date(2025, 2, 1), dt.date(2025, 2, 1)],
        "Paid": [True, False, None, True, True, False],
    }
)


def test_filters_keep_rows_with_any_of_their_values_and_all_filters():
    assert apply(DATA, [Filter("City", ["Berlin", None])]).height == 3  # empty cells can be picked too
    both = apply(DATA, [Filter("City", ["Berlin", "Köln"]), Filter("Date", ["2025-01-01"])])
    assert both["City"].to_list() == ["Berlin", "Berlin"]
    assert apply(DATA, [Filter("Missing", ["x"]), Filter("Net", [])]) is DATA  # nothing to filter by
    assert apply(DATA, [Filter("Net", [7.25, "1"]), Filter("Paid", [False])])["City"].to_list() == [""]


def test_facets_count_values_among_the_rows_of_the_other_filters():
    filters = [Filter("City", ["Berlin"]), Filter("Date", ["2025-02-01"])]
    cities, rows = facet(DATA, filters, 0)
    counts = dict(cities.iter_rows())
    assert rows == 2 and counts == {"Berlin": 0, "Köln": 1, "": 1, "Hamburg": 0, None: 0}
    dates, rows = facet(DATA, filters, 1)  # with City = Berlin
    assert rows == 2 and dict(dates.iter_rows())[dt.date(2025, 1, 1)] == 2
    assert facet(DATA, [Filter("Missing")], 0) is None


@pytest.mark.parametrize(
    "chosen",
    [
        Filter("City", ["Berlin", None, "O'Hara"]),
        Filter("Date", ["2025-01-01"]),
        Filter("Net", [10.5, 7.25]),
        Filter("Paid", [False]),
    ],
)
def test_a_filter_as_formula_keeps_the_same_rows(registry, chosen):
    action = registry.actions["rows.filter_formula"]
    condition = formula(chosen, DATA.schema[chosen.column])
    assert action.run(DATA, action.resolve({"condition": condition}), StubContext()).equals(apply(DATA, [chosen]))


def test_values_are_stored_as_json():
    assert [to_json(v) for v in (dt.date(2025, 1, 2), dt.datetime(2025, 1, 2, 3, 4), "x", 1.5, None)] == [
        "2025-01-02", "2025-01-02T03:04:00", "x", 1.5, None,
    ]


def test_filters_are_saved_but_are_not_edits(session, qtbot):
    a = session.project.add_table("A")
    changed = []
    session.filters_changed.connect(changed.append)
    session.set_filters(a.id, [{"column": "City", "values": ["Berlin"]}])
    assert changed == [a.id] and session.dirty and not session.can_undo
    data = session.project.to_dict()
    assert data["tables"][0]["filters"] == [{"column": "City", "values": ["Berlin"]}]
    assert Project.from_dict(data).tables[0].filters == [{"column": "City", "values": ["Berlin"]}]
    session.rename_table(a.id, "B")
    session.set_filters(a.id, [])
    session.undo()  # undoes the rename, not the filters
    assert session.project.table(a.id).name == "A" and session.filters(a.id) == []


def open_orders(window, settle, session, demo, step):
    window.open_path(demo)
    orders = session.project.table("Orders")
    session.select_table(orders.id)
    session.select_step(orders.steps[step - 1].id)
    settle(session)
    return window.data_panel, orders


def listed(card) -> dict[str, int]:
    """The values a card shows (as text), with their counts, in list order."""
    return {card.model.index(row, 0).data(): card.model.rows[row][1] for row in range(card.model.rowCount())}


def row_of(card, text: str) -> int:
    return list(listed(card)).index(text)


def test_the_filter_pane_follows_rows_through_the_steps(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    all_rows = panel.model.frame.height
    panel.filter_by("City")
    assert panel.filter_button.isChecked() and not panel.filter_pane.isHidden()
    (card,) = panel.filter_pane.cards
    cities = listed(card)
    assert list(cities)[0] == "(empty)" and "Hamburg" in cities  # orders without a customer have no city

    card.view.selectRow(row_of(card, "Hamburg"))
    assert card.apply_button.isEnabled()
    card.apply()
    assert panel.model.frame.height == cities["Hamburg"] < all_rows
    assert panel.filter_button.text() == "Filters (1)" and "filtered" in panel.info.text()
    assert session.filters(orders.id) == [{"column": "City", "values": ["Hamburg"]}]

    session.select_step(orders.steps[1].id)  # before the merge: there is no City yet
    settle(session)
    (card,) = panel.filter_pane.cards
    assert "Not used at this step" in card.status.text() and card.view.isHidden()
    assert "filtered" not in panel.info.text()
    session.select_step(orders.steps[5].id)
    settle(session)
    assert panel.model.frame.height == cities["Hamburg"]

    panel.filter_pane.add_as_step(0)  # a real step after the selected one; the filter goes away
    settle(session)
    assert orders.steps[6].action == "rows.filter_formula" and session.step is orders.steps[6]
    assert orders.steps[6].params["condition"] == "[City] IN ('Hamburg')"
    assert session.filters(orders.id) == [] and panel.filter_pane.cards == []
    assert panel.model.frame.height == cities["Hamburg"]


def test_quick_selection_search_and_clear(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    panel.filter_by("Status")
    (card,) = panel.filter_pane.cards
    statuses = set(listed(card))
    card.search.setText("E")  # geliefert, offen (not case sensitive)
    visible = set(listed(card))
    assert visible == {s for s in statuses if "e" in s}
    card.quick_select("all")
    card.apply()
    assert set(session.filters(orders.id)[0]["values"]) == visible
    (card,) = panel.filter_pane.cards
    card.search.clear()
    card.quick_select("excluded")  # inverts the selection
    card.apply()
    assert set(session.filters(orders.id)[0]["values"]) == statuses - visible
    (card,) = panel.filter_pane.cards
    card.clear_button.click()
    assert session.filters(orders.id) == [{"column": "Status", "values": []}]
    assert "filtered" not in panel.info.text()


def test_sorting_and_searching_keep_the_selection(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    panel.filter_by("City")
    (card,) = panel.filter_pane.cards
    card.view.selectRow(row_of(card, "Wien"))
    header = card.view.horizontalHeader()
    header.setSortIndicator(1, Qt.SortOrder.DescendingOrder)  # by count, most first
    counts = list(listed(card).values())
    assert counts == sorted(counts, reverse=True)
    card.search.setText("i")
    assert card.pending() == ["Wien"]
    card.search.clear()
    assert card.pending() == ["Wien"] and card.apply_button.isEnabled()


def test_show_only_rows_with_a_value_from_the_grid(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    column = panel.model.frame.columns.index("CustomerNo")
    value = panel.model.value(0, column)
    panel.filter_by("CustomerNo", [to_json(value)])  # what the cell's right-click menu does
    assert set(panel.model.frame["CustomerNo"].to_list()) == {value}
    other = session.project.table("Customers")
    session.select_table(other.id)  # every table has its own filters
    settle(session)
    assert panel.filter_pane.cards == [] and "filtered" not in panel.info.text()


def test_double_click_filters_by_one_value(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    panel.filter_by("Size")
    (card,) = panel.filter_pane.cards
    card.apply_only(card.model.index(row_of(card, "large"), 0))
    assert set(panel.model.frame["Size"].to_list()) == {"large"}
    (card,) = panel.filter_pane.cards
    assert list(listed(card)) == ["large", "normal"]  # applied values come first
    assert card.view.selectionModel().isRowSelected(0)
    assert card.model.data(card.model.index(1, 0), Qt.ItemDataRole.ForegroundRole) is not None  # "normal" is grey


def test_many_different_values_stay_fast(session):
    """A column with 60,000 different values (once took half a minute)."""
    df = pl.DataFrame({"i": pl.int_range(0, 300_000, eager=True)}).select(
        Customer=pl.format("C{}", pl.col("i") % 60_000), Net=(pl.col("i") % 997) / 10
    )
    table = session.project.add_table("Big")
    session.set_filters(table.id, [{"column": "Customer", "values": ["C17"]}, {"column": "Net", "values": []}])
    pane = FilterPane(session)
    started = time.perf_counter()
    pane.show_data(table.id, df)
    assert time.perf_counter() - started < 5
    assert pane.cards[0].model.rows[0][2] == "C17" and len(pane.cards[0].model.all) == 60_000


def test_an_error_dialog_comes_once_per_place(window, monkeypatch):
    shown = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: shown.append(box.text()))
    hook = app._excepthook(window)

    def fail(text):
        try:
            raise RuntimeError(text)
        except RuntimeError as error:
            hook(type(error), error, error.__traceback__)

    for _ in range(3):  # e.g. an error while drawing: it comes back with every repaint
        fail("in paint")
    try:
        {}["missing"]
    except KeyError as error:
        hook(type(error), error, error.__traceback__)
    assert shown == ["RuntimeError: in paint", "KeyError: 'missing'"]


MODES = pl.DataFrame(
    {
        "Date": [dt.date(2025, 1, 1), dt.date(2024, 12, 30), dt.date(2025, 6, 15), None, dt.date(2025, 2, 1)],
        "Stamp": [dt.datetime(2025, 1, 1, 10), dt.datetime(2024, 12, 30, 23), None, dt.datetime(2025, 3, 31), dt.datetime(2025, 3, 1)],
        "Name": ["abc", "GmbH & Co", None, "", "Müller GmbH"],
        "Net": [99.5, 100.0, 1000.0, None, float("nan")],
    }
)


@pytest.mark.parametrize(
    "chosen",
    [
        Filter("Date", ["2025-01", None], "period", {"by": "month"}),
        Filter("Date", ["2025-W01"], "period", {"by": "week"}),  # ISO week: 30 Dec 2024 is in 2025's first week
        Filter("Date", ["2025 Q1"], "period", {"by": "quarter"}),
        Filter("Date", ["2024"], "period", {"by": "year"}),
        Filter("Stamp", ["2025-03-31", "2024-12-30"], "period", {"by": "date"}),
        Filter("Net", [1], "ranges", {"thresholds": [100, 1000], "lower_inclusive": True}),
        Filter("Net", [1, 2], "ranges", {"thresholds": [100, 1000], "lower_inclusive": False}),
        Filter("Net", [0, None], "ranges", {"thresholds": [100]}),
        Filter("Name", [0, 3], "length"),
        Filter("Name", ["match"], "regex", {"pattern": "GmbH$"}),
        Filter("Name", ["no match", None], "regex", {"pattern": "(?i)gmbh"}),
    ],
)
def test_every_mode_as_formula_keeps_the_same_rows(registry, chosen):
    action = registry.actions["rows.filter_formula"]
    condition = formula(chosen, MODES.schema[chosen.column])
    assert action.run(MODES, action.resolve({"condition": condition}), StubContext()).equals(apply(MODES, [chosen]))


def test_modes_group_the_values():
    months, _ = facet(MODES, [Filter("Date", [], "period", {"by": "month"})], 0)
    assert dict(months.iter_rows()) == {"2025-01": 1, "2024-12": 1, "2025-06": 1, "2025-02": 1, None: 1}
    ranges, _ = facet(MODES, [Filter("Net", [], "ranges", {"thresholds": [100, 1000]})], 0)
    assert dict(ranges.iter_rows()) == {0: 1, 1: 1, 2: 2, None: 1}  # NaN counts as the biggest number
    lengths, _ = facet(MODES, [Filter("Name", [], "length")], 0)
    assert dict(lengths.iter_rows()) == {3: 1, 9: 1, 11: 1, 0: 1, None: 1}
    assert facets_for(pl.Date) == ["values", "period"] and facets_for(pl.Float64) == ["values", "ranges"]
    assert facets_for(pl.String) == ["values", "length", "regex"] and facets_for(pl.Boolean) == ["values"]
    assert unusable(Filter("Name", ["2025"], "period"), MODES.schema) == "time periods need a date column"
    assert unusable(Filter("Name", [], "regex", {"pattern": "("}), MODES.schema) == "the pattern is not valid: unclosed group"
    assert apply(MODES, [Filter("Name", ["match"], "regex", {"pattern": "("})]) is MODES  # not used, no error


def test_thresholds_and_their_ranges():
    assert parse_thresholds("1000; 100 1,5 1.000,5 2,000,000 x") == [1.5, 100, 1000, 1000.5, 2_000_000]
    assert parse_thresholds("1,000", decimal=",") == [1.0]  # a decimal comma
    assert range_labels([100, 1000], True, str) == ["< 100", "100 to < 1000", "≥ 1000"]
    assert range_labels([100, 1000], False, str) == ["≤ 100", "> 100 to ≤ 1000", "> 1000"]
    assert range_labels([], True, str) == ["all numbers"]


def test_modes_are_saved_with_the_filter():
    chosen = Filter("Date", ["2025-01"], "period", {"by": "month"})
    assert chosen.to_dict() == {"column": "Date", "facet": "period", "options": {"by": "month"}, "values": ["2025-01"]}
    assert Filter.from_dict(chosen.to_dict()) == chosen
    assert Filter.from_dict({"column": "City", "values": ["x"]}).facet == "values"  # filters saved before modes
    assert Filter.from_dict({"column": "City", "facet": "unknown"}).facet == "values"


def test_filter_dates_by_month_then_make_it_a_step(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    total = panel.model.frame.height
    panel.filter_by("Date")
    (card,) = panel.filter_pane.cards
    card._fill_mode_menu()
    usable = {a.text(): a.isEnabled() for a in card.mode_menu.actions()}
    assert usable == {"Values": True, "Time periods": True, "Number ranges": False, "Text length": False, "Pattern (regex)": False}
    panel.filter_pane.set_facet(0, "period")  # what the mode menu does
    (card,) = panel.filter_pane.cards
    months = listed(card)
    assert card.mode.text().startswith("Time periods") and sum(months.values()) == total
    assert all(len(month) == 7 for month in months)  # 2026-01 ...
    card.period.setCurrentIndex(card.period.findData("quarter"))
    (card,) = panel.filter_pane.cards
    quarters = listed(card)
    assert all(" Q" in quarter for quarter in quarters) and sum(quarters.values()) == total
    first = next(iter(quarters))
    card.view.selectRow(row_of(card, first))
    card.apply()
    assert panel.model.frame.height == quarters[first]
    panel.filter_pane.add_as_step(0)
    settle(session)
    step = session.step
    assert step.action == "rows.filter_formula" and "quarter([Date])" in step.params["condition"]
    assert panel.model.frame.height == quarters[first]


def test_number_ranges_text_length_and_patterns(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    pane = panel.filter_pane
    panel.filter_by("Net")
    pane.set_facet(0, "ranges")
    (card,) = pane.cards
    assert listed(card) == {"all numbers": panel.model.frame.height}
    card.limits.setText("1000; 500")
    card.limits.editingFinished.emit()
    (card,) = pane.cards
    assert card.limits.text() == "500; 1000"
    ranges = listed(card)
    assert list(ranges) == ["< 500", "500 to < 1000", "≥ 1000"]
    card.apply_only(card.model.index(row_of(card, "≥ 1000"), 0))
    assert panel.model.frame["Net"].min() >= 1000 and panel.model.frame.height == ranges["≥ 1000"]

    pane.add_filter("CustomerNo", again=True)
    pane.set_facet(1, "length")
    lengths = listed(pane.cards[1])
    assert "5 characters" in lengths

    pane.add_filter("Name", again=True)
    pane.set_facet(2, "regex")
    pane.set_options(2, {"pattern": "("})
    card = pane.cards[2]
    assert card.status.text() == "Not used at this step: the pattern is not valid: unclosed group." and card.view.isHidden()
    pane.set_options(2, {"pattern": "(?i)gmbh"})
    card = pane.cards[2]
    assert set(listed(card)) <= {"matches", "doesn't match"} and not card.view.isHidden()

    pane.add_filter("Net", again=True)  # a second filter on the same column
    assert [c.column for c in pane.cards] == ["Net", "CustomerNo", "Name", "Net"]

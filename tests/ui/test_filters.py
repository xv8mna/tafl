"""Instant filters (the filter pane next to the grid)."""

import datetime as dt
import json
import math
import time
from decimal import Decimal as D

import polars as pl
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication, QMenu, QMessageBox, QWidget

from tafl.api.testing import StubContext
from tafl.core import Project
from tafl.ui import app, data_panel, filter_pane
from tafl.ui.data_panel import DataPanel
from tafl.ui.filter_pane import MAX_CARDS, FilterPane
from tafl.ui.filters import (
    PERIODS,
    Filter,
    NotAStep,
    apply,
    facet,
    facets_for,
    formula,
    from_json,
    parse_thresholds,
    range_labels,
    selection,
    stored_values,
    to_json,
    unusable,
)
from tafl.ui.labels import tooltip_text

DATA = pl.DataFrame(
    {
        "City": ["Berlin", "Hamburg", None, "Berlin", "Köln", ""],
        "Net": [10.5, 20.0, 5.0, None, 7.25, 1.0],
        "Date": [dt.date(2025, 1, 1), dt.date(2025, 1, 2), None, dt.date(2025, 1, 1), dt.date(2025, 2, 1), dt.date(2025, 2, 1)],
        "Paid": [True, False, None, True, True, False],
        "Code": [b"a", b"\x00\xff", None, b"a", b"it's", b""],
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
        Filter("Code", ["0x61", "0x00FF"]),
        Filter("Code", ["0x", "0x69742773", None]),  # no bytes, a quote, empty cells
    ],
)
def test_a_filter_as_formula_keeps_the_same_rows(registry, chosen):
    action = registry.actions["rows.filter_formula"]
    condition = formula(chosen, DATA.schema[chosen.column])
    filtered = apply(DATA, [chosen])
    assert filtered.height and action.run(DATA, action.resolve({"condition": condition}), StubContext()).equals(filtered)


def test_values_are_stored_as_json():
    assert [to_json(v) for v in (dt.date(2025, 1, 2), dt.datetime(2025, 1, 2, 3, 4), "x", 1.5, None)] == [
        "2025-01-02", "2025-01-02T03:04:00", "x", 1.5, None,
    ]
    assert [to_json(v) for v in (b"\x00\xff", b"", [dt.date(2025, 1, 2), None], {"a": [b"a"]})] == [
        "0x00FF", "0x", ["2025-01-02", None], {"a": ["0x61"]},
    ]


def test_binary_values_are_read_from_their_hex_text():
    assert from_json(["0x00FF", "0x", None, "abc", "0xZZ", "0x0x61", 7, 1.5, True], pl.Binary) == [b"\x00\xff", b""]
    assert apply(DATA, [Filter("Code", ["0x", "0x69742773"])])["Code"].to_list() == [b"it's", b""]
    assert apply(DATA, [Filter("Code", ["abc", 7])]).height == 0
    assert apply(DATA, [Filter("Code", ["abc", 7], excluded=True)]).height == DATA.height


@pytest.mark.parametrize("stored", ["", " ", "61", "0x61 ", "0x00ff", "abcd", 5])
def test_a_binary_value_counts_only_in_the_form_that_is_written(stored):
    assert from_json([stored], pl.Binary) == []
    assert apply(DATA, [Filter("Code", [stored])]).height == 0
    assert apply(DATA, [Filter("Code", [stored], excluded=True)]).height == DATA.height
    assert formula(Filter("Code", [stored]), pl.Binary) == "FALSE"


def test_the_check_of_a_big_binary_value_keeps_no_state_per_byte():
    """It runs in each condition, in the window's thread: a repeated group in the pattern takes
    about 0.8 s and 1 GB for 5 MB."""
    value = "0x" + "AB" * 5_000_000
    started = time.perf_counter()
    assert len(from_json([value], pl.Binary)[0]) == 5_000_000
    assert time.perf_counter() - started < 0.4


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
    card.search.setText("L")  # geliefert (not case sensitive)
    visible = set(listed(card))
    assert visible == {s for s in statuses if "l" in s} == {"geliefert"}
    card.quick_select("all")
    card.apply()
    assert set(session.filters(orders.id)[0]["values"]) == visible
    (card,) = panel.filter_pane.cards
    card.search.clear()
    card.quick_select("excluded")  # inverts the selection
    card.apply()
    assert set(session.filters(orders.id)[0]["values"]) == statuses - visible
    (card,) = panel.filter_pane.cards
    card.quick_select("all")  # all values: stored as "all except none", it doesn't filter
    card.apply()
    assert session.filters(orders.id) == [{"column": "Status", "except": True, "values": []}]
    (card,) = panel.filter_pane.cards
    assert len(card.pending()) == len(statuses) and not card.apply_button.isEnabled()
    assert "filtered" not in panel.info.text() and panel.filter_button.text() == "Filters"
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


def test_texts_are_listed_in_the_order_of_sort(session):
    df = pl.DataFrame({"Name": ["Zebra", "apfel", "Äpfel", "Apfel", "Özdemir", None, "zebra", "Peter", "Straße", "Strasse"]})
    table = session.project.add_table("Names")
    session.set_filters(table.id, [{"column": "Name", "values": []}])
    pane = FilterPane(session)
    pane.show_data(table.id, df)
    model = pane.cards[0].model
    assert [row[0] for row in model.rows] == [
        None, "Apfel", "apfel", "Äpfel", "Özdemir", "Peter", "Strasse", "Straße", "Zebra", "zebra",
    ]


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


def test_weeks_before_the_year_1000_as_formula_keep_the_same_rows(registry):
    """The year of a week had no leading zeros (1-W01), but the step writes four digits (0001-W01):
    it kept no row."""
    days = pl.DataFrame({"Date": [dt.date(1, 1, 1), dt.date(1, 1, 8), dt.date(999, 6, 15), dt.date(999, 12, 31), None]})
    weeks, _ = facet(days, [Filter("Date", [], "period", {"by": "week"})], 0)
    assert sorted(weeks["value"].drop_nulls()) == ["0001-W01", "0001-W02", "0999-W24", "1000-W01"]
    action = registry.actions["rows.filter_formula"]
    for week in weeks["value"].drop_nulls():
        chosen = Filter("Date", [week], "period", {"by": "week"})
        condition = formula(chosen, pl.Date)
        assert action.run(days, action.resolve({"condition": condition}), StubContext()).equals(apply(days, [chosen])), week
        assert apply(days, [chosen]).height == 1


def test_a_stored_week_of_a_year_before_1000_keeps_its_rows(registry):
    """Such a week was stored with a shorter year (24-W18) before the year had four digits: the
    filter kept no row, and with "except" it no longer left the row out."""
    days = pl.DataFrame({"Date": [dt.date(24, 5, 1), dt.date(2024, 5, 1), dt.date(1, 1, 1)], "n": [1, 2, 3]})
    stored = {"column": "Date", "facet": "period", "options": {"by": "week"}, "values": ["24-W18", "1-W01"]}
    chosen = Filter.from_dict(stored)
    assert apply(days, [chosen])["n"].to_list() == [1, 3]
    assert apply(days, [Filter.from_dict({**stored, "except": True})])["n"].to_list() == [2]
    assert chosen.values == ["0024-W18", "0001-W01"]
    action = registry.actions["rows.filter_formula"]
    assert action.run(days, action.resolve({"condition": formula(chosen, pl.Date)}), StubContext())["n"].to_list() == [1, 3]
    assert Filter.from_dict({**stored, "values": ["-2-W52", "2024-W18", "24", 7, None]}).values == ["-002-W52", "2024-W18", "24", 7, None]
    assert Filter.from_dict({**stored, "options": {"by": "month"}}).values == ["24-W18", "1-W01"]  # only weeks
    assert Filter.from_dict({"column": "Date", "values": ["24-W18"]}).values == ["24-W18"]


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


@pytest.mark.parametrize(
    "chosen",
    [
        Filter("City", ["Berlin", None], excluded=True),
        Filter("City", ["Köln"], excluded=True),  # empty cells stay
        Filter("Net", [10.5], excluded=True),
        Filter("Date", [], excluded=True),  # all values: doesn't filter
        Filter("Code", ["0x61", None], excluded=True),
    ],
)
def test_all_values_except_some(registry, chosen):
    expected = DATA.filter(~pl.col(chosen.column).is_in(from_json(chosen.values, DATA.schema[chosen.column])).fill_null(False))
    if None in chosen.values:
        expected = expected.filter(pl.col(chosen.column).is_not_null())
    assert apply(DATA, [chosen]).equals(expected)
    action = registry.actions["rows.filter_formula"]
    condition = formula(chosen, DATA.schema[chosen.column])
    assert action.run(DATA, action.resolve({"condition": condition}), StubContext()).equals(expected)
    assert Filter.from_dict(chosen.to_dict()) == chosen


def test_a_long_selection_is_stored_as_the_values_left_out():
    offered = [f"C{i}" for i in range(2000)]
    assert selection(offered[:3], offered) == (offered[:3], False)
    assert selection(offered, offered) == ([], True)  # all of them
    assert selection(offered[:1900], offered) == (offered[1900:], True)
    assert selection(offered[:900], offered) == (offered[:900], False)  # the values left out are more
    assert selection([], offered) == ([], False)


def test_all_values_then_apply_is_fast_and_short(session, qtbot):
    """100,000 different values: All, then Apply (once took 8 s and stored every value)."""
    df = pl.DataFrame({"Customer": [f"C{i}" for i in range(100_000)], "Net": range(100_000)})
    table = session.project.add_table("Big")
    session.set_filters(table.id, [{"column": "Customer", "values": []}])
    pane = FilterPane(session)
    qtbot.addWidget(pane)
    pane.resize(300, 700)
    pane.show()
    pane.show_data(table.id, df)
    (card,) = pane.cards
    QApplication.processEvents()
    started = time.perf_counter()
    card.quick_select("all")
    QApplication.processEvents()  # drawing the selection
    card.apply()
    pane.show_data(table.id, df)
    (card,) = pane.cards
    QApplication.processEvents()
    assert time.perf_counter() - started < 2
    assert session.filters(table.id) == [{"column": "Customer", "except": True, "values": []}]
    assert len(card.pending()) == 100_000 and not card.apply_button.isEnabled()

    card._select_rows([row for row in range(100_000) if row != 5])  # all but one
    card.apply()
    assert session.filters(table.id) == [{"column": "Customer", "except": True, "values": [card.model.rows[5][2]]}]
    assert apply(df, [Filter.from_dict(f) for f in session.filters(table.id)]).height == 99_999


MALFORMED = [
    {"values": 7},
    {"values": 1.5},
    {"values": True},
    {"values": "abc"},
    {"values": {"a": 1}},
    {"values": [[1]]},
    {"values": [{"a": 1}]},
    {"values": ["Berlin", ["x"], {"a": 1}]},
    {"options": 7},
    {"options": 1.5},
    {"options": True},
    {"options": "x"},
    {"options": ["x"]},
    {"options": [[1]]},
    {"facet": 7},
    {"facet": ["period"]},
    {"except": "yes"},
    {"facet": "period", "options": {"by": []}},
    {"facet": "period", "options": {"by": {}}},
    {"facet": "period", "options": {"by": 10**400}},
    {"facet": "ranges", "options": {"thresholds": 7}},
    {"facet": "ranges", "options": {"thresholds": "abc"}},
    {"facet": "ranges", "options": {"thresholds": [10**400, "x", None, [1], {}]}},
    {"facet": "ranges", "options": {"lower_inclusive": []}},
    {"facet": "regex", "options": {"pattern": 7}},
    {"facet": "regex", "options": {"pattern": ["x"]}},
]


@pytest.mark.parametrize("column", DATA.columns)
@pytest.mark.parametrize("malformed", MALFORMED, ids=repr)
def test_a_malformed_filter_is_no_error(session, qtbot, column, malformed):
    """A project file can hold anything. Before, each refresh ended in an "Unexpected error"."""
    data = {"column": column, "values": [], **malformed}
    table = session.project.add_table("T")
    table.filters = [data]
    f = Filter.from_dict(data)
    assert apply(DATA, [f]).height <= DATA.height
    facet(DATA, [f], 0)
    stored = f.to_dict()
    assert isinstance(stored["values"], list) and isinstance(stored.get("options", {}), dict)
    pane = FilterPane(session)
    qtbot.addWidget(pane)
    pane.show_data(table.id, DATA)
    assert [card.column for card in pane.cards] == [column]


def test_what_is_right_in_a_malformed_filter_stays():
    f = Filter.from_dict({"column": "City", "values": ["Berlin", ["x"], None, {"a": 1}, 7], "options": {"by": [], "other": 1}})
    assert f.values == ["Berlin", None, 7] and f.options == {"other": 1}
    assert apply(DATA, [f])["City"].to_list() == ["Berlin", None, "Berlin"]


def test_a_project_file_with_malformed_filters_opens_and_refreshes(window, settle, session, demo):
    data = json.loads(demo.read_text(encoding="utf-8"))
    orders = next(table for table in data["tables"] if table["name"] == "Orders")
    orders["filters"] = [{"column": "City", "values": 7}, {"column": "City", "options": "x"}, {"column": "City", "values": [["x"]]}]
    demo.write_text(json.dumps(data), encoding="utf-8")
    panel, table = open_orders(window, settle, session, demo, 8)
    panel.filter_button.setChecked(True)
    panel.refresh()
    assert len(panel.filter_pane.cards) == 3 and "filtered" not in panel.info.text()
    assert panel.filter_button.text() == "Filters"  # none of them leaves rows out
    panel.filter_button.setChecked(False)


def test_a_value_that_a_column_can_not_hold_is_left_out():
    numbers = pl.DataFrame({"Amount": pl.Series([D("1.50"), D("2.50")], dtype=pl.Decimal(10, 2)), "Stamp": [dt.datetime(2025, 1, 1)] * 2})
    wrong = ["Infinity", "-Infinity", "NaN", "sNaN", 10**400, "1e20", "1e-400", 5e-324, "1.5"]
    assert from_json(wrong, pl.Decimal(10, 2)) == [D("1.5")]
    assert apply(numbers, [Filter("Amount", wrong)])["Amount"].to_list() == [D("1.50")]
    assert apply(numbers, [Filter("Stamp", ["2025-01-01T00:00:00+01:00"])]).height == 0  # with a time zone
    assert apply(numbers, [Filter("Stamp", ["2025-01-01T00:00:00+01:00"], excluded=True)]).height == 2


@pytest.mark.parametrize("dtype", [pl.Decimal(38, 0), pl.Decimal(38, 2), pl.Decimal(10, 2), pl.Decimal(18, 3), pl.Decimal(38, 38), pl.Decimal(5, 5)])
@pytest.mark.parametrize("value", ["1e-400", "1e-40", "0." + "0" * 60 + "1", "0." + "1" * 45, 5e-324, 1e-300, 1.2345678901234567e-30])
def test_a_value_with_too_many_decimal_places_is_left_out(dtype, value):
    """Before, some of them stopped each refresh with a panic of Polars, which no except sees."""
    numbers = pl.DataFrame({"Amount": pl.Series([D("0"), None], dtype=dtype)})
    assert apply(numbers, [Filter("Amount", [value])]).height == 0
    assert apply(numbers, [Filter("Amount", [value], excluded=True)]).height == 2
    assert apply(numbers, [Filter("Amount", [value, None])]).height == 1
    assert isinstance(formula(Filter("Amount", [value]), dtype), str)


def test_a_decimal_value_is_compared_with_all_its_digits():
    """Polars would round it to the decimal places of the column: 1.555 is not 1.56."""
    numbers = pl.DataFrame({"Amount": pl.Series([D("1.55"), D("1.56")], dtype=pl.Decimal(10, 2))})
    assert apply(numbers, [Filter("Amount", ["1.554"])]).height == 0
    assert apply(numbers, [Filter("Amount", ["1.555"])]).height == 0
    assert apply(numbers, [Filter("Amount", ["1.555"], excluded=True)]).height == 2
    for same in ("1.56", "1.560", "1.56" + "0" * 45, 1.56, "156e-2"):
        assert apply(numbers, [Filter("Amount", [same])])["Amount"].to_list() == [D("1.56")]


@pytest.mark.parametrize(
    "dtype, cells",
    [
        (pl.Decimal(10, 2), ["1.50", "-2.25", "0"]),
        (pl.Decimal(38, 38), ["0.5", "0.00000000000000000000000000000000000001", "0"]),
        (pl.Decimal(38, 0), ["1", "9" * 38, "0"]),
        (pl.Decimal(38, 2), ["9" * 36 + ".99", "0.01", "0"]),
    ],
)
def test_the_values_of_a_decimal_column_find_their_rows(act, dtype, cells):
    numbers = pl.DataFrame({"Amount": pl.Series([D(c) for c in cells], dtype=dtype)})
    for cell in numbers["Amount"]:
        chosen = Filter("Amount", [to_json(cell)])
        assert apply(numbers, [chosen])["Amount"].to_list() == [cell], chosen
        assert apply(numbers, [Filter("Amount", chosen.values, excluded=True)]).height == 2
        made = act("rows.filter_formula", numbers, {"condition": formula(chosen, dtype), "keep": "keep"})
        assert made["Amount"].to_list() == [cell], chosen  # the step made from the filter: the same rows


@pytest.mark.parametrize("dtype", [pl.Float64, pl.Float32])
def test_the_values_of_a_float_column_find_their_rows_as_a_step(act, dtype):
    """DuckDB reads 0.9931027217047139 as an exact decimal, and its cast to a float can miss the
    value by one unit: the step made from the filter kept no row."""
    steps = [0.9931027217047139, 1 / 3, 2 / 3, -2.5e-300, 1e300, 123456.78901234567, 0.1, -7.0, 2**60 + 0.0]
    values = steps + [i / 997 + 1 / 7 for i in range(300)]
    numbers = pl.DataFrame({"x": pl.Series(values, dtype=dtype)})
    for cell in numbers["x"].unique():
        chosen = Filter("x", [to_json(cell)])
        expected = apply(numbers, [chosen]).height
        made = act("rows.filter_formula", numbers, {"condition": formula(chosen, dtype), "keep": "keep"})
        assert made.height == expected > 0, (cell, formula(chosen, dtype))
    typed = Filter("x", [0.1])  # a value of a project file: Polars makes it a value of the column's type
    made = act("rows.filter_formula", numbers, {"condition": formula(typed, dtype), "keep": "keep"})
    assert made.height == apply(numbers, [typed]).height == 1


def test_a_number_range_on_a_wide_decimal_column_works_as_a_step(act):
    """A threshold written as an exact decimal made DuckDB cast the column to one with 10 decimal
    places, and the step stopped."""
    big = D("-8435767307011802148496166840000000")
    numbers = pl.DataFrame({"Amount": pl.Series([big, D(5), D(2_000_000)], dtype=pl.Decimal(38, 0))})
    for picked in ([0], [1], [2], [0, 2]):
        chosen = Filter("Amount", picked, "ranges", {"thresholds": [-557641.5134930343, 1000.5]})
        made = act("rows.filter_formula", numbers, {"condition": formula(chosen, pl.Decimal(38, 0)), "keep": "keep"})
        assert made["Amount"].to_list() == apply(numbers, [chosen])["Amount"].to_list(), picked


@pytest.mark.parametrize("inclusive", [True, False])
def test_number_ranges_as_a_step_keep_the_rows_of_the_filter(act, inclusive):
    """A threshold is a Float32 value for a Float32 column, as Polars makes it; an exact decimal with
    more digits than a float holds is compared as a correctly rounded float, as Polars does."""
    near = D("0.0055287354824883609699130743822903801")  # DuckDB's own cast misses it by one unit
    cases = [
        (pl.Series("x", [0.7, 19.99, 5.0, 0.69], dtype=pl.Float32), [0.7, 19.99]),
        (pl.Series("x", [near, D("0.1"), D(0)], dtype=pl.Decimal(38, 37)), [float(near)]),
    ]
    for column, limits in cases:
        numbers = column.to_frame()
        for picked in range(len(limits) + 1):
            chosen = Filter("x", [picked], "ranges", {"thresholds": limits, "lower_inclusive": inclusive})
            made = act("rows.filter_formula", numbers, {"condition": formula(chosen, column.dtype), "keep": "keep"})
            assert made["x"].to_list() == apply(numbers, [chosen])["x"].to_list(), (column.dtype, picked)


def test_a_small_exact_decimal_finds_its_row_as_a_step(act):
    """Its text had an exponent (1.17E-18), which DuckDB reads as a float."""
    cells = [D("-0.0000000000000000011767350239096252393"), D("0.5"), D("0")]
    numbers = pl.DataFrame({"x": pl.Series(cells, dtype=pl.Decimal(38, 37))})
    for cell in numbers["x"]:
        chosen = Filter("x", [to_json(cell)])
        made = act("rows.filter_formula", numbers, {"condition": formula(chosen, pl.Decimal(38, 37)), "keep": "keep"})
        assert made["x"].to_list() == [cell], formula(chosen, pl.Decimal(38, 37))


def test_a_filter_without_values_keeps_all_rows_also_as_a_step(act):
    """The window offers "Add as step" only for a filter with values, but its formula keeps the same
    rows in any case (it kept no row)."""
    numbers = pl.DataFrame({"x": [1.5, None, 3.0]})
    for chosen in (Filter("x", []), Filter("x", [], excluded=True), Filter("x", [], "ranges", {"thresholds": [2]})):
        assert apply(numbers, [chosen]).height == 3
        made = act("rows.filter_formula", numbers, {"condition": formula(chosen, pl.Float64), "keep": "keep"})
        assert made.height == 3, chosen


@pytest.mark.parametrize("value", ["1.50", "1e-400"])
def test_a_project_file_with_a_decimal_filter_opens_and_refreshes(window, settle, session, registry, tmp_path, value):
    data = tmp_path / "exact.parquet"
    pl.DataFrame({"Exact": pl.Series([D("1.50"), D("2.50")], dtype=pl.Decimal(18, 2))}).write_parquet(data)
    project = Project(name="Exact")
    table = project.add_table("T")
    project.add_step(table, registry.actions["parquet.import"], {"file": str(data)})
    table.filters = [{"column": "Exact", "values": [value]}]
    project.save(tmp_path / "exact.tafl")
    assert window.open_path(tmp_path / "exact.tafl")
    settle(session)
    panel = window.data_panel
    panel.filter_button.setChecked(True)
    panel.refresh()
    assert panel.model.frame["Exact"].to_list() == ([D("1.50")] if value == "1.50" else [])
    panel.filter_button.setChecked(False)


def test_a_column_of_lists_or_records_is_not_filtered(session, qtbot):
    """Before, each refresh stopped: a cell of such a column can't be looked up."""
    df = pl.DataFrame({"List": [[1], [2], None], "Record": [{"a": 1}, {"a": 2}, None], "Nr": [1, 2, 3]})
    table = session.project.add_table("T")
    for column in ("List", "Record"):
        session.set_filters(table.id, [{"column": column, "values": [[1]]}])
        pane = FilterPane(session)
        qtbot.addWidget(pane)
        pane.show_data(table.id, df)
        (card,) = pane.cards
        assert card.status.text() == "Not used at this step: a column of lists or records can't be filtered."
        assert unusable(Filter(column), df.schema) and apply(df, [Filter(column, [[1]])]) is df


class Note:
    """A value of a plugin's own type: only plugins make Object columns."""

    def __init__(self, text):
        self.text = text

    def __str__(self):
        return self.text


def test_the_menus_offer_no_filter_for_a_column_of_a_plugins_own_values(session, qtbot, monkeypatch):
    """Its values can't be compared, and a filter that holds one can't be saved: before, the
    filter of a cell kept no row, or stopped the refresh with an error."""
    shown = []

    class Menu(QMenu):
        def exec(self, *args):  # instead of showing it
            shown.append([action.text() for action in self.actions() if not action.isSeparator()])

    monkeypatch.setattr(data_panel, "QMenu", Menu)
    df = pl.DataFrame([pl.Series("Own", [Note("a\tb"), None], dtype=pl.Object), pl.Series("City", ["x", None])])
    panel = DataPanel(session)
    qtbot.addWidget(panel)
    panel.model.set_frame(df)
    panel.stack.setCurrentWidget(panel.view)
    panel.show()
    QApplication.processEvents()
    header = panel.view.horizontalHeader()
    for column in (0, 1):
        panel._cell_menu(panel.view.visualRect(panel.model.index(0, column)).center())
        panel._header_menu(QPoint(header.sectionViewportPosition(column) + 5, 5))
    assert shown == [["Copy"], ["Copy", "Show only rows where City is “x”", "Filter by City"], ["Filter by City"]]
    panel.filter_pane.show_data(session.project.add_table("T").id, df)
    panel.filter_pane._fill_add_menu()
    assert [action.text() for action in panel.filter_pane.add_menu.actions()] == ["City"]


@pytest.mark.parametrize("values", [[Note("a"), Note("b"), None], [{"a"}, {"b"}, None]], ids=["values", "unhashable"])
def test_a_filter_is_not_used_where_its_column_holds_a_plugins_own_values(session, qtbot, values):
    """E.g. a filter made at a step where the column holds texts. Before, it kept no row there, or
    each refresh stopped: Polars can't look up such values."""
    df = pl.DataFrame([pl.Series("Own", values, dtype=pl.Object), pl.Series("Nr", [1, 2, 3])])
    table = session.project.add_table("T")
    session.set_filters(table.id, [{"column": "Own", "values": ["a"]}])
    pane = FilterPane(session)
    qtbot.addWidget(pane)
    pane.show_data(table.id, df)
    (card,) = pane.cards
    assert card.status.text() == "Not used at this step: a column of a plugin's own values can't be filtered."
    assert apply(df, [Filter("Own", ["a"])]) is df


def saved_project_of(window, settle, session, registry, tmp_path, df):
    """A project that reads ``df``, open in the window, and the data panel."""
    data = tmp_path / "cells.parquet"
    df.write_parquet(data)
    project = Project(name="Cells")
    project.add_step(project.add_table("T"), registry.actions["parquet.import"], {"file": str(data)})
    assert window.open_path(project.save(tmp_path / "cells.tafl"))
    settle(session)
    return window.data_panel


def test_a_filter_on_binary_data_is_saved_and_can_be_made_a_step(window, settle, session, registry, tmp_path):
    """Before, the filter stopped Save with an error, and its step kept no rows."""
    df = pl.DataFrame({"Code": [b"a", b"\x00\xff", None, b"a"], "Nr": [1, 2, 3, 4]})
    panel = saved_project_of(window, settle, session, registry, tmp_path, df)
    panel.filter_by("Code", [to_json(b"a")])  # what the cell's right-click menu does
    assert panel.model.frame["Nr"].to_list() == [1, 4]
    assert window.save_project()
    saved = json.loads(session.project.path.read_text(encoding="utf-8"))
    assert saved["tables"][0]["filters"] == [{"column": "Code", "values": ["0x61"]}]

    assert window.open_path(session.project.path)
    settle(session)
    panel.filter_button.setChecked(True)
    panel.refresh()
    assert panel.model.frame["Nr"].to_list() == [1, 4]
    (card,) = panel.filter_pane.cards
    assert card.pending() == ["0x61"]
    panel.filter_pane.add_as_step(0)
    settle(session)
    assert session.step.params["condition"] == "[Code] IN (from_hex('61'))"
    assert panel.model.frame["Nr"].to_list() == [1, 4]
    panel.filter_button.setChecked(False)


@pytest.mark.parametrize(
    "cells, expected",
    [
        ([[dt.date(2024, 1, 1)], [dt.date(2024, 1, 2)]], ["2024-01-01"]),
        ([{"Day": dt.date(2024, 1, 1)}, {"Day": dt.date(2024, 1, 2)}], {"Day": "2024-01-01"}),
    ],
    ids=["list", "record"],
)
def test_a_filter_on_a_cell_of_dates_in_a_list_or_record_is_saved(window, settle, session, registry, tmp_path, cells, expected):
    panel = saved_project_of(window, settle, session, registry, tmp_path, pl.DataFrame({"Dates": cells, "Nr": [1, 2]}))
    panel.filter_by("Dates", [to_json(panel.model.value(0, 0))])  # what the cell's right-click menu does
    assert window.save_project()
    saved = json.loads(session.project.path.read_text(encoding="utf-8"))
    assert saved["tables"][0]["filters"] == [{"column": "Dates", "values": [expected]}]
    assert window.open_path(session.project.path)
    settle(session)
    assert panel.model.frame["Nr"].to_list() == [1, 2]  # such a column can't be filtered
    panel.filter_button.setChecked(False)


def test_closing_keeps_the_window_when_saving_fails(window, session, monkeypatch, tmp_path):
    """Before, the program ended with an error, and the changes were lost."""
    warnings = []
    monkeypatch.setattr(QMessageBox, "exec", lambda box: warnings.append(box.text()))
    monkeypatch.setattr(window, "ask_save", lambda reason: "save")
    path = session.project.save(tmp_path / "kept.tafl")
    session.set_filters(session.project.add_table("T").id, [{"column": "City", "values": [object()]}])
    assert session.dirty
    assert not window.close()
    assert session.dirty and len(warnings) == 1
    assert warnings[0].startswith(f"Could not save {path}:\n\nThe project holds a value that can't be stored in its file")


@pytest.mark.parametrize("value", [math.inf, -math.inf, math.nan])
def test_closing_with_save_keeps_a_filter_when_a_stored_map_position_is_not_a_finite_number(
    window, settle, session, registry, monkeypatch, tmp_path, value
):
    """Before, the program ended with an error, and the filter was lost."""
    data = tmp_path / "cells.parquet"
    pl.DataFrame({"City": ["a", "b", None]}).write_parquet(data)
    project = Project(name="Cells")
    table = project.add_table("T")
    project.add_step(table, registry.actions["parquet.import"], {"file": str(data)})
    table.group = project.add_group("G").id
    path = project.save(tmp_path / "cells.tafl")
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["tables"][0]["position"] = [value, 0]
    stored["groups"][0]["position"] = [value, 0]
    path.write_text(json.dumps(stored), encoding="utf-8")
    assert window.open_path(path)
    settle(session)
    monkeypatch.setattr(window, "ask_save", lambda reason: "save")
    panel = window.data_panel
    panel.filter_by("City", ["a"])
    settle(session)
    assert session.dirty and panel.model.frame["City"].to_list() == ["a"]
    assert window.close()
    assert not session.dirty
    saved = Project.load(path)
    assert saved.tables[0].filters == [{"column": "City", "values": ["a"]}]
    assert saved.tables[0].position is None and saved.groups[0].position is None
    panel.filter_button.setChecked(False)


@pytest.fixture
def few_cards(monkeypatch):
    """The cards of very many filters would take gigabytes of widgets: a card for each ends the test."""
    built = []
    create = filter_pane.FilterCard

    def counted(*args):
        built.append(args)
        assert len(built) < 10 * MAX_CARDS, "a card for every filter"
        return create(*args)

    monkeypatch.setattr(filter_pane, "FilterCard", counted)


def test_a_project_with_very_many_filters_opens_unchanged(window, session, registry, tmp_path, few_cards, protected_view):
    """A project file written by hand can hold any number of filters, and the filter pane shows
    the filters of a table as soon as the project opens, here in protected view."""
    filters = [{"column": f"c{number}", "values": ["a"]} for number in range(12_000)]
    project = Project(name="Many filters")
    table = project.add_table("A")
    project.add_step(table, registry.actions["csv.import"], {"file": "a.csv"})
    table.filters = filters
    path = project.save(tmp_path / "many.tafl")
    started = time.perf_counter()
    assert window.open_path(path)
    assert time.perf_counter() - started < 10
    assert session.protected
    pane = window.data_panel.filter_pane
    assert [(card.index, card.column) for card in pane.cards] == [(number, f"c{number}") for number in range(MAX_CARDS)]
    assert len(pane.findChildren(QWidget)) < 40 * MAX_CARDS
    assert pane.too_many.text().startswith("11,900 more filters are on this table: too many to show.\n")
    assert pane.too_many.text().endswith("remove 11,901 to add another.")
    assert not pane.too_many.isHidden() and not pane.add_button.isEnabled()
    assert session.project.tables[0].filters == filters and not session.dirty


def test_a_filter_past_the_limit_brings_the_line_into_view(window, settle, session, demo, qapp):
    """A menu of the grid has no other way to say why nothing happens. The pane has no bar to
    scroll to the side: the line and the cards stay where they are across."""
    panel, orders = open_orders(window, settle, session, demo, 8)
    window.show()
    session.set_filters(orders.id, [{"column": "Status", "values": []}] * MAX_CARDS)
    panel.filter_button.setChecked(True)
    qapp.processEvents()
    pane = panel.filter_pane
    pane.container.setMinimumWidth(pane.scroll.viewport().width() + 200)  # as with a big font
    qapp.processEvents()
    viewport = pane.scroll.viewport()
    in_view = lambda: viewport.rect().contains(pane.too_many.mapTo(viewport, pane.too_many.rect().center()))  # noqa: E731
    assert not in_view()
    panel.filter_by("City")
    qapp.processEvents()
    assert in_view() and pane.scroll.horizontalScrollBar().value() == 0


def test_filters_without_a_card_still_apply_and_are_kept(window, settle, session, demo, few_cards):
    panel, orders = open_orders(window, settle, session, demo, 8)
    pane = panel.filter_pane
    panel.filter_by("City")
    hamburg = listed(pane.cards[0])["Hamburg"]
    filters = [{"column": "Status", "values": []}] * 11_999 + [{"column": "City", "values": ["Hamburg"]}]
    started = time.perf_counter()
    session.set_filters(orders.id, filters)
    assert time.perf_counter() - started < 60  # each of the cards would take seconds
    assert len(pane.cards) == MAX_CARDS and panel.filter_button.text() == "Filters (1)"
    assert panel.model.frame.height == hamburg and set(panel.model.frame["City"].to_list()) == {"Hamburg"}
    assert sum(listed(pane.cards[3]).values()) == hamburg  # a card counts the rows of the filters without one
    card = pane.cards[3]
    card.apply_only(card.model.index(0, 0))  # a card changes its own filter and no other
    stored = session.filters(orders.id)
    assert stored[3]["values"] and stored[:3] + stored[4:] == filters[:3] + filters[4:]
    assert session.project.to_dict()["tables"][1]["filters"] == stored


def test_a_filter_cannot_be_added_at_the_limit_of_the_cards(window, settle, session, demo):
    panel, orders = open_orders(window, settle, session, demo, 8)
    pane = panel.filter_pane
    session.set_filters(orders.id, [{"column": "Status", "values": []}] * (MAX_CARDS - 1))
    assert len(pane.cards) == MAX_CARDS - 1 and pane.add_button.isEnabled() and pane.too_many.isHidden()
    panel.filter_by("City")  # what the menus of the grid do
    most = f"A table shows {MAX_CARDS} filters at most: remove one to add another."
    assert len(pane.cards) == MAX_CARDS and not pane.add_button.isEnabled()
    assert not pane.too_many.isHidden() and pane.too_many.text() == most and pane.add_button.toolTip() == tooltip_text(most)
    full = session.filters(orders.id)
    panel.filter_by("Size")
    panel.filter_by("Size", ["large"])
    pane.add_filter("City", again=True)  # what the menu of the button does
    assert session.filters(orders.id) == full
    panel.filter_by("City", ["Hamburg"])  # a filter that is there gets the values
    assert session.filters(orders.id)[-1] == {"column": "City", "values": ["Hamburg"]}
    assert set(panel.model.frame["City"].to_list()) == {"Hamburg"}
    session.set_filters(orders.id, [*full, full[0]])
    most = f"A table shows {MAX_CARDS} filters at most: remove 2 to add another."
    assert pane.too_many.text() == f"1 more filter is on this table: too many to show.\n{most}"
    assert not pane.too_many.isHidden() and not pane.add_button.isEnabled()
    pane.remove(0)
    pane.remove(0)
    assert len(pane.cards) == MAX_CARDS - 1 and pane.add_button.isEnabled() and pane.too_many.isHidden()
    assert "right-click" in pane.add_button.toolTip()


def test_a_filter_past_the_cards_takes_no_values_from_the_grid(window, settle, session, demo):
    """It has no card that would show or clear them."""
    panel, orders = open_orders(window, settle, session, demo, 8)
    stored = [*[{"column": "Status", "values": []}] * MAX_CARDS, {"column": "City", "values": []}]
    session.set_filters(orders.id, stored)
    panel.filter_by("City", ["Hamburg"])
    assert session.filters(orders.id) == stored


def test_a_filter_without_a_card_leaves_the_values_of_a_card_in_its_list(window, settle, session, demo):
    """It takes rows, not values: a value with no rows left stays with the count 0, so that
    the card shows what is selected."""
    panel, orders = open_orders(window, settle, session, demo, 8)
    pane = panel.filter_pane
    first, other = sorted(set(panel.model.frame["CustomerNo"].to_list()))[:2]
    empty = [{"column": "Status", "values": []}] * (MAX_CARDS - 1)
    session.set_filters(orders.id, [{"column": "CustomerNo", "values": [first]}, *empty, {"column": "CustomerNo", "values": [other]}])
    card = pane.cards[0]
    assert card.status.text().endswith("· 1 selected")
    assert [value for value in card.model.applied()] == [first]
    assert panel.model.frame.height == 0


def same_rows(act, df: pl.DataFrame, chosen: Filter) -> list[int]:
    """The rows of the card and of the step that Add as step makes of it: they must be the same."""
    card = apply(df, [chosen])["id"].to_list()
    condition = formula(chosen, df.schema[chosen.column], df.get_column(chosen.column))
    step = act("rows.filter_formula", df, {"condition": condition, "keep": "keep"})["id"].to_list()
    assert step == card, (chosen, condition)
    return card


def zoned(zone: str) -> pl.DataFrame:
    """Moments around the ends of months and years, in the time zone ``zone``."""
    moments = [dt.datetime(2025, 1, 31, 23, 30), dt.datetime(2025, 1, 15, 12), dt.datetime(2025, 3, 31, 22, 30), dt.datetime(2024, 12, 31, 23, 59), dt.datetime(2025, 2, 1, 4, 30), None]  # fmt: skip
    when = pl.Series("When", moments).dt.replace_time_zone("UTC").dt.convert_time_zone(zone)
    return pl.DataFrame({"id": range(len(moments)), "When": when})


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Europe/Berlin", "Asia/Kolkata"])
def test_time_periods_of_a_column_with_a_time_zone_as_a_step(act, zone):
    """The card groups by the local time of the column's zone, as the grid shows it; the step
    took the time in this PC's zone: 2025-01-31 23:30 UTC was February in Berlin."""
    df = zoned(zone)
    local = df["When"].dt.strftime("%Y-%m").to_list()
    for by in PERIODS:
        periods, _ = facet(df, [Filter("When", [], "period", {"by": by})], 0)
        for period in periods["value"].to_list():
            rows = same_rows(act, df, Filter("When", [period], "period", {"by": by}))
            same_rows(act, df, Filter("When", [period], "period", {"by": by}, excluded=True))
            if by == "month" and period is not None:
                assert rows == [row for row, text in enumerate(local) if text == period], (zone, period)


@pytest.mark.parametrize(
    "zone, moments",
    [
        ("America/Vancouver", [dt.datetime(2026, 12, 1, 7, 30), dt.datetime(2026, 12, 1, 6, 30), dt.datetime(2026, 11, 1, 7)]),
        ("Africa/Casablanca", [dt.datetime(2026, 9, 19, 23, 30), dt.datetime(2026, 9, 20, 0, 30), dt.datetime(2026, 12, 31, 23, 30)]),
        ("America/Havana", [dt.datetime(2025, 3, 9, 4, 59, 59), dt.datetime(2025, 3, 9, 5), dt.datetime(2025, 11, 2, 4, 30)]),  # no 00:00
        ("Pacific/Apia", [dt.datetime(2011, 12, 30, 9, 59, 59), dt.datetime(2011, 12, 30, 10), dt.datetime(2011, 12, 31, 12)]),  # no 30 Dec
        ("Europe/Berlin", [dt.datetime(1893, 3, 31, 23, 6, 31), dt.datetime(1893, 3, 31, 23, 6, 32)]),  # the day began at 00:06:32
    ],
)
def test_a_time_period_of_a_zone_whose_rules_duckdb_has_otherwise_as_a_step(act, zone, moments):
    """DuckDB's rules of a zone can differ from Polars' (America/Vancouver from 2026-11-01): the step
    took DuckDB's local time, so 2026-11-30 23:30 there was December. A step now keeps each period
    from its first moment to the first of the next, by Polars' rules, as the card does, also where a
    clock change skips midnight."""
    for unit in ("us", "ns"):
        when = pl.Series("When", [*moments, None], dtype=pl.Datetime(unit)).dt.replace_time_zone("UTC").dt.convert_time_zone(zone)
        df = pl.DataFrame({"id": range(len(moments) + 1), "When": when})
        for by in PERIODS:
            periods, _ = facet(df, [Filter("When", [], "period", {"by": by})], 0)
            for period in periods["value"].to_list():
                same_rows(act, df, Filter("When", [period], "period", {"by": by}))
                same_rows(act, df, Filter("When", [period], "period", {"by": by}, excluded=True))
    vancouver = pl.DataFrame({"id": [0], "When": pl.Series("When", moments[:1]).dt.replace_time_zone("UTC").dt.convert_time_zone(zone)})
    assert zone != "America/Vancouver" or same_rows(act, vancouver, Filter("When", ["2026-11"], "period", {"by": "month"})) == [0]


@pytest.mark.parametrize("zone", ["UTC", "America/New_York", "Europe/Berlin"])
def test_a_value_of_a_column_with_a_time_zone_as_a_step(act, zone):
    """The step's TIMESTAMP dropped the offset: it kept no row, or (in the PC's zone) the other row
    of a time that the clock change repeats."""
    df = zoned(zone)
    for stored in stored_values(df["When"]):
        assert len(same_rows(act, df, Filter("When", [stored]))) == 1
        same_rows(act, df, Filter("When", [stored], excluded=True))
    repeated = pl.Series("When", [dt.datetime(2025, 10, 26, 0, 30), dt.datetime(2025, 10, 26, 1, 30)])  # 02:30 twice in Berlin
    twice = pl.DataFrame({"id": [0, 1], "When": repeated.dt.replace_time_zone("UTC").dt.convert_time_zone("Europe/Berlin")})
    assert [same_rows(act, twice, Filter("When", [stored])) for stored in stored_values(twice["When"])] == [[0], [1]]
    assert same_rows(act, df, Filter("When", ["2025-01-31T23:30:00"])) == [0]  # without an offset: in UTC, as Polars reads it
    naive = pl.DataFrame({"id": [0], "When": [dt.datetime(2025, 1, 31, 23, 30)]})
    assert same_rows(act, naive, Filter("When", ["2025-01-31T23:30:00+00:00"])) == []  # an offset doesn't fit
    assert formula(Filter("When", ["2025-01-31T23:30:00+00:00"]), pl.Datetime) == "FALSE"


def test_a_value_of_a_zone_whose_rules_python_has_otherwise_keeps_its_moment(act):
    """Polars and Python's tzdata can have other rules for a zone (America/Vancouver from
    2026-11-01, Africa/Casablanca, Europe/Chisinau): a value was written with Python's offset for
    Polars' local time, so it named another moment, and the card and the step kept the row of that
    one. Where the rules agree, the text is the same as before: filters of 0.6.3 keep their rows."""
    moments = [dt.datetime(2026, 12, 1, 7, 30), dt.datetime(2026, 12, 1, 6, 30), dt.datetime(2026, 7, 1, 7, 30), None]
    vancouver = pl.Series("When", moments).dt.replace_time_zone("UTC").dt.convert_time_zone("America/Vancouver")
    df = pl.DataFrame({"id": range(4), "When": vancouver})
    for row, stored in enumerate(stored_values(df["When"])[:3]):
        assert same_rows(act, df, Filter("When", [stored])) == [row], stored
    stamps = [*moments, dt.datetime(1890, 6, 1, 12), dt.datetime(2025, 10, 26, 0, 30), dt.datetime(2025, 7, 1, 12, 0, 0, 500)]
    for zone in ("Europe/Berlin", "Asia/Kolkata", "Australia/Lord_Howe"):  # rules that agree: 1890 is local mean time
        same = pl.Series("When", stamps).dt.replace_time_zone("UTC").dt.convert_time_zone(zone)
        assert stored_values(same) == [None if value is None else value.isoformat() for value in same.to_list()]


def test_values_with_nanoseconds_are_stored_with_them(act):
    """08:30:00 and 08:30:00.000000001 were two values, but both were stored as 08:30:00: the
    card and the step kept the row of the first one. A filter that 0.6.3 stored keeps its rows."""
    stamps = pl.Series("When", [1_735_720_200 * 10**9, 1_735_720_200 * 10**9 + 1, -1, None], dtype=pl.Int64).cast(pl.Datetime("ns"))
    times = pl.Series("T", [8 * 3600 * 10**9, 8 * 3600 * 10**9 + 1, 1, None], dtype=pl.Int64).cast(pl.Time)
    df = pl.DataFrame({"id": [0, 1, 2, 3], "When": stamps, "T": times})
    assert stored_values(df["When"]) == ["2025-01-01T08:30:00", "2025-01-01T08:30:00.000000001", "1969-12-31T23:59:59.999999999", None]
    assert stored_values(df["T"]) == ["08:00:00", "08:00:00.000000001", "00:00:00.000000001", None]
    for column in ("When", "T"):
        for row, stored in enumerate(stored_values(df[column])):
            assert same_rows(act, df, Filter(column, [stored])) == [row]
    old = {"column": "When", "values": ["2025-01-01T08:30:00"]}  # as 0.6.3 stored either of the two
    assert same_rows(act, df, Filter.from_dict(old)) == [0]
    assert from_json(["2025-01-01T08:30:00.000000001"], pl.Datetime("us")) == []  # a value the column can't hold


def test_time_spans_are_stored_with_all_their_digits(act):
    """1 µs made INTERVAL '1e-06 seconds', which stopped the step; 1 ns and 2 ns were both 0.0 (the
    card kept no row, the step both); 300 years and 1 µs lost the µs. A step has microseconds only:
    for time spans with nanoseconds, Add as step says that it can't."""
    micro = pl.DataFrame({"id": [0, 1, 2, 3], "S": pl.Series([1, 2, 9_467_280_000_000_001, -1], dtype=pl.Int64).cast(pl.Duration("us"))})
    assert stored_values(micro["S"]) == [1e-06, 2e-06, "P109575DT0.000001S", -1e-06]  # seconds as before, where they keep all digits
    for row, stored in enumerate(stored_values(micro["S"])):
        assert same_rows(act, micro, Filter("S", [stored])) == [row]
    assert to_json(dt.timedelta(hours=1)) == 3600.0 and to_json(dt.timedelta(microseconds=1)) == 1e-06
    assert same_rows(act, micro, Filter("S", [2e-06, "P109575DT0.000001S"], excluded=True)) == [0, 3]
    nano = pl.DataFrame({"id": [0, 1, 2], "S": pl.Series([1, 2, 1000], dtype=pl.Int64).cast(pl.Duration("ns"))})
    assert stored_values(nano["S"]) == ["PT0.000000001S", "PT0.000000002S", 1e-06]
    assert [apply(nano, [Filter("S", [stored])])["id"].to_list() for stored in stored_values(nano["S"])] == [[0], [1], [2]]
    with pytest.raises(NotAStep, match=r"a step compares time spans only to the microsecond, and a picked value has nanoseconds"):
        formula(Filter("S", ["PT0.000000002S"]), pl.Duration("ns"))
    with pytest.raises(NotAStep, match=r"and some in \[S\] have nanoseconds"):  # 1 µs: but 1 ns and 2 ns are 0 µs for the step
        formula(Filter("S", [1e-06]), pl.Duration("ns"), nano["S"])
    whole = pl.DataFrame({"id": [0, 1], "S": pl.Series([1000, 2000], dtype=pl.Int64).cast(pl.Duration("ns"))})
    assert same_rows(act, whole, Filter("S", [1e-06])) == [0]
    assert same_rows(act, nano, Filter("S", [None])) == []  # empty cells: the step can say that


def test_a_time_span_stored_as_the_text_of_a_number_is_seconds(act):
    """A filter made where the column held texts ("3600"), on a column of time spans after a step:
    the text is seconds, as 0.6.3 read it."""
    spans = pl.DataFrame({"id": [0, 1], "S": pl.Series([3_600_000_000, 1], dtype=pl.Int64).cast(pl.Duration("us"))})
    for stored in ("3600", " 3600 ", "3600.0", "3.6e3"):
        assert same_rows(act, spans, Filter("S", [stored])) == [0], stored
    for stored in ("1:00:00", "an hour", "nan", "inf"):
        assert same_rows(act, spans, Filter("S", [stored])) == [], stored
    assert same_rows(act, spans, Filter("S", ["0.000001", "PT1H"])) == [0, 1]


def test_date_times_with_a_time_zone_and_nanoseconds_as_a_step(act):
    """DuckDB has date-times with a time zone only to the microsecond."""
    df = pl.DataFrame({"id": [0, 1], "When": pl.Series([1_735_720_200 * 10**9, 1_735_720_200 * 10**9 + 1]).cast(pl.Datetime("ns")).dt.replace_time_zone("UTC")})
    with pytest.raises(NotAStep, match="a step compares date-times with a time zone only to the microsecond"):
        formula(Filter("When", stored_values(df["When"])[1:]), df.schema["When"])
    with pytest.raises(NotAStep, match=r"some in \[When\] have nanoseconds"):
        formula(Filter("When", stored_values(df["When"])[:1]), df.schema["When"], df["When"])
    whole = df.with_columns(pl.Series("When", [1_735_720_200 * 10**9, 1_735_720_201 * 10**9]).cast(pl.Datetime("ns")).dt.replace_time_zone("UTC"))
    assert same_rows(act, whole, Filter("When", stored_values(whole["When"])[1:])) == [1]
    assert same_rows(act, df, Filter("When", ["2025-01"], "period", {"by": "month"})) == [0, 1]  # after 1970: in the same day


TEXTS = pl.DataFrame({"id": range(10), "T": ["Straße", "ä", "abc", "١٢٣", "a b", "a b", "x_y", "Ⓐ", "", None]})


@pytest.mark.parametrize(
    "pattern",
    [r"^\w+$", r"\d", r"^\d+$", r"\s", r"^\S+$", r"^\W", r"^[\w]+$", r"^[^\d]+$", r"[\d\s]", r"\D", "(?i)STRASSE", r"\p{Greek}", "^$", r"x\_y",
     r"\u{E4}", r"\u00E4", r"(?<letter>ä)", r"^(?<w>\w)+$"],  # RE2 writes these \x{E4} and (?P<letter>...)
)  # fmt: skip
def test_a_pattern_as_a_step_keeps_the_rows_of_the_card(act, pattern):
    """The card uses Polars, whose \\w, \\d and \\s are the letters, digits and spaces of Unicode; the
    step used them as RE2 (DuckDB) reads them, for ASCII only: ^\\w+$ kept Straße and ä in the card
    only, \\d the Arabic-Indic ١٢٣."""
    rows = [same_rows(act, TEXTS, Filter("T", [picked], "regex", {"pattern": pattern})) for picked in ("match", "no match")]
    expected = {r"^\w+$": [0, 1, 2, 3, 6, 7], r"\d": [3], r"\s": [4, 5]}  # Straße, ä, ١٢٣, x_y and Ⓐ are words
    assert pattern not in expected or rows[0] == expected[pattern]


@pytest.mark.parametrize("pattern, reason", [
    (r"\bb", r"a step reads \\b and \\B \(the start or end of a word\) only with the letters A to Z"),
    (r"[\S]", r"a step can't use \\S inside \[ \]"),
    (r"[a&&b]", r"a step can't use &&, -- or ~~ inside \[ \]"),
    (r"[a[b]]", r"a step can't use a class inside a class"),
    (r"(?x)a b", r"^a step knows only the flags i, m, s and U of a pattern, such as \(\?i\)$"),
    (r"(?-u)^\w+$", r"^a step knows only the flags i, m, s and U"),
    (r"a{2}{2}", r"^a step can't repeat a repetition at once, such as a\{2\}\{2\}: write \(a\{2\}\)\{2\}$"),
    (r"a**", r"^a step can't repeat a repetition at once"),
])  # fmt: skip
def test_a_pattern_that_a_step_reads_otherwise_is_no_step(pattern, reason):
    """The reasons are plain words: they said "the step can't read the pattern (invalid perl operator:
    (?x)" and "(bad repetition operator: {2}{2})"."""
    assert unusable(Filter("T", ["match"], "regex", {"pattern": pattern}), TEXTS.schema) is None  # the card works
    with pytest.raises(NotAStep, match=reason):
        formula(Filter("T", ["match"], "regex", {"pattern": pattern}), pl.String)


def test_the_classes_of_a_pattern_as_a_step_are_those_of_the_card_for_every_character(act):
    """All characters: they differ only where DuckDB's tables of Unicode don't know a character
    yet (Polars' are newer)."""
    chars = pl.DataFrame({"T": [chr(c) for c in range(0x110000) if not 0xD800 <= c <= 0xDFFF]}).with_row_index("id")
    known = act("rows.filter_formula", chars, {"condition": r"regexp_matches([T], '^[\p{L}\p{M}\p{N}\p{P}\p{S}\p{Z}\p{C}]$')"})
    unknown = set(chars["id"].to_list()) - set(known["id"].to_list())  # also all code points that have no character
    assert known.height > 250_000
    for pattern in (r"^\w$", r"^\W$", r"^\d$", r"^\D$", r"^\s$", r"^\S$", r"^[\w\s]$", r"^[^\d\s]$"):
        chosen = Filter("T", ["match"], "regex", {"pattern": pattern})
        card = set(apply(chars, [chosen])["id"].to_list())
        step = set(act("rows.filter_formula", chars, {"condition": formula(chosen, pl.String)})["id"].to_list())
        assert card ^ step <= unknown, (pattern, [hex(c) for c in sorted(card ^ step - unknown)[:10]])


def test_a_repetition_with_spaces_as_a_step(act):
    """Polars reads a{1, 2} as one or two a, DuckDB as the text "a{1, 2}": the step kept other rows."""
    texts = pl.DataFrame({"id": range(5), "T": ["a", "aa", "aaa", "a{1, 2}", "a{ 2 }"]})
    for pattern, rows in (("^a{1, 2}$", [0, 1]), ("^a{1 ,2}$", [0, 1]), ("^a{ 2 }$", [1]), ("^a{1,}$", [0, 1, 2]), ("^a{ 2 }?$", [1])):
        assert same_rows(act, texts, Filter("T", ["match"], "regex", {"pattern": pattern})) == rows, pattern
        same_rows(act, texts, Filter("T", ["no match"], "regex", {"pattern": pattern}))
    assert "'^a{1,2}$'" in formula(Filter("T", ["match"], "regex", {"pattern": "^a{1, 2}$"}), pl.String)


def test_a_step_that_would_keep_other_rows_here_is_not_made():
    """Add as step runs the step's condition on the column at the step: where its rows differ from
    the card's, for whatever reason, it says so instead of making the step. Such as a quarter of a
    moment just before 1970 with nanoseconds (the step's DuckDB takes it to the microsecond, into
    1970) and a letter that DuckDB's tables of Unicode don't know yet (U+13460, Unicode 16). Where
    the step would stop with an error on the column, such as on a date-time beyond DuckDB's years,
    it says that."""
    stamps = pl.Series("When", [-1, 5 * 10**9], dtype=pl.Int64).cast(pl.Datetime("ns"))  # 1969-12-31 23:59:59.999999999
    quarter = Filter("When", ["1969 Q4"], "period", {"by": "quarter"})
    letters = pl.Series("T", ["abc", "\U00013460", "ä"])
    word = Filter("T", ["match"], "regex", {"pattern": r"^\w+$"})
    for chosen, data in ((quarter, stamps), (word, letters)):
        assert formula(chosen, data.dtype)  # without the column at the step, as before
        with pytest.raises(NotAStep, match=r"^a step would keep other rows than the filter: 1 of the \d rows here$"):
            formula(chosen, data.dtype, data)
    assert formula(word, pl.String, letters.filter(letters != "\U00013460")).startswith("regexp_matches([T]")
    far = pl.Series("When", [10**16, 0], dtype=pl.Int64).cast(pl.Datetime("ms"))  # the year 318857, and 1970
    assert apply(pl.DataFrame({"id": [0, 1], "When": far}), [Filter("When", ["1970-01-01T00:00:00"])])["id"].to_list() == [1]
    with pytest.raises(NotAStep, match=r"^the step would stop with an error on these values$"):
        formula(Filter("When", ["1970-01-01T00:00:00"]), far.dtype, far)


def test_add_as_step_says_why_a_filter_cant_be_one(session, qtbot):
    df = pl.DataFrame({"T": ["Straße", "abc"], "S": pl.Series([1, 2], dtype=pl.Int64).cast(pl.Duration("ns"))})
    table = session.project.add_table("T")
    stored = [{"column": "T", "facet": "regex", "options": {"pattern": r"\bab"}, "values": ["match"]}, {"column": "S", "values": ["PT0.000000001S"]}]
    session.set_filters(table.id, stored)
    pane = FilterPane(session)
    qtbot.addWidget(pane)
    pane.show_data(table.id, df)
    for index, words in ((0, r"\b and \B"), (1, "time spans only to the microsecond")):
        pane.add_as_step(index)
        assert pane.cards[index].status.text().startswith("Not added as a step: a step") and words in pane.cards[index].status.text()
    assert session.filters(table.id) == stored and table.steps == []  # the filters stay


def test_a_cell_with_nanoseconds_from_the_grid_is_stored_with_them(window, settle, session, registry, tmp_path, monkeypatch):
    """Show only rows where When is “...” stored the cell without its nanoseconds."""
    actions = []

    class Menu(QMenu):
        def exec(self, *args):  # instead of showing it
            actions.extend(action for action in self.actions() if action.text().startswith("Show only rows"))

    monkeypatch.setattr(data_panel, "QMenu", Menu)
    stamps = pl.Series("When", [1_735_720_200 * 10**9, 1_735_720_200 * 10**9 + 1], dtype=pl.Int64).cast(pl.Datetime("ns"))
    panel = saved_project_of(window, settle, session, registry, tmp_path, pl.DataFrame({"When": stamps, "Nr": [1, 2]}))
    panel._cell_menu(panel.view.visualRect(panel.model.index(1, 0)).center())
    actions[-1].trigger()
    assert session.filters(session.table.id) == [{"column": "When", "values": ["2025-01-01T08:30:00.000000001"]}]
    assert panel.model.frame["Nr"].to_list() == [2]
    panel.filter_button.setChecked(False)

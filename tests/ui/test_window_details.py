"""Details of the window: forms in a narrow settings panel, names, the map's keys and group
bar, the connection test, saving over a changed file, colors and the first window size."""

import polars as pl
from PySide6.QtCore import QSettings, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox

from tafl.core import Connection, Project, Status, StepResult, TableResult
from tafl.ui import theme
from tafl.ui.connections_dialog import ConnectionsDialog
from tafl.ui.session import Session
from tafl.ui.settings_panel import SettingsPanel


def test_list_rows_fit_a_narrow_settings_panel(session, settle, demo, qtbot):
    """The Calculate rows of Aggregate were wider than the panel: ▲▼✕ were cut off."""
    session.open_project(demo)
    revenue = session.project.table("Revenue by customer")
    session.select_table(revenue.id)
    session.select_step(revenue.steps[1].id)
    settle(session)
    assert session.step.action == "calc.aggregate"
    panel = SettingsPanel(session, lambda: None)
    qtbot.addWidget(panel)
    panel.resize(260, 800)
    panel.show()
    QApplication.processEvents()
    viewport = panel.scroll.viewport()
    assert panel.form.minimumSizeHint().width() <= viewport.width()
    for widgets in panel.form.editor("calculations")._row_widgets:
        tools = widgets[-1]  # ▲▼✕
        assert tools.mapTo(viewport, tools.rect().topRight()).x() <= viewport.width()
    assert panel.scroll.horizontalScrollBarPolicy() == Qt.ScrollBarPolicy.ScrollBarAsNeeded  # narrower still: it scrolls
    panel.resize(460, 800)  # with room, a function's name is shown in full
    QApplication.processEvents()
    for row in panel.form.editor("calculations")._rows:
        combo = row["function"]._combo
        assert combo.width() >= combo._width(combo.currentText())


def test_an_empty_name_is_refused(window, session, demo, monkeypatch):
    window.open_path(demo)
    orders = session.project.table("Orders")
    group = session.add_group("Inputs", [orders.id])
    warnings = []
    monkeypatch.setattr(QMessageBox, "warning", lambda parent, title, text: warnings.append(text))
    panel = window.tables_panel
    item = next(i for i in panel._table_items() if i.text(0) == "Orders")
    item.setText(0, "   ")  # what typing over the name does
    assert item.text(0) == "Orders" and orders.name == "Orders" and warnings == ["A table needs a name"]
    item = next(i for i in panel._table_items() if i.text(0) == "Orders")
    item.setText(0, " Orders ")  # the same name: no rename, the name is shown as it is
    assert item.text(0) == "Orders" and len(warnings) == 1
    group_item = panel.tree.topLevelItem(0)
    group_item.setText(0, "")
    assert group_item.text(0) == "Inputs" and session.project.group(group.id).name == "Inputs"
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("", True))
    window.map_view.rename_table(orders.id)  # on the map
    assert orders.name == "Orders" and warnings[-1] == "A table needs a name"


def test_map_keys_work_from_the_panels_beside_it(window, settle, session, demo):
    window.resize(1500, 950)
    window.show()
    window.open_path(demo)
    window.show_map(True)
    settle(session)
    customers, orders, revenue = session.project.tables
    session.add_group("Inputs", [customers.id, orders.id])
    reports = session.add_group("Reports", [revenue.id])
    view = window.map_view
    view.rebuild()
    window.activateWindow()
    view.data.view.setFocus()  # the data grid below the map
    QTest.keyClick(view.data.view, Qt.Key.Key_2)
    center = view.canvas.mapToScene(view.canvas.viewport().rect().center())
    assert view.frames[reports.id].rect.contains(center) and not view.auto_fit
    view.steps.list.setFocus()
    QTest.keyClick(view.steps.list, Qt.Key.Key_F, Qt.KeyboardModifier.ControlModifier)
    assert view.search.hasFocus()
    view.settings.note.setFocus()  # a field keeps its digits
    view.auto_fit = True
    QTest.keyClicks(view.settings.note, "12")
    assert view.settings.note.text() == "12" and view.auto_fit
    view.settings.note.clear()


def test_the_group_bar_shows_that_there_are_more_groups(window, settle, session, demo):
    window.resize(1000, 800)
    window.show()
    window.open_path(demo)
    window.show_map(True)
    settle(session)
    revenue = session.project.tables[2]
    for number in range(1, 12):
        session.add_group(f"A group with a long name {number}")
    last = session.add_group("The last group", [revenue.id])
    view = window.map_view
    view.rebuild()
    QApplication.processEvents()
    bar = view.group_bar
    assert bar.more.isVisible() and bar.more.isEnabled() and bar.back.isVisible() and not bar.back.isEnabled()
    bar.more.click()
    assert bar.strip.horizontalScrollBar().value() > 0 and bar.back.isEnabled()
    bar.back.click()
    assert bar.strip.horizontalScrollBar().value() == 0
    view.jump_to_group(last.id)  # its chip comes into sight
    chip = bar.chips[last.id]
    strip = bar.strip.viewport()
    assert 0 <= chip.mapTo(strip, chip.rect().topRight()).x() <= strip.width()


def test_a_long_connection_error_can_be_read_and_copied(session, qtbot):
    session.project.connections = [Connection("shop", "sqlite", {"file": "shop.db"})]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    error = "('08001', '[08001] [Microsoft][ODBC Driver 17 for SQL Server]Named Pipes Provider: " + "x" * 400 + "')"
    dialog._tested((False, error))
    assert dialog.test_result.toPlainText() == error and dialog.test_result.isReadOnly()
    assert dialog.test_result.isVisible()
    dialog.list.setCurrentRow(-1)
    dialog._show(-1)
    assert dialog.test_result.isHidden()


def test_saving_over_a_file_changed_in_another_window_asks(window, session, demo, registry, monkeypatch):
    window.open_path(demo)
    other = Session(registry)  # the same project in another window
    other.open_project(demo)
    other.project.tables[0].note = "from the other window"
    other.save_project()
    asked = []
    answer = [False]
    monkeypatch.setattr(window, "confirm_overwrite", lambda path: asked.append(path) or answer[0])
    session.rename_table(session.project.tables[0].id, "Kunden")
    assert not window.save_project() and asked == [demo.resolve()]
    assert Project.load(demo).tables[0].note == "from the other window" and session.dirty
    answer[0] = True
    assert window.save_project() and len(asked) == 2
    assert Project.load(demo).tables[0].name == "Kunden" and not session.dirty
    session.rename_table(session.project.tables[0].id, "Customers")
    assert window.save_project() and len(asked) == 2  # saved here last: no question


def test_hints_and_placeholders_are_easy_to_read():
    def contrast(a: str, b: str) -> float:
        def luminance(color: str) -> float:
            channels = [int(color[i : i + 2], 16) / 255 for i in (1, 3, 5)]
            r, g, b_ = (c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels)
            return 0.2126 * r + 0.7152 * g + 0.0722 * b_

        high, low = sorted((luminance(a), luminance(b)), reverse=True)
        return (high + 0.05) / (low + 0.05)

    for background in (theme.WINDOW, theme.BASE, theme.ALTERNATE):
        assert contrast(theme.TEXT_DIM, background) >= 4.5
    assert contrast(theme.PLACEHOLDER, theme.BASE) >= 4.5
    assert contrast(theme.MAP_TEXT_DIM, theme.MAP_BOX) >= 4.5
    assert theme.palette().color(theme.palette().ColorRole.PlaceholderText).name() == theme.PLACEHOLDER


def test_the_first_window_fits_the_screen(qapp, session):
    from tafl.ui.main_window import MainWindow

    qapp.setOrganizationName("Tafl Tests")
    QSettings().remove("geometry")  # the first start
    window = MainWindow(session)
    free = (window.screen() or QApplication.primaryScreen()).availableGeometry()
    assert window.width() <= max(free.width() * 0.9, 1) and window.height() <= max(free.height() * 0.9, 1)
    assert window.width() <= 1500 and window.height() <= 900
    window.close()
    window.deleteLater()


def test_steps_after_an_import_that_skipped_without_data(session):
    """An import that skips the rest without data gives a table without columns: the steps after
    it don't know their columns (their column fields aren't marked as missing)."""
    table = session.project.add_table("Inbox")
    first = session.project.add_step(table, "csv.import", {})
    second = session.project.add_step(table, "rows.sort", {})
    session.select_table(table.id)
    message = "No files named *.csv in the inbox"
    session.results[table.id] = TableResult(
        table.id,
        [
            StepResult(first.id, Status.OK, df=pl.DataFrame(), skip_rest=message),
            StepResult(second.id, Status.SKIPPED, df=pl.DataFrame(), skip_rest=message),
        ],
    )
    assert session.input_frame(second) is None and session.input_columns(second) is None
    assert session.input_problem(second) == (
        "Step 1 skips the rest of the table (no files named *.csv in the inbox): no data goes into this step."
    )
    session.results[table.id].steps[0].df = pl.DataFrame({"A": []})  # with the columns it would have had
    assert session.input_columns(second) == ["A"]


def test_help_plugins_names_plugin_folders_that_dont_exist(window, monkeypatch):
    monkeypatch.setattr(window.session.registry, "warnings", ["The plugin folder C:\\nowhere does not exist"])
    text = window.plugins_text()
    assert "Plugin folders:" in text and text.endswith("WARNING: The plugin folder C:\\nowhere does not exist")
    assert "Skip the rest if a table is empty   [Rows, flow.skip_if_empty, v1]" in text


def test_sizes_say_1_row_and_1_column():
    from tafl.ui.session import size_text

    assert size_text(pl.DataFrame({"a": [1]})) == "1 row × 1 column"
    assert size_text(pl.DataFrame({"a": [1, 2], "b": [3, 4]})) == "2 rows × 2 columns"
    assert size_text(pl.DataFrame({"a": range(1500)})) == "1,500 rows × 1 column"

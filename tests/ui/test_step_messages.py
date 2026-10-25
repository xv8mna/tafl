"""What a step reported (its info messages) shows in the settings panel while editing, e.g. how
many rows a merge matched; the texts about the step's status come first."""

from tafl.core import Status, StepResult


def select(window, session, settle, demo, table: str, number: int):
    window.open_path(demo)
    orders = session.project.table(table)
    session.select_table(orders.id)
    session.select_step(orders.steps[number - 1].id)
    settle(session)
    return window.settings_panel


def test_an_import_shows_what_it_found(window, session, settle, demo):
    panel = select(window, session, settle, demo, "Orders", 1)
    assert not panel.info.isHidden()
    assert panel.info.text() == "Encoding utf-8, delimiter semicolon, decimal comma"
    assert panel.error.isHidden() and panel.warning.isHidden()


def test_a_merge_shows_how_many_rows_matched(window, session, settle, demo):
    panel = select(window, session, settle, demo, "Orders", 4)
    assert panel.info.text() == "152 of 156 rows matched, 4 without a match: 09999"
    session.select_step(session.table.steps[2].id)  # a step without messages
    assert panel.info.isHidden() and panel.info.text() == ""


def test_the_status_of_a_step_comes_first(window, session, settle, demo):
    panel = select(window, session, settle, demo, "Orders", 9)  # an export: not run while editing
    assert panel.info.text() == "This step runs when you run the project (F5). Nothing is written while you edit."
    session.add_step(session.registry.actions["rows.filter"])  # not complete yet
    settle(session)
    assert panel.info.text().startswith("Not complete yet: fill in Conditions")


def test_at_most_five_messages_and_the_warnings_apart(window, session, settle, demo, monkeypatch):
    panel = select(window, session, settle, demo, "Orders", 2)
    messages = [("info", f"Note {n}") for n in range(1, 8)] + [("warning", "Watch out")]
    result = StepResult(session.step.id, Status.OK, messages=messages)
    monkeypatch.setattr(session, "step_result", lambda step: result)
    panel.refresh()
    assert panel.info.text() == "Note 1\nNote 2\nNote 3\nNote 4\nNote 5\n…"
    assert panel.warning.text() == "Watch out"
    result = StepResult(session.step.id, Status.OK, messages=[("info", "the table is empty")], skip_rest="the table is empty")
    panel.refresh()  # the steps after it are skipped: said once
    assert panel.info.text() == "The steps after this one are skipped: the table is empty."
    result = StepResult(session.step.id, Status.ERROR, error="Column X is missing", messages=[("info", "Read 3 files")])
    panel.refresh()
    assert panel.info.isHidden() and panel.error.text() == "Column X is missing"

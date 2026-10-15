from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QToolBar


def click_toolbar(window, action):
    toolbar = window.findChild(QToolBar, "MainToolbar")
    QTest.mouseClick(toolbar.widgetForAction(action), Qt.MouseButton.LeftButton)


def test_new_project_asks_to_save_changes(window, settle, session, demo, registry, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "ask_save", lambda reason: asked.append(reason) or "cancel")
    window.open_path(demo)
    settle(session)
    session.add_table(registry.actions["csv.import"])
    click_toolbar(window, window.new_action)
    assert asked == ["before starting a new project"]
    assert session.project.path == demo and len(session.project.tables) == 4  # cancelled: nothing changed

    monkeypatch.setattr(window, "ask_save", lambda reason: "discard")
    click_toolbar(window, window.new_action)
    assert session.project.path is None and not session.project.tables


def test_new_project_confirms_even_without_changes(window, settle, session, demo, monkeypatch):
    confirmations = []
    monkeypatch.setattr(window, "confirm", lambda title, text, ok: confirmations.append(text) or False)
    window.open_path(demo)
    settle(session)
    assert not session.dirty
    window.new_action.trigger()
    assert confirmations and "demo.tafl" in confirmations[0]
    assert session.project.path == demo  # not confirmed: still open

    monkeypatch.setattr(window, "confirm", lambda title, text, ok: True)
    window.new_action.trigger()
    assert session.project.path is None


def test_an_empty_project_is_replaced_without_asking(window, session, monkeypatch):
    monkeypatch.setattr(window, "confirm", lambda *a: (_ for _ in ()).throw(AssertionError("asked")))
    window.new_action.trigger()  # nothing to lose: no question
    assert session.project.path is None

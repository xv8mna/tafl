"""The test of a connection belongs to that connection: its result never shows under another
one selected meanwhile, whose Test button stays usable."""

from tafl.core import Connection
from tafl.ui.connections_dialog import ConnectionsDialog


def test_a_slow_test_result_stays_with_its_connection(session, qtbot, monkeypatch):
    session.project.connections = [
        Connection("server", "sqlite", {"file": "server.db"}),
        Connection("local", "sqlite", {"file": "local.db"}),
    ]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    started = []
    monkeypatch.setattr(dialog._worker, "start", started.append)  # the test runs "for a while"
    dialog.list.setCurrentRow(0)
    dialog._test()
    assert len(started) == 1 and not dialog.test_button.isEnabled()
    assert dialog.test_result.toPlainText() == "Connecting…"

    dialog.list.setCurrentRow(1)  # another connection while the first one is tested
    assert dialog.test_button.isEnabled() and dialog.test_result.isHidden()
    dialog._tested((False, "Could not connect to the server"))  # the first test ends
    assert dialog.test_result.isHidden() and dialog.test_button.isEnabled()

    dialog.list.setCurrentRow(0)  # back at the first one: its result
    assert dialog.test_result.toPlainText() == "Could not connect to the server" and dialog.test_button.isEnabled()
    dialog.list.setCurrentRow(1)
    dialog._test()
    dialog.list.setCurrentRow(0)  # changed settings: the old result is gone
    dialog._form.changed.emit({"file": "other.db"})
    dialog.list.setCurrentRow(1)
    assert dialog.test_result.toPlainText() == "Connecting…" and not dialog.test_button.isEnabled()
    dialog.list.setCurrentRow(0)
    assert dialog.test_result.isHidden()

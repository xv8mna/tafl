"""The test of a connection belongs to that connection: its result never shows under another
one selected meanwhile, whose Test button stays usable. It says how many tables it found."""

import sqlite3
from contextlib import closing

import pytest

from tafl.api import ActionError, Connector, P
from tafl.core import Connection
from tafl.ui.connections_dialog import ConnectionsDialog


class SqliteFile(Connector):
    """A connector from a plugin, for a SQLite file."""

    id = name = "plugin"
    params = (P.File("file", "File"),)

    def connect(self, settings, ctx, *, create=False):
        return sqlite3.connect(settings.file)

    def list_tables(self, conn):
        return [name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")]


class OwnOpen(SqliteFile):
    """``open`` with the keywords it had before ``read_only``."""

    def open(self, settings, ctx, *, create=False):
        return super().open(settings, ctx, create=create)


class RequiredCreate(SqliteFile):
    """``connect`` without a default for ``create``: ``open`` always gives it."""

    def connect(self, settings, ctx, *, create):
        return super().connect(settings, ctx, create=create)


def result_of_the_test(connector, session, qtbot, monkeypatch, tmp_path) -> str:
    """What the Test button says about ``connector``, on a database with one table."""
    database = tmp_path / "a.db"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE a (x INTEGER)")
    monkeypatch.setitem(session.registry.connectors, "plugin", connector)
    session.project.connections = [Connection("a", "plugin", {"file": str(database)})]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.list.setCurrentRow(0)
    dialog._test()
    qtbot.waitUntil(lambda: dialog.test_button.isEnabled(), timeout=10_000)
    return dialog.test_result.toPlainText()


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


def test_the_test_only_reads(session, qtbot, tmp_path, leave_journal):
    """A database that a program left unfinished stays as it is, and the result says what to do."""
    database, named = tmp_path / "a.db", tmp_path / "token.txt"
    with closing(sqlite3.connect(database)) as db:
        db.execute("CREATE TABLE a (x INTEGER)")
    named.write_text("one line", encoding="utf-8")
    leave_journal(database, named)
    session.project.connections = [Connection("a", "sqlite", {"file": str(database)})]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.list.setCurrentRow(0)
    dialog._test()
    qtbot.waitUntil(lambda: dialog.test_button.isEnabled(), timeout=10_000)
    assert "open it once in its own program" in dialog.test_result.toPlainText()
    assert named.exists() and (tmp_path / "a.db-journal").exists()


def test_the_test_leaves_a_wal_file_beside_an_empty_file(session, qtbot, tmp_path):
    database, wal = tmp_path / "a.db", tmp_path / "a.db-wal"
    database.write_bytes(b"")
    wal.write_text("rows", encoding="utf-8")
    session.project.connections = [Connection("a", "sqlite", {"file": str(database)})]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    dialog.list.setCurrentRow(0)
    dialog._test()
    qtbot.waitUntil(lambda: dialog.test_button.isEnabled(), timeout=10_000)
    assert "the file a.db-wal lies beside it" in dialog.test_result.toPlainText()
    assert wal.read_text(encoding="utf-8") == "rows"


def test_the_test_says_how_many_tables_and_views(session, qtbot, tmp_path):
    for name, tables in (("one.db", ["a"]), ("two.db", ["a", "b"])):
        with closing(sqlite3.connect(tmp_path / name)) as db:
            for table in tables:
                db.execute(f"CREATE TABLE {table} (x INTEGER)")
    session.project.connections = [
        Connection("one", "sqlite", {"file": str(tmp_path / "one.db")}),
        Connection("two", "sqlite", {"file": str(tmp_path / "two.db")}),
    ]
    dialog = ConnectionsDialog(session)
    qtbot.addWidget(dialog)
    dialog.show()
    for row, text in ((0, "Connected — 1 table or view"), (1, "Connected — 2 tables and views")):
        dialog.list.setCurrentRow(row)
        dialog._test()
        qtbot.waitUntil(lambda: dialog.test_button.isEnabled(), timeout=10_000)
        assert dialog.test_result.toPlainText() == text


@pytest.mark.parametrize("plugin", [OwnOpen, RequiredCreate])
def test_the_test_works_with_a_connector_that_knows_nothing_of_read_only(plugin, session, qtbot, monkeypatch, tmp_path):
    assert result_of_the_test(plugin(), session, qtbot, monkeypatch, tmp_path) == "Connected — 1 table or view"


def test_the_test_gives_read_only_to_a_connector_that_overrides_connect_read_only(session, qtbot, monkeypatch, tmp_path):
    opened = []

    class Careful(SqliteFile):
        def connect_read_only(self, settings, ctx):
            opened.append("connect_read_only")
            return super().connect(settings, ctx)

        def open(self, settings, ctx, *, create=False, read_only=False):
            opened.append(read_only)
            return super().open(settings, ctx, create=create, read_only=read_only)

    assert result_of_the_test(Careful(), session, qtbot, monkeypatch, tmp_path) == "Connected — 1 table or view"
    assert opened == [True, "connect_read_only"]


def test_the_test_shows_a_message_of_the_connector_as_it_is(session, qtbot, monkeypatch, tmp_path):
    class Refuses(SqliteFile):
        def list_tables(self, conn):
            raise ActionError("Ask the admin for the list of tables")

        def error_message(self, error):
            return f"The driver says: {error}"

    assert result_of_the_test(Refuses(), session, qtbot, monkeypatch, tmp_path) == "Ask the admin for the list of tables"

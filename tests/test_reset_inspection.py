"""Read-only recovery inspection must release every SQLite file handle."""

import sqlite3

import pytest

from qdrant_memory import reset


@pytest.mark.parametrize("reader", [reset.has_pending_reset_file, reset.pending_scope_deletes_file])
@pytest.mark.parametrize("schema", ["absent", "valid", "invalid"])
def test_readonly_recovery_inspection_closes_connections(tmp_path, monkeypatch, reader, schema):
    """Close on early returns, completed reads and query failures, without GC."""
    directory = tmp_path / "qdrant-memory"
    directory.mkdir()
    path = directory / "state.db"
    connect = sqlite3.connect
    database = connect(path)
    try:
        if schema != "absent":
            database.execute("CREATE TABLE qdrant_reset_intents (collection TEXT)")
            database.execute(
                "CREATE TABLE qdrant_scope_deletes (collection TEXT, scope TEXT, scope_json TEXT)"
            )
            if schema == "invalid":
                database.execute(
                    "ALTER TABLE qdrant_reset_intents RENAME COLUMN collection TO wrong_column"
                )
                database.execute(
                    "ALTER TABLE qdrant_scope_deletes RENAME COLUMN collection TO wrong_column"
                )
            database.commit()
    finally:
        database.close()
    opened = []

    def capture(*args, **kwargs):
        """Retain real connections so garbage collection cannot hide a leak."""
        connection = connect(*args, **kwargs)
        opened.append(connection)
        return connection

    monkeypatch.setattr(reset.sqlite3, "connect", capture)
    try:
        if schema == "invalid":
            with pytest.raises(sqlite3.OperationalError):
                reader(tmp_path, "synthetic-collection")
        else:
            assert not reader(tmp_path, "synthetic-collection")
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            opened[0].execute("SELECT 1")
    finally:
        for connection in opened:
            connection.close()

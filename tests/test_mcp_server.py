from __future__ import annotations

import sqlite3

import pytest

pytest.importorskip("mcp")

from interfaces import mcp_server
from storage import database as db


def test_mcp_tools_are_bounded_and_open_sqlite_read_only(tmp_path, monkeypatch):
    db_path = tmp_path / "mcp.db"
    writable = db.connect(db_path)
    db.initialize(writable)
    writable.execute(
        "INSERT INTO reel(shortcode,url,first_seen_at,last_seen_at) VALUES ('ABC','https://instagram.test/ABC','now','now')"
    )
    writable.commit()
    writable.close()
    monkeypatch.setenv("REELS_DB_PATH", str(db_path))

    assert mcp_server.database_summary()["counts"]["reels"] == 1
    assert mcp_server.search_entities("restaurant", limit=999) == []
    with pytest.raises(sqlite3.OperationalError):
        mcp_server._connection().execute("DELETE FROM reel")

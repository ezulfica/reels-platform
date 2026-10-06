"""Durable history for weekly Instagram feed scans."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable

from storage.database import now


def start(conn: sqlite3.Connection) -> int:
    cur = conn.execute(
        "INSERT INTO sync_run(started_at, status) VALUES (?, 'running')", (now(),)
    )
    conn.commit()
    return int(cur.lastrowid)


def observe(conn: sqlite3.Connection, run_id: int, shortcodes: Iterable[str]) -> None:
    stamp = now()
    conn.executemany(
        "INSERT OR IGNORE INTO sync_observation(run_id, shortcode, observed_at) VALUES (?, ?, ?)",
        [(run_id, shortcode, stamp) for shortcode in shortcodes if shortcode],
    )
    conn.commit()


def finish(
    conn: sqlite3.Connection,
    run_id: int,
    *,
    status: str,
    stats: dict,
    error: str | None = None,
) -> None:
    conn.execute(
        """UPDATE sync_run SET completed_at=?, status=?, pages=?, items_seen=?,
           unique_items=?, inserted=?, updated=?, unsaved=?, error=? WHERE id=?""",
        (
            now(),
            status,
            stats.get("pages", 0),
            stats.get("seen", 0),
            conn.execute(
                "SELECT COUNT(*) FROM sync_observation WHERE run_id=?", (run_id,)
            ).fetchone()[0],
            stats.get("inserted", 0),
            stats.get("updated", 0),
            stats.get("unsaved", 0),
            error[:1000] if error else None,
            run_id,
        ),
    )
    conn.commit()

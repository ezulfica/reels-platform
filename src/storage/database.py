"""SQLite connection, schema bootstrap and pipeline-state helpers."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DB = PROJECT_ROOT / "db" / "reels.db"
SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"

# The steps that leave a trace in stage_state. "vision" and "hash" are gone: the
# first after measurement (only 3 of the corpus's 296 verified entities were
# attested by it alone), the second along with perceptual-hash deduplication
# (measured at 26% savings, abandoned). Migration 007 deleted their rows — keeping
# them meant `reels status` listing steps no code knows how to run any more.
STAGES = ("download", "asr", "ocr", "extract")


def initialize_web_state(conn: sqlite3.Connection) -> None:
    """Additive UI-owned state; never changes extracted pipeline facts."""
    conn.executescript("""
    CREATE TABLE IF NOT EXISTS chat_session (id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS chat_message (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id INTEGER NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE, role TEXT NOT NULL CHECK(role IN ('user','assistant')), content TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS idx_chat_message_session ON chat_message(session_id, id);
    """)
    conn.commit()


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def code_sha() -> str | None:
    """The commit that produced a row, `-dirty` if the tree was not clean.

    Stamped on every extraction next to the prompt fingerprint. The two together
    say what produced a result: the fingerprint covers the prompt and the JSON
    schema, this covers everything else — and `build_context()` above all, which
    composes the dossier sent to the model and determines the output as much as
    the prompt does, without being part of the fingerprint. That gap let two
    changes through silently; it is what this column closes.

    Returns None outside a git checkout rather than raising: a stamp missing is a
    stamp missing, it is not a reason to refuse to extract."""
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        if out.returncode != 0:
            return None
        sha = out.stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
        return sha + ("-dirty" if dirty.stdout.strip() else "")
    except (OSError, subprocess.SubprocessError):
        return None


def connect(db_path: Path | str = DEFAULT_DB) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


def initialize(conn: sqlite3.Connection) -> bool:
    """Create the complete schema when opening a new database.

    Existing databases are left untouched. Schema evolution is deliberately not
    supported: the project now owns one current schema rather than a chain of
    historical upgrades.
    """
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'reel'"
    ).fetchone()
    if exists:
        return False
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    return True

# ------------------------------------------------------------------ stage_state


def mark(
    conn: sqlite3.Connection,
    shortcode: str,
    stage: str,
    status: str,
    error: str | None = None,
) -> None:
    """Advance the state of one (reel, step). This is what makes the pipeline
    resumable."""
    conn.execute(
        """
        INSERT INTO stage_state(shortcode, stage, status, attempts, last_error, updated_at)
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT(shortcode, stage) DO UPDATE SET
            status     = excluded.status,
            attempts   = stage_state.attempts + 1,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (shortcode, stage, status, error, now()),
    )


def pending(
    conn: sqlite3.Connection,
    stage: str,
    limit: int | None = None,
    max_attempts: int = 3,
) -> list[str]:
    """Reels still to process for a step, excluding repeated failures."""
    sql = """
        SELECT r.shortcode
        FROM reel r
        LEFT JOIN stage_state s ON s.shortcode = r.shortcode AND s.stage = ?
        WHERE r.unsaved_at IS NULL
          AND (s.status IS NULL
               OR (s.status = 'failed' AND s.attempts < ?))
        ORDER BY r.taken_at DESC
    """
    params: list = [stage, max_attempts]
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
    return [r["shortcode"] for r in conn.execute(sql, params)]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Pipeline state in a single pass, for `reels status`."""
    one = lambda q: conn.execute(q).fetchone()[0]  # noqa: E731
    stats = {
        "reel": one("SELECT COUNT(*) FROM reel"),
        "unsaved": one("SELECT COUNT(*) FROM reel WHERE unsaved_at IS NOT NULL"),
        "with_mp4": one("SELECT COUNT(*) FROM media WHERE mp4_path IS NOT NULL"),
        "in_collection": one("SELECT COUNT(DISTINCT shortcode) FROM reel_collection"),
        "collections": one("SELECT COUNT(*) FROM collection"),
        "asr": one("SELECT COUNT(DISTINCT shortcode) FROM transcript"),
        "ocr": one("SELECT COUNT(DISTINCT shortcode) FROM screen_text"),
        "extractions": one("SELECT COUNT(*) FROM extraction"),
        "candidates": one("SELECT COUNT(*) FROM candidate"),
        "entities": one("SELECT COUNT(*) FROM entity"),
        "repertoire": one("SELECT COUNT(*) FROM extraction WHERE mode = 'repertoire'"),
    }
    return stats

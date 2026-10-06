"""Personal catalogue feedback, kept independent from extracted source facts."""

from __future__ import annotations

import json
import sqlite3
from datetime import date

from storage.database import now

STATUSES = ("considering", "shortlisted", "owned", "favorite", "disappointing", "avoid")
_UNSET = object()


def _personal(conn: sqlite3.Connection, entity_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM entity_personal WHERE entity_id=?", (entity_id,)
    ).fetchone()


def update(
    conn: sqlite3.Connection,
    entity_id: int,
    *,
    status=_UNSET,
    note=_UNSET,
    score=_UNSET,
    reviewed_at=_UNSET,
    need=_UNSET,
    interest_reason=_UNSET,
    open_questions=_UNSET,
    documented: bool = False,
) -> None:
    """Update the current opinion and append a full snapshot to its history."""
    if not conn.execute("SELECT 1 FROM entity WHERE id=?", (entity_id,)).fetchone():
        raise ValueError(f"unknown catalogue entity: {entity_id}")
    if status is not _UNSET and status is not None and status not in STATUSES:
        raise ValueError(f"unknown personal status: {status}")
    if score is not _UNSET and score is not None and not 1 <= score <= 5:
        raise ValueError("score must be between 1 and 5")
    if reviewed_at is not _UNSET and reviewed_at is not None:
        try:
            date.fromisoformat(reviewed_at)
        except (TypeError, ValueError) as error:
            raise ValueError("review date must be an ISO date (YYYY-MM-DD)") from error

    previous = _personal(conn, entity_id)
    fields = (
        "status",
        "note",
        "score",
        "reviewed_at",
        "need",
        "interest_reason",
        "open_questions",
    )
    values = {key: previous[key] if previous else None for key in fields}
    for key, value in (
        ("status", status),
        ("note", note),
        ("score", score),
        ("reviewed_at", reviewed_at),
        ("need", need),
        ("interest_reason", interest_reason),
        ("open_questions", open_questions),
    ):
        if value is not _UNSET:
            values[key] = value
    if status is not _UNSET and reviewed_at is _UNSET:
        values["reviewed_at"] = now()[:10]
    documented_at = (
        now() if documented else (previous["documented_at"] if previous else None)
    )
    stamp = now()
    conn.execute(
        """INSERT INTO entity_personal
           (entity_id,status,note,score,reviewed_at,need,interest_reason,open_questions,
            documented_at,updated_at)
           VALUES (:entity_id,:status,:note,:score,:reviewed_at,:need,:interest_reason,
                   :open_questions,:documented_at,:updated_at)
           ON CONFLICT(entity_id) DO UPDATE SET
             status=excluded.status,note=excluded.note,score=excluded.score,
             reviewed_at=excluded.reviewed_at,need=excluded.need,
             interest_reason=excluded.interest_reason,open_questions=excluded.open_questions,
             documented_at=excluded.documented_at,updated_at=excluded.updated_at""",
        {
            "entity_id": entity_id,
            **values,
            "documented_at": documented_at,
            "updated_at": stamp,
        },
    )
    conn.execute(
        """INSERT INTO entity_personal_history
           (entity_id,status,note,score,reviewed_at,need,interest_reason,open_questions,
            documented_at,changed_at)
           VALUES (:entity_id,:status,:note,:score,:reviewed_at,:need,:interest_reason,
                   :open_questions,:documented_at,:changed_at)""",
        {
            "entity_id": entity_id,
            **values,
            "documented_at": documented_at,
            "changed_at": stamp,
        },
    )
    conn.commit()


def clear_status(conn: sqlite3.Connection, entity_id: int) -> None:
    """Record an explicit return to no status, without deleting past opinions."""
    update(conn, entity_id, status=None)


def history(conn: sqlite3.Connection, entity_id: int) -> list[dict]:
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM entity_personal_history WHERE entity_id=? ORDER BY changed_at,id",
            (entity_id,),
        )
    ]


def products(
    conn: sqlite3.Connection,
    text: str = "",
    *,
    status: str | None = None,
    limit: int = 25,
) -> list[dict]:
    """Search product facts and optional personal state; no opinion is valid."""
    if status is not None and status not in STATUSES:
        raise ValueError(f"unknown personal status: {status}")
    params: list[object] = []
    sql = """SELECT e.id,e.canonical_name,e.highlights,e.why_saved,
                     e.locality,e.city,e.country,
                     e.facets_json,e.updated_at,p.status,p.note,p.score,p.reviewed_at,
                     p.need,p.interest_reason,p.open_questions,p.documented_at
              FROM entity e LEFT JOIN entity_personal p ON p.entity_id=e.id
              WHERE e.type='product'"""
    if text.strip():
        sql += " AND e.id IN (SELECT rowid FROM entity_fts WHERE entity_fts MATCH ?)"
        params.append(text)
    if status is not None:
        sql += " AND p.status=?"
        params.append(status)
    sql += " ORDER BY COALESCE(p.updated_at,e.updated_at) DESC,e.canonical_name LIMIT ?"
    params.append(limit)
    results = []
    for row in conn.execute(sql, params):
        item = dict(row)
        item["facets"] = json.loads(item.pop("facets_json") or "[]")
        item["sources"] = [
            dict(source)
            for source in conn.execute(
                """SELECT r.shortcode,r.url,c.brand,c.intention,c.evidence_json,c.highlights_json
               FROM entity_reel er JOIN reel r ON r.shortcode=er.shortcode
               LEFT JOIN candidate c ON c.id=er.candidate_id
               WHERE er.entity_id=? ORDER BY r.taken_at DESC""",
                (row["id"],),
            )
        ]
        for source in item["sources"]:
            source["evidence"] = json.loads(source.pop("evidence_json") or "[]")
            source["highlights"] = json.loads(source.pop("highlights_json") or "[]")
        item["brand"] = next((s["brand"] for s in item["sources"] if s["brand"]), None)
        item["intention"] = next(
            (s["intention"] for s in item["sources"] if s["intention"]), None
        )
        results.append(item)
    return results

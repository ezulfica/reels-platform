"""Queryable repertoire entries and personal cooking history.

This module is the seam between extraction and the ways a person uses retained
content.  Callers ask for recipes or record an attempt; they do not need to know
how a reel, its video, its Markdown note and its Qwen output are stored.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable

from storage.database import now

CONTENT_KINDS = ("recipe", "exercise", "lesson", "method", "guide", "inspiration")
RECIPE_VERDICTS = ("want_to_try", "liked", "favorite", "disappointing", "avoid")


def _strings(value: object) -> list[str]:
    return (
        [str(item).strip() for item in value if str(item).strip()]
        if isinstance(value, list)
        else []
    )


def upsert_entry(
    conn: sqlite3.Connection,
    *,
    shortcode: str,
    content_kind: str,
    title: str,
    summary: str,
    recipes: Iterable[dict],
) -> None:
    """Refresh extraction-owned fields without overwriting a published note.

    Old recipes are deactivated rather than deleted: a `recipe_attempt` remains
    meaningful even when Qwen later changes the spelling of a dish.
    """
    if content_kind not in CONTENT_KINDS:
        return
    stamp = now()
    conn.execute(
        """INSERT INTO repertoire_entry(shortcode, content_kind, title, summary, updated_at)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(shortcode) DO UPDATE SET
             content_kind=excluded.content_kind, title=excluded.title,
             summary=excluded.summary, updated_at=excluded.updated_at""",
        (shortcode, content_kind, title, summary, stamp),
    )
    conn.execute(
        "UPDATE recipe SET active = 0, updated_at = ? WHERE shortcode = ?",
        (stamp, shortcode),
    )
    for recipe in recipes:
        dish_name = str(recipe.get("dish_name") or "").strip()
        if not dish_name:
            continue
        conn.execute(
            """INSERT INTO recipe(shortcode, dish_name, cuisine, cuisine_family, course,
                                    dietary_tags_json, summary, evidence_json, confidence,
                                    active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(shortcode, dish_name) DO UPDATE SET
                 cuisine=excluded.cuisine, cuisine_family=excluded.cuisine_family,
                 course=excluded.course, dietary_tags_json=excluded.dietary_tags_json,
                 summary=excluded.summary, evidence_json=excluded.evidence_json,
                 confidence=excluded.confidence, active=1, updated_at=excluded.updated_at""",
            (
                shortcode,
                dish_name,
                recipe.get("cuisine") or None,
                recipe.get("cuisine_family") or None,
                recipe.get("course") or None,
                json.dumps(_strings(recipe.get("dietary_tags")), ensure_ascii=False),
                recipe.get("summary") or None,
                json.dumps(
                    recipe.get("evidence")
                    if isinstance(recipe.get("evidence"), list)
                    else [],
                    ensure_ascii=False,
                ),
                recipe.get("confidence"),
                stamp,
                stamp,
            ),
        )


def sync_fts(
    conn: sqlite3.Connection,
    shortcode: str,
    *,
    topic: str,
    why_saved: str,
    tags: list[str],
    key_points: list[str],
    title: str = "",
) -> None:
    row = conn.execute(
        "SELECT rowid FROM reel WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    if not row:
        return
    recipes = conn.execute(
        "SELECT dish_name, cuisine, cuisine_family FROM recipe WHERE shortcode = ? AND active = 1",
        (shortcode,),
    ).fetchall()
    conn.execute("DELETE FROM repertoire_fts WHERE rowid = ?", (row["rowid"],))
    conn.execute(
        """INSERT INTO repertoire_fts(rowid, title, topic, why_saved, tags, key_points, dishes, cuisines)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row["rowid"],
            title,
            topic,
            why_saved,
            " ".join(tags),
            " ".join(key_points),
            " ".join(row["dish_name"] for row in recipes),
            " ".join(
                value
                for row in recipes
                for value in (row["cuisine"], row["cuisine_family"])
                if value
            ),
        ),
    )


def recipes(
    conn: sqlite3.Connection,
    *,
    cuisine: str | None = None,
    verdict: str | None = None,
    course: str | None = None,
    text: str = "",
    limit: int = 20,
) -> list[dict]:
    """Return cookable entries, ranking personal favourites above untried ones."""
    sql = """
        WITH latest AS (
          SELECT recipe_id, verdict, rating, note, changes_made, cooked_at,
                 ROW_NUMBER() OVER (PARTITION BY recipe_id ORDER BY created_at DESC, id DESC) AS n
          FROM recipe_attempt
        )
        SELECT rp.id, rp.dish_name, rp.cuisine, rp.cuisine_family, rp.course,
               rp.dietary_tags_json, rp.summary, rp.confidence, re.shortcode,
               re.title, re.video_status, r.url,
               latest.verdict, latest.rating, latest.note, latest.changes_made, latest.cooked_at
        FROM recipe rp
        JOIN repertoire_entry re ON re.shortcode = rp.shortcode
        JOIN reel r ON r.shortcode = rp.shortcode
        LEFT JOIN latest ON latest.recipe_id = rp.id AND latest.n = 1
        WHERE rp.active = 1
    """
    params: list[object] = []
    if cuisine:
        sql += (
            " AND (lower(rp.cuisine) = lower(?) OR lower(rp.cuisine_family) = lower(?))"
        )
        params += [cuisine, cuisine]
    if course:
        sql += " AND rp.course = ?"
        params.append(course)
    if text.strip():
        sql += " AND (rp.dish_name LIKE ? OR COALESCE(rp.summary, '') LIKE ? OR re.title LIKE ?)"
        term = f"%{text.strip()}%"
        params += [term, term, term]
    if verdict == "untried":
        sql += " AND latest.verdict IS NULL"
    elif verdict:
        sql += " AND latest.verdict = ?"
        params.append(verdict)
    sql += """ ORDER BY CASE latest.verdict WHEN 'favorite' THEN 0 WHEN 'liked' THEN 1
                    WHEN 'want_to_try' THEN 2 WHEN NULL THEN 3 WHEN 'disappointing' THEN 4 ELSE 5 END,
                    rp.updated_at DESC LIMIT ?"""
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [
        {**dict(row), "dietary_tags": json.loads(row["dietary_tags_json"] or "[]")}
        for row in rows
    ]


def recipe_attempts(conn: sqlite3.Connection, recipe_id: int) -> list[dict]:
    """Return the append-only personal history for one recipe."""
    return [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM recipe_attempt WHERE recipe_id=? ORDER BY created_at DESC,id DESC",
            (recipe_id,),
        )
    ]


def record_attempt(
    conn: sqlite3.Connection,
    recipe_id: int,
    *,
    verdict: str,
    rating: int | None = None,
    note: str | None = None,
    changes_made: str | None = None,
    cooked_at: str | None = None,
) -> int:
    if verdict not in RECIPE_VERDICTS:
        raise ValueError(f"unknown recipe verdict: {verdict}")
    if rating is not None and not 1 <= rating <= 5:
        raise ValueError("rating must be between 1 and 5")
    exists = conn.execute("SELECT 1 FROM recipe WHERE id = ?", (recipe_id,)).fetchone()
    if not exists:
        raise ValueError(f"unknown recipe id: {recipe_id}")
    cursor = conn.execute(
        """INSERT INTO recipe_attempt(recipe_id, cooked_at, verdict, rating, changes_made, note, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (recipe_id, cooked_at, verdict, rating, changes_made, note, now()),
    )
    conn.commit()
    return int(cursor.lastrowid)

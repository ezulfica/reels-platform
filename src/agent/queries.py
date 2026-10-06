"""Small read-only query surface for Codex, Hermes or a Discord agent.

The agent sees facts and provenance, not extraction internals. Every function in
this module only reads the supplied SQLite connection and returns JSON-shaped
values; publication is deliberately outside this package.
"""

from __future__ import annotations

import sqlite3
from typing import Any

from . import search as query
from domain import repertoire
from domain.reel_library import get as get_reel


def search_entities(
    conn: sqlite3.Connection,
    text: str,
    *,
    type_: str | None = None,
    city: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search resolved catalogue entities with their source reels."""
    return query.search(conn, text, type_=type_, city=city, limit=limit)


def search_recipes(
    conn: sqlite3.Connection,
    text: str = "",
    *,
    cuisine: str | None = None,
    family: str | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search active recipes and their cooking history."""
    rows = repertoire.recipes(conn, cuisine=cuisine or family, text=text, limit=limit)
    if family and cuisine:
        rows = [row for row in rows if row.get("cuisine_family") == family]
    return rows


def search_repertoire(
    conn: sqlite3.Connection, text: str = "", *, limit: int = 20
) -> list[dict[str, Any]]:
    """Search reusable guides, lessons and exercises by their extracted content."""
    return query.search_repertoire(conn, text, limit=limit)


def get_entity(conn: sqlite3.Connection, entity_id: int) -> dict[str, Any] | None:
    """Return one resolved entity and its personal feedback."""
    row = conn.execute("SELECT * FROM entity WHERE id = ?", (entity_id,)).fetchone()
    if not row:
        return None
    result = dict(row)
    result["reels"] = [
        dict(item)
        for item in conn.execute(
            "SELECT r.shortcode, r.url, r.username FROM entity_reel er "
            "JOIN reel r ON r.shortcode = er.shortcode WHERE er.entity_id = ?",
            (entity_id,),
        )
    ]
    result["feedback"] = [
        dict(item)
        for item in conn.execute(
            "SELECT * FROM entity_personal_history WHERE entity_id = ? ORDER BY id DESC",
            (entity_id,),
        )
    ]
    return result


def database_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    """Return the compact dashboard summary used by future agent interfaces."""
    counts = {
        key: value
        for key, value in conn.execute(
            "SELECT 'reels', COUNT(*) FROM reel UNION ALL "
            "SELECT 'entities', COUNT(*) FROM entity UNION ALL "
            "SELECT 'recipes', COUNT(*) FROM recipe WHERE active = 1 UNION ALL "
            "SELECT 'extractions', COUNT(*) FROM extraction WHERE ok = 1"
        )
    }
    by_type = {
        row["type"]: row["n"]
        for row in conn.execute(
            "SELECT type, COUNT(*) n FROM entity GROUP BY type ORDER BY n DESC, type"
        )
    }
    by_kind = {
        row["content_kind"]: row["n"]
        for row in conn.execute(
            "SELECT COALESCE(content_kind, ''), COUNT(*) n FROM repertoire_entry "
            "GROUP BY content_kind ORDER BY n DESC"
        )
    }
    return {
        "counts": counts,
        "entities_by_type": by_type,
        "repertoire_by_kind": by_kind,
    }

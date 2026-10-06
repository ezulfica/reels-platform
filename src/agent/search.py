"""The single entry point exposed to the recommendation agent.

Hybrid FTS5 search plus structured filters, returning catalogue fiches with their
source reels. This is the ONLY thing the agent sees — not the extraction schema,
not the unverified candidates, not the detail of how a fiche was built.
"""

from __future__ import annotations

import json
import sqlite3


def search(
    conn: sqlite3.Connection,
    text: str,
    *,
    type_: str | None = None,
    city: str | None = None,
    limit: int = 10,
) -> list[dict]:
    sql = """
        SELECT ge.id FROM entity_fts fts
        JOIN entity ge ON ge.id = fts.rowid
        WHERE entity_fts MATCH ?
    """
    params: list = [text]
    if type_:
        sql += " AND ge.type = ?"
        params.append(type_)
    if city:
        sql += " AND ge.city = ?"
        params.append(city)
    sql += " LIMIT ?"
    params.append(limit)

    ids = [r["id"] for r in conn.execute(sql, params)]
    results = []
    for entity_id in ids:
        entity = conn.execute(
            "SELECT * FROM entity WHERE id = ?", (entity_id,)
        ).fetchone()
        reels = conn.execute(
            "SELECT r.shortcode, r.url, r.username FROM entity_reel ger"
            " JOIN reel r ON r.shortcode = ger.shortcode"
            " WHERE ger.entity_id = ?",
            (entity_id,),
        ).fetchall()
        results.append(
            {
                "id": entity["id"],
                "name": entity["canonical_name"],
                "type": entity["type"],
                "facets": json.loads(entity["facets_json"] or "[]"),
                "scale": entity["scale"],
                "locality": entity["locality"],
                "city": entity["city"],
                "country": entity["country"],
                "address": entity["address"],
                "highlights": entity["highlights"],
                "why_saved": entity["why_saved"],
                "gmaps_url": entity["gmaps_url"],
                "reels": [
                    {
                        "shortcode": r["shortcode"],
                        "url": r["url"],
                        "username": r["username"],
                    }
                    for r in reels
                ],
            }
        )
    return results


def search_repertoire(
    conn: sqlite3.Connection, text: str = "", *, limit: int = 25
) -> list[dict]:
    """The repertoire counterpart of search(): reels whose value is in being
    read or watched in full (a recipe, a routine, a tutorial) rather than a
    place or product to go find. One row per reel — repertoire entries do not
    merge across reels the way entities do, so there is no resolution step to
    read through, just `extraction`/`classification`/`reel` directly.

    `text` empty returns the most recent repertoire reels instead of running a
    MATCH query — FTS5 rejects an empty MATCH string."""
    if text.strip():
        rows = conn.execute(
            "SELECT r.shortcode FROM repertoire_fts fts"
            " JOIN reel r ON r.rowid = fts.rowid"
            " WHERE repertoire_fts MATCH ? LIMIT ?",
            (text, limit),
        ).fetchall()
        shortcodes = [r["shortcode"] for r in rows]
    else:
        rows = conn.execute(
            "SELECT shortcode FROM extraction WHERE mode = 'repertoire'"
            " ORDER BY extracted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        shortcodes = [r["shortcode"] for r in rows]

    results = []
    for shortcode in shortcodes:
        cl = conn.execute(
            "SELECT predicted_topic, why_saved FROM classification WHERE shortcode = ?",
            (shortcode,),
        ).fetchone()
        ex = conn.execute(
            "SELECT tags_json FROM extraction WHERE shortcode = ?", (shortcode,)
        ).fetchone()
        reel = conn.execute(
            "SELECT url, username FROM reel WHERE shortcode = ?", (shortcode,)
        ).fetchone()
        results.append(
            {
                "shortcode": shortcode,
                "topic": cl["predicted_topic"] if cl else None,
                "why_saved": cl["why_saved"] if cl else None,
                "tags": json.loads(ex["tags_json"] or "[]")
                if ex and ex["tags_json"]
                else [],
                "url": reel["url"] if reel else None,
                "username": reel["username"] if reel else None,
            }
        )
    return results

"""The reel-library module: a readable fiche over one saved reel.

The catalogue answers "where should I go / what should I buy?".  This module
answers a different question: "what did I save in this video and how do I get
back to it?"  Its small interface intentionally hides SQLite joins, JSON
columns, the optional local MP4, and Markdown rendering from the web UI and the
CLI.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from .canonicalization import normalised_key


def search(
    conn: sqlite3.Connection, text: str = "", *, content_kind: str | None = None, limit: int = 25
) -> list[dict]:
    """Return the most relevant repertoire fiches, newest first without text."""
    if text.strip():
        rows = conn.execute(
            "SELECT r.shortcode FROM repertoire_fts fts"
            " JOIN reel r ON r.rowid = fts.rowid"
            " JOIN repertoire_entry re ON re.shortcode=r.shortcode"
            " WHERE repertoire_fts MATCH ? AND (? IS NULL OR re.content_kind=?) LIMIT ?",
            (text, content_kind, content_kind, limit),
        ).fetchall()
        shortcodes = [row["shortcode"] for row in rows]
    else:
        rows = conn.execute(
            "SELECT e.shortcode FROM extraction e"
            " JOIN repertoire_entry re ON re.shortcode=e.shortcode"
            " WHERE e.mode = 'repertoire' AND (? IS NULL OR re.content_kind=?)"
            " ORDER BY e.extracted_at DESC LIMIT ?",
            (content_kind, content_kind, limit),
        ).fetchall()
        shortcodes = [row["shortcode"] for row in rows]

    return [
        fiche for shortcode in shortcodes if (fiche := get(conn, shortcode)) is not None
    ]


def get(conn: sqlite3.Connection, shortcode: str) -> dict | None:
    """Build one fiche.  The caller never needs to know the storage layout."""
    row = conn.execute(
        """
        SELECT r.shortcode, r.url, r.username, r.caption,
               m.mp4_path, m.poster_path, m.proxy_path,
               re.content_kind, re.title AS entry_title, re.summary AS entry_summary,
               e.mode, e.tags_json, e.key_points_json,
               cl.predicted_topic, cl.why_saved
        FROM reel r
        JOIN extraction e ON e.shortcode = r.shortcode
        LEFT JOIN media m ON m.shortcode = r.shortcode
        LEFT JOIN repertoire_entry re ON re.shortcode = r.shortcode
        LEFT JOIN classification cl ON cl.shortcode = r.shortcode
        WHERE r.shortcode = ?
        """,
        (shortcode,),
    ).fetchone()
    if not row:
        return None

    candidates = conn.execute(
        """
        SELECT name, name_latin, type, facets_json, highlights_json, tags_json,
               why_saved, verified, evidence_status, source
        FROM candidate
        WHERE shortcode = ?
        ORDER BY CASE source WHEN 'human' THEN 0 ELSE 1 END, id
        """,
        (shortcode,),
    ).fetchall()

    def values(raw: str | None) -> list[str]:
        try:
            value = json.loads(raw or "[]")
        except json.JSONDecodeError:
            return []
        return [item for item in value if isinstance(item, str) and item.strip()]

    rendered_candidates = []
    seen_candidates: dict[tuple[str, str], int] = {}
    for candidate in candidates:
        key = (
            candidate["type"],
            _name_key(candidate["name_latin"] or candidate["name"]),
        )
        rendered = {
            "name": candidate["name"],
            "name_latin": candidate["name_latin"],
            "type": candidate["type"],
            "facets": values(candidate["facets_json"]),
            "highlights": values(candidate["highlights_json"]),
            "tags": values(candidate["tags_json"]),
            "why_saved": candidate["why_saved"],
            "verified": candidate["verified"],
            "evidence_status": candidate["evidence_status"],
        }
        if key not in seen_candidates:
            seen_candidates[key] = len(rendered_candidates)
            rendered_candidates.append(rendered)
            continue
        # Historical human rows are ordered first but must not erase the
        # concrete highlights or tags retained from the extraction.
        kept = rendered_candidates[seen_candidates[key]]
        for field in ("facets", "highlights", "tags"):
            kept[field] = list(dict.fromkeys(kept[field] + rendered[field]))
        if not kept["why_saved"] and rendered["why_saved"]:
            kept["why_saved"] = rendered["why_saved"]

    return {
        "shortcode": row["shortcode"],
        "url": row["url"],
        "username": row["username"],
        "caption": row["caption"],
        "mode": row["mode"],
        "topic": row["predicted_topic"],
        "why_saved": row["why_saved"],
        "tags": values(row["tags_json"]),
        "key_points": values(row["key_points_json"]),
        # A path is data, not a URL.  The web adapter maps it to /media/<code>
        # only after checking it remains under the managed media directory.
        "mp4_path": row["mp4_path"],
        "poster_path": row["poster_path"],
        "proxy_path": row["proxy_path"],
        "content_kind": row["content_kind"],
        "entry_title": row["entry_title"],
        "entry_summary": row["entry_summary"],
        "candidates": rendered_candidates,
    }


def _name_key(value: str) -> str:
    return normalised_key(value)


def local_media_path(fiche: dict, media_root: Path) -> Path | None:
    """Return an existing MP4 only when it is inside the managed media root."""
    raw_path = fiche.get("mp4_path")
    if not raw_path:
        return None
    try:
        path = Path(raw_path).resolve()
        path.relative_to(media_root.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None

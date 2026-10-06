"""Instagram collection names.

The endpoint is **paginated**: without pagination you only see the first page —
20 collections out of 50 in this account, and 142 missing memberships.
"""

from __future__ import annotations

import random
import sqlite3
import time

import requests

from storage.database import now

from .session import API, get_json

COLLECTION_TYPES = '["ALL_MEDIA_AUTO_COLLECTION","MEDIA","AUDIO_AUTO_COLLECTION"]'


def fetch_collections(session: requests.Session) -> list[dict]:
    items: list[dict] = []
    max_id: str | None = None

    while True:
        params = {"collection_types": COLLECTION_TYPES}
        if max_id:
            params["max_id"] = max_id
        payload = get_json(session, f"{API}/collections/list/", params)
        items.extend(payload.get("items") or [])

        max_id = payload.get("next_max_id")
        if not payload.get("more_available") or not max_id:
            return items
        time.sleep(random.uniform(1.0, 2.0))


def sync_collections(
    conn: sqlite3.Connection, session: requests.Session
) -> dict[str, int]:
    """Fill in the names. Memberships come from the feed, not from here.

    Automatic collections (ALL_MEDIA_*, AUDIO_*) have a non-numeric id and are not
    folders the user created, so we skip them.
    """
    stamp = now()
    stats = {"seen": 0, "named": 0, "auto_skipped": 0}

    for item in fetch_collections(session):
        cid = str(item.get("collection_id") or "")
        stats["seen"] += 1
        if not cid.isdigit():
            stats["auto_skipped"] += 1
            continue

        conn.execute(
            """
            INSERT INTO collection(collection_id, name, item_count, synced_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(collection_id) DO UPDATE SET
                name       = excluded.name,
                item_count = excluded.item_count,
                synced_at  = excluded.synced_at
            """,
            (
                cid,
                (item.get("collection_name") or cid).strip(),
                item.get("collection_media_count"),
                stamp,
            ),
        )
        stats["named"] += 1

    conn.commit()
    return stats

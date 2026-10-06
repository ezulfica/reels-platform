"""Step 1: parsing the Instagram API response and immutable upsert.

The normalisation logic is the one validated in scrap-saved-instagram/ig_saved.py
(1345 reels extracted, 65 pages, 0 duplicates).
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
import sys
import time
from collections.abc import Callable, Iterable
from typing import Any

import requests

from storage.database import now

from .session import API, get_json

# A caption announcing a list signals a multi-entity reel: v1 only produced a
# single, truncated entity for those.
LIST_MARKERS = re.compile(
    r"(all the (locations|places|spots)|below\s*[⬇️👇]|"
    r"\b\d+\s*(spots|places|lieux|adresses|restos)\b|"
    r"compilation|liste|thread|save this|enregistre)",
    re.IGNORECASE,
)

HASHTAG = re.compile(r"#(\w+)", re.UNICODE)
MENTION = re.compile(r"@([\w.]+)")
URL = re.compile(r"https?://\S+")


def media_kind(media: dict) -> str:
    if media.get("product_type") == "clips":
        return "reel"
    media_type = media.get("media_type")
    if media_type == 8:
        return "carousel"
    if media_type == 2:
        return "video"
    return "post"


def parse_reel(media: dict) -> dict[str, Any]:
    """Reel fields from a raw media object."""
    code = media.get("code") or ""
    kind = media_kind(media)
    path = "reel" if kind == "reel" else "p"
    user = media.get("user") or {}
    return {
        "shortcode": code,
        "ig_id": str(media.get("pk") or media.get("id") or ""),
        "url": f"https://www.instagram.com/{path}/{code}/" if code else "",
        "username": user.get("username") or "",
        "caption": (media.get("caption") or {}).get("text") or "",
        "taken_at": media.get("taken_at"),
        "media_type": media.get("media_type"),
        "product_type": media.get("product_type"),
        "kind": kind,
        "raw_json": json.dumps(media, ensure_ascii=False),
    }


def parse_context(media: dict) -> dict[str, Any]:
    """Rich fields present in the raw payload and never used in v1."""
    caption = (media.get("caption") or {}).get("text") or ""
    dumps = lambda v: json.dumps(v, ensure_ascii=False) if v else None
    return {
        "shortcode": media.get("code") or "",
        "location_json": dumps(media.get("location")),
        "usertags_json": dumps(media.get("usertags")),
        "music_json": dumps((media.get("clips_metadata") or {}).get("music_info")),
        "coauthors_json": dumps(media.get("coauthor_producers")),
        "accessibility_caption": media.get("accessibility_caption"),
        "hashtags": dumps(HASHTAG.findall(caption)),
        "mentions": dumps(MENTION.findall(caption)),
        "links": dumps(URL.findall(caption)),
        "has_list_marker": 1 if LIST_MARKERS.search(caption) else 0,
    }


def collection_ids(media: dict) -> list[str]:
    """The human label: where YOU filed this reel. 722/1345 carry one."""
    return [str(i) for i in (media.get("saved_collection_ids") or [])]


# ------------------------------------------------------------------------ upsert


def upsert_reels(conn: sqlite3.Connection, medias: Iterable[dict]) -> dict[str, int]:
    """Insert or refresh. Two successive runs yield the same rows.

    This is level-1 deduplication: `first_seen_at` is frozen at first sight,
    `last_seen_at` advances on every run. v2 missed this and processed the same
    reel twice (7_Db3Eah_xPOL and 86_Db3Eah_xPOL in its processed/ folder).
    """
    stamp = now()
    known = {r["shortcode"] for r in conn.execute("SELECT shortcode FROM reel")}
    inserted = updated = 0
    seen: list[str] = []

    for media in medias:
        row = parse_reel(media)
        if not row["shortcode"]:
            continue
        seen.append(row["shortcode"])
        if row["shortcode"] in known:
            updated += 1
        else:
            inserted += 1
            known.add(row["shortcode"])

        conn.execute(
            """
            INSERT INTO reel(shortcode, ig_id, url, username, caption, taken_at,
                                    media_type, product_type, kind, raw_json,
                                    first_seen_at, last_seen_at)
            VALUES (:shortcode, :ig_id, :url, :username, :caption, :taken_at,
                    :media_type, :product_type, :kind, :raw_json, :stamp, :stamp)
            ON CONFLICT(shortcode) DO UPDATE SET
                caption      = excluded.caption,
                username     = excluded.username,
                raw_json     = excluded.raw_json,
                last_seen_at = excluded.last_seen_at,
                unsaved_at   = NULL
            """,
            {**row, "stamp": stamp},
        )

        ctx = parse_context(media)
        conn.execute(
            """
            INSERT INTO reel_context(shortcode, location_json, usertags_json,
                music_json, coauthors_json, accessibility_caption, hashtags, mentions,
                links, has_list_marker)
            VALUES (:shortcode, :location_json, :usertags_json, :music_json,
                    :coauthors_json, :accessibility_caption, :hashtags, :mentions,
                    :links, :has_list_marker)
            ON CONFLICT(shortcode) DO UPDATE SET
                location_json         = excluded.location_json,
                usertags_json         = excluded.usertags_json,
                music_json            = excluded.music_json,
                coauthors_json        = excluded.coauthors_json,
                accessibility_caption = excluded.accessibility_caption,
                hashtags              = excluded.hashtags,
                mentions              = excluded.mentions,
                links                 = excluded.links,
                has_list_marker       = excluded.has_list_marker
            """,
            ctx,
        )

        for cid in collection_ids(media):
            conn.execute(
                "INSERT OR IGNORE INTO collection(collection_id, name) VALUES (?, ?)",
                (cid, cid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO reel_collection(shortcode, collection_id)"
                " VALUES (?, ?)",
                (row["shortcode"], cid),
            )

    conn.commit()
    return {"seen": len(seen), "inserted": inserted, "updated": updated}


def sync(
    conn: sqlite3.Connection,
    session: requests.Session,
    limit: int | None = None,
    full: bool = False,
    seen_callback: Callable[[set[str]], None] | None = None,
) -> dict[str, int]:
    """Walk the saved feed and write straight to the database.

    In steady state, stops as soon as a whole page contains only already-known
    reels: the feed is ordered by save date descending, so everything after it is
    already in the database. A daily run then costs 1 request instead of 65.
    `full=True` forces the complete walk.
    """
    known = {r["shortcode"] for r in conn.execute("SELECT shortcode FROM reel")}
    seen: set[str] = set()
    stats = {"pages": 0, "seen": 0, "inserted": 0, "updated": 0}
    max_id: str | None = None
    # Only a walk carried through to the end of the feed licenses the conclusion
    # that a missing reel was unsaved. An early stop or a --limit has seen only a
    # fraction: concluding from that would mark the whole corpus as gone.
    complete = False

    while True:
        payload = get_json(
            session, f"{API}/feed/saved/posts/", {"max_id": max_id} if max_id else {}
        )
        medias = [entry.get("media", entry) for entry in (payload.get("items") or [])]
        stats["pages"] += 1

        batch = upsert_reels(conn, medias)
        stats["seen"] += batch["seen"]
        stats["inserted"] += batch["inserted"]
        stats["updated"] += batch["updated"]
        codes = {m.get("code") for m in medias if m.get("code")}
        seen.update(codes)
        if seen_callback:
            seen_callback(codes)
        print(
            f"  page {stats['pages']:>3}: +{len(medias):>2}"
            f"  (total {stats['seen']}, new {stats['inserted']})",
            file=sys.stderr,
        )

        if limit and stats["seen"] >= limit:
            break
        if not full and known and codes and codes <= known:
            print("  page entirely known -> stopping early", file=sys.stderr)
            break

        max_id = payload.get("next_max_id")
        if not payload.get("more_available") or not max_id:
            complete = True
            break
        time.sleep(random.uniform(1.5, 3.5))

    if complete:
        stats["unsaved"] = mark_unsaved(conn, seen)

    return stats


def mark_unsaved(conn: sqlite3.Connection, seen: set[str]) -> int:
    """Mark as unsaved whatever is in the database but absent from the feed.

    We never delete: the history of what was unsaved has value.
    """
    stamp = now()
    cur = (
        conn.execute(
            "UPDATE reel SET unsaved_at = ? "
            "WHERE unsaved_at IS NULL AND shortcode NOT IN ({})".format(
                ",".join("?" * len(seen))
            ),
            [stamp, *seen],
        )
        if seen
        else None
    )
    conn.commit()
    return cur.rowcount if cur else 0

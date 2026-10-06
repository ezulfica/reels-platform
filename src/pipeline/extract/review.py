"""Human review queue.

Read-only extraction diagnostics. This module only answers one question:
which reels deserve inspection, and why? The output does not change the data.

- REJECTED : an entity `verify` threw out. Sometimes rightly (OCR noise),
             sometimes wrongly (cf. "scallion pancake", rejected because it was
             mixed with an unreadable OCR prefix while the name itself was good).
- SILENT   : `is_actionable=0` on a reel with a substantial caption. Flags the
             false negatives that make a reel yield nothing at all — no entity is
             extracted downstream once that gate is shut.
- UNCERTAIN: an entity kept, but with low confidence.

A reel that already has a hand-entered entity (candidate source='human') leaves
the queue: no point flagging again what has been dealt with.
"""

from __future__ import annotations

import sqlite3

REASON_LABELS = {
    "rejected": "entity rejected (possibly wrongly)",
    "evidence": "evidence missing or invalid",
    "silent": "doubtful non-actionable (the reel yields nothing)",
    "uncertain": "entity kept but low confidence",
}


def queue(
    conn: sqlite3.Connection,
    limit: int | None = None,
    reason: str | None = None,
    confidence_threshold: float = 0.7,
) -> list[dict]:
    items: list[dict] = []

    if reason in (None, "rejected"):
        for r in conn.execute(
            "SELECT shortcode, name, type, verification_note FROM candidate"
            " WHERE verified = 0"
            " AND COALESCE(evidence_status, '') NOT IN ('missing', 'invalid')"
        ):
            items.append(
                {
                    "shortcode": r["shortcode"],
                    "reason": "rejected",
                    "detail": f"{r['name']!r} ({r['type']}) — {r['verification_note']}",
                }
            )

    if reason in (None, "evidence"):
        for r in conn.execute(
            "SELECT shortcode, name, type, evidence_note FROM candidate"
            " WHERE evidence_status IN ('missing', 'invalid')"
        ):
            items.append(
                {
                    "shortcode": r["shortcode"],
                    "reason": "evidence",
                    "detail": f"{r['name']!r} ({r['type']}) — {r['evidence_note']}",
                }
            )

    if reason in (None, "silent"):
        for r in conn.execute(
            "SELECT r.shortcode, r.caption, cl.predicted_topic, cl.why_saved"
            " FROM classification cl JOIN reel r ON r.shortcode = cl.shortcode"
            " WHERE cl.is_actionable = 0 AND length(r.caption) > 60"
        ):
            preview = (r["caption"] or "")[:80].replace("\n", " ")
            items.append(
                {
                    "shortcode": r["shortcode"],
                    "reason": "silent",
                    "detail": f"[{r['predicted_topic']}] caption: {preview}...",
                }
            )

    if reason in (None, "uncertain"):
        for r in conn.execute(
            "SELECT shortcode, name, type, confidence FROM candidate"
            " WHERE verified = 1 AND source = 'llm' AND confidence < ?",
            (confidence_threshold,),
        ):
            items.append(
                {
                    "shortcode": r["shortcode"],
                    "reason": "uncertain",
                    "detail": f"{r['name']!r} ({r['type']}) — confidence {r['confidence']:.2f}",
                }
            )

    # Rejected and silent first: more likely to hide a real error than a mere lack
    # of confidence about an entity that is already correct.
    order = {"evidence": 0, "rejected": 1, "silent": 2, "uncertain": 3}
    items.sort(key=lambda i: order[i["reason"]])
    return items[:limit] if limit else items

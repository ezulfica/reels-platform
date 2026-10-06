"""Re-measure, against the current database, the thresholds that were set by hand.

Every value in `config/settings.py` was calibrated on 60 reels downloaded out of
1347. A threshold set once and never re-examined eventually bites: that is exactly
what happened to `num_ctx=8192`, which reserved more than twice the KV cache it
needed and ended up crashing llama-server on an 8 GB card.

This module changes nothing: it prints the three indicators that were used to set
the thresholds, and flags those that no longer hold. Re-run it as the corpus
grows.
"""

from __future__ import annotations

import re
import sqlite3
import statistics
import unicodedata

from config import settings as config

# Beyond this, the safety margin on num_ctx is no longer enough to absorb a reel
# more talkative than any seen so far.
CTX_ALERT_THRESHOLD = 0.80


def _norm(text: str | None) -> str:
    text = unicodedata.normalize("NFKD", text or "")
    text = "".join(c for c in text if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", text.lower()))


def _col(row, key: str) -> str:
    return row[key] if row and row[key] else ""


def context_sizes(conn: sqlite3.Connection) -> dict:
    """Length of the context dossiers, in estimated tokens (~3 characters per
    token). Says whether num_ctx still holds."""
    from pipeline.extract.extract import SYSTEM_PROMPT, build_context

    sizes = []
    for row in conn.execute("SELECT shortcode FROM reel WHERE unsaved_at IS NULL"):
        ctx = build_context(conn, row["shortcode"])
        if ctx.strip():
            sizes.append(len(ctx) // 3)
    if not sizes:
        return {}
    system = len(SYSTEM_PROMPT) // 3
    return {
        "n": len(sizes),
        "median": statistics.median(sizes),
        "p90": sorted(sizes)[int(len(sizes) * 0.9)],
        "max": max(sizes),
        "worst_case": max(sizes) + system,
        "system": system,
        "num_ctx": config.num_ctx(),
    }


def frames(conn: sqlite3.Connection) -> dict:
    """Distribution of OCR work by frame count. Says whether the cap cuts in the
    right place — that is, into the expensive tail, not into the useful reels."""
    values = [
        r["frames_used"]
        for r in conn.execute(
            "SELECT frames_used FROM screen_text WHERE frames_used IS NOT NULL"
        )
    ]
    if not values:
        return {}
    cap = config.ocr_max_frames()
    return {
        "n": len(values),
        "total": sum(values),
        "median": statistics.median(values),
        "max": max(values),
        "cap": cap,
        "at_cap": sum(1 for v in values if v >= cap),
        "possible_saving": sum(values) - sum(min(v, cap) for v in values),
    }


def asr_confidence(conn: sqlite3.Connection) -> dict:
    """Distribution of transcription confidence, per reel.

    Purely diagnostic: no filter acts on it. Whisper transcribes singing markedly
    less confidently than speech, so a low confidence signals a musical soundtrack
    whose lyrics end up in the context dossier as if they described the subject
    (cf. DZkXscZRD4j). Excluding those reels here would need a threshold fallible
    in both directions; the sorting belongs to extraction, which reads caption, OCR
    and audio together. This measurement is for spotting cases by hand. Stays empty
    until `asr` has been replayed with the version that keeps avg_logprob."""
    import json

    values = []
    for row in conn.execute(
        "SELECT shortcode, segments_json FROM transcript WHERE has_speech = 1"
    ):
        segs = json.loads(row["segments_json"] or "[]")
        lp = [s["avg_logprob"] for s in segs if "avg_logprob" in s]
        if lp:
            values.append((sum(lp) / len(lp), row["shortcode"]))
    if not values:
        return {}
    values.sort()
    return {
        "n": len(values),
        "min": values[0],
        "p10": values[max(0, len(values) // 10)],
        "median": values[len(values) // 2],
        "max": values[-1],
        "worst": values[:5],
    }


def sources(conn: sqlite3.Connection) -> dict:
    """Share of verified entities attested by a single source. This is what says
    whether a source has become useless — or necessary again."""
    count = {"total": 0, "caption_asr": 0, "ocr_only": 0, "reworded": 0}
    carriers: dict[str, int] = {}
    # LLM candidates only: a hand-entered entity says nothing about which source
    # the extraction managed to read, which is the whole question here.
    for row in conn.execute(
        """SELECT shortcode, name FROM candidate
           WHERE verified = 1 AND source = 'llm'"""
    ):
        shortcode = row["shortcode"]
        reel = conn.execute(
            "SELECT caption FROM reel WHERE shortcode = ?", (shortcode,)
        ).fetchone()
        asr = conn.execute(
            "SELECT text FROM transcript WHERE shortcode = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (shortcode,),
        ).fetchone()
        ocr = conn.execute(
            "SELECT text FROM screen_text WHERE shortcode = ?"
            " ORDER BY created_at DESC LIMIT 1",
            (shortcode,),
        ).fetchone()
        ctx = conn.execute(
            "SELECT mentions, hashtags FROM reel_context WHERE shortcode = ?",
            (shortcode,),
        ).fetchone()

        free_text = _norm(
            " ".join(
                [
                    _col(reel, "caption"),
                    _col(asr, "text"),
                    _col(ctx, "mentions"),
                    _col(ctx, "hashtags"),
                ]
            )
        )
        name = _norm(row["name"]).strip()
        count["total"] += 1
        if name and name in free_text:
            count["caption_asr"] += 1
        elif name and name in _norm(_col(ocr, "text")):
            count["ocr_only"] += 1
            carriers[shortcode] = carriers.get(shortcode, 0) + 1
        else:
            count["reworded"] += 1
    count["carrier_reels"] = carriers
    return count


def report(conn: sqlite3.Connection) -> list[str]:
    """The three measurements, plus the alerts. Returns lines to print."""
    out: list[str] = []
    alerts: list[str] = []

    ctx = context_sizes(conn)
    if ctx:
        margin = 1 - ctx["worst_case"] / ctx["num_ctx"]
        out += [
            "",
            f"  CONTEXT  ({ctx['n']} reels, estimated tokens)",
            f"    median {ctx['median']:.0f} | p90 {ctx['p90']} | max {ctx['max']}",
            f"    worst case with the system prompt ({ctx['system']}): {ctx['worst_case']}",
            f"    num_ctx = {ctx['num_ctx']}  ->  margin {margin:.0%}",
        ]
        if ctx["worst_case"] > ctx["num_ctx"] * CTX_ALERT_THRESHOLD:
            alerts.append(
                f"num_ctx={ctx['num_ctx']} leaves only {margin:.0%} margin above the "
                f"worst case ({ctx['worst_case']} tokens). Overflowing truncates the "
                "prompt WITHOUT raising — raise REELS_NUM_CTX."
            )

    fr = frames(conn)
    if fr:
        out += [
            "",
            f"  OCR FRAMES  ({fr['n']} reels, {fr['total']} frames in total)",
            f"    median {fr['median']:.0f} | max {fr['max']} | cap {fr['cap']}",
            f"    {fr['at_cap']} reels reach the cap",
            f"    saving if the cap were applied: {fr['possible_saving']} frames",
        ]

    ca = asr_confidence(conn)
    if ca:
        out += [
            "",
            f"  ASR CONFIDENCE  ({ca['n']} reels with speech)",
            (
                f"    median {ca['median'][0]:.2f} | p10 {ca['p10'][0]:.2f} "
                f"| min {ca['min'][0]:.2f} | max {ca['max'][0]:.2f}"
            ),
            "    the 5 least confident (probable musical soundtrack):",
        ] + [f"      {sc}  {v:.2f}" for v, sc in ca["worst"]]
    else:
        out += [
            "",
            "  ASR CONFIDENCE  : unavailable",
            "    avg_logprob has only been kept since the current version —",
            "    replay `reels asr` to get the diagnostic.",
        ]

    src = sources(conn)
    if src and src["total"]:
        carriers = src["carrier_reels"]
        out += [
            "",
            f"  SOURCES  ({src['total']} verified entities)",
            f"    attested by caption/ASR/mentions : {src['caption_asr']}",
            (
                f"    attested ONLY by the OCR         : {src['ocr_only']}"
                f"  ({100 * src['ocr_only'] // src['total']}%)"
                f" across {len(carriers)} reels"
            ),
            f"    reworded by the model            : {src['reworded']}",
        ]
        # A reel carrying OCR-only entities that exceeds the frame cap is the case
        # where the cap starts costing entities, not just time.
        if fr:
            at_risk = []
            for shortcode, n in carriers.items():
                row = conn.execute(
                    "SELECT frames_used FROM screen_text WHERE shortcode = ?"
                    " ORDER BY created_at DESC LIMIT 1",
                    (shortcode,),
                ).fetchone()
                if row and row["frames_used"] and row["frames_used"] > fr["cap"]:
                    at_risk.append((shortcode, n, row["frames_used"]))
            if at_risk:
                detail = ", ".join(
                    f"{s} ({n} entities, {f} frames)" for s, n, f in at_risk
                )
                alerts.append(
                    f"the {fr['cap']}-frame cap is clipping reels that carry entities "
                    f"only the OCR attests: {detail}. "
                    "Raise REELS_OCR_MAX_FRAMES or accept the loss."
                )
        if src["ocr_only"] == 0:
            alerts.append(
                "no entity is attested by the OCR alone: the step may no longer be "
                "earning its place on this corpus."
            )

    out.append("")
    if alerts:
        out.append("  ALERTS")
        out += [f"    ! {a}" for a in alerts]
    else:
        out.append("  no alert: the thresholds hold on this corpus.")
    out.append("")
    return out

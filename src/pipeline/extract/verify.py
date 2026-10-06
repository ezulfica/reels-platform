"""Verification pass.

A second LLM call confronts each extracted entity with the source text and
rejects it when it is not genuinely attested there. Direct motive: the first
extraction promoted unreadable OCR noise (`FKAFFETJ`, `FIFHILAN`) into entities
with a plausible-sounding `why_saved` — the most dangerous kind of error, because
it cannot be spotted by reading without going back to the source.

Deliberately a separate call, not a re-read by the same prompt: a model that has
just invented an entity is unlikely to correct itself in the same breath. A fresh
call whose only job is to verify has no investment in defending its own answer.
"""

from __future__ import annotations

import sqlite3
import sys

from pydantic import BaseModel, Field

import inference as llm
from .extract import build_context
from .heuristics import is_attested, sources_of

SYSTEM_PROMPT = """You check whether an entity extracted from an Instagram reel \
is genuinely attested in the source text provided.

attested = true only if the entity name (or an obvious spelling variant) appears \
clearly in the caption, the audio transcript, or a readable OCR passage of the \
source text.

The audio transcript garbles the spelling of foreign proper nouns: it writes what \
it hears. A spelling phonetically close to the name being checked, in a context \
that unambiguously designates the same place, counts as attested (e.g. a Japanese \
temple name transcribed by ear). This tolerance applies to audio only — not to \
rescue an incoherent OCR fragment.

attested = false if:
- the name comes only from an unreadable or incoherent OCR fragment (a run of \
meaningless characters, truncated words mixed with garbage);
- the name is invented or guessed, however plausible it sounds;
- the name is in fact a generic word (the type itself, a category) rather than a \
specific proper noun.

Be strict: when genuinely in doubt, attested = false. WRITE `note` IN FRENCH, in \
one short sentence — quote the source passage if attested=true, or say what the \
doubt rests on if attested=false."""


class Verification(BaseModel):
    attested: bool
    note: str = Field(max_length=200)


def verify_one(
    client,
    context: str,
    name: str,
    type_: str,
    why_saved: str,
    model: str | None = None,
) -> Verification:
    prompt = (
        f"Source text:\n{context}\n\n"
        f'Entity to check: name="{name}", type={type_}, why_saved="{why_saved}"'
    )
    raw = llm.chat_json(
        client,
        model or llm.model("verify"),
        SYSTEM_PROMPT,
        prompt,
        Verification.model_json_schema(),
        # Colder than extraction: checking an attestation calls for a stable
        # answer, not variety.
        temperature=0.1,
    )
    return Verification.model_validate_json(raw)


def run(
    conn: sqlite3.Connection, limit: int | None = None, model: str | None = None
) -> dict[str, int]:
    """Verifies every candidate not yet verified.

    This used to need a scope, and a convoluted one: while several extractions of
    the same reel coexisted, `verified IS NULL` also caught the candidates of
    comparison runs — 1074 of them in the database after one session of
    measurements — and, being older, they came FIRST. A `reels verify` launched
    after a fresh extraction set off on a thousand LLM calls before touching what
    it had been asked for. With one extraction per reel there is nothing left to
    scope: the candidates that exist are the candidates in use.

    Hand-entered candidates never appear here: `correct.py` writes them with
    verified=1: a human does not need a fact-check against the source text."""
    model = llm.model("verify", model)

    todo = conn.execute(
        """
        SELECT c.id, c.name, c.type, c.why_saved, c.shortcode, c.evidence_json,
               c.evidence_status, c.evidence_note
        FROM candidate c
        JOIN reel r ON r.shortcode = c.shortcode
        WHERE c.verified IS NULL AND r.unsaved_at IS NULL
        ORDER BY c.id
        """
        + (" LIMIT ?" if limit else ""),
        (limit,) if limit else (),
    ).fetchall()

    stats = {
        "attested": 0,
        "rejected": 0,
        "overridden": 0,
        "deterministic": 0,
        "evidence_rejected": 0,
        "failed": 0,
    }
    if not todo:
        return stats

    client = None
    context_cache: dict[str, str] = {}
    source_cache: dict[str, str] = {}

    def sources(shortcode: str) -> str:
        if shortcode not in source_cache:
            source_cache[shortcode] = sources_of(conn, shortcode)
        return source_cache[shortcode]

    try:
        for i, row in enumerate(todo, 1):
            shortcode = row["shortcode"]
            # Fresh extractions already carry a deterministic evidence judgement.
            # Rows created before migration 016 deliberately keep the old verifier
            # path: retrospectively calling their absent evidence "invalid" would
            # silently empty the current catalogue before they are re-extracted.
            evidence_status = row["evidence_status"]
            if evidence_status in ("missing", "invalid"):
                note = row["evidence_note"] or "evidence needs human review"
                conn.execute(
                    "UPDATE candidate SET verified = 0, verification_note = ? WHERE id = ?",
                    (f"evidence: {note}", row["id"]),
                )
                conn.commit()
                stats["rejected"] += 1
                stats["evidence_rejected"] += 1
                print(
                    f"  [{i}/{len(todo)}] {shortcode} {row['name']!r} "
                    f"REJECTED — evidence: {note}",
                    file=sys.stderr,
                )
                continue
            if evidence_status == "literal":
                conn.execute(
                    "UPDATE candidate SET verified = 1, verification_note = ? WHERE id = ?",
                    ("kept: literal evidence validated locally", row["id"]),
                )
                conn.commit()
                stats["attested"] += 1
                stats["deterministic"] += 1
                print(
                    f"  [{i}/{len(todo)}] {shortcode} {row['name']!r} "
                    "attested — literal evidence",
                    file=sys.stderr,
                )
                continue
            if shortcode not in context_cache:
                context_cache[shortcode] = build_context(conn, shortcode)
            context = context_cache[shortcode]

            try:
                if client is None:
                    client = llm.client()
                result = verify_one(
                    client, context, row["name"], row["type"], row["why_saved"], model
                )
            except Exception as error:  # noqa: BLE001
                stats["failed"] += 1
                print(
                    f"  [{i}/{len(todo)}] #{row['id']} {row['name']!r} FAILED — {error}",
                    file=sys.stderr,
                )
                continue

            # A rejection is only allowed on a name that is NOT literally in the
            # source. Measured over two runs: 6 of verify's 11 rejections named
            # something plainly present — `Soul Eater` and `Fruits Basket`, both
            # listed in their reel's caption, `E FORTUNES 百樂逢` in the OCR —
            # while the note asserted it was absent. Five valid entities were
            # destroyed by the last pass alone.
            #
            # The asymmetry is what makes this safe: `is_attested` returning True
            # is a FACT (the string is there), where returning False is only a
            # hint, since a transcript spells foreign names by ear. So we override
            # the model in the one direction where the free test cannot be wrong,
            # and leave it sovereign in the other — which is the direction it was
            # written for, the plausible name that is simply absent.
            attested = result.attested
            note = result.note
            if not attested and is_attested(sources(shortcode), row["name"]):
                attested = True
                note = "kept: the name is literally present in the source"
                stats["overridden"] += 1

            conn.execute(
                "UPDATE candidate SET verified = ?, verification_note = ? WHERE id = ?",
                (int(attested), note, row["id"]),
            )
            conn.commit()

            if attested:
                stats["attested"] += 1
                flag = "attested"
            else:
                stats["rejected"] += 1
                flag = "REJECTED"
            print(
                f"  [{i}/{len(todo)}] {shortcode} {row['name']!r} ({row['type']}) "
                f"{flag} — {note}",
                file=sys.stderr,
            )
    finally:
        if client is not None:
            llm.unload(client, model)

    return stats

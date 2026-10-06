"""Explainable, local-first name canonicalization for extracted candidates.

Extraction owns the observed mention. This module records a separate decision and
never rewrites that observation. It deliberately has no fuzzy-string fallback:
phonetic resemblance alone is not enough to rename a place.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from dataclasses import dataclass
from typing import Protocol

from storage.database import now


SOURCE_PRIORITY = {"caption": 4, "account": 3, "ocr": 2, "transcript": 1}
SOURCE_CONFIDENCE = {"caption": 0.99, "account": 0.98, "ocr": 0.95, "transcript": 0.60}


class PlaceAuthorityAdapter(Protocol):
    """Future seam for an explicitly selected external place authority.

    No implementation is configured by default. An adapter must return a
    suggestion with provenance; it cannot make a low-confidence rename silently.
    """

    def suggest(
        self, observed_name: str, *, locality: str | None = None
    ) -> "AuthoritySuggestion | None": ...


@dataclass(frozen=True)
class AuthoritySuggestion:
    canonical_name: str
    authority: str
    confidence: float
    reference: str | None = None


@dataclass(frozen=True)
class Decision:
    observed_name: str
    evidence: tuple[dict, ...]
    canonical_name: str | None
    confidence: float
    status: str
    method: str
    explanation: str


def normalised_key(value: str) -> str:
    """Return a deterministic lookup key without changing the observed name.

    This is deliberately not fuzzy matching and never translates a proper name.
    It makes equivalent formatting (case, accents, punctuation and whitespace)
    share a key while preserving non-Latin scripts for multilingual reels.
    ``name`` and ``name_latin`` remain the display/evidence fields.
    """
    value = unicodedata.normalize("NFKD", str(value)).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^\w]+", " ", value, flags=re.UNICODE).split())


def _key(value: str) -> str:
    """Backward-compatible private alias used by the resolution rules."""
    return normalised_key(value)


def _contains_name(text: str, name: str) -> bool:
    """Match complete normalized word sequences, never edit-distance guesses."""
    needle = _key(name)
    haystack = _key(text)
    if not needle:
        return False
    return f" {needle} " in f" {haystack} "


def _repeated_brand_before_home_shopping(
    name: str, entity_type: str | None
) -> str | None:
    """Strip the exact retail descriptor OCR appends after a repeated brand.

    This narrow local rule covers `HAY HAY home shopping`: the two identical
    uppercase tokens form the brand and the trailing phrase is a category label.
    It does not apply to arbitrary extra words or to non-shop entities.
    """
    parts = name.split()
    if (
        entity_type == "shop"
        and len(parts) == 4
        and parts[0].isupper()
        and parts[0] == parts[1]
        and "".join(c for c in parts[0] if c.isalpha())
        and " ".join(parts[2:]).casefold() == "home shopping"
    ):
        return " ".join(parts[:2])
    return None


def decide(
    observed_name: str,
    evidence: list[dict] | tuple[dict, ...],
    local_names: list[str] | tuple[str, ...] = (),
    *,
    entity_type: str | None = None,
    authority: PlaceAuthorityAdapter | None = None,
    locality: str | None = None,
) -> Decision:
    """Choose only an explicitly attested spelling; otherwise preserve/abstain.

    `local_names` is a bounded set of already-known candidate/catalogue spellings.
    A known name must occur in a caption, account, or OCR quote to correct an
    ASR-derived observation. When evidence itself literally attests the observed
    spelling, it is retained as the canonical spelling with a traceable decision.
    """
    clean_evidence = tuple(dict(item) for item in evidence if isinstance(item, dict))
    ranked = sorted(
        clean_evidence,
        key=lambda item: SOURCE_PRIORITY.get(item.get("source", ""), 0),
        reverse=True,
    )
    strong = [
        item for item in ranked if SOURCE_PRIORITY.get(item.get("source", ""), 0) >= 2
    ]

    exact = next(
        (
            item
            for item in ranked
            if _contains_name(str(item.get("quote", "")), observed_name)
        ),
        None,
    )
    repeated_brand = _repeated_brand_before_home_shopping(observed_name, entity_type)
    if (
        repeated_brand
        and exact
        and SOURCE_PRIORITY.get(exact.get("source", ""), 0) >= 2
    ):
        return Decision(
            observed_name,
            clean_evidence,
            repeated_brand,
            0.96,
            "resolved",
            "repeated_brand_before_home_shopping",
            f"Les deux tokens majuscules répétés forment {repeated_brand!r}; "
            "le suffixe OCR `home shopping` est un descripteur de catégorie. "
            "Nom observé et preuve conservés.",
        )
    if exact and SOURCE_PRIORITY.get(exact.get("source", ""), 0) >= 2:
        source = exact.get("source", "")
        return Decision(
            observed_name,
            clean_evidence,
            observed_name,
            SOURCE_CONFIDENCE.get(source, 0.9),
            "resolved",
            "literal_priority_evidence",
            f"Nom observé attesté littéralement par {source}.",
        )

    # A stronger source may spell the entity differently, but only an already
    # available name that appears as a complete phrase can win.
    matching_names = {}
    for name in sorted(
        (n.strip() for n in local_names if n),
        key=lambda value: (len(value), value.casefold(), value),
    ):
        if any(_contains_name(str(item.get("quote", "")), name) for item in strong):
            matching_names.setdefault(_key(name), name)
    if len(matching_names) == 1:
        canonical = next(iter(matching_names.values()))
        winner = next(
            item
            for item in strong
            if _contains_name(str(item.get("quote", "")), canonical)
        )
        return Decision(
            observed_name,
            clean_evidence,
            canonical,
            SOURCE_CONFIDENCE.get(winner.get("source", ""), 0.9),
            "resolved",
            "known_name_in_priority_evidence",
            f"La forme canonique {canonical!r} est attestée dans la source "
            f"prioritaire {winner.get('source')}; nom observé conservé séparément.",
        )
    if len(matching_names) > 1:
        return Decision(
            observed_name,
            clean_evidence,
            None,
            0.0,
            "abstained",
            "ambiguous_local_names",
            "Plusieurs noms locaux sont attestés par les preuves prioritaires.",
        )

    # The future adapter is opt-in and place-only. No adapter is configured by
    # the production local path, so this branch cannot create a network call.
    if authority is not None and entity_type in {
        "place",
        "restaurant",
        "lodging",
        "shop",
    }:
        suggestion = authority.suggest(observed_name, locality=locality)
        if suggestion is not None:
            score = suggestion.confidence
            authority_evidence = (
                *clean_evidence,
                {
                    "source": f"authority:{suggestion.authority}",
                    "quote": suggestion.canonical_name,
                    "reference": suggestion.reference,
                },
            )
            if 0.95 <= score <= 1.0:
                return Decision(
                    observed_name,
                    authority_evidence,
                    suggestion.canonical_name,
                    score,
                    "resolved",
                    f"authority:{suggestion.authority}",
                    f"L’autorité {suggestion.authority} propose "
                    f"{suggestion.canonical_name!r} (confiance "
                    f"{score:.2f}); preuve conservée séparément.",
                )
            return Decision(
                observed_name,
                authority_evidence,
                None,
                score if 0 <= score <= 1 else 0.0,
                "abstained",
                f"low_confidence_authority:{suggestion.authority}",
                f"Suggestion de {suggestion.authority} trop incertaine "
                f"({score:.2f}); nom observé conservé.",
            )

    if exact:
        return Decision(
            observed_name,
            clean_evidence,
            None,
            SOURCE_CONFIDENCE.get(exact.get("source", ""), 0.6),
            "abstained",
            "asr_only",
            "Seule l’ASR atteste cette forme; canonicalisation sans autorité suspendue.",
        )

    return Decision(
        observed_name,
        clean_evidence,
        None,
        0.0,
        "abstained",
        "no_attested_canonical_name",
        "Aucune preuve prioritaire n’atteste un nom canonique local; nom observé conservé.",
    )


def decide_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    authority: PlaceAuthorityAdapter | None = None,
) -> Decision:
    """Evaluate a stored candidate against its reel and existing local names."""
    row = conn.execute(
        "SELECT c.id, c.name, c.type, c.locality, c.evidence_json, c.shortcode, r.username "
        "FROM candidate c JOIN reel r ON r.shortcode=c.shortcode WHERE c.id=?",
        (candidate_id,),
    ).fetchone()
    if not row:
        raise ValueError(f"unknown candidate id {candidate_id}")
    try:
        evidence = json.loads(row["evidence_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        evidence = []

    # The author's account is a high-quality spelling source only when the
    # observed name is itself the handle; unrelated authors are not evidence.
    username = (row["username"] or "").strip().lstrip("@")
    if username and _key(username) == _key(row["name"]):
        evidence.append(
            {"source": "account", "quote": username, "match": "literal", "location": ""}
        )

    local_names = [
        r["name"]
        for r in conn.execute(
            "SELECT name FROM candidate WHERE type=? AND id<>? "
            "UNION SELECT canonical_name AS name FROM entity WHERE type=? "
            "UNION SELECT e.canonical_name AS name FROM entity_alias a "
            "JOIN entity e ON e.id=a.entity_id WHERE e.type=?",
            (row["type"], candidate_id, row["type"], row["type"]),
        )
        if r["name"]
    ]

    # Re-read the local written sources instead of trusting Qwen's chosen
    # evidence label. This catches a caption/OCR spelling that Qwen paired with
    # the phonetic transcript, while only attaching a text as evidence when it
    # literally contains the observed or a known local name.
    source_rows = conn.execute(
        "SELECT r.caption, "
        "(SELECT text FROM screen_text WHERE shortcode=r.shortcode "
        " ORDER BY created_at DESC, rowid DESC LIMIT 1) AS ocr_text, "
        "(SELECT text FROM transcript WHERE shortcode=r.shortcode "
        " ORDER BY created_at DESC, rowid DESC LIMIT 1) AS transcript_text "
        "FROM reel r WHERE r.shortcode=?",
        (row["shortcode"],),
    ).fetchone()
    if source_rows:
        known_names = [row["name"], *local_names]
        existing = {
            (item.get("source"), item.get("quote"))
            for item in evidence
            if isinstance(item, dict)
        }
        for source, field in (
            ("caption", "caption"),
            ("ocr", "ocr_text"),
            ("transcript", "transcript_text"),
        ):
            quote = (source_rows[field] or "").strip()
            if (
                quote
                and (source, quote) not in existing
                and any(_contains_name(quote, name) for name in known_names)
            ):
                evidence.append(
                    {
                        "source": source,
                        "quote": quote,
                        "match": "literal",
                        "location": "",
                    }
                )

    return decide(
        row["name"],
        evidence,
        local_names,
        entity_type=row["type"],
        authority=authority,
        locality=row["locality"],
    )


def record_candidate(
    conn: sqlite3.Connection,
    candidate_id: int,
    *,
    authority: PlaceAuthorityAdapter | None = None,
) -> Decision:
    """Persist an auditable decision for a newly extracted candidate."""
    decision = decide_candidate(conn, candidate_id, authority=authority)
    conn.execute(
        "INSERT INTO candidate_name_resolution "
        "(candidate_id, observed_name, evidence_json, canonical_name, confidence, "
        "status, method, explanation, decided_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(candidate_id) DO UPDATE SET observed_name=excluded.observed_name, "
        "evidence_json=excluded.evidence_json, canonical_name=excluded.canonical_name, "
        "confidence=excluded.confidence, status=excluded.status, method=excluded.method, "
        "explanation=excluded.explanation, decided_at=excluded.decided_at",
        (
            candidate_id,
            decision.observed_name,
            json.dumps(decision.evidence, ensure_ascii=False),
            decision.canonical_name,
            decision.confidence,
            decision.status,
            decision.method,
            decision.explanation,
            now(),
        ),
    )
    return decision

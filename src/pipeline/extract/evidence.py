"""Source-grounded evidence validation and deterministic extraction guards."""

from __future__ import annotations

import json
import re
import sqlite3

from .contracts import Candidate, Evidence


def _normalised_text(value: str) -> str:
    """Normalise formatting noise without pretending a spelling is present."""
    from domain.canonicalization import normalised_key

    return normalised_key(value)


def _evidence_sources(conn: sqlite3.Connection, shortcode: str) -> dict[str, object]:
    """Exact source partitions behind an evidence citation.

    Validation must not search the rendered dossier: a phrase repeated in OCR and
    caption would otherwise make a wrong `source` look valid.  OCR frame labels
    stay addressable even though its aggregate text remains the compatibility
    representation.
    """
    reel = conn.execute(
        "SELECT caption FROM reel WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    transcript = conn.execute(
        "SELECT text FROM transcript WHERE shortcode = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (shortcode,),
    ).fetchone()
    ocr = conn.execute(
        "SELECT text, observations_json FROM screen_text WHERE shortcode = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (shortcode,),
    ).fetchone()
    observations = json.loads(ocr["observations_json"] or "[]") if ocr else []
    return {
        "caption": reel["caption"] if reel and reel["caption"] else "",
        "transcript": transcript["text"] if transcript and transcript["text"] else "",
        "ocr": ocr["text"] if ocr and ocr["text"] else "",
        "ocr_frames": {
            str(item.get("frame", "")): str(item.get("text", ""))
            for item in observations
            if isinstance(item, dict)
        },
    }


def _repair_literal_evidence(
    entity: Candidate, sources: dict[str, object]
) -> Evidence | None:
    """Recover an exact citation when the model chose the wrong source label.

    This is deliberately narrower than fuzzy matching: we repair only a name that
    is literally present in one source partition.  It saves a real entity when
    The extractor writes a correct name and quote but labels its source `ocr` instead of
    `caption`; it cannot turn an inferred entity into an attested one.
    """
    name = _normalised_text(entity.name)
    for source_name in ("caption", "transcript", "ocr"):
        source = str(sources[source_name] or "")
        if name not in _normalised_text(source):
            continue
        location = ""
        if source_name == "ocr":
            for frame, text in sources["ocr_frames"].items():  # type: ignore[union-attr]
                if name in _normalised_text(str(text)):
                    location = str(frame)
                    break
        return Evidence(
            source=source_name, quote=entity.name, match="literal", location=location
        )
    return None


_ARTICLE_TOKENS = frozenset({"le", "la", "les", "un", "une", "des", "du", "de", "d"})


def _article_normalised_tokens(value: str) -> list[str]:
    """Tokens for a deliberately narrow, review-only name-variant signal."""
    return [
        token
        for token in re.findall(r"\w+", _normalised_text(value))
        if token not in _ARTICLE_TOKENS
    ]


def _near_literal_source(entity: Candidate, sources: dict[str, object]) -> str | None:
    """Find a source whose name differs only by omitted articles.

    This does not accept a candidate.  It chooses the targeted verifier path: the
    difference between `Pertes de Valserine` and `Les Pertes de la Valserine` is
    too small to discard a place, but too real to call a literal match.
    """
    candidate = _article_normalised_tokens(entity.name)
    if len(candidate) < 2:
        return None
    for source_name in ("caption", "transcript", "ocr"):
        source = _article_normalised_tokens(str(sources[source_name] or ""))
        width = len(candidate)
        if any(
            source[i : i + width] == candidate for i in range(len(source) - width + 1)
        ):
            return source_name
    return None


def _assess_evidence(
    conn: sqlite3.Connection, shortcode: str, entity: Candidate
) -> tuple[str, str | None, list[Evidence]]:
    """Validate each citation and retain only the ones that are grounded.

    `literal` is a fact that can be established locally.  A phonetic transcript
    match is useful provenance but remains an ambiguity for the targeted verifier
    or a human, never a fabricated literal proof.
    """
    evidence = entity.evidence
    if not evidence:
        return "missing", "no evidence was supplied", []
    sources = _evidence_sources(conn, shortcode)
    name = _normalised_text(entity.name)
    has_literal = False
    valid: list[Evidence] = []
    issues: list[str] = []
    for item in evidence:
        if item.match == "phonetic" and item.source != "transcript":
            issues.append("a phonetic match is only valid for a transcript")
            continue
        if item.source == "ocr" and item.location:
            source = sources["ocr_frames"].get(item.location)  # type: ignore[index,union-attr]
            if source is None:
                # OCR rows from before migration 014 only have the aggregate
                # text.  They cannot substantiate a frame label, but they can
                # still substantiate the citation itself; forcing a full OCR
                # rerun merely to retain an already-readable literal name would
                # turn a provenance improvement into a recall regression.
                if sources["ocr_frames"]:
                    issues.append(f"unknown OCR frame {item.location!r}")
                    continue
                source = sources["ocr"]
                item = item.model_copy(update={"location": ""})
        else:
            if item.source != "ocr" and item.location:
                issues.append("only OCR evidence may carry a frame location")
                continue
            source = sources[item.source]  # type: ignore[index]
            if item.source == "ocr" and sources["ocr_frames"] and not item.location:
                issues.append("OCR evidence needs its frame location")
                continue
        quote = _normalised_text(item.quote)
        if not quote or quote not in _normalised_text(str(source or "")):
            issues.append(f"quote not found in declared {item.source} source")
            continue
        if item.match == "literal":
            if name not in quote:
                issues.append("a literal quote does not contain the entity name")
                continue
            has_literal = True
        valid.append(item)
    repaired = _repair_literal_evidence(entity, sources)
    if not valid and repaired is not None:
        return "literal", "repaired a malformed model citation", [repaired]
    if not valid:
        near_source = _near_literal_source(entity, sources)
        if near_source:
            return (
                "variant",
                (
                    "near-literal name variant in "
                    + near_source
                    + "; needs targeted verification"
                ),
                [],
            )
        return "invalid", issues[0] if issues else "no valid evidence", []
    note = (
        f"discarded {len(issues)} unsupported evidence citation(s)" if issues else None
    )
    if repaired is not None and not has_literal:
        valid.append(repaired)
        return "literal", "repaired a malformed model citation", valid
    if has_literal:
        return "literal", note, valid
    return "phonetic", note or "transcript spelling needs review", valid


def validate_evidence(
    conn: sqlite3.Connection, shortcode: str, entity: Candidate
) -> tuple[str, str | None]:
    """Public status helper used by tests and one-off diagnostics."""
    status, note, _ = _assess_evidence(conn, shortcode, entity)
    return status, note


# A price or quantity can be literally present in the audio or the OCR — so the
# verification pass (verify.py) will let it through, since its job is to check
# attestation, not whether the entity type is appropriate. Seen on the Saizeriya
# reel despite the prompt's instruction: "1,000 yen" extracted as a product. A
# deterministic rule-based guard is more reliable than hoping an 8B applies an
# instruction on every draw (temperature > 0 => variance from run to run, already
# observed on that same reel).
_PRICE_OR_QTY = re.compile(
    r"^[\d.,\s]+(?:€|\$|¥|yens?|euros?|dollars?|ml|cl|l|kg|g|%)?$", re.IGNORECASE
)


def _is_price_or_qty(name: str) -> bool:
    return bool(_PRICE_OR_QTY.match(name.strip()))


def _attested(value: str | None, context: str) -> str | None:
    """Return the value if the source text carries it, None otherwise.

    Serves `locality` (since v9) and `name_latin` (since v10): the field changes,
    the risk does not — the model fills a field with something plausible rather
    than leaving it empty.

    Measured on `address`: making it required in the schema took it from 0% to 18%
    of locatable entities, but 2 of the 14 addresses produced were invented, and in
    the worst way — "12 Rue de Lancry" copied onto a MUJI in Taipei.

    Measured again on `name_latin`, on the very first v11-era reel: the prompt says
    "never romanise or translate it yourself", and the model still returned
    `Kome Koro! Ichipushi Rice Washing Bowl` for `こめコレ！いち押し米とぎボウル` —
    a romanisation invented end to end, and wrong along the way (`Ichipushi` for
    いち押し, which reads ichioshi). The same reel gave `Iidaya` for `飯田屋`,
    which IS present in the source: the rule keeps the second and discards the
    first, which no instruction achieved.

    `verify.py` does not catch either case: it confronts the entity's NAME with the
    source, never its fields. A false value would travel all the way to the
    catalogue fiche and to the Maps link with nothing to flag it.

    A rule is preferable to an instruction here because it is exactly checkable,
    unlike "never invent", which an 8B applies hit and miss. Same reason to exist
    as _is_price_or_qty. The criterion is deliberately tolerant — half the
    significant fragments is enough — because a locality is often reworded
    ("Paris 3" for "📍 Paris 3eme") without being invented for that."""
    value = (value or "").strip()
    if not value:
        return None
    source = context.lower()
    frags = [f for f in value.lower().replace(",", " ").split() if len(f) > 3]
    if not frags:
        return value if value.lower() in source else None
    attested = sum(1 for f in frags if f in source)
    return value if attested >= max(1, len(frags) // 2) else None

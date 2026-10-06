"""LLM transform: understand why a reel was saved, classify it, get 0..N entities
out of it. Runs locally on Ollama, through `inference`.

The collection never enters the context dossier: the model has to learn to do
without the user's manual filing, since future reels will not have it.

The context dossier is now purely textual: caption, @tagged accounts, hashtags,
audio transcript, on-screen text (OCR). The vision step that used to feed it has
been removed — measured over the corpus's 296 verified entities: the VLM's output
attested only 3 of them on its own (1%), all generic common nouns the prompt
rejects anyway.

The extraction contract and quality protocol are documented in
`docs/extraction-quality.md`.
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
import time
import unicodedata
from collections.abc import Collection

from pydantic import BaseModel

import inference as llm
from domain import repertoire
from storage.database import code_sha, mark, now

from .contracts import (
    FACETS,
    FACETS_BY_FAMILY,
    FAMILIES_BY_TYPE,
    FAMILY_OF_FACET,
    MAX_FACETS,
    PLACE_TYPES,
    SCALES,  # noqa: F401 - compatibility re-export
    TYPES,  # noqa: F401 - compatibility re-export
    Candidate,
    ContentIndexResult,
    DiscoveryResult,
    EnrichedEntity,
    EnrichmentResult,
    Evidence,  # noqa: F401 - compatibility re-export
    ExtractionResult,
    FicheResult,
    Mention,
    RecipeCard,  # noqa: F401 - compatibility re-export
)
from .evidence import (
    _assess_evidence,
    _attested,
    _is_price_or_qty,
    _normalised_text,
    validate_evidence,  # noqa: F401 - compatibility re-export
)
from .prompts import (
    CONTENT_INDEX_SYSTEM_PROMPT,
    DISCOVERY_SYSTEM_PROMPT,
    ENRICHMENT_SYSTEM_PROMPT,
    FICHE_SYSTEM_PROMPT,
    context_fingerprint,
    prompt_fingerprint,
)

# Default local extraction model; configuration may override it.
DEFAULT_MODEL = "qwen3:8b"


def _parse_enrichment(raw: str) -> EnrichmentResult:
    """Validate enrichment while preserving useful open-vocabulary suggestions."""
    payload = json.loads(raw)
    moved: list[str] = []
    for entity in payload.get("entities", []):
        facets = entity.get("facets") or []
        valid = [value for value in facets if value in FACETS]
        unknown = [value for value in facets if value not in FACETS]
        if not unknown:
            continue
        entity["facets"] = valid
        tags = list(entity.get("tags") or [])
        for value in unknown:
            if isinstance(value, str) and value and value not in tags:
                tags.append(value)
                moved.append(value)
        entity["tags"] = tags
    if moved:
        print(
            "  WARNING: unknown facets moved to tags: " + ", ".join(sorted(set(moved))),
            file=sys.stderr,
        )
    return EnrichmentResult.model_validate(payload)


def build_context(conn: sqlite3.Connection, shortcode: str) -> str:
    """The per-reel dossier: everything available without manual action.
    Deliberately WITHOUT saved_collection_ids — the model has to learn to do
    without the user's filing, since future reels will not have it. (The
    catalogue does use collections, but as structured data at resolution time,
    which is a different matter: nothing is left for the LLM to infer.)"""
    reel = conn.execute(
        "SELECT username, caption FROM reel WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    # accessibility_caption is deliberately NOT read: measured over the corpus,
    # Meta only generates one for photo posts. It is empty on 100% of video reels
    # (0/60 downloaded), and only 5 reels out of 1347 carry one with a real
    # description. The field stays in the database, but injecting it here only
    # lengthened the prompt for nothing.
    ctx = conn.execute(
        "SELECT hashtags, mentions, has_list_marker"
        " FROM reel_context WHERE shortcode = ?",
        (shortcode,),
    ).fetchone()
    # ORDER BY created_at DESC LIMIT 1: several tool_versions can coexist for one
    # reel (a new OCR pass does not erase the old one) — without this ordering the
    # returned row would be arbitrary.
    asr = conn.execute(
        "SELECT text, has_speech FROM transcript WHERE shortcode = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (shortcode,),
    ).fetchone()
    ocr = conn.execute(
        "SELECT text, observations_json FROM screen_text WHERE shortcode = ?"
        " ORDER BY created_at DESC, rowid DESC LIMIT 1",
        (shortcode,),
    ).fetchone()

    parts = [
        f"Author account: @{reel['username']}" if reel and reel["username"] else ""
    ]
    if reel and reel["caption"]:
        parts.append(f"Caption: {reel['caption']}")
    if ctx:
        hashtags = json.loads(ctx["hashtags"] or "[]")
        if hashtags:
            parts.append(f"Hashtags: {', '.join(hashtags)}")
        # An @tagged account in the caption often designates the reel's place or
        # product directly (e.g. "@thesomafamily 📍 Paris 3" = the restaurant
        # itself) — already parsed into reel_context.mentions but never passed
        # through to here, which made the model fabricate "restaurant japonais"
        # instead of using the name.
        mentions = json.loads(ctx["mentions"] or "[]")
        if mentions:
            parts.append(
                f"Accounts @tagged in the caption (possibly the place or "
                f"product itself): {', '.join('@' + m for m in mentions)}"
            )
        if ctx["has_list_marker"]:
            parts.append(
                "NOTE: this reel seems to list several places or items "
                "(list marker detected in the caption)."
            )
    if asr and asr["has_speech"] and asr["text"]:
        parts.append(f"Audio transcript: {asr['text']}")
    elif asr and not asr["has_speech"]:
        parts.append("(no voice-over)")
    # OCR comes last and stays flagged as noisy. It is nevertheless the most
    # valuable source after the caption: 45 of the corpus's 296 verified entities
    # (15%) are attested by it alone — the "silent list reel" kind, where names are
    # burned into the image, never spoken, absent from the caption.
    if ocr and ocr["text"]:
        observations = json.loads(ocr["observations_json"] or "[]")
        if observations:
            ocr_lines = "\n".join(
                f"[{item['frame']}] {item['text']}" for item in observations
            )
            parts.append(
                "On-screen text (raw OCR, possibly noisy; frame markers "
                f"are evidence locations):\n{ocr_lines}"
            )
        else:
            parts.append(f"On-screen text (raw OCR, possibly noisy): {ocr['text']}")

    return "\n\n".join(p for p in parts if p)


def _chat(client, model: str, system: str, user: str, schema: type[BaseModel]) -> str:
    return llm.chat_json(client, model, system, user, schema.model_json_schema())


def _enrichment_prompt(context: str, mentions: list[Mention]) -> str:
    supplied = [{"name": item.name, "type": item.type} for item in mentions]
    return f"Source dossier:\n{context}\n\nFixed names to enrich:\n{json.dumps(supplied, ensure_ascii=False)}"


def _dedupe_mentions(mentions: list[Mention]) -> list[Mention]:
    """Remove exact normalized repeats before spending enrichment tokens.

    Discovery may mention a name once per transcript/OCR occurrence. Repeating
    it in the enrichment input cannot improve recall: enrichment is forbidden to
    invent names and the first evidence-bearing mention is sufficient. Similar
    but distinct names intentionally remain separate for later retrieval.
    """
    from domain.canonicalization import normalised_key

    seen: set[tuple[str, str]] = set()
    kept: list[Mention] = []
    for mention in mentions:
        key = (mention.type, normalised_key(mention.name))
        if not key[1] or key in seen:
            continue
        seen.add(key)
        kept.append(mention)
    return kept


def _combine(
    fiche: FicheResult,
    discovery: DiscoveryResult,
    enrichment: EnrichmentResult,
    content_index: ContentIndexResult | None = None,
) -> ExtractionResult:
    """Join on discovery names; enrichment is never allowed to change recall."""
    by_name = {_normalised_text(item.name): item for item in enrichment.entities}
    entities: list[Candidate] = []
    for mention in discovery.mentions:
        if not mention.direct:
            continue
        # A recipe is itself indexed in `recipes`, not emitted as a catalogue
        # entity.  Product names found only on a package frame are ingredients or
        # marketing copy, not a recommendation to buy; preserving them produced
        # "No preservatives" and "No need refrigerat" as products on DQTz… . A
        # caption/transcript-backed product may still be deliberately recommended.
        if fiche.content_kind == "recipe":
            if mention.type in {"recipe", "method", "exercise"}:
                continue
            if all(item.source == "ocr" for item in mention.evidence):
                continue
        detail = by_name.get(_normalised_text(mention.name))
        if detail is None:
            # A missing description must not erase a name discovered with proof.
            detail = EnrichedEntity(
                name=mention.name,
                facets=[],
                scale="",
                city="",
                country="",
                locality="",
                highlights=[],
                tags=[],
                why_saved=fiche.why_saved,
                confidence=fiche.confidence,
            )
        entities.append(
            Candidate(
                name=mention.name,
                name_latin=mention.name_latin,
                evidence=mention.evidence,
                type=mention.type,
                facets=detail.facets,
                scale=detail.scale,
                city=detail.city,
                country=detail.country,
                locality=detail.locality,
                brand=detail.brand,
                intention=detail.intention,
                highlights=detail.highlights,
                tags=detail.tags,
                why_saved=detail.why_saved,
                confidence=detail.confidence,
            )
        )
    recipes = content_index.recipes if content_index else []
    mode = "repertoire" if fiche.content_kind else fiche.mode
    topic = fiche.topic.strip()
    if (
        fiche.content_kind == "recipe"
        and topic.casefold() in {"repertoire", "recette", "recipe"}
        and recipes
    ):
        topic = recipes[0].dish_name
    return ExtractionResult(
        is_actionable=fiche.is_actionable,
        topic=topic,
        why_saved=fiche.why_saved,
        confidence=fiche.confidence,
        mode=mode,
        tags=fiche.tags,
        key_points=fiche.key_points,
        entities=entities,
        content_kind=fiche.content_kind,
        recipes=recipes,
    )


def extract_staged(
    client, context: str, model: str | None = None
) -> tuple[ExtractionResult, str]:
    """Run fiche -> name discovery -> enrichment and retain each raw completion."""
    llm.reset_usage()
    model_name = model or llm.model("extract")
    raw_fiche = _chat(client, model_name, FICHE_SYSTEM_PROMPT, context, FicheResult)
    fiche = FicheResult.model_validate_json(raw_fiche)
    if fiche.content_kind == "recipe":
        # Recipes are indexed by their dedicated contract below. A generic name
        # scan treated every package frame as a product and could consume the
        # whole output budget on redundant citations; no catalogue entity is
        # needed to make a recipe searchable.
        raw_discovery = json.dumps({"mentions": []})
        discovery = DiscoveryResult(mentions=[])
        raw_enrichment = json.dumps({"entities": []})
        enrichment = EnrichmentResult(entities=[])
    else:
        raw_discovery = _chat(
            client, model_name, DISCOVERY_SYSTEM_PROMPT, context, DiscoveryResult
        )
        discovery = DiscoveryResult.model_validate_json(raw_discovery)
        direct = _dedupe_mentions([item for item in discovery.mentions if item.direct])
        if direct:
            raw_enrichment = _chat(
                client,
                model_name,
                ENRICHMENT_SYSTEM_PROMPT,
                _enrichment_prompt(context, direct),
                EnrichmentResult,
            )
            enrichment = _parse_enrichment(raw_enrichment)
        else:
            raw_enrichment = json.dumps({"entities": []})
            enrichment = EnrichmentResult(entities=[])
    if fiche.content_kind == "recipe":
        raw_content_index = _chat(
            client, model_name, CONTENT_INDEX_SYSTEM_PROMPT, context, ContentIndexResult
        )
        content_index = ContentIndexResult.model_validate_json(raw_content_index)
    else:
        raw_content_index = json.dumps({"recipes": []})
        content_index = ContentIndexResult(recipes=[])
    result = _combine(fiche, discovery, enrichment, content_index)
    raw = json.dumps(
        {
            "fiche": json.loads(raw_fiche),
            "discovery": json.loads(raw_discovery),
            "enrichment": json.loads(raw_enrichment),
            "content_index": json.loads(raw_content_index),
        },
        ensure_ascii=False,
    )
    return result, raw


def parse_stored_response(raw: str) -> ExtractionResult:
    """Read both historic one-shot rows and the current staged trace.

    Guards can therefore still be replayed over either generation without a new
    LLM call.  `raw_response` is evidence, not an internal transport format to
    discard at the next prompt iteration.
    """
    payload = json.loads(raw)
    if not isinstance(payload, dict) or "fiche" not in payload:
        return ExtractionResult.model_validate(payload)
    fiche_payload = dict(payload["fiche"])
    # Traces produced by the first staged contract predate the repertoire index.
    fiche_payload.setdefault("content_kind", "")
    discovery_payload = dict(payload["discovery"])
    # v1 staged traces allowed an unbounded list of identical citations.  Keep
    # their first proof when replaying them through the tighter current contract.
    for mention in discovery_payload.get("mentions", []):
        if isinstance(mention, dict) and isinstance(mention.get("evidence"), list):
            mention["evidence"] = mention["evidence"][:1]
    return _combine(
        FicheResult.model_validate(fiche_payload),
        DiscoveryResult.model_validate(discovery_payload),
        EnrichmentResult.model_validate(payload["enrichment"]),
        ContentIndexResult.model_validate(
            payload.get("content_index", {"recipes": []})
        ),
    )


def extract_raw(client, context: str, model: str | None = None) -> str:
    """Compatibility helper: expose the staged result as one JSON document."""
    _, raw = extract_staged(client, context, model)
    return raw


def extract_one(client, context: str, model: str | None = None) -> ExtractionResult:
    result, _ = extract_staged(client, context, model)
    return result


# Two entities fused into a single `name` field, separated by a list character.
# Measured: `Da0PnbIzoLp` went from 7 entities (v4) to 1 (v9) because the model
# returned `郁郁YùYù | 赤峰氣味日常體驗室` — two Taipei shops in one string. That is
# not a missed extraction, it is a formatting fault, and both halves are valid: we
# split rather than reject.
#
# The hyphen and the colon are deliberately ABSENT from the list: they are part of
# real names (`Ay-Chung Flour-Rice Noodle`, `EPISODE149:DAAN`). Only separators no
# proper noun carries are included.
_SEPARATORS = re.compile(r"\s*[|/;]\s*|\s+·\s+")


def _split_names(name: str) -> list[str]:
    pieces = [m.strip() for m in _SEPARATORS.split(name)]
    pieces = [m for m in pieces if len(m) > 1]
    return pieces or [name.strip()]


def _useful_tags(tags: list[str], entity: Candidate) -> list[str]:
    """Drop tags that restate an already-structured field.

    The prompt forbids it explicitly; the model does it anyway. Measured on v9:
    20% of tags echoed a caption hashtag, 8% restated the city or country, 9% the
    name of an entity from the same reel — and on `Yoridokoro`, the only tag
    produced was `JAPON` while the fiche already carried `country=Japon`.

    Same doctrine as _is_price_or_qty, _scale_for_place and _attested:
    what is exactly checkable gets checked by rule. An 8B does not apply a
    prohibition every time — 30 non-places received a `scale` despite an
    unambiguous instruction.

    Strict equality is not enough: the first v10 reel returned `artisanat
    japonais` while `artisanat` was already a facet, and `boutique de cuisine`
    while the type was `shop` and `cuisine` a facet. So we compare WORDS, not
    strings — a tag sharing a significant word with an already-structured field
    says nothing new."""

    def words(value: str | None) -> set[str]:
        return {m for m in _norm_tag(value).split() if len(m) > 3}

    already: set[str] = set()
    for field in (
        entity.type,
        entity.city,
        entity.country,
        entity.name,
        entity.name_latin,
        *entity.facets,
    ):
        already |= words(field)

    seen: list[str] = []
    for tag in tags:
        normalised = _norm_tag(tag)
        if not normalised or normalised in seen or (words(tag) & already):
            continue
        seen.append(normalised)
    return seen[:3]


def _useful_reel_tags(tags: list[str]) -> list[str]:
    """Dedupe reel-level tags and cap them. Simpler than _useful_tags: there is
    no per-entity structured field to compare against at this level (type,
    city, facets... belong to entities, not to the reel), so this only removes
    exact repeats after normalisation and enforces the prompt's 0-5 ceiling."""
    seen: list[str] = []
    for tag in tags:
        normalised = _norm_tag(tag)
        if normalised and normalised not in seen:
            seen.append(normalised)
    return seen[:5]


def _useful_key_points(points: list[str]) -> list[str]:
    """Keep a compact, readable and non-duplicated fiche summary.

    This is deliberately a formatting guard, not an attempt to decide whether a
    culinary or training claim is true.  That judgement belongs to the source-
    grounded extraction prompt; here we only prevent a model from turning a
    short fiche into a repeated transcript fragment.
    """
    kept: list[str] = []
    seen: set[str] = set()
    for point in points:
        cleaned = " ".join(point.split()).strip()
        key = _normalised_text(cleaned)
        if not cleaned or key in seen:
            continue
        seen.add(key)
        kept.append(cleaned)
    return kept[:8]


def _sync_repertoire_fts(
    conn: sqlite3.Connection,
    shortcode: str,
    result: ExtractionResult,
    tags: list[str],
    key_points: list[str],
) -> None:
    """Keep `repertoire_fts` in step with the latest extraction of one reel.

    One row per reel, no cross-reel resolution needed (unlike entity_fts, which
    `resolve.py` rebuilds in full because entities merge across reels — a
    repertoire entry is its own reference, cf. the plan). `repertoire_fts`
    therefore keeps its own copy of the text rather than being a contentless
    index over another table: a plain DELETE + INSERT per reel is enough, and
    avoids the old-content bookkeeping a contentless FTS5 table would need on
    every re-extraction.

    Keyed on `reel.rowid`, not `extraction.id`: `run()` does `INSERT OR
    REPLACE` on `extraction.shortcode` (UNIQUE, not PRIMARY KEY), so a
    re-extraction gives that row a brand new autoincrement id every time.
    `reel.rowid` is stable for the reel's whole life."""
    row = conn.execute(
        "SELECT rowid FROM reel WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    if not row:
        return
    conn.execute("DELETE FROM repertoire_fts WHERE rowid = ?", (row["rowid"],))
    if result.mode == "repertoire":
        repertoire.sync_fts(
            conn,
            shortcode,
            topic=result.topic,
            why_saved=result.why_saved,
            tags=tags,
            key_points=key_points,
            title=result.topic,
        )


def _norm_tag(value: str | None) -> str:
    """Storage form of a tag or a facet: lowercase, no accents, no punctuation.
    Facets already have that shape (`street_food`); tags and collections
    (`Next Trip Japan`) do not. A vocabulary where `Randonnée` and `randonnee`
    coexist is not a vocabulary.

    Applies ONLY to tags and facets: entity names keep their casing, because that
    is what makes them readable in the catalogue and because the leading capital is
    the signal separating a proper noun (`Kamakura`) from a common one (`bocal`) —
    a signal `compare` relies on to measure a run's quality."""
    value = unicodedata.normalize("NFKD", (value or "").strip().lower())
    value = "".join(c for c in value if not unicodedata.combining(c))
    value = re.sub(r"[^\w\s-]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _scale_for_place(type_: str, scale: str | None) -> str | None:
    """`scale` only means something for a place; impose it by rule rather than by
    instruction.

    The prompt says "Leave it empty for non-places". Measured on the first v9 run:
    30 non-place entities out of 62 came back with `scale='site'` anyway — 8
    brands, 8 media, 5 methods, 3 people. A clothing brand has no geographic
    scale, and letting it through would pollute the controlled vocabulary exactly
    as `sub_category` did.

    The schema cannot express it: making `scale` depend on `type` would need a
    discriminated union, which Ollama's constrained grammar does not guarantee.
    Same reason to exist as _is_price_or_qty and _attested — what is exactly
    checkable gets checked, we do not hope for it from an 8B."""
    return scale if type_ in PLACE_TYPES else None


def _facets_for_type(type_: str, facets: list[str]) -> list[str]:
    """Deduplicate, drop what belongs to a family the type has no use for, cap.

    Three faults measured on the run of 2026-08-24, all three of a kind the schema
    cannot express — an enum constrains the VALUES, not their relevance:

    1. duplicates. `['craft', 'craft', 'craft']` on a recipe, `noodles` four times
       on an anime. Nothing downstream reads a facet twice, so a duplicate is pure
       noise in a controlled vocabulary.
    2. the wrong family. 17% of facets, 20% of candidates.
    3. enumeration. `#108 'I Parry Everything'` came back with 20 facets: the
       ENTIRE `nature` family, listed twice. `#55 '100 Meters'` with the entire
       `food` family. This is new, and it is the price of the closed vocabulary
       that fixed sub_category's long tail: a model with nothing to say used to
       invent, now it enumerates the list it was handed. Both are worse than an
       empty list.

    Order is preserved, so the cap keeps what the model produced FIRST — read
    fresh, before it started padding.

    WHAT THIS DOES NOT FIX, and it is worth knowing before trusting it: a wrong
    facet from a family the type DOES allow. A sake brewery given `beauty,
    clothing, outdoor` keeps them, because it is a `shop` and a MUJI store is a
    shop where those three are exactly right. Family membership cannot separate
    the two. Measured over the corpus, the rule drops 163 facets of 560 (29%)
    across 80 candidates of 215 — most of it `media` (which allows nothing yet)
    and the cap; the "right family, wrong facet" class survives and needs the
    prompt.
    """
    allowed = FAMILIES_BY_TYPE.get(type_, set(FACETS_BY_FAMILY))
    kept: list[str] = []
    for facet in facets:
        if facet in kept or FAMILY_OF_FACET.get(facet) not in allowed:
            continue
        kept.append(facet)
    return kept[:MAX_FACETS]


def _location_for_type(
    type_: str, city: str | None, country: str | None, locality: str | None
) -> tuple[str | None, str | None, str | None]:
    """(city, country, locality) — empty what has no meaning for the type.

    Measured on the same run: 29% of candidates carried a `city` on a NON-place
    type, and 14% had `city == country`. Both damage the same thing, because
    _gmaps_url builds its query from name + locality + city + country: a fiche
    ends up looked up as `Gorges du Tarn, France, France`, and a recipe surfaces
    under a city filter that is supposed to mean "you can go there".

    Some were plain invented: `Mount Dabajian` given `city='Taipei'` (it is in
    Hsinchu, and the source says only "Taiwan"), `Bungo Stray Dogs` given
    `city='Yokohama'` — the FICTIONAL setting of the story.

    `country` SURVIVES on a non-place, and that is a deliberate compromise rather
    than an oversight: a product usually has an origin worth keeping (`MUJI Steel
    Nail Clipper` -> Japon), and the catalogue can then answer "Japanese brands".
    The known cost, measured: on a reel shot in Berlin, 15 products inherited
    `Allemagne`, which is where the reel was filmed and not where they were made.
    The grain is coarse enough to be right more often than wrong.
    """
    if type_ not in PLACE_TYPES:
        city, locality = None, None
    if city and country and city.strip().lower() == country.strip().lower():
        city = None
    return city, country, locality


# A locality is meant to SHARPEN a Maps query. These sharpen nothing.
_LOCALITY_JUNK = re.compile(r"\b\d+[.,]?\d*\s*(m|km|mi|ft)\b", re.IGNORECASE)


def _clean_locality(locality: str | None) -> str | None:
    """Refuse what is not a locality.

    Observed: `'7'`, `'3'`, `'11'` — arrondissement numbers stripped of their
    city, which sharpen nothing and can only mislead the query; and
    `'1ER ARRONDISSEMENT, 4E ARRONDISSEMENT, 860 m, 75005'`, four OCR fragments
    including a distance. A wrong locality is worse than none: it is appended to
    the Maps query and drags it away from the place."""
    locality = (locality or "").strip()
    if len(locality) < 3 or locality.count(",") > 2 or _LOCALITY_JUNK.search(locality):
        return None
    return locality


def _clean_name(name: str) -> str:
    """An Instagram handle is not a name.

    11 candidates of the run were named with a raw `@handle` — `@muku.paris` typed
    `restaurant`, `@olivieblake` typed `media`. That costs twice: the Maps link is
    unusable, and the fiche reads as a handle rather than a place.

    WHAT IT CHANGES, exactly: readability and the Maps link, and NOTHING about
    grouping. `resolve.normalize_name` already strips the `@` and the punctuation,
    so `@muku.paris` and `Muku Paris` reduce to the same blocking key — this rule
    moves no candidate between groups. Verified rather than assumed: on a fresh
    database `Muku` and `Muku Paris` still resolve to two separate fiches. In the
    real corpus they DID merge, but through `entity_alias`, which remembers a
    fiche's past spellings from an earlier run; that mechanism, not this rule, is
    what joined them (cf. resolve._group_candidates, which documents this very
    case).

    So the handle-to-name problem proper is unsolved: `Olivieblake` and
    `Olivie Blake`, extracted from the same reel, remain two fiches. Joining them
    needs resolution against the corpus, a catalogue-level job."""
    name = name.strip()
    if not name.startswith("@"):
        return name
    words = re.split(r"[._]+", name.lstrip("@"))
    return " ".join(w[:1].upper() + w[1:] for w in words if w) or name


def _blank_to_none(value: str | None) -> str | None:
    """Fields made required in the schema answer "empty" with an empty string;
    the database says empty with NULL. One encoding on the storage side, otherwise
    every query has to test both."""
    value = (value or "").strip()
    # The model does not always answer "empty" with an empty string: it writes the
    # WORD. Four candidates came back with `city='Inconnu'`, `country='Inconnu'`.
    # Left alone, `Inconnu` becomes a city of the catalogue and a `controlled` tag
    # — the exact pollution the closed vocabulary was meant to end.
    if value.lower() in ("inconnu", "inconnue", "unknown", "n/a", "na", "-", "?"):
        return None
    return value or None


# A path or a filename, as they appear in a repository listing.
_FILE_LIKE = re.compile(
    r"(/$|\.(md|sh|py|js|ts|tsx|json|ya?ml|txt|toml|cfg|ini|sql|html|css|env)$)",
    re.IGNORECASE,
)

# Below this many file-like names in ONE reel, they are treated as ordinary
# entities. That threshold is the whole rule: a reel mentioning `CLAUDE.md` is
# talking about a real thing worth cataloguing; a reel listing twelve of them is
# showing a directory tree, and a tree is not a catalogue of places to go.
_FILE_TREE_MIN = 4


def _drop_file_tree(entities: list[Candidate]) -> list[Candidate]:
    """Drop a directory listing that got catalogued as entities.

    Measured on Da2g9a0OZAT, a reel about organising a repository: 12 of its 14
    entities were `agents/`, `hooks/`, `rules/`, `runbook.md`, `validate-bash.sh`.
    They passed every other guard — they are attested (they are on screen), they
    are not prices, their type is in the enum — and they alone doubled the corpus
    count of `service`.

    Judged on the count rather than on each name, because that is where the signal
    is: one filename is a subject, a dozen is a screenshot of a tree.

    COST, and it was measured before being accepted: `CLAUDE.md` goes too, and it
    was the reel's actual subject. A finer discriminator was tried — keep the ones
    the CAPTION names, drop the ones only the OCR saw — and it does not separate:
    that caption also names `context/`, `progress_tracking.md` and
    `task_group.md`. So the trade is 12 junk entities removed for 1 real one lost,
    taken knowingly rather than by omission."""
    file_like = [e for e in entities if _FILE_LIKE.search(e.name.strip())]
    if len(file_like) < _FILE_TREE_MIN:
        return entities
    dropped = {id(e) for e in file_like}
    print(
        f"      (filtered: {len(file_like)} file/directory names — this reel "
        f"shows a tree, not entities)",
        file=sys.stderr,
    )
    return [e for e in entities if id(e) not in dropped]


def _dedupe(entities: list[Candidate]) -> list[Candidate]:
    """The same place comes out several times from a reel that mentions it several
    times (observed on the Kamakura reel: Kamakura Tanukian x3, Yoridokoro x2, once
    per mention in the audio and the caption). The catalogue would merge them again
    anyway, but each duplicate costs a full verification call — better to drop them
    here, where it is deterministic.

    We keep the first occurrence: the one the model produced reading the source
    fresh, hence generally the most complete."""
    from domain.canonicalization import normalised_key

    seen: set[tuple[str, str]] = set()
    kept = []
    for entity in entities:
        key = (entity.type, normalised_key(entity.name))
        if key in seen:
            continue
        seen.add(key)
        kept.append(entity)
    return kept


def _todo(
    conn: sqlite3.Connection,
    limit: int | None,
    prompt_sha: str,
    model: str | None = None,
    force: bool = False,
    shortcodes: Collection[str] | None = None,
) -> list[str]:
    """A reel enters the queue if it has no extraction, or if the one it has was
    produced by a different prompt.

    That comparison is the whole re-extraction mechanism. There is no version to
    bump and no flag to remember: editing the system prompt or the JSON schema
    moves the fingerprint, and the corpus becomes stale by itself. The old
    arrangement asked a human to keep a label in step with the prompt, and twice
    the human did not.

    The price, and it is real: an innocuous edit to the prompt now costs a full
    re-extraction. That is why `run()` announces the change and the number of
    reels before starting, and why `--limit` exists.

    `force=True` replays reels already extracted with the current prompt — a
    second draw, to see how much of a difference between two runs is just the
    model's variance."""
    model = model or llm.model("extract")
    sql = """
        SELECT r.shortcode, e.prompt_sha, e.model, e.context_sha
        FROM reel r
        LEFT JOIN extraction e ON e.shortcode = r.shortcode
        WHERE r.unsaved_at IS NULL
                    -- EXISTS, and definitely not a JOIN: transcript and screen_text carry
                    -- one row PER tool_version, so the choice of row to read belongs to
                    -- build_context, which takes the most recent.
                    AND EXISTS (SELECT 1 FROM transcript sa WHERE sa.shortcode = r.shortcode)
                    AND EXISTS (SELECT 1 FROM screen_text so WHERE so.shortcode = r.shortcode)
                ORDER BY r.rowid
        """
    params: tuple[str, ...] = ()
    if shortcodes is not None:
        selected = tuple(dict.fromkeys(shortcodes))
        if not selected:
            return []
        placeholders = ", ".join("?" for _ in selected)
        sql = sql.replace(
            "        ORDER BY r.rowid",
            f"        AND r.shortcode IN ({placeholders})\n        ORDER BY r.rowid",
        )
        params = selected
    rows = list(conn.execute(sql, params))
    if force:
        candidates = [r["shortcode"] for r in rows]
        return candidates[:limit] if limit else candidates

    pending = []
    for row in rows:
        if (
            row["prompt_sha"] == prompt_sha
            and row["model"] == model
            and row["context_sha"]
            == context_fingerprint(build_context(conn, row["shortcode"]))
        ):
            continue
        pending.append(row["shortcode"])
    return pending[:limit] if limit else pending


def _record_attempt(
    conn: sqlite3.Connection,
    shortcode: str,
    *,
    model: str,
    prompt_sha: str,
    code: str | None,
    context_sha: str,
    started_at: str,
    duration_ms: int,
    raw_response: str | None,
    parsed_response: str | None,
    ok: bool,
    error: str | None,
) -> None:
    """Append provenance without competing with the single active extraction."""
    generation = llm.generation_settings(temperature=0.2)
    generation["usage"] = llm.usage()
    conn.execute(
        "INSERT INTO extraction_attempt"
        "(shortcode, model, prompt_sha, code_sha, context_sha, generation_json,"
        " started_at, completed_at, duration_ms, raw_response, parsed_response, ok, error)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            shortcode,
            model,
            prompt_sha,
            code,
            context_sha,
            json.dumps(generation, sort_keys=True),
            started_at,
            now(),
            duration_ms,
            raw_response,
            parsed_response,
            int(ok),
            error,
        ),
    )


def write_candidates(
    conn: sqlite3.Connection, shortcode: str, result: ExtractionResult, context: str
) -> list[Candidate]:
    """Turn one model response into `candidate` rows, guards applied. Returns the
    entities kept, for the caller to report on.

    Split out of `run()` so that `scripts/replay_guards.py` goes through EXACTLY
    this code path. The guards are pure functions of fields already stored, and
    `extraction.raw_response` keeps every model response, so a rule can be
    corrected and re-applied over the whole corpus without a single LLM call —
    but only as long as replaying and extracting write through the same function.
    Two code paths would let the database drift into a state no extraction could
    reproduce, and the replay would stop proving anything.

    Only `source='llm'` rows are touched. A hand-entered entity is not a competing
    version of the extraction but an addition on top of it, and survives every
    pass.
    """
    conn.execute(
        "DELETE FROM candidate WHERE shortcode = ? AND source = 'llm'", (shortcode,)
    )

    # Split BEFORE deduplication: two halves from one concatenated field can
    # perfectly well duplicate an entity already extracted elsewhere in the reel,
    # and that is _dedupe's call.
    split_out = []
    for entity in result.entities:
        pieces = _split_names(entity.name)
        if len(pieces) == 1:
            split_out.append(entity)
            continue
        latin_pieces = _split_names(entity.name_latin) if entity.name_latin else []
        for i, piece in enumerate(pieces):
            # We only pair the latin forms when there are as many of them;
            # otherwise an empty field beats a false pairing.
            latin = latin_pieces[i] if len(latin_pieces) == len(pieces) else ""
            split_out.append(
                entity.model_copy(update={"name": piece, "name_latin": latin})
            )

    kept_entities = []
    for entity in _drop_file_tree(_dedupe(split_out)):
        if _is_price_or_qty(entity.name):
            print(
                f"      (filtered: {entity.name!r} looks like a price or "
                "quantity, not an entity)",
                file=sys.stderr,
            )
            continue
        kept_entities.append(entity)

    inserted_candidates = []
    for entity in kept_entities:
        city, country, locality = _location_for_type(
            entity.type,
            _blank_to_none(entity.city),
            _blank_to_none(entity.country),
            _clean_locality(_attested(entity.locality, context)),
        )
        evidence_status, evidence_note, evidence = _assess_evidence(
            conn, shortcode, entity
        )
        cursor = conn.execute(
            "INSERT INTO candidate"
            "(shortcode, source, name, name_latin, type, facets_json,"
            " scale, city, country, locality, brand, intention,"
            " highlights_json, tags_json, evidence_json, evidence_status, evidence_note,"
            " why_saved, confidence)"
            " VALUES (?, 'llm', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                shortcode,
                _clean_name(entity.name),
                _attested(entity.name_latin, context),
                entity.type,
                json.dumps(
                    _facets_for_type(entity.type, entity.facets), ensure_ascii=False
                ),
                _scale_for_place(entity.type, _blank_to_none(entity.scale)),
                city,
                country,
                locality,
                entity.brand,
                entity.intention,
                json.dumps(entity.highlights, ensure_ascii=False),
                json.dumps(_useful_tags(entity.tags, entity), ensure_ascii=False),
                json.dumps([e.model_dump() for e in evidence], ensure_ascii=False),
                evidence_status,
                evidence_note,
                entity.why_saved,
                entity.confidence,
            ),
        )
        inserted_candidates.append(cursor.lastrowid)

    # Canonicalization is a separate local rule pass. It records its own evidence
    # and may abstain; the observed name and source evidence above are never rewritten.
    from domain.canonicalization import record_candidate

    for candidate_id in inserted_candidates:
        record_candidate(conn, candidate_id)
    return kept_entities


def run(
    conn: sqlite3.Connection,
    limit: int | None = None,
    model: str | None = None,
    force: bool = False,
    shortcodes: Collection[str] | None = None,
) -> dict[str, int]:
    model = llm.model("extract", model)
    prompt_sha = prompt_fingerprint()
    code = code_sha()
    todo = _todo(
        conn, limit, prompt_sha, model=model, force=force, shortcodes=shortcodes
    )

    # Say WHICH prompt is about to run and what it replaces, before spending an
    # hour on it. Since the fingerprint alone decides what is stale, a
    # prompt edited without meaning to would otherwise silently replay the whole
    # corpus with no announcement of any kind.
    previous = {
        r["prompt_sha"]
        for r in conn.execute("SELECT DISTINCT prompt_sha FROM extraction")
    } - {prompt_sha}
    if previous:
        print(
            f"  prompt {prompt_sha} (replaces {', '.join(sorted(previous))})",
            file=sys.stderr,
        )
    else:
        print(f"  prompt {prompt_sha}, code {code or 'unknown'}", file=sys.stderr)

    stats = {"ok": 0, "actionable": 0, "entities": 0, "repertoire": 0, "failed": 0}
    if not todo:
        return stats

    client = llm.client()
    try:
        for i, shortcode in enumerate(todo, 1):
            context = build_context(conn, shortcode)
            context_sha = context_fingerprint(context)
            started_at = now()
            started = time.monotonic()
            raw_response: str | None = None
            result: ExtractionResult | None = None
            # Persist the boundary before calling the model. If the process is
            # killed here, no authoritative `extraction` row exists and the next
            # startup deliberately selects this shortcode again from zero.
            mark(conn, shortcode, "extract", "running")
            conn.commit()
            try:
                result, raw_response = extract_staged(client, context, model)
            except Exception as error:  # noqa: BLE001
                _record_attempt(
                    conn,
                    shortcode,
                    model=model,
                    prompt_sha=prompt_sha,
                    code=code,
                    context_sha=context_sha,
                    started_at=started_at,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    raw_response=raw_response,
                    parsed_response=None,
                    ok=False,
                    error=str(error)[:300],
                )
                stats["failed"] += 1
                mark(conn, shortcode, "extract", "failed", str(error)[:300])
                print(
                    f"  [{i}/{len(todo)}] {shortcode} FAILED — {error}", file=sys.stderr
                )
                conn.commit()
                continue

            assert result is not None
            reel_tags = _useful_reel_tags(result.tags)
            key_points = _useful_key_points(result.key_points)
            try:
                # Do not make a partially-written result authoritative.  If a
                # guard or database write fails, the old LLM candidates remain
                # intact and the raw completion is recorded as a failed attempt.
                conn.execute("SAVEPOINT materialize_extraction")
                _record_attempt(
                    conn,
                    shortcode,
                    model=model,
                    prompt_sha=prompt_sha,
                    code=code,
                    context_sha=context_sha,
                    started_at=started_at,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    raw_response=raw_response,
                    parsed_response=result.model_dump_json(),
                    ok=True,
                    error=None,
                )
                # OR REPLACE on the reel: one extraction per reel, so a new pass
                # replaces the previous one instead of piling up beside it.
                conn.execute(
                    "INSERT OR REPLACE INTO extraction"
                    "(shortcode, model, prompt_sha, code_sha, extracted_at,"
                    " raw_response, mode, tags_json, key_points_json, content_kind, context_sha, ok)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        shortcode,
                        model,
                        prompt_sha,
                        code,
                        now(),
                        raw_response,
                        result.mode,
                        json.dumps(reel_tags, ensure_ascii=False),
                        json.dumps(key_points, ensure_ascii=False),
                        result.content_kind or None,
                        context_sha,
                    ),
                )
                if result.mode == "repertoire":
                    repertoire.upsert_entry(
                        conn,
                        shortcode=shortcode,
                        content_kind=result.content_kind or "guide",
                        title=result.topic,
                        summary=result.why_saved,
                        recipes=[recipe.model_dump() for recipe in result.recipes],
                    )
                _sync_repertoire_fts(conn, shortcode, result, reel_tags, key_points)
                # Only the LLM's own candidates: an entity entered by hand is not a
                # competing version of the extraction but an addition on top of it.
                conn.execute(
                    "DELETE FROM candidate WHERE shortcode = ? AND source = 'llm'",
                    (shortcode,),
                )
                kept_entities = write_candidates(conn, shortcode, result, context)
                conn.execute(
                    "INSERT OR REPLACE INTO classification"
                    "(shortcode, predicted_topic, is_actionable, why_saved,"
                    " confidence, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        shortcode,
                        result.topic,
                        int(result.is_actionable),
                        result.why_saved,
                        result.confidence,
                        now(),
                    ),
                )
                mark(conn, shortcode, "extract", "done")
                conn.execute("RELEASE SAVEPOINT materialize_extraction")
            except Exception as error:  # noqa: BLE001
                conn.execute("ROLLBACK TO SAVEPOINT materialize_extraction")
                conn.execute("RELEASE SAVEPOINT materialize_extraction")
                _record_attempt(
                    conn,
                    shortcode,
                    model=model,
                    prompt_sha=prompt_sha,
                    code=code,
                    context_sha=context_sha,
                    started_at=started_at,
                    duration_ms=round((time.monotonic() - started) * 1000),
                    raw_response=raw_response,
                    parsed_response=result.model_dump_json(),
                    ok=False,
                    error=f"materialisation: {str(error)[:280]}",
                )
                stats["failed"] += 1
                mark(conn, shortcode, "extract", "failed", str(error)[:300])
                print(
                    f"  [{i}/{len(todo)}] {shortcode} FAILED while saving — {error}",
                    file=sys.stderr,
                )
                conn.commit()
                continue
            conn.commit()

            stats["ok"] += 1
            stats["entities"] += len(kept_entities)
            if result.is_actionable:
                stats["actionable"] += 1
            if result.mode == "repertoire":
                stats["repertoire"] += 1
            flag = "actionable" if result.is_actionable else "not-actionable"
            names = ", ".join(e.name for e in kept_entities) or "(no entity)"
            print(
                f"  [{i}/{len(todo)}] {shortcode} {flag} [{result.mode}] "
                f"[{result.topic}] — {names}",
                file=sys.stderr,
            )
    finally:
        llm.unload(client, model)

    return stats

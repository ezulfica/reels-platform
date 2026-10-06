"""Typed contracts and controlled vocabularies for extraction."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

# --------------------------------------------------------------- vocabularies

# The first five are PLACES: they have coordinates, hence a Maps link
# (cf. resolve.TYPES_WITH_MAP). The others have nothing to look up on a map.
PLACE_TYPES = ("restaurant", "lodging", "shop", "place", "transport")
OTHER_TYPES = (
    "product",
    "brand",
    "service",
    "exercise",
    "recipe",
    "method",
    "media",
    "person",
    "other",
)
TYPES = PLACE_TYPES + OTHER_TYPES

# Granularity of a place. Empty for anything that is not a place.
# Without this axis, Kamakura (the city) and Jochiji (a temple it contains) were
# two sibling fiches of the same type, both with `city=Kamakura`.
SCALES = ("city", "district", "site")

# CLOSED vocabulary, enforced by the JSON schema. That is the difference between
# a controlled vocabulary and the intention of one: `sub_category` was free and
# the catalogue ended up with `calme` and `bambous` as "controlled" tags. Here, a
# value outside the list is structurally impossible.
#
# Built from values actually observed on the corpus, not a priori: every entry
# below covers at least one case seen in the database. Deliberately generic — a
# facet used once is not a facet, it is a detail, and its place is `highlights`.
#
# Grouped, not merely listed: the prompt presents them by family. A flat list of
# 46 words forces the model to re-read everything for each entity, where families
# give it a path — it only reads "body" for an exercise. The grouping has no
# existence in the database, it is a reading device.
FACETS_BY_FAMILY = {
    "food": (
        "ramen",
        "noodles",
        "sushi",
        "tempura",
        "street_food",
        "dessert",
        "bakery",
        "cafe",
        "bar",
        "breakfast",
        "vegetarian",
    ),
    "nature": ("beach", "waterfall", "gorge", "forest", "mountain", "cave", "garden"),
    "heritage": ("temple", "church", "abbey", "castle", "museum", "monument"),
    "urban": ("market", "street", "viewpoint"),
    "body": ("back", "hips", "shoulders", "legs", "mobility", "strength", "stretching"),
    "object": ("cooking", "photo", "video", "clothing", "beauty", "hair", "outdoor"),
    "style": ("design", "craft", "vintage", "luxury", "budget"),
    # Added after the run of 2026-08-24, where 18 of 21 `media` entities picked a
    # facet from a family that had nothing to do with them — `Bungo Stray Dogs`
    # given `sushi, museum, design` — because NO family described a film, an anime
    # or a book. Two of them enumerated an entire family instead. The model was
    # not being sloppy: it had no correct answer available and the field is
    # required.
    "media": (
        "anime",
        "film",
        "series",
        "book",
        "music",
        "podcast",
        "game",
        "documentary",
    ),
}

# Which facet FAMILIES mean anything for which type. The enum stops the model
# writing a value outside the vocabulary; it cannot stop it writing a value from
# the wrong end of it. Measured on the run of 2026-08-24: 17% of facets (99 of
# 560) fell outside any family plausible for their entity, affecting 20% of
# candidates — a sake brewery with `beauty, clothing, outdoor`, a tea farm with
# `tempura, noodles, vegetarian`.
#
# The mapping is deliberately GENEROUS: a `place` may legitimately carry food
# facets (a market), a `shop` may carry them too (a bakery). What it refuses is
# the reach into an unrelated family, which is where the noise lives.
#
# `media` maps to its own family and to nothing else. Before that family existed
# it mapped to the empty set, which emptied the field rather than let 18 of 21
# media entities carry `sushi` or `museum` — an empty list being the honest answer
# when the vocabulary has no correct one to offer. The family replaces silence
# with something usable; the restriction is what stops the reaching from coming
# back.
FAMILIES_BY_TYPE = {
    "restaurant": {"food", "style", "urban"},
    "lodging": {"style", "nature", "urban"},
    "shop": {"food", "object", "style", "urban"},
    "place": {"nature", "heritage", "urban", "style", "food"},
    "transport": {"urban", "style"},
    "product": {"object", "style", "food", "body"},
    "brand": {"object", "style", "food", "body"},
    "service": {"object", "style", "urban"},
    "exercise": {"body"},
    "recipe": {"food", "object"},
    "method": {"object", "body", "style", "food"},
    "person": {"style", "object", "body", "food"},
    "media": {"media"},
    "other": set(FACETS_BY_FAMILY),
}

FAMILY_OF_FACET = {
    f: family for family, facets in FACETS_BY_FAMILY.items() for f in facets
}

# At most this many facets on one entity. Same number and same reason as
# _useful_tags: past three, the model is no longer describing, it is enumerating.
MAX_FACETS = 3

FACETS = tuple(f for famille in FACETS_BY_FAMILY.values() for f in famille)

FACETS_BLOCK = "\n".join(
    f"- {famille} : {', '.join(valeurs)}"
    for famille, valeurs in FACETS_BY_FAMILY.items()
)


class Evidence(BaseModel):
    source: Literal["caption", "transcript", "ocr"]
    quote: str = Field(min_length=1, max_length=500)
    match: Literal["literal", "phonetic"]
    location: str


class Candidate(BaseModel):
    name: str
    # The readable latin form, when the reel shows it NEXT TO the original script
    # — and it almost always does: `葱油饼 SCALLION PANCAKE`,
    # `東區粉圓 EASTERN ICE STORE`, `よる YORU`, `蘭芳...LAN FANG`. Since RapidOCR
    # renders CJK correctly, the model often picks the original script and the
    # catalogue becomes unreadable: `葱油饼` where `scallion pancake` would be
    # usable.
    #
    # We ask for something ATTESTED, not a translation: that is what separates
    # this field from a computed transliteration (`unidecode` would give
    # `cong you bing`, neither the original nor useful) and what lets `verify`
    # check it like any other text.
    name_latin: str
    evidence: list[Evidence]
    type: Literal[TYPES]  # type: ignore[valid-type]
    # city/country/locality/scale are deliberately NON-optional and WITHOUT a
    # default, unlike the other descriptive fields. As `str | None`, the JSON
    # schema exposes a null branch the model takes systematically (100% null on
    # the v1 corpus); with a default, the field leaves `required` and it omits it,
    # which amounts to the same. With no default, it has to write something. The
    # empty string remains the legitimate answer for what has no location
    # (product, recipe) — _blank_to_none converts it back to NULL before
    # insertion, so the column keeps a single way of saying "empty".
    # CLOSED vocabulary, enforced by the schema's grammar: the model CANNOT write
    # a value outside the list, where a free `sub_category` produced ~90 values
    # for 133 fiches including `calme` and `bambous`. A list, because the model
    # was already writing lists into the single field (`dessert / soupe`,
    # `boisson / lait vegetal`) — the separator was its way of asking for this
    # field.
    facets: list[Literal[FACETS]]  # type: ignore[valid-type]
    # The empty string is a fully admissible value: a product has no scale.
    # Including it in the Literal rather than making the field optional avoids the
    # schema's null branch, which the model always takes.
    scale: Literal[SCALES + ("",)]  # type: ignore[valid-type]

    @field_validator("scale", mode="before")
    @classmethod
    def _street_is_a_site(cls, value: object) -> object:
        # Some free-form backends use "street" for a street-level place. It is
        # not a distinct catalogue granularity: store it as the existing `site`
        # value rather than failing the whole reel or changing the prompt schema.
        return "site" if value == "street" else value

    city: str
    country: str
    # Replaces `address`, measured at 0/133 in the catalogue. Optional it returned
    # nothing; required (v6) it rose to 18% but fabricated addresses — "12 Rue de
    # Lancry" copied onto a MUJI in Taipei — and cost 17 locatable entities,
    # 2.4 times the noise floor.
    #
    # What we ever wanted was not the postal address but a Maps link that lands
    # correctly. So `locality` asks only for what the reel already gives and what
    # sharpens the query: a district, an arrondissement, a landmark. Far more often
    # present on screen than a full address, and with nothing to invent. The
    # `address` remains an optional source field for place entities.
    locality: str
    brand: str | None = None
    intention: str | None = None
    # The concrete details the reel gives about THIS entity. This is what was
    # missing for a fiche to say anything beyond a generic intention. No default,
    # like city/country/locality and for the same reason: a field with a default
    # leaves `required` in the JSON schema, and the model omits it. An insistent
    # prompt compensated; a concise one did not. The empty list remains a valid
    # answer, but it has to be written.
    highlights: list[str]
    # FREE keywords about this entity. At ENTITY level and not reel level, unlike
    # v9: the field lived on ExtractionResult, so a guide-style reel showing 8
    # places stamped the same 5 words on all 8. Measured result: 234 labels for
    # 293 occurrences, 201 of them seen exactly once, and a head of the list
    # (`taiwan`, `japon`, `exercise`, `dos`) that merely restated
    # `city`/`country`/`type`/`facets`. The prompt gave it a single line.
    #
    # Assumed role: the escape hatch for the closed vocabulary. When no facet fits
    # (`michelin`, read in よる's OCR), a tag takes over. The most FREQUENT tags of
    # a run then point at the facets to add to FACETS in the next version — the
    # free vocabulary feeds the controlled one instead of competing with it.
    tags: list[str]
    why_saved: str
    confidence: float = Field(ge=0, le=1)

    @field_validator("confidence", mode="before")
    @classmethod
    def _percent_to_ratio(cls, value):
        """Some models answer as a percentage (95 instead of 0.95) and the JSON
        Schema does not stop them: Ollama enforces the grammar of types, not the
        `ge`/`le` bounds. Pydantic validation then rejected the reel's whole
        extraction — some model configurations returned percentages instead of ratios. The
        "between 0 and 1" wording is back in the prompt, but it is this conversion
        that guarantees it."""
        if isinstance(value, (int, float)) and 1 < value <= 100:
            return value / 100
        return value


class ExtractionResult(BaseModel):
    is_actionable: bool
    topic: str
    # `tags` left this level for Candidate in v10: a keyword is about an entity,
    # not about a reel. cf. Candidate.tags. It comes BACK at this level below,
    # as a different field (`tags`, reel-wide) for repertoire reels specifically
    # — see that field's own comment for why the two do not conflict.
    why_saved: str
    confidence: float = Field(ge=0, le=1)
    # repertoire | recommandation, decided in the SAME call that already reads
    # everything — no separate pre-filter call (see the extraction quality contract and
    # the staged extraction decision: a cheap classifier decided upstream
    # of the full context risks silently losing entities it had no signal to
    # ask for). Declared AFTER topic/why_saved on purpose: Ollama's constrained
    # JSON generation fills fields in declaration order, so asking for the
    # category before any reasoning text commits the model before it has
    # written one. Moving an equivalent `raison` field before the category in
    # a standalone probe fixed 2 of 7 disagreements against a Sonnet reference
    # on this exact question, at no prompt-text cost (the historical comparison).
    mode: Literal["repertoire", "recommandation"]
    # Reel-level, free, 0 to 5 items — see the prompt's "reel-level tags"
    # section. Kept separate from Candidate.tags (per-entity) rather than
    # reviving the pre-v10 design: a repertoire reel typically has few or no
    # entities to carry search keywords on its behalf, so they need a home at
    # the reel level too, specifically for that case.
    tags: list[str]
    # A compact reading layer for the reel fiche.  It deliberately belongs to
    # the extraction, so a Markdown export is a projection of the same record
    # rather than a second editable truth that can drift from the source.
    key_points: list[str]
    entities: list[Candidate]
    # A repertoire needs a usable kind before it can be queried.  Empty remains
    # legitimate for an ordinary recommendation and preserves historic rows.
    content_kind: Literal[
        "", "recipe", "exercise", "lesson", "method", "guide", "inspiration"
    ] = ""
    recipes: list["RecipeCard"] = Field(default_factory=list)

    _ratio = field_validator("confidence", mode="before")(
        Candidate._percent_to_ratio.__func__  # meme normalisation, meme raison
    )


# These calls deliberately have different jobs. Asking one 8B model to discover
# every spelling while it also writes summaries and controlled metadata is where
# recall was lost. The public result remains ExtractionResult, so the catalogue
# does not know, or care, how it was produced.
class FicheResult(BaseModel):
    is_actionable: bool
    topic: str
    why_saved: str
    confidence: float = Field(ge=0, le=1)
    mode: Literal["repertoire", "recommandation"]
    tags: list[str]
    key_points: list[str]
    content_kind: Literal[
        "", "recipe", "exercise", "lesson", "method", "guide", "inspiration"
    ]

    _ratio = field_validator("confidence", mode="before")(
        Candidate._percent_to_ratio.__func__
    )


class Mention(BaseModel):
    """A source-grounded name, before any descriptive inference is attempted."""

    name: str
    name_latin: str
    type: Literal[TYPES]  # type: ignore[valid-type]
    direct: bool
    evidence: list[Evidence] = Field(min_length=1, max_length=1)


class DiscoveryResult(BaseModel):
    mentions: list[Mention]


class EnrichedEntity(BaseModel):
    """Metadata for one name supplied by discovery; it cannot introduce names."""

    name: str
    facets: list[Literal[FACETS]]  # type: ignore[valid-type]
    scale: Literal[SCALES + ("",)]  # type: ignore[valid-type]
    city: str
    country: str
    locality: str
    brand: str | None = None
    intention: str | None = None
    highlights: list[str]
    tags: list[str]
    why_saved: str
    confidence: float = Field(ge=0, le=1)

    _ratio = field_validator("confidence", mode="before")(
        Candidate._percent_to_ratio.__func__
    )


    _street_is_a_site = field_validator("scale", mode="before")(
        Candidate._street_is_a_site.__func__
    )


class EnrichmentResult(BaseModel):
    entities: list[EnrichedEntity]


class RecipeCard(BaseModel):
    """Decision fields for one dish, deliberately separate from its long note."""

    dish_name: str
    cuisine: str
    cuisine_family: str
    course: Literal["", "entree", "plat", "dessert", "boisson", "accompagnement"]
    dietary_tags: list[str]
    summary: str
    evidence: list[Evidence]
    confidence: float = Field(ge=0, le=1)

    _ratio = field_validator("confidence", mode="before")(
        Candidate._percent_to_ratio.__func__
    )


class ContentIndexResult(BaseModel):
    recipes: list[RecipeCard]


ExtractionResult.model_rebuild()

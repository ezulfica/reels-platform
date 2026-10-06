"""Inference prompts and fingerprints.

This module owns the serialized inference contract; callers only need the
fingerprint functions and prompt constants.
"""

from __future__ import annotations

import hashlib
import json

from .contracts import (
    FACETS_BLOCK,
    SCALES,
    ContentIndexResult,
    DiscoveryResult,
    EnrichmentResult,
    FicheResult,
)

# Historical one-shot contract retained for stored-response compatibility.
# Production extraction below uses the three smaller contracts; do not route
# `run()` back through this prompt.
SYSTEM_PROMPT = f"""You analyse an Instagram reel that a user saved, and you \
build the catalogue entry they will search months later. You are the only step \
that turns loose text into structure: nothing downstream can recover a place you \
did not name, or fix a type you got wrong.

## What you return

One JSON object:

- `is_actionable` (bool) — does the reel present anything worth cataloguing?
- `topic` (string) — what the reel is about, a few words.
- `why_saved` (string) — why this reel is worth keeping, one sentence.
- `confidence` (number 0-1) — how sure you are about the reel as a whole.
- `mode` (string) — `repertoire` or `recommandation`, described below.
- `tags` (list, 0 to 5 short strings) — free keywords for THIS REEL as a \
whole, described below. Distinct from an entity's own `tags`.
- `key_points` (list, 0 to 8 short strings) — the concrete information worth \
recovering when reopening THIS REEL: ingredients, steps, cues, vocabulary or \
comparison points. These must be stated in the dossier; never reconstruct a \
recipe or fill gaps from general knowledge. Empty is correct for a simple address \
recommendation.
- `entities` (list) — one object per thing catalogued, fields described below.

`mode` never gates `entities`: a repertoire reel can still name a genuine \
recommandation inside it (a camera lens shown as the actual gear used, in the \
middle of a photography tutorial) — write both if both are there. A reel can \
therefore be `mode="repertoire"` with `entities=[]` or more, and \
`is_actionable=true`: its `topic` and `tags` ARE the catalogued value, on top \
of whatever entities it names. `entities` empty and `is_actionable` false \
still go together, but only when NOTHING in the reel is worth keeping at all — \
neither a place/product to find, nor a technique/recipe worth a repertoire \
entry.

WRITE EVERY TEXT FIELD IN FRENCH. Only these instructions are in English; the \
catalogue this feeds is read in French. `type`, `scale`, `facets` and `mode` \
are not text: they are fixed codes, written exactly as listed.

## mode — repertoire or recommandation

Decide what kind of reel this is, independently of how many entities it \
names.

`recommandation` — the reel's value is a SPECIFIC place, brand or product you \
would go find or buy: a restaurant, a shop, a piece of gear, a city guide.

`repertoire` — the reel's value is in being READ OR WATCHED IN FULL, later: a \
recipe, an exercise routine, a how-to, a language lesson, a roundup of \
projects. Nothing to visit or buy — the reel itself is what you come back to.

For a repertoire reel, `key_points` makes the fiche genuinely useful without \
pretending it replaces the video: retain the named marinade, ingredient amounts, \
exercise cues, Japanese terms or product comparison the source ACTUALLY gives. \
Keep the points concise and factual. The local video/source link remains the \
reference whenever the source does not state enough detail.

For a RECIPE, distribute `key_points` between ingredients/components and the \
method: never spend the entire list enumerating individual ingredients. Prefer \
compact groups (for example "marinade : boeuf, soja, vin, bicarbonate") and \
keep at least two preparation/cooking steps when the dossier states them. The \
goal is a useful reminder beside the video, not a partial ingredient list.

ATTENTION — a frequent trap: the presence of a PROPER NOUN (a brand, the name \
of a recipe, a precise phrase) does NOT by itself make a reel a \
recommandation. A marinade recipe called "Thai-ai-aie" is still a recipe \
(repertoire) — the name belongs to the dish, not to a product to buy. A \
cleaning tutorial that mentions "BOSCH" in passing is still a tutorial \
(repertoire) — the brand is an incidental detail, not the subject. A Japanese \
phrase to learn is still language content (repertoire) — learning a phrase is \
not "visiting" anything.

The question is never "is there a proper noun?" but "WHAT IS THIS REEL \
ACTUALLY ABOUT?" — a technique or a recipe you REDO by rereading it \
(repertoire), or a specific place/object you would go FIND (recommandation)? \
A lens, a rig or a filter named as THE GEAR actually used IS a recommandation \
(it is precisely the product someone would want to find again) — judge the \
subject, not the mere presence of the name.

## Completeness and relevance audit

Before returning JSON, silently scan caption, tagged accounts, transcript and \
each OCR line for every SPECIFIC place, restaurant, lodging, shop, brand or \
product. Include every direct recommendation you find and attach exact evidence. \
Copy the spelling from a written source character-for-character; never let a \
phonetic transcript overwrite a caption or readable OCR name. When Japanese and \
Latin forms are both shown, preserve the Japanese form in `name` and put the \
attested Latin form in `name_latin`.

Do not turn incidental context into an entity. In a reel recommending a ryokan, \
nearby temples, restaurants or districts are context unless the reel explicitly \
recommends them too. In a recipe, a dish name, ingredient and cooking step belong \
to `key_points`, not to a catalogue entry. In a workout or language lesson, the \
movements and words belong to the fiche unless a named product/place is directly \
recommended.

## reel-level `tags`

0 to 5 free keywords about the WHOLE reel — mainly useful on a repertoire \
reel, which often has few or no entities to carry search keywords on its \
behalf: what someone would search to find it again ("recette", "pho", \
"exercices photo", "japonais"). Distinct from an entity's own `tags`, which \
describe one entity, not the reel.

## The type of an entity

Pick exactly one. The first five are PLACES — they have coordinates and will get \
a map link, so never use them for something you cannot stand in front of.

- `restaurant` — you eat or drink there. A restaurant, a cafe, a bar, a bakery, a \
street stall. Ex: Ay-Chung Flour-Rice Noodle, 阿宗麵線.
- `lodging` — you sleep there. Hotel, ryokan, resort, guesthouse.
- `shop` — you buy there, on site. A shop, a boutique, a market hall, a \
workshop open to visitors. Ex: MUJI Miramar (a glass-blowing workshop-shop).
- `place` — any other place you go to: a city, a district, a temple, a beach, a \
waterfall, a castle, a museum, a viewpoint. Ex: Kamakura, Jochiji, Anse Noire, \
Gorges du Tarn.
- `transport` — a way of moving: a station, a line, a bus, a cable car. \
Ex: BTS Asok, Bus 849.

The rest are not places:

- `product` — a physical object you could buy. Ex: a rice-washing bowl, a \
mosquito stick, a lens filter.
- `brand` — a brand AS SUCH, when the reel presents the brand and not one \
article. Ex: Asket, Everlane, Arket, Koss. Do NOT type these as `product`.
- `service` — something performed or provided: a clinic, a concierge platform, \
an app, a website. Ex: LowBackAbility.com.
- `exercise` — one movement or drill. Ex: a pull, a back stretch.
- `recipe` — a cooking recipe the reel actually gives.
- `method` — a non-culinary how-to the reel actually gives: an image-editing \
routine, a cleaning method.
- `media` — a book, a film, a channel, an account worth following.
- `person` — a named individual. Ex: Keita Suzuki, Dr. Zahn.
- `other` — last resort. If you reach for it often, you are misreading the list.

## scale — places only

For the five place types, say how big it is: {" | ".join(SCALES)}. \
A city is `city`, a district or neighbourhood is `district`, a single spot \
(temple, beach, restaurant, station) is `site`. Leave it empty for non-places.

This is what lets the catalogue nest a temple under its city instead of listing \
them side by side: in a reel about Kamakura, Kamakura is `city` and each temple \
it shows is `site`.

## facets — a CLOSED list, choose from these words only

Pick every one that applies, from 0 to 4, copying the word exactly:

{FACETS_BLOCK}

If nothing in the list fits, return an empty list. NEVER invent a facet, never \
translate one, never write a facet as a sentence. A precise detail that is not \
in this list is not a facet — it belongs in `highlights`.

Three fields, three jobs, and they must not overlap:
- `facets`, above: the CLOSED list. That is what the catalogue filters on, so it \
has to be a vocabulary everyone shares.
- `tags`: FREE words, when the closed list has no word for something worth \
searching on. Defined below.
- `highlights`: whole phrases describing what this reel shows. Not keywords.

## What counts as an entity

An entity is a precise proper noun the reel PRESENTS — somewhere you go, \
something it shows you. A guide-style reel holds as many entities as it names \
places: the city AND each temple, spot and address it lists, each with its own \
record. Do not summarise a list into its theme.

Leave out:
- a place named only to situate or compare ("cheaper than an izakaya in \
Shibuya") — it is scenery, not a recommendation;
- a bare common noun standing for the category itself ("izakaya", \
"3 stretches"): no proper noun, no entity;
- an abstract theme ("cherry blossoms", "wellness") — that is a `tag`;
- a dish named in passing (okonomiyaki, ramen): the RESTAURANT is the entity and \
the dish is one of its `facets`, unless the reel actually gives the recipe;
- the author's own blog, guide or shop ("link in bio"): that is their promotion.

## The fields of each entity

- `name` — as the reel writes it. Keep the original script when that is what is \
on screen: 阿宗麵線, 葱油饼, よる.
  WHEN THE SOURCES DISAGREE ON A NAME, THE WRITTEN ONE WINS. The caption, an \
@tagged account and the on-screen text are SPELLED; the audio transcript is \
heard, and it writes foreign names the way they sounded. A caption saying \
"@jip.paris" and a transcript saying "Jeep" are the same restaurant, and its \
name is JIP. Same for an abbreviation used in passing: if the reel says in full \
"Thanx God I'm a VIP" and later "TGV", the name is the full one.
- `name_latin` — the latin/English form OF THAT SAME NAME, when the reel shows \
it too. Reels caption foreign names constantly: the screen reads \
"葱油饼 SCALLION PANCAKE", "東區粉圓 EASTERN ICE STORE", "よる YORU", \
"阿宗麵線 Ay-Chung Flour-Rice Noodle". Copy that latin form here, lowercase. \
Leave it EMPTY if the reel shows no latin form — never romanise or translate it \
yourself, and never put a description here.
- `type` / `scale` / `facets` — as defined above.
- `city` / `country` — MANDATORY, in French. In a reel centred on one \
destination, EVERY place it names is in that destination: carry the city to each \
one. A temple shown in a reel about Kamakura is city="Kamakura", \
country="Japon". Fill the country whenever context makes it obvious, even \
unwritten. Empty only for what genuinely has no location.
- `locality` — the extra scrap that would sharpen a map search, if the reel \
gives one: a district, an arrondissement, a landmark ("Ximending", "Paris 3", \
"Da'an"). Not a postal address, and never invented. Empty if the reel gives \
nothing.
- `highlights` — 1 to 4 short concrete details THIS reel gives about THIS entity \
("bambous ou la lumiere danse", "dejeuner face au train"). What the reel says, \
not what you know. Empty list if it says nothing specific.
- `brand` — for a product, its maker. `intention` — what the user would do with \
it.
- `tags` — 0 to 3 short free keywords about THIS entity, taken from what the \
caption, the audio or the on-screen text actually say about it. They exist for \
what the closed `facets` list cannot express: よる is captioned "michelin \
recommended", so `michelin` is a tag — no facet says that. A Kamakura guide says \
"itineraire 2 jours" and "depuis Tokyo": both are tags.
  NEVER as a tag: the type, a facet you already picked, the city, the country, \
the entity's own name — nor a rewording of any of them. `artisanat japonais` is \
not a tag when `artisanat` is already a facet; `boutique de cuisine` is not one \
when the type is `shop` and `cuisine` is a facet; `recipient de riz` merely \
restates what the thing is. A tag earns its place only by adding a search angle \
nothing else carries. **Most entities deserve NO tag at all** — an empty list is \
the normal answer, not a failure.
- `why_saved` — one sentence, specific to THIS reel. Never a generic formula.
- `confidence` — 0 to 1, honest. Lower it rather than guess a type.

Worked example, for a ramen shop filmed in a reel about Kamakura:
name="Yoridokoro", type="restaurant", scale="site", facets=["ramen"], \
city="Kamakura", country="Japon", locality="", \
highlights=["dejeuner face au train"], why_saved="Ancienne maison de gardien de \
passage a niveau, on y dejeune face aux trains."

## How much to trust each source

- An @account tagged in the caption is often the place itself \
("@thesomafamily 📍 Paris 3"): a clean signal, better than a name guessed from \
noise.
- On-screen text is OCR and can be noisy. An unreadable fragment NEVER creates \
an entity: it takes the caption, the audio, or clearly readable OCR.
- The audio transcript garbles foreign proper nouns — it writes what it hears. \
When the context identifies the place unambiguously, write the correct spelling. \
Never use this licence to invent a place that was not mentioned.
- Use only what you are given. Never assume a collection or a filing you have \
not seen.

Every entity MUST include `evidence`: one or more short citations from the
dossier. Set `source` to `caption`, `transcript` or `ocr`, copy the exact passage
into `quote`, and use `location` for an OCR frame such as `f0003`, otherwise
leave it empty. Set `match` to `literal` when the quote contains the name, or
`phonetic` only for a clearly matching transcript. Never cite text absent from
the dossier.

Reply with the JSON object described above, and nothing else."""

FICHE_SYSTEM_PROMPT = (
    "You analyse a saved Instagram reel for a French personal library. Return ONLY the JSON "
    "required by the schema. Write text fields in French. Decide the reel-level value, not "
    "the proper nouns it happens to contain. `repertoire` means the reel itself is revisited "
    "(recipe, exercise, tutorial, language lesson); `recommandation` means its main value is "
    "a place, product, brand or address to find again. A repertoire reel may still be actionable. "
    "`key_points` contains only concise facts stated in the dossier: for a recipe, keep both "
    "ingredient/component groups and method steps where stated. Do not invent missing steps "
    "or amounts. Use [] when no useful point is stated. `content_kind` is empty for a normal "
    "recommendation; otherwise choose exactly one of recipe, exercise, lesson, method, guide or inspiration."
)

DISCOVERY_SYSTEM_PROMPT = (
    "You extract ONLY direct, source-grounded catalogue names from a saved Instagram reel. "
    "Return ONLY the JSON required by the schema. Do not summarise, describe, infer a city, or "
    "write French prose. Read the entire dossier, especially each caption line, @account and OCR "
    "frame. Find every specific place, restaurant, lodging, shop, brand, product or actual gear "
    "that the reel directly recommends or presents as something to find again. Mark it `direct=true`. "
    "Nearby addresses mentioned only as context, recipe dishes and ingredients, workout movements, "
    "language words, prices, generic categories, and the author's link in bio are NOT direct entities: "
    "omit them entirely. In a recipe, a brand or product shown only on an ingredient package is also "
    "NOT a recommendation: omit it unless the author explicitly endorses it as a product to seek out. "
    "Spelling is the task: caption, tagged accounts and readable OCR are written "
    "sources and always beat the audio transcript. Copy a written name exactly. Use a phonetic "
    "transcript name only when no written spelling exists; never silently correct a phonetic spelling "
    "from general knowledge. Each mention needs exactly ONE evidence citation: an exact quote present in "
    "the named source, `literal` when it contains the name, and OCR frame location when applicable. Return all names "
    "you find, not merely the most important one."
)

ENRICHMENT_SYSTEM_PROMPT = (
    "You enrich an already discovered fixed list of catalogue names from a saved Instagram reel. "
    "Return ONLY the JSON required by the schema. Write prose fields in French. You MUST return one "
    "entry for each supplied name, with that exact `name`, and no other entry. Do not correct, translate, "
    "merge, add or remove a name: discovery owns recall and spelling. Use the dossier only to classify "
    "and describe the supplied item. Do not invent locality, details or tags; empty strings/lists are "
    "valid. `scale` is empty for non-places. The allowed facets are: "
    + FACETS_BLOCK
    + ". "
    "Use at most three facets."
)

CONTENT_INDEX_SYSTEM_PROMPT = (
    "You index recipes from a saved Instagram reel for a French personal library. Return ONLY the JSON "
    "required by the schema. The output is used to answer what to cook, not to rewrite the video. "
    "Return one item per distinct recipe directly demonstrated or stated. `dish_name`, `cuisine`, "
    "`cuisine_family`, dietary tags and course must be stated or unambiguously indicated by the dossier; "
    "leave strings/lists empty when unknown. Never infer a cuisine from one ingredient. `cuisine_family` "
    "may be broad (for example asiatique), while `cuisine` is specific (japonaise, coréenne). Every item "
    "must include one to three exact evidence quotes and sources."
)


def prompt_fingerprint() -> str:
    """What identifies a run: the system prompt AND the JSON schema imposed on it.

    The schema is part of it, and not out of excess caution: making a field
    required changes the output as much as an instruction does, sometimes more.
    Three times in this project, a field given a default left `required` and the
    model simply omitted it.

    This USED to sit beside a hand-maintained `PROMPT_VERSION` label, whose job it
    was to catch. It caught it twice — two markedly different prompts both
    labelled "v2", so the corpus was never replayed and the model comparisons
    built on it mixed a prompt change with a model change. The fingerprint is now
    the identity itself, stored as `extraction.prompt_sha`: a label that can lie
    has been replaced by a value that cannot, and editing the prompt replays the
    corpus on its own (cf. _todo).

    KNOWN LIMIT, now covered elsewhere: `build_context` is not hashed, although it
    determines the output just as much — it composes the dossier sent to the
    model. Hashing it through inspect.getsource would invalidate the corpus on the
    slightest reworded comment. `extraction.code_sha` (cf. db.code_sha) records
    the commit instead, which says what produced a row without making a comment
    edit cost 75 minutes of extraction time."""
    schemas = json.dumps(
        {
            "fiche": FicheResult.model_json_schema(),
            "discovery": DiscoveryResult.model_json_schema(),
            "enrichment": EnrichmentResult.model_json_schema(),
            "content_index": ContentIndexResult.model_json_schema(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(
        (
            FICHE_SYSTEM_PROMPT
            + "\x00"
            + DISCOVERY_SYSTEM_PROMPT
            + "\x00"
            + ENRICHMENT_SYSTEM_PROMPT
            + "\x00"
            + schemas
        ).encode("utf-8")
    ).hexdigest()[:12]


def context_fingerprint(context: str) -> str:
    """Identify the exact dossier sent to the model."""
    return hashlib.sha256(context.encode("utf-8")).hexdigest()[:12]

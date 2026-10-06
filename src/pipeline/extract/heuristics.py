"""Free, exact checks on a candidate — no LLM call.

They are deliberately part of the production verification module: `verify.py`
needs deterministic checks that do not depend on an external script or an LLM.

That promotion was earned the hard way. Over two runs, `verify.py` — an LLM call
per candidate — produced 6 false rejections out of 11, every one of them naming
something LITERALLY PRESENT in the source while asserting it was absent:
`Soul Eater` and `Fruits Basket`, both listed in their reel's caption;
`E FORTUNES 百樂逢`, plainly in the OCR. Meanwhile every rejection it got RIGHT
was one `is_attested` already caught.

The cause is not subtle: "does this string occur in this text" is a substring
test, and an 8B performs it worse than `in` does.
"""

from __future__ import annotations

import sqlite3


# The types whose entities are LOOKED UP by a name someone gave them. Everywhere
# else, a common noun IS the correct answer: a recipe is `raviolis sans pliage`,
# a product is `standing desk`, an exercise is `reverse plank`. None of them has
# a proper noun to carry.
#
# Measured, and it is why this constant exists: of the 38 names the criterion
# flagged on the run of 2026-08-24, 37 sat on `product`, `recipe`, `exercise` or
# `service`. Exactly ONE was a genuine defect. Applied without the type, the
# heuristic reports the vocabulary of the catalogue as a fault — it made me write
# "17% of names are common nouns" into a judgement as if that were a problem.
PROPER_NOUN_TYPES = frozenset(
    {
        "restaurant",
        "lodging",
        "shop",
        "place",
        "transport",
        "brand",
        "person",
        "media",
    }
)


def is_common_noun(name: str | None, type_: str | None = None) -> bool:
    """A name without a single capital, ON A TYPE THAT EXPECTS A PROPER NOUN.

    Without `type_` the check is name-only and over-reports; pass the type
    whenever you have it (cf. PROPER_NOUN_TYPES).

    Crude on purpose: the goal is not to classify perfectly but to make two runs'
    propensity to promote generic words comparable. False positives (a shop sign
    written all in lowercase) hit both runs the same way.

    It works only because entity names keep their original casing, unlike tags and
    facets which are normalised to lowercase (cf. extract._norm_tag).

    A CASELESS script (Chinese, Japanese) necessarily escapes the criterion:
    `葱油饼 == 葱油饼.lower()` is true, which would count it as a common noun. That
    bias would be systematic and directed — it would hit precisely the reels the
    OCR reads best. Hence: at least one cased letter before concluding."""
    if type_ is not None and type_ not in PROPER_NOUN_TYPES:
        return False
    name = (name or "").strip()
    if not name or not any(c.isalpha() and c.lower() != c.upper() for c in name):
        return False
    return name == name.lower()


def sources_of(conn: sqlite3.Connection, shortcode: str) -> str:
    """Caption, latest transcript and latest OCR, lowercased and concatenated."""
    parts = []
    for sql in (
        "SELECT caption FROM reel WHERE shortcode = ?",
        "SELECT text FROM transcript WHERE shortcode = ?"
        " ORDER BY created_at DESC LIMIT 1",
        "SELECT text FROM screen_text WHERE shortcode = ?"
        " ORDER BY created_at DESC LIMIT 1",
    ):
        row = conn.execute(sql, (shortcode,)).fetchone()
        if row and row[0]:
            parts.append(row[0].lower())
    return " \n ".join(parts)


def is_attested(sources: str, name: str | None) -> bool:
    """Does the name appear literally in the caption, the audio or the OCR?

    Catches what counting alone cannot: OCR fragments promoted to entities
    (`festaurant`, `kalios GA`, `FKAFFETJ`) and invented names.

    Deliberately ONE-SIDED, and that asymmetry is the whole point of using it
    against verify.py. A False here is a hint, not a verdict — a transcript that
    spells a foreign name by ear ("Yoridokoro" heard as "Yorido Koro") is a real
    attestation this test cannot see, which is exactly the case an LLM judges
    better. But a True is a FACT: the string is there, and no amount of reasoning
    makes it absent."""
    name = (name or "").strip().lower()
    if not name:
        return True
    return name in sources

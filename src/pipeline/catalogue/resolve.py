"""Entity resolution: merges step-3 candidates (LLM plus manual) into canonical
fiches.

Deliberately simple for now: blocking by (type, normalised name) only, widened by
known aliases — no embeddings, no LLM arbitration. To be added later if volume
reveals real duplicates this rule misses (paraphrases with no shared name, e.g.
"that little Italian place" vs "Saizeriya").

Full rebuild on every run rather than incremental: unlike extract (expensive
LLM calls, hence timestamp tracking), resolving entities here is just string
grouping in memory — replaying over the whole corpus every time is simpler and
stays fast, even at full-corpus scale (a few thousand candidates).
"""

from __future__ import annotations

import json
import sqlite3
from urllib.parse import quote_plus

from storage.database import now
from domain.canonicalization import normalised_key
from ..extract import extract

# These types have a real physical address: a Maps link makes sense. The others
# (product, brand, service, recipe...) have nothing to look up on a map.
#
# Older vocabularies stay listed: the catalogue can hold fiches from an earlier
# extraction (it keeps one extraction per reel, not necessarily of the current
# version), and stripping their Maps link would be an invisible regression.
TYPES_WITH_MAP = {
    # v11, English vocabulary
    "restaurant",
    "lodging",
    "shop",
    "place",
    "transport",
    # v9-v10, French
    "resto",
    "hebergement",
    "commerce",
    "lieu",
    "destination",  # <= v8
}


def normalize_name(name: str) -> str:
    """Blocking key: lowercase, accents stripped, punctuation stripped, spaces
    normalised. A @tagged account and its name without the @ must land in the same
    group (cf. @thesomafamily)."""
    return normalised_key(name.lstrip("@"))


def _gmaps_url(
    name: str, locality: str | None, city: str | None, country: str | None
) -> str | None:
    """`locality` (district, arrondissement, landmark) slots between the name and
    the city: that is what separates two same-named stalls in one district, and it
    is the field's only reason to exist — we never wanted the postal address, we
    wanted a link that lands correctly."""
    query = " ".join(p for p in (name, locality, city, country) if p)
    return f"https://www.google.com/maps/search/?api=1&query={quote_plus(query)}"


def _pick_canonical_name(rows: list[sqlite3.Row]) -> str:
    """The most frequent name in the group; on a tie, the shortest (avoids
    dragging along noisy variants like 'Saizeriya, apparently').

    A variant without the @ always beats the same one with it: both land in the
    same group (normalize_name strips the @), but '@muku.paris' is a handle while
    'Muku Paris' is the name of the place — that is the one we display.

    And the latin form beats the original script when the reel gave both: the
    catalogue is meant to be read, and `scallion pancake` is usable there where
    `葱油饼` says nothing to someone who does not read Chinese. The original script
    is not lost for all that — it goes into the aliases, so it stays searchable and
    serves the matching (cf. _group_candidates)."""
    counts: dict[str, int] = {}
    for row in rows:
        resolved = (
            (row["resolved_name"] or "").strip()
            if "resolved_name" in row.keys()
            else ""
        )
        n = resolved or (row["name_latin"] or "").strip() or row["name"]
        counts[n] = counts.get(n, 0) + 1
    best = max(
        counts.items(), key=lambda kv: (not kv[0].startswith("@"), kv[1], -len(kv[0]))
    )
    return best[0]


def _merge_highlights(rows: list[sqlite3.Row]) -> str | None:
    """Deduplicated union of the details the reels give about this entity.
    Deduplicated on the normalised form so the same sentence is not kept twice up
    to case or punctuation, but the original spelling is preserved — this is text
    shown to the user.

    Stored as a ' | ' string rather than JSON: entity.highlights is fed as-is into
    the FTS column of the same name, and doubles as displayable text. JSON would
    force every reader to decode it for nothing.
    """
    seen: dict[str, str] = {}
    for row in rows:
        for item in json.loads(row["highlights_json"] or "[]"):
            item = (item or "").strip()
            if item:
                seen.setdefault(normalize_name(item), item)
    return " | ".join(seen.values()) or None


def _majority(rows: list[sqlite3.Row], field: str) -> tuple[str | None, list[str]]:
    """The value kept for a field, and the competing values set aside.

    Replaces the original `next(r[field] for r in rows if r[field])` — the first
    non-null value encountered, in whatever arbitrary order SQLite returned the
    rows. Two reels disagreeing about the city of one place therefore produced an
    arbitrary winner, with no trace, and the Maps link followed it.

    This is the catalogue-side counterpart of a known defect on the extraction
    side: `verify.py` confronts an entity's NAME with its source, never its fields.
    So nobody checks that a city is the right one. Lacking a way to decide, we keep
    the best-attested value and record the disagreement (cf.
    entity.conflicts_json) — which makes the problem visible instead of hiding it.

    The vote runs on the values as they are: an entity where 3 reels say "Kamakura"
    and 1 says "Kanagawa" keeps Kamakura and flags Kanagawa."""
    counts: dict[str, int] = {}
    for row in rows:
        value = (row[field] or "").strip()
        if value:
            counts[value] = counts.get(value, 0) + 1
    if not counts:
        return None, []
    kept = max(counts.items(), key=lambda kv: (kv[1], -len(kv[0])))[0]
    return kept, sorted(v for v in counts if v != kept)


def _merge_facets(rows: list[sqlite3.Row]) -> list[str]:
    """Union of the facets of the group's candidates. No majority vote here,
    unlike `city`: facets are multi-valued by construction, and two reels seeing
    one `ramen` and the other `street_food` on the same stall are both right. The
    vocabulary being closed (cf. extract.FACETS), the union cannot drift."""
    seen: list[str] = []
    for row in rows:
        for f in json.loads(row["facets_json"] or "[]"):
            if f and f not in seen:
                seen.append(f)
    return seen


def _collections(conn: sqlite3.Connection, shortcodes: set[str]) -> list[str]:
    """The collections the user filed these reels under.

    This is the only vocabulary source the pipeline cannot guess: it comes from
    the user. It is also by far the most stable — 50 labels for 864 reels
    (`Next Trip Japan`, `Foods`, `Stretching`, `Summer in Paris`), where the LLM
    produced 234 for 293 tags. No inference, therefore no possible hallucination.

    `tag.kind` has reserved the value 'collection' since the initial migration
    without anything ever writing one: the 50 collections were synced at capture
    time and stopped there.

    Note: `build_context` still does NOT show the collections to the model (a
    deliberate choice, cf. its docstring). Wiring them here is a different matter —
    we give the LLM nothing to infer from, we join structured data at resolution
    time."""
    if not shortcodes:
        return []
    marks = ",".join("?" * len(shortcodes))
    return [
        r["name"]
        for r in conn.execute(
            f"""SELECT DISTINCT bc.name FROM reel_collection brc
            JOIN collection bc ON bc.collection_id = brc.collection_id
            WHERE brc.shortcode IN ({marks})""",
            tuple(shortcodes),
        )
    ]


def _hashtags(conn: sqlite3.Connection, shortcodes: set[str]) -> list[str]:
    """The hashtags of the source reels: the author's own words, verbatim, already
    parsed at sync time into reel_context. Nothing to infer here either."""
    if not shortcodes:
        return []
    marks = ",".join("?" * len(shortcodes))
    seen: list[str] = []
    for row in conn.execute(
        f"SELECT hashtags FROM reel_context WHERE shortcode IN ({marks})",
        tuple(shortcodes),
    ):
        for h in json.loads(row["hashtags"] or "[]"):
            h = (h or "").lstrip("#")
            if h and h not in seen:
                seen.append(h)
    return seen


def _candidate_tags(rows: list[sqlite3.Row]) -> list[str]:
    """Union of the free keywords carried by the group's candidates."""
    seen: list[str] = []
    for row in rows:
        for t in json.loads(row["tags_json"] or "[]"):
            if t and t not in seen:
                seen.append(t)
    return seen


# One label can arrive from several sources after normalisation: the city `Tokyo`
# and the hashtag `#tokyo` both yield `tokyo`. The strongest kind wins, otherwise
# insertion order would decide whether `tokyo` is filterable or not.
_KIND_RANK = {"free": 0, "collection": 1, "controlled": 2}


def _get_or_create_tag(conn: sqlite3.Connection, label: str, kind: str) -> int:
    row = conn.execute("SELECT id, kind FROM tag WHERE label = ?", (label,)).fetchone()
    if row:
        if _KIND_RANK.get(kind, 0) > _KIND_RANK.get(row["kind"], 0):
            conn.execute("UPDATE tag SET kind = ? WHERE id = ?", (kind, row["id"]))
        return row["id"]
    return conn.execute(
        "INSERT INTO tag(label, kind) VALUES (?, ?)", (label, kind)
    ).lastrowid


def _unverified_count(conn: sqlite3.Connection) -> int:
    """How many candidates have not yet been confronted with the source text."""
    return conn.execute(
        """
        SELECT COUNT(*) FROM candidate c
        JOIN reel r ON r.shortcode = c.shortcode
        WHERE c.verified IS NULL AND r.unsaved_at IS NULL
        """
    ).fetchone()[0]


def _group_candidates(
    conn: sqlite3.Connection, candidates: list[sqlite3.Row]
) -> dict[tuple[str, str], list[sqlite3.Row]]:
    """Blocking by (type, normalised name), widened by already-known aliases.

    WHAT THIS FIXES, and it is an observed bug: a fiche's identity is
    (canonical_name, type), and `_pick_canonical_name` can elect a different name
    from one run to the next as soon as the group's composition moves — that
    happened with '@muku.paris' becoming 'Muku Paris'. The old fiche then found
    itself with no candidate at all, so it was purged as an orphan, and everything
    attached to it went with it. Aliases give the group a memory of its past
    spellings: a candidate carrying the old name rejoins the current fiche instead
    of founding a new one.

    WHAT THIS DOES NOT FIX, and it needs saying: the table is only ever filled
    with the names of candidates ALREADY gathered into one group. So it preserves
    merges, it creates none. 葱油饼 read by RapidOCR on one reel and "scallion
    pancake" written in another's caption will stay two distinct fiches — no string
    rule brings them together. Uniting them needs either a manual entry (the table
    already accepts it, `source='manuel'`) or the deliberately deferred embedding
    arbitration. What is gained here is that the day either arrives, it will have
    somewhere to write."""
    alias_to_group: dict[tuple[str, str], tuple[str, str]] = {}
    for row in conn.execute(
        "SELECT a.alias, e.type, e.canonical_name FROM entity_alias a"
        " JOIN entity e ON e.id = a.entity_id"
    ):
        alias_to_group[(row["type"], row["alias"])] = (
            row["type"],
            normalize_name(row["canonical_name"]),
        )

    groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
    for c in candidates:
        resolved = (
            (c["resolved_name"] or "").strip() if "resolved_name" in c.keys() else ""
        )
        key = (c["type"], normalize_name(resolved or c["name"]))
        key = alias_to_group.get(key, key)
        groups.setdefault(key, []).append(c)
    return groups


def run(conn: sqlite3.Connection, force: bool = False) -> dict[str, int]:
    # Guardrail: resolving is destructive by design (links and tags are rebuilt
    # entirely, and fiches left orphaned are purged). Combined with "a single
    # extraction per reel, the most recent" and the `verified = 1` filter, a fresh
    # unverified extraction makes its reels invisible here — their fiches become
    # orphans and disappear.
    #
    # Not theoretical: an `extract && verify && catalog` interrupted by an Ollama
    # crash left 52 reels out of 60 with an unverified extraction. A `catalog` run
    # in that state would have taken the catalogue from 140 fiches down to about
    # ten, silently. Better to refuse to run.
    if not force:
        pending_count = _unverified_count(conn)
        if pending_count:
            raise RuntimeError(
                f"{pending_count} candidates of the latest extraction are not yet "
                "verified: resolving now would purge their fiches as orphans. Run "
                "`reels verify` first (or force=True if you know what you are "
                "doing)."
            )

    # One extraction per reel means there is nothing to arbitrate here any more.
    # This used to be a correlated subquery choosing WHICH of a reel's extractions
    # was authoritative, plus an exception clause to let hand-entered candidates
    # through — both gone with the version key. `source` is read only to be
    # displayed; the resolution treats a human candidate exactly like an LLM one.
    # A recipe, exercise or method extracted from a repertoire reel is useful
    # evidence for its fiche, not a recommendation to merge with every other
    # similarly named recipe in the catalogue.  Products and places remain: a
    # reel can be a tutorial while directly recommending a named lens or shop.
    # Human entries are deliberately exempt — a person can make that curation
    # decision explicitly.
    candidates = conn.execute(
        """
        SELECT c.id AS candidate_id, c.shortcode, c.source, c.name, c.name_latin,
               CASE WHEN nr.status='resolved' THEN nr.canonical_name END AS resolved_name,
               c.type, c.facets_json, c.scale, c.city, c.country, c.locality,
               c.address, c.brand, c.intention, c.highlights_json, c.tags_json,
               c.why_saved, c.confidence
        FROM candidate c
        LEFT JOIN candidate_name_resolution nr ON nr.candidate_id=c.id
        JOIN reel r ON r.shortcode = c.shortcode
        JOIN extraction e ON e.shortcode = c.shortcode
        WHERE c.verified = 1 AND r.unsaved_at IS NULL
          AND (c.source = 'human' OR e.mode IS NULL OR e.mode != 'repertoire'
               OR c.type NOT IN ('recipe', 'exercise', 'method'))
        """
    ).fetchall()

    groups = _group_candidates(conn, candidates)

    stats = {"entities": 0, "links": 0}
    # Start clean on every run: links and tags are recomputed entirely from the
    # current candidates, never accumulated.
    conn.execute("DELETE FROM entity_tag")
    conn.execute("DELETE FROM entity_reel")
    # Requalify rather than purge. `entity_tag` is emptied on every run, `tag`
    # never was: 225 labels out of 312 were no longer attached to any fiche and
    # lingered as kind='controlled' — old free `sub_category` values like
    # `Casque de realite virtuelle` or `Circuit de Formule 1`, polluting the
    # filtering vocabulary.
    #
    # We reset everything to 'free' and the run raises the kind of whatever it
    # meets (cf. _KIND_RANK). A label that has become an orphan thus falls back to
    # 'free' without being destroyed: it costs nothing and may attach to a future
    # fiche.
    conn.execute("UPDATE tag SET kind = 'free'")
    # `DELETE FROM` is refused on a contentless FTS5 table ('content=""'): with no
    # stored content, SQLite cannot reconstruct the terms to remove. The
    # 'delete-all' command is the form provided for this case.
    conn.execute("INSERT INTO entity_fts(entity_fts) VALUES('delete-all')")

    for (type_, _norm), rows in groups.items():
        canonical_name = _pick_canonical_name(rows)
        # The latin form kept, stored separately: `canonical_name` already carries
        # it when it exists, but the column says which of the two cases we are in —
        # an originally latin name (`Ay-Chung`) or a non-latin script made readable
        # (`葱油饼` -> `Scallion Pancake`). The original script is never lost: it
        # is in the aliases.
        name_latin, _ = _majority(rows, "name_latin")
        # Majority vote plus a record of the disagreement, where it used to be
        # "first non-null value encountered, silently".
        city, city_rejected = _majority(rows, "city")
        country, country_rejected = _majority(rows, "country")
        locality, locality_rejected = _majority(rows, "locality")
        scale, scale_rejected = _majority(rows, "scale")
        address, _ = _majority(rows, "address")
        conflicts = {
            field: rejected
            for field, rejected in (
                ("city", city_rejected),
                ("country", country_rejected),
                ("locality", locality_rejected),
                ("scale", scale_rejected),
            )
            if rejected
        }
        conflicts_json = (
            json.dumps(conflicts, ensure_ascii=False) if conflicts else None
        )
        facets = _merge_facets(rows)
        facets_json = json.dumps(facets, ensure_ascii=False)
        why_saved_list = sorted({r["why_saved"] for r in rows if r["why_saved"]})
        why_saved = " | ".join(why_saved_list) if why_saved_list else None
        highlights = _merge_highlights(rows)
        gmaps_url = (
            _gmaps_url(canonical_name, locality, city, country)
            if type_ in TYPES_WITH_MAP
            else None
        )

        existing = conn.execute(
            "SELECT id FROM entity WHERE canonical_name = ? AND type = ?",
            (canonical_name, type_),
        ).fetchone()
        if existing:
            entity_id = existing["id"]
            conn.execute(
                "UPDATE entity SET name_latin=?, facets_json=?, scale=?,"
                " city=?, country=?, locality=?, address=?, gmaps_url=?,"
                " highlights=?, why_saved=?, conflicts_json=?, updated_at=?"
                " WHERE id=?",
                (
                    name_latin,
                    facets_json,
                    scale,
                    city,
                    country,
                    locality,
                    address,
                    gmaps_url,
                    highlights,
                    why_saved,
                    conflicts_json,
                    now(),
                    entity_id,
                ),
            )
        else:
            entity_id = conn.execute(
                "INSERT INTO entity"
                "(canonical_name, name_latin, type, facets_json,"
                " scale, city, country, locality, address, gmaps_url,"
                " highlights, why_saved, conflicts_json, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    canonical_name,
                    name_latin,
                    type_,
                    facets_json,
                    scale,
                    city,
                    country,
                    locality,
                    address,
                    gmaps_url,
                    highlights,
                    why_saved,
                    conflicts_json,
                    now(),
                ),
            ).lastrowid
        stats["entities"] += 1

        for r in rows:
            conn.execute(
                "INSERT OR IGNORE INTO entity_reel"
                "(entity_id, shortcode, candidate_id, relevance_note)"
                " VALUES (?, ?, ?, ?)",
                (entity_id, r["shortcode"], r["candidate_id"], r["why_saved"]),
            )
            stats["links"] += 1

        # `kind` tells the truth about the vocabulary, and it did not use to.
        #
        # `sub_category` was labelled 'controlled' while being a free text field:
        # the catalogue's "controlled" vocabulary contained `calme`, `bambous`,
        # `petit matin`, `dejeuner face au train`. A filter built on it offered
        # 90 values for 133 fiches, nearly all unique.
        #
        # From now on: 'controlled' is whatever draws its values from a closed list
        # — `type`, `scale`, `facets` (imposed by the JSON schema) — plus
        # `city`/`country`, which are proper nouns rather than free vocabulary.
        # Everything else is 'free'.
        shortcodes = {r["shortcode"] for r in rows}
        labels = [
            # Closed vocabulary or proper nouns: what we FILTER on.
            *((t, "controlled") for t in (type_, scale, city, country) if t),
            *((f, "controlled") for f in facets),
            # The user's own filing.
            *((c, "collection") for c in _collections(conn, shortcodes)),
            # Free text: FTS recall only, never filtering.
            *((t, "free") for t in _candidate_tags(rows)),
            *((h, "free") for h in _hashtags(conn, shortcodes)),
        ]
        for label, kind in labels:
            # Lowercase, no accents, no punctuation for the WHOLE vocabulary:
            # facets already have that shape (`street_food`), neither collections
            # (`Next Trip Japan`) nor hashtags do. Entity NAMES, by contrast, keep
            # their casing — cf. extract._norm_tag.
            label = extract._norm_tag(label)
            if not label:
                continue
            tag_id = _get_or_create_tag(conn, label, kind)
            conn.execute(
                "INSERT OR IGNORE INTO entity_tag(entity_id, tag_id, source)"
                " VALUES (?, ?, 'resolve')",
                (entity_id, tag_id),
            )

        # The aliases: every spelling this group has been seen under, BOTH the
        # original script and the latin form. Since v11 a single candidate carries
        # the two (`name` and `name_latin`), so `葱油饼` and `scallion pancake`
        # become aliases of the same fiche — and a future reel naming that stall
        # either way joins it instead of founding a second one.
        # cf. _group_candidates(), which reads them back on the next run.
        graphies = {r["name"] for r in rows} | {
            r["name_latin"] for r in rows if (r["name_latin"] or "").strip()
        }
        for nom in graphies:
            alias = normalize_name(nom)
            if alias:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_alias(entity_id, alias, source)"
                    " VALUES (?, ?, 'candidate')",
                    (entity_id, alias),
                )

        tags_text = " ".join(
            dict.fromkeys([extract._norm_tag(lbl) for lbl, _ in labels if lbl])
        )
        conn.execute(
            "INSERT INTO entity_fts(rowid, canonical_name, summary, highlights, tags)"
            " VALUES (?, ?, ?, ?, ?)",
            (entity_id, canonical_name, why_saved or "", highlights or "", tags_text),
        )

    # A fiche no candidate speaks of any more is a residue of a previous run: the
    # reel was unsaved, or a new extraction renamed the entity (seen with
    # '@muku.paris' -> 'Muku Paris'). It used to survive indefinitely, with no
    # reel, invisible in search but present in the catalogue. Hand-confirmed fiches
    # are spared: they represent work the pipeline has no right to erase.
    stats["purged"] = conn.execute(
        "DELETE FROM entity WHERE verified = 0 AND id NOT IN"
        " (SELECT entity_id FROM entity_reel)"
    ).rowcount

    conn.commit()
    return stats

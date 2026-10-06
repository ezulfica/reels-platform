"""Tests on what breaks silently — not on what raises an exception."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import inference as llm
from adapters import video_archive
from config import settings as config
from domain import canonicalization, repertoire
from domain import feedback as catalog_feedback
from domain import reel_library as library
from pipeline.capture.instagram import (
    collection_ids,
    media_kind,
    parse_context,
    parse_reel,
    upsert_reels,
)
from pipeline.catalogue import resolve as catalog_resolve
from pipeline.enrich.ocr import ocr_frames, ocr_observations
from pipeline.extract import extract
from quality import regression
from storage import database as db


@pytest.fixture
def conn(tmp_path):
    c = db.connect(tmp_path / "test.db")
    db.initialize(c)
    return c


def media(code="ABC123", **extra):
    base = {
        "pk": 12345,
        "code": code,
        "taken_at": 1721000000,
        "media_type": 2,
        "product_type": "clips",
        "caption": {"text": "Un reel"},
        "user": {"username": "someone"},
    }
    base.update(extra)
    return base


# ------------------------------------------------------------------ idempotence


def test_two_runs_yield_the_same_rows(conn):
    """v2 processed the same reel twice (7_Db3Eah_xPOL and 86_Db3Eah_xPOL)."""
    batch = [media("AAA"), media("BBB")]
    upsert_reels(conn, batch)
    first = conn.execute("SELECT COUNT(*) FROM reel").fetchone()[0]

    upsert_reels(conn, batch)
    second = conn.execute("SELECT COUNT(*) FROM reel").fetchone()[0]

    assert first == second == 2


def test_first_seen_frozen_last_seen_advances(conn):
    upsert_reels(conn, [media("AAA")])
    before = conn.execute("SELECT first_seen_at, last_seen_at FROM reel").fetchone()

    conn.execute("UPDATE reel SET last_seen_at = '2000-01-01T00:00:00+00:00'")
    upsert_reels(conn, [media("AAA")])
    after = conn.execute("SELECT first_seen_at, last_seen_at FROM reel").fetchone()

    assert after["first_seen_at"] == before["first_seen_at"]
    assert after["last_seen_at"] != "2000-01-01T00:00:00+00:00"


def test_reappearing_reel_loses_its_unsaved_at(conn):
    """Re-saving a reel must reactivate it, not create a duplicate."""
    upsert_reels(conn, [media("AAA")])
    conn.execute("UPDATE reel SET unsaved_at = '2026-01-01T00:00:00+00:00'")
    upsert_reels(conn, [media("AAA")])
    assert conn.execute("SELECT unsaved_at FROM reel").fetchone()[0] is None


# ------------------------------------------------------------------ collections


def test_reel_in_several_collections(conn):
    upsert_reels(conn, [media("AAA", saved_collection_ids=["111", "222"])])
    n = conn.execute("SELECT COUNT(*) FROM reel_collection").fetchone()[0]
    assert n == 2


def test_missing_collection_ids_breaks_nothing(conn):
    upsert_reels(conn, [media("AAA")])
    assert conn.execute("SELECT COUNT(*) FROM reel_collection").fetchone()[0] == 0


def test_collection_ids_converted_to_text():
    assert collection_ids({"saved_collection_ids": [111, "222"]}) == ["111", "222"]


# ---------------------------------------------------------------------- parsing


@pytest.mark.parametrize(
    "payload,attendu",
    [
        ({"product_type": "clips", "media_type": 2}, "reel"),
        ({"media_type": 8}, "carousel"),
        ({"media_type": 2}, "video"),
        ({"media_type": 1}, "post"),
    ],
)
def test_media_kind(payload, attendu):
    assert media_kind(payload) == attendu


def test_media_without_caption_or_user_survives():
    row = parse_reel({"pk": 1, "code": "X", "media_type": 1})
    assert row["caption"] == "" and row["username"] == ""


def test_list_marker_detected():
    """These reels contain ten entities; v1 produced only one, truncated."""
    assert (
        parse_context(media(caption={"text": "All the locations below ⬇️"}))[
            "has_list_marker"
        ]
        == 1
    )
    assert (
        parse_context(media(caption={"text": "Joli coucher de soleil"}))[
            "has_list_marker"
        ]
        == 0
    )


def test_hashtags_mentions_links_extracted():
    ctx = parse_context(
        media(caption={"text": "Test #japon #food @teacher_tanuki https://a.b/c"})
    )
    assert json.loads(ctx["hashtags"]) == ["japon", "food"]
    assert json.loads(ctx["mentions"]) == ["teacher_tanuki"]
    assert json.loads(ctx["links"]) == ["https://a.b/c"]


def test_ocr_keeps_frame_locations_and_legacy_text():
    class Result:
        def __init__(self, *texts):
            self.txts = list(texts)

    results = iter((Result("Taipei"), Result("Taipei"), Result("Lan Fang")))
    reader = lambda _path: next(results)
    frames = ["f0001.jpg", "f0002.jpg", "f0003.jpg"]
    observations = ocr_observations(reader, [Path(f) for f in frames])

    assert observations == [
        {"frame": "f0001", "text": "Taipei"},
        {"frame": "f0003", "text": "Lan Fang"},
    ]

    results = iter((Result("Taipei"), Result("Taipei"), Result("Lan Fang")))
    assert ocr_frames(reader, [Path(f) for f in frames]) == ("Taipei\nLan Fang")


# --------------------------------------------------- sync : parcours partiel vs complet


class FakeSession:
    """Simulated paginated feed: two pages, then the end."""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, url, params=None, timeout=None):  # pragma: no cover - non utilise
        raise AssertionError("sync doit passer par get_json")


def fake_get_json_factory(pages):
    state = {"i": 0}

    def fake_get_json(session, url, params=None):
        page = pages[state["i"]]
        state["i"] += 1
        return page

    return fake_get_json


def page(codes, more=True, next_id="x"):
    return {
        "items": [{"media": media(c)} for c in codes],
        "more_available": more,
        "next_max_id": next_id if more else None,
    }


def test_early_stop_unsaves_nobody(conn, monkeypatch):
    """The bug: an early stop had seen only one page, yet concluded that the whole
    rest of the corpus had vanished from the feed — 1326 reels wrongly marked."""
    upsert_reels(conn, [media("AAA"), media("BBB"), media("CCC")])

    from pipeline.capture import instagram as ig

    monkeypatch.setattr(ig, "get_json", fake_get_json_factory([page(["AAA"])]))
    monkeypatch.setattr(ig.time, "sleep", lambda *_: None)

    stats = ig.sync(conn, FakeSession([]))

    assert stats["pages"] == 1  # stops at the first known page
    assert "unsaved" not in stats  # no conclusion drawn
    orphelins = conn.execute(
        "SELECT COUNT(*) FROM reel WHERE unsaved_at IS NOT NULL"
    ).fetchone()[0]
    assert orphelins == 0


def test_limit_unsaves_nobody(conn, monkeypatch):
    upsert_reels(conn, [media("AAA"), media("BBB")])

    from pipeline.capture import instagram as ig

    monkeypatch.setattr(ig, "get_json", fake_get_json_factory([page(["ZZZ"])]))
    monkeypatch.setattr(ig.time, "sleep", lambda *_: None)

    ig.sync(conn, FakeSession([]), limit=1)

    orphelins = conn.execute(
        "SELECT COUNT(*) FROM reel WHERE unsaved_at IS NOT NULL"
    ).fetchone()[0]
    assert orphelins == 0


def test_full_walk_unsaves_the_absent(conn, monkeypatch):
    """Conversely: a walk carried through to the end is authoritative."""
    upsert_reels(conn, [media("AAA"), media("PARTI")])

    from pipeline.capture import instagram as ig

    monkeypatch.setattr(
        ig, "get_json", fake_get_json_factory([page(["AAA"], more=False)])
    )
    monkeypatch.setattr(ig.time, "sleep", lambda *_: None)

    stats = ig.sync(conn, FakeSession([]), full=True)

    assert stats["unsaved"] == 1
    row = conn.execute(
        "SELECT unsaved_at FROM reel WHERE shortcode = 'PARTI'"
    ).fetchone()
    assert row["unsaved_at"] is not None


# ------------------------------------------------------------------ non-regression


def test_sync_manifest_records_pages_and_observations(conn, monkeypatch):
    from pipeline import sync_manifest
    from pipeline.capture import instagram as ig

    seen = []
    monkeypatch.setattr(
        ig, "get_json", fake_get_json_factory([page(["AAA"], more=False)])
    )
    monkeypatch.setattr(ig.time, "sleep", lambda *_: None)
    run_id = sync_manifest.start(conn)
    stats = ig.sync(
        conn,
        FakeSession([]),
        full=True,
        seen_callback=lambda codes: (
            seen.extend(codes),
            sync_manifest.observe(conn, run_id, codes),
        ),
    )
    sync_manifest.finish(conn, run_id, status="done", stats=stats)
    row = conn.execute("SELECT * FROM sync_run WHERE id=?", (run_id,)).fetchone()
    assert row["status"] == "done"
    assert row["pages"] == 1 and row["items_seen"] == 1 and row["unique_items"] == 1
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM sync_observation WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        == 1
    )
    assert seen == ["AAA"]


def test_entity_reel_join_matches(conn):
    """The v1 bug: entity_reels.reel_id held the shortcode while the FK declared
    reels(id) = "{pk}_{userid}". The JOIN matched nothing."""
    upsert_reels(conn, [media("AAA")])
    conn.execute(
        "INSERT INTO entity(canonical_name, type, updated_at)"
        " VALUES ('Chartier', 'resto', '2026-01-01')"
    )
    conn.execute("INSERT INTO entity_reel(entity_id, shortcode) VALUES (1, 'AAA')")
    n = conn.execute(
        "SELECT COUNT(*) FROM entity_reel er JOIN reel r ON r.shortcode = er.shortcode"
    ).fetchone()[0]
    assert n == 1


def test_duplicate_entity_rejected(conn):
    """(canonical name, type) identifies an entity: v1 had 3 copies of one."""
    for _ in range(3):
        conn.execute(
            "INSERT OR IGNORE INTO entity(canonical_name, type, updated_at)"
            " VALUES ('Guide Taiwan', 'destination', '2026-01-01')"
        )
    assert conn.execute("SELECT COUNT(*) FROM entity").fetchone()[0] == 1


def test_fk_refuses_an_unknown_shortcode(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO transcript(shortcode, tool_version, created_at)"
            " VALUES ('INEXISTANT', 'v1', '2026-01-01')"
        )


# ----------------------------------------------------------------- orchestration


def test_stage_state_makes_steps_resumable(conn):
    """v1's single-column `status` stayed stuck on 'to_review' for 1340 reels."""
    upsert_reels(conn, [media("AAA"), media("BBB")])
    assert set(db.pending(conn, "asr")) == {"AAA", "BBB"}

    db.mark(conn, "AAA", "asr", "done")
    assert db.pending(conn, "asr") == ["BBB"]


def test_repeated_failures_leave_the_queue(conn):
    upsert_reels(conn, [media("AAA")])
    for _ in range(3):
        db.mark(conn, "AAA", "asr", "failed", "boom")
    assert db.pending(conn, "asr", max_attempts=3) == []


def test_unsaved_reel_excluded_from_work(conn):
    upsert_reels(conn, [media("AAA")])
    conn.execute("UPDATE reel SET unsaved_at = '2026-01-01T00:00:00+00:00'")
    assert db.pending(conn, "asr") == []


def test_migrations_are_replayable(tmp_path):
    c = db.connect(tmp_path / "m.db")
    assert db.initialize(c) is True
    assert db.initialize(c) is False


# --------------------------------------------------------------- prompt/version


def test_prompt_fingerprint_is_the_stored_identity():
    """The fingerprint used to guard a hand-maintained label, `PROMPT_VERSION`.
    It had to, because the label could lie and twice did: the v2 prompt was
    tightened from 5572 to 2754 bytes with fields going from optional to required,
    and stayed labelled "v2". Two markedly different prompts, indistinguishable in
    the database, and a model comparison built on top measuring the prompt change
    as much as the model change.

    The label is gone: `extraction.prompt_sha` stores the fingerprint itself, so a
    reel is re-extracted exactly when the prompt that produced it differs from the
    current one. What is left to lock is that the value is stable across a run —
    it is a database key, and a fingerprint that moved on a whim would silently
    invalidate the corpus.

    If this test fails you have touched the prompt or the schema. That is allowed,
    and it costs a full re-extraction: replace the value below deliberately.
    """
    assert extract.prompt_fingerprint() == "a81669ebd7dc", (
        "the prompt or the schema changed — the whole corpus is now stale "
        f"(new fingerprint: {extract.prompt_fingerprint()})"
    )


def test_editing_the_prompt_makes_the_corpus_stale(conn, monkeypatch):
    """The mechanism that replaces the version bump: `_todo` compares the stored
    fingerprint with the current one. Nothing to remember, nothing to increment —
    which is the point, since remembering is what failed before."""
    upsert_reels(conn, [media("AAA", caption={"text": "Un reel"})])
    for table in ("transcript", "screen_text"):
        conn.execute(
            f"INSERT INTO {table}(shortcode, tool_version, text,"
            f" created_at) VALUES ('AAA', 't', 'text', '2026-01-01')"
        )
    conn.execute(
        "INSERT INTO extraction(shortcode, model, prompt_sha, extracted_at,"
        " context_sha, ok) VALUES ('AAA', 'm', ?, '2026-01-01', ?, 1)",
        (
            extract.prompt_fingerprint(),
            extract.context_fingerprint(extract.build_context(conn, "AAA")),
        ),
    )

    assert extract._todo(conn, None, extract.prompt_fingerprint(), model="m") == []
    # A prompt edited by one character: the reel is stale again.
    assert extract._todo(conn, None, "0000deadbeef", model="m") == ["AAA"]
    # --force replays it even without a prompt change.
    assert extract._todo(
        conn, None, extract.prompt_fingerprint(), model="m", force=True
    ) == ["AAA"]


def test_editing_the_context_makes_the_corpus_stale(conn):
    upsert_reels(conn, [media("AAA", caption={"text": "Un reel"})])
    for table in ("transcript", "screen_text"):
        conn.execute(
            f"INSERT INTO {table}(shortcode, tool_version, text,"
            f" created_at) VALUES ('AAA', 't', 'text', '2026-01-01')"
        )
    context_sha = extract.context_fingerprint(extract.build_context(conn, "AAA"))
    conn.execute(
        "INSERT INTO extraction(shortcode, model, prompt_sha, extracted_at,"
        " context_sha, ok) VALUES ('AAA', 'm', ?, '2026-01-01', ?, 1)",
        (extract.prompt_fingerprint(), context_sha),
    )
    assert extract._todo(conn, None, extract.prompt_fingerprint(), model="m") == []

    conn.execute("UPDATE reel SET caption = 'Un autre reel' WHERE shortcode = 'AAA'")
    assert extract._todo(conn, None, extract.prompt_fingerprint(), model="m") == ["AAA"]


def test_running_extraction_is_selected_again_without_authoritative_row(conn):
    upsert_reels(conn, [media("AAA", caption={"text": "Un reel"})])
    for table in ("transcript", "screen_text"):
        conn.execute(
            f"INSERT INTO {table}(shortcode, tool_version, text, created_at)"
            f" VALUES ('AAA', 't', 'text', '2026-01-01')"
        )
    db.mark(conn, "AAA", "extract", "running")
    conn.commit()
    assert extract._todo(conn, None, extract.prompt_fingerprint(), model="m") == ["AAA"]


def test_extract_can_target_a_fixed_reference_sample(conn):
    for shortcode in ("AAA", "BBB"):
        upsert_reels(conn, [media(shortcode)])
        for table in ("transcript", "screen_text"):
            conn.execute(
                f"INSERT INTO {table}(shortcode, tool_version, text, created_at)"
                f" VALUES (?, 'v', 'text', '2026-01-01')",
                (shortcode,),
            )
    assert extract._todo(conn, None, "new-prompt", model="m", shortcodes=["BBB"]) == [
        "BBB"
    ]
    assert extract._todo(conn, None, "new-prompt", model="m", shortcodes=[]) == []


def test_decisive_fields_required_in_the_schema():
    """Three times in a row (sub_category, then city/country, then highlights), a
    field given a default left `required` in the JSON Schema and the model simply
    omitted it — 100% empty values across the corpus. What must be guaranteed goes
    in the schema, not in the prompt."""
    requis = extract.ExtractionResult.model_json_schema()["$defs"]["Candidate"][
        "required"
    ]
    for champ in (
        "facets",
        "scale",
        "city",
        "country",
        "locality",
        "highlights",
        "name_latin",
        "tags",
        "evidence",
    ):
        assert champ in requis, f"{champ} doit rester obligatoire"

    # `address` is the counter-proof, and it bounds the rule: making it required
    # does take it from 0% to 15% of locatable entities, but per-reel pairing shows
    # it costs 17 locatable entities (78 against 95), i.e. 2.4 times the measured
    # noise floor. The model reclassifies a place as a `product` rather than admit
    # it has no address: `destination` falls from 70 to 56, `product` rises from
    # 47 to 53.
    #
    # In other words, a required field is not free. The rule is not "make
    # everything required" but "make required what costs more by its absence than
    # by its constraint".
    #
    # `address` was removed from the schema in v9: measured at 0/133 in the
    # catalogue, optional it returned nothing and required it fabricated addresses.
    # `locality` replaces it — required, but it only asks for what the reel already
    # carries (a district, a landmark), so with nothing to invent, and _attested
    # discards whatever the source does not confirm.
    assert "address" not in extract.Candidate.model_fields, (
        "address a ete remplace par locality en v9"
    )
    assert "key_points" in extract.ExtractionResult.model_json_schema()["required"], (
        "la fiche doit toujours recevoir une liste, meme vide"
    )


def test_unattested_value_is_discarded():
    """The field changed in v9 (address -> locality), the risk did not: the model
    fills a location field with a plausible value rather than leaving it empty.
    "12 Rue de Lancry" had been copied onto a MUJI in Taipei.

    verify.py does not catch this case — it confronts the entity's NAME with the
    source, never its fields. Without this filter, a false locality travels to the
    catalogue fiche and to the Maps link with nothing to flag it."""
    ctx = "Caption: we tried the new spot, 12 Rue de Lancry in Paris 10eme."
    assert extract._attested("12 Rue de Lancry", ctx) == "12 Rue de Lancry"
    assert extract._attested("Rue de Lancry", ctx) == "Rue de Lancry"
    assert extract._attested("45 Avenue Foch, Lyon", ctx) is None
    assert extract._attested("", ctx) is None
    assert extract._attested(None, ctx) is None


def test_json_extraction_isolates_the_first_object():
    """Salvaged from the old vision.py, where it was written after a precise
    failure: a greedy brace regex captures up to the LAST brace in the text, so as
    soon as the model adds a comment after its JSON, `json.loads` fails with
    "Extra data" — 13 reels out of 38 on the first run.

    Only used in unconstrained mode: with `format=<schema>`, malformed output is
    structurally impossible."""
    import inference as llm

    assert llm.extract_json('Voici : {"a": 1} et {parasite}') == '{"a": 1}'
    assert llm.extract_json('{"a": {"b": 2}} apres') == '{"a": {"b": 2}}'
    # a closing brace INSIDE a string must not terminate the object
    assert llm.extract_json('{"x": "a } inside"}') == '{"x": "a } inside"}'
    # nor an escaped brace
    assert llm.extract_json(r'{"x": "\""}') == r'{"x": "\""}'
    with pytest.raises(ValueError):
        llm.extract_json("aucun json ici")
    with pytest.raises(ValueError):
        llm.extract_json('{"a": 1')


# ------------------------------------------------- replay after a tool change


def test_ocr_replays_the_corpus_when_the_reader_changes(conn):
    """Defect hit when moving from easyocr to RapidOCR: `_todo` looked only at
    the PRESENCE of a screen_text row, not its tool_version. The 60 reels already
    processed under easyocr therefore dropped out of the queue, and the new reader
    ran on none of them — a `reels ocr` that does nothing and has nothing to
    report, the most silent failure possible."""
    from pipeline.enrich import ocr

    upsert_reels(conn, [media("AAA")])
    conn.execute("INSERT INTO media(shortcode, mp4_path) VALUES ('AAA', '/x.mp4')")
    conn.execute(
        "INSERT INTO screen_text(shortcode, tool_version, text, created_at)"
        " VALUES ('AAA', 'un-vieux-lecteur', 'texte', '2026-01-01')"
    )
    db.mark(conn, "AAA", "ocr", "done")

    assert [s for s, _ in ocr._todo(conn, None)] == ["AAA"]

    conn.execute(
        "INSERT INTO screen_text(shortcode, tool_version, text, created_at)"
        " VALUES ('AAA', ?, 'texte', '2026-01-02')",
        (ocr.TOOL_VERSION,),
    )
    assert ocr._todo(conn, None) == []


def test_asr_replays_the_corpus_when_the_model_changes(conn, monkeypatch):
    """ASR, like OCR, is versioned input to extraction rather than a one-off."""
    from pipeline.enrich import asr

    upsert_reels(conn, [media("AAA")])
    conn.execute("INSERT INTO media(shortcode, mp4_path) VALUES ('AAA', '/tmp/a.mp4')")
    conn.execute(
        "INSERT INTO transcript(shortcode, tool_version, text, created_at)"
        " VALUES ('AAA', 'asr-v1', 'old', '2026-01-01')"
    )
    monkeypatch.setattr(asr, "TOOL_VERSION", "asr-v2")
    assert [s for s, _ in asr._todo(conn, None)] == ["AAA"]

    conn.execute(
        "INSERT INTO transcript(shortcode, tool_version, text, created_at)"
        " VALUES ('AAA', 'asr-v2', 'new', '2026-01-02')"
    )
    assert asr._todo(conn, None) == []


def test_re_extracting_spares_hand_entered_candidates(conn):
    """What `candidate.source` replaced: hand-entered entities used to live in a
    parallel extraction row (prompt_version='manuel'), structurally isolated from
    the automatic one so a re-run could not touch them. The isolation must survive
    the simplification — a correction is work the pipeline has no right to erase.
    """
    upsert_reels(conn, [media("AAA")])
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type, verified)"
        " VALUES ('AAA', 'llm', 'Yoridokoro', 'restaurant', 1)"
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type, verified)"
        " VALUES ('AAA', 'human', 'Kamakura Tanukian', 'restaurant', 1)"
    )

    # Exactly what extract.run does before writing a new pass.
    conn.execute(
        "DELETE FROM candidate WHERE shortcode = ? AND source = 'llm'", ("AAA",)
    )

    left = conn.execute("SELECT name, source FROM candidate").fetchall()
    assert [(r["name"], r["source"]) for r in left] == [("Kamakura Tanukian", "human")]


def test_extract_queues_a_reel_only_once(conn):
    """transcript and screen_text carry one row per tool_version. `_todo` joined
    them, so a reel with two OCR passes (easyocr then RapidOCR) came out of the
    queue twice and was extracted twice. INSERT OR REPLACE made the final result
    correct — hence the silence — but at the cost of doubling extraction time: 118
    extractions for 60 reels."""
    from pipeline.extract import extract as ex

    upsert_reels(conn, [media("AAA")])
    for version in ("asr-v1", "asr-v2"):
        conn.execute(
            "INSERT INTO transcript(shortcode, tool_version, created_at)"
            " VALUES ('AAA', ?, '2026-01-01')",
            (version,),
        )
    for version in ("ocr-v1", "ocr-v2", "ocr-v3"):
        conn.execute(
            "INSERT INTO screen_text(shortcode, tool_version, text, created_at)"
            " VALUES ('AAA', ?, 'x', '2026-01-01')",
            (version,),
        )

    assert ex._todo(conn, None, "a-fingerprint") == ["AAA"]


# ------------------------------------------------------------------ v9 ontology


def test_facets_are_a_closed_vocabulary():
    """`sub_category` was free while passing for a category: ~90 distinct values
    for 133 fiches, including `calme`, `bambous`, `petit matin`, all of it poured
    into tag as kind='controlled'. A controlled vocabulary that accepts any string
    is not controlled — the JSON schema has to enforce it, not a prompt instruction
    an 8B follows hit and miss."""
    schema = extract.ExtractionResult.model_json_schema()
    facets = schema["$defs"]["Candidate"]["properties"]["facets"]
    enum = facets["items"]["enum"]
    assert set(enum) == set(extract.FACETS)
    assert len(extract.FACETS) == len(set(extract.FACETS)), "facette en double"

    # `scale` admits the empty string: a product is not a place. That value must
    # stay INSIDE the enum rather than making the field optional — a field with a
    # default leaves `required` and the model omits it (measured: 100% omissions,
    # three times in a row on three different fields).
    scale = schema["$defs"]["Candidate"]["properties"]["scale"]["enum"]
    assert "" in scale and set(extract.SCALES) <= set(scale)


def test_unknown_enrichment_facets_become_tags(capfd):
    raw = json.dumps(
        {
            "entities": [
                {
                    "name": "Example",
                    "facets": ["coding", "design"],
                    "scale": "",
                    "city": "",
                    "country": "",
                    "locality": "",
                    "highlights": [],
                    "tags": ["tech"],
                    "why_saved": "outil utile",
                    "confidence": 0.8,
                }
            ]
        }
    )
    result = extract._parse_enrichment(raw)
    assert result.entities[0].facets == ["design"]
    assert result.entities[0].tags == ["tech", "coding"]
    assert "unknown facets moved to tags" in capfd.readouterr().err


def test_map_types_cover_old_and_new_names():
    """v9 renamed `destination` to `lieu`, v11 moved the vocabulary to English.
    A fiche resolved under an earlier vocabulary keeps its Maps link: stripping it
    would be an invisible regression, and the catalogue is not re-resolved from
    scratch every time the vocabulary moves."""
    from pipeline.catalogue import resolve

    for old_name in ("resto", "destination", "hebergement", "lieu"):
        assert old_name in resolve.TYPES_WITH_MAP
    for new_name in extract.PLACE_TYPES:
        assert new_name in resolve.TYPES_WITH_MAP


def test_catalog_records_disagreement_instead_of_hiding_it(conn):
    """resolve took `next(r for r in rows if r["city"])` — the first non-null
    value, in SQLite's arbitrary order. Two reels disagreeing about a place's city
    produced an arbitrary winner, with no trace, and the Maps link followed it.
    verify does not catch this case: it confronts only an entity's NAME with its
    source, never its fields."""
    from pipeline.catalogue import resolve

    lignes = [{"city": "Kamakura"}, {"city": "Kamakura"}, {"city": "Kanagawa"}]
    retenue, rejetes = resolve._majority(lignes, "city")
    assert retenue == "Kamakura"
    assert rejetes == ["Kanagawa"]

    # No value at all: no conflict to report, no false alarm.
    assert resolve._majority([{"city": None}, {"city": ""}], "city") == (None, [])


def test_alias_reattaches_an_old_name_to_its_fiche(conn):
    """A fiche's identity is (canonical_name, type), and _pick_canonical_name can
    elect a different name from one run to the next — '@muku.paris' becoming
    'Muku Paris'. The old fiche then had no candidate, so it was purged as an
    orphan. Aliases give the group a memory of its past spellings."""
    from pipeline.catalogue import resolve

    entity_id = conn.execute(
        "INSERT INTO entity(canonical_name, type, updated_at)"
        " VALUES ('Muku Paris', 'restaurant', '2026-01-01')"
    ).lastrowid
    conn.execute(
        "INSERT INTO entity_alias(entity_id, alias, source) VALUES (?, ?, 'candidate')",
        (entity_id, "muku paris"),
    )

    candidat = {"type": "restaurant", "name": "@muku.paris"}
    groupes = resolve._group_candidates(conn, [candidat])
    # normalize_name already strips the @; the alias must bring it back to the
    # existing group's canonical spelling, not found a new one.
    assert list(groupes) == [("restaurant", "muku paris")]


def test_scale_reserved_for_places():
    """The prompt says "leave it empty for non-places"; the model ignores it.
    Measured on the first v9 run: 30 non-place entities out of 62 came back with
    `scale='site'` — 8 brands, 8 media, 3 people. A clothing brand has no
    geographic scale, and letting it through would pollute the controlled
    vocabulary as `sub_category` did."""
    assert extract._scale_for_place("place", "city") == "city"
    assert extract._scale_for_place("restaurant", "site") == "site"
    assert extract._scale_for_place("brand", "site") is None
    assert extract._scale_for_place("person", "site") is None


def test_concatenated_name_is_split():
    """`Da0PnbIzoLp` went from 7 entities (v4) to 1 (v9) because the model
    returned `郁郁YùYù | 赤峰氣味日常體驗室` — two Taipei shops in a single `name`
    field. A formatting fault, not an extraction one: both halves are valid, so we
    split."""
    assert extract._split_names("郁郁YùYù | 赤峰氣味日常體驗室") == [
        "郁郁YùYù",
        "赤峰氣味日常體驗室",
    ]
    # The hyphen and the colon are part of real names: leave them alone.
    assert extract._split_names("Ay-Chung Flour-Rice Noodle") == [
        "Ay-Chung Flour-Rice Noodle"
    ]


def test_tags_do_not_restate_structured_fields():
    """The prompt forbids it, the model does it anyway: on `Yoridokoro`, the only
    tag produced was `JAPON` while the fiche already carried `country=Japon`. On
    v9, 8% of tags restated the city or country and 9% the name of an entity from
    the same reel."""
    e = extract.Candidate(
        name="Yoridokoro",
        name_latin="",
        type="restaurant",
        facets=["ramen"],
        scale="site",
        city="Kamakura",
        country="Japon",
        locality="",
        highlights=[],
        tags=["JAPON", "Kamakura", "ramen", "michelin", "Yoridokoro", "michelin"],
        why_saved="x",
        confidence=0.9,
        evidence=[],
    )
    # Only what no structured field already carried survives, deduplicated.
    assert extract._useful_tags(e.tags, e) == ["michelin"]


def test_tags_and_facets_normalised_not_names():
    """A vocabulary where `Randonnée` and `randonnee` coexist is not one. But
    NAMES keep their casing: that is what makes them readable, and the leading
    capital is the signal `compare` uses to separate a proper noun (`Kamakura`)
    from a common one (`bocal`)."""
    assert extract._norm_tag("Next Trip Japan") == "next trip japan"
    assert extract._norm_tag("Randonnée") == "randonnee"
    assert extract._norm_tag("street_food") == "street_food"


def test_review_queue_runs(conn):
    """`queue()` referred to a variable that no longer existed (`corriges`, left
    behind by the pass that put the codebase into English). Nothing raised until
    the function was actually called — and nothing ever called it: `reels review`
    and the web page `/?flag=a_revoir` both died with a NameError, in a module
    covered by no test at all.

    The point of this test is therefore not the return value but the call: any
    name that stops resolving inside `queue()` fails here."""
    from pipeline.extract import review

    upsert_reels(conn, [media("AAA")])
    conn.execute(
        "INSERT INTO candidate(shortcode, name, type, verified, verification_note)"
        " VALUES ('AAA', 'FKAFFETJ', 'restaurant', 0, 'OCR noise')"
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, name, type, verified, confidence)"
        " VALUES ('AAA', 'Yoridokoro', 'restaurant', 1, 0.4)"
    )

    items = review.queue(conn)
    reasons = {i["reason"] for i in items}
    assert reasons == {"rejected", "uncertain"}
    assert all(i["reason"] in review.REASON_LABELS for i in items)


def test_sampled_download_is_deterministic_and_counts_successes(conn, monkeypatch):
    """Two properties the draw has to have, and neither is free.

    Deterministic: without a recorded seed, "we added 20 reels at random" is not
    a fact anyone can re-check — the corpus stops being reproducible from the
    repository.

    Counted on SUCCESSES, not attempts: the older a reel, the likelier it has
    been deleted on Instagram since. Stopping after N attempts would silently
    return fewer videos than asked for, precisely on the part of the corpus the
    draw exists to reach."""
    from pipeline.capture import download as dl

    upsert_reels(conn, [media(f"R{i:03d}") for i in range(40)])
    monkeypatch.setattr(dl.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.setattr(dl, "write_cookie_file", lambda path: path)

    attempted: list[str] = []

    def fake_download(shortcode, url, cookies, timeout=180):
        attempted.append(shortcode)
        # Every other reel is gone, as an old corpus would be.
        if len(attempted) % 2 == 0:
            return 200, None, ""
        return 404, None, "deleted on Instagram"

    monkeypatch.setattr(dl, "download_one", fake_download)
    stats = dl.download(conn, sample=5, seed=42, delay=(0, 0))

    assert stats["ok"] == 5, "must stop on 5 successes, not 5 attempts"
    assert len(attempted) == 10

    # Same seed, same draw — on a fresh database, since the first run marked
    # every reel it touched.
    other = db.connect(":memory:")
    db.initialize(other)
    upsert_reels(other, [media(f"R{i:03d}") for i in range(40)])
    attempted.clear()
    dl.download(other, sample=5, seed=42, delay=(0, 0))
    first_five = list(attempted)
    attempted.clear()

    third = db.connect(":memory:")
    db.initialize(third)
    upsert_reels(third, [media(f"R{i:03d}") for i in range(40)])
    dl.download(third, sample=5, seed=42, delay=(0, 0))
    assert attempted == first_five


def test_download_can_retry_only_requested_shortcodes(conn, monkeypatch, tmp_path):
    from pipeline.capture import download as dl

    upsert_reels(conn, [media("AAA"), media("BBB")])
    monkeypatch.setattr(dl.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.setattr(dl, "write_cookie_file", lambda path: path)
    output = tmp_path / "target.mp4"
    output.write_bytes(b"video")
    attempted = []
    monkeypatch.setattr(
        dl,
        "download_one",
        lambda shortcode, *args: attempted.append(shortcode) or (200, output, ""),
    )

    stats = dl.download(conn, shortcodes=["BBB"], delay=(0, 0))

    assert stats["ok"] == 1
    assert attempted == ["BBB"]


def test_download_retries_done_stage_when_file_is_missing(conn, monkeypatch, tmp_path):
    from pipeline.capture import download as dl

    upsert_reels(conn, [media("STALE")])
    conn.execute(
        "INSERT INTO media(shortcode, mp4_path) VALUES ('STALE', ?)",
        (str(tmp_path / "removed.mp4"),),
    )
    conn.execute(
        "INSERT INTO stage_state(shortcode, stage, status, attempts, updated_at) "
        "VALUES ('STALE', 'download', 'done', 1, 'now')"
    )
    conn.commit()

    monkeypatch.setattr(dl.shutil, "which", lambda _: "/usr/bin/yt-dlp")
    monkeypatch.setattr(dl, "write_cookie_file", lambda path: path)
    output = tmp_path / "new.mp4"
    output.write_bytes(b"video")
    monkeypatch.setattr(dl, "download_one", lambda *args, **kwargs: (200, output, ""))
    stats = dl.download(conn, delay=(0, 0))
    assert stats["ok"] == 1


# ------------------------------------------------------- quality heuristics


def test_common_noun_heuristic_does_not_punish_cjk():
    """The heuristic behind the quality axis: a name without a single capital is
    almost certainly a common noun, not an entity. Counting entities without
    judging them rewards junk — a run promoting `bocal`, `persil` and `festaurant`
    beat one that refused them, and that wrong conclusion stood for hours.

    The trap, caught by this test when it was first written: `葱油饼 ==
    葱油饼.lower()` is true, so every CJK name scored as a common noun. The bias
    would be systematic and directed — against precisely the reels the OCR reads
    best."""
    from pipeline.extract.heuristics import is_common_noun

    assert is_common_noun("bocal")
    assert is_common_noun("horizontal pull")
    assert not is_common_noun("Kamakura")
    assert not is_common_noun("Ay-Chung Flour-Rice Noodle")
    # Caseless scripts must escape the criterion, not fail it.
    assert not is_common_noun("葱油饼")
    assert not is_common_noun("よる")


def test_attestation_heuristic_reads_all_three_sources():
    """`festaurant`, `kalios GA`, `FKAFFETJ`: OCR fragments promoted to entities.
    The check is literal presence in the caption, the audio or the OCR — the free,
    rule-based version of what verify.py pays an LLM call per candidate for."""
    from pipeline.extract.heuristics import is_attested

    sources = "un ramen a taipei \n on va chez yoridokoro \n ay-chung flour-rice"
    assert is_attested(sources, "Yoridokoro")
    assert is_attested(sources, "Ay-Chung Flour-Rice")
    assert not is_attested(sources, "festaurant")


# ------------------------------------------------- guards added after judging
# the run of 2026-08-24 (var/quality/2026-08-24-judgement.md). Each test carries
# the real candidate that motivated its rule: a guard whose motivating case is
# not written down is a guard nobody dares remove later.


def test_facets_stop_at_three_and_never_enumerate_a_family():
    """`#108 'I Parry Everything'` came back with 20 facets: the entire `nature`
    family, listed twice. `#55 '100 Meters'` with the entire `food` family. This
    is the closed vocabulary's own failure mode — a model with nothing to say used
    to invent a sub_category, now it enumerates the list it was handed."""
    nature = list(extract.FACETS_BY_FAMILY["nature"]) * 2
    kept = extract._facets_for_type("media", nature)
    assert kept == [], "nature facets say nothing about a film"

    kept = extract._facets_for_type("place", nature)
    assert len(kept) <= extract.MAX_FACETS
    assert len(kept) == len(set(kept)), "duplicates are pure noise in a closed list"


def test_facets_from_an_unrelated_family_are_dropped():
    """The reach into a family the type has no use for: `Fuji Rock Festival`,
    typed `transport`, came back with `museum, forest`.

    This test also records what the rule does NOT do, because the boundary is the
    interesting part. A sake brewery given `beauty, clothing, outdoor` keeps them:
    it is a `shop`, and `object` facets are legitimate for a shop — a MUJI store
    is a shop where `beauty` and `clothing` are exactly right. Family membership
    cannot separate the two. That class of error needs the prompt, or a different
    mechanism; claiming this rule fixes it would be claiming too much."""
    assert extract._facets_for_type("transport", ["museum", "forest"]) == []
    # An exercise takes body facets and nothing else.
    assert extract._facets_for_type("exercise", ["sushi", "back", "temple"]) == ["back"]
    # A market is a place that legitimately carries food facets: the mapping is
    # generous on purpose, it refuses the reach into an unrelated family.
    assert extract._facets_for_type("place", ["market", "street_food"]) == [
        "market",
        "street_food",
    ]
    # The boundary, asserted rather than described: these survive.
    assert extract._facets_for_type("shop", ["beauty", "clothing", "outdoor"]) == [
        "beauty",
        "clothing",
        "outdoor",
    ]


def test_location_emptied_where_it_cannot_mean_anything():
    """29% of candidates carried a `city` on a non-place type and 14% had
    `city == country` — the `city='France'` case, 26 fiches. Both feed _gmaps_url,
    which then looks up `Gorges du Tarn, France, France`."""
    # A recipe has no city; its country of origin is worth keeping.
    assert extract._location_for_type("recipe", "Paris", "France", "11e") == (
        None,
        "France",
        None,
    )
    # city == country: the country was copied into the city field.
    assert extract._location_for_type("place", "France", "France", None) == (
        None,
        "France",
        None,
    )
    # A real place keeps everything.
    assert extract._location_for_type("restaurant", "Kamakura", "Japon", "Hase") == (
        "Kamakura",
        "Japon",
        "Hase",
    )


def test_locality_refuses_debris():
    """`'7'`, `'3'`, `'11'`: arrondissement numbers stripped of their city, which
    sharpen nothing. And `'1ER ARRONDISSEMENT, 4E ARRONDISSEMENT, 860 m, 75005'`,
    four OCR fragments including a distance. A wrong locality is worse than none:
    it is appended to the Maps query and drags it away from the place."""
    assert extract._clean_locality("7") is None
    assert (
        extract._clean_locality("1ER ARRONDISSEMENT, 4E ARRONDISSEMENT, 860 m, 75005")
        is None
    )
    assert extract._clean_locality("860 m") is None
    assert extract._clean_locality("Asakusa") == "Asakusa"
    assert extract._clean_locality("Paris 11e") == "Paris 11e"
    assert (
        extract._clean_locality("12 Rue de Lancry, 75010") == "12 Rue de Lancry, 75010"
    )


def test_instagram_handle_is_not_a_name():
    """11 candidates were named with a raw @handle — `@muku.paris` typed
    `restaurant`, `@olivieblake` typed `media`. The Maps link becomes unusable and
    the fiche reads as a handle."""
    assert extract._clean_name("@meeko_shoes") == "Meeko Shoes"
    assert extract._clean_name("@muku.paris") == "Muku Paris"
    # Casing inside a word is preserved: only the first letter is forced up.
    assert extract._clean_name("@theOrdinary") == "TheOrdinary"
    assert (
        extract._clean_name("Ay-Chung Flour-Rice Noodle")
        == "Ay-Chung Flour-Rice Noodle"
    )


def test_the_word_unknown_is_an_empty_value():
    """The model does not always answer "empty" with an empty string: it writes
    the word. Four candidates came back with `city='Inconnu'`. Left alone,
    `Inconnu` becomes a city of the catalogue and a `controlled` tag."""
    assert extract._blank_to_none("Inconnu") is None
    assert extract._blank_to_none("unknown") is None
    assert extract._blank_to_none("  ") is None
    assert extract._blank_to_none("Tokyo") == "Tokyo"


def test_write_candidates_spares_hand_entered_entities(conn):
    """The one line that replaced a whole parallel extraction row. If it ever
    widens to every candidate, hand-entered work disappears silently on the next
    extraction — which is exactly what the old structure existed to prevent."""
    upsert_reels(conn, [media("AAA", caption={"text": "Yoridokoro a Kamakura"})])
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type, verified)"
        " VALUES ('AAA', 'human', 'Kamakura Tanukian', 'restaurant', 1)"
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('AAA', 'llm', 'stale', 'restaurant')"
    )

    result = extract.ExtractionResult(
        is_actionable=True,
        topic="t",
        why_saved="w",
        confidence=0.9,
        mode="recommandation",
        tags=[],
        key_points=[],
        entities=[
            extract.Candidate(
                name="Yoridokoro",
                name_latin="",
                type="restaurant",
                facets=["noodles"],
                scale="site",
                city="Kamakura",
                country="Japon",
                locality="",
                highlights=[],
                tags=[],
                why_saved="w",
                confidence=0.9,
                evidence=[
                    extract.Evidence(
                        source="caption",
                        quote="Yoridokoro",
                        match="literal",
                        location="",
                    )
                ],
            )
        ],
    )
    kept = extract.write_candidates(conn, "AAA", result, "Yoridokoro à Kamakura")

    assert len(kept) == 1
    rows = conn.execute(
        "SELECT name, source, evidence_json, evidence_status FROM candidate ORDER BY source"
    ).fetchall()
    assert [(r["name"], r["source"]) for r in rows] == [
        ("Kamakura Tanukian", "human"),
        ("Yoridokoro", "llm"),
    ]
    assert json.loads(rows[1]["evidence_json"])[0]["quote"] == "Yoridokoro"
    assert rows[1]["evidence_status"] == "literal"


def test_evidence_is_validated_against_its_declared_source(conn):
    upsert_reels(conn, [media("AAA", caption={"text": "Yoridokoro a Kamakura"})])
    conn.execute(
        "INSERT INTO transcript(shortcode, tool_version, text, created_at)"
        " VALUES ('AAA', 'v', 'Yoridokoro', '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO screen_text(shortcode, tool_version, text, observations_json, created_at)"
        " VALUES ('AAA', 'v', 'Yoridokoro', '[{\"frame\": \"f0001\", \"text\": \"Yoridokoro\"}]', '2026-01-01')"
    )
    literal = extract.Candidate(
        name="Yoridokoro",
        name_latin="",
        type="restaurant",
        facets=[],
        scale="site",
        city="Kamakura",
        country="Japon",
        locality="",
        highlights=[],
        tags=[],
        why_saved="w",
        confidence=0.9,
        evidence=[
            extract.Evidence(
                source="ocr", quote="Yoridokoro", match="literal", location="f0001"
            )
        ],
    )
    assert extract.validate_evidence(conn, "AAA", literal) == ("literal", None)

    unsupported = literal.model_copy(
        update={
            "name": "Unknown Shop",
            "evidence": [
                extract.Evidence(
                    source="caption", quote="Yoridokoro", match="literal", location=""
                )
            ],
        }
    )
    status, note = extract.validate_evidence(conn, "AAA", unsupported)
    assert status == "invalid" and "contain" in (note or "")

    phonetic = literal.model_copy(
        update={
            "name": "Yorido Koro",
            "evidence": [
                extract.Evidence(
                    source="transcript",
                    quote="Yoridokoro",
                    match="phonetic",
                    location="",
                )
            ],
        }
    )
    assert extract.validate_evidence(conn, "AAA", phonetic)[0] == "phonetic"


def test_legacy_ocr_evidence_falls_back_to_aggregate_text(conn):
    """Pre-frame OCR remains a valid source, without inventing a frame location."""
    upsert_reels(conn, [media("AAA")])
    conn.execute(
        "INSERT INTO screen_text(shortcode, tool_version, text, created_at)"
        " VALUES ('AAA', 'v', 'Yoridokoro', '2026-01-01')"
    )
    candidate = extract.Candidate(
        name="Yoridokoro",
        name_latin="",
        type="restaurant",
        facets=[],
        scale="site",
        city="",
        country="",
        locality="",
        highlights=[],
        tags=[],
        why_saved="w",
        confidence=0.9,
        evidence=[
            extract.Evidence(
                source="ocr", quote="Yoridokoro", match="literal", location="f0001"
            )
        ],
    )
    status, _, kept = extract._assess_evidence(conn, "AAA", candidate)
    assert status == "literal" and kept[0].location == ""


def test_mislabelled_evidence_is_repaired_only_when_the_name_is_literal(conn):
    upsert_reels(conn, [media("AAA", caption={"text": "Fujiyama Glass"})])
    candidate = extract.Candidate(
        name="Fujiyama Glass",
        name_latin="",
        type="product",
        facets=[],
        scale="",
        city="",
        country="",
        locality="",
        highlights=[],
        tags=[],
        why_saved="w",
        confidence=0.9,
        evidence=[
            extract.Evidence(
                source="ocr", quote="Fujiyama Glass", match="literal", location=""
            )
        ],
    )
    status, note, kept = extract._assess_evidence(conn, "AAA", candidate)
    assert status == "literal" and "repaired" in (note or "")
    assert kept[0].source == "caption" and kept[0].quote == "Fujiyama Glass"


def test_article_only_name_variant_is_sent_to_targeted_verification(conn):
    upsert_reels(
        conn,
        [
            media(
                "AAA",
                caption={"text": "Les Pertes de la Valserine - Bellegarde"},
            )
        ],
    )
    candidate = extract.Candidate(
        name="Pertes de Valserine",
        name_latin="",
        type="place",
        facets=[],
        scale="site",
        city="",
        country="",
        locality="",
        highlights=[],
        tags=[],
        why_saved="w",
        confidence=0.9,
        evidence=[
            extract.Evidence(
                source="caption", quote="Bellegarde", match="literal", location=""
            )
        ],
    )
    status, note, kept = extract._assess_evidence(conn, "AAA", candidate)
    assert status == "variant" and "caption" in (note or "") and kept == []


def test_verify_accepts_valid_literal_evidence_without_an_llm_call(conn, monkeypatch):
    """A local fact must not pay for — or risk — a second model judgement."""
    from pipeline.extract import verify

    upsert_reels(conn, [media("AAA", caption={"text": "Yoridokoro a Kamakura"})])
    result = extract.ExtractionResult(
        is_actionable=True,
        topic="t",
        why_saved="w",
        confidence=0.9,
        mode="recommandation",
        tags=[],
        key_points=[],
        entities=[
            extract.Candidate(
                name="Yoridokoro",
                name_latin="",
                type="restaurant",
                facets=[],
                scale="site",
                city="Kamakura",
                country="Japon",
                locality="",
                highlights=[],
                tags=[],
                why_saved="w",
                confidence=0.9,
                evidence=[
                    extract.Evidence(
                        source="caption",
                        quote="Yoridokoro",
                        match="literal",
                        location="",
                    )
                ],
            )
        ],
    )
    extract.write_candidates(conn, "AAA", result, "Yoridokoro a Kamakura")
    monkeypatch.setattr(
        verify.llm, "client", lambda: pytest.fail("LLM must not be called")
    )

    stats = verify.run(conn)
    row = conn.execute("SELECT verified, verification_note FROM candidate").fetchone()
    assert stats["deterministic"] == 1
    assert row["verified"] == 1 and "literal evidence" in row["verification_note"]


def test_failed_extraction_keeps_its_raw_attempt(conn, monkeypatch):
    """A malformed/truncated completion is diagnosis data, not just a retry."""
    upsert_reels(conn, [media("AAA")])
    for table in ("transcript", "screen_text"):
        conn.execute(
            f"INSERT INTO {table}(shortcode, tool_version, text, created_at)"
            f" VALUES ('AAA', 'v', 'text', '2026-01-01')"
        )
    monkeypatch.setattr(extract.llm, "client", lambda: object())
    monkeypatch.setattr(extract.llm, "unload", lambda *args: None)
    monkeypatch.setattr(
        extract,
        "extract_staged",
        lambda *args: (_ for _ in ()).throw(ValueError('{"entities": []}')),
    )

    stats = extract.run(conn, model="test-model")
    attempt = conn.execute(
        "SELECT model, raw_response, ok, error, generation_json FROM extraction_attempt"
    ).fetchone()
    assert stats["failed"] == 1
    assert attempt["model"] == "test-model" and attempt["raw_response"] is None
    assert attempt["ok"] == 0 and attempt["error"]
    assert json.loads(attempt["generation_json"])["num_ctx"] > 0


def test_handle_cleanup_changes_readability_not_grouping():
    """A rule is worth what it actually does. `_clean_name` was written believing
    it might re-merge `@muku.paris` with `Muku`; it does not, and cannot —
    `normalize_name` already strips the `@` and the dot, so both spellings had the
    same blocking key all along.

    The real corpus DID merge those two, through `entity_alias` remembering an
    earlier spelling. Attributing that to this rule would be crediting it with
    someone else's work, and would hide that the handle problem is still open."""
    from pipeline.catalogue import resolve

    assert resolve.normalize_name("@muku.paris") == resolve.normalize_name("Muku Paris")
    # ... and neither reaches the fiche actually named Muku.
    assert resolve.normalize_name("Muku Paris") != resolve.normalize_name("Muku")


def test_media_has_a_family_of_its_own_and_only_that_one():
    """18 of 21 media entities carried a facet from an unrelated family — `Bungo
    Stray Dogs` given `sushi, museum, design`. The model was not being sloppy: the
    field is required and no family described a film, so it reached for whichever
    it saw first, and twice enumerated an entire one.

    The family gives it a correct answer; the restriction is what stops the
    reaching from returning once a correct answer exists."""
    assert extract._facets_for_type("media", ["anime", "series"]) == ["anime", "series"]
    assert extract._facets_for_type("media", ["sushi", "museum", "anime"]) == ["anime"]
    # And the media vocabulary stays out of everything else.
    assert extract._facets_for_type("restaurant", ["anime", "ramen"]) == ["ramen"]


# -------------------------------------------------------------- reel library


def test_reel_library_is_a_reading_view_over_the_existing_records(conn, tmp_path):
    """A fiche has one source of truth: extraction + media, not a copied note."""
    upsert_reels(conn, [media("LIB", caption={"text": "Marinade soja gingembre"})])
    local_root = tmp_path / "media"
    video = local_root / "LIB" / "LIB.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"not a real video")
    conn.execute(
        "INSERT INTO media(shortcode, mp4_path) VALUES ('LIB', ?)", (str(video),)
    )
    conn.execute(
        "INSERT INTO extraction(shortcode, model, prompt_sha, extracted_at, mode, tags_json, key_points_json)"
        " VALUES ('LIB', 'qwen3:8b', 'test', '2026-01-01', 'repertoire', ?, ?)",
        (
            json.dumps(["recette", "marinade"]),
            json.dumps(["Mariner avec sauce soja et gingembre."]),
        ),
    )
    conn.execute(
        "INSERT INTO classification(shortcode, predicted_topic, is_actionable, why_saved, confidence, created_at)"
        " VALUES ('LIB', 'Marinade soja gingembre', 1, 'Retrouver la marinade.', .9, '2026-01-01')"
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type, highlights_json, verified)"
        " VALUES ('LIB', 'llm', 'Marinade soja gingembre', 'recipe', ?, 1)",
        (json.dumps(["Base soja et gingembre."]),),
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type, verified)"
        " VALUES ('LIB', 'human', 'Marinade soja gingembre', 'recipe', 1)"
    )

    fiche = library.get(conn, "LIB")
    assert fiche and fiche["topic"] == "Marinade soja gingembre"
    assert fiche["key_points"] == ["Mariner avec sauce soja et gingembre."]
    assert len(fiche["candidates"]) == 1
    assert library.local_media_path(fiche, local_root) == video.resolve()


def test_catalogue_keeps_products_but_not_recipe_content_from_repertoire(conn):
    """A recipe reel is a fiche; a named purchasable product inside it survives."""
    from pipeline.catalogue import resolve

    upsert_reels(conn, [media("LIBCAT")])
    conn.execute(
        "INSERT INTO extraction(shortcode, model, prompt_sha, extracted_at, mode)"
        " VALUES ('LIBCAT', 'qwen3:8b', 'test', '2026-01-01', 'repertoire')"
    )
    for name, type_ in (
        ("Marinade", "recipe"),
        ("Etirement", "exercise"),
        ("Decoupe", "method"),
        ("Poele Takumi", "product"),
    ):
        conn.execute(
            "INSERT INTO candidate(shortcode, source, name, type, verified)"
            " VALUES ('LIBCAT', 'llm', ?, ?, 1)",
            (name, type_),
        )

    resolve.run(conn, force=True)
    names = [
        row["canonical_name"]
        for row in conn.execute("SELECT canonical_name FROM entity")
    ]
    assert names == ["Poele Takumi"]


def test_discovery_name_survives_an_incomplete_enrichment():
    fiche = extract.FicheResult(
        is_actionable=True,
        topic="guide",
        why_saved="w",
        confidence=0.8,
        mode="recommandation",
        tags=["kyoto"],
        key_points=[],
        content_kind="",
    )
    discovery = extract.DiscoveryResult(
        mentions=[
            extract.Mention(
                name="Adashino Nenbutsuji Temple",
                name_latin="",
                type="place",
                direct=True,
                evidence=[
                    extract.Evidence(
                        source="caption",
                        quote="Adashino Nenbutsuji Temple",
                        match="literal",
                        location="",
                    )
                ],
            )
        ]
    )
    result = extract._combine(fiche, discovery, extract.EnrichmentResult(entities=[]))
    assert [entity.name for entity in result.entities] == ["Adashino Nenbutsuji Temple"]
    assert result.entities[0].type == "place"


def test_entity_lookup_key_is_deterministic_without_rewriting_observed_name():
    assert canonicalization.normalised_key("Café de l'Étoile") == "cafe de l etoile"
    assert canonicalization.normalised_key("  Café-de l'Étoile  ") == "cafe de l etoile"
    assert canonicalization.normalised_key("葱油饼") == "葱油饼"


def test_discovery_duplicates_are_removed_before_enrichment_tokens():
    evidence = [
        extract.Evidence(
            source="caption", quote="Yoridokoro", match="literal", location=""
        )
    ]
    mentions = [
        extract.Mention(
            name="Yoridokoro",
            name_latin="",
            type="place",
            direct=True,
            evidence=evidence,
        ),
        extract.Mention(
            name="yoridokoro",
            name_latin="",
            type="place",
            direct=True,
            evidence=evidence,
        ),
        extract.Mention(
            name="Yoridokoro",
            name_latin="",
            type="shop",
            direct=True,
            evidence=evidence,
        ),
        extract.Mention(
            name="Yoridokoro Annex",
            name_latin="",
            type="place",
            direct=True,
            evidence=evidence,
        ),
    ]
    kept = extract._dedupe_mentions(mentions)
    assert [(item.type, item.name) for item in kept] == [
        ("place", "Yoridokoro"),
        ("shop", "Yoridokoro"),
        ("place", "Yoridokoro Annex"),
    ]


def test_hermes_backend_uses_bounded_cli_and_is_unstructured(monkeypatch):
    calls = []

    class Completed:
        returncode = 0
        stdout = '{"ok": true}'
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return Completed()

    monkeypatch.setattr(llm.subprocess, "run", fake_run)
    monkeypatch.setenv("REELS_HERMES_REASONING", "low")
    monkeypatch.setenv("REELS_HERMES_EXECUTABLE", "hermes-test")
    client = llm.HermesClient()
    raw = llm.chat_json(client, "", "system", "user", {"type": "object"})
    assert raw == '{"ok": true}'
    command, kwargs = calls[0]
    assert command[:3] == ["hermes-test", "chat", "-q"]
    assert command[3].startswith("system\n\nThe JSON object must match this schema")
    assert command[3].endswith("\n\nUSER DOSSIER:\nuser")
    assert "--reasoning" in command and "low" in command
    assert kwargs["timeout"] == 900


def test_codex_backend_parses_agent_message_and_usage(monkeypatch):
    class Completed:
        returncode = 0
        stdout = (
            '{"type":"item.completed","item":{"type":"agent_message",'
            '"text":"{\\"ok\\":true}"}}\n'
            '{"type":"turn.completed","usage":{"input_tokens":12,'
            '"cached_input_tokens":4,"output_tokens":3,"reasoning_output_tokens":1}}\n'
        )
        stderr = ""

    calls = []
    monkeypatch.setattr(
        llm.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or Completed(),
    )
    monkeypatch.setenv("REELS_CODEX_REASONING", "low")
    client = llm.CodexClient()
    raw = llm.chat_json(client, "gpt-5.6-luna", "system", "user", {"type": "object"})
    assert raw == '{"ok":true}'
    assert "--ignore-rules" in calls[0] and "gpt-5.6-luna" in calls[0]
    assert llm.usage()["input_tokens"] == 12
    assert llm.usage()["cached_input_tokens"] == 4


def test_codex_is_default_backend_and_backends_are_selectable(monkeypatch):
    monkeypatch.delenv("REELS_LLM_BACKEND", raising=False)
    assert config.llm_backend() == "codex"
    monkeypatch.setenv("REELS_LLM_BACKEND", "hermes")
    assert config.llm_backend() == "hermes"
    monkeypatch.setenv("REELS_LLM_BACKEND", "api")
    assert config.llm_backend() == "api"
    monkeypatch.setenv("REELS_LLM_BACKEND", "ollama")
    assert config.llm_backend() == "ollama"


def test_api_backend_parses_openai_compatible_response(monkeypatch):
    class Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {
                "choices": [{"message": {"content": '{"ok":true}'}}],
                "usage": {"prompt_tokens": 11, "completion_tokens": 4},
            }

    calls = []
    monkeypatch.setattr(
        llm.requests,
        "post",
        lambda *args, **kwargs: calls.append((args, kwargs)) or Response(),
    )
    monkeypatch.setenv("REELS_API_BASE_URL", "https://example.test/v1")
    monkeypatch.setenv("REELS_API_KEY", "secret")
    client = llm.ApiClient()
    raw = llm.chat_json(client, "dedicated-model", "system", "user", {"type": "object"})
    assert raw == '{"ok":true}'
    assert calls[0][0][0] == "https://example.test/v1/chat/completions"
    assert calls[0][1]["headers"]["Authorization"] == "Bearer secret"


def test_recipe_content_is_repertoire_and_ignores_package_only_products():
    fiche = extract.FicheResult(
        is_actionable=True,
        topic="repertoire",
        why_saved="w",
        confidence=0.8,
        mode="recommandation",
        tags=[],
        key_points=[],
        content_kind="recipe",
    )
    discovery = extract.DiscoveryResult(
        mentions=[
            extract.Mention(
                name="Fresh Udon",
                name_latin="",
                type="product",
                direct=True,
                evidence=[
                    extract.Evidence(
                        source="ocr",
                        quote="Fresh Udon",
                        match="literal",
                        location="f0001",
                    )
                ],
            )
        ]
    )
    recipes = extract.ContentIndexResult(
        recipes=[
            extract.RecipeCard(
                dish_name="Udon curry",
                cuisine="japonaise",
                cuisine_family="asiatique",
                course="plat",
                dietary_tags=[],
                summary="w",
                confidence=0.8,
                evidence=[
                    extract.Evidence(
                        source="caption",
                        quote="Udon curry",
                        match="literal",
                        location="",
                    )
                ],
            )
        ]
    )
    result = extract._combine(
        fiche, discovery, extract.EnrichmentResult(entities=[]), recipes
    )
    assert (
        result.mode == "repertoire"
        and result.topic == "Udon curry"
        and result.entities == []
    )


def test_staged_raw_response_can_be_replayed_by_guards():
    fiche = {
        "is_actionable": True,
        "topic": "guide",
        "why_saved": "w",
        "confidence": 0.8,
        "mode": "recommandation",
        "tags": [],
        "key_points": [],
    }
    discovery = {
        "mentions": [
            {
                "name": "HAY HAY",
                "name_latin": "",
                "type": "restaurant",
                "direct": True,
                "evidence": [
                    {
                        "source": "caption",
                        "quote": "HAY HAY",
                        "match": "literal",
                        "location": "",
                    }
                ],
            }
        ]
    }
    enrichment = {
        "entities": [
            {
                "name": "HAY HAY",
                "facets": [],
                "scale": "site",
                "city": "",
                "country": "",
                "locality": "",
                "brand": None,
                "intention": None,
                "highlights": [],
                "tags": [],
                "why_saved": "w",
                "confidence": 0.8,
            }
        ]
    }
    raw = json.dumps({"fiche": fiche, "discovery": discovery, "enrichment": enrichment})
    assert extract.parse_stored_response(raw).entities[0].name == "HAY HAY"


def test_recall_regression_looks_at_qwen_not_manual_corrections(conn):
    upsert_reels(conn, [media("REG")])
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('REG', 'human', 'Hasedera', 'place')"
    )
    cases = [{"shortcode": "REG", "expected_names": ["Hasedera", "Ginza Kagari"]}]
    assert regression.missing_llm_names(conn, cases) == [
        {"shortcode": "REG", "names": ["Hasedera", "Ginza Kagari"]}
    ]
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('REG', 'llm', 'Hasedera', 'place')"
    )
    assert regression.missing_llm_names(conn, cases) == [
        {"shortcode": "REG", "names": ["Ginza Kagari"]}
    ]


def test_name_canonicalization_uses_known_attested_hay_hay_and_preserves_observation():
    evidence = [
        {
            "source": "ocr",
            "quote": "HAY HAY home shopping",
            "match": "literal",
            "location": "f0001",
        }
    ]
    decision = canonicalization.decide(
        "HAY HAY home shoppng", evidence, local_names=["HAY HAY"]
    )
    assert decision.status == "resolved"
    assert decision.canonical_name == "HAY HAY"
    assert decision.observed_name == "HAY HAY home shoppng"
    assert decision.method == "known_name_in_priority_evidence"

    # The same exact, explainable spelling rule works when the short form is not
    # already present in the local candidate set.
    rule_decision = canonicalization.decide(
        "HAY HAY home shopping", evidence, entity_type="shop"
    )
    assert rule_decision.canonical_name == "HAY HAY"
    assert rule_decision.method == "repeated_brand_before_home_shopping"


def test_name_canonicalization_abstains_on_phonetic_hokokuji_without_authority():
    evidence = [
        {
            "source": "transcript",
            "quote": "les bambous de Okokujji",
            "match": "literal",
        },
        {"source": "ocr", "quote": "cle Hbk_kuc &i", "match": "literal"},
    ]
    decision = canonicalization.decide("Okokujji", evidence)
    assert decision.status == "abstained"
    assert decision.canonical_name is None
    assert decision.observed_name == "Okokujji"
    assert decision.method == "asr_only"


def test_name_canonicalization_prefers_caption_over_conflicting_asr():
    decision = canonicalization.decide(
        "Okokujji",
        [
            {"source": "transcript", "quote": "Okokujji", "match": "literal"},
            {"source": "caption", "quote": "Hokokuji", "match": "literal"},
        ],
        local_names=["Hokokuji"],
    )
    assert decision.status == "resolved"
    assert decision.canonical_name == "Hokokuji"
    assert decision.explanation.startswith("La forme canonique 'Hokokuji'")


def test_name_canonicalization_does_not_let_an_adapter_override_written_spelling():
    class Authority:
        def suggest(self, observed_name, *, locality=None):
            return canonicalization.AuthoritySuggestion(
                canonical_name="Other Temple", authority="test", confidence=0.99
            )

    decision = canonicalization.decide(
        "Hokokuji",
        [{"source": "caption", "quote": "Hokokuji"}],
        entity_type="place",
        authority=Authority(),
    )
    assert decision.status == "resolved"
    assert decision.canonical_name == "Hokokuji"


def test_name_canonicalization_abstains_on_a_low_confidence_authority_suggestion():
    class Authority:
        def suggest(self, observed_name, *, locality=None):
            return canonicalization.AuthoritySuggestion(
                canonical_name="Hokokuji", authority="test", confidence=0.7
            )

    decision = canonicalization.decide(
        "Okokujji",
        [{"source": "transcript", "quote": "Okokujji"}],
        entity_type="place",
        authority=Authority(),
    )
    assert decision.status == "abstained"
    assert decision.canonical_name is None
    assert decision.method == "low_confidence_authority:test"


def test_name_canonicalization_persists_observation_and_decision_separately(conn):
    upsert_reels(conn, [media("CANON", caption={"text": "Hokokuji"})])
    candidate_id = conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type, evidence_json) "
        "VALUES ('CANON', 'llm', 'Okokujji', 'place', ?) "
        "RETURNING id",
        (
            json.dumps(
                [
                    {
                        "source": "transcript",
                        "quote": "Okokujji",
                        "match": "phonetic",
                        "location": "",
                    }
                ]
            ),
        ),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type) "
        "VALUES ('CANON', 'llm', 'Hokokuji', 'place')"
    )

    decision = canonicalization.record_candidate(conn, candidate_id)
    stored = conn.execute(
        "SELECT observed_name, canonical_name, evidence_json, status "
        "FROM candidate_name_resolution WHERE candidate_id=?",
        (candidate_id,),
    ).fetchone()
    assert decision.canonical_name == "Hokokuji"
    assert stored["observed_name"] == "Okokujji"
    assert stored["canonical_name"] == "Hokokuji"
    evidence = json.loads(stored["evidence_json"])
    assert any(
        item["source"] == "caption" and item["quote"] == "Hokokuji" for item in evidence
    )


def test_blind_regression_scores_only_qwen_and_reports_false_positives_and_time(conn):
    upsert_reels(conn, [media("BLIND")])
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('BLIND', 'human', 'Invented by hand', 'product')"
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('BLIND', 'llm', 'Invented by Qwen', 'product')"
    )
    raw = json.dumps(
        {
            "is_actionable": False,
            "mode": "recommandation",
            "content_kind": "",
            "entities": [],
        }
    )
    conn.execute(
        "INSERT INTO extraction(shortcode, model, prompt_sha, extracted_at, raw_response,"
        " mode, content_kind, key_points_json, ok)"
        " VALUES ('BLIND', 'qwen3:8b', 'prompt-a', 'now', ?,"
        " 'recommandation', NULL, '[\"日本語 phrase\"]', 1)",
        (raw,),
    )
    conn.execute(
        "INSERT INTO extraction_attempt(shortcode, model, prompt_sha, context_sha,"
        " generation_json, started_at, completed_at, duration_ms, ok)"
        " VALUES ('BLIND', 'qwen3:8b', 'prompt-a', 'ctx-a', '{}', 'now', 'now', 1234, 1)"
    )
    cases = [
        {
            "shortcode": "BLIND",
            "category": "non_actionnable",
            "expected_entities": [],
            "entity_scope": "complete",
            "expected_mode": "recommandation",
            "expected_content_kind": "",
            "expected_actionable": False,
            "expected_recipes": [],
            "expected_terms": ["日本語"],
        }
    ]

    report = regression.score(conn, cases)

    assert report["false_positives"] == 1
    assert report["false_positive_items"] == [
        {"shortcode": "BLIND", "name": "Invented by Qwen", "type": "product"}
    ]
    assert report["cases"][0]["duration_ms"] == 1234
    assert report["canonicalized_entities_found"] == report["entities_found"]
    assert report["canonicalized_false_positives"] == report["false_positives"]
    assert report["cases"][0]["actionable_correct"] is True
    assert report["mode_correct"] == report["content_kind_correct"] == 1
    assert report["terms_expected"] == report["terms_found"] == 1


def test_recipe_index_is_queryable_and_keeps_personal_attempts(conn):
    upsert_reels(conn, [media("FOOD")])
    repertoire.upsert_entry(
        conn,
        shortcode="FOOD",
        content_kind="recipe",
        title="Dîner asiatique",
        summary="Deux plats à refaire",
        recipes=[
            {
                "dish_name": "Poulet karaage",
                "cuisine": "japonaise",
                "cuisine_family": "asiatique",
                "course": "plat",
                "dietary_tags": ["frit"],
                "summary": "Poulet mariné.",
                "evidence": [],
                "confidence": 0.9,
            },
            {
                "dish_name": "Salade verte",
                "cuisine": "française",
                "cuisine_family": "européenne",
                "course": "entree",
                "dietary_tags": [],
                "summary": "Simple.",
                "evidence": [],
                "confidence": 0.8,
            },
        ],
    )
    asian = repertoire.recipes(conn, cuisine="asiatique")
    assert [item["dish_name"] for item in asian] == ["Poulet karaage"]
    recipe_id = asian[0]["id"]
    repertoire.record_attempt(
        conn,
        recipe_id,
        verdict="favorite",
        rating=5,
        note="À refaire",
        changes_made="moins de sucre",
    )
    assert repertoire.recipes(conn, verdict="favorite")[0]["note"] == "À refaire"

    # A changed model output retires the old recipe from proposals but preserves
    # the cooking history instead of cascading it away.
    repertoire.upsert_entry(
        conn,
        shortcode="FOOD",
        content_kind="recipe",
        title="Dîner asiatique",
        summary="Une recette",
        recipes=[],
    )
    assert repertoire.recipes(conn, cuisine="asiatique") == []
    assert (
        conn.execute(
            "SELECT COUNT(*) FROM recipe_attempt WHERE recipe_id = ?", (recipe_id,)
        ).fetchone()[0]
        == 1
    )


def test_recipe_flows_from_fresh_extraction_to_queryable_index(conn, monkeypatch):
    """The production seam, exercised on an empty initialized database.

    The model is faked only at the LLM call site: every materialisation, FTS and
    recipe query uses the normal implementation.
    """
    upsert_reels(conn, [media("SOUP", caption={"text": "Une soupe japonaise"})])
    for table in ("transcript", "screen_text"):
        conn.execute(
            f"INSERT INTO {table}(shortcode, tool_version, text, created_at)"
            f" VALUES ('SOUP', 'v', 'Soupe miso japonaise', '2026-01-01')"
        )
    responses = iter(
        [
            json.dumps(
                {
                    "is_actionable": True,
                    "topic": "Soupe miso",
                    "why_saved": "Recette rapide.",
                    "confidence": 0.9,
                    "mode": "repertoire",
                    "tags": ["soupe"],
                    "key_points": ["Préparer le bouillon."],
                    "content_kind": "recipe",
                }
            ),
            json.dumps(
                {
                    "recipes": [
                        {
                            "dish_name": "Soupe miso",
                            "cuisine": "japonaise",
                            "cuisine_family": "asiatique",
                            "course": "plat",
                            "dietary_tags": ["végétarien"],
                            "summary": "Soupe au miso.",
                            "evidence": [
                                {
                                    "source": "transcript",
                                    "quote": "Soupe miso japonaise",
                                    "match": "literal",
                                    "location": "",
                                }
                            ],
                            "confidence": 0.9,
                        }
                    ]
                }
            ),
        ]
    )
    monkeypatch.setattr(extract.llm, "client", lambda: object())
    monkeypatch.setattr(extract.llm, "unload", lambda *args: None)
    monkeypatch.setattr(
        extract.llm, "chat_json", lambda *args, **kwargs: next(responses)
    )

    assert extract.run(conn, model="fake")["ok"] == 1
    result = repertoire.recipes(conn, cuisine="asiatique")
    assert result[0]["dish_name"] == "Soupe miso"
    raw = conn.execute(
        "SELECT raw_response, content_kind FROM extraction WHERE shortcode = 'SOUP'"
    ).fetchone()
    assert raw["content_kind"] == "recipe" and "content_index" in raw["raw_response"]


def _product_fixture(conn, name="Objectif lumineux"):
    upsert_reels(
        conn, [media("LENS", caption={"text": "Objectif conseillé pour la photo"})]
    )
    candidate_id = conn.execute(
        """INSERT INTO candidate(shortcode,source,name,type,brand,intention,
                                 highlights_json,why_saved,evidence_json,verified)
           VALUES ('LENS','llm',?,'product','Optique Exemple','photo de nuit',
                   ?, 'bonne qualité en basse lumière', ?,1) RETURNING id""",
        (
            name,
            '["léger"]',
            json.dumps(
                [
                    {
                        "source": "caption",
                        "quote": "objectif lumineux conseillé",
                        "match": "literal",
                    }
                ]
            ),
        ),
    ).fetchone()[0]
    entity_id = conn.execute(
        "INSERT INTO entity(canonical_name,type,facets_json,highlights,why_saved,updated_at)"
        " VALUES (?,'product','[]','léger','bonne qualité en basse lumière','2026-09-12') RETURNING id",
        (name,),
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entity_reel(entity_id,shortcode,candidate_id) VALUES (?, 'LENS', ?)",
        (entity_id, candidate_id),
    )
    conn.execute(
        "INSERT INTO entity_fts(rowid,canonical_name,summary,highlights,tags)"
        " VALUES (?,?,'','léger','photo objectif')",
        (entity_id, name),
    )
    conn.commit()
    return entity_id


def test_product_feedback_is_separate_append_only_and_queryable(conn):
    entity_id = _product_fixture(conn)
    assert catalog_feedback.products(conn)[0]["status"] is None
    catalog_feedback.update(
        conn, entity_id, status="considering", note="À comparer", score=3
    )
    catalog_feedback.update(
        conn,
        entity_id,
        status="shortlisted",
        note="Poids raisonnable",
        score=4,
        need="Photographier en basse lumière",
    )
    matching = catalog_feedback.products(conn, "photo", status="shortlisted")
    assert [row["id"] for row in matching] == [entity_id]
    assert matching[0]["brand"] == "Optique Exemple"
    assert (
        matching[0]["sources"][0]["evidence"][0]["quote"]
        == "objectif lumineux conseillé"
    )
    history = catalog_feedback.history(conn, entity_id)
    assert [row["status"] for row in history] == ["considering", "shortlisted"]
    catalog_feedback.clear_status(conn, entity_id)
    assert catalog_feedback.products(conn)[0]["status"] is None
    assert [row["status"] for row in catalog_feedback.history(conn, entity_id)] == [
        "considering",
        "shortlisted",
        None,
    ]


def test_product_feedback_survives_catalog_rebuild_for_same_source_entity(conn):
    entity_id = _product_fixture(conn)
    catalog_feedback.update(
        conn, entity_id, status="shortlisted", note="Historique important"
    )
    # Re-resolving verified source facts refreshes entity and entity_reel only.
    catalog_resolve.run(conn)
    assert (
        conn.execute(
            "SELECT status FROM entity_personal WHERE entity_id=?", (entity_id,)
        ).fetchone()[0]
        == "shortlisted"
    )
    assert (
        catalog_feedback.history(conn, entity_id)[0]["note"] == "Historique important"
    )


def test_video_proxy_command_uses_a_valid_single_filter_expression(tmp_path):
    command = video_archive._proxy_command(
        tmp_path / "source.mp4", tmp_path / "proxy.mp4", 720
    )
    assert command[command.index("-vf") + 1] == "scale=-2:min(720\\,ih)"


def test_verify_may_not_reject_a_name_that_is_literally_there(conn, monkeypatch):
    """Over two runs, 6 of verify's 11 rejections named something plainly present
    in the source while asserting it was absent — `Soul Eater` and `Fruits Basket`,
    both listed in their reel's caption. Five valid entities were destroyed by one
    pass.

    Asking an 8B "does this string occur in this text" is asking it to do a
    substring test, which `in` does better. The override is one-sided on purpose:
    `is_attested` returning True is a fact, returning False is only a hint (a
    transcript spells foreign names by ear), so the model stays sovereign in the
    direction it was written for."""
    from pipeline.extract import verify

    upsert_reels(conn, [media("AAA", caption={"text": "jujutsu kaisen, soul eater"})])
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('AAA', 'llm', 'Soul Eater', 'media')"
    )
    conn.execute(
        "INSERT INTO candidate(shortcode, source, name, type)"
        " VALUES ('AAA', 'llm', 'FKAFFETJ', 'restaurant')"
    )

    monkeypatch.setattr(verify.llm, "client", lambda: object())
    monkeypatch.setattr(verify.llm, "unload", lambda *a: None)
    monkeypatch.setattr(
        verify,
        "verify_one",
        lambda *a, **k: verify.Verification(
            attested=False, note="n'apparait pas dans la source"
        ),
    )

    stats = verify.run(conn)
    verdicts = dict(conn.execute("SELECT name, verified FROM candidate").fetchall())
    assert verdicts["Soul Eater"] == 1, "it is in the caption; the model is overruled"
    assert verdicts["FKAFFETJ"] == 0, "absent from the source: the model decides"
    assert stats["overridden"] == 1


def test_a_common_noun_is_only_a_defect_where_a_proper_noun_is_expected():
    """The correction that matters most to the instrument itself. Of the 38 names
    this criterion flagged on the run of 2026-08-24, 37 sat on `product`,
    `recipe`, `exercise` or `service` — types that name a thing by WHAT IT IS.
    `raviolis sans pliage` is the correct name of a recipe; `standing desk` of a
    product; `reverse plank` of an exercise. Exactly one flag was a real defect.

    Applied blind, the heuristic reports the catalogue's own vocabulary as a
    fault, and it made me write "17% of names are common nouns" into a judgement
    as though that were a problem. Scoped, it says something."""
    from pipeline.extract.heuristics import is_common_noun

    # Correct names that used to be flagged.
    assert not is_common_noun("raviolis sans pliage", "recipe")
    assert not is_common_noun("standing desk", "product")
    assert not is_common_noun("reverse plank", "exercise")
    # A place, a shop or a brand does have a proper noun.
    assert is_common_noun("bocal", "product") is False
    assert is_common_noun("festaurant", "restaurant") is True
    assert is_common_noun("cielétoilé", "media") is True
    assert not is_common_noun("Kamakura", "place")


def test_a_directory_tree_is_not_a_catalogue():
    """Da2g9a0OZAT, a reel about organising a repository: 12 of its 14 entities
    were `agents/`, `hooks/`, `runbook.md`, `validate-bash.sh`. They passed every
    other guard — attested, not prices, type in the enum — and alone doubled the
    corpus count of `service`.

    Judged on the COUNT, not on each name: one filename is a subject worth
    cataloguing, a dozen is a screenshot of a tree."""

    def entity(name):
        return extract.Candidate(
            name=name,
            name_latin="",
            type="service",
            facets=[],
            scale="",
            city="",
            country="",
            locality="",
            highlights=[],
            tags=[],
            why_saved="w",
            confidence=0.9,
            evidence=[],
        )

    tree = [
        entity(n)
        for n in (
            "agents/",
            "hooks/",
            "rules/",
            "runbook.md",
            "data-model.md",
            "validate-bash.sh",
        )
    ]
    assert extract._drop_file_tree(tree) == []

    # One filename among real entities is left alone: that reel is ABOUT it.
    lone = [entity("CLAUDE.md"), entity("Cursor"), entity("Zed")]
    assert extract._drop_file_tree(lone) == lone


def test_asr_model_uses_cuda_by_default(monkeypatch):
    from pipeline.enrich import asr

    captured = {}
    monkeypatch.setenv("REELS_ASR_DEVICE", "cuda")
    monkeypatch.setattr(
        asr, "_configure_cuda_runtime", lambda: captured.setdefault("cuda", True)
    )
    monkeypatch.setattr(
        asr,
        "WhisperModel",
        lambda name, **kwargs: captured.update(name=name, **kwargs) or object(),
    )

    asr._model()

    assert captured == {
        "cuda": True,
        "name": "large-v3",
        "device": "cuda",
        "compute_type": "float16",
    }


def test_asr_model_accepts_explicit_cpu_fallback(monkeypatch):
    from pipeline.enrich import asr

    captured = {}
    monkeypatch.setenv("REELS_ASR_DEVICE", "cpu")
    monkeypatch.setattr(
        asr,
        "WhisperModel",
        lambda name, **kwargs: captured.update(name=name, **kwargs) or object(),
    )

    asr._model()

    assert captured == {"name": "large-v3", "device": "cpu", "compute_type": "int8"}


def test_a_video_without_audio_is_a_silent_reel_not_a_failure(tmp_path, monkeypatch):
    """Three reels of the older corpus are VIDEO ONLY — yt-dlp fell back to `best`
    and returned VP9 with no audio stream — and faster-whisper dies on them with
    `tuple index out of range`, which says nothing about the cause.

    Counted as failures they carry no `transcript` row, and extract._todo requires
    one: the reels drop out of the corpus silently rather than being treated as
    what they are, reels without speech. 0 of 100 in the recent corpus, 3 of 100
    in the older one — the format changed, not the code."""
    from pipeline.enrich import asr

    monkeypatch.setattr(asr, "has_audio_stream", lambda path: False)
    calls = []

    def explode(model, path):
        calls.append(path)
        raise IndexError("tuple index out of range")

    monkeypatch.setattr(asr, "transcribe_one", explode)
    monkeypatch.setattr(asr, "_model", lambda: object())
    monkeypatch.setattr(
        asr, "_todo", lambda conn, limit: [("AAA", str(tmp_path / "a.mp4"))]
    )

    conn = db.connect(":memory:")
    db.initialize(conn)
    upsert_reels(conn, [media("AAA")])
    stats = asr.run(conn)

    assert stats == {"ok": 0, "no_speech": 1, "failed": 0}
    row = conn.execute("SELECT has_speech, text FROM transcript").fetchone()
    assert row["has_speech"] == 0 and row["text"] == ""

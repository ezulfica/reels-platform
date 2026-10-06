-- Complete schema for a new reels-platform database.
-- This snapshot replaces the historical incremental migrations.

-- Source: 001_initial.sql
-- Initial schema: bronze / silver / gold.
--
-- Convention: the business key is the Instagram `shortcode` everywhere. v1
-- declared an FK on reels(id) = "{pk}_{userid}" while the data stored the
-- shortcode, so the entity_reels -> reels JOIN matched nothing.

-- ------------------------------------------------------------- BRONZE (immutable)

CREATE TABLE bronze_reel (
    shortcode     TEXT PRIMARY KEY,
    ig_id         TEXT,
    url           TEXT NOT NULL,
    username      TEXT,
    caption       TEXT,
    taken_at      INTEGER,           -- epoch UTC
    media_type    INTEGER,           -- 1=image 2=video 8=carousel
    product_type  TEXT,              -- 'clips' => reel
    kind          TEXT,              -- reel | post | video | carousel (derive)
    raw_json      TEXT,              -- reponse API brute, source de verite
    first_seen_at TEXT NOT NULL,
    last_seen_at  TEXT NOT NULL,
    unsaved_at    TEXT               -- non-NULL = absent du feed = desenregistre
);
CREATE INDEX idx_bronze_reel_username ON bronze_reel(username);
CREATE INDEX idx_bronze_reel_taken_at ON bronze_reel(taken_at);
CREATE INDEX idx_bronze_reel_unsaved  ON bronze_reel(unsaved_at) WHERE unsaved_at IS NULL;

CREATE TABLE bronze_media (
    shortcode     TEXT PRIMARY KEY REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    mp4_path      TEXT,
    bytes         INTEGER,
    duration_s    REAL,
    downloaded_at TEXT,
    http_status   INTEGER            -- 404 = supprime chez Instagram, != echec reseau
);

CREATE TABLE bronze_collection (
    collection_id TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    item_count    INTEGER,
    synced_at     TEXT
);

-- The human label: which collection YOU filed this reel under.
CREATE TABLE bronze_reel_collection (
    shortcode     TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    collection_id TEXT NOT NULL REFERENCES bronze_collection(collection_id) ON DELETE CASCADE,
    PRIMARY KEY (shortcode, collection_id)
);
CREATE INDEX idx_brc_collection ON bronze_reel_collection(collection_id);

CREATE TABLE bronze_reel_context (
    shortcode             TEXT PRIMARY KEY REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    location_json         TEXT,
    usertags_json         TEXT,
    music_json            TEXT,
    coauthors_json        TEXT,
    accessibility_caption TEXT,      -- description auto-generee par Meta
    hashtags              TEXT,      -- JSON array, extracted from the caption
    mentions              TEXT,      -- JSON array
    links                 TEXT,      -- JSON array
    has_list_marker       INTEGER DEFAULT 0   -- "all the locations below" => multi-entites
);

-- --------------------------------------------------- SILVER / enrich (costly, cached)

CREATE TABLE silver_asr (
    shortcode    TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    tool_version TEXT NOT NULL,
    lang         TEXT,
    text         TEXT,
    segments_json TEXT,
    has_speech   INTEGER DEFAULT 1,  -- 0 = music only, do not retry
    created_at   TEXT NOT NULL,
    PRIMARY KEY (shortcode, tool_version)
);

CREATE TABLE silver_ocr (
    shortcode    TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    tool_version TEXT NOT NULL,
    text         TEXT,
    frames_used  INTEGER,
    created_at   TEXT NOT NULL,
    PRIMARY KEY (shortcode, tool_version)
);

-- The VLM understands the scene, where OCR only reads characters.
CREATE TABLE silver_vision (
    shortcode         TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    tool_version      TEXT NOT NULL,
    model             TEXT,
    scene_description TEXT,
    text_in_context   TEXT,
    frames_used       INTEGER,
    created_at        TEXT NOT NULL,
    PRIMARY KEY (shortcode, tool_version)
);

-- ------------------------------------------------------------------- SILVER / dedup

CREATE TABLE silver_media_hash (
    shortcode         TEXT PRIMARY KEY REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    phash_frames      TEXT,          -- JSON array de hashes perceptuels
    audio_fp          TEXT,
    transcript_simhash TEXT,
    created_at        TEXT
);

CREATE TABLE silver_dupe_cluster (
    cluster_id   TEXT NOT NULL,
    shortcode    TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    is_canonical INTEGER DEFAULT 0,
    method       TEXT,               -- phash | audio | simhash | caption
    score        REAL,
    PRIMARY KEY (cluster_id, shortcode)
);
CREATE INDEX idx_dupe_shortcode ON silver_dupe_cluster(shortcode);

-- --------------------------------------------- SILVER / extract (LLM, replayable)

CREATE TABLE silver_extraction (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode      TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    prompt_version TEXT NOT NULL,
    model          TEXT NOT NULL,
    extracted_at   TEXT NOT NULL,
    raw_response   TEXT,
    ok             INTEGER DEFAULT 1,
    error          TEXT
);
CREATE UNIQUE INDEX idx_extraction_unique ON silver_extraction(shortcode, prompt_version, model);

-- A reel yields 0..N entities: a meme has none, a compilation has ten.
CREATE TABLE silver_candidate (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id INTEGER NOT NULL REFERENCES silver_extraction(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    type          TEXT NOT NULL,
    sub_category  TEXT,
    city          TEXT,
    country       TEXT,
    address       TEXT,
    brand         TEXT,
    intention     TEXT,
    why_saved     TEXT,
    confidence    REAL,
    agreement     INTEGER,           -- agreeing passes out of N (self-consistency)
    evidence_json TEXT
);
CREATE INDEX idx_candidate_extraction ON silver_candidate(extraction_id);
CREATE INDEX idx_candidate_type ON silver_candidate(type);

CREATE TABLE silver_classification (
    shortcode           TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    prompt_version      TEXT NOT NULL,
    model               TEXT NOT NULL,
    predicted_topic     TEXT,
    predicted_tags_json TEXT,
    is_actionable       INTEGER,
    why_saved           TEXT,
    confidence          REAL,
    created_at          TEXT NOT NULL,
    PRIMARY KEY (shortcode, prompt_version, model)
);

-- ------------------------------------------------------------------------- GOLD

CREATE TABLE gold_entity (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL,
    type           TEXT NOT NULL,
    sub_category   TEXT,
    city           TEXT,
    country        TEXT,
    address        TEXT,
    gmaps_url      TEXT,
    summary        TEXT,
    highlights     TEXT,
    external_links TEXT,
    why_saved      TEXT,
    embedding      BLOB,
    verified       INTEGER DEFAULT 0,   -- 1 = confirmed by hand
    updated_at     TEXT NOT NULL
);
CREATE INDEX idx_entity_type ON gold_entity(type);
CREATE INDEX idx_entity_city ON gold_entity(city);

CREATE TABLE gold_entity_reel (
    entity_id      INTEGER NOT NULL REFERENCES gold_entity(id) ON DELETE CASCADE,
    shortcode      TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    candidate_id   INTEGER REFERENCES silver_candidate(id) ON DELETE SET NULL,
    relevance_note TEXT,
    PRIMARY KEY (entity_id, shortcode)
);
CREATE INDEX idx_ger_shortcode ON gold_entity_reel(shortcode);

CREATE TABLE gold_tag (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    label TEXT NOT NULL UNIQUE,
    kind  TEXT NOT NULL            -- collection | controlled | free
);

CREATE TABLE gold_entity_tag (
    entity_id  INTEGER NOT NULL REFERENCES gold_entity(id) ON DELETE CASCADE,
    tag_id     INTEGER NOT NULL REFERENCES gold_tag(id) ON DELETE CASCADE,
    source     TEXT,
    confidence REAL,
    PRIMARY KEY (entity_id, tag_id)
);

CREATE VIRTUAL TABLE gold_entity_fts USING fts5(
    canonical_name, summary, highlights, tags,
    content='',                    -- index externe, alimente explicitement
    tokenize='unicode61 remove_diacritics 2'
);

-- --------------------------------------------------- Improvement loop

-- The few-shot pool: what "learns" in place of the model's weights.
CREATE TABLE fewshot_example (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode    TEXT REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    context_json TEXT NOT NULL,
    expected_json TEXT NOT NULL,
    origin       TEXT NOT NULL,     -- collection | correction | curated
    active       INTEGER DEFAULT 1,
    added_at     TEXT NOT NULL
);
CREATE INDEX idx_fewshot_active ON fewshot_example(active, origin);

CREATE TABLE gold_entity_correction (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id    INTEGER REFERENCES gold_entity(id) ON DELETE CASCADE,
    field        TEXT NOT NULL,
    old_value    TEXT,
    new_value    TEXT,
    corrected_at TEXT NOT NULL
);

-- ----------------------------------------------------------------- orchestration

-- One row per (reel, step): this is what makes each step independently resumable
-- and makes v1's batch_worker_* family unnecessary, where `status` stayed frozen
-- on 'to_review' for all 1340 reels.
CREATE TABLE stage_state (
    shortcode  TEXT NOT NULL REFERENCES bronze_reel(shortcode) ON DELETE CASCADE,
    stage      TEXT NOT NULL,       -- download | asr | ocr | vision | hash | extract
    status     TEXT NOT NULL,       -- pending | done | failed | skipped
    attempts   INTEGER DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (shortcode, stage)
);
CREATE INDEX idx_stage_pending ON stage_state(stage, status);


-- Source: 002_gold_unicity.sql
-- A Gold entity is identified by (canonical name, type): two rows sharing both
-- are the same entity. Makes the reference import replayable and states the
-- invariant that entity resolution will have to maintain.
CREATE UNIQUE INDEX idx_gold_entity_identity
    ON gold_entity(canonical_name, type);


-- Source: 003_verification.sql
-- Verification pass: a second LLM call confronts each candidate with the source
-- text and rejects whatever is not attested, rather than trusting the first
-- extraction. `verified` is NULL until the pass has run (a state distinct from
-- "rejected").
ALTER TABLE silver_candidate ADD COLUMN verified INTEGER;          -- NULL=not verified yet, 1=attested, 0=rejected
ALTER TABLE silver_candidate ADD COLUMN verification_note TEXT;    -- pourquoi rejete, si rejete


-- Source: 004_highlights.sql
-- The concrete details a reel gives about an entity ("the bamboo that lets the
-- light dance", "a lunch facing the passing train") had nowhere to go: a Gold
-- fiche could only carry name/type/why_saved, hence fiches that were correct but
-- empty. gold_entity.highlights had existed since 001 but was never fed, for lack
-- of a source on the Silver side.
-- JSON array of short strings, like predicted_tags_json.
ALTER TABLE silver_candidate ADD COLUMN highlights_json TEXT;


-- Source: 005_ontologie_v9.sql
-- v9: three classification axes where there was only one.
--
-- `sub_category` was a free text field presented as a category: ~90 distinct
-- values for 133 fiches, 62 to 70% singletons across every prompt version, and it
-- fed gold_tag with kind='controlled' — the catalogue's "controlled" vocabulary
-- contained `calme`, `bambous`, `petit matin`. It is replaced by:
--
--   facets_json  CLOSED list (cf. extract.FACETS), enforced by the JSON schema
--   echelle      city | district | site, for places only
--   locality     the complement that sharpens a Maps query (replaces address)
--
-- The `sub_category` and `address` columns are KEPT, not dropped: they carry the
-- v1 to v8 extractions already in the database, which the catalogue may still
-- select for a given reel, and `address` remains an optional place field.

ALTER TABLE silver_candidate ADD COLUMN facets_json TEXT;
ALTER TABLE silver_candidate ADD COLUMN echelle     TEXT;
ALTER TABLE silver_candidate ADD COLUMN locality    TEXT;

ALTER TABLE gold_entity ADD COLUMN facets_json    TEXT;
ALTER TABLE gold_entity ADD COLUMN echelle        TEXT;
ALTER TABLE gold_entity ADD COLUMN locality       TEXT;
-- Disagreements between reels about one entity, instead of hiding them.
-- resolve.run() took the first non-null value that came, silently: two reels
-- disagreeing about a place's city produced an arbitrary winner and no trace. This
-- is the catalogue-side counterpart of the known extraction-side defect — verify
-- confronts only an entity's NAME with its source, never its fields.
ALTER TABLE gold_entity ADD COLUMN conflicts_json TEXT;

-- The other ways of naming one entity. Blocking runs on the normalised name, so
-- 葱油饼 (read by RapidOCR on one reel) and "scallion pancake" (written in
-- another's caption) would never meet. The problem was BORN with the move to
-- RapidOCR: while the OCR returned approximate latin, every variant was latin and
-- similar enough to fall into the same group.
--
-- (v11 largely closes this: a candidate now carries both spellings through
-- name_latin, so both become aliases of the same fiche.)
CREATE TABLE gold_entity_alias (
    entity_id INTEGER NOT NULL REFERENCES gold_entity(id) ON DELETE CASCADE,
    alias     TEXT NOT NULL,          -- forme normalisee (cf. normalize_name)
    source    TEXT NOT NULL,          -- 'candidate' | 'manuel'
    PRIMARY KEY (entity_id, alias)
);
CREATE INDEX idx_alias_lookup ON gold_entity_alias(alias);


-- Source: 006_tags_entite_et_nom_latin.sql
-- v10: two additions to the candidate.
--
-- name_latin: the readable latin form of the name, when the reel shows it next to
-- the original script — which it almost always does (`葱油饼 SCALLION PANCAKE`,
-- `よる YORU`). Since RapidOCR the model often keeps the CJK alone, and the
-- catalogue becomes unreadable. We store something attested, not a computed
-- transliteration: `unidecode` would give `cong you bing`, neither the original
-- nor useful, and `verify` could not check it.
--
-- tags_json: keywords move down from the REEL to the ENTITY. At reel level (v9),
-- a guide-style reel showing 8 places stamped the same 5 words on all 8 — 234
-- labels for 293 occurrences, 201 seen exactly once, with a head of the list
-- redundant with city/country/type/facets. classification.predicted_tags_json is
-- kept for v1-v9 runs but is no longer fed.

ALTER TABLE silver_candidate ADD COLUMN name_latin TEXT;
ALTER TABLE silver_candidate ADD COLUMN tags_json  TEXT;

ALTER TABLE gold_entity ADD COLUMN name_latin TEXT;


-- Source: 007_menage.sql
-- Cleanup: dead tables and rows.
--
-- Four tables never served, or no longer serve, all at 0 rows and without a single
-- reference in the code outside migrations:
--
--   silver_media_hash       perceptual-hash deduplication, measured at only 26%
--                           savings and abandoned (cf. ocr.py)
--   silver_dupe_cluster     the counterpart of the previous one
--   gold_entity_correction  never implemented; manual corrections go through a
--                           dedicated extraction (prompt_version='manuel'), not
--                           through a separate table
--   fewshot_example         never fed, yet COUNTED by db.counts(): `reels status`
--                           therefore displayed a counter frozen at 0
--
-- And the remains of the vision step, removed several sessions ago after
-- measurement (of the corpus's 296 verified entities, the VLM's output attested
-- only 3 on its own):
--
--   silver_vision           12 rows, still read by web/app.py for a reel's detail
--                           page — a display talking about a step that no longer
--                           exists
--   stage_state             14 rows with stage='vision'
--
-- The mp4s and the capture rows are untouched: nothing here is a source.

DROP TABLE IF EXISTS silver_media_hash;
DROP TABLE IF EXISTS silver_dupe_cluster;
DROP TABLE IF EXISTS gold_entity_correction;
DROP TABLE IF EXISTS fewshot_example;
DROP TABLE IF EXISTS silver_vision;

DELETE FROM stage_state WHERE stage = 'vision';


-- Source: 008_renommage_tables.sql
-- The tables carried the name of their layer (bronze_/silver_/gold_), which said
-- which floor you were on but not what happened there. The folders became
-- step1_capture / step2_enrich / step3_extract / step4_catalog; without this
-- rename the code would say `catalog` while every SQL query said `gold`, which is
-- worse than either on its own.
--
-- No step prefix on the tables: the ordering is readable in the folder tree, and
-- repeating it in the database would add nothing while freezing the decomposition
-- into data.
--
-- SQLite propagates a RENAME to the indexes and foreign keys referencing the
-- table (>= 3.25; we run 3.47). No triggers in this schema. The shadow tables of
-- entity_fts (_data, _idx, _docsize, _config) follow the rename of the FTS5 table
-- itself.
--
-- Migrations 001-006 are not rewritten: on a fresh database they create the old
-- names and this one renames them. Odd to read, but deterministic — and the
-- project rule is that an applied migration is never rewritten.

ALTER TABLE bronze_reel            RENAME TO reel;
ALTER TABLE bronze_media           RENAME TO media;
ALTER TABLE bronze_collection      RENAME TO collection;
ALTER TABLE bronze_reel_collection RENAME TO reel_collection;
ALTER TABLE bronze_reel_context    RENAME TO reel_context;

ALTER TABLE silver_asr             RENAME TO transcript;
ALTER TABLE silver_ocr             RENAME TO screen_text;
ALTER TABLE silver_classification  RENAME TO classification;
ALTER TABLE silver_extraction      RENAME TO extraction;
ALTER TABLE silver_candidate       RENAME TO candidate;

ALTER TABLE gold_entity            RENAME TO entity;
ALTER TABLE gold_entity_alias      RENAME TO entity_alias;
ALTER TABLE gold_entity_reel       RENAME TO entity_reel;
ALTER TABLE gold_entity_tag        RENAME TO entity_tag;
ALTER TABLE gold_entity_fts        RENAME TO entity_fts;
ALTER TABLE gold_tag               RENAME TO tag;


-- Source: 009_candidats_orphelins.sql
-- Repairs two orphaned candidates: they reference a deleted extraction.
--
-- Owned origin: the v10 test extraction (a single reel, produced with a prompt
-- predating the tightened instructions) was deleted by hand through the `sqlite3`
-- client, which does NOT enable `PRAGMA foreign_keys` by default. So candidate's
-- ON DELETE CASCADE never ran, and its two candidates outlived their extraction.
-- `db.connect()` does enable the pragma; going through the CLI bypassed the
-- guarantee.
--
-- They are unreachable (every read goes through a join on extraction), so they
-- have no effect on the catalogue — but `PRAGMA foreign_key_check` reports them,
-- and a schema that lies about its integrity eventually costs.
--
-- No-op on a fresh database.

DELETE FROM candidate
WHERE extraction_id NOT IN (SELECT id FROM extraction);


-- Source: 010_scale_column.sql
-- `echelle` -> `scale`: the repository moves entirely to English, code and
-- vocabulary included. This was the schema's only French-named column.
ALTER TABLE candidate RENAME COLUMN echelle TO scale;
ALTER TABLE entity    RENAME COLUMN echelle TO scale;


-- Source: 011_one_extraction_per_reel.sql
-- One prompt, one model, one row per reel.
--
-- The tables being rebuilt here were shaped by a need that is no longer the
-- everyday need: comparing prompts and models against each other. That need
-- produced five concepts, all of which disappear with this migration.
--
--   prompt_version          a label kept up to date by hand, which twice lied
--                           (two different prompts both labelled "v2")
--   the +t2 / +libre suffix  several draws of one config coexisting
--   SELECTED_EXTRACTION      the query deciding WHICH of a reel's N extractions
--                            is authoritative — needed only because there were N
--   extraction 'manuel'      a fake extraction row whose only job was to give
--                            hand-entered candidates somewhere to hang
--   candidate.extraction_id  an indirection to recover a shortcode we already had
--
-- Comparing two prompts stays possible; it becomes a deliberate act, done by an
-- ad hoc script whose conclusion is a commit, rather than a permanent data
-- structure that every query has to navigate around.
--
-- What replaces the version label: two provenance stamps, `prompt_sha` (the
-- fingerprint already computed by extract.prompt_fingerprint) and `code_sha`
-- (git). Nothing to name, nothing to maintain — and a reel is re-extracted when
-- its prompt_sha differs from the current one, so editing the prompt replays the
-- corpus without anyone having to remember to bump anything.
--
-- DATA: this deletes 901 extractions and 2169 candidates (v1 -> v10, qwen3.5:9b,
-- deepseek-r1:7b, and the +easy / +libre / +t2 draws). They were about to be
-- replaced by a full re-extraction in any case: the move to English changed the
-- stored vocabulary, hence the JSON schema, hence every one of them. The single
-- hand-entered candidate is carried over.

-- 1. The catalogue is derived, entirely, from candidates that are about to
-- disappear. Emptying it here rather than leaving 171 fiches pointing at nothing
-- is the difference between a catalogue that is empty and one that lies. All 171
-- are verified=0 (none hand-confirmed) and every link carries a candidate_id, so
-- `reels catalog` rebuilds the lot after the next extraction.
DELETE FROM entity_tag;
DELETE FROM entity_reel;
DELETE FROM entity_alias;
DELETE FROM entity;
DELETE FROM tag;
-- 'delete-all' rather than DELETE FROM: refused on a contentless FTS5 table.
INSERT INTO entity_fts(entity_fts) VALUES('delete-all');

-- 2. candidate: keyed by the reel, with the origin as a column.
--
-- `source` is what replaces the 'manuel' extraction: a re-extraction deletes the
-- reel's source='llm' rows and nothing else, so hand-entered entities survive by
-- construction rather than by a special clause every query has to carry.
--
-- Dropped on the way: `sub_category` (replaced by `facets` at v9, no surviving
-- row fills it), `agreement` (a self-consistency scheme never implemented, NULL
-- everywhere) and `evidence_json` (written as '{}' and read nowhere).
CREATE TABLE candidate_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode         TEXT NOT NULL REFERENCES "reel"(shortcode) ON DELETE CASCADE,
    source            TEXT NOT NULL DEFAULT 'llm',   -- llm | human
    name              TEXT NOT NULL,
    name_latin        TEXT,
    type              TEXT NOT NULL,
    facets_json       TEXT,
    scale             TEXT,
    city              TEXT,
    country           TEXT,
    locality          TEXT,
    address           TEXT,
    brand             TEXT,
    intention         TEXT,
    highlights_json   TEXT,
    tags_json         TEXT,
    why_saved         TEXT,
    confidence        REAL,
    verified          INTEGER,
    verification_note TEXT
);

INSERT INTO candidate_new
    (shortcode, source, name, name_latin, type, facets_json, scale, city,
     country, locality, address, brand, intention, highlights_json, tags_json,
     why_saved, confidence, verified, verification_note)
SELECT e.shortcode, 'human', c.name, c.name_latin,
       -- The one surviving hand-entered row predates the move to English and
       -- carries `produit`, which is outside the current enum.
       CASE c.type WHEN 'produit' THEN 'product' ELSE c.type END,
       c.facets_json, c.scale, c.city, c.country, c.locality, c.address,
       c.brand, c.intention, c.highlights_json, c.tags_json, c.why_saved,
       c.confidence, c.verified, c.verification_note
FROM candidate c
JOIN extraction e ON e.id = c.extraction_id
WHERE e.model = 'humain';

DROP TABLE candidate;
ALTER TABLE candidate_new RENAME TO candidate;
CREATE INDEX idx_candidate_shortcode ON candidate(shortcode);
CREATE INDEX idx_candidate_type ON candidate(type);

-- 3. extraction: one row per reel, no version key to arbitrate.
DROP TABLE extraction;
CREATE TABLE extraction (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode    TEXT NOT NULL UNIQUE REFERENCES "reel"(shortcode) ON DELETE CASCADE,
    model        TEXT NOT NULL,
    prompt_sha   TEXT NOT NULL,   -- extract.prompt_fingerprint(): prompt + JSON schema
    code_sha     TEXT,            -- git rev-parse --short HEAD, NULL outside a repo
    extracted_at TEXT NOT NULL,
    raw_response TEXT,
    ok           INTEGER DEFAULT 1,
    error        TEXT
);

-- 4. classification: same key as its reel, nothing more.
-- Emptied rather than carried over: its 1061 rows are one per
-- (shortcode, prompt_version, model), i.e. the same 80 reels seen through every
-- prompt that ever ran. All 80 are about to be re-extracted.
-- `predicted_tags_json` goes too: tags have lived on the candidate since v10.
CREATE TABLE classification_new (
    shortcode       TEXT PRIMARY KEY REFERENCES "reel"(shortcode) ON DELETE CASCADE,
    predicted_topic TEXT,
    is_actionable   INTEGER,
    why_saved       TEXT,
    confidence      REAL,
    created_at      TEXT NOT NULL
);
DROP TABLE classification;
ALTER TABLE classification_new RENAME TO classification;

-- 5. entity.sub_category follows candidate.sub_category out. It survived as a
-- fallback chip in the catalogue ("show sub_category when a fiche has no
-- facets"), which from here on could only ever be NULL — a branch that looks
-- like a feature and can never fire.
ALTER TABLE entity DROP COLUMN sub_category;


-- Source: 012_repertoire.sql
-- A mode `repertoire`, additive alongside `recommandation`.
--
-- The four defects measured the evening this was designed (a recipe's 27
-- ingredients catalogued as entities, a tech roundup's projects the same, a
-- language lesson's phrases typed `product`, 8 photography exercises with
-- nowhere to go and silently dropped) turned out to be one problem: some
-- reels were never meant to be decomposed into entities. Their value is in
-- being read/watched in full, not in a list of places to visit or things to
-- buy.
--
-- `mode` never gates `entities` — a repertoire reel keeps any genuine
-- recommandation it names, on top of its own repertoire fiche. That is also
-- why deciding `mode` upstream, from caption+transcript alone, before OCR
-- runs, was tested and rejected: `ocr.py` already measures that 15% of the
-- corpus's verified entities are attested ONLY by OCR, invisible to caption
-- or transcript — a classifier that never reads OCR cannot know it is missing
-- something it has no signal for. `mode` is decided in the same call that
-- already reads everything, OCR included, exactly like `topic`/`why_saved`.
--
-- Reels already extracted under the previous prompt: `mode` stays NULL until
-- their prompt_sha goes stale and they are re-extracted (cf. extract._todo) —
-- no backfill invented for a distinction the old prompt never made.
ALTER TABLE extraction ADD COLUMN mode TEXT;
ALTER TABLE extraction ADD COLUMN tags_json TEXT;

-- A repertoire entry is its own reference, autonomous — unlike an entity,
-- which merges across reels through resolve.py's blocking logic. There is
-- nothing to arbitrate here, so no separate resolve-stage rebuild: extract.py
-- keeps this table in step with the extraction it just wrote, one reel at a
-- time (cf. extract._sync_repertoire_fts).
--
-- Self-contained rather than contentless (unlike entity_fts's `content=''`):
-- a contentless FTS5 table needs the OLD indexed values to delete a stale row
-- correctly, which extract.py's per-reel INSERT OR REPLACE does not keep
-- around. Storing its own copy of the text trades a little duplication for a
-- plain DELETE-by-rowid + INSERT on every re-extraction.
CREATE VIRTUAL TABLE repertoire_fts USING fts5(
    topic, why_saved, tags,
    tokenize='unicode61 remove_diacritics 2'
);


-- Source: 013_extraction_context_sha.sql
-- An extraction depends on the exact dossier sent to the model, not only on
-- the prompt and schema. NULL marks rows produced before this migration and
-- makes them eligible for one refresh.
ALTER TABLE extraction ADD COLUMN context_sha TEXT;

-- Source: 014_ocr_observations.sql
-- Keep the frame-level OCR observations alongside the aggregate text. The
-- aggregate remains the compatibility/search representation; observations let
-- extraction cite where a name was seen without rerunning OCR.
ALTER TABLE screen_text ADD COLUMN observations_json TEXT;

-- Source: 015_candidate_evidence.sql
-- Evidence keeps the source passage that caused a candidate to be extracted.
-- It is JSON because one candidate may be supported by several sources.
ALTER TABLE candidate ADD COLUMN evidence_json TEXT;

-- Source: 016_extraction_attempts_and_evidence_status.sql
-- `extraction` is the latest successful materialisation consumed by the
-- catalogue.  Attempts are separate: retaining an experiment or a malformed
-- response must not make every normal query choose between historical rows.
CREATE TABLE extraction_attempt (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode       TEXT NOT NULL REFERENCES reel(shortcode) ON DELETE CASCADE,
    model           TEXT NOT NULL,
    prompt_sha      TEXT NOT NULL,
    code_sha        TEXT,
    context_sha     TEXT NOT NULL,
    generation_json TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT NOT NULL,
    duration_ms     INTEGER NOT NULL,
    raw_response    TEXT,
    parsed_response TEXT,
    ok              INTEGER NOT NULL,
    error           TEXT
);
CREATE INDEX idx_extraction_attempt_shortcode ON extraction_attempt(shortcode, id);

-- The JSON evidence remains the record itself.  These two columns are the
-- deterministic judgement of that record, so the catalogue and review queue
-- need not re-parse JSON or repeat an LLM call to learn why it was withheld.
ALTER TABLE candidate ADD COLUMN evidence_status TEXT;
ALTER TABLE candidate ADD COLUMN evidence_note TEXT;
CREATE INDEX idx_candidate_evidence_status ON candidate(evidence_status)
    WHERE evidence_status IS NOT NULL;


-- Source: 017_reel_library.sql
-- A repertoire fiche needs a small, source-grounded reading layer.  It is
-- stored with the extraction (not in a second note system) so re-extraction is
-- still the single authoritative write for a reel.
ALTER TABLE extraction ADD COLUMN key_points_json TEXT;

-- Rebuild the small FTS index to include the fiche's useful points.  Existing
-- entries are restored from their authoritative extraction/classification rows;
-- old rows simply have no key points until their next extraction.
DROP TABLE repertoire_fts;
CREATE VIRTUAL TABLE repertoire_fts USING fts5(
    topic, why_saved, tags, key_points,
    tokenize='unicode61 remove_diacritics 2'
);
INSERT INTO repertoire_fts(rowid, topic, why_saved, tags, key_points)
SELECT r.rowid, cl.predicted_topic, cl.why_saved, e.tags_json,
       COALESCE(e.key_points_json, '')
FROM extraction e
JOIN reel r ON r.shortcode = e.shortcode
LEFT JOIN classification cl ON cl.shortcode = e.shortcode
WHERE e.mode = 'repertoire';


-- Source: 018_content_repertoire.sql
-- The repertoire is not merely full-text notes: it is an index of reusable
-- content.  A reel is the source container; one reel may expose several recipes
-- (or exercises) and a recipe can later receive personal cooking attempts.
-- Attempt rows are never deleted by a Qwen re-extraction.

ALTER TABLE extraction ADD COLUMN content_kind TEXT;

CREATE TABLE repertoire_entry (
    shortcode       TEXT PRIMARY KEY REFERENCES reel(shortcode) ON DELETE CASCADE,
    content_kind    TEXT NOT NULL,
    title           TEXT,
    summary         TEXT,
    cover_path      TEXT,
    video_status    TEXT NOT NULL DEFAULT 'local',
    obsidian_path   TEXT,
    note_status     TEXT NOT NULL DEFAULT 'none',
    generated_sha   TEXT,
    published_at    TEXT,
    updated_at      TEXT NOT NULL,
    CHECK (content_kind IN ('recipe', 'exercise', 'lesson', 'method', 'guide', 'inspiration')),
    CHECK (video_status IN ('local', 'archived', 'unavailable')),
    CHECK (note_status IN ('none', 'generated', 'personally_edited'))
);

CREATE TABLE recipe (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    shortcode           TEXT NOT NULL REFERENCES repertoire_entry(shortcode) ON DELETE CASCADE,
    dish_name           TEXT NOT NULL,
    cuisine             TEXT,
    cuisine_family      TEXT,
    course              TEXT,
    dietary_tags_json   TEXT NOT NULL DEFAULT '[]',
    summary             TEXT,
    evidence_json       TEXT NOT NULL DEFAULT '[]',
    confidence          REAL,
    active              INTEGER NOT NULL DEFAULT 1,
    created_at          TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    UNIQUE(shortcode, dish_name),
    CHECK (course IS NULL OR course IN ('entree', 'plat', 'dessert', 'boisson', 'accompagnement')),
    CHECK (confidence IS NULL OR (confidence >= 0 AND confidence <= 1))
);
CREATE INDEX idx_recipe_cuisine_family ON recipe(cuisine_family) WHERE active = 1;
CREATE INDEX idx_recipe_cuisine ON recipe(cuisine) WHERE active = 1;

CREATE TABLE recipe_attempt (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    recipe_id       INTEGER NOT NULL REFERENCES recipe(id) ON DELETE RESTRICT,
    cooked_at       TEXT,
    verdict         TEXT NOT NULL,
    rating          INTEGER,
    changes_made    TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL,
    CHECK (verdict IN ('want_to_try', 'liked', 'favorite', 'disappointing', 'avoid')),
    CHECK (rating IS NULL OR (rating >= 1 AND rating <= 5))
);
CREATE INDEX idx_recipe_attempt_recipe ON recipe_attempt(recipe_id, created_at DESC);

DROP TABLE repertoire_fts;
CREATE VIRTUAL TABLE repertoire_fts USING fts5(
    title, topic, why_saved, tags, key_points, dishes, cuisines,
    tokenize='unicode61 remove_diacritics 2'
);
INSERT INTO repertoire_fts(rowid, title, topic, why_saved, tags, key_points, dishes, cuisines)
SELECT r.rowid, '', COALESCE(cl.predicted_topic, ''), COALESCE(cl.why_saved, ''),
       COALESCE(e.tags_json, ''), COALESCE(e.key_points_json, ''), '', ''
FROM extraction e
JOIN reel r ON r.shortcode = e.shortcode
LEFT JOIN classification cl ON cl.shortcode = e.shortcode
WHERE e.mode = 'repertoire';


-- Source: 019_video_archive.sql
-- Archive facts are measured locally.  An object-store URL remains optional:
-- choosing and configuring a remote archive must not be a hidden side effect of
-- downloading a reel.
ALTER TABLE media ADD COLUMN sha256 TEXT;
ALTER TABLE media ADD COLUMN width INTEGER;
ALTER TABLE media ADD COLUMN height INTEGER;
ALTER TABLE media ADD COLUMN video_codec TEXT;
ALTER TABLE media ADD COLUMN audio_codec TEXT;
ALTER TABLE media ADD COLUMN validated_at TEXT;
ALTER TABLE media ADD COLUMN validation_error TEXT;
ALTER TABLE media ADD COLUMN poster_path TEXT;
ALTER TABLE media ADD COLUMN proxy_path TEXT;
ALTER TABLE media ADD COLUMN archive_uri TEXT;


-- Source: 020_candidate_name_resolution.sql
-- Keep Qwen's observed spelling and its evidence intact. Canonicalization is a
-- separate, replayable decision with an explicit abstention state.
CREATE TABLE candidate_name_resolution (
    candidate_id   INTEGER PRIMARY KEY REFERENCES candidate(id) ON DELETE CASCADE,
    observed_name  TEXT NOT NULL,
    evidence_json  TEXT NOT NULL,
    canonical_name TEXT,
    confidence     REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
    status         TEXT NOT NULL CHECK (status IN ('resolved', 'abstained')),
    method         TEXT NOT NULL,
    explanation    TEXT NOT NULL,
    decided_at     TEXT NOT NULL
);
CREATE INDEX idx_candidate_name_resolution_status
    ON candidate_name_resolution(status, confidence);


-- Source: 021_catalogue_feedback.sql
-- Personal opinions are a separate layer from the source-grounded catalogue.
-- No FK cascade: catalogue rebuilds/re-extractions must never erase feedback.
CREATE TABLE entity_personal (
    entity_id       INTEGER PRIMARY KEY,
    status          TEXT,
    note            TEXT,
    score           INTEGER,
    reviewed_at     TEXT,
    need            TEXT,
    interest_reason TEXT,
    open_questions  TEXT,
    documented_at   TEXT,
    obsidian_path   TEXT,
    note_status     TEXT NOT NULL DEFAULT 'none',
    generated_sha   TEXT,
    published_at    TEXT,
    updated_at      TEXT NOT NULL,
    CHECK (status IS NULL OR status IN
           ('considering', 'shortlisted', 'owned', 'favorite', 'disappointing', 'avoid')),
    CHECK (score IS NULL OR score BETWEEN 1 AND 5),
    CHECK (note_status IN ('none', 'generated', 'personally_edited'))
);
CREATE INDEX idx_entity_personal_status ON entity_personal(status);

CREATE TABLE entity_personal_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_id       INTEGER NOT NULL,
    status          TEXT,
    note            TEXT,
    score           INTEGER,
    reviewed_at     TEXT,
    need            TEXT,
    interest_reason TEXT,
    open_questions  TEXT,
    documented_at   TEXT,
    changed_at      TEXT NOT NULL,
    CHECK (status IS NULL OR status IN
           ('considering', 'shortlisted', 'owned', 'favorite', 'disappointing', 'avoid')),
    CHECK (score IS NULL OR score BETWEEN 1 AND 5)
);
CREATE INDEX idx_entity_personal_history_entity
    ON entity_personal_history(entity_id, changed_at DESC, id DESC);


-- Source: 022_saved_reel_status.sql
-- Read model for the saved-feed corpus. Source metadata stays in `reel`;
-- processing state remains per stage so a scan never causes a replay.
CREATE VIEW IF NOT EXISTS saved_reel_status AS
SELECT
    r.shortcode,
    r.kind,
    r.username,
    r.url,
    r.caption,
    r.taken_at,
    r.first_seen_at,
    r.last_seen_at,
    CASE WHEN m.mp4_path IS NOT NULL THEN 1 ELSE 0 END AS downloaded,
    m.mp4_path,
    m.bytes AS media_bytes,
    m.downloaded_at,
    m.http_status,
    MAX(CASE WHEN s.stage = 'download' THEN s.status END) AS download_status,
    MAX(CASE WHEN s.stage = 'asr' THEN s.status END) AS asr_status,
    MAX(CASE WHEN s.stage = 'ocr' THEN s.status END) AS ocr_status,
    MAX(CASE WHEN s.stage = 'extract' THEN s.status END) AS extract_status,
    COUNT(DISTINCT er.entity_id) AS entity_count
FROM reel r
LEFT JOIN media m ON m.shortcode = r.shortcode
LEFT JOIN stage_state s ON s.shortcode = r.shortcode
LEFT JOIN entity_reel er ON er.shortcode = r.shortcode
GROUP BY r.shortcode;


-- Source: 023_sync_manifest.sql
-- One durable record per Instagram scan. This is observation history, not a
-- guessed save date: Instagram exposes the current saved feed, not save_at.
CREATE TABLE sync_run (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at      TEXT NOT NULL,
    completed_at    TEXT,
    status          TEXT NOT NULL DEFAULT 'running',
    pages           INTEGER NOT NULL DEFAULT 0,
    items_seen      INTEGER NOT NULL DEFAULT 0,
    unique_items    INTEGER NOT NULL DEFAULT 0,
    inserted        INTEGER NOT NULL DEFAULT 0,
    updated         INTEGER NOT NULL DEFAULT 0,
    unsaved         INTEGER NOT NULL DEFAULT 0,
    error           TEXT,
    CHECK (status IN ('running', 'done', 'failed'))
);

CREATE TABLE sync_observation (
    run_id          INTEGER NOT NULL REFERENCES sync_run(id) ON DELETE CASCADE,
    shortcode       TEXT NOT NULL,
    observed_at     TEXT NOT NULL,
    PRIMARY KEY (run_id, shortcode)
);

CREATE INDEX idx_sync_run_started ON sync_run(started_at DESC);
CREATE INDEX idx_sync_observation_shortcode ON sync_observation(shortcode);



-- Persistent user conversations are separate from extracted facts and may be deleted.
CREATE TABLE chat_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE chat_message (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL REFERENCES chat_session(id) ON DELETE CASCADE,
    role TEXT NOT NULL CHECK(role IN ('user','assistant')),
    content TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX idx_chat_message_session ON chat_message(session_id, id);

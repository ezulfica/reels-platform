"""Single entry point. Every command is idempotent and driven by stage_state."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from domain import feedback as catalog_feedback
from domain import reel_library
from domain import repertoire
from quality import benchmark as reels_benchmark
from quality import regression as reels_regression
from storage import database as db
from adapters import video_archive
from pipeline.capture import collections as capture_collections
from pipeline.capture import download as capture_download
from pipeline.capture import instagram
from pipeline.capture.session import IGError, make_session
from pipeline.catalogue import resolve as catalog_resolve
from agent import search as reels_query
from pipeline.enrich import asr as enrich_asr
from pipeline.extract import extract as extract_llm
from pipeline.extract import review as extract_review
from pipeline.enrich import ocr as enrich_ocr
from pipeline.extract import verify as extract_verify
import inference as llm
from pipeline import sync_manifest
from quality import tuning as reels_tuning


def _conn(args):
    conn = db.connect(args.db)
    db.initialize(conn)
    return conn



def cmd_status(args) -> int:
    conn = _conn(args)
    stats = db.counts(conn)
    width = max(len(k) for k in stats)
    print()
    for key, value in stats.items():
        print(f"  {key:<{width}}  {value:>6}")

    print("\n  steps:")
    rows = conn.execute(
        "SELECT stage, status, COUNT(*) n FROM stage_state GROUP BY 1, 2 ORDER BY 1, 2"
    ).fetchall()
    for row in rows or []:
        print(f"    {row['stage']:<10} {row['status']:<8} {row['n']:>6}")
    if not rows:
        print("    (none)")

    print()
    return 0


def _sync_with_manifest(
    conn, session, *, full: bool = True, limit: int | None = None
) -> dict:
    run_id = sync_manifest.start(conn)
    observed = lambda codes: sync_manifest.observe(conn, run_id, codes)
    try:
        stats = instagram.sync(
            conn, session, limit=limit, full=full, seen_callback=observed
        )
    except Exception as error:
        sync_manifest.finish(conn, run_id, status="failed", stats={}, error=str(error))
        raise
    sync_manifest.finish(conn, run_id, status="done", stats=stats)
    stats = dict(stats)
    stats["sync_run_id"] = run_id
    return stats


def cmd_sync(args) -> int:
    conn = _conn(args)
    session = make_session()
    print("→ saved feed", file=sys.stderr)
    stats = _sync_with_manifest(conn, session, full=args.full, limit=args.limit)
    print(f"\n  {stats}\n")
    return 0


def cmd_sync_collections(args) -> int:
    conn = _conn(args)
    session = make_session()
    print("→ collections (paginated)", file=sys.stderr)
    stats = capture_collections.sync_collections(conn, session)

    linked = conn.execute(
        "SELECT COUNT(DISTINCT shortcode) FROM reel_collection"
    ).fetchone()[0]
    print(f"\n  {stats}")
    print(f"  reels attached to >=1 collection: {linked}\n")
    return 0


def cmd_download(args) -> int:
    conn = _conn(args)
    print("→ downloading videos", file=sys.stderr)
    stats = capture_download.download(
        conn,
        limit=args.limit,
        sample=getattr(args, "sample", None),
        seed=getattr(args, "seed", 0),
        shortcodes=getattr(args, "shortcode", None),
    )
    go = stats.pop("bytes", 0) / 1e9
    print(f"\n  {stats}   {go:.2f} GB\n")
    return 0


def cmd_asr(args) -> int:
    conn = _conn(args)
    print("→ transcription (faster-whisper large-v3)", file=sys.stderr)
    stats = enrich_asr.run(conn, limit=args.limit)
    print(f"\n  {stats}\n")
    return 0


def cmd_ocr(args) -> int:
    conn = _conn(args)
    print(f"→ OCR ({enrich_ocr.FPS} fps, {enrich_ocr.TOOL_VERSION})", file=sys.stderr)
    stats = enrich_ocr.run(conn, limit=args.limit)
    print(f"\n  {stats}\n")
    return 0


def cmd_extract(args) -> int:
    conn = _conn(args)
    model = llm.model("extract", getattr(args, "model", None))
    print(f"→ LLM extraction ({model})", file=sys.stderr)
    shortcodes = None
    if args.shortcodes_file and args.shortcode:
        raise ValueError("use either --shortcodes-file or --shortcode, not both")
    if args.shortcodes_file:
        payload = json.loads(args.shortcodes_file.read_text(encoding="utf-8"))
        shortcodes = payload["shortcodes"] if isinstance(payload, dict) else payload
        if not isinstance(shortcodes, list) or not all(
            isinstance(s, str) for s in shortcodes
        ):
            raise ValueError(
                "--shortcodes-file must be a JSON list or object with a string shortcodes list"
            )
    elif args.shortcode:
        shortcodes = args.shortcode
    stats = extract_llm.run(
        conn,
        limit=args.limit,
        model=model,
        force=getattr(args, "force", False),
        shortcodes=shortcodes,
    )
    print(f"\n  {stats}\n")
    return 0


def cmd_web(args) -> int:
    import uvicorn

    os.environ["REELS_DB_PATH"] = str(Path(args.db).expanduser().resolve())
    print(f"→ http://127.0.0.1:{args.port} · database: {args.db}", file=sys.stderr)
    uvicorn.run(
        "interfaces.web.app:app",
        host="127.0.0.1",
        port=args.port,
        reload=args.reload,
    )
    return 0


def cmd_query(args) -> int:
    conn = _conn(args)
    results = reels_query.search(conn, args.text, type_=args.type, city=args.city)
    if not results:
        print("\n  no result\n")
        return 0
    print()
    for r in results:
        print(f"  {r['name']} ({r['type']}{', ' + r['city'] if r['city'] else ''})")
        if r["why_saved"]:
            print(f"    {r['why_saved']}")
        if r["gmaps_url"]:
            print(f"    maps: {r['gmaps_url']}")
        for reel in r["reels"]:
            print(f"    source: @{reel['username']} {reel['url']}")
        print()
    return 0


def cmd_repertoire(args) -> int:
    conn = _conn(args)
    results = reel_library.search(conn, args.text or "")
    if not results:
        print("\n  no result\n")
        return 0
    print()
    for r in results:
        print(f"  {r['topic'] or '(sans titre)'}")
        if r["why_saved"]:
            print(f"    {r['why_saved']}")
        if r["tags"]:
            print(f"    tags: {', '.join(r['tags'])}")
        if r["url"]:
            print(f"    source: @{r['username']} {r['url']}")
        print()
    return 0


def cmd_recipes(args) -> int:
    rows = repertoire.recipes(
        _conn(args), cuisine=args.cuisine, verdict=args.verdict, limit=args.limit
    )
    if not rows:
        print("\n  no recipe\n")
        return 0
    print()
    for row in rows:
        place = " · ".join(
            value
            for value in (row["cuisine_family"], row["cuisine"], row["course"])
            if value
        )
        verdict = f" · {row['verdict']}" if row["verdict"] else " · à essayer"
        print(
            f"  #{row['id']} {row['dish_name']}{f' — {place}' if place else ''}{verdict}"
        )
        if row["summary"]:
            print(f"    {row['summary']}")
        if row["note"]:
            print(f"    retour : {row['note']}")
        print(f"    reel: {row['url']}")
    print()
    return 0


def cmd_recipe_feedback(args) -> int:
    attempt_id = repertoire.record_attempt(
        _conn(args),
        args.recipe_id,
        verdict=args.verdict,
        rating=args.rating,
        note=args.note,
        changes_made=args.changes,
        cooked_at=args.cooked_at,
    )
    print(f"\n  recipe attempt #{attempt_id} recorded\n")
    return 0


def cmd_products(args) -> int:
    rows = catalog_feedback.products(
        _conn(args), args.text or "", status=args.status, limit=args.limit
    )
    if not rows:
        print("\n  aucun produit\n")
        return 0
    print()
    for row in rows:
        label = row["status"] or "sans avis"
        score = f" · {row['score']}/5" if row["score"] is not None else ""
        print(f"  #{row['id']} {row['canonical_name']} · {label}{score}")
        if row["note"]:
            print(f"    retour : {row['note']}")
        if row["brand"]:
            print(f"    marque : {row['brand']}")
        for source in row["sources"]:
            print(f"    source : {source['shortcode']} {source['url']}")
            for evidence in source["evidence"]:
                if evidence.get("quote"):
                    print(f"      preuve : {evidence['quote']}")
    print()
    return 0


def cmd_product_feedback(args) -> int:
    conn = _conn(args)
    if args.clear:
        if (
            any(
                value is not None
                for value in (
                    args.status,
                    args.note,
                    args.score,
                    args.date,
                    args.need,
                    args.interest,
                    args.questions,
                )
            )
            or args.documented
        ):
            print(
                "\n  --clear ne peut pas être combiné à des champs de retour\n",
                file=sys.stderr,
            )
            return 2
        catalog_feedback.update(
            conn, args.entity_id, status=None, note=None, score=None, reviewed_at=None
        )
        print(
            f"\n  avis personnel effacé pour le produit #{args.entity_id}; historique conservé\n"
        )
        return 0
    changes = {
        "status": args.status,
        "note": args.note,
        "score": args.score,
        "reviewed_at": args.date,
        "need": args.need,
        "interest_reason": args.interest,
        "open_questions": args.questions,
    }
    changes = {key: value for key, value in changes.items() if value is not None}
    if not changes and not args.documented:
        print("\n  fournissez un champ à modifier ou --documented\n", file=sys.stderr)
        return 2
    catalog_feedback.update(conn, args.entity_id, **changes, documented=args.documented)
    print(f"\n  retour personnel enregistré pour le produit #{args.entity_id}\n")
    return 0


def cmd_product_history(args) -> int:
    rows = catalog_feedback.history(_conn(args), args.entity_id)
    if not rows:
        print(f"\n  aucun historique pour #{args.entity_id}\n")
        return 0
    print()
    for row in rows:
        score = f" · {row['score']}/5" if row["score"] is not None else ""
        print(f"  {row['changed_at']} · {row['status'] or 'sans statut'}{score}")
        if row["note"]:
            print(f"    {row['note']}")
    print()
    return 0


def cmd_validate_video(args) -> int:
    info = video_archive.validate(_conn(args), args.shortcode)
    print(
        f"\n  valid video: {info['width']}x{info['height']} · {info['duration_s']:.1f}s · {info['video_codec']}\n"
    )
    return 0


def cmd_prepare_entity_frames(args) -> int:
    stats = video_archive.make_entity_first_frames(_conn(args))
    print(f"\n  first frames: {stats['done']} done · {stats['failed']} failed\n")
    return 0 if not stats["failed"] else 1


def cmd_prepare_video(args) -> int:
    assets = video_archive.make_viewing_assets(_conn(args), args.shortcode)
    print(f"\n  poster: {assets['poster_path']}\n  proxy: {assets['proxy_path']}\n")
    return 0


def cmd_benchmark(args) -> int:
    report = reels_benchmark.run(
        source_path=Path(args.source_db),
        fixture_path=args.file,
        target_path=args.working_db,
        report_path=args.report,
        model=llm.model("extract", args.model),
    )
    score = report["score"]
    print(
        f"\n  {report['model']} · {len(score['cases'])} reels · "
        f"{report['duration_seconds']:.1f}s\n"
        f"  entity recall: {score['entities_found']}/{score['entities_expected']} "
        f"({score['recall']:.0%})\n"
        f"  report: {args.report}\n"
    )
    return 1 if _regression_failed(score) else 0


def cmd_regression(args) -> int:
    conn = _conn(args)
    cases = reels_regression.load_cases(args.file)
    report = reels_regression.score(conn, cases)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if _regression_failed(report) else 0

    print(f"\n  Jeu aveugle Qwen · {len(cases)} reels")
    print(
        f"  Rappel entités : {report['entities_found']}/{report['entities_expected']} "
        f"({report['recall']:.0%})"
    )
    print(
        f"  Après canonicalisation locale : "
        f"{report['canonicalized_entities_found']}/{report['entities_expected']} "
        f"({report['canonicalized_recall']:.0%}) · "
        f"{report['canonicalization_resolved']} résolues / "
        f"{report['canonicalization_abstained']} abstentions"
    )
    print(
        f"  Faux positifs : {report['false_positives']} sur "
        f"{report['false_positive_cases']} reels à liste exhaustive"
    )
    print(
        f"  Faux positifs après canonicalisation : "
        f"{report['canonicalized_false_positives']}"
    )
    print(
        f"  content_kind : {report['content_kind_correct']}/"
        f"{report['content_kind_expected']} exacts · mode : "
        f"{report['mode_correct']}/{report['mode_expected']} exacts"
    )
    print(
        f"  Actionnabilité : {report['actionable_correct']}/"
        f"{report['actionable_expected']} exactes"
    )
    print(f"  Termes attendus : {report['terms_found']}/{report['terms_expected']}")
    if report["prompt_shas"]:
        print(f"  Extraction : {', '.join(report['prompt_shas'])}")
    print("\n  Par catégorie :")
    for category, values in sorted(report["by_category"].items()):
        recall = (
            f"{values['entities_found']}/{values['entities_expected']} entités"
            if values["entities_expected"]
            else "sans entités attendues"
        )
        fp = (
            f" · {values['false_positives']} FP/{values['false_positive_cases']}"
            if values["false_positive_cases"]
            else ""
        )
        print(
            f"    {category}: {recall} · content_kind "
            f"{values['content_kind_correct']}/{values['content_kind_expected']} · mode "
            f"{values['mode_correct']}/{values['mode_expected']} · actionnable "
            f"{values['actionable_correct']}/{values['actionable_expected']} · termes "
            f"{values['terms_found']}/{values['terms_expected']}{fp}"
        )
    print("\n  Par reel :")
    for row in report["cases"]:
        duration = (
            f"{row['duration_ms'] / 1000:.1f}s"
            if row["duration_ms"] is not None
            else "n/d"
        )
        entities = f"{row['entity_found']}/{row['entity_expected']} entités"
        mode = (
            "n/a"
            if row["mode_correct"] is None
            else ("OK" if row["mode_correct"] else "ERREUR")
        )
        kind = (
            "n/a"
            if row["content_kind_correct"] is None
            else ("OK" if row["content_kind_correct"] else "ERREUR")
        )
        print(
            f"    {row['shortcode']} [{row['status']}] {entities} · mode {mode} · "
            f"content_kind {kind} · {duration}"
        )
        if row["missing_entities"]:
            print(f"      noms absents : {', '.join(row['missing_entities'])}")
    for item in report["false_positive_items"]:
        print(f"    FP {item['shortcode']}: {item['name']} ({item['type']})")
    for error in report["type_errors"]:
        print(
            f"    Type {error['shortcode']}: {error['name']} = {error['actual']} "
            f"(attendu : {'/'.join(error['expected'])})"
        )
    print()
    return 1 if _regression_failed(report) else 0


def _regression_failed(report: dict) -> bool:
    return bool(
        report["missing"]
        or report["false_positive_items"]
        or report["type_errors"]
        or report["canonicalized_false_positives"] > report["false_positives"]
        or report["canonicalized_entities_found"] < report["entities_found"]
        or report["mode_correct"] != report["mode_expected"]
        or report["content_kind_correct"] != report["content_kind_expected"]
        or report["actionable_correct"] != report["actionable_expected"]
        or report["terms_found"] != report["terms_expected"]
        or report["recipes_found"] != report["recipes_expected"]
    )


def cmd_weekly_run(args) -> int:
    """Run the bounded, resumable weekly workflow without forcing any stage."""
    conn = _conn(args)
    session = make_session()

    def step(label, fn):
        print(f"\n=== {label} ===", file=sys.stderr)
        stats = fn()
        print(f"  {stats}", file=sys.stderr)
        return stats

    step("sync", lambda: _sync_with_manifest(conn, session, full=True))
    step("download", lambda: capture_download.download(conn))
    step("asr", lambda: enrich_asr.run(conn))
    step("ocr", lambda: enrich_ocr.run(conn))
    step("extract", lambda: extract_llm.run(conn))
    step("verify", lambda: extract_verify.run(conn))
    step("catalog", lambda: catalog_resolve.run(conn))
    print("\n=== weekly run done ===\n", file=sys.stderr)
    return 0


def cmd_pipeline(args) -> int:
    """Chains every step, now linearly:
    sync -> download -> asr -> ocr -> extract -> verify -> catalog.

    A single extraction pass since the vision step was removed: the double pass
    only existed to let the vision arbitration slot in between the two. Each step
    stays individually idempotent and resumable (stage_state); this is only a
    chaining, not new logic.

    `verify` and `catalog` are NOT optional once `extract` has run: the catalogue
    reads only verified candidates. A fresh extraction not followed by `verify`
    therefore makes its reels invisible to `catalog`, which then purges their
    fiches as orphans. It happened: an `extract && verify && catalog` interrupted
    by a crash left 52 reels out of 60 with unverified candidates, and a `catalog`
    run in that state would have taken the catalogue from 140 fiches down to about
    ten. Hence the unconditional chaining below, and the guardrail in
    catalog_resolve.run()."""
    conn = _conn(args)
    session = make_session()

    def step(label, fn):
        print(f"\n=== {label} ===", file=sys.stderr)
        stats = fn()
        print(f"  {stats}", file=sys.stderr)

    step("sync", lambda: _sync_with_manifest(conn, session, full=False))
    try:
        step(
            "sync-collections",
            lambda: capture_collections.sync_collections(conn, session),
        )
    except IGError as error:
        print(
            f"  WARNING: collections sync unavailable; continuing: {error}",
            file=sys.stderr,
        )
    step("download", lambda: capture_download.download(conn))
    step("asr", lambda: enrich_asr.run(conn))
    step("ocr", lambda: enrich_ocr.run(conn))
    step("extract", lambda: extract_llm.run(conn))
    step("verify", lambda: extract_verify.run(conn))
    step("catalog", lambda: catalog_resolve.run(conn))
    print("\n=== done ===\n", file=sys.stderr)
    return 0


def cmd_tuning(args) -> int:
    """Changes nothing: re-measures the thresholds and flags those that no longer
    hold."""
    conn = _conn(args)
    for line in reels_tuning.report(conn):
        print(line)
    return 0


def cmd_catalog(args) -> int:
    conn = _conn(args)
    print(
        "→ resolving the catalogue (blocking by type + normalised name)",
        file=sys.stderr,
    )
    stats = catalog_resolve.run(conn)
    print(f"\n  {stats}\n")
    return 0


def cmd_review(args) -> int:
    conn = _conn(args)
    items = extract_review.queue(conn, limit=args.limit, reason=args.reason)
    if not items:
        print("\n  queue empty\n")
        return 0
    print()
    for it in items:
        label = extract_review.REASON_LABELS[it["reason"]]
        print(f"  [{label}] {it['shortcode']}")
        print(f"    {it['detail']}")
    print(
        f"\n  {len(items)} diagnostics à inspecter dans la fiche source.\n"
    )
    return 0



def cmd_verify(args) -> int:
    conn = _conn(args)
    print("→ verifying entities against the source text", file=sys.stderr)
    stats = extract_verify.run(
        conn, limit=args.limit, model=getattr(args, "model", None)
    )
    print(f"\n  {stats}\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="reels", description=__doc__)
    parser.add_argument("--db", default=str(db.DEFAULT_DB))
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("status", help="pipeline status").set_defaults(fn=cmd_status)

    sync = sub.add_parser("sync", help="scrape the saved feed")
    sync.add_argument("--limit", type=int, help="stop after N reels")
    sync.add_argument("--full", action="store_true", help="full walk, no early stop")
    sync.set_defaults(fn=cmd_sync)

    sub.add_parser("sync-collections", help="collection names").set_defaults(
        fn=cmd_sync_collections
    )

    dl = sub.add_parser("download", help="download the videos (yt-dlp)")
    dl.add_argument("--limit", type=int, help="stop after N videos, in feed order")
    # A random draw rather than the head of the queue: the corpus downloaded so
    # far is the top of the feed, i.e. one period and its dominant themes.
    dl.add_argument(
        "--sample", type=int, help="draw N reels at random from the whole corpus"
    )
    dl.add_argument(
        "--seed",
        type=int,
        default=0,
        help="seed for --sample, so the draw can be replayed",
    )
    dl.add_argument(
        "--shortcode",
        action="append",
        help="retry this exact reel only (repeatable)",
    )
    dl.set_defaults(fn=cmd_download)

    asr = sub.add_parser("asr", help="transcribe the audio (faster-whisper)")
    asr.add_argument("--limit", type=int, help="stop after N reels")
    asr.set_defaults(fn=cmd_asr)

    ocr = sub.add_parser("ocr", help="OCR the keyframes (RapidOCR)")
    ocr.add_argument("--limit", type=int, help="stop after N reels")
    ocr.set_defaults(fn=cmd_ocr)

    extract = sub.add_parser(
        "extract", help="LLM transform: classify and extract entities"
    )
    extract.add_argument("--limit", type=int, help="stop after N reels")
    extract.add_argument("--model", help="override the configured extraction model")
    # What used to be `--label t2`: a second draw of the same configuration. It
    # now overwrites the first rather than sitting beside it — measuring the
    # model's variance is an ad hoc job, not something the schema carries.
    extract.add_argument(
        "--force",
        action="store_true",
        help="re-extract reels already done with this prompt",
    )
    extract.add_argument(
        "--shortcodes-file",
        type=Path,
        help="JSON list (or {shortcodes: [...]}) to extract exactly",
    )
    extract.add_argument(
        "--shortcode",
        action="append",
        help="extract this shortcode exactly (repeatable)",
    )
    extract.set_defaults(fn=cmd_extract)

    sub.add_parser(
        "catalog", help="resolve candidates into canonical fiches"
    ).set_defaults(fn=cmd_catalog)

    sub.add_parser(
        "pipeline", help="chain sync->download->asr->ocr->extract->verify->catalog"
    ).set_defaults(fn=cmd_pipeline)

    sub.add_parser(
        "weekly-run", help="full weekly sync and resumable processing workflow"
    ).set_defaults(fn=cmd_weekly_run)

    sub.add_parser(
        "tuning",
        help="re-measure the thresholds (context, frames, sources) on the database",
    ).set_defaults(fn=cmd_tuning)

    web = sub.add_parser("web", help="start the local web UI")
    web.add_argument("--port", type=int, default=8420)
    web.add_argument("--reload", action="store_true", help="hot reload (dev)")
    web.set_defaults(fn=cmd_web)

    query = sub.add_parser("query", help="search the catalogue")
    query.add_argument("text")
    query.add_argument("--type", choices=extract_llm.TYPES)
    query.add_argument("--city")
    query.set_defaults(fn=cmd_query)

    repertoire_parser = sub.add_parser(
        "repertoire",
        help="search repertoire reels (recipes, tutorials, "
        "routines — no place/product to go find)",
    )
    repertoire_parser.add_argument(
        "text", nargs="?", help="empty: most recent repertoire reels"
    )
    repertoire_parser.set_defaults(fn=cmd_repertoire)

    recipes = sub.add_parser("recipes", help="recommend indexed recipes")
    recipes.add_argument("--cuisine", help="specific cuisine or family, e.g. asiatique")
    recipes.add_argument("--verdict", choices=repertoire.RECIPE_VERDICTS)
    recipes.add_argument("--limit", type=int, default=20)
    recipes.set_defaults(fn=cmd_recipes)

    feedback = sub.add_parser("recipe-feedback", help="record a real cooking attempt")
    feedback.add_argument("recipe_id", type=int)
    feedback.add_argument(
        "--verdict", required=True, choices=repertoire.RECIPE_VERDICTS
    )
    feedback.add_argument("--rating", type=int)
    feedback.add_argument("--note")
    feedback.add_argument("--changes")
    feedback.add_argument("--cooked-at", help="ISO date, e.g. 2026-09-12")
    feedback.set_defaults(fn=cmd_recipe_feedback)

    products = sub.add_parser("products", help="search products and personal shortlist")
    products.add_argument("text", nargs="?", default="", help="FTS terms, e.g. photo")
    products.add_argument("--status", choices=catalog_feedback.STATUSES)
    products.add_argument("--limit", type=int, default=25)
    products.set_defaults(fn=cmd_products)

    product_feedback = sub.add_parser(
        "product-feedback", help="record personal product feedback"
    )
    product_feedback.add_argument("entity_id", type=int)
    product_feedback.add_argument("--status", choices=catalog_feedback.STATUSES)
    product_feedback.add_argument("--note")
    product_feedback.add_argument("--score", type=int, choices=range(1, 6))
    product_feedback.add_argument("--date", help="date of this opinion, ISO format")
    product_feedback.add_argument("--need", help="the need this product should meet")
    product_feedback.add_argument("--interest", help="why this product interests you")
    product_feedback.add_argument("--questions", help="open questions to resolve")
    product_feedback.add_argument(
        "--documented",
        action="store_true",
        help="mark as genuinely documented, eligible for a note",
    )
    product_feedback.add_argument(
        "--clear",
        action="store_true",
        help="clear current opinion fields while keeping history",
    )
    product_feedback.set_defaults(fn=cmd_product_feedback)

    product_history = sub.add_parser(
        "product-history", help="show append-only product feedback history"
    )
    product_history.add_argument("entity_id", type=int)
    product_history.set_defaults(fn=cmd_product_history)

    video_check = sub.add_parser(
        "validate-video", help="validate one local reel archive"
    )
    video_check.add_argument("shortcode")
    video_check.set_defaults(fn=cmd_validate_video)

    frames = sub.add_parser("prepare-entity-frames", help="create first-frame previews for entity sources")
    frames.set_defaults(fn=cmd_prepare_entity_frames)

    video_assets = sub.add_parser(
        "prepare-video", help="create poster and lightweight proxy"
    )
    video_assets.add_argument("shortcode")
    video_assets.set_defaults(fn=cmd_prepare_video)

    benchmark = sub.add_parser(
        "benchmark", help="run a model on a frozen fixture in an isolated database"
    )
    benchmark.add_argument("--file", type=Path, required=True)
    benchmark.add_argument("--source-db", default=str(db.DEFAULT_DB))
    benchmark.add_argument("--working-db", type=Path, required=True)
    benchmark.add_argument("--report", type=Path, required=True)
    benchmark.add_argument("--model", help="override the configured extraction model")
    benchmark.set_defaults(fn=cmd_benchmark)

    regression = sub.add_parser(
        "regression", help="check extraction quality on curated reels"
    )
    regression.add_argument(
        "--file",
        type=Path,
        default=Path("tests/fixtures/qwen_extraction_blind_v1.json"),
    )
    regression.add_argument(
        "--json", action="store_true", help="print the complete scorecard as JSON"
    )
    regression.set_defaults(fn=cmd_regression)

    review = sub.add_parser("review", help="human review queue")
    review.add_argument("--limit", type=int)
    review.add_argument(
        "--reason",
        choices=["rejected", "evidence", "silent", "uncertain"],
        help="show only one category",
    )
    review.set_defaults(fn=cmd_review)



    verify = sub.add_parser("verify", help="confront each entity with the source text")
    verify.add_argument("--limit", type=int, help="stop after N candidates")
    verify.add_argument("--model", help="defaults to extract's model")
    verify.set_defaults(fn=cmd_verify)

    args = parser.parse_args(argv)
    try:
        return args.fn(args)
    except IGError as error:
        print(f"\nError: {error}\n", file=sys.stderr)
        return 2
    except ValueError as error:
        print(f"\nError: {error}\n", file=sys.stderr)
        return 2
    except RuntimeError as error:
        # The pipeline guardrails (cf. catalog_resolve.run) raise RuntimeError with
        # an actionable message. A traceback would teach the user nothing more.
        print(f"\nError: {error}\n", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            "\nInterrupted (resumable: the state is in the database).", file=sys.stderr
        )
        return 130


if __name__ == "__main__":
    sys.exit(main())

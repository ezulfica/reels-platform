"""Isolated, reproducible extraction benchmarks.

A benchmark copies only source evidence into a fresh SQLite database. It never
reads prior extractions, candidates, feedback, or user-authored state from the
production database.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import inference as llm
from pipeline.catalogue import resolve as catalogue
from pipeline.extract import extract
from storage import database as db

from . import regression

SOURCE_TABLES = ("reel", "reel_context", "transcript", "screen_text")


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [row["name"] for row in conn.execute(f"PRAGMA table_info({table})")]


def build_database(source_path: Path, target_path: Path, shortcodes: list[str]) -> None:
    """Create a fresh benchmark DB containing only the selected source evidence."""
    if target_path.exists():
        raise FileExistsError(f"benchmark database already exists: {target_path}")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(f"file:{source_path.resolve()}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    target = db.connect(target_path)
    try:
        db.initialize(target)
        placeholders = ", ".join("?" for _ in shortcodes)
        for table in SOURCE_TABLES:
            columns = _columns(target, table)
            quoted = ", ".join(f'"{column}"' for column in columns)
            rows = source.execute(
                f"SELECT {quoted} FROM {table} WHERE shortcode IN ({placeholders})",
                shortcodes,
            ).fetchall()
            value_placeholders = ", ".join("?" for _ in columns)
            target.executemany(
                f"INSERT INTO {table} ({quoted}) VALUES ({value_placeholders})",
                [tuple(row[column] for column in columns) for row in rows],
            )
        target.commit()
    finally:
        source.close()
        target.close()


def run(
    *,
    source_path: Path,
    fixture_path: Path,
    target_path: Path,
    report_path: Path,
    model: str,
) -> dict:
    """Run and score one model against a frozen fixture in an isolated database."""
    cases = regression.load_cases(fixture_path)
    shortcodes = [case["shortcode"] for case in cases]
    if len(shortcodes) != len(set(shortcodes)):
        raise ValueError("benchmark fixture contains duplicate shortcodes")
    build_database(source_path, target_path, shortcodes)

    conn = db.connect(target_path)
    started = time.monotonic()
    llm.reset_usage()
    try:
        extraction_stats = extract.run(conn, model=model, shortcodes=shortcodes)
        catalogue_stats = catalogue.run(conn)
        score = regression.score(conn, cases)
    finally:
        conn.close()

    report = {
        "benchmark_version": 1,
        "fixture": str(fixture_path),
        "source_database": str(source_path),
        "working_database": str(target_path),
        "model": model,
        "duration_seconds": round(time.monotonic() - started, 3),
        "extraction": extraction_stats,
        "catalogue": catalogue_stats,
        "usage": llm.usage(),
        "score": score,
    }
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    return report

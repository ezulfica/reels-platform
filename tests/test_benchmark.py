from __future__ import annotations

import sqlite3

from quality.benchmark import build_database
from storage import database as db


def test_benchmark_database_contains_only_selected_source_evidence(tmp_path):
    source_path = tmp_path / "source.sqlite"
    source = db.connect(source_path)
    db.initialize(source)
    source.execute("INSERT INTO reel (shortcode, url, caption, first_seen_at, last_seen_at) VALUES ('AAA', 'https://instagram.test/AAA', 'caption', 'a', 'a')")
    source.execute("INSERT INTO reel (shortcode, url, first_seen_at, last_seen_at) VALUES ('BBB', 'https://instagram.test/BBB', 'b', 'b')")
    source.execute("INSERT INTO reel_context (shortcode) VALUES ('AAA')")
    source.execute("INSERT INTO transcript (shortcode, tool_version, text, created_at) VALUES ('AAA', 'v1', 'audio', 'a')")
    source.execute("INSERT INTO screen_text (shortcode, tool_version, text, created_at) VALUES ('BBB', 'v1', 'ocr', 'b')")
    source.commit()
    source.close()

    target_path = tmp_path / "benchmark.sqlite"
    build_database(source_path, target_path, ["AAA"])

    target = sqlite3.connect(target_path)
    assert target.execute("SELECT shortcode FROM reel").fetchall() == [("AAA",)]
    assert target.execute("SELECT shortcode FROM reel_context").fetchall() == [("AAA",)]
    assert target.execute("SELECT text FROM transcript").fetchall() == [("audio",)]
    assert target.execute("SELECT COUNT(*) FROM screen_text").fetchone()[0] == 0
    assert target.execute("SELECT COUNT(*) FROM candidate").fetchone()[0] == 0

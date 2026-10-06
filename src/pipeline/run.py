"""Thin ordered façades over the existing pipeline stages.

These functions give CLI, Web and external agents one ordered vocabulary over
the pipeline implementations.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Collection

from .capture import download, instagram
from .catalogue import resolve
from .enrich import asr, ocr
from .extract import extract, verify


def run_capture(
    conn: sqlite3.Connection, session, *, full: bool = True, limit: int | None = None
) -> dict:
    return instagram.sync(conn, session, full=full, limit=limit)


def run_enrichment(conn: sqlite3.Connection, *, limit: int | None = None) -> dict:
    download.download(conn, limit=limit)
    asr.run(conn, limit=limit)
    return ocr.run(conn, limit=limit)


def run_extraction(
    conn: sqlite3.Connection,
    *,
    limit: int | None = None,
    model: str | None = None,
    force: bool = False,
    shortcodes: Collection[str] | None = None,
) -> dict[str, int]:
    result = extract.run(
        conn, limit=limit, model=model, force=force, shortcodes=shortcodes
    )
    verify.run(conn, limit=limit, model=model)
    return result


def run_catalogue(conn: sqlite3.Connection, *, force: bool = False) -> dict:
    return resolve.run(conn, force=force)

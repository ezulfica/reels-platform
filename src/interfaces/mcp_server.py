"""Read-only MCP server for an external recommendation agent.

The server communicates over stdio. Its stdout belongs to the MCP protocol: do
not add prints or logs here. It never imports pipeline commands and opens the
SQLite database in read-only mode.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from mcp.server import MCPServer

from agent import (
    database_summary as _database_summary,
    get_entity as _get_entity,
    get_reel as _get_reel,
    search_entities as _search_entities,
    search_recipes as _search_recipes,
    search_repertoire as _search_repertoire,
)
from storage import database as db

mcp = MCPServer(
    "reels-platform",
    title="Reels Platform",
    description="Recherche lecture seule dans les entités, recettes et reels extraits.",
    instructions=(
        "Les résultats sont des faits extraits de reels sauvegardés. "
        "Utilise les liens source avant toute recommandation et ne prétends pas "
        "qu'une absence de résultat prouve une absence dans le monde réel."
    ),
)


def _db_path() -> Path:
    return Path(os.environ.get("REELS_DB_PATH", str(db.DEFAULT_DB))).expanduser().resolve()


def _connection() -> sqlite3.Connection:
    path = _db_path()
    if not path.is_file():
        raise ValueError(f"database unavailable: {path}")
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _limit(value: int, maximum: int = 20) -> int:
    return max(1, min(value, maximum))


@mcp.tool()
def database_summary() -> dict[str, Any]:
    """Return counts by content type to understand what the local database contains."""
    with closing(_connection()) as conn:
        return _database_summary(conn)


@mcp.tool()
def search_entities(query: str, type: str | None = None, city: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Search resolved places, products, brands and services with their source reels."""
    with closing(_connection()) as conn:
        return _search_entities(conn, query.strip(), type_=type or None, city=city or None, limit=_limit(limit))


@mcp.tool()
def get_entity(entity_id: int) -> dict[str, Any] | None:
    """Get one entity with its source reels and personal feedback history."""
    with closing(_connection()) as conn:
        return _get_entity(conn, entity_id)


@mcp.tool()
def get_reel(shortcode: str) -> dict[str, Any] | None:
    """Get the extracted fiche, evidence and local-media availability for one reel."""
    with closing(_connection()) as conn:
        return _get_reel(conn, shortcode.strip())


@mcp.tool()
def search_recipes(query: str = "", cuisine: str | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Search active cooking recipes by dish, extracted text or cuisine."""
    with closing(_connection()) as conn:
        return _search_recipes(conn, query.strip(), cuisine=cuisine or None, limit=_limit(limit))


@mcp.tool()
def search_repertoire(query: str = "", limit: int = 10) -> list[dict[str, Any]]:
    """Search guides, lessons, methods and exercises that are not catalogue entities."""
    with closing(_connection()) as conn:
        return _search_repertoire(conn, query.strip(), limit=_limit(limit))


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()

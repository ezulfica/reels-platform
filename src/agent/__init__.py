"""Read-only interface exposed to an external agent."""

from .queries import (
    database_summary,
    get_entity,
    get_reel,
    search_entities,
    search_recipes,
    search_repertoire,
)

__all__ = [
    "database_summary",
    "get_entity",
    "get_reel",
    "search_entities",
    "search_recipes",
    "search_repertoire",
]

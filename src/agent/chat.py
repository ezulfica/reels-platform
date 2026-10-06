"""Grounded, read-only answers over the extracted database."""

from __future__ import annotations

import json
import re
import sqlite3

import inference
from domain import repertoire

from . import search

_STOP_WORDS = {
    "avec",
    "dans",
    "pour",
    "quel",
    "quelle",
    "quels",
    "quelles",
    "des",
    "les",
    "une",
    "est",
    "sur",
    "the",
    "and",
    "que",
    "mon",
    "mes",
    "moi",
    "voir",
    "trouver",
    "veux",
}


def _terms(question: str) -> list[str]:
    return [
        token
        for token in re.findall(r"[\w-]+", question.casefold())
        if len(token) >= 3 and token not in _STOP_WORDS
    ][:6]


def answer(
    conn: sqlite3.Connection, question: str, *, history: list[dict] | None = None
) -> str:
    """Answer from a bounded set of database facts; never writes or runs work."""
    question = question.strip()
    if not question:
        raise ValueError("pose une question")
    if len(question) > 800:
        raise ValueError("la question doit faire au plus 800 caractères")

    terms = _terms(question)
    entities: list[dict] = []
    if terms:
        try:
            entities = search.search(conn, " OR ".join(terms), limit=8)
        except sqlite3.OperationalError:
            # Search must remain useful when an FTS tokenizer rejects an unusual term.
            entities = []
    recipes = repertoire.recipes(conn, text=terms[0] if terms else "", limit=6)
    context = {
        "entities": entities,
        "recipes": [
            {
                "dish_name": row["dish_name"],
                "cuisine": row["cuisine"],
                "cuisine_family": row["cuisine_family"],
                "summary": row["summary"],
                "shortcode": row["shortcode"],
                "url": row["url"],
            }
            for row in recipes
        ],
    }
    system = """Tu es l'assistant de recherche de Reels Platform. Réponds en français.
Tu ne connais que le CONTEXTE BASE fourni ci-dessous : n'ajoute aucune recommandation,
aucun détail ou lien qui n'y figure pas. Si le contexte ne permet pas de répondre,
dis-le clairement. Distingue les entités (lieux, produits, marques) des recettes.
Réponds de façon concise et cite le nom et le lien Instagram source quand ils existent.
Tu ne peux pas modifier la base, lancer un traitement, ni agir hors de cette conversation."""
    conversation = "\n".join(
        f"{item['role']}: {item['content']}" for item in (history or [])
    )
    user = (
        "CONVERSATION PRÉCÉDENTE:\n"
        + (conversation or "(nouvelle conversation)")
        + "\n\nQUESTION:\n"
        + question
        + "\n\nCONTEXTE BASE:\n"
        + json.dumps(context, ensure_ascii=False)
    )
    return inference.chat_text(
        inference.client(), inference.model("chat"), system, user
    )

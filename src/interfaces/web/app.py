"""Local web UI, reading and writing the same SQLite database as the CLI.

Local dashboard and browser for the extracted database.

The UI reads the same SQLite database as the CLI. It exposes explicit, manual
refresh actions, but never starts a download, extraction, or scan on page load.
No secrets are displayed or edited here.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from urllib.parse import urlencode

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from config import settings as config
from config import runtime
from storage import database as db
import inference as reels_llm
from domain import reel_library
from domain import repertoire as recipe_index
from domain import feedback as catalog_feedback
from pipeline.capture.download import MEDIA_ROOT
from pipeline.extract import extract as extract_llm
from pipeline.extract import review as extract_review
from agent import chat as agent_chat
from agent import conversations
from agent import search as reels_query

TEMPLATES_DIR = Path(__file__).parent / "templates"

app = FastAPI(title="Reels Platform")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# A dashboard needs a stable vocabulary, not the generated title of each reel.
# This projection is derived from already extracted facts: it is not persisted,
# never calls a model and can evolve without replaying the corpus.
_SPORT_TAGS = {
    "sport", "fitness", "workout", "yoga", "mobilite", "souplesse", "etirement",
    "renforcement", "musculation", "course", "tennis", "calisthenie", "posture",
    "abdominaux", "gainage", "echauffement", "entrainement", "preparation physique",
}


_THEME_RULES = (
    ("Food & restaurants", {"recipe", "restaurant"}, {"cuisine", "food", "restaurant", "recette", "izakaya", "gastronomie"}),
    ("Learning & languages", {"lesson"}, {"langue", "language", "japonais", "vocabulaire", "phrase"}),
    ("Sport & wellbeing", {"exercise"}, {"sport", "fitness", "workout", "yoga", "bien etre", "wellness"}),
    ("Travel & places", {"place", "lodging", "transport"}, {"voyage", "travel", "roadtrip", "citytrip", "visit", "tourisme", "excursion"}),
    ("Shopping & products", {"product", "brand", "shop"}, {"shopping", "souvenir", "cadeau", "achat", "occasion"}),
    ("Culture & media", {"media"}, {"anime", "art", "culture", "streaming", "fantasy", "histoire"}),
    ("Home & DIY", set(), {"maison", "mobilier", "decoration", "canape", "diy", "rangement", "nettoyage"}),
    ("Nature & outdoors", set(), {"nature", "randonnee", "montagne", "panorama", "outdoor", "plage"}),
    ("Style & beauty", set(), {"mode", "beaute", "beauty", "skincare", "vintage"}),
    ("Tech & creation", set(), {"ia", "photographie", "photo", "tech", "camera", "design"}),
)


def _is_sport(fiche: dict) -> bool:
    return bool({tag.casefold().strip() for tag in fiche["tags"]} & _SPORT_TAGS)


def _theme_for_reel(content_kind: str | None, entity_types: set[str], tags: set[str]) -> str:
    """Assign one broad dashboard theme from structured extraction fields."""
    normalized_tags = {tag.casefold().strip() for tag in tags}
    for label, types, markers in _THEME_RULES:
        if content_kind in types or entity_types & types or normalized_tags & markers:
            return label
    return "Other inspiration"


def _conn():
    """Open the database selected by `reels --db ... web`.

    The environment is set by the CLI before Uvicorn imports this ASGI app; the
    fallback keeps `uvicorn interfaces.web.app:app` convenient in development.
    """
    db_path = os.environ.get("REELS_DB_PATH", str(db.DEFAULT_DB))
    conn = db.connect(db_path)
    db.initialize(conn)
    db.initialize_web_state(conn)
    return conn


def _dashboard(conn) -> dict:
    """Small, bounded view of database health for the local home page."""
    saved_where = "WHERE unsaved_at IS NULL"
    scalar = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731
    counts = {
        "saved": scalar(f"SELECT COUNT(*) FROM reel {saved_where}"),
        "downloaded": scalar(
            "SELECT COUNT(*) FROM media m JOIN reel r ON r.shortcode=m.shortcode "
            "WHERE r.unsaved_at IS NULL AND m.mp4_path IS NOT NULL"
        ),
        "transcribed": scalar(
            "SELECT COUNT(DISTINCT t.shortcode) FROM transcript t JOIN reel r "
            "ON r.shortcode=t.shortcode WHERE r.unsaved_at IS NULL"
        ),
        "ocr": scalar(
            "SELECT COUNT(DISTINCT o.shortcode) FROM screen_text o JOIN reel r "
            "ON r.shortcode=o.shortcode WHERE r.unsaved_at IS NULL"
        ),
        "extracted": scalar(
            "SELECT COUNT(*) FROM extraction e JOIN reel r ON r.shortcode=e.shortcode "
            "WHERE r.unsaved_at IS NULL AND e.ok=1"
        ),
        "entities": scalar("SELECT COUNT(*) FROM entity"),
        "repertoire": scalar("SELECT COUNT(*) FROM repertoire_entry"),
        "recipes": scalar("SELECT COUNT(*) FROM recipe WHERE active=1"),
    }
    theme_rows = conn.execute(
        "SELECT e.shortcode, e.content_kind, e.tags_json, "
        "GROUP_CONCAT(DISTINCT c.type) AS entity_types, "
        "GROUP_CONCAT(DISTINCT tag.value) AS candidate_tags "
        "FROM extraction e JOIN reel r ON r.shortcode=e.shortcode "
        "LEFT JOIN candidate c ON c.shortcode=e.shortcode "
        "LEFT JOIN json_each(CASE WHEN json_valid(c.tags_json) THEN c.tags_json ELSE '[]' END) tag "
        "WHERE r.unsaved_at IS NULL AND e.ok=1 GROUP BY e.shortcode"
    ).fetchall()
    themes: dict[str, int] = {}
    for row in theme_rows:
        tags: set[str] = set()
        try:
            tags.update(value for value in json.loads(row["tags_json"] or "[]") if isinstance(value, str))
        except json.JSONDecodeError:
            # A malformed historical tag payload must not hide a reel or break the page.
            pass
        tags.update(value for value in (row["candidate_tags"] or "").split(",") if value)
        entity_types = set((row["entity_types"] or "").split(",")) - {""}
        theme = _theme_for_reel(row["content_kind"], entity_types, tags)
        themes[theme] = themes.get(theme, 0) + 1
    topics = [
        {"label": label, "count": count}
        for label, count in sorted(themes.items(), key=lambda item: (-item[1], item[0]))
    ]
    entity_types = conn.execute(
        "SELECT type AS label, COUNT(*) AS count FROM entity "
        "GROUP BY type ORDER BY count DESC, label LIMIT 10"
    ).fetchall()
    recent = conn.execute(
        "SELECT r.shortcode, r.username, r.caption, r.url, r.first_seen_at, "
        "cl.predicted_topic, EXISTS(SELECT 1 FROM extraction e WHERE e.shortcode=r.shortcode) AS extracted "
        "FROM reel r LEFT JOIN classification cl ON cl.shortcode=r.shortcode "
        "WHERE r.unsaved_at IS NULL ORDER BY r.first_seen_at DESC LIMIT 8"
    ).fetchall()
    return {"counts": counts, "topics": topics, "entity_types": entity_types, "recent": recent}


# ---------------------------------------------------------------- dashboard


@app.get("/")
def dashboard(request: Request):
    return templates.TemplateResponse(request, "dashboard.html", _dashboard(_conn()))


# ------------------------------------------------------------------- reels


@app.get("/reels")
def reel_list(request: Request, flag: str | None = None, sort: str = "added_desc", page: int = 1, view: str = "grid"):
    conn = _conn()
    per_page = 25
    offset = (page - 1) * per_page
    ordering = {"added_desc": "r.first_seen_at DESC", "added_asc": "r.first_seen_at ASC"}
    if sort not in ordering:
        raise HTTPException(status_code=400, detail="unknown reel sort")
    if view not in ("list", "grid"):
        raise HTTPException(status_code=400, detail="unknown view")

    if flag == "a_revoir":
        # The same criteria as `reels review`, but one flag per reel rather than
        # a flat list of candidates.
        flagged_shortcodes = {i["shortcode"] for i in extract_review.queue(conn)}
        if not flagged_shortcodes:
            rows = []
            total = 0
        else:
            placeholders = ",".join("?" * len(flagged_shortcodes))
            rows = conn.execute(
                f"""
                SELECT r.shortcode, r.username, r.caption, r.url, r.first_seen_at,
                       cl.predicted_topic, cl.is_actionable, cl.confidence,
                       (SELECT COUNT(*) FROM candidate c
                        WHERE c.shortcode = r.shortcode) AS n_entities
                FROM reel r
                LEFT JOIN classification cl ON cl.shortcode = r.shortcode
                WHERE r.shortcode IN ({placeholders})
                ORDER BY {ordering[sort]} LIMIT ? OFFSET ?
                """,
                (*flagged_shortcodes, per_page, offset),
            ).fetchall()
            total = len(flagged_shortcodes)
    else:
        rows = conn.execute(
            f"""
            SELECT r.shortcode, r.username, r.caption, r.url, r.first_seen_at,
                   cl.predicted_topic, cl.is_actionable, cl.confidence,
                   (SELECT COUNT(*) FROM candidate c
                    WHERE c.shortcode = r.shortcode) AS n_entities
            FROM reel r
            LEFT JOIN classification cl ON cl.shortcode = r.shortcode
            WHERE r.unsaved_at IS NULL
            ORDER BY {ordering[sort]} LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        ).fetchall()
        total = conn.execute(
            "SELECT COUNT(*) FROM reel WHERE unsaved_at IS NULL"
        ).fetchone()[0]

    return templates.TemplateResponse(
        request,
        "reel_list.html",
        {
            "reels": rows,
            "page": page,
            "total": total,
            "per_page": per_page,
            "flag": flag,
            "sort": sort,
        },
    )


@app.get("/reel/{shortcode}")
def reel_detail(request: Request, shortcode: str):
    conn = _conn()
    reel = conn.execute(
        "SELECT * FROM reel WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    if not reel:
        return templates.TemplateResponse(
            request, "not_found.html", {"shortcode": shortcode}, status_code=404
        )

    asr = conn.execute(
        "SELECT * FROM transcript WHERE shortcode = ? ORDER BY created_at DESC LIMIT 1",
        (shortcode,),
    ).fetchone()
    ocr = conn.execute(
        "SELECT * FROM screen_text WHERE shortcode = ? ORDER BY created_at DESC LIMIT 1",
        (shortcode,),
    ).fetchone()
    classification = conn.execute(
        "SELECT * FROM classification WHERE shortcode = ?", (shortcode,)
    ).fetchone()
    candidates = conn.execute(
        "SELECT * FROM candidate WHERE shortcode = ? ORDER BY source, id",
        (shortcode,),
    ).fetchall()
    media = conn.execute(
        "SELECT mp4_path, proxy_path, poster_path FROM media WHERE shortcode = ?",
        (shortcode,),
    ).fetchone()
    proxy_path = _managed_path(media["proxy_path"]) if media else None
    original_path = _managed_path(media["mp4_path"]) if media else None
    poster_path = _managed_path(media["poster_path"]) if media else None

    return templates.TemplateResponse(
        request,
        "reel_detail.html",
        {
            "reel": reel,
            "asr": asr,
            "ocr": ocr,
            "classification": classification,
            "candidates": candidates,
            "types": extract_llm.TYPES,
            # Prefer the derived lightweight proxy; the original is only a local fallback.
            "video_url": f"/media/{shortcode}/view" if proxy_path or original_path else None,
            "poster_url": f"/media/{shortcode}/poster" if poster_path else None,
        },
    )



# ------------------------------------------------------------------ gold


@app.get("/catalog")
def catalog_list(request: Request, q: str = "", type_: str = "", status: str = "", view: str = "grid"):
    conn = _conn()
    if view not in ("list", "grid"):
        raise HTTPException(status_code=400, detail="unknown view")
    if status and status not in catalog_feedback.STATUSES:
        raise HTTPException(status_code=400, detail="unknown catalogue status")
    if q.strip():
        # Apply personal shortlist filtering after searching the full catalogue,
        # so an older shortlisted entity is not hidden by the default page cap.
        results = reels_query.search(
            conn, q, type_=type_ or None, limit=100000 if status else 100
        )
    else:
        sql = "SELECT id FROM entity"
        params: list = []
        clauses = []
        if type_:
            clauses.append("type = ?")
            params.append(type_)
        if status:
            clauses.append(
                "EXISTS (SELECT 1 FROM entity_personal p"
                " WHERE p.entity_id=entity.id AND p.status=?)"
            )
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY canonical_name LIMIT 200"
        ids = [r["id"] for r in conn.execute(sql, params)]
        results = []
        for entity_id in ids:
            entity = conn.execute(
                "SELECT * FROM entity WHERE id = ?", (entity_id,)
            ).fetchone()
            reels = conn.execute(
                "SELECT r.shortcode, r.url, r.username, m.poster_path FROM entity_reel ger"
                " JOIN reel r ON r.shortcode = ger.shortcode"
                " LEFT JOIN media m ON m.shortcode = r.shortcode"
                " WHERE ger.entity_id = ?",
                (entity_id,),
            ).fetchall()
            results.append(
                {
                    "id": entity["id"],
                    "name": entity["canonical_name"],
                    "type": entity["type"],
                    "facets": json.loads(entity["facets_json"] or "[]"),
                    "scale": entity["scale"],
                    "locality": entity["locality"],
                    "city": entity["city"],
                    "country": entity["country"],
                    "address": entity["address"],
                    "highlights": entity["highlights"],
                    "why_saved": entity["why_saved"],
                    "gmaps_url": entity["gmaps_url"],
                    "reels": [dict(r) for r in reels],
                    "preview": next(
                        (
                            {
                                "shortcode": reel["shortcode"],
                                "url": f"/media/{reel['shortcode']}/poster",
                            }
                            for reel in reels
                            if _managed_path(reel["poster_path"])
                        ),
                        None,
                    ),
                }
            )

    for entity in results:
        personal = conn.execute(
            "SELECT status,note,score FROM entity_personal WHERE entity_id=?",
            (entity["id"],),
        ).fetchone()
        entity["status"] = personal["status"] if personal else None
        entity["personal_note"] = personal["note"] if personal else None
        entity["score"] = personal["score"] if personal else None
        entity["history"] = catalog_feedback.history(conn, entity["id"])
    if status:
        results = [entity for entity in results if entity["status"] == status]
    return templates.TemplateResponse(
        request,
        "catalog_list.html",
        {
            "results": results,
            "q": q,
            "type_": type_,
            "types": extract_llm.TYPES,
            "status": status,
            "personal_statuses": catalog_feedback.STATUSES,
            "view": view,
        },
    )


@app.get("/entity/{entity_id}")
def entity_detail(request: Request, entity_id: int):
    conn = _conn()
    entity = conn.execute("SELECT * FROM entity WHERE id=?", (entity_id,)).fetchone()
    if not entity:
        return templates.TemplateResponse(request, "not_found.html", {"shortcode": entity_id}, status_code=404)
    sources = conn.execute(
        "SELECT r.shortcode, r.username, r.url, r.caption, cl.predicted_topic, m.poster_path "
        "FROM entity_reel er JOIN reel r ON r.shortcode=er.shortcode "
        "LEFT JOIN classification cl ON cl.shortcode=r.shortcode "
        "LEFT JOIN media m ON m.shortcode=r.shortcode "
        "WHERE er.entity_id=? ORDER BY r.taken_at DESC",
        (entity_id,),
    ).fetchall()
    personal = conn.execute(
        "SELECT status,note,score FROM entity_personal WHERE entity_id=?", (entity_id,)
    ).fetchone()
    return templates.TemplateResponse(
        request,
        "entity_detail.html",
        {
            "entity": entity,
            "sources": [
                {**dict(source), "poster_url": f"/media/{source['shortcode']}/poster" if _managed_path(source["poster_path"]) else None}
                for source in sources
            ],
            "personal": personal,
            "history": catalog_feedback.history(conn, entity_id),
            "personal_statuses": catalog_feedback.STATUSES,
        },
    )


@app.post("/catalog/{entity_id}/feedback")
def catalog_feedback_submit(
    entity_id: int,
    status: str = Form(...),
    note: str = Form(""),
    score: str = Form(""),
    q: str = Form(""),
    type_: str = Form(""),
    return_to: str = Form(""),
):
    conn = _conn()
    try:
        if status == "clear":
            catalog_feedback.clear_status(conn, entity_id)
        else:
            values = {"status": status or None}
            if note.strip():
                values["note"] = note.strip()
            if score.strip():
                values["score"] = int(score)
            catalog_feedback.update(conn, entity_id, **values)
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    if return_to == f"/entity/{entity_id}":
        return RedirectResponse(return_to, status_code=303)
    return RedirectResponse("/catalog?" + urlencode({"q": q, "type_": type_}), status_code=303)


@app.get("/recipes")
def recipe_list(
    request: Request, q: str = "", cuisine: str = "", course: str = "", status: str = "", view: str = "grid"
):
    conn = _conn()
    if view not in ("list", "grid"):
        raise HTTPException(status_code=400, detail="unknown view")
    if status and status not in (*recipe_index.RECIPE_VERDICTS, "untried"):
        raise HTTPException(status_code=400, detail="unknown personal recipe status")
    rows = recipe_index.recipes(
        conn,
        cuisine=cuisine or None,
        course=course or None,
        verdict=status or None,
        text=q,
        limit=200,
    )
    # Options follow the current result set, instead of exposing every historic
    # vocabulary value regardless of the selected text, course or personal state.
    cuisines = sorted({value for recipe in rows for value in (recipe["cuisine_family"], recipe["cuisine"]) if value})
    courses = sorted({recipe["course"] for recipe in rows if recipe["course"]})
    return templates.TemplateResponse(
        request,
        "recipe_list.html",
        {
            "recipes": rows,
            "q": q,
            "cuisine": cuisine,
            "course": course,
            "status": status,
            "cuisines": cuisines,
            "courses": courses,
            "statuses": recipe_index.RECIPE_VERDICTS,
            "view": view,
        },
    )


@app.get("/repertoire")
def repertoire_list(request: Request, q: str = "", view: str = "grid"):
    if view not in ("list", "grid"):
        raise HTTPException(status_code=400, detail="unknown view")
    conn = _conn()
    results = reel_library.search(conn, q)
    return templates.TemplateResponse(
        request,
        "repertoire_list.html",
        {
            "results": results,
            "q": q,
            "heading": "Guides & inspirations",
            "description": "Saved methods, lessons, guides, and inspiration.",
            "view": view,
        },
    )


@app.get("/sport")
def sport_list(request: Request, q: str = "", view: str = "grid"):
    if view not in ("list", "grid"):
        raise HTTPException(status_code=400, detail="unknown view")
    conn = _conn()
    exercises = reel_library.search(conn, q, content_kind="exercise", limit=200)
    results = [fiche for fiche in exercises if _is_sport(fiche)]
    return templates.TemplateResponse(
        request,
        "repertoire_list.html",
        {
            "results": results,
            "q": q,
            "heading": "Sport & mobility",
            "description": "Sport, mobility, and strength exercises extracted from saved reels.",
            "view": view,
        },
    )


@app.get("/repertoire/{shortcode}")
def repertoire_detail(request: Request, shortcode: str):
    conn = _conn()
    fiche = reel_library.get(conn, shortcode)
    if not fiche or fiche["mode"] != "repertoire":
        return templates.TemplateResponse(
            request, "not_found.html", {"shortcode": shortcode}, status_code=404
        )
    recipes = [
        dict(row)
        for row in conn.execute(
            "SELECT * FROM recipe WHERE shortcode=? AND active=1 ORDER BY id",
            (shortcode,),
        ).fetchall()
    ]
    for recipe in recipes:
        recipe["dietary_tags"] = json.loads(recipe["dietary_tags_json"] or "[]")
        recipe["evidence"] = json.loads(recipe["evidence_json"] or "[]")
        recipe["attempts"] = recipe_index.recipe_attempts(conn, recipe["id"])
    return templates.TemplateResponse(
        request,
        "repertoire_detail.html",
        {
            "fiche": fiche,
            "has_local_media": reel_library.local_media_path(fiche, MEDIA_ROOT)
            is not None,
            "has_proxy_media": _managed_path(fiche.get("proxy_path")) is not None,
            "poster_url": f"/media/{shortcode}/poster"
            if _managed_path(fiche.get("poster_path"))
            else None,
            "recipes": recipes,
        },
    )


@app.post("/repertoire/{shortcode}/recipes/{recipe_id}/feedback")
def recipe_feedback_submit(
    shortcode: str,
    recipe_id: int,
    verdict: str = Form(...),
    rating: str = Form(""),
    note: str = Form(""),
    changes_made: str = Form(""),
    cooked_at: str = Form(""),
):
    conn = _conn()
    belongs = conn.execute(
        "SELECT 1 FROM recipe WHERE id=? AND shortcode=? AND active=1",
        (recipe_id, shortcode),
    ).fetchone()
    if not belongs:
        raise HTTPException(status_code=404, detail="recipe not found")
    try:
        recipe_index.record_attempt(
            conn,
            recipe_id,
            verdict=verdict,
            rating=int(rating) if rating.strip() else None,
            note=note.strip() or None,
            changes_made=changes_made.strip() or None,
            cooked_at=cooked_at.strip() or None,
        )
    except (ValueError, TypeError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return RedirectResponse(f"/repertoire/{shortcode}", status_code=303)


@app.get("/media/{shortcode}")
def local_media(shortcode: str):
    """Serve only a downloaded reel known to the database, never an arbitrary path."""
    fiche = reel_library.get(_conn(), shortcode)
    path = reel_library.local_media_path(fiche, MEDIA_ROOT) if fiche else None
    if path is None:
        raise HTTPException(status_code=404, detail="local video unavailable")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


def _managed_path(raw_path: str | None) -> Path | None:
    if not raw_path:
        return None
    try:
        path = Path(raw_path).resolve()
        path.relative_to(MEDIA_ROOT.resolve())
    except (OSError, ValueError):
        return None
    return path if path.is_file() else None


@app.get("/media/{shortcode}/view")
def view_media(shortcode: str):
    fiche = reel_library.get(_conn(), shortcode)
    path = _managed_path(fiche.get("proxy_path")) if fiche else None
    path = path or (reel_library.local_media_path(fiche, MEDIA_ROOT) if fiche else None)
    if path is None:
        raise HTTPException(status_code=404, detail="local video unavailable")
    return FileResponse(path, media_type="video/mp4", filename=path.name)


@app.get("/media/{shortcode}/poster")
def view_poster(shortcode: str):
    fiche = reel_library.get(_conn(), shortcode)
    path = _managed_path(fiche.get("poster_path")) if fiche else None
    if path is None:
        raise HTTPException(status_code=404, detail="local poster unavailable")
    media_type = {".png": "image/png", ".webp": "image/webp"}.get(
        path.suffix.lower(), "image/jpeg"
    )
    return FileResponse(path, media_type=media_type, filename=path.name)



@app.get("/settings")
def settings_page(request: Request):
    values = runtime.load()
    return templates.TemplateResponse(request, "settings.html", {"values": values, "profiles": runtime.profiles(), "days": runtime.DAYS, "saved": request.query_params.get("saved") == "1", "error": None})


@app.post("/settings")
def settings_save(request: Request, enabled: str = Form(""), day: str = Form(...), time: str = Form(...), profile: str = Form(...)):
    try:
        values = runtime.save({"enabled": enabled == "on", "day": day, "time": time, "profile": profile})
        runtime.apply(values)
    except (ValueError, OSError, subprocess.SubprocessError) as error:
        return templates.TemplateResponse(request, "settings.html", {"values": runtime.load(), "profiles": runtime.profiles(), "days": runtime.DAYS, "saved": False, "error": str(error)}, status_code=400)
    return RedirectResponse("/settings?saved=1", status_code=303)

# ------------------------------------------------------------------- chat


@app.get("/chat")
def chat_page(request: Request, session_id: int | None = None):
    conn = _conn()
    active = conversations.get(conn, session_id) if session_id else None
    if session_id and not active:
        raise HTTPException(status_code=404, detail="conversation introuvable")
    return templates.TemplateResponse(request, "chat.html", {"sessions": conversations.sessions(conn), "active": active, "error": None})


@app.post("/chat")
def chat_submit(request: Request, message: str = Form(...), session_id: str = Form("")):
    try:
        identifier = conversations.ask(_conn(), int(session_id) if session_id else None, message)
    except ValueError as error:
        conn = _conn()
        return templates.TemplateResponse(request, "chat.html", {"sessions": conversations.sessions(conn), "active": None, "error": str(error)}, status_code=400)
    return RedirectResponse(f"/chat?session_id={identifier}", status_code=303)


@app.post("/chat/{session_id}/delete")
def chat_delete(session_id: int):
    conversations.delete(_conn(), session_id)
    return RedirectResponse("/chat", status_code=303)

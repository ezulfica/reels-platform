from __future__ import annotations

import asyncio

import httpx
import pytest

from config import runtime
from domain import repertoire
from interfaces.web import app as web
from pipeline.capture.instagram import upsert_reels
from storage import database as db


class _ASGIClient:
    """Sync facade over ASGITransport for normal pytest route tests."""

    def __init__(self, app):
        self.app = app

    def request(self, method, path, *, params=None, data=None, follow_redirects=True):
        async def send():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=self.app),
                base_url="http://test",
                follow_redirects=follow_redirects,
            ) as client:
                return await client.request(method, path, params=params, data=data)

        return asyncio.run(send())

    def get(self, path, *, params=None):
        return self.request("GET", path, params=params)

    def post(self, path, *, data=None, follow_redirects=True):
        return self.request("POST", path, data=data, follow_redirects=follow_redirects)


@pytest.fixture
def web_client(tmp_path, monkeypatch):
    db_path = tmp_path / "web.db"
    conn = db.connect(db_path)
    db.initialize(conn)

    def connect_for_request():
        request_conn = db.connect(db_path)
        db.initialize(request_conn)
        return request_conn

    monkeypatch.setattr(web, "_conn", connect_for_request)

    # This environment's AnyIO worker-thread backend cannot start threads.
    # Keep ASGI dispatch intact while running these local SQLite handlers inline.
    async def inline_sync_call(function, *args, **kwargs):
        kwargs.pop("limiter", None)
        return function(*args, **kwargs)

    import fastapi.routing
    import starlette.routing

    monkeypatch.setattr(starlette.routing, "run_in_threadpool", inline_sync_call)
    monkeypatch.setattr(fastapi.routing, "run_in_threadpool", inline_sync_call)
    import anyio.to_thread

    monkeypatch.setattr(anyio.to_thread, "run_sync", inline_sync_call)
    monkeypatch.setattr(web, "MEDIA_ROOT", tmp_path / "media")
    return conn, _ASGIClient(web.app), tmp_path


def _recipe(conn, shortcode="FOOD"):
    upsert_reels(
        conn,
        [
            {
                "pk": 123,
                "code": shortcode,
                "taken_at": 1721000000,
                "media_type": 2,
                "product_type": "clips",
                "caption": {"text": "Udon au curry, faire revenir les oignons."},
                "user": {"username": "cuisinier"},
            }
        ],
    )
    conn.execute(
        "INSERT INTO extraction(shortcode,model,prompt_sha,extracted_at,raw_response,mode,content_kind,ok)"
        " VALUES (?,'test','sha','2026-09-12','{}','repertoire','recipe',1)",
        (shortcode,),
    )
    conn.execute(
        "INSERT INTO classification(shortcode,predicted_topic,why_saved,created_at) VALUES (?, ?, ?, ?)",
        (shortcode, "Udon crémeux", "Dîner rapide", "2026-09-12"),
    )
    repertoire.upsert_entry(
        conn,
        shortcode=shortcode,
        content_kind="recipe",
        title="Udon crémeux",
        summary="Une recette indexée.",
        recipes=[
            {
                "dish_name": "Udon crémeux au curry",
                "cuisine": "japonaise",
                "cuisine_family": "asiatique",
                "course": "plat",
                "summary": "Cuire les udon.",
                "evidence": [
                    {
                        "source": "caption",
                        "quote": "faire revenir les oignons",
                        "location": "00:04",
                    }
                ],
                "dietary_tags": ["rapide"],
                "confidence": 0.9,
            }
        ],
    )
    conn.commit()
    return conn.execute(
        "SELECT id FROM recipe WHERE shortcode=?", (shortcode,)
    ).fetchone()[0]


def test_web_connection_uses_selected_database_path(tmp_path, monkeypatch):
    selected = tmp_path / "selected.db"
    monkeypatch.setenv("REELS_DB_PATH", str(selected))

    conn = web._conn()

    assert selected.exists()
    assert conn.execute("SELECT 1 FROM sqlite_master WHERE name='reel'").fetchone()


def test_dashboard_summarises_saved_data_and_links_to_browser(web_client):
    conn, client, _ = web_client
    _recipe(conn, "DASH")

    response = client.get("/")

    assert response.status_code == 200
    assert "Overview" in response.text
    assert "Saved items" in response.text
    assert "Udon crémeux" in response.text
    assert 'href="/reels"' in response.text


def test_settings_selects_a_validated_llm_profile(web_client, monkeypatch):
    _, client, tmp_path = web_client
    monkeypatch.setattr(runtime, "CONFIG_PATH", tmp_path / "runtime.json")
    applied = {}
    monkeypatch.setattr(runtime, "apply", lambda value: applied.update(value))

    page = client.get("/settings")
    assert page.status_code == 200
    assert "Connection, model, and reasoning effort" in page.text
    assert "Codex · Terra · medium" in page.text

    response = client.post(
        "/settings",
        data={
            "enabled": "on",
            "day": "sun",
            "time": "10:00",
            "profile": "codex-terra-medium",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert applied["profile"] == "codex-terra-medium"


def test_recipe_filters_and_detail_render_source_personal_and_optional_links(
    web_client, monkeypatch
):
    conn, client, tmp_path = web_client
    recipe_id = _recipe(conn)
    root = tmp_path / "media"
    root.mkdir()
    proxy = root / "FOOD-proxy.mp4"
    poster = root / "FOOD-cover.jpg"
    proxy.write_bytes(b"proxy")
    poster.write_bytes(b"poster")
    conn.execute(
        "INSERT INTO media(shortcode,proxy_path,poster_path) VALUES ('FOOD',?,?)",
        (str(proxy), str(poster)),
    )
    conn.commit()

    filtered = client.get(
        "/recipes",
        params={"cuisine": "asiatique", "course": "plat", "status": "untried"},
    )
    assert filtered.status_code == 200
    assert "Udon crémeux au curry" in filtered.text
    assert 'name="cuisine"' in filtered.text and 'name="course"' in filtered.text

    detail = client.get("/repertoire/FOOD")
    assert detail.status_code == 200
    assert "View Instagram source" in detail.text
    assert "Facts indexed from the source" in detail.text
    assert "My feedback and attempts" in detail.text
    assert "faire revenir les oignons" in detail.text
    assert "/media/FOOD/view" in detail.text and "/media/FOOD/poster" in detail.text

    response = client.post(
        f"/repertoire/FOOD/recipes/{recipe_id}/feedback",
        data={
            "verdict": "favorite",
            "rating": "5",
            "note": "À refaire",
            "changes_made": "moins de sel",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    history = repertoire.recipe_attempts(conn, recipe_id)
    assert len(history) == 1 and history[0]["verdict"] == "favorite"
    rendered = client.get("/repertoire/FOOD")
    assert "À refaire" in rendered.text and "moins de sel" in rendered.text

    served = client.get("/media/FOOD/view")
    assert served.status_code == 200 and served.content == b"proxy"
    assert client.get("/media/FOOD/poster").status_code == 200


def test_dashboard_aggregates_recipe_into_a_broad_theme(web_client):
    conn, client, _ = web_client
    _recipe(conn, "THEME")

    response = client.get("/")

    assert response.status_code == 200
    assert "Food &amp; restaurants" in response.text
    assert "Guide de voyage à Taïwan" not in response.text


def test_chat_uses_read_only_answer_service(web_client, monkeypatch):
    _, client, _ = web_client
    called = {}

    def fake_answer(conn, question, *, history=None):
        called["question"] = question
        called["history"] = history
        assert conn.execute("SELECT COUNT(*) FROM reel").fetchone()[0] == 0
        return "Aucune entité correspondante."

    monkeypatch.setattr(web.agent_chat, "answer", fake_answer)
    response = client.post("/chat", data={"message": "Un restaurant à Paris ?"})

    assert response.status_code == 200
    assert "Aucune entité correspondante." in response.text
    assert called["question"] == "Un restaurant à Paris ?"
    assert called["history"] == []
    assert "Conversations" in response.text


def test_reel_detail_embeds_local_proxy_and_poster(web_client):
    conn, client, tmp_path = web_client
    _recipe(conn, "WATCH")
    media_root = tmp_path / "media"
    media_root.mkdir()
    proxy = media_root / "WATCH-proxy.mp4"
    poster = media_root / "WATCH-cover.jpg"
    proxy.write_bytes(b"proxy")
    poster.write_bytes(b"poster")
    conn.execute(
        "INSERT INTO media(shortcode,proxy_path,poster_path) VALUES ('WATCH',?,?)",
        (str(proxy), str(poster)),
    )
    conn.commit()

    page = client.get("/reel/WATCH")

    assert page.status_code == 200
    assert "<video controls" in page.text
    assert "/media/WATCH/view" in page.text
    assert "/media/WATCH/poster" in page.text


def test_catalogue_shortlist_feedback_is_explicit_and_keeps_history(web_client):
    conn, client, _ = web_client
    upsert_reels(
        conn,
        [
            {
                "pk": 123,
                "code": "ITEM",
                "taken_at": 1721000000,
                "media_type": 2,
                "product_type": "clips",
                "caption": {"text": "Un objectif léger"},
                "user": {"username": "photo"},
            }
        ],
    )
    entity_id = conn.execute(
        "INSERT INTO entity(canonical_name,type,facets_json,highlights,updated_at)"
        " VALUES ('Objectif léger','product','[]','compact','2026-09-12') RETURNING id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO entity_reel(entity_id,shortcode) VALUES (?, 'ITEM')", (entity_id,)
    )
    conn.execute(
        "INSERT INTO entity_fts(rowid,canonical_name,summary,highlights,tags)"
        " VALUES (?, 'Objectif léger','','compact','photo')",
        (entity_id,),
    )
    conn.commit()

    entity_page = client.get(f"/entity/{entity_id}")
    assert entity_page.status_code == 200
    assert "Associated reels (1)" in entity_page.text
    assert "/reel/ITEM" in entity_page.text

    response = client.post(
        f"/catalog/{entity_id}/feedback",
        data={
            "status": "shortlisted",
            "note": "À comparer",
            "score": "4",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    shortlist = client.get("/catalog", params={"status": "shortlisted"})
    assert "Objectif léger" in shortlist.text
    assert "À comparer" not in shortlist.text
    assert "My tracking" not in shortlist.text
    detail = client.get(f"/entity/{entity_id}")
    assert "My tracking" in detail.text
    assert "À comparer" in detail.text
    assert "History (1)" in detail.text

    client.post(
        f"/catalog/{entity_id}/feedback",
        data={"status": "clear"},
        follow_redirects=False,
    )
    assert (
        conn.execute(
            "SELECT status FROM entity_personal WHERE entity_id=?", (entity_id,)
        ).fetchone()[0]
        is None
    )
    history = conn.execute(
        "SELECT status,note FROM entity_personal_history WHERE entity_id=? ORDER BY id",
        (entity_id,),
    ).fetchall()
    assert [(row["status"], row["note"]) for row in history] == [
        ("shortlisted", "À comparer"),
        (None, "À comparer"),
    ]


def test_media_links_are_optional_and_paths_outside_media_root_are_not_served(
    web_client,
):
    conn, client, tmp_path = web_client
    _recipe(conn, "SAFE")
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"private")
    conn.execute(
        "INSERT INTO media(shortcode,mp4_path) VALUES ('SAFE',?)", (str(outside),)
    )
    conn.commit()

    detail = client.get("/repertoire/SAFE")
    assert detail.status_code == 200
    assert "Local video is currently unavailable" in detail.text
    assert client.get("/media/SAFE/view").status_code == 404

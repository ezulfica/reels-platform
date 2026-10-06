"""Persistent, bounded chat sessions over the read-only research assistant."""
from __future__ import annotations

import sqlite3
from storage.database import now
from . import chat

_MAX_CONTEXT_MESSAGES = 12


def sessions(conn: sqlite3.Connection) -> list[dict]:
    return [dict(row) for row in conn.execute("SELECT id,title,created_at,updated_at FROM chat_session ORDER BY updated_at DESC, id DESC")]


def get(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT id,title,created_at,updated_at FROM chat_session WHERE id=?", (session_id,)).fetchone()
    if not row:
        return None
    messages = [dict(item) for item in conn.execute("SELECT id,role,content,created_at FROM chat_message WHERE session_id=? ORDER BY id", (session_id,))]
    return {**dict(row), "messages": messages}


def _title(message: str) -> str:
    compact = " ".join(message.split())
    return compact[:72] + ("…" if len(compact) > 72 else "")


def ask(conn: sqlite3.Connection, session_id: int | None, message: str) -> int:
    message = message.strip()
    if not message:
        raise ValueError("pose une question")
    if len(message) > 800:
        raise ValueError("la question doit faire au plus 800 caractères")
    if session_id is None:
        cursor = conn.execute("INSERT INTO chat_session(title,created_at,updated_at) VALUES (?,?,?)", (_title(message), now(), now()))
        session_id = int(cursor.lastrowid)
    elif not conn.execute("SELECT 1 FROM chat_session WHERE id=?", (session_id,)).fetchone():
        raise ValueError("conversation introuvable")
    conn.execute("INSERT INTO chat_message(session_id,role,content,created_at) VALUES (?,?,?,?)", (session_id, "user", message, now()))
    history = [dict(row) for row in conn.execute("SELECT role,content FROM chat_message WHERE session_id=? ORDER BY id DESC LIMIT ?", (session_id, _MAX_CONTEXT_MESSAGES)).fetchall()][::-1]
    response = chat.answer(conn, message, history=history[:-1])
    conn.execute("INSERT INTO chat_message(session_id,role,content,created_at) VALUES (?,?,?,?)", (session_id, "assistant", response, now()))
    conn.execute("UPDATE chat_session SET updated_at=? WHERE id=?", (now(), session_id))
    conn.commit()
    return session_id


def delete(conn: sqlite3.Connection, session_id: int) -> None:
    conn.execute("DELETE FROM chat_session WHERE id=?", (session_id,))
    conn.commit()

# Local web interface

The web interface is a local FastAPI application. It reads the same SQLite
database as the CLI and does not start pipeline work while loading a page.

## Run

```bash
make dev
make web
```

`make dev` synchronizes locked development and optional interface dependencies,
creates `.env` from the template only when absent, then enables Git hooks.
`make web` starts the app on `127.0.0.1:8420`; `make watch` adds automatic
reload. None of these commands start pipeline work. Adapt the port or database
with, for example, `make web PORT=8421 DB=/path/to/reels.db`. Run `make doctor`
to inspect optional local tools.

The CLI passes the selected database through `REELS_DB_PATH`; Uvicorn imports
`interfaces.web.app:app`. Development defaults to `db/reels.db`.

## Structure and data

`src/interfaces/web/app.py` contains FastAPI routes and adapts domain facts for
Jinja templates. Templates live in `src/interfaces/web/templates/`; tests live
in `tests/test_web.py`. Catalogue, recipe and reel pages query SQLite. Personal
entity tracking writes explicitly to `entity_personal` with append-only history.
Media is served only under `MEDIA_ROOT`; source MP4s remain unchanged.

Chat sessions and messages use dedicated `chat_session` and `chat_message`
tables, separate from extracted facts. Each turn searches SQLite first, then
sends bounded context to the configured LLM.

## Scheduling and LLM profile

`/settings` stores schedule and inference profile in
`~/.config/reels-platform/runtime.yaml`. Applying it writes user-systemd
overrides for `reels-weekly.service` and `reels-weekly.timer`, then reloads
systemd. It never reads or writes API keys, Instagram cookies or Codex sessions.
Profiles choose the connection, model, and reasoning effort through
non-sensitive backend variables. They apply to the next weekly run and new local
chat or manual pipeline requests; the matching connection must already work on
the machine.

Profiles live in the versioned `config/llm-profiles.yaml` catalogue. The UI
selects a profile; profile definitions stay reviewable as code. An optional
local catalogue at `~/.config/reels-platform/llm-profiles.local.yaml` can add
machine-specific, non-secret profiles without changing the project file.

# Local setup and operation

Reels Platform is a local application. SQLite, downloaded media, cookies, model
credentials, and user feedback remain on the machine. The web interface reads
the database; loading a page never starts the pipeline.

## 1. Install and inspect an existing library

Requirements: Python 3.11–3.12, `uv`, `ffmpeg`, `yt-dlp`, and, for enrichment,
the local ASR and OCR dependencies installed by `uv sync`.

```bash
uv sync --group dev
uv run reels status
uv run reels web --port 8420
```

Open <http://127.0.0.1:8420>. This requires neither an Instagram cookie nor an
LLM connection when a local database already exists.

## 2. Configure secrets and inference

Create the untracked configuration file only when this machine captures saved
Instagram media:

```bash
cp .env.example .env
chmod 600 .env
```

`.env.example` deliberately contains only `IG_SESSIONID`. The default setup uses
the authenticated Codex CLI, Luna low, and CUDA ASR, so browsing and the default
inference path need no `.env` model setting. Shell variables take precedence.
Never commit `.env` or put an Instagram cookie, API key, or Codex session in a
systemd unit or the web UI.

Choose one backend with `REELS_LLM_BACKEND`:

| Backend | Required local setup | Typical settings |
|---|---|---|
| `codex` | Authenticated `codex` CLI | `REELS_MODEL_CODEX`, `REELS_CODEX_REASONING` |
| `hermes` | Working Hermes executable and connection | `REELS_HERMES_EXECUTABLE`, `REELS_HERMES_REASONING` |
| `api` | OpenAI-compatible endpoint and API key | `REELS_API_BASE_URL`, `REELS_API_KEY`, `REELS_MODEL_API` |
| `ollama` | Running Ollama with the selected model | `REELS_MODEL_EXTRACT` |

The available profiles are versioned in
[`config/llm-profiles.yaml`](../../config/llm-profiles.yaml). The web interface
selects one profile but never edits that project file. A machine-specific profile
catalogue may be added at `~/.config/reels-platform/llm-profiles.local.yaml`;
it uses the same schema, cannot override a project profile, and is not versioned.
The active profile and schedule are stored in
`~/.config/reels-platform/runtime.yaml` with user-only permissions.

For capture, add `IG_SESSIONID` from an already authenticated Instagram browser
session. A password is never used. Add other cookie fields only when Instagram
rejects the minimal session cookie and the browser provides them.

CUDA ASR is the workstation default. Set `REELS_ASR_DEVICE=cpu` only on a
machine without the NVIDIA runtime; CPU ASR is substantially slower.

## 3. Run the pipeline deliberately

Each stage is idempotent and resumes its own unfinished items. It does **not**
mean a command is small: pending work can still be large. Inspect state first.

```bash
uv run reels status

# Incremental saved-media scan only.
uv run reels sync

# Resume a precise failed download or extraction.
uv run reels download --shortcode SHORTCODE
uv run reels extract --shortcode SHORTCODE
```

`reels pipeline` chains the incremental scan, media download, ASR, OCR,
extraction, verification, and catalogue resolution. `reels weekly-run` is the
scheduled equivalent. Do not use either merely to test credentials on a corpus
with a large backlog; use a temporary database or a deliberately bounded command
instead. `--force` re-extracts already completed items and should be restricted
to an explicit evaluation sample.

```bash
# Regular local operation after configuration is confirmed.
uv run reels weekly-run

# Browse and query without writing pipeline facts.
uv run reels query "Japanese restaurant" --city Paris
uv run reels recipes --cuisine japanese
```

## 4. Local services

The web service listens only on `127.0.0.1:8420`. The settings page stores the
schedule and selected connection, model, and reasoning profile in
`~/.config/reels-platform/runtime.yaml`; it does not store secrets. The selected
profile applies to new local chat and manual pipeline requests as well as the
next weekly run. Install the user services when the local configuration is ready:

```bash
mkdir -p ~/.config/systemd/user
cp deploy/systemd/reels-*.service deploy/systemd/reels-*.timer ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable --now reels-web.service reels-weekly.timer
```

The MCP is not a network service. The external agent starts it as a read-only
stdio subprocess:

```bash
uv run --extra mcp reels-mcp
```

## 5. Verify changes

```bash
uv run pytest -q
uv run reels regression
git diff --check
```

The regression command evaluates materialized extraction results. It does not
make live LLM calls by itself. Model quality belongs to a separate frozen,
annotated evaluation run, outside pull-request CI.

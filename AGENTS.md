# Reels Platform

## Product contract

This repository captures saved Instagram media, extracts source-grounded facts,
and makes entities, recipes and reels searchable locally. The catalogue is
source evidence; personal feedback and chat history are separate user data.

Before changing capture, enrichment, extraction, canonicalisation, media,
quality or recommendation behaviour, read [docs/fr/domain-and-flow.md](docs/fr/domain-and-flow.md).
Read the focused document when relevant:

- [docs/fr/extraction-quality.md](docs/fr/extraction-quality.md) for prompts, model evaluation or regression;
- [docs/fr/web-interface.md](docs/fr/web-interface.md) for FastAPI, local services or settings;
- [docs/fr/mcp.md](docs/fr/mcp.md) for the external agent interface;
- [docs/fr/data-model.md](docs/fr/data-model.md) for changes to persistence or data ownership.

## Working rules

- Preserve existing user data, local media and unrelated worktree changes.
- Pipeline stages are idempotent. Resume a failed or interrupted item narrowly; do not replay the corpus unless explicitly requested.
- Keep model inference provider-neutral. The configured backend can be Codex, Hermes, an OpenAI-compatible API or Ollama.
- Retain evidence and observed names. Canonicalisation is deterministic and may abstain; it does not use an LLM to silently merge names.
- The web application does not start pipeline work on page load. The MCP is read-only. Markdown and Obsidian publication belong to the external agent.
- Validate relevant changes with focused tests, then `uv run pytest -q` when practical. Run `git diff --check` before committing.

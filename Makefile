.DEFAULT_GOAL := help

UV ?= uv
PORT ?= 8420
DB ?= db/reels.db

.PHONY: help setup env init dev web watch status doctor test regression check hooks

help: ## Show available local development commands.
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install locked application, development, and optional interface dependencies.
	$(UV) sync --group dev --all-extras

env: ## Create a permission-restricted .env from the template when absent.
	@if [ -e .env ]; then \
		printf '%s\n' '.env already exists; leaving it unchanged.'; \
	else \
		install -m 600 .env.example .env; \
		printf '%s\n' 'Created .env from .env.example; add credentials only when needed.'; \
	fi

init: dev ## Alias for `make dev`.

dev: setup env hooks ## Prepare a clone for local development without overwriting .env.

web: setup env ## Start the local web application.
	$(UV) run reels --db "$(DB)" web --port "$(PORT)"

watch: setup env ## Start the local web application with automatic reload.
	$(UV) run reels --db "$(DB)" web --port "$(PORT)" --reload

status: ## Show the local library and pipeline state.
	$(UV) run reels --db "$(DB)" status

doctor: ## Report locally available optional capture and inference tools.
	@command -v $(UV) >/dev/null || { printf '%s\n' 'missing: uv'; exit 1; }
	@for tool in ffmpeg yt-dlp codex; do \
		if command -v $$tool >/dev/null; then printf '%-8s %s\n' "$$tool" "$$(command -v $$tool)"; \
		else printf '%-8s %s\n' "$$tool" 'not found (optional for browsing)'; fi; \
	done

test: ## Run the unit and integration test suite.
	$(UV) run pytest -q

regression: ## Run the materialized extraction regression suite.
	$(UV) run reels regression

check: ## Run the repository quality gates used locally.
	$(UV) lock --check
	$(UV) run ruff check --select E9,F63,F7,F82 src tests
	$(UV) run pytest -q

hooks: ## Enable the repository Git hooks once per clone.
	./scripts/install-git-hooks.sh

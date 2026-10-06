.DEFAULT_GOAL := help

UV ?= uv
PORT ?= 8420
DB ?= db/reels.db

.PHONY: help setup dev test regression check hooks

help: ## Show available local development commands.
	@awk 'BEGIN {FS = ":.*##"} /^[a-zA-Z_-]+:.*##/ {printf "  %-12s %s\n", $$1, $$2}' $(MAKEFILE_LIST)

setup: ## Install locked application, development, and optional interface dependencies.
	$(UV) sync --group dev --all-extras

dev: setup ## Start the local web app with automatic reload.
	$(UV) run reels --db "$(DB)" web --port "$(PORT)" --reload

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

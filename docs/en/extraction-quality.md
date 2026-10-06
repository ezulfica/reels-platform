# Extraction quality

Extraction transforms reel sources into structured facts. Provider and model are
execution parameters; they do not change domain rules.

## Three reads

1. **Fiche**: topic, mode (`repertoire` or `recommendation`), actionability,
   tags and revisit points.
2. **Discovery**: directly recommended names and exact evidence.
3. **Enrichment**: type and metadata for discovered names only.

Recipes follow the fiche then the recipe index. One materialised extraction is
kept per reel, together with attempts and provenance.

## What success means

The blind fixture `tests/fixtures/qwen_extraction_blind_v1.json` measures entity
recall, false positives on exhaustive cases, content kind, mode, actionability,
recipes, expected terms and duration per reel.

```bash
uv run reels regression
uv run reels regression --json
```

Compare models on the same holdout, sources and prompt. Never tune and report on
the same sample or replay the corpus for an evaluation.

## Resumption and provenance

Extraction runs again only when absent, failed or incompatible with the current
model, prompt or context. Attempts retain model, parameters, raw responses,
duration and error. Model output is not truth by itself: each fact stays linked
to a source and uncertain decisions abstain.

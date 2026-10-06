"""Blind, explicit regression corpus for Qwen extraction quality."""

from __future__ import annotations

import json
import sqlite3
import unicodedata
from pathlib import Path

from domain import canonicalization


def load_cases(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list):
        raise TypeError("regression file must contain a cases list")
    for case in cases:
        if not isinstance(case, dict) or not isinstance(case.get("shortcode"), str):
            raise TypeError("each regression case needs a shortcode")
        if not all(isinstance(name, str) for name in case.get("expected_names", [])):
            raise TypeError("expected_names must be a list of strings")
        entities = case.get("expected_entities", [])
        if not isinstance(entities, list):
            raise TypeError("expected_entities must be a list")
        for entity in entities:
            if not isinstance(entity, dict) or not isinstance(entity.get("name"), str):
                raise TypeError("each expected entity needs a name")
            types = entity.get(
                "types", [entity.get("type")] if entity.get("type") else []
            )
            if not isinstance(types, list) or not all(
                isinstance(item, str) for item in types
            ):
                raise ValueError("expected entity types must be a list of strings")
            if "aliases" in entity and (
                not isinstance(entity["aliases"], list)
                or not all(isinstance(x, str) for x in entity["aliases"])
            ):
                raise ValueError("expected entity aliases must be a list of strings")
        if case.get("entity_scope", "minimum") not in {"minimum", "complete"}:
            raise ValueError("entity_scope must be 'minimum' or 'complete'")
        if "expected_mode" in case and case["expected_mode"] not in {
            None,
            "repertoire",
            "recommandation",
        }:
            raise ValueError(
                "expected_mode must be repertoire, recommandation, or null"
            )
        if "expected_content_kind" in case and case["expected_content_kind"] not in {
            "",
            "recipe",
            "exercise",
            "lesson",
            "method",
            "guide",
            "inspiration",
        }:
            raise ValueError("invalid expected_content_kind")
        if "expected_recipes" in case:
            recipes = case["expected_recipes"]
            if not isinstance(recipes, list) or not all(
                isinstance(x, dict) and isinstance(x.get("name"), str) for x in recipes
            ):
                raise ValueError("expected_recipes must be a list of named recipes")
        if "expected_terms" in case and (
            not isinstance(case["expected_terms"], list)
            or not all(isinstance(x, str) for x in case["expected_terms"])
        ):
            raise ValueError("expected_terms must be a list of strings")
    return cases


def _key(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).casefold()
    value = "".join(char for char in value if not unicodedata.combining(char))
    return " ".join(value.split())


def missing_llm_names(conn: sqlite3.Connection, cases: list[dict]) -> list[dict]:
    """Expected names absent from the current Qwen/LLM materialisation only."""
    missing: list[dict] = []
    for case in cases:
        names = {
            _key(row["name"])
            for row in conn.execute(
                "SELECT name FROM candidate WHERE shortcode = ? AND source = 'llm'",
                (case["shortcode"],),
            )
        }
        expected = [
            name for name in case.get("expected_names", []) if _key(name) not in names
        ]
        if expected:
            missing.append({"shortcode": case["shortcode"], "names": expected})
    return missing


def score(conn: sqlite3.Connection, cases: list[dict]) -> dict:
    """Score only Qwen-owned materialisations; human rows never enter any metric."""
    results = []
    totals = {
        "entities_expected": 0,
        "entities_found": 0,
        "false_positives": 0,
        "false_positive_cases": 0,
        "content_kind_expected": 0,
        "content_kind_correct": 0,
        "mode_expected": 0,
        "mode_correct": 0,
        "recipes_expected": 0,
        "recipes_found": 0,
        "terms_expected": 0,
        "terms_found": 0,
        "actionable_expected": 0,
        "actionable_correct": 0,
        "canonicalized_entities_found": 0,
        "canonicalization_resolved": 0,
        "canonicalization_abstained": 0,
        "canonicalized_false_positives": 0,
        "canonicalized_false_positive_cases": 0,
    }
    type_errors, missing, false_positives, canonicalized_false_positives = (
        [],
        [],
        [],
        [],
    )
    by_category: dict[str, dict] = {}

    for case in cases:
        shortcode = case["shortcode"]
        category = case.get("category", "non catégorisé")
        extraction = conn.execute(
            "SELECT model, prompt_sha, context_sha, mode, content_kind, raw_response, "
            "key_points_json, ok "
            "FROM extraction WHERE shortcode = ?",
            (shortcode,),
        ).fetchone()
        candidates = conn.execute(
            "SELECT id, name, name_latin, type FROM candidate "
            "WHERE shortcode = ? AND source = 'llm'",
            (shortcode,),
        ).fetchall()
        actual = {}
        canonical_actual = {}
        candidate_decisions = {}
        for row in candidates:
            decision = canonicalization.decide_candidate(conn, row["id"])
            candidate_decisions[row["id"]] = decision
            canonical_name = (
                decision.canonical_name
                if decision.status == "resolved"
                else row["name"]
            )
            totals["canonicalization_resolved"] += int(decision.status == "resolved")
            totals["canonicalization_abstained"] += int(decision.status == "abstained")
            for value in (row["name"], row["name_latin"] or ""):
                if value:
                    actual.setdefault(_key(value), row)
            for value in (canonical_name, row["name_latin"] or ""):
                if value:
                    canonical_actual.setdefault(_key(value), row)
        expected_entities = case.get("expected_entities") or [
            {"name": name} for name in case.get("expected_names", [])
        ]
        accepted: set[str] = set()
        case_found = 0
        canonicalized_case_found = 0
        case_missing = []
        for entity in expected_entities:
            aliases = [entity["name"], *entity.get("aliases", [])]
            alias_keys = {_key(name) for name in aliases}
            accepted.update(alias_keys)
            totals["entities_expected"] += 1
            match = next((actual[key] for key in alias_keys if key in actual), None)
            canonical_match = next(
                (
                    canonical_actual[key]
                    for key in alias_keys
                    if key in canonical_actual
                ),
                None,
            )
            canonicalized_case_found += int(canonical_match is not None)
            totals["canonicalized_entities_found"] += int(canonical_match is not None)
            if match:
                case_found += 1
                totals["entities_found"] += 1
                expected_types = entity.get(
                    "types", [entity.get("type")] if entity.get("type") else []
                )
                if expected_types and match["type"] not in expected_types:
                    type_errors.append(
                        {
                            "shortcode": shortcode,
                            "name": entity["name"],
                            "expected": expected_types,
                            "actual": match["type"],
                        }
                    )
            else:
                case_missing.append(entity["name"])
        if case_missing:
            missing.append({"shortcode": shortcode, "names": case_missing})

        fp_count = None
        if case.get("entity_scope", "minimum") == "complete":
            fp_count = 0
            canonicalized_fp_count = 0
            for row in candidates:
                keys = {_key(row["name"]), _key(row["name_latin"] or "")}
                if not keys.intersection(accepted):
                    item = {
                        "shortcode": shortcode,
                        "name": row["name"],
                        "type": row["type"],
                    }
                    false_positives.append(item)
                    fp_count += 1
                decision = candidate_decisions[row["id"]]
                projected_name = (
                    decision.canonical_name
                    if decision.status == "resolved"
                    else row["name"]
                )
                canonical_keys = {_key(projected_name), _key(row["name_latin"] or "")}
                if not canonical_keys.intersection(accepted):
                    item = {
                        "shortcode": shortcode,
                        "name": projected_name,
                        "type": row["type"],
                    }
                    canonicalized_false_positives.append(item)
                    canonicalized_fp_count += 1
            totals["false_positives"] += fp_count
            totals["false_positive_cases"] += 1
            totals["canonicalized_false_positives"] += canonicalized_fp_count
            totals["canonicalized_false_positive_cases"] += 1

        kind_expected = "expected_content_kind" in case
        kind_correct = (
            bool(extraction)
            and extraction["ok"] == 1
            and kind_expected
            and ((extraction["content_kind"] or "") == case["expected_content_kind"])
        )
        if kind_expected:
            totals["content_kind_expected"] += 1
            totals["content_kind_correct"] += int(kind_correct)
        mode_expected = "expected_mode" in case and case["expected_mode"] is not None
        mode_correct = (
            bool(extraction)
            and extraction["ok"] == 1
            and mode_expected
            and (extraction["mode"] == case["expected_mode"])
        )
        if mode_expected:
            totals["mode_expected"] += 1
            totals["mode_correct"] += int(mode_correct)

        raw = {}
        if extraction and extraction["raw_response"]:
            try:
                raw = json.loads(extraction["raw_response"])
            except (TypeError, json.JSONDecodeError):
                raw = {}
        fiche = raw.get("fiche", raw) if isinstance(raw, dict) else {}
        actionable_expected = "expected_actionable" in case
        actionable_correct = (
            actionable_expected
            and extraction
            and extraction["ok"] == 1
            and isinstance(fiche, dict)
            and fiche.get("is_actionable") is case["expected_actionable"]
        )
        if actionable_expected:
            totals["actionable_expected"] += 1
            totals["actionable_correct"] += int(bool(actionable_correct))
        recipe_payload = raw.get("content_index", {}) if isinstance(raw, dict) else {}
        actual_recipes = (
            recipe_payload.get("recipes", [])
            if isinstance(recipe_payload, dict)
            else []
        )
        if not actual_recipes and isinstance(raw, dict):
            actual_recipes = raw.get("recipes", [])
        recipe_names = {
            _key(recipe.get("dish_name", ""))
            for recipe in actual_recipes
            if isinstance(recipe, dict)
        }
        expected_recipes = case.get("expected_recipes", [])
        recipe_found = 0
        for recipe in expected_recipes:
            totals["recipes_expected"] += 1
            if any(
                _key(name) in recipe_names
                for name in [recipe["name"], *recipe.get("aliases", [])]
            ):
                recipe_found += 1
                totals["recipes_found"] += 1
        key_points = []
        try:
            key_points = (
                json.loads(extraction["key_points_json"] or "[]") if extraction else []
            )
        except (TypeError, json.JSONDecodeError):
            pass
        points_key = _key(
            " ".join(point for point in key_points if isinstance(point, str))
        )
        expected_terms = case.get("expected_terms", [])
        terms_found = sum(_key(term) in points_key for term in expected_terms)
        totals["terms_expected"] += len(expected_terms)
        totals["terms_found"] += terms_found
        duration = None
        if extraction:
            attempt = conn.execute(
                "SELECT duration_ms FROM extraction_attempt WHERE shortcode = ? AND model = ? "
                "AND prompt_sha = ? AND ok = 1 AND (? IS NULL OR context_sha = ?) "
                "ORDER BY id DESC LIMIT 1",
                (
                    shortcode,
                    extraction["model"],
                    extraction["prompt_sha"],
                    extraction["context_sha"],
                    extraction["context_sha"],
                ),
            ).fetchone()
            if attempt:
                duration = attempt["duration_ms"]
        if not extraction or extraction["ok"] != 1:
            status = "absent" if not extraction else "échec"
        else:
            status = "ok"

        row_result = {
            "shortcode": shortcode,
            "category": category,
            "status": status,
            "model": extraction["model"] if extraction else None,
            "prompt_sha": extraction["prompt_sha"] if extraction else None,
            "entity_expected": len(expected_entities),
            "entity_found": case_found,
            "canonicalized_entity_found": canonicalized_case_found,
            "missing_entities": case_missing,
            "false_positives": fp_count,
            "canonicalized_false_positives": (
                canonicalized_fp_count
                if case.get("entity_scope", "minimum") == "complete"
                else None
            ),
            "expected_mode": case.get("expected_mode"),
            "actual_mode": extraction["mode"] if extraction else None,
            "mode_correct": bool(mode_correct) if mode_expected else None,
            "expected_actionable": case.get("expected_actionable"),
            "actionable_correct": bool(actionable_correct)
            if actionable_expected
            else None,
            "expected_content_kind": case.get("expected_content_kind"),
            "actual_content_kind": (extraction["content_kind"] or "")
            if extraction
            else None,
            "content_kind_correct": bool(kind_correct) if kind_expected else None,
            "recipes_expected": len(expected_recipes),
            "recipes_found": recipe_found,
            "terms_expected": len(expected_terms),
            "terms_found": terms_found,
            "duration_ms": duration,
        }
        results.append(row_result)
        bucket = by_category.setdefault(
            category,
            {
                "cases": 0,
                "entities_expected": 0,
                "entities_found": 0,
                "content_kind_expected": 0,
                "content_kind_correct": 0,
                "mode_expected": 0,
                "mode_correct": 0,
                "false_positives": 0,
                "false_positive_cases": 0,
                "actionable_expected": 0,
                "actionable_correct": 0,
                "terms_expected": 0,
                "terms_found": 0,
            },
        )
        bucket["cases"] += 1
        bucket["entities_expected"] += len(expected_entities)
        bucket["entities_found"] += case_found
        if kind_expected:
            bucket["content_kind_expected"] += 1
            bucket["content_kind_correct"] += int(kind_correct)
        if mode_expected:
            bucket["mode_expected"] += 1
            bucket["mode_correct"] += int(mode_correct)
        if actionable_expected:
            bucket["actionable_expected"] += 1
            bucket["actionable_correct"] += int(bool(actionable_correct))
        bucket["terms_expected"] += len(expected_terms)
        bucket["terms_found"] += terms_found
        if fp_count is not None:
            bucket["false_positives"] += fp_count
            bucket["false_positive_cases"] += 1

    return {
        **totals,
        "recall": totals["entities_found"] / totals["entities_expected"]
        if totals["entities_expected"]
        else 1.0,
        "canonicalized_recall": totals["canonicalized_entities_found"]
        / totals["entities_expected"]
        if totals["entities_expected"]
        else 1.0,
        "content_kind_accuracy": totals["content_kind_correct"]
        / totals["content_kind_expected"]
        if totals["content_kind_expected"]
        else 1.0,
        "mode_accuracy": totals["mode_correct"] / totals["mode_expected"]
        if totals["mode_expected"]
        else 1.0,
        "actionable_accuracy": totals["actionable_correct"]
        / totals["actionable_expected"]
        if totals["actionable_expected"]
        else 1.0,
        "term_recall": totals["terms_found"] / totals["terms_expected"]
        if totals["terms_expected"]
        else 1.0,
        "cases": results,
        "by_category": by_category,
        "missing": missing,
        "false_positive_items": false_positives,
        "canonicalized_false_positive_items": canonicalized_false_positives,
        "type_errors": type_errors,
        "prompt_shas": sorted(
            {
                row["model"] + ":" + row["prompt_sha"]
                for row in results
                if row["model"] and row["prompt_sha"]
            }
        ),
    }

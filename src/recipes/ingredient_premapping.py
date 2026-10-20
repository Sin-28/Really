"""Bounded, resumable offline pre-mapping for imported Food.com ingredients."""

import json
import os
import time
from pathlib import Path

from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy
from src.recipes.ingredient_parser import parse_foodcom_ingredient


class IngredientPremappingWorkflow:
    """Scan a bounded recipe sample, rank unique ingredient contexts, and map.

    The workflow defaults to read-only dry-run mode. Database writes require
    both ``dry_run=False`` and ``write_enabled=True``. An optional JSON
    checkpoint atomically records scan progress and completed ingredient keys.
    """

    CHECKPOINT_VERSION = 1
    MAX_SAMPLE_RECIPES = 1000
    MAX_UNIQUE_INGREDIENTS = 25
    MAX_BATCH_SIZE = 5
    MAX_RECIPE_BATCH_SIZE = 1000

    def __init__(self, kg, mapper, *, policy=None, validator=None):
        self.kg = kg
        self.mapper = mapper
        self.policy = policy or AutomaticIngredientMappingPolicy()
        self.validator = validator

    @staticmethod
    def _initial_state(configuration):
        return {
            "checkpoint_version": IngredientPremappingWorkflow.CHECKPOINT_VERSION,
            "configuration": configuration,
            "after_recipe_id": "",
            "recipes_scanned": 0,
            "total_ingredient_occurrences": 0,
            "unparsed_occurrences": 0,
            "groups": {},
            "scan_complete": False,
            "selected_keys": None,
            "completed": {},
            "cumulative_processing_seconds": 0.0,
        }

    @staticmethod
    def _save_checkpoint(path, state):
        if path is None:
            return
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(target.name + ".tmp")
        temporary.write_text(json.dumps(state, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
        os.replace(temporary, target)

    @staticmethod
    def _load_checkpoint(path, configuration):
        if path is None or not Path(path).is_file():
            return IngredientPremappingWorkflow._initial_state(configuration)
        try:
            state = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read pre-mapping checkpoint: {exc}") from exc
        if (not isinstance(state, dict)
                or state.get("checkpoint_version") != IngredientPremappingWorkflow.CHECKPOINT_VERSION
                or state.get("configuration") != configuration):
            raise ValueError("Checkpoint configuration or version does not match this pre-mapping run.")
        return state

    @staticmethod
    def _parsed_item(item):
        if not isinstance(item, dict):
            return None
        name, quantity = item.get("name"), item.get("quantity_raw")
        if not name:
            return None
        return {**item, "parsed": parse_foodcom_ingredient(name, quantity)}

    def _scan(self, state, *, max_recipes, max_ingredients, recipe_batch_size, dataset_scope,
              checkpoint_path, progress_callback):
        reader = getattr(self.kg, "get_foodcom_recipe_batch", None)
        if not callable(reader):
            raise RuntimeError("The graph does not provide a bounded Food.com recipe reader.")
        while not state["scan_complete"] and state["recipes_scanned"] < max_recipes:
            remaining = max_recipes - state["recipes_scanned"]
            rows = reader(after_recipe_id=state["after_recipe_id"],
                          limit=min(recipe_batch_size, remaining))
            if not rows:
                state["scan_complete"] = True
                self._save_checkpoint(checkpoint_path, state)
                break
            for row in rows:
                recipe_id = str(row.get("recipe_id") or "")
                state["after_recipe_id"] = recipe_id
                state["recipes_scanned"] += 1
                try:
                    details = json.loads(row.get("details_json") or "{}")
                except (TypeError, json.JSONDecodeError):
                    details = {}
                items = details.get("ingredient_items") or []
                for source_item in items:
                    state["total_ingredient_occurrences"] += 1
                    parsed_item = self._parsed_item(source_item)
                    if parsed_item is None:
                        state["unparsed_occurrences"] += 1
                        continue
                    try:
                        key = self.mapper.mapping_cache_key(
                            parsed_item, dataset_scope=dataset_scope,
                        )
                    except (TypeError, ValueError):
                        state["unparsed_occurrences"] += 1
                        continue
                    group = state["groups"].get(key)
                    parsed = parsed_item.get("parsed") or {}
                    if group is None:
                        instructions = details.get("instructions") or []
                        if isinstance(instructions, str):
                            instructions = [instructions]
                        group = {
                            "cache_key": key,
                            "ingredient": parsed.get("ingredient_name") or parsed_item.get("name") or "",
                            "normalized_name": parsed.get("normalized_name") or "",
                            "parsed_ingredient": parsed_item,
                            "occurrences": 0,
                            "first_recipe_context": {
                                "name": str(row.get("recipe_name") or "")[:200],
                                "instructions": [str(step)[:300] for step in instructions[:3]],
                            },
                        }
                        state["groups"][key] = group
                    group["occurrences"] += 1
            if state["recipes_scanned"] >= max_recipes:
                state["scan_complete"] = True
            self._save_checkpoint(checkpoint_path, state)
            if progress_callback:
                progress_callback({
                    "phase": "scan",
                    "recipes_scanned": state["recipes_scanned"],
                    "ingredient_occurrences": state["total_ingredient_occurrences"],
                    "unique_context_keys": len(state["groups"]),
                })
        if state["selected_keys"] is None and state["scan_complete"]:
            ordered = sorted(state["groups"], key=lambda key: (
                -int(state["groups"][key]["occurrences"]),
                state["groups"][key]["normalized_name"], key,
            ))
            state["selected_keys"] = ordered[:max_ingredients]
            self._save_checkpoint(checkpoint_path, state)

    @staticmethod
    def _cache_mapping_result(cached, group, dataset_scope):
        return {
            "ingredient": group["ingredient"],
            "normalized_name": group["normalized_name"],
            "attributes": cached.get("attributes") or {},
            "dataset_scope": cached.get("dataset_scope") or dataset_scope,
            "mapping_version": cached.get("mapping_version"),
            "cache_key": cached.get("cache_key") or group["cache_key"],
            "status": cached.get("status"),
            "candidates": cached.get("candidates") or [],
            "fdc_id": cached.get("fdc_id"),
            "verification_method": cached.get("verification_method") or cached.get("validation_method"),
            "verification_policy_version": cached.get("verification_policy_version"),
            "validated_by": cached.get("validated_by"),
            "reviewer": cached.get("reviewer") or cached.get("validated_by"),
        }

    @staticmethod
    def _current_policy_evaluation(cached, policy_version):
        evaluation = (cached or {}).get("automatic_policy_evaluation")
        return evaluation if isinstance(evaluation, dict) and evaluation.get("policy_version") == policy_version else None

    @staticmethod
    def _slm_evaluation_failed(evaluation):
        if not isinstance(evaluation, dict):
            return False
        if evaluation.get("cache_status") == "WRITE_ERROR":
            return True
        if evaluation.get("status") != "REVIEW_REQUIRED":
            return False
        reason = str(evaluation.get("reason") or "").casefold()
        return any(marker in reason for marker in (
            "unavailable", "timed out", "evaluation failed", "malformed",
            "omitted required", "did not contain a json object", "not a json object",
        ))

    def _process_group(self, group, *, dataset_scope, dry_run, write_enabled):
        key = group["cache_key"]
        try:
            cached = self.kg.get_ingredient_mapping_cache(key)
        except Exception as exc:
            return {
                "cache_key": key, "ingredient": group["ingredient"],
                "occurrences": group["occurrences"], "status": "RETRIEVAL_ERROR",
                "retrieval_failure": True, "error": f"Mapping cache read failed ({type(exc).__name__}).",
            }

        if cached and cached.get("status") == "VERIFIED":
            method = cached.get("verification_method") or cached.get("validation_method")
            current_auto = (method == "DETERMINISTIC_POLICY"
                            and cached.get("verification_policy_version") == self.policy.VERSION)
            if method == "human_review" or current_auto:
                return {
                    "cache_key": key, "ingredient": group["ingredient"],
                    "occurrences": group["occurrences"], "status": "VERIFIED",
                    "fdc_id": cached.get("fdc_id"), "verification_method": method,
                    "verification_policy_version": cached.get("verification_policy_version"),
                    "verified_cache_hit": True,
                    "human_reviewed_reused": method == "human_review",
                    "candidate_count": len(cached.get("candidates") or []),
                }

        mapping_result = None
        slm_evaluation = None
        policy_evaluation = self._current_policy_evaluation(cached, self.policy.VERSION)
        if (cached and cached.get("status") != "VERIFIED" and policy_evaluation
                and {str(item.get("fdc_id")) for item in cached.get("candidates", [])}
                == {str(item) for item in policy_evaluation.get("candidate_ids", [])}):
            mapping_result = self._cache_mapping_result(cached, group, dataset_scope)
            slm_evaluation = cached.get("slm_evaluation")
        else:
            try:
                mapping_result = self.mapper.retrieve_candidates(
                    group["parsed_ingredient"], dataset_scope=dataset_scope,
                    persist=not dry_run,
                    reuse_verified_policy_version=self.policy.VERSION,
                )
            except Exception as exc:
                return {
                    "cache_key": key, "ingredient": group["ingredient"],
                    "occurrences": group["occurrences"], "status": "RETRIEVAL_ERROR",
                    "retrieval_failure": True, "error": f"USDA retrieval failed ({type(exc).__name__}).",
                }
            if mapping_result.get("status") == "RETRIEVAL_ERROR":
                return {
                    "cache_key": key, "ingredient": group["ingredient"],
                    "occurrences": group["occurrences"], "status": "RETRIEVAL_ERROR",
                    "retrieval_failure": True,
                    "candidate_count": 0,
                    "error": mapping_result.get("error", "USDA retrieval failed."),
                }
            if self.validator is not None and mapping_result.get("candidates"):
                slm_evaluation = self.validator.evaluate(
                    group["parsed_ingredient"], mapping_result,
                    recipe_context=group.get("first_recipe_context"), persist=not dry_run,
                )
            policy_evaluation = self.policy.evaluate(
                group["parsed_ingredient"], mapping_result, slm_evaluation=slm_evaluation,
            )

        candidate_count = len(mapping_result.get("candidates") or [])
        if (not dry_run and mapping_result.get("cache_status") != "STALE_VERIFIED_POLICY"
                and mapping_result.get("cache_key")):
            saver = getattr(self.kg, "save_ingredient_mapping_policy_evaluation", None)
            if callable(saver):
                try:
                    saver(mapping_result["cache_key"], policy_evaluation)
                except Exception as exc:
                    policy_evaluation = {**policy_evaluation,
                                         "persistence_error": type(exc).__name__}

        result = {
            "cache_key": key,
            "ingredient": group["ingredient"],
            "normalized_name": group["normalized_name"],
            "occurrences": group["occurrences"],
            "candidate_count": candidate_count,
            "candidate_retrieved": candidate_count > 0,
            "status": policy_evaluation.get("status", "REVIEW_REQUIRED"),
            "policy_eligible": policy_evaluation.get("eligible") is True,
            "policy_evaluation": policy_evaluation,
            "slm_status": (slm_evaluation or {}).get("status", "NOT_CONFIGURED"),
            "slm_failure": self._slm_evaluation_failed(slm_evaluation),
        }
        if policy_evaluation.get("eligible") is True:
            if dry_run:
                result["status"] = "ELIGIBLE_DRY_RUN"
            else:
                try:
                    verification = self.mapper.verify_candidate_automatically(
                        group["parsed_ingredient"], mapping_result, self.policy,
                        slm_evaluation=slm_evaluation,
                        enabled=write_enabled,
                        dataset_scope=dataset_scope,
                    )
                    result.update({
                        "status": "VERIFIED",
                        "fdc_id": verification.get("fdc_id"),
                        "verification_method": verification.get("verification_method"),
                        "verification_policy_version": verification.get("verification_policy_version"),
                        "reviewer": verification.get("reviewer"),
                        "verification_cache_status": verification.get("cache_status"),
                        "verified_cache_hit": verification.get("cache_status") in {
                            "POLICY_VERIFIED_REUSED", "HUMAN_VERIFIED_PRESERVED",
                        },
                        "human_reviewed_reused": verification.get("verification_method") == "human_review",
                    })
                except Exception as exc:
                    result.update(status="REVIEW_REQUIRED", policy_eligible=False,
                                  verification_error=type(exc).__name__)
        return result

    @staticmethod
    def _summarize(state, *, dry_run, processed_this_run, duration):
        selected_keys = state.get("selected_keys") or []
        completed = state.get("completed") or {}
        items = [completed[key] for key in selected_keys if key in completed]
        processed = len(items)
        candidate_covered = sum(bool(item.get("candidate_retrieved") or item.get("candidate_count")) for item in items)
        verified = sum(item.get("status") == "VERIFIED" for item in items)
        return {
            "dry_run": dry_run,
            "recipes_scanned": state.get("recipes_scanned", 0),
            "total_ingredient_occurrences": state.get("total_ingredient_occurrences", 0),
            "unparsed_occurrences": state.get("unparsed_occurrences", 0),
            "unique_context_aware_ingredient_keys": len(state.get("groups") or {}),
            "selected_ingredient_keys": len(selected_keys),
            "processed_unique_ingredient_keys": processed,
            "batch_processed_this_run": processed_this_run,
            "remaining_selected_ingredient_keys": max(0, len(selected_keys) - processed),
            "existing_verified_cache_hits": sum(bool(item.get("verified_cache_hit")) for item in items),
            "automatically_verified_mappings": sum(
                item.get("status") == "VERIFIED" and item.get("verification_method") == "DETERMINISTIC_POLICY"
                and not item.get("verified_cache_hit") for item in items
            ),
            "human_reviewed_mappings_reused": sum(bool(item.get("human_reviewed_reused")) for item in items),
            "ambiguous_mappings": sum(item.get("status") == "AMBIGUOUS" for item in items),
            "unresolved_mappings": sum(item.get("status") in {
                "UNRESOLVED", "REJECTED", "REVIEW_REQUIRED", "RETRIEVAL_ERROR",
            } for item in items),
            "retrieval_failures": sum(bool(item.get("retrieval_failure")) for item in items),
            "slm_evaluation_failures": sum(bool(item.get("slm_failure")) for item in items),
            "candidate_retrieval_coverage": {
                "unique_with_candidates": candidate_covered,
                "rate_over_processed": candidate_covered / processed if processed else 0.0,
            },
            "verified_mapping_coverage": {
                "current_verified_unique": verified,
                "rate_over_processed": verified / processed if processed else 0.0,
            },
            "processing_duration_seconds": state.get("cumulative_processing_seconds", 0.0) + duration,
            "items": items,
        }

    def run(self, *, dataset_scope="all", batch_size=5, max_ingredients=25,
            max_recipes=1000, recipe_batch_size=250, dry_run=True,
            write_enabled=False, checkpoint_path=None, progress_callback=None):
        """Scan a representative sample and process at most one bounded batch.

        Call again with the same checkpoint to resume. Dry-run may read the
        graph and write a local checkpoint, but performs no graph mutations.
        """
        if dry_run is not True and write_enabled is not True:
            raise ValueError("Persistent processing requires both dry_run=False and write_enabled=True.")
        if dry_run is True and write_enabled is True:
            raise ValueError("Dry-run and persistent writes cannot be enabled together.")
        if any(int(value) < 1 for value in (batch_size, max_ingredients, max_recipes, recipe_batch_size)):
            raise ValueError("Batch sizes and processing limits must be positive integers.")
        if (int(max_recipes) > self.MAX_SAMPLE_RECIPES
                or int(max_ingredients) > self.MAX_UNIQUE_INGREDIENTS
                or int(batch_size) > self.MAX_BATCH_SIZE
                or int(recipe_batch_size) > self.MAX_RECIPE_BATCH_SIZE):
            raise ValueError(
                "Pre-mapping limits exceed the per-run safety caps: at most "
                f"{self.MAX_SAMPLE_RECIPES} recipes, {self.MAX_UNIQUE_INGREDIENTS} unique ingredients, "
                f"{self.MAX_BATCH_SIZE} ingredients per invocation, and "
                f"{self.MAX_RECIPE_BATCH_SIZE} recipes per read batch."
            )
        dataset_scope = self.mapper._scope(dataset_scope)
        configuration = {
            "dataset_scope": dataset_scope,
            "mapping_version": self.mapper.mapping_version,
            "policy_version": self.policy.VERSION,
            "max_recipes": int(max_recipes),
            "max_ingredients": int(max_ingredients),
        }
        state = self._load_checkpoint(checkpoint_path, configuration)
        processing_mode = "persistent" if write_enabled else "dry_run"
        previous_mode = state.get("processing_mode", "dry_run")
        if previous_mode != processing_mode and processing_mode == "persistent":
            # A dry-run checkpoint contains decisions, not completed writes.
            # Reprocess its selected keys once the user explicitly opts in.
            state["completed"] = {}
            state["cumulative_processing_seconds"] = 0.0
        state["processing_mode"] = processing_mode
        self._save_checkpoint(checkpoint_path, state)
        started = time.monotonic()
        self._scan(
            state, max_recipes=int(max_recipes), max_ingredients=int(max_ingredients),
            recipe_batch_size=int(recipe_batch_size),
            dataset_scope=dataset_scope, checkpoint_path=checkpoint_path,
            progress_callback=progress_callback,
        )
        selected = state.get("selected_keys") or []
        # Clamp checkpoints written by older versions before processing or reporting.
        if len(selected) > int(max_ingredients):
            selected = selected[:int(max_ingredients)]
            state["selected_keys"] = selected
            self._save_checkpoint(checkpoint_path, state)
        pending = [key for key in selected if key not in state.get("completed", {})]
        current = pending[:int(batch_size)]
        for key in current:
            group = state["groups"][key]
            outcome = self._process_group(
                group, dataset_scope=dataset_scope, dry_run=dry_run,
                write_enabled=write_enabled,
            )
            state["completed"][key] = outcome
            self._save_checkpoint(checkpoint_path, state)
            if progress_callback:
                progress_callback({
                    "phase": "map",
                    "processed": len(state["completed"]),
                    "selected": len(selected),
                    "ingredient": group["ingredient"],
                    "status": outcome.get("status"),
                })
        duration = time.monotonic() - started
        state["cumulative_processing_seconds"] = state.get("cumulative_processing_seconds", 0.0) + duration
        self._save_checkpoint(checkpoint_path, state)
        report = self._summarize(state, dry_run=dry_run,
                                 processed_this_run=len(current), duration=0.0)
        report["processing_duration_seconds"] = state["cumulative_processing_seconds"]
        report["checkpoint_path"] = str(checkpoint_path) if checkpoint_path else None
        report["scan_sample_limited"] = state["recipes_scanned"] >= max_recipes
        return report

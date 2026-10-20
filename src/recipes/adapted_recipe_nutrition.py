"""Informational nutrition validation for an adapted ingredient composition."""

import copy
from time import perf_counter

from src.recipes.ingredient_mapping import MAPPING_VERSION
from src.recipes.recipe_change_detection import RecipeChangeDetector


class AdaptedRecipeNutritionEnrichmentService:
    """Remap and recalculate an adaptation using the existing Step 5B services."""

    def __init__(self, nutrient_service, constraint_adapter, *, change_detector=None):
        self.nutrient_service = nutrient_service
        self.constraint_adapter = constraint_adapter
        self.change_detector = change_detector or RecipeChangeDetector()

    def enrich(self, original_recipe, adapted_recipe, constraints, *, requested_servings=None,
               adaptation_id=None, original_enrichment=None, mapping_cache=None,
               nutrient_record_cache=None):
        started = perf_counter()
        original = copy.deepcopy(original_recipe) if isinstance(original_recipe, dict) else {}
        adapted = copy.deepcopy(adapted_recipe) if isinstance(adapted_recipe, dict) else {}

        phase = perf_counter()
        detected = self.change_detector.compare(original, adapted)
        change_detection_ms = (perf_counter() - phase) * 1000.0

        if original_enrichment is None:
            if original.get("source_dataset") == "Food.com Recipes and Reviews (Kaggle)":
                original_enrichment = {
                    "calculation_status": "UNAVAILABLE",
                    "error": "Food.com source quantities are not ingredient weights.",
                    "ingredient_mapping_enrichment": {"results": []},
                }
            else:
                original_enrichment = self.nutrient_service.enrich(original)
        original_enrichment = copy.deepcopy(original_enrichment)

        phase = perf_counter()
        quantities = self.nutrient_service.quantity_resolver.resolve_recipe(adapted)
        quantity_resolution_ms = (perf_counter() - phase) * 1000.0
        phase = perf_counter()
        mapping = self._map_adapted(
            original, adapted, detected, quantities,
            (original_enrichment.get("ingredient_mapping_enrichment") or {}),
            mapping_cache if mapping_cache is not None else {},
        )
        mapping_ms = (perf_counter() - phase) * 1000.0

        phase = perf_counter()
        calculation = self.nutrient_service.calculator.calculate_recipe(
            adapted, mapping, quantity_resolution=quantities,
            food_record_cache=nutrient_record_cache,
        )
        calculation["ingredient_mapping_enrichment"] = mapping
        calculation_ms = (perf_counter() - phase) * 1000.0

        phase = perf_counter()
        validation = self.constraint_adapter.evaluate(
            calculation, constraints or {}, requested_servings=requested_servings,
        )
        validation_ms = (perf_counter() - phase) * 1000.0
        unresolved = self._unresolved_ingredients(calculation)
        unresolved.extend({
            "reason": item.get("reason"),
            "original_position": item.get("original_position"),
            "adapted_position": item.get("adapted_position"),
        } for item in detected.get("unresolved_changes", []))
        return {
            "adaptation_id": adaptation_id,
            "change_detection_status": detected.get("status", "UNRESOLVED"),
            "changes": detected.get("changes", []),
            "unresolved_changes": detected.get("unresolved_changes", []),
            "original_recipe": self._recipe_representation(original),
            "adapted_recipe": self._recipe_representation(adapted),
            "original_nutritional_calculation": original_enrichment,
            "nutritional_calculation": calculation,
            "constraint_validation": validation,
            "original_servings": self._servings(original),
            "adapted_servings": self._servings(adapted),
            "requested_servings": requested_servings,
            "unresolved_ingredients": unresolved,
            "performance_ms": {
                "change_detection": change_detection_ms,
                "ingredient_remapping": mapping_ms,
                "quantity_resolution": quantity_resolution_ms,
                "nutrient_recalculation": calculation_ms,
                "constraint_revalidation": validation_ms,
                "overall_adapted_enrichment": (perf_counter() - started) * 1000.0,
            },
        }

    def _map_adapted(self, original, adapted, detected, quantity_resolution,
                     original_mapping_result, mapping_cache):
        originals = self._mapping_rows(original_mapping_result)
        change_by_adapted_position = {
            change.get("adapted_position"): change
            for change in detected.get("changes", [])
            if change.get("adapted_position") is not None
        }
        mapping_service = self.nutrient_service.mapping_service
        current_version = getattr(getattr(mapping_service, "mapper", None), "mapping_version", MAPPING_VERSION)
        mapping_rows = []
        results_by_position = {}
        mapping_cache_lookups = 0
        for item in quantity_resolution.get("ingredients", []):
            position = item.get("position")
            change = change_by_adapted_position.get(position, {})
            old_position = change.get("original_position")
            original_row = originals.get(old_position)
            original_mapping = (original_row or {}).get("mapping") or {}
            can_reuse, reuse_reason = self._can_reuse(
                change, original_mapping, current_version,
            )
            if can_reuse:
                row = copy.deepcopy(original_row)
                row["position"] = position
                row["mapping_reuse"] = {
                    "reused": True, "source_position": old_position,
                    "reason": reuse_reason,
                }
                results_by_position[position] = row
                continue
            source_item = self._source_item(adapted, position, item)
            source_item = self._mapping_source_item(
                source_item, item, mapping_service,
                force_name_remap=change.get("change_type") in {
                    "PREPARATION_CHANGED", "PROCESSING_CHANGED", "ATTRIBUTES_CHANGED",
                },
            )
            cache_key = self._item_cache_key(source_item, item, current_version)
            cached = mapping_cache.get(cache_key)
            if cached is None:
                if mapping_service is None:
                    cached = self._unresolved_mapping_row(position, item, "MAPPING_SERVICE_UNAVAILABLE")
                else:
                    try:
                        result = mapping_service.resolve_recipe({"raw_ingredient_items": [source_item]})
                        mapping_cache_lookups += int(result.get("mapping_cache_lookups") or 0)
                        resolved = (result.get("results") or [{}])[0]
                        cached = copy.deepcopy(resolved)
                        cached["position"] = position
                    except Exception as exc:
                        cached = self._unresolved_mapping_row(
                            position, item, f"MAPPING_FAILED_{type(exc).__name__}",
                        )
                mapping_cache[cache_key] = copy.deepcopy(cached)
            row = copy.deepcopy(cached)
            row["position"] = position
            row["mapping_reuse"] = {"reused": False, "reason": "NO_COMPATIBLE_VERIFIED_ORIGINAL_MAPPING"}
            results_by_position[position] = row

        for item in quantity_resolution.get("ingredients", []):
            row = results_by_position.get(item.get("position"))
            if row is not None:
                mapping_rows.append(row)
        unresolved = sum(
            (row.get("mapping") or {}).get("status") not in {
                "VERIFIED_MAPPING", "EXACT_IDENTITY", "SLM_RESOLVED", "REFERENCE_DEFAULT",
            }
            for row in mapping_rows
        )
        return {
            "mode": getattr(mapping_service, "mode", "READ_ONLY"),
            "ingredient_count": len(mapping_rows),
            "unique_ingredient_contexts": len({self._item_cache_key(
                self._source_item(adapted, item.get("position"), item), item, current_version,
            ) for item in quantity_resolution.get("ingredients", [])}),
            "mapping_cache_lookups": mapping_cache_lookups,
            "usda_candidate_retrievals": sum(
                int(bool((row.get("mapping") or {}).get("candidate_retrieval_performed")))
                for row in mapping_rows
            ),
            "slm_inference_calls": sum(
                int(bool((row.get("mapping") or {}).get("slm_inference_performed")))
                for row in mapping_rows
            ),
            "unresolved_ingredients": unresolved,
            "results": mapping_rows,
            "source_quantity_alignment_status": quantity_resolution.get("alignment_status"),
        }

    @staticmethod
    def _mapping_rows(mapping_result):
        return {
            row.get("position"): row
            for row in (mapping_result.get("results") or [])
            if isinstance(row, dict) and row.get("position") is not None
        }

    @staticmethod
    def _can_reuse(change, mapping, current_version):
        if not change.get("identity_unchanged"):
            return False, "INGREDIENT_IDENTITY_CHANGED"
        if change.get("change_type") in {
            "PREPARATION_CHANGED", "PROCESSING_CHANGED", "ATTRIBUTES_CHANGED",
        }:
            return False, "RELEVANT_INGREDIENT_ATTRIBUTES_CHANGED"
        if mapping.get("status") != "VERIFIED_MAPPING":
            return False, "ORIGINAL_MAPPING_NOT_VERIFIED"
        if mapping.get("mapping_version") != current_version:
            return False, "ORIGINAL_MAPPING_VERSION_STALE"
        return True, "UNCHANGED_IDENTITY_ATTRIBUTES_AND_MAPPING_VERSION"

    @staticmethod
    def _source_item(recipe, position, resolved):
        items = recipe.get("raw_ingredient_items")
        if not isinstance(items, list) or not items:
            items = recipe.get("ingredient_items")
        if not isinstance(items, list) or not items:
            items = recipe.get("ingredients")
        if isinstance(items, list) and position is not None and 0 <= position < len(items):
            source = items[position]
            if isinstance(source, dict):
                return copy.deepcopy(source)
            if isinstance(source, str):
                return {"name": source}
        item = {
            "name": resolved.get("original_name") or "",
            "quantity_raw": resolved.get("quantity_raw"),
            "attributes": copy.deepcopy(resolved.get("explicit_attributes") or {}),
        }
        return item

    @staticmethod
    def _mapping_source_item(source_item, resolved, mapping_service, *, force_name_remap=False):
        """Use a graph link when it has an FDC target; otherwise map its name."""
        source = copy.deepcopy(source_item) if isinstance(source_item, dict) else {"name": str(source_item)}
        stable_key = next((key for key in ("id", "ingredient_id", "food_id")
                           if source.get(key) is not None), None)
        graph = getattr(mapping_service, "kg", None)
        if stable_key and (force_name_remap or graph is not None):
            food = (getattr(graph, "ingredients", {}) or {}).get(source[stable_key], {})
            fdc_id = ((food.get("source") or {}).get("fdc_id")
                      if isinstance(food, dict) else None)
            if force_name_remap or fdc_id is None:
                for key in ("id", "ingredient_id", "food_id"):
                    source.pop(key, None)
                source.setdefault("name", resolved.get("original_name") or "")
                if force_name_remap:
                    explicit_state = (source.get("preparation_state") or source.get("preparation")
                                      or source.get("processing_state") or source.get("processing")
                                      or source.get("food_state"))
                    if explicit_state:
                        state = str(explicit_state).strip()
                        name = str(source.get("name") or resolved.get("original_name") or "").strip()
                        if state and state.casefold() not in name.casefold():
                            source["name"] = f"{state} {name}".strip()
                if source.get("quantity_raw") is None and resolved.get("quantity_g") is not None:
                    source["quantity_raw"] = f"{resolved['quantity_g']:g} g"
        return source

    @staticmethod
    def _item_cache_key(source, resolved, mapping_version):
        return (
            str(mapping_version), str(source.get("id") or source.get("ingredient_id") or ""),
            str(resolved.get("normalized_name") or resolved.get("original_name") or "").casefold(),
            repr(sorted((resolved.get("explicit_attributes") or {}).items())),
            str(source.get("food_state") or "").casefold(),
            str(source.get("preparation_state") or source.get("preparation") or "").casefold(),
            str(source.get("processing_state") or source.get("processing") or "").casefold(),
        )

    @staticmethod
    def _unresolved_mapping_row(position, item, reason):
        return {
            "position": position,
            "original_name": item.get("original_name"),
            "normalized_name": item.get("normalized_name"),
            "mapping": {
                "status": "UNRESOLVED", "fdc_id": None,
                "mapping_version": None, "verification_provenance": None,
                "candidate_ids": [], "unresolved_attributes": [],
                "ambiguity_reasons": [reason], "nutritional_uncertainty": "UNRELIABLE",
            },
        }

    @staticmethod
    def _unresolved_ingredients(calculation):
        results = []
        for item in calculation.get("ingredients") or []:
            if item.get("calculation_status") not in {"COMPLETE", "ASSUMPTION_BASED"}:
                results.append({
                    "position": item.get("position"),
                    "ingredient": item.get("ingredient_name"),
                    "quantity_status": item.get("quantity_status"),
                    "mapping_status": item.get("mapping_status"),
                    "reasons": item.get("block_reasons") or item.get("nutrient_data_issues") or [],
                })
        return results

    @staticmethod
    def _servings(recipe):
        value = recipe.get("recipe_servings", recipe.get("servings"))
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        return number if number > 0 else None

    @staticmethod
    def _recipe_representation(recipe):
        keys = (
            "id", "name", "recipe_servings", "servings", "recipe_servings_raw",
            "recipe_yield", "ingredients", "raw_ingredient_items", "ingredient_items",
            "raw_ingredients", "raw_ingredient_names", "raw_ingredient_quantities",
            "ingredient_quantities",
        )
        return {key: copy.deepcopy(recipe[key]) for key in keys if key in recipe}


__all__ = ["AdaptedRecipeNutritionEnrichmentService"]

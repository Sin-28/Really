"""Opt-in composition of quantity resolution, hybrid mapping, and calculation."""

import math
from time import perf_counter

from src.constraints.recipe_nutrient_adapter import RecipeNutrientConstraintAdapter
from src.nutrition import PRIMARY_RECIPE_NUTRIENTS
from src.recipes.ingredient_nutrient_calculator import IngredientNutrientCalculator
from src.recipes.quantity_resolution import IngredientQuantityResolver


class RecipeNutrientEnrichmentService:
    """Enrich a recipe without mutating source fields or replacing adaptation."""

    def __init__(self, kg, mapping_service, *, quantity_resolver=None, calculator=None):
        self.kg = kg
        self.mapping_service = mapping_service
        self.quantity_resolver = quantity_resolver or IngredientQuantityResolver()
        self.calculator = calculator or IngredientNutrientCalculator(
            kg, quantity_resolver=self.quantity_resolver,
        )

    def enrich(self, recipe, *, mapping_result=None, mapping_elapsed_ms=None,
               max_ingredients=None):
        started = perf_counter()
        quantity_resolution = self.quantity_resolver.resolve_recipe(recipe)
        if mapping_result is None:
            mapping_recipe = self._mapping_input(recipe, quantity_resolution)
            mapping_started = perf_counter()
            if max_ingredients is None:
                mapping_result = self.mapping_service.resolve_recipe(mapping_recipe)
            else:
                mapping_result = self.mapping_service.resolve_recipe(
                    mapping_recipe, max_ingredients=max_ingredients,
                )
            mapping_elapsed_ms = (perf_counter() - mapping_started) * 1000.0
        mapping_result["source_quantity_alignment_status"] = quantity_resolution.get("alignment_status")
        calculation = self.calculator.calculate_recipe(
            recipe, mapping_result, quantity_resolution=quantity_resolution,
        )
        calculation["ingredient_mapping_enrichment"] = mapping_result
        calculation["performance_ms"]["mapping_resolution"] = mapping_elapsed_ms
        calculation["performance_ms"]["overall_enrichment"] = (perf_counter() - started) * 1000.0
        return calculation

    @staticmethod
    def _mapping_input(recipe, quantity_resolution):
        """Pass names and validated attributes to Step 5A mapping.

        Quantities do not affect ingredient identity. Retaining a quantity here
        is useful for Step 5A's existing metadata, but only for a trustworthy
        row or aligned parallel arrays; mismatched arrays remain unpaired.
        """
        mapping_recipe = dict(recipe) if isinstance(recipe, dict) else {}
        items = []
        alignment = quantity_resolution.get("alignment_status")
        for resolved in quantity_resolution.get("ingredients", []):
            if not resolved.get("original_name"):
                continue
            row_alignment = resolved.get("quantity_alignment_status")
            quantity_raw = resolved.get("quantity_raw")
            if alignment == "MISMATCHED_ARRAY_LENGTHS" or row_alignment == "MISMATCHED_ARRAY_LENGTHS":
                quantity_raw = None
            items.append({
                "position": resolved.get("position"),
                "name": resolved["original_name"],
                "quantity_raw": quantity_raw,
                "attributes": resolved.get("explicit_attributes") or {},
            })
        mapping_recipe["raw_ingredient_items"] = items
        for source_key in ("raw_ingredients", "raw_ingredient_names",
                           "raw_ingredient_quantities", "ingredient_quantities"):
            mapping_recipe.pop(source_key, None)
        return mapping_recipe


def assess_foodcom_nutrient_evidence(calculation):
    """Classify mapping and USDA profile coverage without using source amounts."""
    rows = calculation.get("ingredients") or []
    keys = [definition[0] for definition in PRIMARY_RECIPE_NUTRIENTS.values()]
    availability = {key: {"available_ingredients": 0, "missing_ingredients": [],
                          "recorded_zero_ingredients": []} for key in keys}
    unresolved = []
    matched = 0
    complete = 0
    for row in rows:
        name = row.get("ingredient_name") or f"ingredient {row.get('position', '?')}"
        mapping = row.get("mapping") or {}
        mapping_ok = IngredientNutrientCalculator._mapping_is_authorized(mapping)
        # Approved defaults may carry assumptions; optimisation needs an
        # ingredient identity that does not depend on a substituted reference.
        mapping_ok = mapping_ok and mapping.get("status") != "REFERENCE_DEFAULT"
        if mapping_ok:
            matched += 1
        missing = []
        measurements = row.get("nutrient_measurements_per_100g") or {}
        for key in keys:
            measurement = measurements.get(key) or {}
            available = (mapping_ok and measurement.get("status") in {"RECORDED", "RECORDED_ZERO"}
                         and measurement.get("value_per_100g") is not None)
            if available:
                availability[key]["available_ingredients"] += 1
                if measurement["status"] == "RECORDED_ZERO":
                    availability[key]["recorded_zero_ingredients"].append(name)
            else:
                availability[key]["missing_ingredients"].append(name)
                missing.append(key)
        if mapping_ok and not missing:
            complete += 1
        else:
            unresolved.append({
                "position": row.get("position"), "name": name,
                "mapping_status": mapping.get("status") or "UNRESOLVED",
                "missing_nutrients": missing,
                "reason": ("Ingredient identity is unresolved or assumption-dependent."
                           if not mapping_ok else "Required USDA nutrient measurements are missing or invalid."),
            })
    total = len(rows)
    ready = total > 0 and complete == total and calculation.get("calculation_status") == "UNAVAILABLE"
    status = "REQUIRES_OPTIMISATION" if ready else "INSUFFICIENT_DATA"
    reason = (
        "Every ingredient has a supported USDA identity and sodium, potassium, phosphorus, and protein per 100 g; independent ingredient weights are still required."
        if ready else
        "At least one ingredient identity or required USDA nutrient measurement is missing; quantity optimisation cannot proceed reliably."
    )
    return {
        "nutritional_status": status,
        "ingredient_matching_coverage": {"matched": matched, "total": total,
                                         "fraction": matched / total if total else 0.0},
        "nutrient_data_availability": {key: {**data, "total_ingredients": total}
                                       for key, data in availability.items()},
        "missing_or_unresolved_ingredients": unresolved,
        "nutritional_reason": reason,
        "can_proceed_to_quantity_optimisation": ready,
        "quantity_basis": "FOODCOM_SOURCE_METADATA_ONLY",
    }


def assess_verified_nutritional_feasibility(calculation, constraints, *, requested_servings=None):
    """Classify a future, independently weighed recipe using the existing adapter.

    This entry point deliberately requires explicit provenance. Food.com source
    quantities cannot satisfy it, even when they look numeric.
    """
    calculation = calculation if isinstance(calculation, dict) else {}
    rows = calculation.get("ingredients") or []
    nutrient_keys = [definition[0] for definition in PRIMARY_RECIPE_NUTRIENTS.values()]
    def valid_nonnegative(value):
        return (isinstance(value, (int, float)) and not isinstance(value, bool)
                and math.isfinite(value) and value >= 0)

    verified = (
        calculation.get("quantity_source") == "INDEPENDENTLY_VERIFIED"
        and calculation.get("serving_basis_verified") is True
        and calculation.get("calculation_status") == "COMPLETE"
        and isinstance(calculation.get("servings"), (int, float))
        and not isinstance(calculation.get("servings"), bool)
        and math.isfinite(calculation["servings"]) and calculation["servings"] > 0
        and bool(rows)
        and all(valid_nonnegative(row.get("quantity_g")) and row["quantity_g"] > 0
                and row.get("calculation_status") == "COMPLETE"
                and IngredientNutrientCalculator._mapping_is_authorized(row.get("mapping"))
                and row["mapping"].get("status") != "REFERENCE_DEFAULT"
                and all(valid_nonnegative((row.get("nutrients") or {}).get(key))
                        for key in nutrient_keys)
                for row in rows)
    )
    if not verified:
        return {"nutritional_status": "INSUFFICIENT_DATA",
                "nutritional_reason": "Independent gram weights and a verified serving basis are required.",
                "constraint_assessment": None}
    assessment = RecipeNutrientConstraintAdapter().evaluate(
        calculation, constraints, requested_servings=requested_servings,
    )
    status = {"PASS": "FEASIBLE", "FAIL": "INFEASIBLE"}.get(
        assessment["constraint_status"], "INSUFFICIENT_DATA",
    )
    return {
        "nutritional_status": status,
        "nutritional_reason": assessment["explanation"],
        "constraint_assessment": assessment,
    }


__all__ = ["RecipeNutrientEnrichmentService", "assess_foodcom_nutrient_evidence",
           "assess_verified_nutritional_feasibility"]

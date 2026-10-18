"""Opt-in composition of quantity resolution, hybrid mapping, and calculation."""

from time import perf_counter

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

    def enrich(self, recipe, *, mapping_result=None, mapping_elapsed_ms=None):
        started = perf_counter()
        quantity_resolution = self.quantity_resolver.resolve_recipe(recipe)
        if mapping_result is None:
            mapping_recipe = self._mapping_input(recipe, quantity_resolution)
            mapping_started = perf_counter()
            mapping_result = self.mapping_service.resolve_recipe(mapping_recipe)
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


__all__ = ["RecipeNutrientEnrichmentService"]

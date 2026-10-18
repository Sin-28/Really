"""Deterministic nutrient calculation from authorized USDA resolutions.

Candidate lists never authorize a calculation. This module consumes results
from the existing hybrid identity resolver and retrieves the selected USDA
FoodRecord's original nutrient measurements through the Neo4j repository.
"""

import json
import math
import re
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from time import perf_counter

from src.nutrition import PRIMARY_RECIPE_NUTRIENTS
from src.recipes.ingredient_parser import WEIGHT_GRAMS
from src.recipes.quantity_resolution import IngredientQuantityResolver


CALCULATION_VERSION = "usda-recipe-nutrients-v1"
USDA_PORTION_REFERENCE_VERSION = "usda-food-portion-v1"
AUTHORIZED_MAPPING_STATUSES = {"VERIFIED_MAPPING", "EXACT_IDENTITY", "SLM_RESOLVED", "REFERENCE_DEFAULT"}


def _to_decimal(value):
    try:
        if isinstance(value, bool) or value is None:
            return None
        value = Decimal(str(value))
        return value if value.is_finite() else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _public_decimal(value):
    return None if value is None else float(value)


def _fdc_id(value):
    if isinstance(value, bool) or not str(value or "").strip().isdigit():
        return None
    value = int(value)
    return value if value > 0 else None


def _as_portions(food):
    portions = food.get("food_portions")
    if isinstance(portions, list):
        return portions
    raw = food.get("food_portions_json")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return []
    return parsed if isinstance(parsed, list) else []


def _canonical_unit(value):
    text = " ".join(str(value or "").casefold().strip().split())
    aliases = {
        "tablespoon": "tbsp", "tablespoons": "tbsp", "tbsp": "tbsp",
        "teaspoon": "tsp", "teaspoons": "tsp", "tsp": "tsp",
        "cups": "cup", "cup": "cup",
        "fluid ounce": "fl oz", "fluid ounces": "fl oz", "fl oz": "fl oz",
        "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml", "ml": "ml",
        "liter": "l", "liters": "l", "litre": "l", "litres": "l", "l": "l",
        "pints": "pint", "pint": "pint", "quarts": "quart", "quart": "quart",
        "gallons": "gallon", "gallon": "gallon", "sticks": "stick", "stick": "stick",
        "cloves": "clove", "clove": "clove", "slices": "slice", "slice": "slice",
        "pieces": "piece", "piece": "piece", "pinches": "pinch", "pinch": "pinch",
        "dashes": "dash", "dash": "dash", "packages": "package", "package": "package",
        "cans": "can", "can": "can", "count": "count",
    }
    return aliases.get(text)


def _canonical_unit_in_modifier(modifier):
    text = " ".join(str(modifier or "").casefold().strip().split())
    # USDA portion modifiers sometimes include an explicit edible count after
    # a volume measure, e.g. "cup (4.86 large eggs)". Its unit is still cup.
    if text.startswith("cup ("):
        return "cup", {}
    direct = _canonical_unit(text)
    if direct:
        return direct, {}
    # These count modifiers are only usable when the source states the same
    # attribute. A bare "large" portion is never a generic count conversion.
    size_map = {
        "extra large": "extra_large", "jumbo": "jumbo", "large": "large",
        "medium": "medium", "small": "small", "peewee": "peewee",
    }
    for label, value in size_map.items():
        if text == label:
            return "count", {"egg_size": value}
    return None, {}


@dataclass(frozen=True)
class IngredientUnitConversionReference:
    """A reusable, auditable ingredient-specific unit-to-gram reference."""

    ingredient_identity: str
    unit: str
    grams_per_unit: float
    source: str
    applicable_attributes: dict
    assumptions: tuple
    reference_version: str
    fdc_id: int
    source_url: str | None
    portion_id: str | None
    portion_modifier: str


class USDAFoodPortionConversionStore:
    """Build unit conversion references from USDA FoodData Central portions."""

    def references_for_food(self, food):
        if not isinstance(food, dict):
            return []
        fdc = _fdc_id(food.get("fdc_id"))
        if fdc is None:
            return []
        desc = str(food.get("description") or food.get("name") or f"FDC {fdc}")
        source_name = str(food.get("source_dataset") or "USDA FoodData Central")
        source_url = food.get("source_url") or f"https://fdc.nal.usda.gov/food-details/{fdc}/nutrients"
        references = []
        for portion in _as_portions(food):
            if not isinstance(portion, dict):
                continue
            modifier = str(portion.get("modifier") or "").strip()
            unit, attributes = _canonical_unit_in_modifier(modifier)
            if unit is None:
                continue
            if attributes.get("egg_size") and not re.search(r"\beggs?\b", desc.casefold()):
                continue
            gram_weight = _to_decimal(portion.get("gramWeight"))
            amount = _to_decimal(portion.get("amount") or portion.get("value") or 1)
            if gram_weight is None or amount is None or gram_weight <= 0 or amount <= 0:
                continue
            grams_per_unit = float(gram_weight / amount)
            if not math.isfinite(grams_per_unit) or grams_per_unit <= 0:
                continue
            portion_id = portion.get("id")
            source = (
                f"{source_name}, FDC {fdc}, Food Portion {portion_id or 'unspecified'}: "
                f"{amount} {modifier} = {gram_weight} g"
            )
            references.append(IngredientUnitConversionReference(
                ingredient_identity=f"{desc} (FDC {fdc})",
                unit=unit,
                grams_per_unit=grams_per_unit,
                source=source,
                applicable_attributes=attributes,
                assumptions=(),
                reference_version=USDA_PORTION_REFERENCE_VERSION,
                fdc_id=fdc,
                source_url=source_url,
                portion_id=str(portion_id) if portion_id is not None else None,
                portion_modifier=modifier,
            ))
        return references

    def find(self, food, unit, explicit_attributes=None):
        unit = _canonical_unit(unit)
        if unit is None or unit in WEIGHT_GRAMS:
            return None, "NO_INGREDIENT_SPECIFIC_FACTOR"
        attributes = dict(explicit_attributes or {})
        references = [reference for reference in self.references_for_food(food)
                      if reference.unit == unit]
        compatible = []
        for reference in references:
            if all(str(attributes.get(key, "")).casefold().replace(" ", "_")
                   == str(value).casefold().replace(" ", "_")
                   for key, value in reference.applicable_attributes.items()):
                compatible.append(reference)
        if not compatible:
            if references:
                return None, "REQUIRED_PORTION_ATTRIBUTES_NOT_STATED"
            return None, "NO_INGREDIENT_SPECIFIC_FACTOR"
        gram_values = {round(item.grams_per_unit, 12) for item in compatible}
        if len(gram_values) != 1:
            return None, "MULTIPLE_INCOMPATIBLE_PORTION_FACTORS"
        return compatible[0], None


class IngredientNutrientCalculator:
    """Calculate ingredient and recipe nutrients with deterministic arithmetic."""

    def __init__(self, kg, *, quantity_resolver=None, portion_store=None):
        self.kg = kg
        self.quantity_resolver = quantity_resolver or IngredientQuantityResolver()
        self.portion_store = portion_store or USDAFoodPortionConversionStore()

    def calculate_recipe(self, recipe, mapping_enrichment, *, quantity_resolution=None,
                         food_record_cache=None):
        overall_started = perf_counter()
        quantity_resolution = quantity_resolution or self.quantity_resolver.resolve_recipe(recipe)
        ingredients = quantity_resolution.get("ingredients") or []
        mapping_results = self._mapping_by_position(mapping_enrichment)
        mapping_ids = set()
        for item in ingredients:
            mapping = mapping_results.get(item.get("position"), {}).get("mapping") or {}
            if self._mapping_is_authorized(mapping):
                mapping_ids.add(_fdc_id(mapping.get("fdc_id")))
        mapping_ids.discard(None)

        lookup_started = perf_counter()
        cache = food_record_cache if isinstance(food_record_cache, dict) else None
        ids_to_lookup = (sorted(mapping_ids) if cache is None else
                         sorted(food_id for food_id in mapping_ids if food_id not in cache))
        try:
            rows = self.kg.get_usda_food_records_by_fdc_ids(ids_to_lookup) if ids_to_lookup else []
            lookup_error = None
        except Exception as exc:
            rows = []
            lookup_error = f"USDA nutrient lookup failed ({type(exc).__name__})."
        nutrient_lookup_ms = (perf_counter() - lookup_started) * 1000.0
        foods = {}
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            food = row.get("food") if isinstance(row.get("food"), dict) else {}
            fdc = _fdc_id(food.get("fdc_id"))
            if fdc is not None:
                foods[fdc] = {"food": food, "nutrients": row.get("nutrients") or []}
        if cache is not None and lookup_error is None:
            for food_id in ids_to_lookup:
                cache[food_id] = foods.get(food_id)
            foods = {food_id: cache.get(food_id) for food_id in mapping_ids}

        calculation_started = perf_counter()
        ingredient_results = []
        for quantity in ingredients:
            mapping_result = mapping_results.get(quantity.get("position")) or {}
            mapping = mapping_result.get("mapping") or {}
            ingredient_results.append(self._calculate_ingredient(
                quantity, mapping, foods, lookup_error=lookup_error,
            ))
        ingredient_calculation_ms = (perf_counter() - calculation_started) * 1000.0

        aggregation_started = perf_counter()
        calculated = [item for item in ingredient_results if item.get("calculation_status") in {"COMPLETE", "ASSUMPTION_BASED"}]
        unresolved = [item for item in ingredient_results if item not in calculated]
        nutrient_keys = [definition[0] for definition in PRIMARY_RECIPE_NUTRIENTS.values()]
        totals = {key: sum((_to_decimal(item["nutrients"][key]) for item in calculated), Decimal("0"))
                  for key in nutrient_keys}
        has_assumptions = any(item.get("calculation_status") == "ASSUMPTION_BASED" for item in calculated)
        if not ingredient_results or not calculated:
            status = "UNAVAILABLE"
        elif unresolved:
            status = "INCOMPLETE"
        elif has_assumptions:
            status = "ASSUMPTION_BASED"
        else:
            status = "COMPLETE"

        servings = self._servings(recipe)
        recipe_totals = None if status in {"INCOMPLETE", "UNAVAILABLE"} else {
            key: _public_decimal(value) for key, value in totals.items()
        }
        calculated_subtotals = ({key: _public_decimal(value) for key, value in totals.items()}
                                if calculated else None)
        per_serving = None
        per_serving_reason = None
        if servings is None:
            per_serving_reason = "A positive, finite source serving count is unavailable."
        elif recipe_totals is not None:
            divisor = _to_decimal(servings)
            per_serving = {key: _public_decimal(value / divisor) for key, value in totals.items()}
        else:
            per_serving_reason = "Per-serving values are withheld because the recipe calculation is incomplete."

        references = {}
        for item in calculated:
            reference = item.get("usda_reference")
            if reference:
                references[str(reference["fdc_id"])] = reference
        quantity_assumptions = []
        mapping_uncertainty = []
        missing_nutrients = []
        for item in ingredient_results:
            if item.get("quantity_conversion_reference"):
                quantity_assumptions.append({
                    "position": item["position"],
                    "ingredient": item["ingredient_name"],
                    **item["quantity_conversion_reference"],
                })
            mapping_uncertainty.append({
                "position": item["position"], "ingredient": item["ingredient_name"],
                "status": item.get("mapping", {}).get("status"),
                "nutritional_uncertainty": item.get("mapping", {}).get("nutritional_uncertainty"),
                "assumed_attributes": item.get("mapping", {}).get("assumed_attributes") or {},
                "unresolved_attributes": item.get("mapping", {}).get("unresolved_attributes") or [],
                "candidate_ids": item.get("mapping", {}).get("candidate_ids") or [],
                "ambiguity_reasons": item.get("mapping", {}).get("ambiguity_reasons") or [],
                "block_reasons": item.get("block_reasons") or [],
            })
            if item.get("nutrient_data_issues"):
                missing_nutrients.append({
                    "position": item["position"], "ingredient": item["ingredient_name"],
                    "issues": item["nutrient_data_issues"],
                })
        aggregation_ms = (perf_counter() - aggregation_started) * 1000.0

        return {
            "version": CALCULATION_VERSION,
            "calculation_status": status,
            "ingredient_count": len(ingredient_results),
            "calculated_ingredient_count": len(calculated),
            "unresolved_ingredient_count": len(unresolved),
            "ingredients": ingredient_results,
            "recipe_totals": recipe_totals,
            "calculated_subtotals": calculated_subtotals,
            "per_serving": per_serving,
            "servings": servings,
            "per_serving_unavailable_reason": per_serving_reason,
            "usda_references_used": list(references.values()),
            "quantity_conversion_assumptions": quantity_assumptions,
            "mapping_uncertainty": mapping_uncertainty,
            "missing_nutrient_information": missing_nutrients,
            "unresolved_ingredients": [
                {"position": item.get("position"), "ingredient": item.get("ingredient_name"),
                 "quantity_status": item.get("quantity_status"),
                 "mapping_status": (item.get("mapping") or {}).get("status"),
                 "reasons": item.get("block_reasons") or [],
                 "mapping_reasons": ((item.get("mapping") or {}).get("ambiguity_reasons") or [])}
                for item in unresolved
            ],
            "quantity_alignment_status": quantity_resolution.get("alignment_status"),
            "performance_ms": {
                "quantity_resolution": quantity_resolution.get("quantity_resolution_ms"),
                "nutrient_lookup": nutrient_lookup_ms,
                "ingredient_calculation": ingredient_calculation_ms,
                "recipe_aggregation": aggregation_ms,
                "overall_calculation": (perf_counter() - overall_started) * 1000.0,
            },
        }

    @staticmethod
    def _mapping_by_position(mapping_enrichment):
        result = {}
        if not isinstance(mapping_enrichment, dict):
            return result
        for item in mapping_enrichment.get("results", []):
            if isinstance(item, dict) and isinstance(item.get("position"), int):
                result[item["position"]] = item
        return result

    @staticmethod
    def _mapping_is_authorized(mapping):
        if not isinstance(mapping, dict):
            return False
        status = mapping.get("status")
        if status not in AUTHORIZED_MAPPING_STATUSES or _fdc_id(mapping.get("fdc_id")) is None:
            return False
        if status == "REFERENCE_DEFAULT":
            return mapping.get("reference_default") is True
        if mapping.get("unresolved_attributes"):
            return False
        if mapping.get("nutritional_uncertainty") == "UNRELIABLE":
            return False
        return True

    def _calculate_ingredient(self, quantity, mapping, foods, *, lookup_error=None):
        position = quantity.get("position")
        name = str(quantity.get("original_name") or "")
        result = {
            "position": position,
            "ingredient_name": name,
            "original_text": quantity.get("original_text"),
            "quantity_raw": quantity.get("quantity_raw"),
            "quantity": quantity.get("quantity"),
            "unit": quantity.get("unit"),
            "quantity_g": quantity.get("quantity_g"),
            "quantity_g_range": None,
            "quantity_status": quantity.get("quantity_status"),
            "mapping": self._mapping_provenance(mapping),
            "nutrients": None,
            "nutrient_measurements_per_100g": {},
            "nutrient_data_issues": [],
            "usda_reference": None,
            "quantity_conversion_reference": None,
            "calculation_status": "INCOMPLETE",
            "block_reasons": [],
        }
        fdc = _fdc_id(mapping.get("fdc_id")) if self._mapping_is_authorized(mapping) else None
        if fdc is None:
            result["block_reasons"].append("MAPPING_NOT_AUTHORIZED_OR_UNRESOLVED")
        elif lookup_error:
            result["block_reasons"].append("USDA_NUTRIENT_LOOKUP_FAILED")
        elif fdc not in foods:
            result["block_reasons"].append("SELECTED_USDA_FOOD_NOT_FOUND")

        food_record = foods.get(fdc) if fdc is not None else None
        food = food_record.get("food") if food_record else None
        quantity_g = _to_decimal(quantity.get("quantity_g"))
        is_range = (quantity.get("quantity_status") in {"CONVERTED_RANGE", "UNRESOLVED_RANGE"}
                    and quantity.get("quantity_min") is not None
                    and quantity.get("quantity_max") is not None)
        factor = None
        if quantity_g is None and food and not is_range:
            factor, factor_reason = self.portion_store.find(
                food, quantity.get("unit"), quantity.get("explicit_attributes"),
            )
            amount = _to_decimal(quantity.get("quantity"))
            if factor and amount is not None and quantity.get("quantity_scope") == "TOTAL":
                quantity_g = amount * _to_decimal(factor.grams_per_unit)
                result["quantity_conversion_reference"] = asdict(factor)
                result["quantity_g"] = _public_decimal(quantity_g)
            elif factor_reason and quantity.get("quantity_status") not in {"MISSING", "CONVERTED_RANGE", "UNRESOLVED_RANGE"}:
                result["block_reasons"].append(factor_reason)

        if is_range:
            minimum = _to_decimal(quantity.get("quantity_g_min"))
            maximum = _to_decimal(quantity.get("quantity_g_max"))
            if minimum is None and food:
                range_factor, _ = self.portion_store.find(
                    food, quantity.get("unit"), quantity.get("explicit_attributes"),
                )
                if range_factor:
                    per_unit = _to_decimal(range_factor.grams_per_unit)
                    minimum = _to_decimal(quantity.get("quantity_min")) * per_unit
                    maximum = _to_decimal(quantity.get("quantity_max")) * per_unit
                    result["quantity_conversion_reference"] = asdict(range_factor)
            result["quantity_g_range"] = {
                "minimum": _public_decimal(minimum), "maximum": _public_decimal(maximum),
            } if minimum is not None and maximum is not None else None
            result["block_reasons"].append("QUANTITY_RANGE_HAS_NO_SINGLE_DEFINITIVE_AMOUNT")
        elif quantity_g is None:
            result["block_reasons"].extend(quantity.get("block_reasons") or ["QUANTITY_NOT_RESOLVED_TO_GRAMS"])
        elif quantity.get("quantity_scope") != "TOTAL":
            quantity_g = None
            result["quantity_g"] = None
            result["block_reasons"].append("QUANTITY_IS_NOT_A_RECIPE_TOTAL")

        if food is not None:
            nutrient_values, nutrient_issues, measurements = self._nutrients_per_100g(
                food, food_record.get("nutrients", []),
            )
        else:
            nutrient_values, measurements = None, {}
            if lookup_error:
                nutrient_issues = [{"status": "UNAVAILABLE", "reason": lookup_error}]
            elif fdc is not None:
                nutrient_issues = [{"status": "MISSING_FOOD_RECORD", "fdc_id": fdc}]
            else:
                nutrient_issues = []
        result["nutrient_measurements_per_100g"] = measurements
        result["nutrient_data_issues"] = nutrient_issues
        if nutrient_issues:
            result["block_reasons"].append("REQUIRED_NUTRIENT_DATA_INCOMPLETE")
        if quantity_g is None and "QUANTITY_RANGE_HAS_NO_SINGLE_DEFINITIVE_AMOUNT" not in result["block_reasons"]:
            if "QUANTITY_NOT_RESOLVED_TO_GRAMS" not in result["block_reasons"]:
                result["block_reasons"].append("QUANTITY_NOT_RESOLVED_TO_GRAMS")

        if food and fdc is not None:
            result["usda_reference"] = self._usda_reference(food)
        if quantity_g is not None and not nutrient_issues and nutrient_values is not None and fdc is not None:
            result["nutrients"] = {
                key: _public_decimal(value * quantity_g / Decimal("100"))
                for key, value in nutrient_values.items()
            }
            assumption_based = (
                mapping.get("status") == "REFERENCE_DEFAULT"
                and (mapping.get("nutritional_uncertainty") == "ASSUMPTION_BASED"
                     or bool(mapping.get("assumed_attributes")))
            )
            result["calculation_status"] = "ASSUMPTION_BASED" if assumption_based else "COMPLETE"
        return result

    @staticmethod
    def _mapping_provenance(mapping):
        keys = (
            "status", "fdc_id", "usda_description", "resolution_reason",
            "verification_method", "verification_policy_version", "verification_provenance",
            "reference_default", "reference_fdc_id", "reference_policy_version",
            "assumed_attributes", "unresolved_attributes", "nutritional_uncertainty",
            "candidate_ids", "ambiguity_reasons", "missing_attributes", "retrieval_status",
        )
        return {key: mapping.get(key) for key in keys if key in mapping}

    @staticmethod
    def _servings(recipe):
        if not isinstance(recipe, dict):
            return None
        value = recipe.get("recipe_servings")
        if value is None:
            value = recipe.get("servings")
        number = _to_decimal(value)
        return float(number) if number is not None and number > 0 else None

    @staticmethod
    def _basis_valid(food):
        basis = str(food.get("nutrition_basis") or food.get("basis") or "").casefold()
        normalized = re.sub(r"\s+", "", basis)
        return normalized in {"per100g", "per100gram", "per100grams", "100g", "100gram", "100grams"}

    @staticmethod
    def _unit_matches(actual, expected):
        text = " ".join(str(actual or "").casefold().strip().split())
        aliases = {
            "mg": {"mg", "milligram", "milligrams"},
            "g": {"g", "gram", "grams"},
        }
        return text in aliases.get(expected, {expected})

    def _nutrients_per_100g(self, food, records):
        if not food:
            return None, [{"status": "MISSING_FOOD_RECORD", "reason": "Selected USDA FoodRecord was not found."}], {}
        if not self._basis_valid(food):
            issue = {"status": "INVALID_MEASUREMENT_BASIS",
                     "basis": food.get("nutrition_basis") or food.get("basis")}
            measurements = {
                key: {"fdc_nutrient_id": nutrient_id, "status": "INVALID_MEASUREMENT_BASIS",
                      "unit": None, "value_per_100g": None, "basis": issue["basis"]}
                for nutrient_id, (key, _unit) in PRIMARY_RECIPE_NUTRIENTS.items()
            }
            return None, [issue], measurements
        values = {}
        issues = []
        measurements = {}
        for nutrient_id, (key, expected_unit) in PRIMARY_RECIPE_NUTRIENTS.items():
            matching = [record for record in records or []
                        if isinstance(record, dict)
                        and str(record.get("fdc_nutrient_id")) == str(nutrient_id)]
            if not matching:
                issues.append({"nutrient_id": nutrient_id, "nutrient": key, "status": "MISSING"})
                measurements[key] = {"fdc_nutrient_id": nutrient_id, "status": "MISSING",
                                     "unit": None, "value_per_100g": None}
                continue
            if len(matching) != 1:
                values_seen = {(record.get("amount"), record.get("unit")) for record in matching}
                if len(values_seen) != 1:
                    issues.append({"nutrient_id": nutrient_id, "nutrient": key, "status": "DUPLICATE_CONFLICT"})
                    measurements[key] = {"fdc_nutrient_id": nutrient_id, "status": "DUPLICATE_CONFLICT",
                                         "unit": None, "value_per_100g": None}
                    continue
                record = matching[0]
            else:
                record = matching[0]
            amount = _to_decimal(record.get("amount"))
            if amount is None:
                issues.append({"nutrient_id": nutrient_id, "nutrient": key, "status": "MISSING_VALUE"})
                measurements[key] = {"fdc_nutrient_id": nutrient_id, "status": "MISSING_VALUE",
                                     "unit": record.get("unit"), "value_per_100g": None}
                continue
            if amount < 0:
                issues.append({"nutrient_id": nutrient_id, "nutrient": key, "status": "INVALID_VALUE"})
                measurements[key] = {"fdc_nutrient_id": nutrient_id, "status": "INVALID_VALUE",
                                     "unit": record.get("unit"), "value_per_100g": None}
                continue
            if not self._unit_matches(record.get("unit"), expected_unit):
                issues.append({"nutrient_id": nutrient_id, "nutrient": key,
                               "status": "UNSUPPORTED_UNIT", "unit": record.get("unit"),
                               "expected_unit": expected_unit})
                measurements[key] = {"fdc_nutrient_id": nutrient_id, "status": "UNSUPPORTED_UNIT",
                                     "unit": record.get("unit"), "value_per_100g": None,
                                     "expected_unit": expected_unit}
                continue
            values[key] = amount
            measurements[key] = {
                "fdc_nutrient_id": nutrient_id,
                "status": "RECORDED_ZERO" if amount == 0 else "RECORDED",
                "unit": record.get("unit"),
                "value_per_100g": _public_decimal(amount),
            }
        if issues:
            return None, issues, measurements
        return values, [], measurements

    @staticmethod
    def _usda_reference(food):
        fdc = _fdc_id(food.get("fdc_id"))
        return {
            "fdc_id": fdc,
            "description": food.get("description") or food.get("name"),
            "dataset": food.get("source_dataset") or food.get("dataset_key"),
            "dataset_key": food.get("dataset_key"),
            "data_type": food.get("data_type"),
            "nutrition_basis": food.get("nutrition_basis"),
            "source_url": food.get("source_url") or (
                f"https://fdc.nal.usda.gov/food-details/{fdc}/nutrients" if fdc else None
            ),
        }


__all__ = [
    "IngredientNutrientCalculator", "USDAFoodPortionConversionStore",
    "IngredientUnitConversionReference", "CALCULATION_VERSION",
    "USDA_PORTION_REFERENCE_VERSION",
]

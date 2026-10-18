"""Evaluate USDA recipe nutrient calculations against existing active budgets.

This adapter is informational. It consumes the Step 5B-1 calculation result and
the request's already-normalized RenAlly constraints; it does not define new
thresholds or participate in recipe selection/adaptation.
"""

import math
from time import perf_counter

from src.nutrition import NUTRIENT_UNITS, normalize_nutrient_name


_SUPPORTED_NUTRIENTS = {
    "sodium_mg": "mg",
    "potassium_mg": "mg",
    "phosphorus_mg": "mg",
    "protein_g": "g",
}
_AUTHORIZED_MAPPING_STATUSES = {"VERIFIED_MAPPING", "EXACT_IDENTITY", "SLM_RESOLVED", "REFERENCE_DEFAULT"}
_UPPER = {"max": "<=", "max_value": "<=", "upper": "<=", "upper_bound": "<="}
_LOWER = {"min": ">=", "min_value": ">=", "lower": ">=", "lower_bound": ">="}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _fdc_id(value):
    if isinstance(value, bool):
        return None
    try:
        result = int(value)
    except (TypeError, ValueError):
        return None
    return result if result > 0 and str(value).strip() == str(result) else None


def _unit(value):
    normalized = " ".join(str(value or "").casefold().strip().split())
    return {
        "milligram": "mg", "milligrams": "mg", "mg": "mg",
        "gram": "g", "grams": "g", "g": "g",
    }.get(normalized)


class RecipeNutrientConstraintAdapter:
    """Evaluate each explicit active RenAlly bound against USDA evidence."""

    def evaluate(self, calculation, constraints, *, requested_servings=None):
        started = perf_counter()
        calculation = calculation if isinstance(calculation, dict) else {}
        constraints = constraints if isinstance(constraints, dict) else {}
        active = self._active_bounds(constraints)
        evidence = self._evidence(calculation)

        if not active:
            return {
                "constraint_status": "NOT_APPLICABLE",
                "calculation_status": calculation.get("calculation_status", "UNAVAILABLE"),
                "active_constraint_count": 0,
                "evaluations": [],
                "assumption_dependent": False,
                "missing_evidence": [],
                "explanation": "No active numeric meal or remaining-intake constraints were supplied.",
                **evidence,
                "performance_ms": {"constraint_adapter": (perf_counter() - started) * 1000.0},
            }

        results = []
        for bound in active:
            results.append(self._evaluate_bound(
                bound, calculation, requested_servings=requested_servings,
            ))

        statuses = {item["status"] for item in results}
        if "FAIL" in statuses:
            overall = "FAIL"
            explanation = "At least one active nutrient constraint is definitively violated."
        elif "REVIEW_REQUIRED" in statuses:
            overall = "REVIEW_REQUIRED"
            explanation = "Approved reference assumptions may affect one or more active constraint decisions."
        elif "UNKNOWN" in statuses:
            overall = "UNKNOWN"
            explanation = "Available evidence is insufficient to evaluate every active nutrient constraint."
        else:
            overall = "PASS"
            explanation = "Every active nutrient constraint passes on complete, authorized, basis-compatible data."

        assumption_dependent = any(item.get("assumption_dependent") for item in results)
        missing_evidence = []
        for item in results:
            for missing in item.get("missing_evidence") or []:
                if missing not in missing_evidence:
                    missing_evidence.append(missing)
        return {
            "constraint_status": overall,
            "calculation_status": calculation.get("calculation_status", "UNAVAILABLE"),
            "active_constraint_count": len(active),
            "evaluations": results,
            "assumption_dependent": assumption_dependent,
            "missing_evidence": missing_evidence,
            "explanation": explanation,
            **evidence,
            "performance_ms": {"constraint_adapter": (perf_counter() - started) * 1000.0},
        }

    @staticmethod
    def _active_bounds(constraints):
        bounds = []
        for name, rule in constraints.items():
            if not isinstance(rule, dict):
                continue
            nutrient = normalize_nutrient_name(name)
            emitted = False
            for key, operator in (*_UPPER.items(), *_LOWER.items()):
                threshold = rule.get(key)
                if threshold is None:
                    continue
                # Avoid duplicate aliases in rules that happen to carry both.
                if any(item["nutrient_key"] == nutrient and item["operator"] == operator
                       for item in bounds):
                    emitted = True
                    break
                bounds.append({
                    "nutrient": str(name), "nutrient_key": nutrient,
                    "operator": operator, "threshold": threshold,
                    "unit": rule.get("unit"), "scope": rule.get("scope", "meal"),
                    "basis": rule.get("basis"),
                })
                emitted = True
            # Preserve a malformed/qualitative active request as UNKNOWN instead
            # of silently dropping it from an otherwise passing result.
            if not emitted:
                bounds.append({
                    "nutrient": str(name), "nutrient_key": nutrient,
                    "operator": None, "threshold": None,
                    "unit": rule.get("unit") if isinstance(rule, dict) else None,
                    "scope": rule.get("scope", "meal") if isinstance(rule, dict) else "meal",
                    "basis": rule.get("basis") if isinstance(rule, dict) else None,
                })
        return bounds

    def _evaluate_bound(self, bound, calculation, *, requested_servings):
        nutrient = bound["nutrient_key"]
        expected_unit = _SUPPORTED_NUTRIENTS.get(nutrient)
        threshold = _number(bound.get("threshold"))
        unit = _unit(bound.get("unit") or NUTRIENT_UNITS.get(nutrient))
        basis, basis_reason = self._basis(bound, calculation, requested_servings)
        entry = {
            "nutrient": bound["nutrient"],
            "nutrient_key": nutrient,
            "operator": bound.get("operator"),
            "threshold": threshold,
            "unit": bound.get("unit") or NUTRIENT_UNITS.get(nutrient),
            "basis": basis,
            "observed_value": None,
            "known_subtotal": None,
            "status": "UNKNOWN",
            "reason": None,
            "assumption_dependent": False,
            "missing_evidence": [],
        }
        if expected_unit is None:
            entry["reason"] = "The active constraint nutrient is not available from the USDA recipe calculation."
            entry["missing_evidence"].append(f"unsupported_nutrient:{nutrient}")
            return entry
        if bound.get("operator") not in {"<=", ">="} or threshold is None:
            entry["reason"] = "The active constraint has no supported finite numeric upper or lower bound."
            entry["missing_evidence"].append(f"constraint_definition:{nutrient}")
            return entry
        if unit != expected_unit:
            entry["reason"] = f"Constraint unit is incompatible with the calculated {expected_unit} nutrient values."
            entry["missing_evidence"].append(f"incompatible_unit:{nutrient}")
            return entry
        if basis_reason:
            entry["reason"] = basis_reason
            entry["missing_evidence"].append(f"serving_basis:{nutrient}")
            if _positive(calculation.get("servings")) is None:
                entry["missing_evidence"].append("source_serving_count")
            if str(bound.get("scope") or "meal").casefold() == "remaining" and _positive(requested_servings) is None:
                entry["missing_evidence"].append("requested_serving_count")
            return entry

        calculation_status = calculation.get("calculation_status")
        assumption = self._assumption_affects(calculation)
        complete = self._complete_for_required_data(calculation, nutrient)
        values = calculation.get("per_serving") if basis == "per_serving" else None
        if basis == "requested_servings":
            values = self._scaled_per_serving(
                calculation.get("per_serving"), requested_servings,
            )
        value = _number(values.get(nutrient)) if isinstance(values, dict) else None

        if calculation_status == "COMPLETE" and complete and value is not None:
            entry["observed_value"] = value
            if assumption:
                entry["status"] = "REVIEW_REQUIRED"
                entry["assumption_dependent"] = True
                entry["reason"] = "The calculated value includes an approved reference default assumption."
                return entry
            entry["status"] = self._compare(value, bound["operator"], threshold)
            entry["reason"] = self._result_reason(entry["status"], value, bound["operator"], threshold)
            return entry

        subtotal, subtotal_reason = self._verified_partial_subtotal(
            calculation, nutrient, basis, requested_servings,
        )
        entry["known_subtotal"] = subtotal
        if bound["operator"] == "<=" and subtotal is not None and subtotal > threshold:
            entry["status"] = "FAIL"
            entry["reason"] = "The verified nonnegative subtotal already exceeds the upper bound."
            return entry

        if assumption:
            entry["status"] = "REVIEW_REQUIRED"
            entry["assumption_dependent"] = True
            entry["reason"] = "Approved reference assumptions could change the constraint result; complete verified evidence is unavailable."
            entry["missing_evidence"].extend(self._missing_for_calculation(calculation, nutrient))
            return entry

        entry["reason"] = subtotal_reason or self._incomplete_reason(calculation)
        entry["missing_evidence"].extend(self._missing_for_calculation(calculation, nutrient))
        if not entry["missing_evidence"]:
            entry["missing_evidence"].append(f"complete_calculation:{nutrient}")
        return entry

    @staticmethod
    def _basis(bound, calculation, requested_servings):
        scope = str(bound.get("scope") or "meal").strip().casefold()
        expected = {"meal": "per_serving", "remaining": "requested_servings"}.get(scope)
        explicit = str(bound.get("basis") or "").strip().casefold().replace("-", "_").replace(" ", "_")
        aliases = {
            "per_serving": "per_serving", "serving": "per_serving",
            "requested_servings": "requested_servings", "remaining": "requested_servings",
            "recipe_total": "recipe_total", "whole_recipe": "recipe_total",
        }
        if explicit:
            explicit = aliases.get(explicit, explicit)
            if expected and explicit != expected:
                return explicit, "Constraint and calculation serving bases do not match."
            if not expected:
                expected = explicit
        if expected not in {"per_serving", "requested_servings"}:
            return expected, "The active constraint serving basis is unsupported or unspecified."
        servings = _positive(calculation.get("servings"))
        if servings is None:
            return expected, "A positive source serving count is required to compare this recipe on a per-serving basis."
        if expected == "requested_servings" and _positive(requested_servings) is None:
            return expected, "A positive requested serving count is required for a remaining-intake constraint."
        return expected, None

    @staticmethod
    def _scaled_per_serving(values, requested_servings):
        requested = _positive(requested_servings)
        if not isinstance(values, dict) or requested is None:
            return None
        scaled = {}
        for key, value in values.items():
            number = _number(value)
            if number is not None:
                scaled[key] = number * requested
        return scaled

    @staticmethod
    def _complete_for_required_data(calculation, nutrient):
        count = calculation.get("ingredient_count")
        calculated = calculation.get("calculated_ingredient_count")
        ingredients = calculation.get("ingredients")
        if not isinstance(count, int) or count <= 0 or calculated != count:
            return False
        if not isinstance(ingredients, list) or len(ingredients) != count:
            return False
        positions = [item.get("position") for item in ingredients if isinstance(item, dict)]
        if len(positions) != count or any(position is None for position in positions) or len(set(positions)) != count:
            return False
        for item in ingredients:
            if not isinstance(item, dict) or item.get("calculation_status") != "COMPLETE":
                return False
            if _positive(item.get("quantity_g")) is None:
                # A measured zero quantity cannot support a meaningful recipe contribution.
                return False
            mapping = item.get("mapping") or {}
            if mapping.get("status") not in _AUTHORIZED_MAPPING_STATUSES or _fdc_id(mapping.get("fdc_id")) is None:
                return False
            if mapping.get("status") == "REFERENCE_DEFAULT" and mapping.get("reference_default") is not True:
                return False
            nutrients = item.get("nutrients")
            value = _number(nutrients.get(nutrient)) if isinstance(nutrients, dict) else None
            if value is None or value < 0:
                return False
        return True

    @staticmethod
    def _assumption_affects(calculation):
        if calculation.get("calculation_status") == "ASSUMPTION_BASED":
            return True
        for item in calculation.get("ingredients") or []:
            if not isinstance(item, dict):
                continue
            mapping = item.get("mapping") or {}
            if (item.get("calculation_status") == "ASSUMPTION_BASED"
                    or mapping.get("nutritional_uncertainty") == "ASSUMPTION_BASED"
                    or mapping.get("assumed_attributes")
                    or mapping.get("unresolved_attributes")):
                return True
        return False

    @staticmethod
    def _verified_partial_subtotal(calculation, nutrient, basis, requested_servings):
        if calculation.get("calculation_status") not in {"INCOMPLETE", "ASSUMPTION_BASED"}:
            return None, None
        ingredients = calculation.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            return None, None
        ingredient_count = calculation.get("ingredient_count")
        if not isinstance(ingredient_count, int) or ingredient_count != len(ingredients):
            return None, "The partial subtotal is not used because the ingredient result set is inconsistent."
        all_positions = [item.get("position") for item in ingredients if isinstance(item, dict)]
        if (len(all_positions) != len(ingredients) or any(position is None for position in all_positions)
                or len(set(all_positions)) != len(all_positions)):
            return None, "The partial subtotal is not used because calculated ingredient positions are duplicated or unknown."
        contributors = [item for item in ingredients
                        if isinstance(item, dict) and item.get("calculation_status") == "COMPLETE"]
        if not contributors:
            return None, None
        calculated_count = calculation.get("calculated_ingredient_count")
        all_calculated_count = sum(
            1 for item in ingredients
            if isinstance(item, dict)
            and item.get("calculation_status") in {"COMPLETE", "ASSUMPTION_BASED"}
        )
        if calculated_count != all_calculated_count or calculated_count < len(contributors):
            return None, "The partial subtotal is not used because its calculated ingredient count is inconsistent."
        subtotal = 0.0
        for item in contributors:
            mapping = item.get("mapping") or {}
            if (mapping.get("status") not in {"VERIFIED_MAPPING", "EXACT_IDENTITY", "SLM_RESOLVED"}
                    or _fdc_id(mapping.get("fdc_id")) is None
                    or item.get("calculation_status") != "COMPLETE"):
                return None, "The partial subtotal contains a mapping that is not verified without assumptions."
            quantity = _positive(item.get("quantity_g"))
            values = item.get("nutrients")
            value = _number(values.get(nutrient)) if isinstance(values, dict) else None
            if quantity is None or value is None or value < 0:
                return None, "The partial subtotal has missing, negative, or unresolved ingredient contributions."
            subtotal += value
        servings = _positive(calculation.get("servings"))
        if servings is None:
            return None, "A valid source serving count is required before using a partial subtotal."
        subtotal /= servings
        if basis == "requested_servings":
            requested = _positive(requested_servings)
            if requested is None:
                return None, "A valid requested serving count is required before using a partial subtotal."
            subtotal *= requested
        elif basis != "per_serving":
            return None, "The partial subtotal basis cannot be reconciled with the constraint."
        return subtotal, None

    @staticmethod
    def _compare(value, operator, threshold):
        return "PASS" if (value <= threshold if operator == "<=" else value >= threshold) else "FAIL"

    @staticmethod
    def _result_reason(status, value, operator, threshold):
        if status == "PASS":
            return "The complete observed value satisfies the active constraint."
        return f"The complete observed value {value:g} {operator} {threshold:g} violates the active constraint."

    @staticmethod
    def _incomplete_reason(calculation):
        status = calculation.get("calculation_status", "UNAVAILABLE")
        if status == "INCOMPLETE":
            return "Ingredient quantities, mappings, or nutrient measurements remain unresolved."
        if status == "UNAVAILABLE":
            return "The required USDA nutrient calculation is unavailable."
        if status != "COMPLETE":
            return f"Calculation status {status} is not sufficient for a definitive evaluation."
        return "The calculation is missing complete, authorized ingredient evidence."

    @staticmethod
    def _missing_for_calculation(calculation, nutrient):
        missing = []
        if _positive(calculation.get("servings")) is None:
            missing.append("source_serving_count")
        if calculation.get("calculation_status") != "COMPLETE":
            missing.append(f"calculation:{calculation.get('calculation_status', 'UNKNOWN')}")
        for item in calculation.get("unresolved_ingredients") or []:
            if isinstance(item, dict):
                position = item.get("position")
                missing.append(f"ingredient:{position if position is not None else 'unknown'}")
        if not missing:
            missing.append(f"nutrient_value:{nutrient}")
        return list(dict.fromkeys(missing))

    @staticmethod
    def _evidence(calculation):
        ingredients = [item for item in calculation.get("ingredients", []) if isinstance(item, dict)]
        references = calculation.get("usda_references_used") or []
        fdc_ids = set()
        for reference in references:
            if isinstance(reference, dict):
                fdc = _fdc_id(reference.get("fdc_id"))
                if fdc is not None:
                    fdc_ids.add(fdc)
        verification_methods = []
        mapping_evidence = []
        approved_reference_defaults = []
        defaults = []
        unresolved_mappings = []
        missing_quantities = []
        for item in ingredients:
            mapping = item.get("mapping") or {}
            fdc = _fdc_id(mapping.get("fdc_id"))
            if fdc is not None:
                fdc_ids.add(fdc)
            method = mapping.get("verification_method")
            if method:
                verification_methods.append(method)
            mapping_evidence.append({
                "position": item.get("position"),
                "ingredient": item.get("ingredient_name"),
                "mapping_status": mapping.get("status"),
                "fdc_id": fdc,
                "verification_method": method,
                "nutritional_uncertainty": mapping.get("nutritional_uncertainty"),
            })
            if mapping.get("status") == "REFERENCE_DEFAULT":
                reference = {
                    "position": item.get("position"),
                    "ingredient": item.get("ingredient_name"),
                    "fdc_id": fdc,
                    "reference_default": mapping.get("reference_default"),
                    "assumed_attributes": mapping.get("assumed_attributes") or {},
                    "unresolved_attributes": mapping.get("unresolved_attributes") or [],
                    "reference_policy_version": mapping.get("reference_policy_version"),
                }
                approved_reference_defaults.append(reference)
                if (mapping.get("nutritional_uncertainty") == "ASSUMPTION_BASED"
                        or mapping.get("assumed_attributes")
                        or mapping.get("unresolved_attributes")):
                    defaults.append(reference)
            if (mapping.get("status") not in _AUTHORIZED_MAPPING_STATUSES or fdc is None
                    or mapping.get("unresolved_attributes")
                    or mapping.get("nutritional_uncertainty") == "UNRELIABLE"):
                unresolved_mappings.append({
                    "position": item.get("position"), "ingredient": item.get("ingredient_name"),
                    "status": mapping.get("status"), "candidate_ids": mapping.get("candidate_ids") or [],
                })
            if (_positive(item.get("quantity_g")) is None
                    or item.get("quantity_status") in {"MISSING", "UNRESOLVED", "UNRESOLVED_RANGE"}):
                missing_quantities.append({
                    "position": item.get("position"), "ingredient": item.get("ingredient_name"),
                    "quantity_raw": item.get("quantity_raw"), "quantity_status": item.get("quantity_status"),
                    "reasons": item.get("block_reasons") or [],
                })
        return {
            "usda_fdc_ids_used": sorted(fdc_ids),
            "verification_methods": list(dict.fromkeys(verification_methods)),
            "ingredient_mapping_evidence": mapping_evidence,
            "approved_reference_defaults_used": approved_reference_defaults,
            "reference_default_assumptions": defaults,
            "missing_ingredient_quantities": missing_quantities,
            "unresolved_ingredient_mappings": unresolved_mappings,
            "missing_nutrients": list(calculation.get("missing_nutrient_information") or []),
            "serving_basis": {
                "source_servings": calculation.get("servings"),
                "per_serving_available": isinstance(calculation.get("per_serving"), dict),
                "per_serving_unavailable_reason": calculation.get("per_serving_unavailable_reason"),
                "quantity_alignment_status": calculation.get("quantity_alignment_status"),
            },
        }


__all__ = ["RecipeNutrientConstraintAdapter"]

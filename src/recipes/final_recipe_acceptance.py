"""Deterministic final acceptance policy for adapted recipes.

This layer consumes Step 5C nutritional evidence and Step 6A culinary evidence.
It performs no nutrient calculation, ingredient mapping, inference, or ranking.
"""

import math


FINAL_ACCEPTANCE_POLICY_VERSION = "final-recipe-acceptance-v1"
AUTHORIZED_MAPPING_STATUSES = {"VERIFIED_MAPPING", "EXACT_IDENTITY", "SLM_RESOLVED", "REFERENCE_DEFAULT"}
_NUTRIENTS = ("sodium_mg", "potassium_mg", "phosphorus_mg", "protein_g")
_NUTRITION_STATUS = {
    "PASS": "PASS", "FAIL": "FAIL", "REVIEW_REQUIRED": "REVIEW_REQUIRED",
    "UNKNOWN": "UNKNOWN", "UNAVAILABLE": "UNKNOWN", "NOT_APPLICABLE": "NOT_APPLICABLE",
}


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _unique(values):
    return list(dict.fromkeys(str(value) for value in values if value))


def _active_constraint_count(constraints, validation):
    if isinstance(constraints, dict) and constraints:
        count = sum(1 for value in constraints.values() if isinstance(value, dict) and value)
        if count:
            return count
    try:
        return max(0, int((validation or {}).get("active_constraint_count", 0)))
    except (TypeError, ValueError):
        return 0


class FinalRecipeAcceptancePolicy:
    """Aggregate existing evidence under the versioned deterministic policy."""

    def evaluate(self, nutritional_enrichment=None, culinary_validation=None, *,
                 constraints=None, adapted_recipe=None):
        nutrition = nutritional_enrichment if isinstance(nutritional_enrichment, dict) else {}
        culinary = culinary_validation if isinstance(culinary_validation, dict) else {}
        calculation = nutrition.get("nutritional_calculation")
        calculation = calculation if isinstance(calculation, dict) else {}
        validation = nutrition.get("constraint_validation")
        validation = validation if isinstance(validation, dict) else {}
        constraint_count = _active_constraint_count(constraints, validation)

        raw_nutrition = str(validation.get("constraint_status") or "UNKNOWN").upper()
        nutritional_status = (
            _NUTRITION_STATUS.get(raw_nutrition, "UNKNOWN")
            if constraint_count else "NOT_APPLICABLE"
        )
        culinary_status = str(culinary.get("status") or "UNAVAILABLE").upper()
        if culinary_status not in {"PASS", "FAIL", "REVIEW_REQUIRED"}:
            culinary_status = "UNAVAILABLE"

        mappings = self._mapping_completeness(calculation)
        quantities = self._quantity_completeness(calculation, adapted_recipe)
        valid_basis, basis_reason = self._valid_nutritional_basis(calculation)
        missing = []
        if constraint_count:
            if not validation:
                missing.append("NUTRITIONAL_CONSTRAINT_VALIDATION_MISSING")
            missing.extend(validation.get("missing_evidence") or [])
            if nutritional_status == "UNKNOWN":
                if not validation.get("missing_evidence"):
                    missing.append("NUTRITIONAL_VALIDATION_UNAVAILABLE_OR_UNKNOWN")
            if mappings["status"] != "COMPLETE":
                missing.extend(mappings["missing_evidence"] or ["AUTHORIZED_INGREDIENT_MAPPINGS_INCOMPLETE"])
            if quantities["status"] != "COMPLETE":
                missing.extend(quantities["missing_evidence"] or ["ESSENTIAL_QUANTITIES_UNRESOLVED"])
            if not valid_basis:
                missing.append(basis_reason or "VALID_NUTRITIONAL_BASIS_MISSING")
            if culinary_status == "UNAVAILABLE":
                missing.append("CULINARY_VALIDATION_UNAVAILABLE")
            missing.extend(culinary.get("missing_information") or [])
            if culinary_status == "PASS" and culinary.get("slm_output_valid") is not True:
                missing.append("CULINARY_STRUCTURED_OUTPUT_NOT_VERIFIED")
            for row in culinary.get("functional_coverage_assessments") or []:
                if not isinstance(row, dict) or row.get("importance") != "ESSENTIAL":
                    continue
                coverage = row.get("coverage")
                if coverage in {"PARTIALLY_COVERED", "UNKNOWN"}:
                    # The lower-level validator should already prevent PASS in
                    # this case; retain a conservative final guard as well.
                    pass
                elif coverage == "COVERED" and not (
                    row.get("supporting_ingredient_ids") or row.get("supporting_process_steps")
                ):
                    missing.append(f"CULINARY_FUNCTION_SUPPORT_EVIDENCE_MISSING:{row.get('function') or 'UNKNOWN'}")
            if nutrition.get("unresolved_changes"):
                missing.append("ADAPTED_CHANGE_DETECTION_UNRESOLVED")
            if nutrition.get("unresolved_ingredients"):
                missing.append("ADAPTED_NUTRITION_HAS_UNRESOLVED_INGREDIENTS")

        guard = culinary.get("deterministic_guards")
        guard = guard if isinstance(guard, dict) else {}
        findings = guard.get("findings") if isinstance(guard.get("findings"), list) else []
        confirmed_guard_failure = (
            guard.get("status") == "FAIL"
            or any(row.get("severity") == "CONFIRMED_VIOLATION" for row in findings if isinstance(row, dict))
        )
        rejection_reasons = []
        if nutritional_status == "FAIL":
            rejection_reasons.extend(
                item.get("reason") or f"NUTRITIONAL_CONSTRAINT_FAILED:{item.get('nutrient_key') or item.get('nutrient') or 'UNKNOWN'}"
                for item in validation.get("evaluations", [])
                if isinstance(item, dict) and item.get("status") == "FAIL"
            )
            if not rejection_reasons:
                rejection_reasons.append("NUTRITIONAL_CONSTRAINT_FAILED")
        if culinary_status == "FAIL":
            rejection_reasons.extend(culinary.get("review_reasons") or ["CULINARY_VALIDATION_FAILED"])
        if confirmed_guard_failure:
            rejection_reasons.extend(
                row.get("code") or "DETERMINISTIC_CULINARY_GUARD_FAILED"
                for row in findings if isinstance(row, dict)
                and row.get("severity") == "CONFIRMED_VIOLATION"
            )
            if guard.get("status") == "FAIL" and not findings:
                rejection_reasons.append("DETERMINISTIC_CULINARY_GUARD_FAILED")

        review_reasons = []
        if nutritional_status == "REVIEW_REQUIRED":
            review_reasons.extend(validation.get("missing_evidence") or [])
            review_reasons.extend(
                item.get("reason") or f"NUTRITIONAL_ASSUMPTION:{item.get('nutrient_key') or 'UNKNOWN'}"
                for item in validation.get("evaluations", [])
                if isinstance(item, dict) and item.get("status") == "REVIEW_REQUIRED"
            )
            if not review_reasons:
                review_reasons.append("NUTRITIONAL_VALIDATION_DEPENDS_ON_ASSUMPTIONS")
        if culinary_status == "REVIEW_REQUIRED":
            review_reasons.extend(culinary.get("review_reasons") or ["CULINARY_REVIEW_REQUIRED"])
        if culinary_status == "PASS":
            for row in culinary.get("functional_coverage_assessments") or []:
                if (isinstance(row, dict) and row.get("importance") == "ESSENTIAL"
                        and row.get("coverage") in {"PARTIALLY_COVERED", "UNKNOWN"}):
                    review_reasons.append(
                        f"ESSENTIAL_FUNCTION_COVERAGE_UNRESOLVED:{row.get('function') or 'UNKNOWN'}"
                    )
        if guard.get("status") == "INCOMPLETE":
            review_reasons.extend(["DETERMINISTIC_GUARDS_INCOMPLETE"])

        # A confirmed failure is decisive even when another evidence source is absent.
        if rejection_reasons:
            status = "REJECTED"
        # With no active dietary policy, acceptance is not applicable. A known
        # culinary failure above still remains a rejection.
        elif constraint_count == 0:
            status = "NOT_APPLICABLE"
        elif missing:
            status = "UNVERIFIED"
        elif review_reasons:
            status = "REVIEW_REQUIRED"
        elif (nutritional_status == "PASS" and culinary_status == "PASS"
              and mappings["status"] == "COMPLETE"
              and quantities["status"] == "COMPLETE" and valid_basis
              and not confirmed_guard_failure):
            status = "ACCEPTED"
        else:
            status = "UNVERIFIED"
            missing.append("ACCEPTANCE_EVIDENCE_INCOMPLETE")

        provenance = {
            "nutrient_calculation_version": calculation.get("version"),
            "constraint_validation": validation.get("validation_version") or "RecipeNutrientConstraintAdapter",
            "culinary_model_id": culinary.get("model_id"),
            "culinary_prompt_version": culinary.get("prompt_version"),
            "culinary_output_valid": culinary.get("slm_output_valid"),
            "mapping_verification": mappings["verification_provenance"],
        }
        return {
            "policy_version": FINAL_ACCEPTANCE_POLICY_VERSION,
            "status": status,
            "nutritional_status": nutritional_status,
            "culinary_status": culinary_status,
            "mapping_completeness": {key: value for key, value in mappings.items()
                                     if key != "verification_provenance"},
            "quantity_completeness": quantities,
            "nutritional_basis_valid": valid_basis,
            "active_constraint_count": constraint_count,
            "missing_evidence": _unique(missing),
            "rejection_reasons": _unique(rejection_reasons),
            "review_reasons": _unique(review_reasons),
            "verification_provenance": provenance,
        }

    @staticmethod
    def _mapping_completeness(calculation):
        ingredients = calculation.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            return {"status": "UNKNOWN", "required_count": None, "authorized_count": 0,
                    "missing_evidence": ["NUTRITIONAL_INGREDIENT_MAPPING_EVIDENCE_MISSING"],
                    "verification_provenance": []}
        authorized = 0
        missing = []
        provenance = []
        for index, item in enumerate(ingredients):
            mapping = item.get("mapping") if isinstance(item, dict) else None
            mapping = mapping if isinstance(mapping, dict) else {}
            fdc_id = mapping.get("fdc_id")
            valid_id = isinstance(fdc_id, int) and not isinstance(fdc_id, bool) and fdc_id > 0
            status = mapping.get("status")
            valid = status in AUTHORIZED_MAPPING_STATUSES and valid_id
            if status == "REFERENCE_DEFAULT" and mapping.get("reference_default") is not True:
                valid = False
            if mapping.get("unresolved_attributes") or mapping.get("nutritional_uncertainty") == "UNRELIABLE":
                valid = False
            if valid:
                authorized += 1
            else:
                missing.append(f"AUTHORIZED_MAPPING_MISSING:{index}")
            provenance.append({
                "position": item.get("position", index) if isinstance(item, dict) else index,
                "status": status,
                "fdc_id": fdc_id,
                "verification_method": mapping.get("verification_method"),
                "verification_provenance": mapping.get("verification_provenance"),
                "verification_policy_version": mapping.get("verification_policy_version"),
            })
        return {"status": "COMPLETE" if authorized == len(ingredients) else "INCOMPLETE",
                "required_count": len(ingredients), "authorized_count": authorized,
                "missing_evidence": missing, "verification_provenance": provenance}

    @staticmethod
    def _quantity_completeness(calculation, adapted_recipe):
        ingredients = calculation.get("ingredients")
        if not isinstance(ingredients, list) or not ingredients:
            return {"status": "UNKNOWN", "required_count": None, "resolved_count": 0,
                    "missing_evidence": ["NUTRITIONAL_QUANTITY_EVIDENCE_MISSING"]}
        expected_count = None
        if isinstance(adapted_recipe, dict):
            for key in ("ingredients", "raw_ingredient_items", "ingredient_items"):
                if isinstance(adapted_recipe.get(key), list):
                    expected_count = len(adapted_recipe[key])
                    break
        required_count = max(len(ingredients), expected_count or 0)
        resolved = 0
        missing = []
        if expected_count is not None and len(ingredients) != expected_count:
            missing.append("QUANTITY_CALCULATION_INGREDIENT_COUNT_MISMATCH")
        for index, item in enumerate(ingredients):
            amount = _number(item.get("quantity_g")) if isinstance(item, dict) else None
            if amount is not None and amount > 0:
                resolved += 1
            else:
                missing.append(f"ESSENTIAL_QUANTITY_UNRESOLVED:{index}")
        return {"status": "COMPLETE" if resolved == required_count and not missing else "INCOMPLETE",
                "required_count": required_count, "resolved_count": resolved,
                "missing_evidence": missing}

    @staticmethod
    def _valid_nutritional_basis(calculation):
        if calculation.get("calculation_status") != "COMPLETE":
            return False, f"NUTRIENT_CALCULATION_NOT_COMPLETE:{calculation.get('calculation_status') or 'UNKNOWN'}"
        servings = _number(calculation.get("servings"))
        if servings is None or servings <= 0:
            return False, "VALID_RECIPE_SERVING_BASIS_MISSING"
        values = calculation.get("per_serving")
        if not isinstance(values, dict) or any(_number(values.get(key)) is None for key in _NUTRIENTS):
            return False, "COMPLETE_PER_SERVING_NUTRIENT_BASIS_MISSING"
        return True, None


__all__ = ["FinalRecipeAcceptancePolicy", "FINAL_ACCEPTANCE_POLICY_VERSION"]

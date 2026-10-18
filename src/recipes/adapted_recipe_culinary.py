"""Informational culinary review for adapted recipes.

This module deliberately has no dependency on nutrient calculation, candidate
ranking, or graph writes. It combines conservative structured guards with a
bounded, optional Qwen assessment and reports uncertainty instead of filling
in missing recipe facts.
"""

import copy
import json
import logging
import math
import re
import threading
from datetime import datetime, timezone
from time import perf_counter

from src.recipes.recipe_change_detection import RecipeChangeDetector


CULINARY_PROMPT_VERSION = "adapted-recipe-culinary-v8"
_VALID_FUNCTIONAL = {"COMPATIBLE", "PARTIAL", "INCOMPATIBLE", "UNKNOWN"}
_VALID_QUANTITY = {"PLAUSIBLE", "IMPLAUSIBLE", "UNKNOWN"}
_VALID_COOKING = {"COMPATIBLE", "INCOMPATIBLE", "UNKNOWN"}
_VALID_COHERENCE = {"COHERENT", "INCOHERENT", "UNCERTAIN"}
_VALID_FUNCTIONS = {
    "PRIMARY_PROTEIN", "STARCH_BASE", "FLAVOR_BASE", "SALTINESS", "UMAMI",
    "AROMATICS", "ACIDITY", "SWEETNESS", "FAT", "LIQUID", "THICKENING",
    "BINDING", "LEAVENING", "TEXTURE", "STRUCTURAL_INTEGRITY", "OTHER",
}
_VALID_FUNCTION_IMPORTANCE = {"ESSENTIAL", "SUPPORTING", "OPTIONAL", "INTENTIONALLY_REDUCED"}
_VALID_FUNCTION_COVERAGE = {"COVERED", "PARTIALLY_COVERED", "NOT_COVERED", "UNKNOWN"}
_VALID_CHANGES = {
    "REPLACED", "ADDED", "REMOVED", "QUANTITY_CHANGED", "UNIT_CHANGED",
    "PREPARATION_CHANGED", "PROCESSING_CHANGED", "ATTRIBUTES_CHANGED",
}
_LOGGER = logging.getLogger("renally.culinary_validation")


def _recipe_items(recipe):
    if not isinstance(recipe, dict):
        return None
    for key in ("raw_ingredient_items", "ingredient_items", "ingredients"):
        value = recipe.get(key)
        if isinstance(value, list):
            return value
    names = recipe.get("raw_ingredients", recipe.get("raw_ingredient_names"))
    if isinstance(names, list):
        quantities = recipe.get("raw_ingredient_quantities", recipe.get("ingredient_quantities"))
        return [
            {"name": name, "quantity_raw": quantities[i] if isinstance(quantities, list) and len(quantities) == len(names) else None,
             "position": i}
            for i, name in enumerate(names)
        ]
    return None


def _description(item):
    if isinstance(item, str):
        return item.strip()
    if not isinstance(item, dict):
        return ""
    return str(item.get("name") or item.get("ingredient_name") or item.get("description") or "").strip()


def _identifier(item):
    if not isinstance(item, dict):
        return None
    value = next((item.get(key) for key in ("id", "ingredient_id", "food_id")
                  if item.get(key) is not None), None)
    return str(value) if value is not None else None


def _quantity(item):
    if not isinstance(item, dict):
        return {"amount": None, "unit": None, "raw": None}
    amount = next((item.get(key) for key in ("amount_g", "quantity_g", "amount", "quantity")
                   if item.get(key) is not None), None)
    unit = item.get("unit")
    if amount is not None and unit is None and any(item.get(key) is not None for key in ("amount_g", "quantity_g")):
        unit = "g"
    return {"amount": amount, "unit": unit, "raw": item.get("quantity_raw")}


def _method(recipe):
    if not isinstance(recipe, dict):
        return None
    for key in ("method", "instructions", "cooking_instructions", "directions"):
        value = recipe.get(key)
        if isinstance(value, str) and value.strip():
            return [value.strip()]
        if isinstance(value, list) and value:
            steps = [str(step).strip() for step in value if str(step).strip()]
            if steps:
                return steps
    return None


def _change_rows(structured_changes):
    if isinstance(structured_changes, dict):
        rows = structured_changes.get("changes")
    else:
        rows = structured_changes
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None


def _number(value):
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _norm(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


class AdaptedRecipeCulinaryValidationService:
    """Assess recipe-level culinary plausibility without changing adaptation."""

    def __init__(self, generator_provider=None, *, change_detector=None,
                 inference_timeout_seconds=105.0, max_input_characters=24000):
        self.generator_provider = generator_provider
        self.change_detector = change_detector or RecipeChangeDetector()
        self.inference_timeout_seconds = max(1.0, float(inference_timeout_seconds))
        self.max_input_characters = max(1000, int(max_input_characters))
        self._inference_gate = threading.Lock()

    def validate(self, original_recipe, adapted_recipe, structured_changes=None, *,
                 adaptation_id=None, substitution_metadata=None, ingredient_attributes=None):
        started = perf_counter()
        original = copy.deepcopy(original_recipe) if isinstance(original_recipe, dict) else {}
        adapted = copy.deepcopy(adapted_recipe) if isinstance(adapted_recipe, dict) else {}
        original_items = _recipe_items(original)
        adapted_items = _recipe_items(adapted)
        detected = self.change_detector.compare(original, adapted)
        supplied_changes = _change_rows(structured_changes)
        changes = supplied_changes if supplied_changes is not None else detected.get("changes", [])
        identified_changes, duplicate_change_ids = self._identified_changes(changes)

        missing = []
        if original_items is None:
            missing.append("ORIGINAL_INGREDIENT_LIST_MISSING")
        elif not original_items:
            missing.append("ORIGINAL_INGREDIENT_LIST_EMPTY")
        if adapted_items is None:
            missing.append("ADAPTED_INGREDIENT_LIST_MISSING")
        if not _method(original):
            missing.append("ORIGINAL_COOKING_INSTRUCTIONS_MISSING")
        if not _method(adapted):
            missing.append("ADAPTED_COOKING_INSTRUCTIONS_MISSING")
        if detected.get("unresolved_changes"):
            missing.extend("CHANGE_RELATIONSHIP_UNRESOLVED:" + str(row.get("reason") or "UNKNOWN")
                           for row in detected["unresolved_changes"])
        if not changes and detected.get("changes"):
            missing.append("STRUCTURED_CHANGE_RECORDS_MISSING")
        if not identified_changes:
            # An empty ingredient-change list cannot establish that adapted
            # instructions are equivalent. The current judgment schema has no
            # instruction-only change type, so this remains a human review.
            missing.append("NO_STRUCTURED_CHANGES_TO_EVALUATE")
        for label, items in (("ORIGINAL", original_items), ("ADAPTED", adapted_items)):
            if items is None:
                continue
            for index, item in enumerate(items):
                if not _description(item):
                    missing.append(f"{label}_INGREDIENT_IDENTITY_AMBIGUOUS:{index}")
                quantity = _quantity(item)
                if quantity["amount"] is None and not quantity["raw"]:
                    missing.append(f"{label}_INGREDIENT_QUANTITY_MISSING:{index}")
                elif quantity["amount"] is None and quantity["raw"]:
                    # A raw quantity is evidence for the model, but the app does
                    # not guess its unit or convert it to a numeric amount.
                    missing.append(f"{label}_INGREDIENT_QUANTITY_UNINTERPRETED:{index}")

        guard = self._deterministic_guards(original, adapted, original_items, adapted_items, changes)
        for change_id in duplicate_change_ids:
            guard["findings"].append({
                "severity": "CONFIRMED_VIOLATION", "code": "DUPLICATE_STRUCTURED_CHANGE_ID",
                "evidence": f"Structured change ID {change_id!r} is duplicated.",
            })
        if duplicate_change_ids:
            guard["status"] = "FAIL"
        evaluation_input = self._evaluation_input(
            original, adapted, original_items, adapted_items, identified_changes,
            substitution_metadata=substitution_metadata,
            ingredient_attributes=ingredient_attributes,
            missing_information=missing,
            required_function_assessments=self._required_function_assessments(
                original, original_items, identified_changes,
            ),
        )
        input_text = json.dumps(evaluation_input, ensure_ascii=False, default=str)
        if not identified_changes:
            slm_result, slm_latency = {
                "status": "NOT_APPLICABLE", "error": "NO_STRUCTURED_CHANGES_TO_EVALUATE",
            }, 0.0
        elif len(input_text) > self.max_input_characters:
            slm_result = {"status": "UNAVAILABLE", "error": "INPUT_SIZE_LIMIT_EXCEEDED"}
            slm_latency = 0.0
        else:
            slm_result, slm_latency = self._run_slm(evaluation_input)

        unavailable = isinstance(slm_result, dict) and slm_result.get("status") == "UNAVAILABLE"
        if identified_changes:
            adapted_recipe_input = evaluation_input.get("adapted_recipe", {})
            adapted_ingredient_refs = {
                row.get("ingredient_ref") for row in adapted_recipe_input.get("ingredients", [])
                if isinstance(row, dict) and row.get("ingredient_ref")
            }
            adapted_step_refs = {
                row.get("step_ref") for row in adapted_recipe_input.get("cooking_instructions", [])
                if isinstance(row, dict) and row.get("step_ref")
            }
            valid_slm, schema_errors = self._validate_slm_result(
                slm_result, identified_changes,
                adapted_ingredient_refs=adapted_ingredient_refs,
                adapted_step_refs=adapted_step_refs,
                required_functions_by_change={
                    row["change_id"]: row["functions"]
                    for row in evaluation_input.get("required_function_assessments", [])
                },
            )
        else:
            valid_slm, schema_errors = None, []
        truncated = bool((slm_result.get("inference_diagnostics") or {}).get("output_truncated")) \
            if isinstance(slm_result, dict) else False
        status, reasons = self._combine(
            guard, missing, valid_slm, schema_errors,
            unavailable=unavailable,
            unavailable_error=slm_result.get("error") if unavailable else None,
            truncated=truncated, no_changes=not identified_changes,
            adaptation_intent=evaluation_input.get("adaptation_intent"),
        )
        elapsed = (perf_counter() - started) * 1000.0
        generator, provider_error = self._generator_info()
        model_id = getattr(generator, "model_name", None) if generator is not None else None
        if model_id is None and isinstance(slm_result, dict):
            model_id = slm_result.get("model_id")
        normalized_evaluations = self._public_evaluations(valid_slm, identified_changes)
        normalized_coverage = self._public_functional_coverage(valid_slm, identified_changes)
        if not valid_slm:
            normalized_coverage = self._missing_function_placeholders(
                slm_result, identified_changes,
                evaluation_input.get("required_function_assessments", []),
            )
        model_coverage = []
        if isinstance(slm_result, dict):
            raw_evaluations = slm_result.get("evaluations", [])
            if isinstance(raw_evaluations, list):
                for row in raw_evaluations:
                    if not isinstance(row, dict) or not isinstance(row.get("functional_coverage"), list):
                        continue
                    for assessment in row["functional_coverage"]:
                        if isinstance(assessment, dict):
                            model_coverage.append({
                                **copy.deepcopy(assessment),
                                "source_change_ids": [row.get("change_id")],
                            })
        public_overall = self._public_overall_evaluation(
            status, reasons, normalized_evaluations, normalized_coverage, missing, model_id, slm_result,
        )
        dimension_results = {"ingredient_function": [], "functional_compatibility": [],
                             "functional_coverage": copy.deepcopy(normalized_coverage),
                             "quantity_appropriateness": [], "cooking_method_compatibility": [],
                             "recipe_coherence": None, "per_change_recipe_coherence": []}
        for evaluation in normalized_evaluations:
            dimension_results["functional_compatibility"].append({
                "change_id": evaluation.get("change_id"),
                "change_type": evaluation.get("change_type"),
                "status": evaluation.get("functional_compatibility"),
            })
            dimension_results["quantity_appropriateness"].append({
                "change_id": evaluation.get("change_id"),
                "change_type": evaluation.get("change_type"),
                "status": evaluation.get("quantity_appropriateness"),
            })
            dimension_results["cooking_method_compatibility"].append({
                "change_id": evaluation.get("change_id"),
                "change_type": evaluation.get("change_type"),
                "status": evaluation.get("cooking_compatibility"),
            })
            dimension_results["per_change_recipe_coherence"].append({
                "change_id": evaluation.get("change_id"),
                "change_type": evaluation.get("change_type"),
                "status": evaluation.get("recipe_coherence"),
            })
        dimension_results["ingredient_function"] = copy.deepcopy(normalized_coverage)
        coherences = {evaluation.get("model_recipe_coherence") for evaluation in normalized_evaluations}
        dimension_results["recipe_coherence"] = (
            "INCOHERENT" if "INCOHERENT" in coherences else
            "UNKNOWN" if "UNCERTAIN" in coherences else
            "COHERENT" if coherences else "UNAVAILABLE"
        )
        output = {
            "status": status,
            "requires_review": status in {"REVIEW_REQUIRED", "UNAVAILABLE"},
            "adaptation_id": adaptation_id,
            "original_recipe_id": original.get("id"),
            "adapted_recipe_id": adapted.get("id"),
            "structured_changes": copy.deepcopy(identified_changes),
            "python_required_function_assessments": copy.deepcopy(
                evaluation_input.get("required_function_assessments", [])),
            "deterministic_guards": guard,
            "slm_evaluation": public_overall,
            "slm_output_valid": bool(valid_slm),
            "slm_output_validation_error": ";".join(schema_errors) if schema_errors else None,
            "slm_output_schema_errors": list(schema_errors),
            # Retain the actual model judgments for manual diagnosis even when
            # strict schema validation rejects a partial response. They never
            # participate in status aggregation unless the whole schema passes.
            "model_reported_evaluations": copy.deepcopy(slm_result.get("evaluations", []))
                if isinstance(slm_result, dict) and isinstance(slm_result.get("evaluations"), list) else [],
            "model_reported_functional_coverage": model_coverage,
            "per_substitution_evaluations": copy.deepcopy(normalized_evaluations),
            "functional_coverage_assessments": copy.deepcopy(normalized_coverage),
            "per_dimension_results": dimension_results,
            "missing_information": list(dict.fromkeys(missing)),
            "review_reasons": reasons,
            "model_id": model_id,
            "prompt_version": CULINARY_PROMPT_VERSION,
            "evaluated_at": datetime.now(timezone.utc).isoformat(),
            "evaluation_duration_ms": elapsed,
            "slm_inference_duration_ms": slm_latency,
        }
        diagnostics = slm_result.get("inference_diagnostics") if isinstance(slm_result, dict) else None
        if isinstance(diagnostics, dict):
            diagnostics = copy.deepcopy(diagnostics)
            diagnostics["schema_valid"] = bool(valid_slm)
            diagnostics["model_schema_complete"] = bool(valid_slm)
            diagnostics["schema_errors"] = list(schema_errors)
            diagnostics["culinary_validation_status"] = status
            diagnostics["total_evaluation_ms"] = elapsed
            output["inference_diagnostics"] = diagnostics
        output["slm_evaluation"].update({
            "inference_diagnostics": copy.deepcopy(diagnostics) if isinstance(diagnostics, dict) else None,
            "schema_errors": list(schema_errors),
        })
        if provider_error and not model_id:
            output["model_unavailable_reason"] = provider_error
        self._log_evaluation(output, changes)
        return output

    @staticmethod
    def _identified_changes(changes):
        """Assign stable, unique IDs for the model's one-to-one response contract."""
        rows = [copy.deepcopy(row) for row in (changes or [])
                if str(row.get("change_type") or "").upper() != "UNCHANGED"]
        seen = set()
        duplicates = []
        for index, row in enumerate(rows, start=1):
            supplied = row.get("change_id")
            if isinstance(supplied, str) and supplied.strip() and supplied.strip() not in seen:
                change_id = supplied.strip()
            else:
                if isinstance(supplied, str) and supplied.strip() in seen:
                    duplicates.append(supplied.strip())
                change_id = f"change-{index}"
                while change_id in seen:
                    index += 1
                    change_id = f"change-{index}"
            row["change_id"] = change_id
            seen.add(change_id)
        return rows, list(dict.fromkeys(duplicates))

    @staticmethod
    def _required_function_assessments(original_recipe, original_items, changes):
        """Identify source functions from ingredient roles and recipe context."""
        if not isinstance(original_items, list):
            return []
        original_recipe = original_recipe if isinstance(original_recipe, dict) else {}
        context = _norm(" ".join([
            str(original_recipe.get("name") or ""),
            *(_method(original_recipe) or []),
        ]))
        baking_context = bool(re.search(
            r"\b(?:bake|baked|baking|loaf|muffin|cookie|cookies|cake|bread|batter|dough)\b", context,
        ))
        soup_sauce_context = bool(re.search(r"\b(?:soup|sauce|stew|gravy)\b", context))
        herb_names = re.compile(r"\b(?:basil|parsley|cilantro|coriander|oregano|thyme|dill|rosemary|mint|chives)\b", re.I)
        thickener_names = re.compile(r"\b(?:cornstarch|corn flour|arrowroot|tapioca starch|potato starch)\b", re.I)
        leavener_names = re.compile(r"\b(?:baking soda|baking powder|yeast)\b", re.I)
        original_by_position = {}
        for index, item in enumerate(original_items):
            row = item if isinstance(item, dict) else {"name": item}
            position = row.get("position", index)
            original_by_position[position] = row
        required = []
        for change in changes:
            if str(change.get("change_type") or "").upper() == "ADDED":
                continue
            source = original_by_position.get(change.get("original_position"), {})
            name = _description(source)
            attrs = source.get("attributes") or source.get("culinary_attributes") or {}
            if not isinstance(attrs, dict):
                attrs = {}
            role = source.get("role") or attrs.get("role") or attrs.get("culinary_role") or ""
            explicit_role = _norm(role).replace(" ", "_")
            concentrated_seasoning = bool(
                re.search(r"\bseasoning\s+(?:packet|mix|blend)\b", name, re.I)
                or re.search(r"\b(?:soup|noodle)\s+seasoning\b", name, re.I)
                or explicit_role in {"seasoning_mix", "seasoning_blend", "concentrated_seasoning",
                                     "soup_base", "noodle_base"}
            )
            functions = []
            if concentrated_seasoning:
                functions = ["FLAVOR_BASE", "SALTINESS", "UMAMI"]
            else:
                role_functions = {
                    "aromatic": ["AROMATICS"], "aromatics": ["AROMATICS"],
                    "binder": ["BINDING"], "binding": ["BINDING"],
                    "thickener": ["THICKENING"], "thickening": ["THICKENING"],
                    "leavener": ["LEAVENING", "STRUCTURAL_INTEGRITY"],
                    "leavening": ["LEAVENING", "STRUCTURAL_INTEGRITY"],
                    "liquid": ["LIQUID"], "fat": ["FAT"],
                    "starch_base": ["STARCH_BASE"], "primary_protein": ["PRIMARY_PROTEIN"],
                    "flavor_base": ["FLAVOR_BASE"], "saltiness": ["SALTINESS"],
                    "umami": ["UMAMI"], "texture": ["TEXTURE"],
                }
                functions.extend(role_functions.get(explicit_role, []))
                explicit_functions = attrs.get("culinary_functions") or attrs.get("functions") or []
                if isinstance(explicit_functions, str):
                    explicit_functions = [explicit_functions]
                if isinstance(explicit_functions, list):
                    functions.extend(
                        str(value).strip().upper().replace(" ", "_")
                        for value in explicit_functions
                        if str(value).strip().upper().replace(" ", "_") in _VALID_FUNCTIONS
                    )
                if herb_names.search(name):
                    functions.append("AROMATICS")
                if "egg" in _norm(name).split() and baking_context:
                    functions.extend(["BINDING", "STRUCTURAL_INTEGRITY"])
                if leavener_names.search(name) and baking_context:
                    functions.extend(["LEAVENING", "STRUCTURAL_INTEGRITY"])
                if thickener_names.search(name):
                    functions.append("THICKENING")
                normalized_name = _norm(name)
                name_tokens = set(normalized_name.split())
                if "salt" in name_tokens:
                    functions.append("SALTINESS")
                if name_tokens & {"oil", "butter", "cream", "ghee", "shortening"}:
                    functions.append("FAT")
                if name_tokens & {"cream", "milk"} and soup_sauce_context:
                    functions.extend(["LIQUID", "TEXTURE"])
                if name_tokens & {"broth", "stock"}:
                    functions.append("LIQUID")
                    if re.search(r"\b(?:cook|boil|simmer)\b.*\b(?:in|with)\b", context):
                        functions.append("FLAVOR_BASE")
            functions = list(dict.fromkeys(functions))
            if functions:
                required.append({
                    "change_id": change["change_id"],
                    "functions": functions,
                })
        return required

    def _generator_info(self):
        if not callable(self.generator_provider):
            return None, "SLM_GENERATOR_PROVIDER_NOT_CONFIGURED"
        try:
            value = self.generator_provider()
        except Exception as exc:
            return None, f"SLM_PROVIDER_FAILED_{type(exc).__name__}"
        if isinstance(value, tuple) and len(value) == 2:
            return value[0], value[1]
        return value, None

    def _run_slm(self, evaluation_input):
        generator, provider_error = self._generator_info()
        if generator is None:
            return {"status": "UNAVAILABLE", "error": provider_error or "SLM_UNAVAILABLE"}, 0.0
        method = getattr(generator, "evaluate_culinary_compatibility", None)
        if not callable(method):
            return {"status": "UNAVAILABLE", "error": "CULINARY_EVALUATOR_UNAVAILABLE"}, 0.0
        if not self._inference_gate.acquire(blocking=False):
            return {"status": "UNAVAILABLE", "error": "SLM_BUSY_AFTER_PRIOR_INFERENCE"}, 0.0
        started = perf_counter()
        finished = threading.Event()
        result_holder = {}

        def invoke():
            try:
                result_holder["result"] = method(evaluation_input)
            except Exception as exc:  # Model, tokenizer, and JSON errors are data here.
                result_holder["error"] = f"SLM_EVALUATION_FAILED_{type(exc).__name__}"
            finally:
                finished.set()
                self._inference_gate.release()

        # A daemon worker bounds request latency even if a local inference call
        # stalls. Qwen's shared generation lock continues to serialize use.
        worker = threading.Thread(target=invoke, name="renally-culinary-qwen", daemon=True)
        worker.start()
        if not finished.wait(self.inference_timeout_seconds):
            diagnostics = {}
            getter = getattr(generator, "get_last_inference_diagnostics", None)
            if callable(getter):
                try:
                    diagnostics = getter()
                except Exception:
                    diagnostics = {}
            if isinstance(diagnostics, dict):
                diagnostics = copy.deepcopy(diagnostics)
                diagnostics["timed_out"] = True
                diagnostics["timeout_phase"] = "outer_wait"
            result = {"status": "UNAVAILABLE", "error": "SLM_TIMEOUT"}
            if diagnostics:
                result["inference_diagnostics"] = diagnostics
            # Keep the inference gate held until the generation worker exits.
            # Python cannot safely kill a thread running a native model kernel.
            return result, (perf_counter() - started) * 1000.0
        elapsed = (perf_counter() - started) * 1000.0
        if "error" in result_holder:
            return {"status": "UNAVAILABLE", "error": result_holder["error"]}, elapsed
        result = result_holder.get("result")
        if not isinstance(result, dict):
            return {"status": "UNAVAILABLE", "error": "SLM_RETURNED_NON_OBJECT"}, elapsed
        return result, elapsed

    @staticmethod
    def _evaluation_input(original, adapted, original_items, adapted_items, changes,
                          *, substitution_metadata, ingredient_attributes, missing_information,
                          required_function_assessments=None):
        def rows(items, attributes, prefix):
            result = []
            for position, item in enumerate(items or []):
                item_dict = item if isinstance(item, dict) else {"name": item}
                identity = _identifier(item_dict)
                extra = (attributes or {}).get(identity, {}) if identity else {}
                result.append({
                    "position": item_dict.get("position", position),
                    # Stable, Python-assigned references let the model cite
                    # evidence without inventing catalog ingredient IDs.
                    "ingredient_ref": f"{prefix}-ingredient-{position + 1}",
                    "ingredient_id": identity,
                    "description": _description(item_dict) or None,
                    "quantity": _quantity(item_dict),
                    "attributes": copy.deepcopy(item_dict.get("attributes") or item_dict.get("culinary_attributes") or extra),
                    "food_state": item_dict.get("food_state"),
                    "preparation": item_dict.get("preparation") or item_dict.get("preparation_state"),
                    "processing": item_dict.get("processing") or item_dict.get("processing_state"),
                    "ingredient_type": item_dict.get("ingredient_type"),
                    "role": item_dict.get("role"),
                    "substitution_group": item_dict.get("substitution_group"),
                    "substitution_metadata": copy.deepcopy(item_dict.get("substitution_metadata") or {}),
                })
            return result

        original_rows = rows(original_items, ingredient_attributes, "original")
        adapted_rows = rows(adapted_items, ingredient_attributes, "adapted")
        original_ref_by_position = {row["position"]: row["ingredient_ref"] for row in original_rows}
        adapted_ref_by_position = {row["position"]: row["ingredient_ref"] for row in adapted_rows}
        identified_change_rows = []
        for change in changes:
            row = copy.deepcopy(change)
            row["original_ingredient_ref"] = original_ref_by_position.get(row.get("original_position"))
            row["adapted_ingredient_ref"] = adapted_ref_by_position.get(row.get("adapted_position"))
            identified_change_rows.append(row)

        def instruction_rows(recipe, prefix):
            return [
                {"step_ref": f"{prefix}-step-{index + 1}", "text": step}
                for index, step in enumerate(_method(recipe) or [])
            ]

        intent = None
        if isinstance(substitution_metadata, dict):
            intent = substitution_metadata.get("adaptation_intent")

        return {
            "original_recipe": {"id": original.get("id"), "name": original.get("name"),
                                "servings": original.get("servings", original.get("recipe_servings")),
                                "ingredients": original_rows,
                                "cooking_instructions": instruction_rows(original, "original")},
            "adapted_recipe": {"id": adapted.get("id"), "name": adapted.get("name"),
                               "servings": adapted.get("servings", adapted.get("recipe_servings")),
                               "ingredients": adapted_rows,
                               "cooking_instructions": instruction_rows(adapted, "adapted")},
            "structured_changes": identified_change_rows,
            "substitution_metadata": copy.deepcopy(substitution_metadata or {}),
            "adaptation_intent": copy.deepcopy(intent),
            "required_function_assessments": copy.deepcopy(required_function_assessments or []),
            "missing_information": list(dict.fromkeys(missing_information)),
        }

    @staticmethod
    def _deterministic_guards(original, adapted, original_items, adapted_items, changes):
        findings = []
        if adapted_items is not None and not adapted_items:
            findings.append({"severity": "CONFIRMED_VIOLATION", "code": "EMPTY_ADAPTED_INGREDIENT_LIST",
                             "evidence": "The adapted recipe contains no ingredients."})
        if adapted_items is None or original_items is None:
            return {"status": "INCOMPLETE", "findings": findings}

        final_by_position = {}
        for index, item in enumerate(adapted_items):
            item_dict = item if isinstance(item, dict) else {"name": item}
            position = item_dict.get("position", index)
            final_by_position[position] = item_dict
        old_by_position = {}
        for index, item in enumerate(original_items):
            item_dict = item if isinstance(item, dict) else {"name": item}
            old_by_position[item_dict.get("position", index)] = item_dict

        for change in changes or []:
            kind = str(change.get("change_type") or "").upper()
            adapted_position = change.get("adapted_position")
            original_position = change.get("original_position")
            if kind not in _VALID_CHANGES and kind != "UNCHANGED":
                findings.append({"severity": "CONFIRMED_VIOLATION", "code": "INVALID_STRUCTURED_CHANGE_TYPE",
                                 "evidence": f"Structured change type {kind or '(missing)'!r} is not supported."})
            if kind in {"REPLACED", "REMOVED", "QUANTITY_CHANGED", "UNIT_CHANGED",
                        "PREPARATION_CHANGED", "PROCESSING_CHANGED", "ATTRIBUTES_CHANGED"}:
                original_row = old_by_position.get(original_position)
                expected_original_id = change.get("original_ingredient_id")
                expected_original_name = change.get("original_ingredient")
                if (original_row is None
                        or (expected_original_id is not None and _identifier(original_row) != str(expected_original_id))
                        or (expected_original_name and _norm(_description(original_row)) != _norm(expected_original_name))):
                    findings.append({
                        "severity": "CONFIRMED_VIOLATION", "code": "CHANGE_RECORD_ORIGINAL_ITEM_MISSING_OR_CONFLICTING",
                        "evidence": f"{kind} record at original position {original_position!r} does not match the original ingredient list.",
                    })
            if kind == "ADDED" and (original_position is not None
                                      or change.get("original_ingredient_id") is not None
                                      or change.get("original_ingredient") is not None):
                findings.append({"severity": "CONFIRMED_VIOLATION", "code": "ADDITION_RECORD_HAS_ORIGINAL_ITEM",
                                 "evidence": "An ADDED change record also identifies an original ingredient."})
            if kind == "REMOVED" and (adapted_position is not None
                                        or change.get("adapted_ingredient_id") is not None
                                        or change.get("adapted_ingredient") is not None):
                findings.append({"severity": "CONFIRMED_VIOLATION", "code": "REMOVAL_RECORD_HAS_ADAPTED_ITEM",
                                 "evidence": "A REMOVED change record also identifies an adapted ingredient."})
            if kind in {"REPLACED", "ADDED"}:
                row = final_by_position.get(adapted_position)
                expected_id = change.get("adapted_ingredient_id")
                expected_name = change.get("adapted_ingredient")
                if row is None or (expected_id is not None and _identifier(row) != str(expected_id)) or (
                    expected_name and _norm(_description(row)) != _norm(expected_name)
                ):
                    findings.append({
                        "severity": "CONFIRMED_VIOLATION", "code": "CHANGE_RECORD_ADAPTED_ITEM_MISSING_OR_CONFLICTING",
                        "evidence": f"{kind} record at adapted position {adapted_position!r} does not match the final ingredient list.",
                    })
            elif kind in {"QUANTITY_CHANGED", "UNIT_CHANGED", "PREPARATION_CHANGED", "PROCESSING_CHANGED", "ATTRIBUTES_CHANGED"}:
                row = final_by_position.get(adapted_position)
                expected_id = change.get("adapted_ingredient_id")
                expected_name = change.get("adapted_ingredient")
                if row is None or (expected_id is not None and _identifier(row) != str(expected_id)) or (
                    expected_name and _norm(_description(row)) != _norm(expected_name)
                ):
                    findings.append({
                        "severity": "CONFIRMED_VIOLATION", "code": "CHANGE_RECORD_ADAPTED_ITEM_MISSING_OR_CONFLICTING",
                        "evidence": f"{kind} record at adapted position {adapted_position!r} does not match the final ingredient list.",
                    })
                else:
                    recorded_amount = _number(change.get("adapted_quantity_g"))
                    final_amount = _number(_quantity(row)["amount"])
                    if recorded_amount is not None and final_amount is not None and not math.isclose(
                        recorded_amount, final_amount, rel_tol=0.0, abs_tol=1e-9,
                    ):
                        findings.append({
                            "severity": "CONFIRMED_VIOLATION", "code": "CHANGE_RECORD_QUANTITY_CONTRADICTS_FINAL_LIST",
                            "evidence": f"{kind} record says {recorded_amount:g} g but the final item is {final_amount:g} g.",
                        })
            if kind == "REMOVED":
                old = old_by_position.get(original_position)
                old_id = change.get("original_ingredient_id") or _identifier(old or {})
                old_name = change.get("original_ingredient") or _description(old or {})
                if old_id and any(_identifier(item if isinstance(item, dict) else {"name": item}) == str(old_id)
                                  for item in adapted_items):
                    findings.append({"severity": "CONFIRMED_VIOLATION", "code": "REMOVAL_RECORD_CONTRADICTS_FINAL_LIST",
                                     "evidence": f"Removed ingredient ID {old_id} is still in the adapted ingredient list."})
                elif old_name and any(_norm(_description(item if isinstance(item, dict) else {"name": item})) == _norm(old_name)
                                      for item in adapted_items):
                    findings.append({"severity": "CONFIRMED_VIOLATION", "code": "REMOVAL_RECORD_CONTRADICTS_FINAL_LIST",
                                     "evidence": f"Removed ingredient {old_name!r} is still in the adapted ingredient list."})
            if kind in {"REMOVED", "REPLACED"}:
                old = old_by_position.get(original_position)
                old_name = change.get("original_ingredient") or _description(old or {})
                still_listed = old_name and any(
                    _norm(_description(item if isinstance(item, dict) else {"name": item})) == _norm(old_name)
                    for item in adapted_items
                )
                instructions = "\n".join(_method(adapted) or [])
                if (old_name and not still_listed and len(old_name) >= 3
                        and re.search(r"(?<!\w)" + re.escape(old_name) + r"(?!\w)", instructions, re.I)):
                    findings.append({"severity": "CONFIRMED_VIOLATION", "code": "REMOVED_INGREDIENT_STILL_IN_INSTRUCTIONS",
                                     "evidence": f"Adapted instructions still explicitly reference removed ingredient {old_name!r}."})

        # Also detect stale references when the change records themselves are
        # missing or unresolved. Exact identity absence is sufficient evidence
        # of removal; no replacement pairing is inferred here.
        instructions = "\n".join(_method(adapted) or [])
        for index, source in enumerate(original_items):
            old = source if isinstance(source, dict) else {"name": source}
            old_name, old_id = _description(old), _identifier(old)
            still_present = any(
                (_identifier(item if isinstance(item, dict) else {"name": item}) == old_id if old_id else False)
                or (_norm(_description(item if isinstance(item, dict) else {"name": item})) == _norm(old_name)
                    if old_name else False)
                for item in adapted_items
            )
            if (old_name and not still_present and len(old_name) >= 3
                    and re.search(r"(?<!\w)" + re.escape(old_name) + r"(?!\w)", instructions, re.I)
                    and not any(item.get("code") == "REMOVED_INGREDIENT_STILL_IN_INSTRUCTIONS"
                                and old_name in item.get("evidence", "") for item in findings)):
                findings.append({"severity": "CONFIRMED_VIOLATION", "code": "REMOVED_INGREDIENT_STILL_IN_INSTRUCTIONS",
                                 "evidence": f"Adapted instructions still explicitly reference absent original ingredient {old_name!r} (position {index})."})

        # Honor only explicit per-food quantity bounds. Typical amounts alone
        # are not hard limits and are intentionally ignored.
        for index, item in enumerate(adapted_items):
            row = item if isinstance(item, dict) else {"name": item}
            quantity = _number(_quantity(row)["amount"])
            if quantity is None:
                continue
            attrs = row.get("attributes") or row.get("culinary_attributes") or {}
            low = next((_number(value) for value in (
                row.get("minimum_amount_g"), row.get("min_amount_g"), attrs.get("minimum_amount_g"), attrs.get("min_amount_g")
            ) if _number(value) is not None), None)
            high = next((_number(value) for value in (
                row.get("maximum_amount_g"), row.get("max_amount_g"), attrs.get("maximum_amount_g"), attrs.get("max_amount_g")
            ) if _number(value) is not None), None)
            if low is not None and quantity < low:
                findings.append({"severity": "CONFIRMED_VIOLATION", "code": "EXPLICIT_MINIMUM_QUANTITY_VIOLATED",
                                 "evidence": f"Adapted item {index} is {quantity:g} g below its explicit {low:g} g minimum."})
            if high is not None and quantity > high:
                findings.append({"severity": "CONFIRMED_VIOLATION", "code": "EXPLICIT_MAXIMUM_QUANTITY_VIOLATED",
                                 "evidence": f"Adapted item {index} is {quantity:g} g above its explicit {high:g} g maximum."})
        return {"status": "FAIL" if any(row["severity"] == "CONFIRMED_VIOLATION" for row in findings)
                else "PASS" if not findings else "REVIEW_REQUIRED", "findings": findings}

    @staticmethod
    def _validate_slm_result(result, changes, *, adapted_ingredient_refs=None, adapted_step_refs=None,
                             required_functions_by_change=None):
        """Validate change judgments and recipe-level function evidence references."""
        if not isinstance(result, dict):
            return None, ["ROOT_NOT_OBJECT"]
        if result.get("status") == "UNAVAILABLE":
            return None, []
        result = copy.deepcopy(result)
        # The model nests coverage beside each parent change judgment. Python
        # binds rows to that change ID before performing strict validation.
        if "functional_coverage" not in result:
            flattened = []
            model_evaluations = result.get("evaluations")
            if isinstance(model_evaluations, list):
                for evaluation in model_evaluations:
                    if not isinstance(evaluation, dict):
                        continue
                    rows = evaluation.get("functional_coverage")
                    if not isinstance(rows, list):
                        continue
                    for row in rows:
                        if not isinstance(row, dict):
                            flattened.append(row)
                            continue
                        row = copy.deepcopy(row)
                        if "source_change_ids" in row:
                            row["_model_supplied_source_change_ids"] = row.pop("source_change_ids")
                        row["source_change_ids"] = [evaluation.get("change_id")]
                        flattened.append(row)
            result["functional_coverage"] = flattened
        allowed_root = {"evaluations", "functional_coverage", "model_id", "prompt_version",
                        "inference_diagnostics"}
        errors = [f"UNEXPECTED_ROOT_FIELD:{key}" for key in result if key not in allowed_root]
        evaluations = result.get("evaluations")
        if not isinstance(evaluations, list):
            return None, [*errors, "EVALUATIONS_MISSING_OR_NOT_ARRAY"]
        required = {
            "change_id", "functional_compatibility", "quantity_appropriateness",
            "cooking_compatibility", "recipe_coherence", "reason", "functional_coverage",
        }
        expected_ids = [row["change_id"] for row in changes]
        expected_by_id = {row["change_id"]: row for row in changes}
        seen = set()
        accepted = {}
        for index, evaluation in enumerate(evaluations):
            if not isinstance(evaluation, dict):
                errors.append(f"EVALUATION_{index}_NOT_OBJECT")
                continue
            missing_fields = required - evaluation.keys()
            errors.extend(f"EVALUATION_{index}_MISSING_FIELD:{key}" for key in sorted(missing_fields))
            extra_fields = evaluation.keys() - required
            errors.extend(f"EVALUATION_{index}_UNEXPECTED_FIELD:{key}" for key in sorted(extra_fields))
            change_id = evaluation.get("change_id")
            if not isinstance(change_id, str) or not change_id.strip():
                errors.append(f"EVALUATION_{index}_CHANGE_ID_INVALID")
                continue
            if change_id in seen:
                errors.append(f"DUPLICATE_CHANGE_ID:{change_id}")
                continue
            seen.add(change_id)
            if change_id not in expected_by_id:
                errors.append(f"UNKNOWN_CHANGE_ID:{change_id}")
                continue
            if not missing_fields and not extra_fields:
                accepted[change_id] = evaluation
            if evaluation.get("functional_compatibility") not in _VALID_FUNCTIONAL:
                errors.append(f"EVALUATION_{index}_FUNCTIONAL_COMPATIBILITY_INVALID")
            if evaluation.get("quantity_appropriateness") not in _VALID_QUANTITY:
                errors.append(f"EVALUATION_{index}_QUANTITY_APPROPRIATENESS_INVALID")
            if evaluation.get("cooking_compatibility") not in _VALID_COOKING:
                errors.append(f"EVALUATION_{index}_COOKING_COMPATIBILITY_INVALID")
            if evaluation.get("recipe_coherence") not in _VALID_COHERENCE:
                errors.append(f"EVALUATION_{index}_RECIPE_COHERENCE_INVALID")
            if not isinstance(evaluation.get("reason"), str) or not evaluation["reason"].strip():
                errors.append(f"EVALUATION_{index}_REASON_MISSING")
        errors.extend(f"MISSING_CHANGE_EVALUATION:{change_id}" for change_id in expected_ids if change_id not in seen)
        # For known source roles, require Python-selected function rows so
        # omissions cannot be interpreted as full coverage.
        coverage_rows = result.get("functional_coverage")
        if not isinstance(coverage_rows, list):
            errors.append("FUNCTIONAL_COVERAGE_MISSING_OR_NOT_ARRAY")
            coverage_rows = []
        allowed_source_changes = {
            row["change_id"] for row in changes
            if str(row.get("change_type") or "").upper() != "ADDED"
        }
        assessed_source_changes = set()
        seen_coverage = set()
        assessed_functions = set()
        accepted_coverage = []
        required_coverage_fields = {
            "function", "importance", "coverage", "source_change_ids",
            "supporting_ingredient_ids", "supporting_process_steps", "reason",
        }
        adapted_ingredient_refs = set(adapted_ingredient_refs or [])
        adapted_step_refs = set(adapted_step_refs or [])
        for index, assessment in enumerate(coverage_rows):
            if not isinstance(assessment, dict):
                errors.append(f"FUNCTION_COVERAGE_{index}_NOT_OBJECT")
                continue
            if "supporting_evidence_ids" in assessment:
                if ("supporting_ingredient_ids" in assessment
                        or "supporting_process_steps" in assessment):
                    errors.append(f"FUNCTION_COVERAGE_{index}_MULTIPLE_EVIDENCE_FIELDS")
                evidence_refs = assessment.pop("supporting_evidence_ids")
                if not isinstance(evidence_refs, list) or any(not isinstance(value, str) for value in evidence_refs):
                    errors.append(f"FUNCTION_COVERAGE_{index}_SUPPORTING_EVIDENCE_IDS_INVALID")
                    evidence_refs = []
                if len(evidence_refs) != len(set(evidence_refs)):
                    errors.append(f"FUNCTION_COVERAGE_{index}_DUPLICATE_SUPPORTING_EVIDENCE_ID")
                unknown_evidence = set(evidence_refs) - adapted_ingredient_refs - adapted_step_refs
                if unknown_evidence:
                    errors.append(
                        f"FUNCTION_COVERAGE_{index}_UNKNOWN_SUPPORTING_EVIDENCE_ID:{','.join(sorted(unknown_evidence))}"
                    )
                assessment["supporting_ingredient_ids"] = [
                    value for value in evidence_refs if value in adapted_ingredient_refs
                ]
                assessment["supporting_process_steps"] = [
                    value for value in evidence_refs if value in adapted_step_refs
                ]
            missing_fields = required_coverage_fields - assessment.keys()
            extra_fields = assessment.keys() - required_coverage_fields
            errors.extend(f"FUNCTION_COVERAGE_{index}_MISSING_FIELD:{key}" for key in sorted(missing_fields))
            errors.extend(f"FUNCTION_COVERAGE_{index}_UNEXPECTED_FIELD:{key}" for key in sorted(extra_fields))
            if "_model_supplied_source_change_ids" in assessment:
                errors.append(f"FUNCTION_COVERAGE_{index}_UNEXPECTED_FIELD:source_change_ids")
            function = assessment.get("function")
            importance = assessment.get("importance")
            coverage = assessment.get("coverage")
            if not isinstance(function, str) or function not in _VALID_FUNCTIONS:
                errors.append(f"FUNCTION_COVERAGE_{index}_FUNCTION_INVALID")
            if not isinstance(importance, str) or importance not in _VALID_FUNCTION_IMPORTANCE:
                errors.append(f"FUNCTION_COVERAGE_{index}_IMPORTANCE_INVALID")
            if not isinstance(coverage, str) or coverage not in _VALID_FUNCTION_COVERAGE:
                errors.append(f"FUNCTION_COVERAGE_{index}_COVERAGE_INVALID")
            source_ids = assessment.get("source_change_ids")
            if (not isinstance(source_ids, list) or not source_ids
                    or any(not isinstance(value, str) or not value for value in source_ids)):
                errors.append(f"FUNCTION_COVERAGE_{index}_SOURCE_CHANGE_IDS_INVALID")
                source_ids = []
            if len(source_ids) != len(set(source_ids)):
                errors.append(f"FUNCTION_COVERAGE_{index}_DUPLICATE_SOURCE_CHANGE_ID")
            unknown_sources = set(source_ids) - allowed_source_changes
            if unknown_sources:
                errors.append(f"FUNCTION_COVERAGE_{index}_UNKNOWN_SOURCE_CHANGE_ID:{','.join(sorted(unknown_sources))}")
            assessed_source_changes.update(set(source_ids) & allowed_source_changes)
            if isinstance(function, str):
                for source_id in set(source_ids) & allowed_source_changes:
                    assessed_functions.add((source_id, function))
            ingredient_refs = assessment.get("supporting_ingredient_ids")
            if not isinstance(ingredient_refs, list) or any(not isinstance(value, str) for value in ingredient_refs):
                errors.append(f"FUNCTION_COVERAGE_{index}_SUPPORTING_INGREDIENT_IDS_INVALID")
                ingredient_refs = []
            if len(ingredient_refs) != len(set(ingredient_refs)):
                errors.append(f"FUNCTION_COVERAGE_{index}_DUPLICATE_SUPPORTING_INGREDIENT_ID")
            unknown_ingredients = set(ingredient_refs) - adapted_ingredient_refs
            if unknown_ingredients:
                errors.append(
                    f"FUNCTION_COVERAGE_{index}_UNKNOWN_SUPPORTING_INGREDIENT_ID:{','.join(sorted(unknown_ingredients))}"
                )
            step_refs = assessment.get("supporting_process_steps")
            if not isinstance(step_refs, list) or any(not isinstance(value, str) for value in step_refs):
                errors.append(f"FUNCTION_COVERAGE_{index}_SUPPORTING_PROCESS_STEPS_INVALID")
                step_refs = []
            if len(step_refs) != len(set(step_refs)):
                errors.append(f"FUNCTION_COVERAGE_{index}_DUPLICATE_SUPPORTING_PROCESS_STEP")
            unknown_steps = set(step_refs) - adapted_step_refs
            if unknown_steps:
                errors.append(f"FUNCTION_COVERAGE_{index}_UNKNOWN_SUPPORTING_PROCESS_STEP:{','.join(sorted(unknown_steps))}")
            if coverage == "COVERED" and not ingredient_refs and not step_refs:
                errors.append(f"FUNCTION_COVERAGE_{index}_COVERED_WITHOUT_EVIDENCE")
            reason = assessment.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"FUNCTION_COVERAGE_{index}_REASON_MISSING")
            key = (function if isinstance(function, str) else repr(function), tuple(sorted(source_ids)))
            if key in seen_coverage:
                errors.append(f"DUPLICATE_FUNCTION_COVERAGE:{function}:{','.join(sorted(source_ids))}")
            seen_coverage.add(key)
            if not missing_fields and not extra_fields:
                accepted_coverage.append(assessment)
        for change_id in sorted(allowed_source_changes - assessed_source_changes):
            errors.append(f"MISSING_FUNCTIONAL_COVERAGE_FOR_CHANGE:{change_id}")
        for change_id, functions in (required_functions_by_change or {}).items():
            for function in functions:
                if (change_id, function) not in assessed_functions:
                    errors.append(f"MISSING_REQUIRED_FUNCTION_ASSESSMENT:{change_id}:{function}")
        if errors:
            return None, list(dict.fromkeys(errors))
        return {
            "evaluations": [accepted[change_id] for change_id in expected_ids],
            "functional_coverage": accepted_coverage,
        }, []

    @staticmethod
    def _public_evaluations(valid_slm, changes):
        if not valid_slm:
            return []
        change_by_id = {row["change_id"]: row for row in changes}
        public = []
        for item in valid_slm["evaluations"]:
            change = change_by_id[item["change_id"]]
            functional = item["functional_compatibility"]
            coherence = item["recipe_coherence"]
            public.append({
                "change_id": item["change_id"],
                "change_type": str(change.get("change_type") or "").upper(),
                "original_ingredient": change.get("original_ingredient"),
                "adapted_ingredient": change.get("adapted_ingredient"),
                "functional_compatibility": "COMPLETE" if functional == "COMPATIBLE" else functional,
                "quantity_appropriateness": item["quantity_appropriateness"],
                "cooking_compatibility": item["cooking_compatibility"],
                "recipe_coherence": "UNKNOWN" if coherence == "UNCERTAIN" else coherence,
                "reason": item["reason"].strip(),
                "model_functional_compatibility": functional,
                "model_recipe_coherence": coherence,
            })
        return public

    @staticmethod
    def _public_functional_coverage(valid_slm, changes):
        if not valid_slm:
            return []
        change_by_id = {row["change_id"]: row for row in changes}
        public = []
        for index, row in enumerate(valid_slm.get("functional_coverage", []), start=1):
            public.append({
                "function_id": f"function-{index}",
                "function": row["function"],
                "importance": row["importance"],
                "coverage": row["coverage"],
                "source_change_ids": list(row["source_change_ids"]),
                "source_ingredients": [
                    change_by_id[change_id].get("original_ingredient")
                    for change_id in row["source_change_ids"] if change_id in change_by_id
                ],
                "supporting_ingredient_ids": list(row["supporting_ingredient_ids"]),
                "supporting_process_steps": list(row["supporting_process_steps"]),
                "reason": row["reason"].strip(),
                "evidence_references_validated": True,
            })
        return public

    @staticmethod
    def _missing_function_placeholders(slm_result, changes, required_assessments):
        """Expose unreturned source functions as UNKNOWN for review metadata."""
        evaluations = slm_result.get("evaluations", []) if isinstance(slm_result, dict) else []
        model_functions = {}
        if isinstance(evaluations, list):
            for evaluation in evaluations:
                if not isinstance(evaluation, dict) or not isinstance(evaluation.get("change_id"), str):
                    continue
                rows = evaluation.get("functional_coverage")
                if isinstance(rows, list):
                    model_functions[evaluation["change_id"]] = {
                        row.get("function") for row in rows if isinstance(row, dict)
                    }
        required_by_change = {
            row.get("change_id"): row.get("functions", [])
            for row in required_assessments if isinstance(row, dict)
        }
        placeholders = []
        for change in changes:
            if str(change.get("change_type") or "").upper() == "ADDED":
                continue
            change_id = change["change_id"]
            expected = required_by_change.get(change_id) or ["OTHER"]
            for function in expected:
                if function in model_functions.get(change_id, set()):
                    continue
                placeholders.append({
                    "function_id": f"function-{len(placeholders) + 1}",
                    "function": function,
                    "importance": None,
                    "coverage": "UNKNOWN",
                    "source_change_ids": [change_id],
                    "source_ingredients": [change.get("original_ingredient")],
                    "supporting_ingredient_ids": [],
                    "supporting_process_steps": [],
                    "reason": "The model did not return this function assessment.",
                    "evidence_references_validated": False,
                    "assessment_source": "PYTHON_MISSING_MODEL_OUTPUT",
                })
        return placeholders

    @staticmethod
    def _public_overall_evaluation(status, reasons, evaluations, functional_coverage, missing,
                                   model_id, raw_result):
        coherence_values = {row["model_recipe_coherence"] for row in evaluations}
        coherence = (
            "INCOHERENT" if "INCOHERENT" in coherence_values else
            "UNKNOWN" if "UNCERTAIN" in coherence_values else
            "COHERENT" if coherence_values else "UNKNOWN"
        )
        diagnostics = raw_result.get("inference_diagnostics") if isinstance(raw_result, dict) else None
        return {
            "status": status,
            "overall_reason": "; ".join(reasons) if reasons else "All required culinary checks are supported.",
            "recipe_coherence": coherence,
            "issues": list(reasons),
            "missing_information": list(dict.fromkeys(missing)),
            "evaluations": copy.deepcopy(evaluations),
            "functional_coverage": copy.deepcopy(functional_coverage),
            "requires_review": status in {"REVIEW_REQUIRED", "UNAVAILABLE"},
            "model_id": model_id,
            "prompt_version": CULINARY_PROMPT_VERSION,
            "error": raw_result.get("error") if isinstance(raw_result, dict) else None,
            "inference_diagnostics": copy.deepcopy(diagnostics) if isinstance(diagnostics, dict) else None,
        }

    @staticmethod
    def _combine(guard, missing, valid_slm, schema_errors, *, unavailable, unavailable_error,
                 truncated, no_changes, adaptation_intent=None):
        confirmed = [finding for finding in guard.get("findings", [])
                     if finding.get("severity") == "CONFIRMED_VIOLATION"]
        if confirmed:
            return "FAIL", list(dict.fromkeys(finding["code"] for finding in confirmed))
        if unavailable:
            return "UNAVAILABLE", [str(unavailable_error or "SLM_UNAVAILABLE")]
        if no_changes:
            return "REVIEW_REQUIRED", list(dict.fromkeys(missing or ["NO_STRUCTURED_CHANGES_TO_EVALUATE"]))
        if schema_errors:
            return "REVIEW_REQUIRED", ["MODEL_SCHEMA_INVALID", *schema_errors]
        if truncated:
            return "REVIEW_REQUIRED", ["MODEL_OUTPUT_TRUNCATED"]

        if missing:
            return "REVIEW_REQUIRED", list(dict.fromkeys(missing))
        if guard.get("status") != "PASS":
            return "REVIEW_REQUIRED", [finding.get("code", "DETERMINISTIC_GUARD_UNRESOLVED")
                                       for finding in guard.get("findings", [])]

        evaluations = (valid_slm or {}).get("evaluations", [])
        coverage_rows = (valid_slm or {}).get("functional_coverage", [])
        unresolved = []
        coverage_by_change = {}
        for row in coverage_rows:
            for change_id in row["source_change_ids"]:
                coverage_by_change.setdefault(change_id, []).append(row)

        # A broad flavor-base claim cannot rest only on exactly the same
        # evidence already cited for a narrower contribution such as umami.
        # This is a consistency guard, not an ingredient-pair rule: distinct
        # complementary evidence (for example broth plus aromatics) remains
        # eligible for a complete model judgment.
        for change_id, rows in coverage_by_change.items():
            narrow_rows = [row for row in rows if row["function"] in {
                "UMAMI", "SALTINESS", "AROMATICS", "ACIDITY", "SWEETNESS",
            } and row["coverage"] == "COVERED"]
            narrow_evidence = {
                ref for row in narrow_rows
                for ref in row["supporting_ingredient_ids"] + row["supporting_process_steps"]
            }
            for row in rows:
                broad_evidence = set(row["supporting_ingredient_ids"] + row["supporting_process_steps"])
                if (row["function"] == "FLAVOR_BASE" and row["importance"] == "ESSENTIAL"
                        and row["coverage"] == "COVERED" and narrow_evidence
                        and broad_evidence and broad_evidence <= narrow_evidence):
                    unresolved.append(
                        f"ESSENTIAL_FLAVOR_BASE_EVIDENCE_NOT_DISTINCT_FROM_COMPONENT:{change_id}"
                    )

        # A whole-recipe coverage assertion and a broad per-change judgment can
        # conflict. Preserve that uncertainty instead of choosing the optimistic
        # one. Other changes may still compensate for a particular ingredient.
        contradictory = []
        for evaluation in evaluations:
            change_id = evaluation["change_id"]
            if (evaluation["functional_compatibility"] == "COMPATIBLE"
                    and any(row["importance"] == "ESSENTIAL" and row["coverage"] == "NOT_COVERED"
                            for row in coverage_by_change.get(change_id, []))):
                contradictory.append(change_id)
        coherence_values = {row["recipe_coherence"] for row in evaluations}
        if len(coherence_values) > 1 and "INCOHERENT" in coherence_values and "COHERENT" in coherence_values:
            contradictory.append("RECIPE_COHERENCE")
        if contradictory:
            return "REVIEW_REQUIRED", [
                f"CONTRADICTORY_CULINARY_JUDGMENTS:{value}" for value in dict.fromkeys(contradictory)
            ]

        if any(row["functional_compatibility"] in {"PARTIAL", "UNKNOWN"}
               or row["quantity_appropriateness"] in {"IMPLAUSIBLE", "UNKNOWN"}
               or row["cooking_compatibility"] in {"INCOMPATIBLE", "UNKNOWN"}
               or row["recipe_coherence"] == "UNCERTAIN" for row in evaluations):
            unresolved.extend(
                f"CULINARY_JUDGMENT_UNRESOLVED:{row['change_id']}" for row in evaluations
                if row["functional_compatibility"] in {"PARTIAL", "UNKNOWN"}
                or row["quantity_appropriateness"] in {"IMPLAUSIBLE", "UNKNOWN"}
                or row["cooking_compatibility"] in {"INCOMPATIBLE", "UNKNOWN"}
                or row["recipe_coherence"] == "UNCERTAIN"
            )
        essential_rows = [row for row in coverage_rows if row["importance"] == "ESSENTIAL"]
        unresolved.extend(
            f"ESSENTIAL_FUNCTION_COVERAGE_UNRESOLVED:{row['function']}"
            for row in essential_rows if row["coverage"] in {"PARTIALLY_COVERED", "UNKNOWN"}
        )
        intentional_rows = [row for row in coverage_rows
                            if row["importance"] == "INTENTIONALLY_REDUCED"
                            and row["coverage"] != "COVERED"]
        if intentional_rows and not adaptation_intent:
            unresolved.extend(
                f"INTENTIONAL_REDUCTION_JUSTIFICATION_MISSING:{row['function']}"
                for row in intentional_rows
            )
        if unresolved:
            return "REVIEW_REQUIRED", list(dict.fromkeys(unresolved))

        missing_essential = [row for row in essential_rows if row["coverage"] == "NOT_COVERED"]
        coherent_incompatibility = any(row["recipe_coherence"] == "INCOHERENT" for row in evaluations)
        if missing_essential:
            if coherent_incompatibility:
                return "FAIL", [f"ESSENTIAL_FUNCTION_NOT_COVERED:{row['function']}"
                                 for row in missing_essential]
            return "REVIEW_REQUIRED", [f"ESSENTIAL_FUNCTION_NOT_COVERED:{row['function']}"
                                        for row in missing_essential]

        if any(row["recipe_coherence"] == "INCOHERENT" for row in evaluations):
            return "REVIEW_REQUIRED", ["RECIPE_COHERENCE_REQUIRES_REVIEW"]
        if any(row["functional_compatibility"] == "INCOMPATIBLE"
               for row in evaluations):
            return "REVIEW_REQUIRED", [
                f"CHANGE_COMPATIBILITY_REQUIRES_REVIEW:{row['change_id']}"
                for row in evaluations if row["functional_compatibility"] == "INCOMPATIBLE"
            ]
        return "PASS", []

    @staticmethod
    def _log_evaluation(output, changes):
        # Log only structured, non-conversational evaluation data. No prompts or
        # user transcript are persisted, and this service performs no DB writes.
        record = {
            "event": "adapted_recipe_culinary_validation",
            "original_recipe_id": output.get("original_recipe_id"),
            "adaptation_id": output.get("adaptation_id"),
            "structured_changes": changes,
            "status": output.get("status"),
            "per_dimension_results": output.get("per_dimension_results"),
            "deterministic_guard_findings": output.get("deterministic_guards", {}).get("findings"),
            "slm_evaluation": {
                key: value for key, value in (output.get("slm_evaluation") or {}).items()
                if key not in {"_raw_output", "raw_output"}
            },
            "missing_information": output.get("missing_information"),
            "model_id": output.get("model_id"),
            "prompt_version": output.get("prompt_version"),
            "evaluation_duration_ms": output.get("evaluation_duration_ms"),
            "slm_inference_duration_ms": output.get("slm_inference_duration_ms"),
            "inference_diagnostics": output.get("inference_diagnostics"),
        }
        _LOGGER.info("%s", json.dumps(record, ensure_ascii=False, default=str))


__all__ = ["AdaptedRecipeCulinaryValidationService", "CULINARY_PROMPT_VERSION"]

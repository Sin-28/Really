"""Validate local Qwen disambiguation against retrieved USDA candidates."""

import json
import os
import queue
import re
import threading
from datetime import datetime, timezone

from src.recipes.ingredient_context import variant_gaps


PROMPT_VERSION = "usda-ingredient-validation-v2"
DEFAULT_MODEL_ID = "Qwen/Qwen3-1.7B"
_ALLOWED_STATUSES = {"RECOMMENDED", "AMBIGUOUS", "REJECTED", "REVIEW_REQUIRED"}
_UNKNOWN_VALUES = {"", "unknown", "unspecified", "not specified", "not stated", "none", "null"}

_COOKED_STATES = {"cooked", "boiled", "grilled", "baked", "fried", "roasted", "steamed", "poached"}
_RAW_STATES = {"raw", "uncooked"}
_PROCESSED_STATES = {"smoked", "dried", "canned", "frozen", "fermented", "pickled", "cured"}
_FOOD_STATES = _COOKED_STATES | _RAW_STATES | _PROCESSED_STATES | {"fresh"}
_SALMON_SPECIES = {"atlantic", "sockeye", "pink", "chinook", "coho", "chum", "steelhead"}
_OIL_TYPES = {"olive", "vegetable", "canola", "rapeseed", "sunflower", "soybean", "soy", "corn", "peanut", "sesame", "coconut", "avocado", "safflower", "grapeseed"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _words(value):
    return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _id_key(value):
    if isinstance(value, bool) or value is None or not isinstance(value, (int, str)):
        return None
    text = str(value).strip()
    return text if text.isdecimal() and int(text) > 0 else None


def _decode_json_object(raw):
    decoder = json.JSONDecoder()
    for index, char in enumerate(raw):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(raw[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise ValueError("SLM response did not contain a JSON object")


def _json_safe(value):
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return str(value)[:200]


def _ingredient_parts(parsed_ingredient):
    if not isinstance(parsed_ingredient, dict):
        return {}, {}
    nested = parsed_ingredient.get("parsed")
    parsed = nested if isinstance(nested, dict) else parsed_ingredient
    return parsed, parsed_ingredient if parsed is not parsed_ingredient else {}


def _explicit_text(parsed, outer, mapping_result):
    parts = [
        parsed.get("ingredient_name"), parsed.get("normalized_name"),
        parsed.get("original_text"), outer.get("name"),
        mapping_result.get("ingredient"), mapping_result.get("normalized_name"),
    ]
    attributes = mapping_result.get("attributes")
    if not isinstance(attributes, dict):
        attributes = parsed.get("attributes") if isinstance(parsed.get("attributes"), dict) else {}
    for key, value in attributes.items():
        if str(value).casefold() in _UNKNOWN_VALUES:
            continue
        parts.extend((key, value if isinstance(value, str) else " ".join(map(str, value))
                      if isinstance(value, (list, tuple, set)) else value))
    return " ".join(str(part) for part in parts if part is not None)


def _states(text):
    tokens = _words(text)
    if "uncooked" in tokens:
        tokens.add("raw")
    return tokens & _FOOD_STATES


def _salmon_species(text):
    tokens = _words(text)
    if "salmon" not in tokens:
        return set()
    # Steelhead is commonly named without the word salmon in USDA descriptions.
    return tokens & _SALMON_SPECIES


def _oil_type(text):
    tokens = _words(text)
    if "oil" not in tokens:
        return None
    matches = tokens & _OIL_TYPES
    return sorted(matches)[0] if len(matches) == 1 else None


def _candidate_description(candidate):
    return " ".join(str(candidate.get(field) or "") for field in ("description", "food_state"))


def _explicit_mismatches(source_text, candidate_text):
    """Return deterministic incompatibilities and unsupported specificity."""
    requested_states = _states(source_text)
    candidate_states = _states(candidate_text)
    reasons = []
    unresolved = set()
    state_mismatch = False

    if requested_states and candidate_states:
        if requested_states & _RAW_STATES and candidate_states & (_COOKED_STATES | _PROCESSED_STATES):
            state_mismatch = True
        if requested_states & _COOKED_STATES and candidate_states & _RAW_STATES:
            state_mismatch = True
        if requested_states & _PROCESSED_STATES:
            requested_processing = requested_states & _PROCESSED_STATES
            candidate_processing = candidate_states & _PROCESSED_STATES
            if candidate_states & _RAW_STATES or (candidate_processing and not requested_processing.intersection(candidate_processing)):
                state_mismatch = True
            elif not candidate_processing:
                state_mismatch = True
        if "fresh" in requested_states and candidate_states & _PROCESSED_STATES:
            state_mismatch = True
        requested_methods = requested_states & (_COOKED_STATES - {"cooked"})
        candidate_methods = candidate_states & (_COOKED_STATES - {"cooked"})
        if requested_methods and candidate_methods and not requested_methods.intersection(candidate_methods):
            state_mismatch = True
        if state_mismatch:
            reasons.append("Explicit raw/cooked/preparation/processing state conflicts with the USDA candidate.")
    elif requested_states and not candidate_states:
        unresolved.add("food_state")
        reasons.append("The candidate description does not establish the explicitly requested food state.")
    elif not requested_states and candidate_states:
        unresolved.add("cooking_state")
        reasons.append("The source ingredient does not specify the candidate's listed food state.")

    requested_species = _salmon_species(source_text)
    candidate_species = _salmon_species(candidate_text)
    species_mismatch = bool(requested_species and candidate_species and not requested_species.intersection(candidate_species))
    if species_mismatch:
        reasons.append("Explicit salmon species conflicts with the USDA candidate.")
    elif requested_species and not candidate_species:
        unresolved.add("species")
        reasons.append("The candidate description does not establish the explicitly requested salmon species.")
    elif not requested_species and candidate_species:
        unresolved.add("species")
        reasons.append("The source ingredient does not specify the candidate's listed salmon species.")

    requested_oil = _oil_type(source_text)
    candidate_oil = _oil_type(candidate_text)
    if requested_oil and candidate_oil and requested_oil != candidate_oil:
        reasons.append("Explicit oil variety conflicts with the USDA candidate.")
    elif requested_oil and not candidate_oil:
        unresolved.add("oil_type")
        reasons.append("The candidate description does not establish the explicitly requested oil type.")
    elif not requested_oil and candidate_oil:
        unresolved.add("oil_type")
        reasons.append("The source ingredient does not specify the candidate's listed oil type.")

    incompatible = state_mismatch or species_mismatch or bool(requested_oil and candidate_oil and requested_oil != candidate_oil)
    return incompatible, sorted(unresolved), reasons


class _LazyQwenEvaluator:
    """Adapter that loads the existing local Qwen implementation on first use."""

    def __init__(self, model_id):
        self.model_id = model_id
        self._generator = None

    def evaluate_ingredient_candidates(self, payload):
        if self._generator is None:
            from src.generation.qwen_generator import QwenRecipeGenerator
            self._generator = QwenRecipeGenerator(model_name=self.model_id)
        return self._generator.evaluate_ingredient_candidates(payload)


class SharedQwenEvaluator:
    """Use the application's already managed lazy Qwen instance."""

    def __init__(self, provider):
        self.provider = provider

    def evaluate_ingredient_candidates(self, payload):
        generator, error = self.provider()
        if generator is None:
            raise RuntimeError(error or "Local Qwen is unavailable")
        return generator.evaluate_ingredient_candidates(payload)


class SLMIngredientMappingValidator:
    """Validate a Step 2 candidate result without changing verification state.

    ``evaluator`` may be a callable accepting the structured payload or an
    object with ``evaluate_ingredient_candidates(payload)``. The default uses
    RenAlly's existing optional Qwen model and loads it only on first use.
    """

    def __init__(self, kg=None, evaluator=None, *, model_id=None, timeout_seconds=45):
        self.kg = kg
        self.evaluator = evaluator
        self.model_id = (model_id or os.environ.get("RENALLY_QWEN_MODEL") or DEFAULT_MODEL_ID)
        self.timeout_seconds = float(timeout_seconds)
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._inference_lock = threading.Lock()

    def _invoke_evaluator(self, payload):
        evaluator = self.evaluator
        if evaluator is None:
            evaluator = _LazyQwenEvaluator(self.model_id)
            self.evaluator = evaluator
        method = getattr(evaluator, "evaluate_ingredient_candidates", None)
        if callable(method):
            return method(payload)
        if callable(evaluator):
            return evaluator(payload)
        raise TypeError("The ingredient evaluator must be callable or implement evaluate_ingredient_candidates().")

    def _call_with_timeout(self, payload):
        if not self._inference_lock.acquire(blocking=False):
            raise TimeoutError("An earlier ingredient evaluation is still running")
        result_queue = queue.Queue(maxsize=1)

        def run():
            try:
                result_queue.put((True, self._invoke_evaluator(payload)))
            except Exception as exc:  # surfaced as a structured review-required result
                result_queue.put((False, exc))
            finally:
                self._inference_lock.release()

        worker = threading.Thread(target=run, name="renally-ingredient-evaluation", daemon=True)
        worker.start()
        try:
            ok, result = result_queue.get(timeout=self.timeout_seconds)
        except queue.Empty as exc:
            raise TimeoutError("Ingredient evaluation exceeded its configured timeout") from exc
        if not ok:
            raise result
        return result

    @staticmethod
    def _payload(parsed_ingredient, mapping_result, recipe_context):
        parsed, outer = _ingredient_parts(parsed_ingredient)
        original = (parsed.get("original_text") or parsed.get("ingredient_name")
                    or outer.get("name") or mapping_result.get("ingredient") or "")
        normalized = (parsed.get("normalized_name") or mapping_result.get("normalized_name") or "")
        quantity = parsed.get("quantity")
        if quantity is None and (parsed.get("quantity_min") is not None or parsed.get("quantity_max") is not None):
            quantity = {"min": parsed.get("quantity_min"), "max": parsed.get("quantity_max")}
        attributes = mapping_result.get("attributes")
        if not isinstance(attributes, dict):
            attributes = parsed.get("attributes") if isinstance(parsed.get("attributes"), dict) else {}
        candidates = mapping_result.get("candidates") or []
        allowed_candidates = [
            {"fdc_id": item.get("fdc_id"), "description": item.get("description") or "",
             "data_type": item.get("data_type"), "food_state": item.get("food_state")}
            for item in candidates if isinstance(item, dict)
        ]
        context = None
        if isinstance(recipe_context, str):
            context = recipe_context[:1500]
        elif isinstance(recipe_context, dict):
            # Only task-relevant recipe fields are sent to inference. Context is
            # deliberately excluded from the persistent audit record.
            context = {}
            for key in ("name", "description", "instructions", "method"):
                if recipe_context.get(key) is not None:
                    value = recipe_context[key]
                    context[key] = (" ".join(map(str, value)) if isinstance(value, list)
                                    else str(value))[:1500]
            ingredients = recipe_context.get("ingredients")
            if isinstance(ingredients, list):
                context["ingredients"] = [str(value)[:120] for value in ingredients[:30]]
        return {
            "original_ingredient": str(original)[:500],
            "normalized_name": str(normalized)[:250],
            "quantity": quantity,
            "unit": parsed.get("unit"),
            "attributes": attributes,
            "recipe_context": context,
            "candidates": allowed_candidates,
        }

    @staticmethod
    def _guard_candidates(parsed_ingredient, mapping_result, recipe_context=None):
        parsed, outer = _ingredient_parts(parsed_ingredient)
        source_text = _explicit_text(parsed, outer, mapping_result)
        guards = []
        for candidate in mapping_result.get("candidates") or []:
            if not isinstance(candidate, dict):
                continue
            incompatible, unresolved, reasons = _explicit_mismatches(
                source_text, _candidate_description(candidate)
            )
            if not str(candidate.get("description") or "").strip():
                incompatible = True
                reasons.append("The retrieved USDA candidate has no usable description.")
            gaps = variant_gaps(source_text, candidate, unit=parsed.get("unit"),
                                recipe_context=recipe_context)
            conflicts = {gap for gap in gaps if gap.endswith("_conflict")}
            if conflicts:
                incompatible = True
                reasons.append("The candidate conflicts with an explicit ingredient attribute or preparation.")
            unresolved = sorted(set(unresolved) | (gaps - conflicts))
            guards.append({
                "fdc_id": candidate.get("fdc_id"),
                "compatible": False if incompatible else True,
                "unresolved_attributes": unresolved,
                "reasons": reasons,
            })
        return guards

    @staticmethod
    def _base_result(payload, mapping_result, model_id, *, status, reason, guards, unresolved=()):
        return {
            "ingredient": payload["original_ingredient"],
            "candidate_ids": [item.get("fdc_id") for item in payload["candidates"]],
            "slm_recommended_fdc_id": None,
            "recommended_fdc_id": None,
            "compatible_candidate_ids": [],
            "status": status,
            "unresolved_attributes": sorted(set(unresolved)),
            "reason": str(reason)[:1000],
            "guard_results": guards,
            "model_id": str(model_id)[:200],
            "prompt_version": PROMPT_VERSION,
            "evaluated_at": _now(),
            "requires_review": True,
            "mapping_version": mapping_result.get("mapping_version"),
        }

    @staticmethod
    def _validate_output(raw, payload, mapping_result, model_id, guards):
        known = {}
        for item in payload["candidates"]:
            key = _id_key(item.get("fdc_id"))
            if key is None or key in known:
                return SLMIngredientMappingValidator._base_result(
                    payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                    reason="The retrieved candidate list has missing or duplicate FDC IDs.", guards=guards,
                )
            known[key] = item.get("fdc_id")

        if isinstance(raw, str):
            try:
                output = _decode_json_object(raw)
            except ValueError as exc:
                return SLMIngredientMappingValidator._base_result(
                    payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                    reason=str(exc), guards=guards,
                )
        elif isinstance(raw, dict):
            output = raw
        else:
            return SLMIngredientMappingValidator._base_result(
                payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                reason="The SLM response was not a JSON object.", guards=guards,
            )

        required = {"status", "recommended_fdc_id", "compatible_candidate_ids",
                    "unresolved_attributes", "reason", "requires_review"}
        if not required.issubset(output):
            return SLMIngredientMappingValidator._base_result(
                payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                reason="The SLM response omitted required evaluation fields.", guards=guards,
            )
        status = output.get("status")
        status = status.strip().upper() if isinstance(status, str) else ""
        recommendation = output.get("recommended_fdc_id")
        compatible = output.get("compatible_candidate_ids")
        unresolved = output.get("unresolved_attributes")
        reason = output.get("reason")
        if (status not in _ALLOWED_STATUSES or (recommendation is not None and _id_key(recommendation) is None)
                or not isinstance(compatible, list) or not isinstance(unresolved, list)
                or any(not isinstance(item, str) or not item.strip() for item in unresolved)
                or not isinstance(reason, str) or not reason.strip()
                or not isinstance(output.get("requires_review"), bool)):
            return SLMIngredientMappingValidator._base_result(
                payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                reason="The SLM response contained malformed evaluation fields.", guards=guards,
            )
        if any(_id_key(item) is None for item in compatible):
            return SLMIngredientMappingValidator._base_result(
                payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                reason="The SLM response contained a malformed candidate ID.", guards=guards,
            )

        slm_compatible = [item for item in compatible]
        id_candidates = [recommendation, *slm_compatible]
        hallucinated_by_key = {}
        for item in id_candidates:
            if item is not None and _id_key(item) not in known:
                hallucinated_by_key.setdefault(_id_key(item), item)
        hallucinated = list(hallucinated_by_key.values())
        guard_by_id = {_id_key(item.get("fdc_id")): item for item in guards}
        blocked_compatible = [item for item in slm_compatible
                              if guard_by_id.get(_id_key(item), {}).get("compatible") is False]
        unresolved_values = sorted({str(item).strip() for item in unresolved if str(item).strip()})
        result = SLMIngredientMappingValidator._base_result(
            payload, mapping_result, model_id, status=status,
            reason=reason.strip(), guards=guards, unresolved=unresolved_values,
        )
        result["slm_recommended_fdc_id"] = _json_safe(recommendation)
        result["slm_compatible_candidate_ids"] = _json_safe(slm_compatible)

        if hallucinated:
            result.update(status="REVIEW_REQUIRED", recommended_fdc_id=None,
                          compatible_candidate_ids=[], requires_review=True,
                          reason="The SLM named an FDC ID absent from the retrieved candidate set; no recommendation was accepted.")
            result["hallucinated_candidate_ids"] = _json_safe(hallucinated)
            return result
        if blocked_compatible:
            if recommendation is not None and any(_id_key(item) == _id_key(recommendation) for item in blocked_compatible):
                result.update(status="REJECTED", recommended_fdc_id=None,
                              compatible_candidate_ids=[], requires_review=True,
                              reason="A deterministic compatibility guard rejected the SLM-recommended candidate.")
                return result
            result.update(status="REVIEW_REQUIRED", recommended_fdc_id=None,
                          compatible_candidate_ids=[], requires_review=True,
                          reason="The SLM marked a deterministically incompatible candidate as compatible.")
            return result

        # Internally inconsistent recommendation/status/ID combinations are
        # never promoted to a specific identity. A model that recommends one
        # variant while listing several compatible variants can safely be
        # downgraded to ambiguity without trusting the arbitrary choice.
        if status == "RECOMMENDED":
            if (len(slm_compatible) > 1 and recommendation is not None
                    and _id_key(recommendation) in {_id_key(item) for item in slm_compatible}):
                result.update(status="AMBIGUOUS", recommended_fdc_id=None,
                              compatible_candidate_ids=slm_compatible, requires_review=True,
                              reason="Several retrieved candidates remain compatible; no specific variant was selected.")
                result["unresolved_attributes"] = sorted(set(result["unresolved_attributes"]) | {
                    attr for candidate_id in slm_compatible
                    for attr in guard_by_id.get(_id_key(candidate_id), {}).get("unresolved_attributes", [])
                })
                return result
            if recommendation is None or len(slm_compatible) != 1 or _id_key(recommendation) != _id_key(slm_compatible[0]):
                result.update(status="REVIEW_REQUIRED", requires_review=True,
                              reason="The SLM recommendation conflicts with its compatible candidate list.")
                return result
            selected_guard = guard_by_id.get(_id_key(recommendation), {})
            selected_unresolved = selected_guard.get("unresolved_attributes") or []
            if selected_unresolved:
                result.update(status="AMBIGUOUS", recommended_fdc_id=None,
                              compatible_candidate_ids=slm_compatible, requires_review=True,
                              reason="The candidate has attributes the source ingredient does not establish: "
                                     + ", ".join(selected_unresolved) + ".")
                result["unresolved_attributes"] = sorted(set(result["unresolved_attributes"]) | set(selected_unresolved))
                return result
            result.update(status="RECOMMENDED", recommended_fdc_id=known[_id_key(recommendation)],
                          compatible_candidate_ids=slm_compatible,
                          requires_review=bool(output.get("requires_review")))
            return result
        if status == "AMBIGUOUS":
            if recommendation is not None or not slm_compatible:
                result.update(status="REVIEW_REQUIRED", requires_review=True,
                              reason="The SLM's AMBIGUOUS response contains a conflicting recommendation or no compatible candidates.")
                return result
            result.update(status="AMBIGUOUS", recommended_fdc_id=None,
                          compatible_candidate_ids=slm_compatible, requires_review=True)
            result["unresolved_attributes"] = sorted(set(result["unresolved_attributes"]) | {
                attr for candidate_id in slm_compatible
                for attr in guard_by_id.get(_id_key(candidate_id), {}).get("unresolved_attributes", [])
            })
            return result
        if status == "REJECTED":
            if recommendation is not None or slm_compatible:
                result.update(status="REVIEW_REQUIRED", requires_review=True,
                              reason="The SLM's REJECTED response also identifies a recommendation or compatible candidate.")
                return result
            result.update(status="REJECTED", recommended_fdc_id=None,
                          compatible_candidate_ids=[], requires_review=True)
            return result
        if recommendation is not None:
            result["reason"] = "The SLM requested review while also returning a recommendation; no recommendation was accepted."
        result.update(status="REVIEW_REQUIRED", recommended_fdc_id=None,
                      compatible_candidate_ids=slm_compatible, requires_review=True)
        return result

    def evaluate(self, parsed_ingredient, mapping_result, *, recipe_context=None, persist=True):
        """Evaluate and cache a Step 2 result without modifying mapping status."""
        mapping_result = mapping_result if isinstance(mapping_result, dict) else {}
        payload = self._payload(parsed_ingredient, mapping_result, recipe_context)
        guards = self._guard_candidates(parsed_ingredient, mapping_result, recipe_context)
        model_id = getattr(self.evaluator, "model_id", None) or getattr(self.evaluator, "model_name", None) or self.model_id
        candidates = payload["candidates"]
        if not candidates:
            result = self._base_result(
                payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                reason="The Step 2 result contains no USDA candidates to evaluate.", guards=guards,
            )
        else:
            try:
                raw = self._call_with_timeout(payload)
            except TimeoutError:
                result = self._base_result(
                    payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                    reason="SLM inference timed out or is still handling an earlier request.", guards=guards,
                )
            except Exception as exc:
                result = self._base_result(
                    payload, mapping_result, model_id, status="REVIEW_REQUIRED",
                    reason=f"SLM inference is unavailable ({type(exc).__name__}).", guards=guards,
                )
            else:
                result = self._validate_output(raw, payload, mapping_result, model_id, guards)

        result["cache_status"] = "DRY_RUN" if not persist else "NOT_CONFIGURED"
        cache_key = mapping_result.get("cache_key")
        if persist and self.kg is not None and cache_key:
            saver = getattr(self.kg, "save_ingredient_mapping_evaluation", None)
            if not callable(saver):
                result["cache_status"] = "WRITE_ERROR"
                result["cache_error"] = "The graph cache does not support separate SLM evaluations."
            else:
                try:
                    audit_record = {key: value for key, value in result.items()
                                    if key not in {"cache_status", "cache_error"}}
                    stored = saver(cache_key, audit_record)
                    result["cache_status"] = "SAVED" if stored else "WRITE_ERROR"
                    if not stored:
                        result["cache_error"] = "No mapping cache record was updated."
                except Exception as exc:
                    result["cache_status"] = "WRITE_ERROR"
                    result["cache_error"] = f"Evaluation cache update failed ({type(exc).__name__})."
        return result

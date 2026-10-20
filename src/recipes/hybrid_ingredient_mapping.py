"""Read-only and on-demand ingredient mapping for retrieved recipes.

This service composes the existing parser, USDA mapper, deterministic policy,
optional SLM validator, preferred-reference manager, and offline pre-mapping
workflow. It never changes recipe source fields or nutrient calculations.
"""

import copy
import math
import os

from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy
from src.recipes.ingredient_identity_resolution import IngredientIdentityResolver
from src.recipes.ingredient_mapping import MAPPING_VERSION, USDAIngredientMapper
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator
from src.recipes.ingredient_parser import parse_foodcom_ingredient, parse_ingredient_text
from src.recipes.ingredient_premapping import IngredientPremappingWorkflow
from src.recipes.ingredient_reference import PreferredUSDAReferenceManager
from src.recipes.quantity_resolution import extract_recipe_ingredient_rows


MAPPING_MODES = {"READ_ONLY", "ON_DEMAND", "OFFLINE"}


def recipe_mapping_configuration(*, enabled=None, mode=None, persist_mappings=None,
                                 use_slm=None, environ=None):
    """Resolve explicit service settings from arguments or environment."""
    env = os.environ if environ is None else environ
    def choose(value, key, fallback):
        if value is not None:
            return value
        return env.get(key, fallback)

    enabled_value = enabled if enabled is not None else _env_bool_from(env, "RENALLY_RECIPE_MAPPING_ENRICHMENT", False)
    mode_value = str(choose(mode, "RENALLY_INGREDIENT_MAPPING_MODE", "READ_ONLY")).strip().upper()
    persist_value = (persist_mappings if persist_mappings is not None else
                     _env_bool_from(env, "RENALLY_INGREDIENT_MAPPING_PERSIST", False))
    slm_value = use_slm if use_slm is not None else _env_bool_from(env, "RENALLY_INGREDIENT_MAPPING_USE_SLM", False)
    if mode_value not in MAPPING_MODES:
        raise ValueError(f"Unsupported hybrid ingredient mapping mode: {mode_value!r}.")
    if mode_value != "ON_DEMAND" and persist_value:
        raise ValueError("Persistent recipe mapping writes are supported only in ON_DEMAND mode.")
    return {
        "enabled": bool(enabled_value),
        "mode": mode_value,
        "persist_mappings": bool(persist_value),
        "use_slm": bool(slm_value),
    }


def _env_bool_from(env, name, default=False):
    value = env.get(name)
    if value is None:
        return default
    return str(value).strip().casefold() in {"1", "true", "yes", "on"}


class HybridIngredientMappingService:
    """Resolve raw recipe ingredients with the existing mapping infrastructure.

    READ_ONLY is the default. ON_DEMAND candidate/cache writes and deterministic
    verification require ``persist_mappings=True``. OFFLINE request resolution
    reads existing cache/reference data only; batch processing is available
    solely through ``run_offline_batch`` and retains the workflow's own caps.
    """

    def __init__(self, kg, retriever, *, mode="READ_ONLY", persist_mappings=False,
                 use_slm=False, dataset_scope="all", mapper=None,
                 reference_manager=None, resolver=None, validator=None,
                 premapping_workflow=None, candidate_limit=5):
        self.kg = kg
        self.retriever = retriever
        self.mode = str(mode).strip().upper()
        if self.mode not in MAPPING_MODES:
            raise ValueError(f"Unsupported hybrid ingredient mapping mode: {mode!r}.")
        self.persist_mappings = bool(persist_mappings)
        if self.mode != "ON_DEMAND" and self.persist_mappings:
            raise ValueError("Persistent recipe mapping writes are supported only in ON_DEMAND mode.")
        self.use_slm = bool(use_slm)
        self.dataset_scope = str(dataset_scope or "all")
        self.candidate_limit = max(1, min(int(candidate_limit), 25))
        self.mapper = mapper or USDAIngredientMapper(kg, retriever, mapping_version=MAPPING_VERSION)
        self.reference_manager = reference_manager or PreferredUSDAReferenceManager(kg)
        self.policy = AutomaticIngredientMappingPolicy()
        self.resolver = resolver or IngredientIdentityResolver(
            self.mapper, self.reference_manager, mapping_policy=self.policy,
            allow_reference_defaults=True,
            auto_verify_exact=(self.mode == "ON_DEMAND" and self.persist_mappings),
        )
        self.validator = validator
        if self.use_slm and self.validator is None:
            self.validator = SLMIngredientMappingValidator(kg)
        self.premapping_workflow = premapping_workflow
        if self.mode == "OFFLINE" and self.premapping_workflow is None:
            self.premapping_workflow = IngredientPremappingWorkflow(
                kg, self.mapper, policy=self.policy,
                validator=self.validator if self.use_slm else None,
            )

    @classmethod
    def _recipe_items(cls, recipe):
        """Return source ingredient rows, preferring persisted Step 1 parses."""
        rows, _alignment_status = extract_recipe_ingredient_rows(recipe)
        return rows

    @staticmethod
    def _parsed_item(source_item, kind):
        if kind == "invalid":
            raise ValueError("Recipe ingredient record is not a supported object or string.")
        if not isinstance(source_item, dict):
            raise ValueError("Recipe ingredient record is not an object.")
        if isinstance(source_item.get("parsed"), dict):
            return copy.deepcopy(source_item)
        if kind == "text":
            parsed = parse_ingredient_text(source_item.get("name", ""))
            return {"name": source_item.get("name", ""), "parsed": parsed}
        name = source_item.get("name") or source_item.get("ingredient_name")
        quantity = source_item.get("quantity_raw")
        if name is None and source_item.get("original_text"):
            parsed = parse_ingredient_text(source_item["original_text"])
            return {"name": parsed.get("ingredient_name", ""), "parsed": parsed}
        if name is None or not str(name).strip():
            raise ValueError("Legacy recipe ingredient has no source name.")
        return {
            **copy.deepcopy(source_item),
            "name": str(name),
            "parsed": (parse_ingredient_text(f"{quantity} {name}") if quantity is not None
                       else parse_ingredient_text(name)),
        }

    @staticmethod
    def _quantity_metadata(item):
        parsed = item.get("parsed") if isinstance(item.get("parsed"), dict) else item
        raw = item.get("quantity_raw")
        amount = parsed.get("quantity_g")
        if not isinstance(amount, (int, float)) or isinstance(amount, bool) or not math.isfinite(amount):
            amount = None
        return {
            "quantity_raw": raw,
            "quantity_g": amount,
            "quantity_status": parsed.get("quantity_status", "UNRESOLVED"),
            "quantity_scope": parsed.get("quantity_scope", "UNSPECIFIED"),
        }

    def _direct_recipe_link(self, position, source_item):
        food_id = source_item.get("id")
        food = (getattr(self.kg, "ingredients", {}) or {}).get(food_id, {})
        source = food.get("source") or {}
        fdc_id = source.get("fdc_id")
        description = food.get("fdc_description") or food.get("name")
        amount = source_item.get("amount_g")
        valid_amount = (amount if isinstance(amount, (int, float)) and not isinstance(amount, bool)
                        and math.isfinite(amount) and amount >= 0 else None)
        return {
            "position": position,
            "original_text": description or str(food_id),
            "original_name": description or food.get("name") or str(food_id),
            "normalized_name": str(food.get("name") or description or "").casefold(),
            "quantity_raw": None,
            "quantity_g": valid_amount,
            "quantity_status": "RECIPE_GRAPH_AMOUNT" if valid_amount is not None else "UNRESOLVED",
            "quantity_scope": "TOTAL",
            "mapping": {
                "status": "EXACT_IDENTITY" if fdc_id is not None else "UNRESOLVED",
                "fdc_id": fdc_id,
                "usda_description": description,
                "resolution_source": "RECIPE_FOOD_LINK",
                "verification_method": None,
                "mapping_version": None,
                "verification_provenance": None,
                "reference_default": False,
                "candidate_ids": [fdc_id] if fdc_id is not None else [],
                "unresolved_attributes": [],
                "ambiguity_reasons": [],
                "missing_attributes": [],
                "nutritional_uncertainty": "IDENTITY_EXACT" if fdc_id is not None else "UNRELIABLE",
                "resolution_reason": (
                    "The recipe is already linked to this local USDA food record; this is not an IngredientMapping verification."
                    if fdc_id is not None else "The recipe link does not resolve to a local USDA FDC ID."
                ),
            },
        }

    @staticmethod
    def _cache_result(cached, *, cache_key, normalized_name, dataset_scope, mapping_version):
        if not cached:
            return {
                "status": "UNRESOLVED", "candidates": [], "cache_key": cache_key,
                "mapping_version": mapping_version, "normalized_name": normalized_name,
                "dataset_scope": dataset_scope, "cache_hit": False,
                "cache_status": "OFFLINE_CACHE_MISS",
            }
        method = cached.get("verification_method") or cached.get("validation_method")
        stale_policy = (cached.get("status") == "VERIFIED" and method == "DETERMINISTIC_POLICY"
                        and cached.get("verification_policy_version") != AutomaticIngredientMappingPolicy.VERSION)
        if cached.get("mapping_version") != mapping_version or stale_policy:
            return {
                "status": "UNRESOLVED", "candidates": [], "cache_key": cache_key,
                "mapping_version": mapping_version, "normalized_name": normalized_name,
                "dataset_scope": dataset_scope, "cache_hit": False,
                "cache_status": "STALE_CACHE_REQUIRES_BATCH_REEVALUATION",
                "stale_cache_record": True,
                "previous_verified_mapping": ({
                    "fdc_id": cached.get("fdc_id"), "verification_method": method,
                    "verification_policy_version": cached.get("verification_policy_version"),
                } if cached.get("status") == "VERIFIED" else None),
            }
        return {
            **cached,
            "cache_key": cache_key,
            "normalized_name": normalized_name,
            "mapping_version": mapping_version,
            "dataset_scope": dataset_scope,
            "cache_hit": True,
            "cache_status": "HIT",
        }

    def _mapping_result(self, parsed_item, recipe_context):
        if self.mode == "OFFLINE":
            key = self.mapper.mapping_cache_key(parsed_item, dataset_scope=self.dataset_scope)
            parsed = parsed_item.get("parsed") or {}
            normalized_name = parsed.get("normalized_name") or ""
            try:
                cached = self.kg.get_ingredient_mapping_cache(key)
            except Exception as exc:
                return {
                    "status": "CACHE_ERROR", "candidates": [], "cache_key": key,
                    "mapping_version": self.mapper.mapping_version,
                    "normalized_name": normalized_name, "dataset_scope": self.dataset_scope,
                    "cache_status": "READ_ERROR", "error": str(exc),
                }
            return self._cache_result(
                cached, cache_key=key, normalized_name=normalized_name,
                dataset_scope=self.dataset_scope, mapping_version=self.mapper.mapping_version,
            )

        return self.mapper.retrieve_candidates(
            parsed_item, dataset_scope=self.dataset_scope, limit=self.candidate_limit,
            persist=(self.mode == "ON_DEMAND" and self.persist_mappings),
            reuse_verified_policy_version=self.policy.VERSION,
        )

    def _mapping_for_item(self, parsed_item, recipe_context):
        mapping_result = self._mapping_result(parsed_item, recipe_context)
        # The mapping cache is keyed by ingredient attributes, not by recipe.
        # A previous recipe's SLM judgment cannot be reused for this context.
        mapping_result = {**mapping_result, "slm_evaluation": None}
        slm_evaluation = None
        slm_error = None
        if (self.use_slm and self.mode != "OFFLINE"
                and mapping_result.get("status") != "VERIFIED"
                and mapping_result.get("candidates")
                and not self.policy.evaluate(parsed_item, mapping_result).get("eligible")):
            try:
                slm_evaluation = self.validator.evaluate(
                    parsed_item, mapping_result, recipe_context=recipe_context,
                    persist=False,
                )
                mapping_result["slm_inference_performed"] = True
            except Exception as exc:
                slm_error = f"SLM evaluation failed ({type(exc).__name__})."
                slm_evaluation = {
                    "status": "REVIEW_REQUIRED", "recommended_fdc_id": None,
                    "requires_review": True, "reason": slm_error,
                }

        try:
            resolution = self.resolver.resolve(
                parsed_item, mapping_result=mapping_result,
                slm_evaluation=slm_evaluation, dataset_scope=self.dataset_scope,
                persist=(self.mode == "ON_DEMAND" and self.persist_mappings),
            )
        except Exception as exc:
            resolution = {
                "resolution_status": "UNRESOLVED",
                "selected_fdc_id": None,
                "selected_usda_description": None,
                "nutritional_certainty": "UNRELIABLE",
                "unresolved_attributes": [],
                "resolution_reason": f"Ingredient resolution failed ({type(exc).__name__}).",
                "resolution_error": str(exc),
            }

        candidates = mapping_result.get("candidates") or []
        policy = resolution.get("identity_policy_evaluation") or {}
        ref_policy = resolution.get("reference_policy_evaluation") or {}
        unresolved = set(resolution.get("unresolved_attributes") or [])
        if resolution.get("resolution_status") != "SLM_RESOLVED":
            unresolved.update(policy.get("unresolved_attributes") or [])
            unresolved.update(ref_policy.get("unresolved_attributes") or [])
        missing_attributes = set()
        for candidate in policy.get("candidate_evidence") or []:
            missing_attributes.update(candidate.get("unresolved_attributes") or [])
        errors = []
        if mapping_result.get("error"):
            errors.append({"stage": "retrieval", "message": str(mapping_result["error"])})
        if mapping_result.get("cache_read_error"):
            errors.append({"stage": "mapping_cache", "message": str(mapping_result["cache_read_error"])})
        if mapping_result.get("cache_error"):
            errors.append({"stage": "mapping_cache", "message": str(mapping_result["cache_error"])})
        if slm_error:
            errors.append({"stage": "slm_validation", "message": slm_error})
        if slm_evaluation and slm_evaluation.get("status") == "REVIEW_REQUIRED":
            reason = slm_evaluation.get("reason")
            if reason:
                errors.append({"stage": "slm_validation", "message": str(reason)})
        if resolution.get("resolution_error"):
            errors.append({"stage": "resolution", "message": str(resolution["resolution_error"])})

        status = resolution.get("resolution_status") or "UNRESOLVED"
        entity_status = ("FAMILY_RESOLVED" if status == "FAMILY_RESOLVED" else
                         "RESOLVED" if status in {"VERIFIED_MAPPING", "EXACT_IDENTITY", "REFERENCE_DEFAULT", "SLM_RESOLVED"}
                         else "AMBIGUOUS")
        reference = resolution.get("preferred_reference") or {}
        verification_method = (resolution.get("verification_method")
                              or mapping_result.get("verification_method")
                              or mapping_result.get("validation_method"))
        verification_provenance = None
        if verification_method:
            provenance = {
                "verification_method": verification_method,
                "verification_policy_version": (resolution.get("verification_policy_version")
                                                 or mapping_result.get("verification_policy_version")),
                "verification_evidence": (resolution.get("verification_evidence")
                                          or mapping_result.get("verification_evidence")),
                "reviewer": (resolution.get("verification_reviewer")
                             or mapping_result.get("reviewer")
                             or mapping_result.get("validated_by")),
                "validated_at": (resolution.get("verification_timestamp")
                                 or mapping_result.get("validated_at")),
                "validation_note": (resolution.get("verification_note")
                                    or mapping_result.get("validation_note")),
            }
            verification_provenance = {key: value for key, value in provenance.items() if value is not None}

        reference_default = status == "REFERENCE_DEFAULT"
        reasons = list(policy.get("reasons") or [])
        if resolution.get("resolution_reason") and resolution.get("resolution_reason") not in reasons:
            reasons.append(resolution["resolution_reason"])
        if ref_policy.get("reasons"):
            reasons.extend(reason for reason in ref_policy["reasons"] if reason not in reasons)
        if mapping_result.get("cache_status") == "OFFLINE_CACHE_MISS":
            reasons.append("No cached mapping exists; use the explicit OFFLINE batch workflow for candidate retrieval.")

        return {
            "status": status,
            "entity_resolution_status": entity_status,
            "resolved_family": resolution.get("resolved_family"),
            "fdc_id": resolution.get("selected_fdc_id"),
            "usda_description": resolution.get("selected_usda_description"),
            "verification_method": verification_method,
            "verification_policy_version": (resolution.get("verification_policy_version")
                                             or mapping_result.get("verification_policy_version")),
            "mapping_version": mapping_result.get("mapping_version"),
            "verification_provenance": verification_provenance,
            "reference_default": reference_default,
            "reference_fdc_id": reference.get("fdc_id") if reference_default else None,
            "reference_policy_version": (reference.get("reference_policy_version")
                                         or resolution.get("reference_policy_version")),
            "assumed_attributes": resolution.get("assumed_attributes") or {},
            "unresolved_attributes": sorted(unresolved),
            "candidate_ids": (resolution.get("plausible_candidate_ids")
                              or [candidate.get("fdc_id") for candidate in candidates
                                  if candidate.get("fdc_id") is not None]),
            "candidates": [
                {key: item.get(key) for key in ("fdc_id", "description", "data_type", "food_state", "dataset_key")}
                for item in candidates if isinstance(item, dict)
            ],
            "ambiguity_reasons": reasons,
            "missing_attributes": sorted(missing_attributes),
            "nutritional_uncertainty": resolution.get("nutritional_certainty", "UNRELIABLE"),
            "resolution_reason": resolution.get("resolution_reason"),
            "retrieval_status": mapping_result.get("status"),
            "cache_hit": bool(mapping_result.get("cache_hit")),
            "cache_status": mapping_result.get("cache_status"),
            "cache_version_mismatch": bool(mapping_result.get("stale_cache_record")
                                            or mapping_result.get("cache_status") == "STALE_VERIFIED_POLICY"),
            "slm_validation": slm_evaluation,
            "slm_inference_performed": bool(mapping_result.get("slm_inference_performed")),
            "candidate_retrieval_performed": bool(
                self.mode != "OFFLINE" and not mapping_result.get("cache_hit")
            ),
            "errors": errors,
        }

    def resolve_ingredient(self, parsed_item, recipe_context=None):
        """Resolve one parsed source ingredient against a bounded USDA shortlist."""
        return self._mapping_for_item(parsed_item, recipe_context or {})

    def _structured_result(self, position, source_item, parsed_item, mapping, *, link=None):
        parsed = parsed_item.get("parsed") if isinstance(parsed_item, dict) else {}
        if not isinstance(parsed, dict) and isinstance(parsed_item, dict):
            parsed = parsed_item
        parsed = parsed if isinstance(parsed, dict) else {}
        quantity = self._quantity_metadata(parsed_item)
        name = parsed.get("ingredient_name") or parsed_item.get("name") or ""
        original_text = (parsed.get("original_text") or
                         " ".join(str(part) for part in (quantity.get("quantity_raw"), name) if part) or name)
        result = {
            "position": position,
            "original_text": str(original_text),
            "original_name": str(name),
            "normalized_name": parsed.get("normalized_name") or "",
            **quantity,
            "mapping": mapping,
        }
        if link is not None:
            result["source_recipe_link"] = {"food_record_id": link.get("id")}
        return result

    @staticmethod
    def _parse_failure(position, source_item, exc):
        name = source_item.get("name") if isinstance(source_item, dict) else str(source_item or "")
        quantity = source_item.get("quantity_raw") if isinstance(source_item, dict) else None
        return {
            "position": position,
            "original_text": " ".join(str(part) for part in (quantity, name) if part),
            "original_name": str(name or ""),
            "normalized_name": None,
            "quantity_raw": quantity,
            "quantity_g": None,
            "quantity_status": "UNPARSED",
            "quantity_scope": "UNSPECIFIED",
            "mapping": {
                "status": "UNRESOLVED", "fdc_id": None, "usda_description": None,
                "verification_method": None, "mapping_version": None,
                "verification_provenance": None, "reference_default": False,
                "candidate_ids": [], "unresolved_attributes": [],
                "ambiguity_reasons": [f"Legacy ingredient could not be parsed ({type(exc).__name__})."],
                "missing_attributes": [], "nutritional_uncertainty": "UNRELIABLE",
                "errors": [{"stage": "parsing", "message": str(exc)}],
            },
        }

    def resolve_recipe(self, recipe, *, max_ingredients=None):
        """Resolve each source ingredient while preserving every input record."""
        rows, alignment_status = extract_recipe_ingredient_rows(recipe)
        source_ingredient_count = len(rows)
        if max_ingredients is not None:
            rows = rows[:max(1, int(max_ingredients))]
        recipe_context = {
            key: recipe.get(key) for key in ("name", "description", "method", "instructions")
            if isinstance(recipe, dict) and recipe.get(key) is not None
        }
        if isinstance(recipe, dict):
            recipe_context["ingredients"] = [
                str(item.get("name") or item.get("ingredient_name") or "")
                for _position, item, _kind in rows if isinstance(item, dict)
            ][:30]
        results, resolved_by_key = [], {}
        cache_lookups = candidate_retrievals = slm_calls = unresolved = 0
        for position, source_item, kind in rows:
            if kind == "recipe_link":
                result = self._direct_recipe_link(position, source_item)
                if result["mapping"]["status"] == "UNRESOLVED":
                    unresolved += 1
                results.append(result)
                continue
            try:
                if (isinstance(recipe, dict)
                        and recipe.get("source_dataset") == "Food.com Recipes and Reviews (Kaggle)"
                        and isinstance(source_item, dict)):
                    source_item = {**source_item}
                    source_item["parsed"] = parse_foodcom_ingredient(
                        source_item.get("name") or source_item.get("ingredient_name"),
                        source_item.get("quantity_raw"),
                    )
                parsed_item = self._parsed_item(source_item, kind)
                key = self.mapper.mapping_cache_key(parsed_item, dataset_scope=self.dataset_scope)
            except Exception as exc:
                results.append(self._parse_failure(position, source_item, exc))
                unresolved += 1
                continue
            if key in resolved_by_key:
                resolved = copy.deepcopy(resolved_by_key[key])
            else:
                try:
                    resolved = self._mapping_for_item(parsed_item, recipe_context)
                    cache_lookups += 1
                    candidate_retrievals += int(resolved.get("candidate_retrieval_performed"))
                    slm_calls += int(resolved.get("slm_inference_performed"))
                except Exception as exc:
                    resolved = {
                        "status": "UNRESOLVED", "fdc_id": None, "usda_description": None,
                        "verification_method": None, "mapping_version": self.mapper.mapping_version,
                        "verification_provenance": None, "reference_default": False,
                        "candidate_ids": [], "unresolved_attributes": [],
                        "ambiguity_reasons": [f"Ingredient mapping failed ({type(exc).__name__})."],
                        "missing_attributes": [], "nutritional_uncertainty": "UNRELIABLE",
                        "errors": [{"stage": "mapping", "message": str(exc)}],
                    }
                resolved_by_key[key] = copy.deepcopy(resolved)
            if resolved.get("status") not in {"VERIFIED_MAPPING", "EXACT_IDENTITY", "REFERENCE_DEFAULT", "SLM_RESOLVED"}:
                unresolved += 1
            results.append(self._structured_result(position, source_item, parsed_item, resolved))
        linked_contexts = {str(item.get("id")) for _, item, kind in rows
                           if kind == "recipe_link" and isinstance(item, dict)}
        return {
            "mode": self.mode,
            "ingredient_count": len(rows),
            "source_ingredient_count": source_ingredient_count,
            "ingredient_limit_reached": len(rows) < source_ingredient_count,
            "quantity_alignment_status": alignment_status,
            "unique_ingredient_contexts": len(resolved_by_key) + len(linked_contexts),
            "mapping_cache_lookups": cache_lookups,
            "usda_candidate_retrievals": candidate_retrievals,
            "slm_inference_calls": slm_calls,
            "unresolved_ingredients": unresolved,
            "results": results,
        }

    def run_offline_batch(self, **kwargs):
        """Delegate one explicitly requested batch to the existing checkpointed workflow."""
        if self.mode != "OFFLINE":
            raise ValueError("run_offline_batch is available only in OFFLINE mode.")
        return self.premapping_workflow.run(**kwargs)

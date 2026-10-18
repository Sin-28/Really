"""Request orchestration for the local RenAlly chat interface."""
import copy
import json
import os
import re
from pathlib import Path
from time import perf_counter

from src.constraints.inputs import parse_budget_mapping
from src.constraints.profiles import resolve_reference_rules
from src.constraints.recipe_nutrient_adapter import RecipeNutrientConstraintAdapter
from src.generation.grounded_generator import GroundedRecipeGenerator, check_method_faithfulness
from src.nutrition import NUTRIENT_UNITS
from src.runtime import create_runtime
from src.recipes.hybrid_ingredient_mapping import (
    HybridIngredientMappingService,
    recipe_mapping_configuration,
)
from src.recipes.ingredient_mapping_validator import SharedQwenEvaluator, SLMIngredientMappingValidator
from src.recipes.adapted_recipe_nutrition import AdaptedRecipeNutritionEnrichmentService
from src.recipes.adapted_recipe_culinary import AdaptedRecipeCulinaryValidationService
from src.recipes.final_recipe_acceptance import FinalRecipeAcceptancePolicy
from src.web.request_planner import RequestPlanner

PROJECT_ROOT = Path(__file__).resolve().parents[2]


ALLOWED_CONDITIONS = {"haemodialysis", "diabetes", "hypertension"}


def recipe_nutrition_enrichment_configuration(*, enabled=None, environ=None):
    """Resolve the optional Step 5B-1 recipe enrichment flag (default off)."""
    env = os.environ if environ is None else environ
    if enabled is not None:
        return bool(enabled)
    value = str(env.get("RENALLY_RECIPE_NUTRITION_ENRICHMENT", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def recipe_constraint_enrichment_configuration(*, enabled=None, environ=None):
    """Resolve the optional Step 5B-2 informational validation flag."""
    env = os.environ if environ is None else environ
    if enabled is not None:
        return bool(enabled)
    value = str(env.get("RENALLY_RECIPE_CONSTRAINT_ENRICHMENT", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def adapted_recipe_nutrition_enrichment_configuration(*, enabled=None, environ=None):
    """Resolve the optional Step 5C adaptation validation flag (default off)."""
    env = os.environ if environ is None else environ
    if enabled is not None:
        return bool(enabled)
    value = str(env.get("RENALLY_ADAPTED_RECIPE_NUTRITION_ENRICHMENT", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def adapted_recipe_culinary_validation_configuration(*, enabled=None, environ=None):
    """Resolve the optional Step 6A culinary-validation flag (default off)."""
    env = os.environ if environ is None else environ
    if enabled is not None:
        return bool(enabled)
    value = str(env.get("RENALLY_ADAPTED_RECIPE_CULINARY_VALIDATION", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def final_recipe_acceptance_configuration(*, enabled=None, environ=None):
    """Resolve the optional Step 6B acceptance metadata flag (default off)."""
    env = os.environ if environ is None else environ
    if enabled is not None:
        return bool(enabled)
    value = str(env.get("RENALLY_FINAL_RECIPE_ACCEPTANCE", "false")).strip().casefold()
    return value in {"1", "true", "yes", "on"}


def parse_budgets(values, scope):
    return parse_budget_mapping(values, scope)


class RenAllyWebService:
    MAX_SOURCE_INGREDIENT_LOOKUPS = 30

    def __init__(self, data_dir=None, *, recipe_mapping_enabled=None,
                 ingredient_mapping_mode=None, ingredient_mapping_persist=None,
                 ingredient_mapping_use_slm=None,
                 recipe_nutrition_enrichment_enabled=None,
                 recipe_constraint_enrichment_enabled=None,
                 adapted_recipe_nutrition_enrichment_enabled=None,
                 adapted_recipe_culinary_validation_enabled=None,
                 final_recipe_acceptance_enabled=None):
        data_dir = Path(data_dir) if data_dir else PROJECT_ROOT / "data"
        self.recipe_mapping_config = recipe_mapping_configuration(
            enabled=recipe_mapping_enabled, mode=ingredient_mapping_mode,
            persist_mappings=ingredient_mapping_persist,
            use_slm=ingredient_mapping_use_slm,
        )
        self.recipe_nutrition_enrichment_enabled = recipe_nutrition_enrichment_configuration(
            enabled=recipe_nutrition_enrichment_enabled,
        )
        self.recipe_constraint_enrichment_enabled = recipe_constraint_enrichment_configuration(
            enabled=recipe_constraint_enrichment_enabled,
        )
        self.adapted_recipe_nutrition_enrichment_enabled = adapted_recipe_nutrition_enrichment_configuration(
            enabled=adapted_recipe_nutrition_enrichment_enabled,
        )
        self.adapted_recipe_culinary_validation_enabled = adapted_recipe_culinary_validation_configuration(
            enabled=adapted_recipe_culinary_validation_enabled,
        )
        self.final_recipe_acceptance_enabled = final_recipe_acceptance_configuration(
            enabled=final_recipe_acceptance_enabled,
        )
        self.runtime = create_runtime(data_dir)
        self.kg = self.runtime.kg
        self.profiles = json.loads((data_dir / "clinical_constraints.json").read_text())
        self.retriever = self.runtime.retriever
        self.engine = self.runtime.engine
        self.substitution_analyzer = self.runtime.substitution_analyzer
        self.deterministic_generator = GroundedRecipeGenerator()
        self.planner = RequestPlanner(self.kg, self.retriever, self.deterministic_generator)
        self.ingredient_slm_validator = SLMIngredientMappingValidator(
            self.kg, evaluator=SharedQwenEvaluator(self.planner.get_slm_generator),
        )
        self.ingredient_mapping_service = None
        if (self.recipe_mapping_config["enabled"] or self.recipe_nutrition_enrichment_enabled
                or self.recipe_constraint_enrichment_enabled
                or self.adapted_recipe_nutrition_enrichment_enabled):
            mapping_mode = (self.recipe_mapping_config["mode"]
                            if self.recipe_mapping_config["enabled"] else "READ_ONLY")
            mapping_persist = (self.recipe_mapping_config["persist_mappings"]
                               if self.recipe_mapping_config["enabled"] else False)
            mapping_slm = (self.recipe_mapping_config["use_slm"]
                           if self.recipe_mapping_config["enabled"] else False)
            self.ingredient_mapping_service = HybridIngredientMappingService(
                self.kg, self.retriever,
                mode=mapping_mode,
                persist_mappings=mapping_persist,
                use_slm=mapping_slm,
                **({"validator": self.ingredient_slm_validator} if mapping_slm else {}),
            )
        self.chat_ingredient_mapping_service = None
        self.recipe_nutrient_enrichment_service = None
        if (self.recipe_nutrition_enrichment_enabled or self.recipe_constraint_enrichment_enabled
                or self.adapted_recipe_nutrition_enrichment_enabled):
            self.recipe_nutrient_enrichment_service = self.runtime.create_recipe_nutrient_enrichment(
                mapping_service=self.ingredient_mapping_service,
            )
        self.recipe_nutrient_constraint_adapter = RecipeNutrientConstraintAdapter()
        self.adapted_recipe_nutrition_enrichment_service = (
            AdaptedRecipeNutritionEnrichmentService(
                self.recipe_nutrient_enrichment_service,
                self.recipe_nutrient_constraint_adapter,
            ) if self.recipe_nutrient_enrichment_service is not None else None
        )
        self.adapted_recipe_culinary_validation_service = (
            AdaptedRecipeCulinaryValidationService(self.planner.get_slm_generator)
            if self.adapted_recipe_culinary_validation_enabled else None
        )
        self.final_recipe_acceptance_policy = (
            FinalRecipeAcceptancePolicy() if self.final_recipe_acceptance_enabled else None
        )

    def close(self):
        self.runtime.close()

    def _chat_mapping_service(self):
        """Resolve Food.com ingredients in chat without changing source recipes."""
        service = getattr(self, "chat_ingredient_mapping_service", None)
        if service is None:
            configured = getattr(self, "ingredient_mapping_service", None)
            if (configured is not None and getattr(configured, "mode", None) == "READ_ONLY"
                    and getattr(configured, "use_slm", False)):
                service = configured
            else:
                mapper = getattr(configured, "mapper", None)
                service = HybridIngredientMappingService(
                    self.kg, self.retriever, mode="READ_ONLY", use_slm=True,
                    mapper=mapper, validator=self.ingredient_slm_validator,
                )
            self.chat_ingredient_mapping_service = service
        return service

    def _resolve_chat_foodcom_ingredients(self, response, recipe):
        try:
            # Pipeline metadata calls the title recipe_name; the resolver expects name.
            context = {**recipe, "name": recipe.get("recipe_name") or recipe.get("name")}
            enrichment = self._chat_mapping_service().resolve_recipe(context, max_ingredients=20)
            response["ingredient_mapping_enrichment"] = enrichment
            mapped = enrichment.get("results") or []
            specific = sum(row.get("mapping", {}).get("fdc_id") is not None for row in mapped)
            families = sum(bool(row.get("mapping", {}).get("resolved_family")) for row in mapped)
            unresolved = len(mapped) - specific - families
            names = recipe.get("raw_ingredient_names") or recipe.get("raw_ingredients") or []
            quantities = recipe.get("raw_ingredient_quantities") or []
            if enrichment.get("quantity_alignment_status") == "MISMATCHED_ARRAY_LENGTHS":
                quantity_issue = (
                    f"The source has {len(names)} ingredient names but {len(quantities)} quantity entries, "
                    "so the amounts cannot be paired reliably."
                )
            else:
                quantity_issue = "Source amounts have not been validated as gram weights."
            unitless = bool(quantities) and all(
                re.fullmatch(r"\s*(?:\d+(?:\.\d+)?(?:\s+\d+/\d+)?|\d+/\d+)\s*", str(value or ""))
                for value in quantities
            )
            if unitless:
                quantity_issue += " The quantity entries also have no units."
            enrichment["quantity_diagnostic"] = quantity_issue
            response["answer"] = (
                f'I found "{recipe.get("recipe_name") or "the recipe"}" in Food.com. '
                f"The ingredient check identified {specific} specific USDA food records and "
                f"{families} ingredient families; {unresolved} entries remain unresolved. "
                f"{quantity_issue} Without reliable ingredient identities and gram amounts, "
                "I cannot report nutrient totals or check dietary limits."
            )
            if enrichment.get("ingredient_limit_reached"):
                response["answer"] += (
                    f" I checked the first 20 of {enrichment['source_ingredient_count']} source ingredients."
                )
            response["missing_data"] = {
                **recipe,
                "message": quantity_issue + " Nutrient totals and dietary checks remain unavailable.",
            }
            response["tool_trace"].append(
                "Resolved Food.com ingredient identities against USDA candidates with "
                "deterministic guards and local Qwen for ambiguous cases; source amounts remain unverified."
            )
        except Exception as exc:
            response["ingredient_mapping_enrichment"] = {
                "mode": "READ_ONLY", "results": [],
                "error": f"Ingredient resolution unavailable ({type(exc).__name__}).",
            }
        return response

    def _attach_ingredient_mapping_enrichment(self, response, recipe, *, constraints=None,
                                              requested_servings=None):
        """Attach opt-in mapping and nutrient metadata without mutating source."""
        service = getattr(self, "ingredient_mapping_service", None)
        nutrient_service = getattr(self, "recipe_nutrient_enrichment_service", None)
        mapping_enabled = bool(getattr(self, "recipe_mapping_config", {}).get("enabled"))
        constraint_enabled = bool(getattr(self, "recipe_constraint_enrichment_enabled", False))
        nutrient_enabled = bool(getattr(
            self, "recipe_nutrition_enrichment_enabled",
            nutrient_service is not None and not constraint_enabled,
        ))
        if nutrient_service is not None and (nutrient_enabled or constraint_enabled):
            started = perf_counter()
            try:
                enriched = nutrient_service.enrich(recipe)
                if mapping_enabled:
                    response["ingredient_mapping_enrichment"] = enriched.get(
                        "ingredient_mapping_enrichment", {}
                    )
                if nutrient_enabled:
                    response["ingredient_nutrient_enrichment"] = {
                        key: value for key, value in enriched.items()
                        if key != "ingredient_mapping_enrichment"
                    }
                if constraint_enabled:
                    validation = self.recipe_nutrient_constraint_adapter.evaluate(
                        enriched, constraints or {}, requested_servings=requested_servings,
                    )
                    validation["performance_ms"].update({
                        "nutrient_calculation": (enriched.get("performance_ms") or {}).get("overall_calculation"),
                        "overall_enrichment": (perf_counter() - started) * 1000.0,
                    })
                    validation["dependency_available"] = True
                    response["ingredient_constraint_enrichment"] = validation
            except Exception as exc:
                unavailable = {
                    "calculation_status": "UNAVAILABLE",
                    "ingredient_count": 0,
                    "calculated_ingredient_count": 0,
                    "unresolved_ingredient_count": 0,
                    "ingredients": [], "recipe_totals": None,
                    "calculated_subtotals": None, "per_serving": None,
                    "error": f"Nutrient enrichment failed ({type(exc).__name__}).",
                }
                if nutrient_enabled:
                    response["ingredient_nutrient_enrichment"] = unavailable
                if constraint_enabled:
                    validation = self.recipe_nutrient_constraint_adapter.evaluate(
                        unavailable, constraints or {}, requested_servings=requested_servings,
                    )
                    validation["dependency_available"] = False
                    validation["dependency_error"] = unavailable["error"]
                    validation["performance_ms"]["overall_enrichment"] = (
                        perf_counter() - started
                    ) * 1000.0
                    response["ingredient_constraint_enrichment"] = validation
            return response
        if constraint_enabled:
            unavailable = {"calculation_status": "UNAVAILABLE"}
            validation = self.recipe_nutrient_constraint_adapter.evaluate(
                unavailable, constraints or {}, requested_servings=requested_servings,
            )
            validation["dependency_available"] = False
            validation["dependency_error"] = "The required USDA nutrient enrichment service is unavailable."
            validation["performance_ms"]["overall_enrichment"] = 0.0
            response["ingredient_constraint_enrichment"] = validation
            return response
        adapted_only = bool(getattr(self, "adapted_recipe_nutrition_enrichment_enabled", False))
        if service is None or (adapted_only and not mapping_enabled):
            return response
        try:
            response["ingredient_mapping_enrichment"] = service.resolve_recipe(recipe)
        except Exception as exc:
            # Mapping enrichment is ancillary; a failure must not fail recipe retrieval.
            response["ingredient_mapping_enrichment"] = {
                "mode": getattr(service, "mode", "READ_ONLY"),
                "ingredient_count": 0,
                "unique_ingredient_contexts": 0,
                "mapping_cache_lookups": 0,
                "usda_candidate_retrievals": 0,
                "slm_inference_calls": 0,
                "unresolved_ingredients": 0,
                "results": [],
                "error": f"Ingredient mapping enrichment failed ({type(exc).__name__}).",
            }
        return response

    def _adapted_nutrition_metadata(self, original_recipe, adapted_recipe, constraints,
                                    requested_servings, *, adaptation_id,
                                    original_enrichment=None, mapping_cache=None,
                                    nutrient_record_cache=None):
        """Return informational Step 5C metadata; failures never affect selection."""
        service = getattr(self, "adapted_recipe_nutrition_enrichment_service", None)
        if service is None:
            return {
                "adaptation_id": adaptation_id,
                "change_detection_status": "UNRESOLVED",
                "changes": [],
                "unresolved_changes": [{"reason": "ADAPTED_NUTRIENT_SERVICE_UNAVAILABLE"}],
                "nutritional_calculation": {"calculation_status": "UNAVAILABLE"},
                "constraint_validation": {"constraint_status": "UNKNOWN"},
                "unresolved_ingredients": [{"reason": "ADAPTED_NUTRIENT_SERVICE_UNAVAILABLE"}],
            }
        try:
            return service.enrich(
                original_recipe, adapted_recipe, constraints or {},
                requested_servings=requested_servings, adaptation_id=adaptation_id,
                original_enrichment=original_enrichment, mapping_cache=mapping_cache,
                nutrient_record_cache=nutrient_record_cache,
            )
        except Exception as exc:
            return {
                "adaptation_id": adaptation_id,
                "change_detection_status": "UNRESOLVED",
                "changes": [],
                "unresolved_changes": [{"reason": f"ADAPTED_ENRICHMENT_FAILED_{type(exc).__name__}"}],
                "nutritional_calculation": {"calculation_status": "UNAVAILABLE"},
                "constraint_validation": {"constraint_status": "UNKNOWN"},
                "unresolved_ingredients": [{"reason": f"ADAPTED_ENRICHMENT_FAILED_{type(exc).__name__}"}],
            }

    def _culinary_recipe_context(self, recipe, *, substitution_metadata=None):
        """Add available graph descriptions/attributes without filling missing quantities."""
        result = copy.deepcopy(recipe) if isinstance(recipe, dict) else {}
        key = next((name for name in ("raw_ingredient_items", "ingredient_items", "ingredients")
                    if isinstance(result.get(name), list)), None)
        if key is None:
            return result
        enriched = []
        for source in result[key]:
            row = copy.deepcopy(source) if isinstance(source, dict) else {"name": str(source)}
            ingredient_id = next((row.get(name) for name in ("id", "ingredient_id", "food_id")
                                  if row.get(name) is not None), None)
            food = self.kg.ingredients.get(ingredient_id, {}) if ingredient_id is not None else {}
            if not (row.get("name") or row.get("ingredient_name") or row.get("description")) and food:
                row["name"] = food.get("name") or str(ingredient_id)
            attrs = copy.deepcopy(row.get("attributes") or row.get("culinary_attributes") or {})
            for attr in ("role", "ingredient_type", "substitution_group", "food_state",
                         "preparation_state", "processing_state"):
                if row.get(attr) is None and food.get(attr) is not None:
                    row[attr] = food[attr]
                if food.get(attr) is not None and attr not in attrs:
                    attrs[attr] = food[attr]
            # These are existing catalog bounds when present. Typical amounts
            # are deliberately not passed as hard culinary limits.
            for attr in ("min_amount_g", "max_amount_g", "minimum_amount_g", "maximum_amount_g"):
                if row.get(attr) is None and food.get(attr) is not None:
                    row[attr] = food[attr]
            if attrs:
                row["attributes"] = attrs
            enriched.append(row)
        result[key] = enriched
        if substitution_metadata is not None:
            result["substitution_metadata"] = copy.deepcopy(substitution_metadata)
        return result

    def _adapted_culinary_metadata(self, original_recipe, adapted_recipe, *, adaptation_id,
                                    structured_changes=None, substitution_metadata=None):
        """Return Step 6A metadata; this result cannot affect recipe selection."""
        service = getattr(self, "adapted_recipe_culinary_validation_service", None)
        if service is None:
            return {
                "status": "UNAVAILABLE", "adaptation_id": adaptation_id,
                "deterministic_guards": {"status": "INCOMPLETE", "findings": []},
                "slm_evaluation": {"status": "UNAVAILABLE", "error": "CULINARY_VALIDATOR_UNAVAILABLE"},
                "missing_information": ["CULINARY_VALIDATOR_UNAVAILABLE"],
                "review_reasons": ["CULINARY_VALIDATOR_UNAVAILABLE"],
            }
        try:
            original_context = self._culinary_recipe_context(original_recipe)
            adapted_context = self._culinary_recipe_context(
                adapted_recipe, substitution_metadata=substitution_metadata,
            )
            return service.validate(
                original_context, adapted_context, structured_changes,
                adaptation_id=adaptation_id,
                substitution_metadata=substitution_metadata,
            )
        except Exception as exc:
            return {
                "status": "UNAVAILABLE", "adaptation_id": adaptation_id,
                "deterministic_guards": {"status": "INCOMPLETE", "findings": []},
                "slm_evaluation": {"status": "UNAVAILABLE", "error": f"CULINARY_VALIDATION_FAILED_{type(exc).__name__}"},
                "missing_information": [],
                "review_reasons": [f"CULINARY_VALIDATION_FAILED_{type(exc).__name__}"],
            }

    def _final_recipe_acceptance_metadata(self, nutritional_enrichment, culinary_validation,
                                          constraints, adapted_recipe, *, adaptation_id=None):
        """Aggregate already-produced validation metadata without running dependencies."""
        policy = getattr(self, "final_recipe_acceptance_policy", None)
        if policy is None:
            return None
        result = policy.evaluate(
            nutritional_enrichment, culinary_validation,
            constraints=constraints or {}, adapted_recipe=adapted_recipe,
        )
        if adaptation_id is not None:
            result["adaptation_id"] = adaptation_id
        return result
    @staticmethod
    def _sanitize_conversation_history(value):
        """Keep a small, bounded transcript for resolving conversational follow-ups."""
        if not isinstance(value, list):
            return []
        history = []
        for turn in value[-12:]:
            if not isinstance(turn, dict) or turn.get("role") not in {"user", "assistant"}:
                continue
            item = {"role": turn["role"], "content": str(turn.get("content") or "")[:1200]}
            raw_context = turn.get("context")
            if turn["role"] == "assistant" and isinstance(raw_context, dict):
                context = {}
                for key in ("recipe",):
                    if isinstance(raw_context.get(key), str):
                        context[key] = raw_context[key][:200]
                for key in ("recipe_ingredient_names", "composition_ingredients"):
                    entries = raw_context.get(key)
                    if isinstance(entries, list):
                        context[key] = [str(entry)[:120] for entry in entries[:30]
                                        if isinstance(entry, (str, int, float))]
                ingredient_items = raw_context.get("recipe_ingredient_items")
                if isinstance(ingredient_items, list):
                    context["recipe_ingredient_items"] = []
                    for entry in ingredient_items[:30]:
                        if not isinstance(entry, dict):
                            continue
                        name = entry.get("name")
                        quantity = entry.get("quantity_raw")
                        context["recipe_ingredient_items"].append({
                            "name": str(name)[:120] if name is not None else None,
                            "quantity_raw": str(quantity)[:120] if quantity is not None else None,
                        })
                if raw_context.get("recipe_servings") is not None:
                    context["recipe_servings"] = str(raw_context["recipe_servings"])[:40]
                if isinstance(raw_context.get("dataset_scope"), str):
                    context["dataset_scope"] = raw_context["dataset_scope"][:40]
                lookups = raw_context.get("ingredient_lookups")
                if isinstance(lookups, list):
                    context["ingredient_lookups"] = []
                    for lookup in lookups[:10]:
                        if not isinstance(lookup, dict):
                            continue
                        record_names = []
                        for record in lookup.get("records", [])[:3] if isinstance(lookup.get("records"), list) else []:
                            if isinstance(record, dict):
                                name = str(record.get("name") or "")[:120]
                                if name:
                                    record_names.append({
                                        "name": name,
                                        "dataset_key": str(record.get("dataset_key") or "")[:40],
                                    })
                            elif isinstance(record, str):
                                record_names.append(record[:120])
                        context["ingredient_lookups"].append({
                            "name": str(lookup.get("name") or "")[:120],
                            "status": str(lookup.get("status") or "")[:40],
                            "records": record_names,
                        })
                if context:
                    item["context"] = context
            history.append(item)
        return history

    @staticmethod
    def _dataset_scope_label(scope):
        return {
            "foundation_foods": "USDA Foundation Foods",
            "sr_legacy": "USDA SR Legacy",
            "all": "USDA FoodData Central (Foundation Foods and SR Legacy)",
        }.get(scope, "USDA FoodData Central (Foundation Foods and SR Legacy)")

    def _lookup_ingredients(self, queries, dataset_scope="all"):
        results, used_ids, trace = [], set(), []
        for query in queries[:self.planner.MAX_INGREDIENT_QUERIES]:
            exact_candidates = self.retriever.resolve_ingredient_candidates(
                query, k=5, dataset_scope=dataset_scope,
            )
            exact_ids = [ingredient_id for ingredient_id, score in exact_candidates
                         if score >= 1.0 and ingredient_id in self.kg.ingredients]
            if len(exact_ids) > 1:
                matches = []
                for ingredient_id in exact_ids[:5]:
                    data = self.kg.ingredients[ingredient_id]
                    matches.append({
                        "id": ingredient_id,
                        "name": data.get("name", ingredient_id),
                        "description": data.get("fdc_description", data.get("name", ingredient_id)),
                        "source": data.get("source", {}),
                    })
                results.append({
                    "query": query, "found": False, "ambiguous": True,
                    "matches": matches,
                    "message": "Multiple separate USDA records match this name. Specify a dataset or food state.",
                })
                trace.append(
                    f"Neo4j {self._dataset_scope_label(dataset_scope)} retrieval: "
                    f"multiple exact source records for {query}; no record selected."
                )
                continue
            ranked = self.retriever.retrieve_ingredients(
                query, k=3, dataset_scope=dataset_scope,
            )
            match = next(((iid, score) for iid, score in ranked if score >= 0.8 and iid not in used_ids), None)
            if match is None:
                results.append({"query": query, "found": False,
                                "message": "No matching ingredient record exists in the current catalog."})
                trace.append(f"Neo4j {self._dataset_scope_label(dataset_scope)} retrieval: no match for {query}.")
                continue
            iid, score = match
            used_ids.add(iid)
            data = self.kg.ingredients[iid]
            nutrients = {key: value for key, value in data.get("nutrients_per_100g", {}).items()
                         if value is not None}
            nutrient_units = {**NUTRIENT_UNITS, **data.get("nutrient_units", {})}
            results.append({"query": query, "found": True, "id": iid,
                            "name": data.get("name", iid), "match_score": score,
                            "description": data.get("fdc_description", data.get("name", iid)),
                            "ingredient_type": data.get("ingredient_type"),
                            "substitution_group": data.get("substitution_group"),
                            "nutrients_per_100g": nutrients, "source": data.get("source", {}),
                            "nutrient_units": nutrient_units,
                            "nutrient_names": data.get("nutrient_names", {})})
            trace.append(
                f"Neo4j {data.get('source', {}).get('name', self._dataset_scope_label(dataset_scope))} "
                f"retrieval: {data.get('name', iid)} (score {score:.2f})."
            )
        return results, trace

    @staticmethod
    def _normalized_food_label(value):
        return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))

    @staticmethod
    def _limit_catalog_sources(records, per_source=3):
        selected, counts = [], {}
        for record in records:
            source = record.get("source") or {}
            source_key = source.get("dataset_key") or source.get("name") or "unknown"
            if counts.get(source_key, 0) >= per_source:
                continue
            selected.append(record)
            counts[source_key] = counts.get(source_key, 0) + 1
        return selected

    def _lookup_catalog_membership(self, queries, dataset_scope="all", max_queries=None):
        """Check named foods against the SLM-selected USDA catalog scope."""
        results, trace = [], []
        query_limit = (self.planner.MAX_INGREDIENT_QUERIES if max_queries is None
                       else max(0, int(max_queries)))
        for query in queries[:query_limit]:
            ranked = self.retriever.resolve_ingredient_candidates(
                query, k=5, dataset_scope=dataset_scope,
            )
            records = []
            for ingredient_id, score in ranked:
                data = self.kg.ingredients.get(ingredient_id)
                if data is None:
                    continue
                records.append({
                    "id": ingredient_id,
                    "name": data.get("name", ingredient_id),
                    "description": data.get("fdc_description", data.get("name", ingredient_id)),
                    "ingredient_type": data.get("ingredient_type"),
                    "source": data.get("source", {}),
                    "match_score": score,
                    "labels": [data.get("name", ingredient_id), *(data.get("aliases") or [])],
                })

            normalized_query = self._normalized_food_label(query)
            exact = [record for record in records
                     if any(self._normalized_food_label(label) == normalized_query
                            for label in record["labels"])]
            if exact:
                status = "exact"
                matches = self._limit_catalog_sources(exact)
            elif records:
                status = "matches_found"
                matches = self._limit_catalog_sources(records)
            else:
                status = "not_found"
                matches = []

            # Related full-text hits may help the user identify a different
            # food state, but they are explicitly kept separate from matches.
            related = []
            if not records:
                related_ids = self.retriever.retrieve_ingredients(
                    query, k=3, dataset_scope=dataset_scope,
                )
                for ingredient_id, score in related_ids:
                    data = self.kg.ingredients.get(ingredient_id)
                    if data is None:
                        continue
                    related.append({
                        "id": ingredient_id,
                        "name": data.get("name", ingredient_id),
                        "description": data.get("fdc_description", data.get("name", ingredient_id)),
                        "source": data.get("source", {}),
                        "match_score": score,
                    })

            result = {
                "query": query,
                "found": bool(records),
                "catalog_lookup": True,
                "match_status": status,
                "matches": matches,
                "related_matches": related,
            }
            if status == "not_found":
                result["message"] = (
                    f"No matching record was found in {self._dataset_scope_label(dataset_scope)} for this name."
                )
                trace.append(
                    f"Neo4j {self._dataset_scope_label(dataset_scope)} membership lookup: "
                    f"no matching record for {query}."
                )
            else:
                trace.append(
                    f"Neo4j {self._dataset_scope_label(dataset_scope)} membership lookup: "
                    f"{status.replace('_', ' ')} for {query} "
                    f"({len(matches)} record(s) shown)."
                )
            results.append(result)
        return results, trace

    def _lookup_source_recipe_ingredients(self, missing_data):
        """Check unweighted source ingredient names in both USDA food datasets."""
        names, seen = [], set()
        for raw_name in missing_data.get("raw_ingredient_names", []):
            name = str(raw_name or "").strip()[:120]
            normalized = self._normalized_food_label(name)
            if not normalized or normalized in seen:
                continue
            names.append(name)
            seen.add(normalized)
            if len(names) >= self.MAX_SOURCE_INGREDIENT_LOOKUPS:
                break
        if not names:
            return [], []
        return self._lookup_catalog_membership(
            names, dataset_scope="all", max_queries=len(names),
        )

    def _composition_recipe(self, query, recipe_query, ingredient_ids):
        """Choose the named local recipe as a base, or an exact composition match."""
        if recipe_query:
            ranked = self.retriever.retrieve_nodes(
                recipe_query, k=self.planner.MAX_RECIPE_CANDIDATES,
            )
            candidates = []
            for candidate_id, score in ranked:
                if candidate_id not in self.kg.recipes or score <= 0:
                    continue
                recipe_items = self.kg.recipe_ingredients(candidate_id)
                if any(not isinstance(amount, (int, float)) or amount < 0
                       for _, amount in recipe_items):
                    continue
                candidate_food_ids = {food_id for food_id, _ in recipe_items}
                overlap = len(ingredient_ids & candidate_food_ids)
                candidates.append((candidate_id, overlap, score))
            # An explicitly requested dish is a base composition. Prefer a
            # retrieved recipe that already contains more of the named foods;
            # foods absent from that base are appended by the caller.
            if candidates:
                return max(candidates, key=lambda item: (item[1], item[2]))[0]
            return None
        matching = []
        for recipe_id in self.kg.recipes:
            recipe_items = self.kg.recipe_ingredients(recipe_id)
            if any(not isinstance(amount, (int, float)) or amount < 0 for _, amount in recipe_items):
                continue
            recipe_ingredient_ids = {ingredient_id for ingredient_id, _ in recipe_items}
            if ingredient_ids.issubset(recipe_ingredient_ids):
                matching.append(recipe_id)
        if not matching:
            return None
        ranked = self.retriever.retrieve_nodes(
            query, k=self.planner.MAX_RECIPE_CANDIDATES,
        )
        scores = {recipe_id: score for recipe_id, score in ranked}
        return max(matching, key=lambda recipe_id: scores.get(recipe_id, 0.0))

    def _resolve_composition_ingredients(self, requested, dataset_scope, ingredient_rows, trace):
        """Resolve each stated food while retaining missing and ambiguous rows for the response."""
        resolved, missing, ambiguous = [], [], []
        for item in requested:
            food_query = str(item.get("query", "")).strip()
            amount_g = item.get("amount_g")
            if not food_query or not isinstance(amount_g, (int, float)) or amount_g <= 0:
                missing.append(food_query or "an ingredient or amount I could not parse")
                continue
            canonical_id = item.get("ingredient_id")
            if canonical_id in self.kg.ingredients:
                data = self.kg.ingredients[canonical_id]
                resolved.append((canonical_id, float(amount_g)))
                ingredient_rows.append({
                    "id": canonical_id, "query": food_query or data.get("name", canonical_id),
                    "name": data.get("name", canonical_id),
                    "description": data.get("fdc_description", data.get("name", canonical_id)),
                    "amount_g": float(amount_g), "found": True, "match_score": 1.0,
                    "ingredient_type": data.get("ingredient_type"),
                    "source": data.get("source", {}),
                })
                trace.append(
                    f"Neo4j {data.get('source', {}).get('name', 'USDA FoodData Central')} retrieval: "
                    f"{data.get('name', canonical_id)} "
                    f"(validated catalog ID; FDC {data.get('source', {}).get('fdc_id')})."
                )
                continue
            ranked = self.retriever.resolve_ingredient_candidates(
                food_query, k=5, dataset_scope=dataset_scope,
            )
            if not ranked:
                missing.append(food_query)
                ingredient_rows.append({"query": food_query, "amount_g": amount_g, "found": False})
                trace.append(
                    f"Neo4j {self._dataset_scope_label(dataset_scope)} retrieval: no match for {food_query}."
                )
                continue
            top_score = ranked[0][1]
            tied = [(food_id, score) for food_id, score in ranked if abs(score - top_score) < 1e-9]
            if len(tied) > 1:
                options = [
                    {"id": food_id,
                     "name": self.kg.ingredients.get(food_id, {}).get("name", food_id),
                     "description": self.kg.ingredients.get(food_id, {}).get("fdc_description", ""),
                     "source": self.kg.ingredients.get(food_id, {}).get("source", {})}
                    for food_id, _ in tied
                ]
                ambiguous.append({"query": food_query, "amount_g": amount_g, "options": options})
                ingredient_rows.append({
                    "query": food_query, "amount_g": amount_g, "found": False,
                    "ambiguous_options": options,
                })
                continue
            food_id, score = ranked[0]
            data = self.kg.ingredients.get(food_id)
            if not data:
                missing.append(food_query)
                continue
            resolved.append((food_id, float(amount_g)))
            ingredient_rows.append({
                "id": food_id, "query": food_query,
                "name": data.get("name", food_id),
                "description": data.get("fdc_description", data.get("name", food_id)),
                "amount_g": float(amount_g), "found": True, "match_score": score,
                "ingredient_type": data.get("ingredient_type"),
                "source": data.get("source", {}),
            })
            trace.append(
                f"Neo4j {data.get('source', {}).get('name', 'USDA FoodData Central')} retrieval: "
                f"{data.get('name', food_id)} "
                f"(match score {score:.2f}; FDC {data.get('source', {}).get('fdc_id')})."
            )
        return resolved, missing, ambiguous

    def _execute_composition_question(
        self, query, plan, constraints, servings, generator, slm_used, response_context,
    ):
        trace = []
        requested = plan.get("composition_ingredients") or []
        assessment = {
            "status": "NEEDS_DETAILS", "recipe_source": None, "recipe": None,
            "ingredients": [], "nutrients_per_serving": {},
            "nutrients_for_requested_servings": {}, "unavailable_nutrients": [],
            "constraint_check": None, "method": [], "method_source": None,
        }
        if plan.get("generated_recipe_invalid"):
            assessment.update({"status": "DATA_MISSING", "missing_ingredients": []})
            assessment["answer"] = (
                "The generated composition included a food reference or amount that could not be validated against "
                "the local catalog. I withheld the totals; please try again or provide known foods and amounts."
            )
        elif plan.get("composition_overflow"):
            assessment["answer"] = (
                f"I found more than {self.planner.MAX_COMPOSITION_INGREDIENTS} weighted ingredients. "
                "Please split the composition into smaller requests so I can verify every food and amount."
            )
        elif not requested:
            if plan.get("generated_recipe"):
                assessment["answer"] = (
                    "I couldn't form a complete new composition from the available FoodData Central records. "
                    "Please name the foods you want to use and give an amount for each, or clarify the dish you want."
                )
            elif plan.get("composition_parse_error"):
                assessment["answer"] = (
                    "I couldn't reliably match every weight to one food. Please write each amount next to its food, "
                    "for example ‘40 g shrimp and 40 g crab’."
                )
            else:
                assessment["answer"] = (
                    "Please list each food with a weight, such as ‘40 g shrimp and 40 g crab’. "
                    "I can calculate a composition only when its foods and amounts are explicit and found in the selected USDA data scope."
                )
        else:
            resolved, missing, ambiguous = self._resolve_composition_ingredients(
                requested, plan.get("dataset_scope", "all"), assessment["ingredients"], trace,
            )

            if ambiguous:
                assessment.update({"status": "AMBIGUOUS_INGREDIENT", "ambiguous_ingredients": ambiguous})
                details = "; ".join(
                    f"{item['query']}: {', '.join(option['name'] for option in item['options'])}"
                    for item in ambiguous
                )
                assessment["answer"] = (
                    f"I found multiple FoodData Central records for {details}. Choose the food state or dataset/source "
                    "before I calculate the combined nutrient totals."
                )
            elif missing or len(resolved) != len(requested):
                assessment.update({"status": "DATA_MISSING", "missing_ingredients": missing})
                assessment["answer"] = (
                    f"I couldn't find data in the selected USDA data scope for {', '.join(missing) or 'one or more requested foods'}. "
                    "I can't calculate complete totals for this combination from partial data."
                )
            else:
                requested_amounts = {}
                for food_id, amount_g in resolved:
                    requested_amounts[food_id] = requested_amounts.get(food_id, 0.0) + amount_g
                recipe_id = (None if plan.get("generated_recipe") else
                             self._composition_recipe(query, plan.get("recipe_query"), set(requested_amounts)))
                recipe_data = self.kg.recipes.get(recipe_id) if recipe_id else None
                method_check = None
                if recipe_data:
                    base = self.kg.recipe_ingredients(recipe_id)
                    amounts = {}
                    order = []
                    for food_id, amount_g in base:
                        if food_id not in amounts:
                            order.append(food_id)
                            amounts[food_id] = 0.0
                        amounts[food_id] += amount_g
                    for food_id, amount_g in requested_amounts.items():
                        amounts[food_id] = amount_g
                        if food_id not in order:
                            order.append(food_id)
                    composition = [(food_id, amounts[food_id]) for food_id in order]
                    recipe_name = recipe_data.get("name", recipe_id)
                    added_ids = [food_id for food_id in requested_amounts
                                 if food_id not in {base_id for base_id, _ in base}]
                    method = recipe_data.get("method") or []
                    if isinstance(method, str):
                        method = [method]
                    method_source = "local_recipe"
                    trace.append(
                        f"Neo4j recipe graph: using {recipe_name} as the base; explicit user weights override matching portions."
                    )
                    stored_method_check = check_method_faithfulness(
                        "\n".join(method), composition, self.kg.ingredients,
                    )
                    if added_ids or not stored_method_check["passed"]:
                        if slm_used and hasattr(generator, "generate_method"):
                            try:
                                proposed_method = generator.generate_method(
                                    query, composition, self.kg.ingredients, recipe_name,
                                    base_method=method if stored_method_check["passed"] else [],
                                )
                                proposed_check = check_method_faithfulness(
                                    proposed_method, composition, self.kg.ingredients,
                                )
                            except Exception:
                                proposed_method = None
                                proposed_check = {"passed": False, "issues": ["SLM method generation failed"]}
                            if proposed_method and proposed_check["passed"]:
                                method = proposed_method.splitlines()
                                method_source = "slm_adapted"
                                method_check = proposed_check
                                trace.append(
                                    "SLM adapted the local recipe method to include added validated foods; "
                                    "the ingredient and amount check passed."
                                )
                            else:
                                fallback_method = self.deterministic_generator.generate_method(
                                    query, composition, self.kg.ingredients, recipe_name,
                                )
                                method = [fallback_method]
                                method_check = check_method_faithfulness(
                                    fallback_method, composition, self.kg.ingredients,
                                )
                                method_source = "local_recipe_adapted"
                                trace.append(
                                    "SLM method did not pass ingredient/amount validation; "
                                    "a deterministic method grounded in the validated composition was used."
                                )
                        else:
                            method = [self.deterministic_generator.generate_method(
                                query, composition, self.kg.ingredients, recipe_name,
                            )]
                            method_check = check_method_faithfulness(
                                method[0], composition, self.kg.ingredients,
                            )
                            method_source = "local_recipe_adapted"
                            trace.append(
                                "The stored method did not match the validated composition; "
                                "a deterministic grounded method was used instead."
                            )
                    else:
                        method_check = stored_method_check
                else:
                    composition = list(requested_amounts.items())
                    foods_name = " and ".join(
                        self.kg.ingredients[food_id].get("name", food_id) for food_id, _ in composition
                    )
                    requested_dish = plan.get("recipe_query")
                    if plan.get("generated_recipe"):
                        recipe_name = "Suggested composition"
                    else:
                        recipe_name = (f"Suggested {requested_dish} using {foods_name}"
                                       if requested_dish else f"{foods_name} combination")
                    method_source = "deterministic"
                    method = []
                    method_check = None
                    trace.append("Neo4j recipe graph: no matching stored recipe was selected; preparing a new method from the validated composition.")
                    if slm_used and hasattr(generator, "generate_method"):
                        try:
                            proposed_method = generator.generate_method(
                                query, composition, self.kg.ingredients, recipe_name,
                            )
                            proposed_check = check_method_faithfulness(
                                proposed_method, composition, self.kg.ingredients,
                            )
                        except Exception:
                            proposed_method = None
                            proposed_check = {"passed": False, "issues": ["SLM method generation failed"]}
                        if proposed_method and proposed_check["passed"]:
                            method = proposed_method.splitlines()
                            method_source = "slm_generated"
                            method_check = proposed_check
                            trace.append("SLM suggested cooking instructions from the validated ingredient list; nutrient totals remain calculated by ConstraintEngine.")
                        else:
                            method = [self.deterministic_generator.generate_method(
                                query, composition, self.kg.ingredients, recipe_name,
                            )]
                            method_check = check_method_faithfulness(
                                method[0], composition, self.kg.ingredients,
                            )
                            trace.append("SLM method did not pass the ingredient/quantity check; deterministic cooking instructions were used.")
                    else:
                        method = [self.deterministic_generator.generate_method(
                            query, composition, self.kg.ingredients, recipe_name,
                        )]
                        method_check = check_method_faithfulness(
                            method[0], composition, self.kg.ingredients,
                        )
                        trace.append("SLM recipe writing was not selected; deterministic cooking instructions were used.")

                totals = self.engine.nutrient_totals(composition)
                totals_for_servings = (dict(totals) if servings == 1 else
                                       self.engine.nutrient_totals(composition, multiplier=servings))
                unavailable = [key for key in NUTRIENT_UNITS if key not in totals]
                check = self.engine.evaluate(composition, constraints, servings=servings) if constraints else None
                status = "CALCULATED" if not unavailable else "PARTIAL"
                if check:
                    status = "PASS" if check["passed"] is True and not check["not_evaluated"] else (
                        "FAIL" if check["passed"] is False else "UNVERIFIED"
                    )
                data_states = sorted({
                    self.kg.ingredients[food_id].get("food_state", "")
                    for food_id, _ in composition
                    if self.kg.ingredients.get(food_id, {}).get("food_state")
                })
                recipe_source = ("local_recipe" if recipe_data else
                                 "slm_generated_composition" if plan.get("generated_recipe") else method_source)
                recipe_description = (recipe_data.get("description", "") if recipe_data else
                                      "Suggested composition built from validated FoodData Central records; not a stored recipe."
                                      if plan.get("generated_recipe") else "")
                assessment.update({
                    "status": status, "recipe_source": recipe_source,
                    "recipe": {"id": recipe_id, "name": recipe_name,
                               "source": recipe_data.get("source", {}) if recipe_data else {},
                               "description": recipe_description},
                    "ingredients": [
                        {"id": food_id, "name": self.kg.ingredients.get(food_id, {}).get("name", food_id),
                         "amount_g": amount_g, "source": self.kg.ingredients.get(food_id, {}).get("source", {}),
                         "ingredient_type": self.kg.ingredients.get(food_id, {}).get("ingredient_type")}
                        for food_id, amount_g in composition
                    ],
                    "nutrients_per_serving": totals,
                    "nutrients_for_requested_servings": totals_for_servings,
                    "unavailable_nutrients": unavailable,
                    "nutrient_units": NUTRIENT_UNITS,
                    "constraint_check": check, "method": method, "method_source": method_source,
                    "method_check": method_check,
                    "food_states": data_states,
                })
                trace.append("Constraint engine: calculated nutrient totals from the recorded per-100 g values and validated gram amounts.")
                if check:
                    trace.append(f"Constraint engine: explicit limits {status.lower()} for this composition.")
                ingredient_summary = ", ".join(
                    f"{self.kg.ingredients[food_id].get('name', food_id)} {amount_g:g} g"
                    for food_id, amount_g in requested_amounts.items()
                )
                answer = (
                    f"I calculated the combination using {ingredient_summary} per serving. "
                    "The nutrient totals are shown below. "
                )
                if recipe_data:
                    if added_ids:
                        added_names = ", ".join(
                            self.kg.ingredients[food_id].get("name", food_id) for food_id in added_ids
                        )
                        answer += (
                            f"I used {recipe_name} as the local recipe base, kept its other ingredients, "
                            f"applied your stated weights, and added {added_names}. "
                        )
                    else:
                        answer += (
                            f"I found {recipe_name} in the local recipe graph and applied your stated food weights. "
                        )
                elif plan.get("generated_recipe"):
                    answer += (
                        "No stored recipe matched. Qwen proposed this composition using the selected FoodData Central records; "
                        "the nutrient totals were calculated from those individual records. This is a suggested prototype composition, "
                        "not a clinically validated recipe. "
                    )
                else:
                    answer += "No matching local recipe was selected, so I prepared a suggested method from the validated foods. "
                answer += "Totals use each USDA record's stated food condition; records were not merged, and cooking yield and nutrient retention are not modeled."
                lower_sodium_request = re.search(
                    r"\b(?:lower|low|less|reduced)\s*[- ]?sodium\b|\bless\s+salt\b|\breduce\s+sodium\b",
                    query, flags=re.I,
                )
                if lower_sodium_request and "sodium_mg" not in constraints:
                    if "sodium_mg" not in totals:
                        answer += " Sodium data is missing for at least one selected food, so I cannot verify a lower-sodium result."
                    else:
                        answer += (
                            " I calculated sodium, but without a numeric sodium limit or a comparison recipe "
                            "I cannot verify that this is lower-sodium."
                        )
                if check and status == "FAIL":
                    answer += " The composition fails an entered limit or recorded prototype portion range."
                elif check and status == "PASS":
                    answer += (
                        " The composition meets the explicit numeric limits entered; this is a calculation against those limits, "
                        "not a personalized clinical recommendation."
                    )
                elif check and status == "UNVERIFIED":
                    answer += " At least one entered limit could not be verified from the available nutrient data."
                elif not constraints:
                    answer += " No personal numeric meal limit was entered, so dietary suitability was not evaluated."
                if unavailable:
                    answer += f" Missing nutrient values were omitted: {', '.join(unavailable)}."
                assessment["answer"] = answer
                if (recipe_data and getattr(self, "adapted_recipe_nutrition_enrichment_enabled", False)):
                    adapted_recipe = {
                        "id": recipe_id,
                        "name": recipe_name,
                        "ingredients": copy.deepcopy(assessment["ingredients"]),
                        "servings": servings,
                        "method": copy.deepcopy(assessment.get("method")),
                    }
                    assessment["adapted_recipe_nutrition_enrichment"] = self._adapted_nutrition_metadata(
                        recipe_data, adapted_recipe, constraints, servings,
                        adaptation_id=f"{recipe_id}:composition",
                    )
                if (recipe_data and getattr(
                        self, "adapted_recipe_culinary_validation_enabled", False)):
                    adapted_recipe = {
                        "id": recipe_id,
                        "name": recipe_name,
                        "ingredients": copy.deepcopy(assessment["ingredients"]),
                        "servings": servings,
                        "method": copy.deepcopy(assessment.get("method")),
                    }
                    assessment["adapted_recipe_culinary_validation"] = self._adapted_culinary_metadata(
                        recipe_data, adapted_recipe,
                        adaptation_id=f"{recipe_id}:composition",
                        substitution_metadata={"method_source": assessment.get("method_source")},
                    )

        trace.insert(0, "SLM interpreted the user question." if slm_used
                     else "Deterministic request parser interpreted the user question.")
        return {
            **response_context, "constraints_applied": bool(constraints),
            "answer": assessment["answer"],
            "composition_assessment": assessment, "tool_trace": trace,
            "recipe": None, "missing_data": None, "evaluations": [], "selected": None,
        }

    def _execute_meal_plan(self, query, plan, constraints, servings, generator, slm_used):
        requests = plan.get("recipe_requests", [])[:self.planner.MAX_MEALS]
        target_count = max(1, min(int(plan.get("requested_count", len(requests) or 3)),
                                  self.planner.MAX_MEALS))
        existing = {entry.get("recipe_id") for entry in requests}
        if len(requests) < target_count:
            fallback = self.planner.fallback_plan(f"suggest {target_count} meals")
            requests.extend(entry for entry in fallback["recipe_requests"]
                            if entry.get("recipe_id") not in existing)
            requests = requests[:target_count]

        meal_limits = {name: rule for name, rule in constraints.items()
                       if rule.get("scope", "meal") == "meal"}
        remaining_limits = {name: rule for name, rule in constraints.items()
                            if rule.get("scope") == "remaining"}
        meal_items, combined, trace, evidence = [], [], [], []
        pipeline = self.runtime.create_pipeline(generator)
        for index, request in enumerate(requests):
            rid = request.get("recipe_id")
            search_text = rid or request.get("query", query)
            result = pipeline.run(search_text, meal_limits, use_llm=False, servings=1)
            if result.get("missing_data"):
                missing = result["missing_data"]
                meal_name = missing.get("recipe_name") or request.get("query") or "Requested dish"
                meal_items.append({"slot": request.get("slot") or f"Meal {index + 1}",
                                   "id": missing.get("recipe_id") or rid, "name": meal_name,
                                   "status": "UNVERIFIED", "description": missing.get("message", "Recipe data is unavailable."),
                                   "ingredients": [], "nutrients_per_serving": {}, "reasons": [],
                                   "source": missing.get("source", {}),
                                   "raw_ingredient_names": missing.get("raw_ingredient_names", []),
                                   "raw_ingredient_quantities": missing.get("raw_ingredient_quantities", []),
                                   "raw_ingredient_items": missing.get("raw_ingredient_items", []),
                                   "recipe_servings": missing.get("recipe_servings"),
                                   "recipe_servings_raw": missing.get("recipe_servings_raw"),
                                   "recipe_yield": missing.get("recipe_yield"),
                                   "method": missing.get("method", []),
                                   "minutes": missing.get("minutes"),
                                   "cook_time": missing.get("cook_time"),
                                   "prep_time": missing.get("prep_time"),
                                   "total_time": missing.get("total_time"),
                                   "nutrition_status": missing.get("nutrition_status"),
                                   "missing_data": missing})
                evidence.append({"request": request, "lookup": missing})
                trace.append(
                    f"Neo4j graph retrieval: found {meal_name}, but its ingredient quantities or nutrient links are incomplete."
                )
                continue

            recipe_id = result["recipe_id"]
            recipe_data = self.kg.recipes[recipe_id]
            selected = result.get("selected")
            candidate = selected or (max(result.get("evaluations", []), key=lambda item: item["compatibility"])
                                     if result.get("evaluations") else None)
            actual_ingredients = candidate["ingredients"] if candidate else self.kg.recipe_ingredients(recipe_id)
            combined.extend(actual_ingredients)
            passed = candidate.get("passed") if candidate else None
            not_evaluated = candidate.get("not_evaluated", []) if candidate else []
            status = ("UNVERIFIED" if not_evaluated or candidate is None or passed is False else
                      "PASS" if meal_limits and passed is True else
                      "FAIL" if meal_limits and passed is False else
                      "NOT_EVALUATED")
            stored_method = recipe_data.get("method", [])
            if isinstance(stored_method, str):
                stored_method = [stored_method]
            method_check = check_method_faithfulness(
                "\n".join(stored_method), actual_ingredients, self.kg.ingredients,
            )
            if not method_check["passed"]:
                stored_method = [self.deterministic_generator.generate_method(
                    query, actual_ingredients, self.kg.ingredients, recipe_data.get("name", recipe_id),
                )]
                method_check = check_method_faithfulness(
                    "\n".join(stored_method), actual_ingredients, self.kg.ingredients,
                )
            meal = {
                "slot": request.get("slot") or f"Meal {index + 1}", "id": recipe_id,
                "name": recipe_data.get("name", recipe_id), "description": recipe_data.get("description", ""),
                "source": recipe_data.get("source", {}), "method": stored_method,
                "method_check": method_check,
                "status": status,
                "ingredients": [{"id": iid, "name": self.kg.ingredients[iid].get("name", iid),
                                 "amount_g": amount, "source": self.kg.ingredients[iid].get("source", {}),
                                 "ingredient_type": self.kg.ingredients[iid].get("ingredient_type"),
                                 "substitution_group": self.kg.ingredients[iid].get("substitution_group")}
                                for iid, amount in actual_ingredients],
                "nutrients_per_serving": candidate.get("totals", {}) if candidate else {},
                "reasons": candidate.get("reasons", []) if candidate else [],
                "not_evaluated": candidate.get("not_evaluated", []) if candidate else [],
                "retrieval_score": result.get("retrieval_score", 0),
            }
            meal_items.append(meal)
            evidence.append({"slot": meal["slot"], "recipe": meal["name"], "status": status,
                             "nutrients_per_serving": meal["nutrients_per_serving"],
                             "constraint_reasons": meal["reasons"],
                             "retrieved_graph_context": result.get("retrieval_context", [])})
            trace.append(f"Neo4j full-text and graph-neighborhood retrieval: {meal['name']} (score {meal['retrieval_score']:.2f}).")
            trace.append(f"Candidate generator: evaluated {len(result.get('evaluations', []))} validated ingredient variants.")
            trace.append(f"Constraint engine: {status.lower()} for {meal['slot']}.")

        statuses = [item["status"] for item in meal_items]
        has_unverified_meal = "UNVERIFIED" in statuses
        if has_unverified_meal:
            remaining_check = {
                "passed": None, "reasons": [],
                "not_evaluated": ["One or more selected recipes have no verified ingredient quantities."],
            }
            daily_totals = {}
        else:
            remaining_check = self.engine.evaluate(combined, remaining_limits, servings=servings)
            daily_totals = self.engine.nutrient_totals(combined, multiplier=servings)
        if "FAIL" in statuses or remaining_limits and remaining_check["passed"] is False:
            status = "FAIL"
        elif has_unverified_meal or remaining_limits and remaining_check["passed"] is None:
            status = "UNVERIFIED"
        elif not meal_limits and not remaining_limits:
            status = "NOT_EVALUATED"
        else:
            status = "PASS"
        if remaining_limits:
            remaining_status = "PASS" if remaining_check["passed"] is True else "FAIL" if remaining_check["passed"] is False else "UNVERIFIED"
            trace.append(f"Constraint engine: remaining-today limits {remaining_status.lower()} across the selected plan.")
        answer = ("I checked the requested catalog items and their nutrient records. The menu is illustrative and is not a personalized medical plan.")
        if status == "FAIL":
            answer += " At least one selected meal does not meet the explicit limits entered."
        elif status == "UNVERIFIED":
            answer += " Some requested recipe or nutrient data could not be verified."
        elif status == "PASS":
            answer += " The selected meals meet the explicit numeric limits entered."
        elif status == "NOT_EVALUATED":
            answer += " No personal numeric meal or remaining-intake limits were entered, so suitability against personal limits was not evaluated."
        return {"meals": meal_items, "status": status, "servings": servings,
                "daily_totals_for_requested_servings": daily_totals,
                "remaining_reasons": remaining_check["reasons"],
                "remaining_not_evaluated": remaining_check["not_evaluated"],
                "answer": answer, "tool_trace": trace}

    def chat(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object.")
        query = str(payload.get("query", "")).strip()
        if not query:
            raise ValueError("Type a question first.")
        conditions = payload.get("conditions", [])
        if not isinstance(conditions, list):
            conditions = []
        conditions = list(dict.fromkeys(c for c in conditions
                                        if isinstance(c, str) and c in ALLOWED_CONDITIONS))
        servings = payload.get("servings", 1)
        try:
            servings = int(servings)
        except (ValueError, TypeError):
            raise ValueError("Servings must be a whole number.") from None
        if not 1 <= servings <= 20:
            raise ValueError("Servings must be between 1 and 20.")

        constraints = parse_budgets(payload.get("meal_limits", {}), "meal")
        constraints.update(parse_budgets(payload.get("remaining_limits", {}), "remaining"))
        rules, conflicts = resolve_reference_rules(conditions, self.profiles)
        conversation_history = self._sanitize_conversation_history(payload.get("conversation_history"))
        plan, generator, slm_warning, slm_used = self.planner.plan(
            query, bool(payload.get("use_slm")),
            {"conditions": conditions, "explicit_limits": constraints, "servings": servings,
             "conversation_history": conversation_history},
        )
        planner_name = "SLM" if slm_used else "Deterministic request parser"
        trace = [f"{planner_name} classified the request as {plan.get('intent', 'unknown')}."]
        response_context = {
            "query": query,
            "servings": servings,
            "conditions": [self.profiles[condition]["label"] for condition in conditions],
            "reference_guidance": [
                {"nutrient": rule["nutrient"], "type": rule["type"], "scope": rule["scope"],
                 "value": rule.get("max", rule.get("min")), "unit": rule.get("unit"),
                 "source": rule["source"]}
                for rule in rules
            ],
            "profile_conflicts": conflicts,
            "active_budgets": constraints,
            "slm_warning": slm_warning,
        }
        if plan.get("intent") == "single_recipe":
            requests = plan.get("recipe_requests") or []
            if requests:
                trace.append(f"Recipe target extracted: {requests[0].get('query') or requests[0].get('recipe_id') or query}.")
                if slm_used:
                    trace.append("Recipe target was checked against names in the current or recent user messages.")
        elif plan.get("intent") == "catalog_question":
            names = plan.get("ingredient_queries") or []
            trace.append("Ingredient names to check: " + (", ".join(names) if names else "none identified; clarification needed") + ".")
            trace.append(
                "USDA data scope selected: "
                + self._dataset_scope_label(plan.get("dataset_scope", "all")) + "."
            )
        if plan.get("intent") == "unsupported":
            trace.append("No grounded catalog action was selected; RenAlly asked for clarification instead of choosing a recipe.")
            if plan.get("rejected_intent") == "single_recipe":
                target = plan.get("rejected_recipe_target") or "unidentified recipe"
                trace.append(f"Rejected recipe target '{target}' because its name was not grounded in the current or recent user messages.")
            answer = str(plan.get("clarification_question") or (
                "I couldn't tell what you want me to check. Please name a dish or list the ingredients."
            ))[:500]
            return {
                **response_context, "constraints_applied": False,
                "answer": answer, "tool_trace": trace, "recipe": None,
                "missing_data": None, "meal_plan": None, "evaluations": [], "selected": None,
            }
        if plan.get("intent") == "new_recipe":
            requested_dish = plan.get("recipe_query") or query
            stored_match = self.retriever.retrieve_recipe(requested_dish)
            if stored_match:
                stored_recipe = self.kg.recipes.get(stored_match[0], {})
                texts = [query.casefold()]
                labels = [stored_recipe.get("name", ""), *(stored_recipe.get("aliases") or [])]
                exact_title_present = any(
                    label and any(re.search(r"(?<!\w)" + re.escape(str(label).casefold()) + r"(?!\w)", text)
                                  for text in texts)
                    for label in labels
                )
                if not exact_title_present:
                    stored_match = None
            if stored_match:
                recipe_id = stored_match[0]
                recipe_name = self.kg.recipes.get(recipe_id, {}).get("name", recipe_id)
                plan = {
                    "intent": "single_recipe", "requested_count": 1,
                    "recipe_requests": [{"slot": "", "recipe_id": recipe_id, "query": recipe_name}],
                    "question_type": "recipe",
                }
            else:
                generated = plan.get("generated_composition") or []
                generated_plan = {
                    "intent": "composition_question",
                    "requested_count": 1,
                    "recipe_requests": [],
                    "ingredient_queries": [],
                    "composition_ingredients": [
                        {"ingredient_id": item["ingredient_id"],
                         "query": self.kg.ingredients[item["ingredient_id"]].get("name", item["ingredient_id"]),
                         "amount_g": item["amount_g"]}
                        for item in generated
                        if item.get("ingredient_id") in self.kg.ingredients
                    ],
                    "composition_overflow": bool(plan.get("generated_composition_overflow")),
                    "generated_recipe_invalid": bool(plan.get("generated_composition_invalid")),
                    "composition_parse_error": False,
                    "recipe_query": None,
                    "generated_recipe": True,
                }
                plan = generated_plan
        if plan.get("intent") == "composition_question":
            return self._execute_composition_question(
                query, plan, constraints, servings, generator, slm_used, response_context,
            )
        if plan.get("intent") == "substitution_question":
            original_query = plan.get("original_ingredient_query", "")
            substitute_query = plan.get("substitute_ingredient_query", "")
            if not original_query or not substitute_query:
                assessment = {
                    "status": "NEEDS_DETAILS", "original": None, "substitute": None,
                    "recipe": None, "candidate_recipes": [], "nutrient_comparison": [],
                    "answer": "Tell me which ingredient to replace and what ingredient you want to use instead. If the ingredient is in a particular recipe, include its name too.",
                }
            else:
                assessment = self.substitution_analyzer.assess(
                    original_query, substitute_query,
                    constraints=constraints,
                    servings=servings,
                    recipe_query=plan.get("recipe_query"),
                )
            for key in ("original", "substitute"):
                ingredient = assessment.get(key)
                if ingredient:
                    trace.append(
                        f"Neo4j Foundation Foods retrieval: {ingredient['name']} "
                        f"(match score {ingredient['match_score']:.2f}; FDC {ingredient.get('source', {}).get('fdc_id')})."
                    )
            if assessment.get("missing_ingredients"):
                trace.append("Neo4j Foundation Foods retrieval: one or more requested ingredient records were not found.")
            if assessment.get("recipe"):
                trace.append(
                    f"Neo4j recipe graph traversal: checked whether the source ingredient is in "
                    f"{assessment['recipe']['name']}."
                )
            elif assessment.get("candidate_recipes"):
                trace.append(
                    f"Neo4j recipe graph traversal: source ingredient appears in "
                    f"{len(assessment['candidate_recipes'])} local recipe(s)."
                )
            if assessment.get("limit_check") is not None:
                trace.append("Constraint engine: evaluated the full recipe against the entered numeric limits and, when possible, adjusted only the replacement portion.")
            elif assessment.get("nutrient_comparison"):
                trace.append("Deterministic nutrient arithmetic: compared the available food or recipe nutrient totals; no numeric meal limit was supplied for a pass/fail decision.")
            else:
                trace.append("Nutrient calculation was not performed because the ingredient records could not be resolved unambiguously.")
            trace.append("SLM wording was not used to decide substitution facts or nutrient values." if slm_used
                         else "Deterministic response composed from retrieved records and nutrient calculations.")
            return {
                **response_context,
                "constraints_applied": bool(constraints),
                "answer": assessment["answer"],
                "substitution_assessment": assessment,
                "tool_trace": trace,
                "recipe": None,
                "missing_data": None,
                "evaluations": [],
                "selected": None,
            }
        is_multi = plan.get("intent") == "meal_plan" or int(plan.get("requested_count", 1)) > 1
        if plan.get("intent") in {"ingredient_question", "catalog_question"}:
            catalog_lookup = (
                plan.get("intent") == "catalog_question"
                or plan.get("question_type") == "catalog_existence"
            )
            ingredient_queries = plan.get("ingredient_queries") or ([] if catalog_lookup else [query])
            dataset_scope = plan.get("dataset_scope", "all")
            if catalog_lookup:
                ingredient_results, ingredient_trace = self._lookup_catalog_membership(
                    ingredient_queries, dataset_scope,
                )
            else:
                ingredient_results, ingredient_trace = self._lookup_ingredients(
                    ingredient_queries, dataset_scope,
                )
            trace.extend(ingredient_trace)
            if catalog_lookup:
                exact_names = [item["query"] for item in ingredient_results
                               if item.get("match_status") == "exact"]
                matched_names = [item["query"] for item in ingredient_results
                                 if item.get("match_status") == "matches_found"]
                missing_names = [item["query"] for item in ingredient_results
                                 if item.get("match_status") == "not_found"]
                summary = []
                if exact_names:
                    summary.append("Exact name matches: " + ", ".join(exact_names) + ".")
                if matched_names:
                    summary.append("Possible matching records: " + ", ".join(matched_names) + ".")
                if missing_names:
                    summary.append("No matching record found for: " + ", ".join(missing_names) + ".")
                if ingredient_queries:
                    answer = (
                        "I checked each requested name against "
                        + self._dataset_scope_label(dataset_scope) + ". "
                        + " ".join(summary)
                    )
                else:
                    answer = (
                        "I couldn't identify the ingredient names to check. Please list each ingredient you want "
                        "me to look up in the USDA FoodData Central datasets."
                    )
                if matched_names:
                    answer += " The listed records may use a more specific food state or label; review the matches below."
                if missing_names and any(item.get("related_matches") for item in ingredient_results):
                    answer += " Related records are shown separately and do not count as confirmed matches."
                trace.append("Deterministic response summarized per-ingredient catalog membership results.")
            else:
                answer = "I looked up the matching ingredient records and their per-100 g nutrient values."
                ambiguous_results = [item for item in ingredient_results if item.get("ambiguous")]
                if ambiguous_results:
                    ambiguous_names = ", ".join(item["query"] for item in ambiguous_results)
                    answer = (
                        f"I found separate USDA records for {ambiguous_names}. I didn't select or combine them; "
                        "specify the dataset or recorded food state to choose one before I report nutrient values."
                    )
                elif not any(item.get("found") for item in ingredient_results):
                    answer = "I could not find a matching ingredient record, so I can't verify its nutrient values."
                trace.append("Deterministic response composed from retrieved ingredient records.")
                if constraints:
                    answer += " The entered meal limits were not applied because this lookup is per 100 g and no serving amount was specified."
            return {
                **response_context,
                "constraints_applied": False,
                "answer": answer,
                "dataset_scope": dataset_scope,
                "ingredient_results": ingredient_results, "tool_trace": trace,
                "recipe": None, "missing_data": None, "meal_plan": None,
                "evaluations": [], "selected": None,
            }
        if is_multi:
            meal_plan = self._execute_meal_plan(query, plan, constraints, servings, generator, slm_used)
            answer = meal_plan.pop("answer")
            trace.extend(meal_plan.pop("tool_trace"))
            extreme_match = re.search(r"\b(0|1)\s*mg\s+(?:of\s+)?potassium\b", query, re.I)
            if extreme_match:
                answer += (f" You mentioned {extreme_match.group(1)} mg potassium per day. It was not applied as a verified dietary limit; "
                           "please confirm the intended target with your renal care team.")
            return {
                **response_context, "answer": answer, "meal_plan": meal_plan,
                "tool_trace": trace, "recipe": None, "missing_data": None,
                "evaluations": [], "selected": None,
            }

        pipeline = self.runtime.create_pipeline(generator)
        requests = plan.get("recipe_requests") or [{"query": query}]
        request = requests[0]
        lookup_query = request.get("recipe_id") or request.get("query") or query
        result = pipeline.run(lookup_query, constraints, use_llm=True, servings=servings)
        source_ingredient_results = []
        if result.get("missing_data"):
            missing = result["missing_data"]
            if missing.get("source_dataset") == "Food.com Recipes and Reviews (Kaggle)":
                trace.append(
                    f"Neo4j recipe retrieval: found {missing.get('recipe_name', lookup_query)}; "
                    "Food.com provides no verified ingredient weights for nutrient calculation."
                )
            else:
                trace.append("Neo4j retrieval: requested recipe/ingredient data was not found in Foundation Foods or the recipe graph.")
        else:
            trace.append(f"Neo4j full-text and graph-neighborhood retrieval: {result.get('recipe_id')} (score {result.get('retrieval_score', 0):.2f}).")
            trace.append(f"Candidate generator: evaluated {len(result.get('evaluations', []))} ingredient variants.")
            trace.append("Constraint engine: evaluated recipe candidates against the explicit limits.")
        trace.append("Deterministic response composed from retrieved records; SLM output does not decide nutrient or limit claims.")

        response = {
            **response_context,
            "tool_trace": trace,
            "recipe": None,
            "answer": result.get("answer"),
            "missing_data": result.get("missing_data"),
            "ingredient_results": source_ingredient_results,
            "evaluations": [],
            "selected": None,
        }
        if result.get("missing_data"):
            missing = result.get("missing_data") or {}
            if missing.get("source_dataset") == "Food.com Recipes and Reviews (Kaggle)":
                self._resolve_chat_foodcom_ingredients(response, missing)
            return response

        recipe_id = result.get("recipe_id")
        recipe_data = self.kg.recipes.get(recipe_id) if recipe_id else None
        if recipe_data:
            self._attach_ingredient_mapping_enrichment(
                response, recipe_data, constraints=constraints,
                requested_servings=servings,
            )
            response["recipe"] = {
                "id": recipe_id,
                "name": recipe_data.get("name", recipe_id),
                "description": recipe_data.get("description", ""),
                "source": recipe_data.get("source", {}),
            }
        adapted_validation_enabled = bool(getattr(
            self, "adapted_recipe_nutrition_enrichment_enabled", False,
        ))
        culinary_validation_enabled = bool(getattr(
            self, "adapted_recipe_culinary_validation_enabled", False,
        ))
        final_acceptance_enabled = bool(getattr(self, "final_recipe_acceptance_enabled", False))
        original_nutrient_enrichment = None
        adapted_mapping_cache = {}
        adapted_nutrient_record_cache = {}
        adapted_metadata_by_composition = {}
        culinary_metadata_by_composition = {}
        acceptance_metadata_by_composition = {}
        selected_candidate = result.get("selected")
        selected_signature = (
            tuple((str(iid), float(amount)) for iid, amount in selected_candidate["ingredients"])
            if selected_candidate else None
        )
        if adapted_validation_enabled and recipe_data and result.get("evaluations"):
            try:
                original_nutrient_enrichment = self.recipe_nutrient_enrichment_service.enrich(recipe_data)
            except Exception as exc:
                original_nutrient_enrichment = {
                    "calculation_status": "UNAVAILABLE",
                    "error": f"Original nutrient enrichment failed ({type(exc).__name__}).",
                    "ingredient_mapping_enrichment": {"results": []},
                }
        for item in result.get("evaluations", []):
            evaluation_status = ("NOT_EVALUATED" if not constraints else
                                 "UNVERIFIED" if item.get("not_evaluated") else
                                 "PASS" if item["passed"] is True else
                                 "FAIL" if item["passed"] is False else "UNVERIFIED")
            evaluation_response = {
                "status": evaluation_status,
                "compatibility": item["compatibility"],
                "substitution_basis": item.get("substitution_basis", []),
                "ingredients": [
                    {"id": iid, "name": self.kg.ingredients.get(iid, {}).get("name", iid),
                     "amount_g": amount,
                     "ingredient_type": self.kg.ingredients.get(iid, {}).get("ingredient_type"),
                     "substitution_group": self.kg.ingredients.get(iid, {}).get("substitution_group")}
                    for iid, amount in item["ingredients"]
                ],
                "reasons": item["reasons"],
                "not_evaluated": item["not_evaluated"],
            }
            if (adapted_validation_enabled or culinary_validation_enabled or final_acceptance_enabled) and recipe_data:
                replacement_by_position = {
                    change.get("adapted_position"): change.get("original_ingredient_id")
                    for change in item.get("ingredient_changes", [])
                }
                adapted_recipe = {
                    "id": recipe_id,
                    "name": recipe_data.get("name", recipe_id),
                    "servings": recipe_data.get("servings", recipe_data.get("recipe_servings")),
                    # Candidate adaptations preserve the stored method. The
                    # validator receives it as-is and can flag stale references.
                    "method": copy.deepcopy(recipe_data.get("method")),
                    "ingredients": [
                        {"id": iid, "name": self.kg.ingredients.get(iid, {}).get("name", iid),
                         "amount_g": amount,
                         **({"replaces_id": replacement_by_position[position]}
                            if position in replacement_by_position else {})}
                        for position, (iid, amount) in enumerate(item["ingredients"])
                    ],
                }
                signature = tuple((str(iid), float(amount)) for iid, amount in item["ingredients"])
                adaptation_id = f"{recipe_id}:candidate:{len(response['evaluations'])}"
                changes = None
                if adapted_validation_enabled:
                    evaluation_response["adapted_recipe_nutrition_enrichment"] = self._adapted_nutrition_metadata(
                        recipe_data, adapted_recipe, constraints, servings,
                        adaptation_id=adaptation_id,
                        original_enrichment=original_nutrient_enrichment,
                        mapping_cache=adapted_mapping_cache,
                        nutrient_record_cache=adapted_nutrient_record_cache,
                    )
                    changes = evaluation_response["adapted_recipe_nutrition_enrichment"].get("changes")
                    adapted_metadata_by_composition[signature] = evaluation_response[
                        "adapted_recipe_nutrition_enrichment"
                    ]
                # Culinary SLM work is limited to the selected adaptation. The
                # alternative candidates remain available for Step 6B, without
                # multiplying one request into as many as 256 model calls.
                if culinary_validation_enabled and signature == selected_signature:
                    culinary = culinary_metadata_by_composition.get(signature)
                    if culinary is None:
                        culinary = self._adapted_culinary_metadata(
                            recipe_data, adapted_recipe, adaptation_id=adaptation_id,
                            structured_changes=changes,
                            substitution_metadata={
                                "compatibility": item.get("compatibility"),
                                "substitution_basis": item.get("substitution_basis", []),
                            },
                        )
                    evaluation_response["adapted_recipe_culinary_validation"] = culinary
                    culinary_metadata_by_composition[signature] = culinary
                if final_acceptance_enabled:
                    acceptance = self._final_recipe_acceptance_metadata(
                        evaluation_response.get("adapted_recipe_nutrition_enrichment"),
                        evaluation_response.get("adapted_recipe_culinary_validation"),
                        constraints, adapted_recipe, adaptation_id=adaptation_id,
                    )
                    evaluation_response["final_recipe_acceptance"] = acceptance
                    acceptance_metadata_by_composition[signature] = acceptance
            elif final_acceptance_enabled:
                adaptation_id = f"{recipe_id or 'unknown'}:candidate:{len(response['evaluations'])}"
                response_ingredients = [
                    {"id": iid, "amount_g": amount}
                    for iid, amount in item.get("ingredients", [])
                ]
                evaluation_response["final_recipe_acceptance"] = self._final_recipe_acceptance_metadata(
                    None, None, constraints,
                    {"id": adaptation_id, "ingredients": response_ingredients},
                    adaptation_id=adaptation_id,
                )
            response["evaluations"].append(evaluation_response)
        selected = result.get("selected")
        if selected:
            selected_response = {
                "status": ("NOT_EVALUATED" if not constraints else
                           "UNVERIFIED" if selected.get("not_evaluated") else "PASS"),
                "substitution_basis": selected.get("substitution_basis", []),
                "ingredients": [
                    {"id": iid, "name": self.kg.ingredients[iid].get("name", iid), "amount_g": amount,
                     "source": self.kg.ingredients[iid].get("source", {}),
                     "ingredient_type": self.kg.ingredients[iid].get("ingredient_type"),
                     "substitution_group": self.kg.ingredients[iid].get("substitution_group")}
                    for iid, amount in selected["ingredients"]
                ],
                "nutrients_per_serving": selected["totals"],
                "nutrients_for_requested_servings": selected["totals_for_servings"],
            }
            if (adapted_validation_enabled or culinary_validation_enabled or final_acceptance_enabled) and recipe_data:
                signature = tuple((str(iid), float(amount)) for iid, amount in selected["ingredients"])
                if adapted_validation_enabled:
                    metadata = adapted_metadata_by_composition.get(signature)
                if adapted_validation_enabled and metadata is None:
                    replacement_by_position = {
                        change.get("adapted_position"): change.get("original_ingredient_id")
                        for change in selected.get("ingredient_changes", [])
                    }
                    adapted_recipe = {
                        "id": recipe_id,
                        "name": recipe_data.get("name", recipe_id),
                        "ingredients": [
                            {"id": iid, "name": self.kg.ingredients.get(iid, {}).get("name", iid),
                             "amount_g": amount,
                             **({"replaces_id": replacement_by_position[position]}
                                if position in replacement_by_position else {})}
                            for position, (iid, amount) in enumerate(selected["ingredients"])
                        ],
                    }
                    metadata = self._adapted_nutrition_metadata(
                        recipe_data, adapted_recipe, constraints, servings,
                        adaptation_id=f"{recipe_id}:selected",
                        original_enrichment=original_nutrient_enrichment,
                        mapping_cache=adapted_mapping_cache,
                        nutrient_record_cache=adapted_nutrient_record_cache,
                    )
                if adapted_validation_enabled:
                    selected_response["adapted_recipe_nutrition_enrichment"] = metadata
                if culinary_validation_enabled:
                    culinary = culinary_metadata_by_composition.get(signature)
                    if culinary is None:
                        replacement_by_position = {
                            change.get("adapted_position"): change.get("original_ingredient_id")
                            for change in selected.get("ingredient_changes", [])
                        }
                        adapted_recipe = {
                            "id": recipe_id,
                            "name": recipe_data.get("name", recipe_id),
                            "servings": recipe_data.get("servings", recipe_data.get("recipe_servings")),
                            "method": copy.deepcopy(recipe_data.get("method")),
                            "ingredients": [
                                {"id": iid, "name": self.kg.ingredients.get(iid, {}).get("name", iid),
                                 "amount_g": amount,
                                 **({"replaces_id": replacement_by_position[position]}
                                    if position in replacement_by_position else {})}
                                for position, (iid, amount) in enumerate(selected["ingredients"])
                            ],
                        }
                        culinary = self._adapted_culinary_metadata(
                            recipe_data, adapted_recipe, adaptation_id=f"{recipe_id}:selected",
                            substitution_metadata={
                                "compatibility": selected.get("compatibility"),
                                "substitution_basis": selected.get("substitution_basis", []),
                            },
                        )
                    selected_response["adapted_recipe_culinary_validation"] = culinary
                if final_acceptance_enabled:
                    acceptance = acceptance_metadata_by_composition.get(signature)
                    if acceptance is None:
                        acceptance_recipe = {
                            "id": f"{recipe_id}:selected",
                            "name": recipe_data.get("name", recipe_id),
                            "servings": recipe_data.get("servings", recipe_data.get("recipe_servings")),
                            "ingredients": [
                                {"id": iid, "name": self.kg.ingredients.get(iid, {}).get("name", iid),
                                 "amount_g": amount}
                                for iid, amount in selected["ingredients"]
                            ],
                        }
                        acceptance = self._final_recipe_acceptance_metadata(
                            selected_response.get("adapted_recipe_nutrition_enrichment"),
                            selected_response.get("adapted_recipe_culinary_validation"),
                            constraints, acceptance_recipe,
                            adaptation_id=f"{recipe_id}:selected",
                        )
                    selected_response["final_recipe_acceptance"] = acceptance
            elif final_acceptance_enabled:
                adaptation_id = f"{recipe_id or 'unknown'}:selected"
                selected_response["final_recipe_acceptance"] = self._final_recipe_acceptance_metadata(
                    None, None, constraints,
                    {"id": adaptation_id, "ingredients": [
                        {"id": iid, "amount_g": amount}
                        for iid, amount in selected.get("ingredients", [])
                    ]},
                    adaptation_id=adaptation_id,
                )
            response["selected"] = selected_response
        elif result.get("evaluations"):
            response["answer"] = "No candidate is verified against the explicit limits. Review the missing-data notes or adjust clinician-provided limits."

        lowered = query.casefold()
        asks_suitability = any(word in lowered for word in ("is it ok", "is it okay", "can i eat", "suitable for me"))
        if asks_suitability and not constraints and response["answer"]:
            response["answer"] += ("\n\nThis prototype has no personal meal allowance or product-specific context, "
                                   "so it cannot give a personal yes/no. Check the actual food label and clinician guidance.")
        return response

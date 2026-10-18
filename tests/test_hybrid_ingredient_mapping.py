import copy
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.recipes.hybrid_ingredient_mapping import (
    HybridIngredientMappingService,
    recipe_mapping_configuration,
)
from src.recipes.ingredient_mapping import USDAIngredientMapper
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator
from src.recipes.ingredient_parser import parse_foodcom_ingredient
from src.recipes.ingredient_reference import PreferredUSDAReferenceManager
from src.retrieval.graph_retriever import GraphRetriever
from src.web.service import RenAllyWebService


def _food(fdc_id, description, *, aliases=(), dataset="foundation_foods", data_type="Foundation"):
    return {
        "name": description,
        "fdc_description": description,
        "aliases": list(aliases),
        "food_state": description,
        "source": {"fdc_id": fdc_id, "dataset_key": dataset, "data_type": data_type},
    }


class MemoryMappingCatalog:
    def __init__(self):
        self.ingredients = {
            "fdc:1": _food(1, "Apple"),
            "fdc:2": _food(2, "Banana, raw", dataset="sr_legacy", data_type="SR Legacy"),
            "fdc:3": _food(3, "Fish, salmon, Atlantic, farm raised, raw"),
            "fdc:4": _food(4, "Fish, salmon, Atlantic, wild, raw", dataset="sr_legacy", data_type="SR Legacy"),
            "fdc:5": _food(5, "Salt, table", dataset="sr_legacy", data_type="SR Legacy"),
        }
        self.cache = {}
        self.references = {}
        self.reference_events = []
        self.cache_reads = 0
        self.retrieval_calls = []
        self.write_calls = []
        self.fail_search = False
        self.fail_cache_read = False

    def search(self, index_name, search_query, k=5, dataset_scope="all"):
        if self.fail_search:
            raise ConnectionError("USDA candidate retrieval unavailable")
        self.retrieval_calls.append((search_query, dataset_scope))
        terms = set(search_query.casefold().replace(",", " ").split())
        rows = []
        for food_id, food in self.ingredients.items():
            source = food.get("source", {})
            if dataset_scope != "all" and source.get("dataset_key") != dataset_scope:
                continue
            labels = " ".join([food.get("fdc_description", ""), food.get("name", ""), *food.get("aliases", [])])
            label_terms = set(labels.casefold().replace(",", " ").split())
            if terms & label_terms:
                rows.append({"id": food_id, "name": food["name"], "aliases": food["aliases"],
                             "dataset_key": source.get("dataset_key"), "score": 1.0})
        return rows[:k]

    def get_ingredient_mapping_cache(self, cache_key):
        self.cache_reads += 1
        if self.fail_cache_read:
            raise ConnectionError("mapping cache unavailable")
        return copy.deepcopy(self.cache.get(cache_key))

    def save_ingredient_mapping_cache(self, record):
        self.write_calls.append("mapping_cache")
        current = self.cache.get(record["cache_key"], {})
        self.cache[record["cache_key"]] = {**current, **copy.deepcopy(record)}
        return copy.deepcopy(self.cache[record["cache_key"]])

    def save_ingredient_mapping_evaluation(self, cache_key, evaluation):
        self.write_calls.append("slm_evaluation")
        record = self.cache.get(cache_key)
        if record is None:
            return None
        record["slm_evaluation"] = copy.deepcopy(evaluation)
        return copy.deepcopy(record)

    def list_ingredient_references(self, canonical_ingredient_name=None):
        return [copy.deepcopy(record) for record in self.references.values()
                if canonical_ingredient_name is None
                or record.get("canonical_ingredient_name") == canonical_ingredient_name]

    def save_ingredient_reference_decision(self, reference, event):
        self.write_calls.append("reference")
        self.references[reference["reference_id"]] = copy.deepcopy(reference)
        self.reference_events.append(copy.deepcopy(event))
        return copy.deepcopy(reference)


class FakeValidator:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.calls = []

    def evaluate(self, parsed_ingredient, mapping_result, **kwargs):
        self.calls.append((parsed_ingredient, mapping_result, kwargs))
        if self.error:
            raise self.error
        return copy.deepcopy(self.result)


class FakeOfflineWorkflow:
    def __init__(self):
        self.calls = []

    def run(self, **kwargs):
        self.calls.append(kwargs)
        return {"dry_run": kwargs.get("dry_run", True), "batch_processed_this_run": 1}


class HybridIngredientMappingTests(unittest.TestCase):
    def setUp(self):
        self.kg = MemoryMappingCatalog()
        self.retriever = GraphRetriever(self.kg)
        self.mapper = USDAIngredientMapper(self.kg, self.retriever)
        self.references = PreferredUSDAReferenceManager(self.kg)

    def service(self, **kwargs):
        return HybridIngredientMappingService(
            self.kg, self.retriever, mapper=self.mapper,
            reference_manager=self.references, **kwargs,
        )

    @staticmethod
    def recipe(*items):
        return {"name": "Test recipe", "raw_ingredient_items": list(items)}

    def test_feature_flag_is_disabled_by_default_and_modes_are_explicit(self):
        config = recipe_mapping_configuration(environ={})
        self.assertEqual(config, {
            "enabled": False, "mode": "READ_ONLY", "persist_mappings": False, "use_slm": False,
        })
        config = recipe_mapping_configuration(
            environ={"RENALLY_RECIPE_MAPPING_ENRICHMENT": "true",
                     "RENALLY_INGREDIENT_MAPPING_MODE": "on_demand",
                     "RENALLY_INGREDIENT_MAPPING_PERSIST": "1"},
        )
        self.assertEqual(config["mode"], "ON_DEMAND")
        self.assertTrue(config["enabled"])
        self.assertTrue(config["persist_mappings"])
        with self.assertRaisesRegex(ValueError, "only in ON_DEMAND"):
            recipe_mapping_configuration(mode="READ_ONLY", persist_mappings=True, environ={})

    def test_read_only_resolves_without_any_database_writes(self):
        result = self.service().resolve_recipe(self.recipe({"name": "Apple", "quantity_raw": "2 oz"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "EXACT_IDENTITY")
        self.assertEqual(mapping["candidate_ids"], [1])
        self.assertAlmostEqual(result["results"][0]["quantity_g"], 56.69904625)
        self.assertEqual(result["mode"], "READ_ONLY")
        self.assertEqual(self.kg.write_calls, [])

    def test_chat_row_limit_preserves_full_source_count_and_quantity_alignment(self):
        recipe = {
            "name": "Large recipe", "raw_ingredients": ["Apple"] * 20 + ["Banana"],
            "raw_ingredient_quantities": ["1 oz"] * 21,
        }
        result = self.service().resolve_recipe(recipe, max_ingredients=20)
        self.assertEqual(result["ingredient_count"], 20)
        self.assertEqual(result["source_ingredient_count"], 21)
        self.assertTrue(result["ingredient_limit_reached"])
        self.assertEqual(result["quantity_alignment_status"], "ALIGNED_PARALLEL_ARRAYS")
        self.assertTrue(all(row["original_name"] == "Apple" for row in result["results"]))

    def test_on_demand_persists_only_when_explicitly_configured_and_eligible(self):
        result = self.service(mode="ON_DEMAND", persist_mappings=True).resolve_recipe(
            self.recipe({"name": "Apple", "quantity_raw": "100 g"}),
        )
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "VERIFIED_MAPPING")
        self.assertEqual(mapping["verification_method"], "DETERMINISTIC_POLICY")
        self.assertEqual(mapping["verification_policy_version"], 2)
        self.assertTrue(mapping["verification_provenance"]["verification_evidence"]["eligible"])
        self.assertTrue(self.kg.write_calls)

    def test_on_demand_without_persistence_returns_exact_identity_not_verified(self):
        result = self.service(mode="ON_DEMAND").resolve_recipe(
            self.recipe({"name": "Apple", "quantity_raw": "100 g"}),
        )
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "EXACT_IDENTITY")
        self.assertIsNone(mapping["verification_method"])
        self.assertEqual(self.kg.write_calls, [])

    def test_offline_batch_delegates_to_existing_checkpointed_workflow(self):
        workflow = FakeOfflineWorkflow()
        service = self.service(mode="OFFLINE", premapping_workflow=workflow)
        result = service.run_offline_batch(batch_size=2, dry_run=True, write_enabled=False,
                                           checkpoint_path="/tmp/test-checkpoint.json")
        self.assertEqual(result["batch_processed_this_run"], 1)
        self.assertEqual(workflow.calls, [{
            "batch_size": 2, "dry_run": True, "write_enabled": False,
            "checkpoint_path": "/tmp/test-checkpoint.json",
        }])
        with self.assertRaisesRegex(ValueError, "only in OFFLINE"):
            self.service().run_offline_batch()

    def test_existing_verified_mapping_is_reused_with_original_provenance(self):
        item = {"name": "Apple", "quantity_raw": "100 g", "parsed": parse_foodcom_ingredient("Apple", "100 g")}
        key = self.mapper.mapping_cache_key(item)
        candidate = {"fdc_id": 1, "food_record_id": "fdc:1", "description": "Apple",
                     "data_type": "Foundation", "food_state": "Apple", "approved_aliases": []}
        self.kg.cache[key] = {
            "cache_key": key, "status": "VERIFIED", "mapping_version": self.mapper.mapping_version,
            "normalized_name": "apple", "dataset_scope": "all", "attributes": {},
            "fdc_id": 1, "fdc_description": "Apple", "candidates": [candidate],
            "selected_candidate": candidate, "verification_method": "human_review",
            "validation_method": "human_review", "reviewer": "registered-reviewer",
            "validated_at": "2026-01-01T00:00:00+00:00", "validation_note": "reviewed fixture",
        }
        result = self.service().resolve_recipe(self.recipe(item))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "VERIFIED_MAPPING")
        self.assertEqual(mapping["fdc_id"], 1)
        self.assertEqual(mapping["verification_provenance"]["reviewer"], "registered-reviewer")
        self.assertEqual(mapping["verification_provenance"]["validation_note"], "reviewed fixture")
        self.assertEqual(self.kg.retrieval_calls, [])

    def test_exact_match_skips_slm_inference(self):
        validator = FakeValidator({"status": "RECOMMENDED", "recommended_fdc_id": 1,
                                   "unresolved_attributes": [], "requires_review": False})
        service = self.service(use_slm=True, validator=validator)
        result = service.resolve_recipe(self.recipe({"name": "Apple", "quantity_raw": "1 cup"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(len(validator.calls), 0)
        self.assertEqual(result["slm_inference_calls"], 0)
        self.assertEqual(mapping["status"], "EXACT_IDENTITY")
        self.assertIsNone(mapping["verification_method"])
        self.assertEqual(self.kg.write_calls, [])

    def test_slm_inference_failure_is_reported_without_failing_recipe_resolution(self):
        validator = SLMIngredientMappingValidator(self.kg, evaluator=lambda _payload: (_ for _ in ()).throw(RuntimeError("offline")))
        service = self.service(use_slm=True, validator=validator)
        result = service.resolve_recipe(self.recipe({"name": "salmon", "quantity_raw": "1 oz"}))
        mapping = result["results"][0]["mapping"]
        self.assertIn(mapping["status"], {"AMBIGUOUS", "REVIEW_REQUIRED", "FAMILY_RESOLVED"})
        self.assertTrue(any(error["stage"] == "slm_validation" for error in mapping["errors"]))
        self.assertEqual(self.kg.write_calls, [])

    def test_malformed_slm_output_is_graceful(self):
        validator = SLMIngredientMappingValidator(self.kg, evaluator=lambda _payload: "not JSON")
        result = self.service(use_slm=True, validator=validator).resolve_recipe(
            self.recipe({"name": "salmon", "quantity_raw": "1 oz"}),
        )
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["slm_validation"]["status"], "REVIEW_REQUIRED")
        self.assertIsNone(mapping["verification_method"])

    def test_approved_reference_can_be_applied_without_becoming_verified(self):
        self.references.approve_reference(
            "salt", 5, reviewer_identity="human-reviewer", provenance={"source": "test"},
            attributes={"salt_type": "table"}, allow_as_default=True,
        )
        self.kg.write_calls.clear()
        result = self.service().resolve_recipe(self.recipe({"name": "salt", "quantity_raw": "1 g"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "REFERENCE_DEFAULT")
        self.assertTrue(mapping["reference_default"])
        self.assertEqual(mapping["reference_fdc_id"], 5)
        self.assertFalse(any(record.get("status") == "VERIFIED" for record in self.kg.cache.values()))
        self.assertEqual(self.kg.write_calls, [])

    def test_no_approved_reference_returns_ambiguous_unresolved_identity(self):
        result = self.service().resolve_recipe(self.recipe({"name": "salmon", "quantity_raw": "4 oz"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "AMBIGUOUS")
        self.assertFalse(mapping["reference_default"])
        self.assertIn("species", mapping["unresolved_attributes"])

    def test_generic_ingredient_remains_ambiguous_with_real_candidate_ids(self):
        result = self.service().resolve_recipe(self.recipe({"name": "salmon", "quantity_raw": "1 lb"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "AMBIGUOUS")
        self.assertEqual(set(mapping["candidate_ids"]), {3, 4})
        self.assertIsNone(mapping["fdc_id"])

    def test_incompatible_approved_reference_attributes_are_not_applied(self):
        self.references.approve_reference(
            "salmon", 3, reviewer_identity="human-reviewer", provenance={"source": "test"},
            attributes={"species": "atlantic", "farm_raised": "yes", "preparation_state": "raw",
                        "processing_state": "unspecified"}, allow_as_default=True,
        )
        result = self.service().resolve_recipe(
            self.recipe({"name": "wild Atlantic salmon raw", "quantity_raw": "4 oz"}),
        )
        mapping = result["results"][0]["mapping"]
        self.assertNotEqual(mapping["status"], "REFERENCE_DEFAULT")
        self.assertFalse(mapping["reference_default"])
        self.assertIsNone(mapping["fdc_id"])

    def test_missing_usda_candidates_are_unresolved_without_fabrication(self):
        result = self.service().resolve_recipe(self.recipe({"name": "dragonfruit", "quantity_raw": "1 fruit"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "UNRESOLVED")
        self.assertEqual(mapping["candidate_ids"], [])
        self.assertIsNone(mapping["fdc_id"])

    def test_usda_retrieval_failure_is_returned_as_structured_error(self):
        self.kg.fail_search = True
        result = self.service().resolve_recipe(self.recipe({"name": "Apple", "quantity_raw": "100 g"}))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "UNRESOLVED")
        self.assertTrue(any(error["stage"] == "retrieval" for error in mapping["errors"]))

    def test_legacy_recipe_fields_are_parsed_only_when_step_one_data_is_absent(self):
        recipe = {"name": "Legacy", "raw_ingredients": ["Apple", "salt"],
                  "raw_ingredient_quantities": ["2 oz", "1 g"]}
        result = self.service().resolve_recipe(recipe)
        self.assertEqual(result["ingredient_count"], 2)
        self.assertAlmostEqual(result["results"][0]["quantity_g"], 56.69904625)
        self.assertEqual(result["results"][1]["quantity_g"], 1.0)

    def test_step_one_parsed_ingredient_data_is_reused_without_reparsing(self):
        item = {"name": "Apple", "quantity_raw": "2 oz",
                "parsed": parse_foodcom_ingredient("Apple", "2 oz")}
        with patch("src.recipes.hybrid_ingredient_mapping.parse_foodcom_ingredient",
                   side_effect=AssertionError("parsed Step 1 item should be reused")), \
             patch("src.recipes.hybrid_ingredient_mapping.parse_ingredient_text",
                   side_effect=AssertionError("parsed Step 1 item should be reused")):
            result = self.service().resolve_recipe(self.recipe(item))
        self.assertEqual(result["results"][0]["quantity_status"], "CONVERTED")
        self.assertEqual(result["results"][0]["mapping"]["status"], "EXACT_IDENTITY")

    def test_duplicate_ingredient_contexts_share_one_lookup_but_keep_each_quantity(self):
        recipe = self.recipe({"name": "Apple", "quantity_raw": "1 oz"},
                             {"name": "Apple", "quantity_raw": "2 oz"})
        result = self.service().resolve_recipe(recipe)
        self.assertEqual(result["unique_ingredient_contexts"], 1)
        self.assertEqual(result["mapping_cache_lookups"], 1)
        self.assertEqual(result["usda_candidate_retrievals"], 1)
        self.assertEqual(len(self.kg.retrieval_calls), 2)  # both datasets for one bounded candidate request
        self.assertAlmostEqual(result["results"][0]["quantity_g"], 28.349523125)
        self.assertAlmostEqual(result["results"][1]["quantity_g"], 56.69904625)

    def test_offline_mode_flags_stale_cache_version_and_does_not_retrieve(self):
        item = {"name": "Apple", "quantity_raw": "100 g",
                "parsed": parse_foodcom_ingredient("Apple", "100 g")}
        key = self.mapper.mapping_cache_key(item)
        self.kg.cache[key] = {"status": "VERIFIED", "mapping_version": "old-mapping-version",
                              "fdc_id": 1, "fdc_description": "Apple", "candidates": []}
        result = self.service(mode="OFFLINE").resolve_recipe(self.recipe(item))
        mapping = result["results"][0]["mapping"]
        self.assertTrue(mapping["cache_version_mismatch"])
        self.assertEqual(self.kg.retrieval_calls, [])

    def test_on_demand_does_not_reuse_or_downgrade_a_stale_verified_cache_record(self):
        item = {"name": "Apple", "quantity_raw": "100 g",
                "parsed": parse_foodcom_ingredient("Apple", "100 g")}
        key = self.mapper.mapping_cache_key(item)
        candidate = {"fdc_id": 1, "food_record_id": "fdc:1", "description": "Apple",
                     "data_type": "Foundation", "food_state": "Apple", "approved_aliases": []}
        self.kg.cache[key] = {
            "cache_key": key, "status": "VERIFIED", "mapping_version": "old-mapping-version",
            "normalized_name": "apple", "dataset_scope": "all", "attributes": {},
            "fdc_id": 1, "fdc_description": "Apple", "candidates": [candidate],
            "selected_candidate": candidate, "verification_method": "human_review",
            "validation_method": "human_review", "reviewer": "registered-reviewer",
        }
        result = self.service(mode="ON_DEMAND", persist_mappings=True).resolve_recipe(self.recipe(item))
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "EXACT_IDENTITY")
        self.assertTrue(mapping["cache_version_mismatch"])
        self.assertEqual(self.kg.cache[key]["status"], "VERIFIED")
        self.assertEqual(self.kg.cache[key]["mapping_version"], "old-mapping-version")
        self.assertEqual(self.kg.write_calls, [])

    def test_read_only_slm_does_not_use_cached_evaluation_when_disabled(self):
        item = {"name": "Apple", "quantity_raw": "100 g", "parsed": parse_foodcom_ingredient("Apple", "100 g")}
        key = self.mapper.mapping_cache_key(item)
        self.kg.cache[key] = {
            "status": "CANDIDATE", "mapping_version": self.mapper.mapping_version,
            "normalized_name": "apple", "attributes": {}, "dataset_scope": "all",
            "candidates": [{"fdc_id": 1, "food_record_id": "fdc:1", "description": "Apple",
                            "data_type": "Foundation", "food_state": "Apple", "approved_aliases": []}],
            "slm_evaluation": {"status": "RECOMMENDED", "recommended_fdc_id": 999},
        }
        result = self.service().resolve_recipe(self.recipe(item))
        self.assertIsNone(result["results"][0]["mapping"]["slm_validation"])
        self.assertEqual(result["results"][0]["mapping"]["status"], "EXACT_IDENTITY")

    def test_direct_recipe_food_links_are_reported_without_claiming_mapping_verification(self):
        recipe = {"ingredients": [{"id": "fdc:1", "amount_g": 100.0}]}
        result = self.service().resolve_recipe(recipe)
        mapping = result["results"][0]["mapping"]
        self.assertEqual(mapping["status"], "EXACT_IDENTITY")
        self.assertEqual(mapping["resolution_source"], "RECIPE_FOOD_LINK")
        self.assertIsNone(mapping["verification_method"])
        self.assertEqual(self.kg.retrieval_calls, [])

    def test_recipe_enrichment_attachment_is_optional_and_does_not_mutate_recipe(self):
        recipe = self.recipe({"name": "Apple", "quantity_raw": "1 oz"})
        original = copy.deepcopy(recipe)
        service = object.__new__(RenAllyWebService)
        service.ingredient_mapping_service = None
        response = {"recipe": {"ingredients": ["source-preserved"]}}
        self.assertIs(service._attach_ingredient_mapping_enrichment(response, recipe), response)
        self.assertNotIn("ingredient_mapping_enrichment", response)
        self.assertEqual(recipe, original)

        class Enricher:
            mode = "READ_ONLY"

            def resolve_recipe(self, value):
                return {"mode": self.mode, "results": [{"mapping": {"status": "UNRESOLVED"}}]}

        service.ingredient_mapping_service = Enricher()
        service._attach_ingredient_mapping_enrichment(response, recipe)
        self.assertEqual(response["ingredient_mapping_enrichment"]["results"][0]["mapping"]["status"], "UNRESOLVED")
        self.assertEqual(recipe, original)

    def test_chat_resolves_foodcom_ingredients_even_when_optional_enrichment_is_disabled(self):
        missing = {
            "kind": "missing_ingredients", "source_dataset": "Food.com Recipes and Reviews (Kaggle)",
            "recipe_id": "foodcom:1", "recipe_name": "Imported recipe",
            "raw_ingredient_items": [{"name": "Apple", "quantity_raw": "2 oz"}],
            "raw_ingredients": ["Apple"], "raw_ingredient_quantities": ["2 oz"],
        }

        class Planner:
            def plan(self, *_args):
                return ({"intent": "single_recipe", "requested_count": 1,
                         "recipe_requests": [{"query": "Imported recipe"}]}, None, None, False)

        class Pipeline:
            def run(self, *_args, **_kwargs):
                return {"missing_data": copy.deepcopy(missing), "answer": "existing response"}

        service = object.__new__(RenAllyWebService)
        service.profiles = {}
        service.planner = Planner()
        service.runtime = SimpleNamespace(create_pipeline=lambda _generator: Pipeline())
        service.kg = SimpleNamespace(recipes={}, ingredients={})
        service.ingredient_mapping_service = None
        service.chat_ingredient_mapping_service = SimpleNamespace(resolve_recipe=lambda recipe, **kwargs: {
            "mode": "READ_ONLY", "results": [{"mapping": {
                "status": "SLM_RESOLVED", "fdc_id": 100, "resolved_family": None,
            }}],
        })
        service._lookup_source_recipe_ingredients = lambda *_args: self.fail("Legacy catalog lookup was used")
        response = service.chat({"query": "Imported recipe"})

        self.assertIn("1 specific USDA food records", response["answer"])
        self.assertEqual(response["ingredient_mapping_enrichment"]["results"][0]["mapping"]["fdc_id"], 100)
        self.assertEqual(response["ingredient_results"], [])
        self.assertEqual(response["missing_data"]["raw_ingredient_items"], missing["raw_ingredient_items"])

    def test_chat_attaches_mapping_metadata_after_foodcom_retrieval_without_changing_ingredients(self):
        raw_items = [{"name": "Apple", "quantity_raw": "2 oz"}]
        missing = {
            "kind": "missing_ingredients", "source_dataset": "Food.com Recipes and Reviews (Kaggle)",
            "recipe_id": "foodcom:1", "recipe_name": "Imported recipe",
            "raw_ingredient_items": copy.deepcopy(raw_items),
            "raw_ingredients": ["Apple"], "raw_ingredient_quantities": ["2 oz"],
        }

        class Planner:
            def plan(self, *_args):
                return ({"intent": "single_recipe", "requested_count": 1,
                         "recipe_requests": [{"query": "Imported recipe"}]}, None, None, False)

        class Pipeline:
            def run(self, *_args, **_kwargs):
                return {"missing_data": copy.deepcopy(missing), "answer": "existing response"}

        class Enricher:
            mode = "READ_ONLY"

            def resolve_recipe(self, recipe, **kwargs):
                self.received = copy.deepcopy(recipe)
                return {"mode": self.mode, "results": [{"mapping": {"status": "AMBIGUOUS"}}]}

        enricher = Enricher()
        service = object.__new__(RenAllyWebService)
        service.profiles = {}
        service.planner = Planner()
        service.runtime = SimpleNamespace(create_pipeline=lambda _generator: Pipeline())
        service.kg = SimpleNamespace(recipes={}, ingredients={})
        service.ingredient_mapping_service = enricher
        service.chat_ingredient_mapping_service = enricher
        service._lookup_source_recipe_ingredients = lambda *_args: self.fail("Legacy catalog lookup was used")
        response = service.chat({"query": "Imported recipe"})

        self.assertEqual(response["ingredient_mapping_enrichment"]["results"][0]["mapping"]["status"], "AMBIGUOUS")
        self.assertEqual(enricher.received["raw_ingredient_items"], raw_items)
        self.assertEqual(response["missing_data"]["raw_ingredient_items"], raw_items)
        self.assertEqual(enricher.received["name"], "Imported recipe")
        self.assertIn("1 entries remain unresolved", response["answer"])

    def test_chat_explains_mismatched_unitless_foodcom_quantities(self):
        service = object.__new__(RenAllyWebService)
        service.chat_ingredient_mapping_service = SimpleNamespace(resolve_recipe=lambda recipe, **kwargs: {
            "quantity_alignment_status": "MISMATCHED_ARRAY_LENGTHS",
            "results": [{"mapping": {"fdc_id": None, "resolved_family": None}}] * 3,
        })
        recipe = {
            "recipe_name": "Salmon Steak", "raw_ingredient_names": ["salmon steaks", "lemon juice", "pepper"],
            "raw_ingredient_quantities": ["4", "1", "1", "1/8"],
        }
        response = {"answer": "old", "tool_trace": [], "missing_data": recipe}
        service._resolve_chat_foodcom_ingredients(response, recipe)
        self.assertIn("3 ingredient names but 4 quantity entries", response["answer"])
        self.assertIn("quantity entries also have no units", response["answer"])
        self.assertEqual(response["missing_data"]["raw_ingredient_quantities"], recipe["raw_ingredient_quantities"])


if __name__ == "__main__":
    unittest.main()

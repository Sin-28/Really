import json
import tempfile
import unittest
from pathlib import Path

from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy
from src.recipes.ingredient_mapping import MappingValidationError, USDAIngredientMapper
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator
from src.recipes.ingredient_parser import parse_foodcom_ingredient
from src.recipes.ingredient_premapping import IngredientPremappingWorkflow
from src.retrieval.graph_retriever import GraphRetriever


def _candidate(fdc_id, description, *, aliases=()):
    return {
        "fdc_id": fdc_id,
        "description": description,
        "food_state": description,
        "data_type": "Foundation",
        "approved_aliases": list(aliases),
        "retrieval_score": 9999.0,
    }


def _mapping(name, candidates):
    return {
        "ingredient": name,
        "normalized_name": name.casefold(),
        "cache_key": "policy-test-key",
        "status": "AMBIGUOUS" if len(candidates) > 1 else "CANDIDATE",
        "candidates": candidates,
    }


class InMemoryFoodGraph:
    """Small deterministic graph double that counts every persistent write."""

    def __init__(self, ingredients, recipes=()):
        self.ingredients = ingredients
        self.recipes = list(recipes)
        self.records = {}
        self.write_calls = []
        self.search_calls = []

    def get_foodcom_recipe_batch(self, *, after_recipe_id="", limit=250):
        rows = [row for row in self.recipes if str(row["recipe_id"]) > str(after_recipe_id)]
        return rows[:limit]

    def search(self, index_name, query, k=5, dataset_scope="all"):
        terms = {part.casefold() for part in query.split() if part.casefold() != "or"}
        self.search_calls.append(query)
        rows = []
        for food_id, item in self.ingredients.items():
            source = item.get("source") or {}
            if dataset_scope != "all" and source.get("dataset_key") != dataset_scope:
                continue
            label_words = set((item.get("fdc_description") or item.get("name") or "").casefold().replace(",", " ").split())
            alias_words = set(" ".join(item.get("aliases") or ()).casefold().split())
            if terms & (label_words | alias_words):
                rows.append({
                    "id": food_id,
                    "name": item.get("name"),
                    "aliases": item.get("aliases", []),
                    "dataset_key": source.get("dataset_key"),
                    "score": 1.0,
                })
        return rows[:k]

    def get_ingredient_mapping_cache(self, cache_key):
        record = self.records.get(cache_key)
        return dict(record) if record else None

    def save_ingredient_mapping_cache(self, record):
        self.write_calls.append("mapping_cache")
        old = self.records.get(record["cache_key"])
        if old and old.get("status") == "VERIFIED":
            return dict(old)
        stored = {**(old or {}), **record}
        self.records[record["cache_key"]] = stored
        return dict(stored)

    def save_ingredient_mapping_evaluation(self, cache_key, evaluation):
        self.write_calls.append("slm_evaluation")
        self.records[cache_key]["slm_evaluation"] = dict(evaluation)
        return dict(self.records[cache_key])

    def save_ingredient_mapping_policy_evaluation(self, cache_key, evaluation):
        self.write_calls.append("policy_evaluation")
        self.records[cache_key]["automatic_policy_evaluation"] = dict(evaluation)
        return dict(self.records[cache_key])

    def upgrade_automatic_mapping_policy(self, cache_key, fdc_id, policy_version, evidence):
        self.write_calls.append("policy_upgrade")
        record = self.records[cache_key]
        if (record.get("status") != "VERIFIED"
                or record.get("verification_method") != "DETERMINISTIC_POLICY"
                or str(record.get("fdc_id")) != str(fdc_id)):
            raise ValueError("only same existing auto mapping may be upgraded")
        record.update(verification_policy_version=policy_version,
                      verification_evidence=evidence,
                      automatic_policy_evaluation=evidence)
        return dict(record)


def _food(fdc_id, description, aliases=()):
    return {
        "name": description,
        "fdc_description": description,
        "food_state": description,
        "aliases": list(aliases),
        "source": {"fdc_id": fdc_id, "data_type": "Foundation", "dataset_key": "foundation_foods"},
    }


def _recipe(recipe_id, *ingredient_names):
    return {
        "recipe_id": str(recipe_id),
        "recipe_name": f"Recipe {recipe_id}",
        "details_json": json.dumps({
            "ingredient_items": [{"name": name, "quantity_raw": "1 oz"} for name in ingredient_names],
            "instructions": ["Prepare ingredients."],
        }),
    }


class AutomaticMappingPolicyTests(unittest.TestCase):
    def setUp(self):
        self.policy = AutomaticIngredientMappingPolicy()

    def evaluate(self, name, candidates, *, slm=None):
        parsed = parse_foodcom_ingredient(name, "1 oz")
        return self.policy.evaluate(parsed, _mapping(name, candidates), slm_evaluation=slm)

    def test_exact_identity_and_explicit_state_can_be_automatically_eligible(self):
        result = self.evaluate("raw chicken breast", [_candidate(1, "Chicken breast, raw")])
        self.assertTrue(result["eligible"])
        self.assertEqual(result["fdc_id"], 1)
        self.assertTrue(result["retrieval_scores_ignored"])

    def test_only_curated_exact_alias_counts_as_synonym_evidence(self):
        good = self.evaluate("steelhead trout", [_candidate(2, "Rainbow trout", aliases=("steelhead trout",))])
        bad = self.evaluate("steelhead trout", [_candidate(2, "Rainbow trout")])
        self.assertEqual(good["candidate_evidence"][0]["identity_basis"], "APPROVED_SYNONYM")
        self.assertTrue(good["eligible"])
        self.assertFalse(bad["eligible"])

    def test_duplicate_exact_candidate_records_remain_ambiguous(self):
        result = self.evaluate("raw chicken breast", [
            _candidate(1, "Chicken breast, raw"), _candidate(2, "Chicken breast, raw"),
        ])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "AMBIGUOUS")

    def test_raw_cannot_map_to_cooked(self):
        result = self.evaluate("raw chicken breast", [_candidate(3, "Chicken breast, cooked")])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "REJECTED")

    def test_smoked_cannot_map_to_fresh_candidate(self):
        result = self.evaluate("smoked salmon", [_candidate(4, "Salmon, fresh")])
        self.assertFalse(result["eligible"])
        self.assertTrue(any("processing state conflicts" in reason.casefold() for reason in result["reasons"]))

    def test_species_mismatch_is_rejected(self):
        result = self.evaluate("atlantic salmon", [_candidate(5, "Salmon, sockeye")])
        self.assertFalse(result["eligible"])
        self.assertTrue(any("species" in reason.casefold() for reason in result["reasons"]))

    def test_generic_oil_and_generic_salmon_stay_unresolved(self):
        oil = self.evaluate("oil", [_candidate(6, "Oil, olive")])
        salmon = self.evaluate("salmon", [_candidate(7, "Salmon, Atlantic")])
        self.assertIn("oil_type", oil["unresolved_attributes"])
        self.assertIn("species", salmon["unresolved_attributes"])
        self.assertFalse(oil["eligible"])
        self.assertFalse(salmon["eligible"])

    def test_explicit_olive_oil_can_be_eligible_and_cooked_rice_rejects_raw_rice(self):
        olive = self.evaluate("olive oil", [
            _candidate(61, "Oil, olive"), _candidate(62, "Oil, vegetable"),
        ])
        rice = self.evaluate("cooked rice", [_candidate(63, "Rice, raw")])
        self.assertTrue(olive["eligible"])
        self.assertEqual(olive["fdc_id"], 61)
        self.assertFalse(rice["eligible"])
        self.assertEqual(rice["status"], "REJECTED")

    def test_slm_recommendation_cannot_resolve_missing_source_state(self):
        result = self.evaluate(
            "chicken breast", [_candidate(8, "Chicken breast, raw")],
            slm={"status": "RECOMMENDED", "recommended_fdc_id": 8,
                 "requires_review": False, "unresolved_attributes": []},
        )
        self.assertFalse(result["eligible"])
        self.assertIn("cooking_state", result["candidate_evidence"][0]["unresolved_attributes"])

    def test_conflicting_slm_blocks_an_otherwise_exact_match(self):
        result = self.evaluate(
            "raw chicken breast", [_candidate(9, "Chicken breast, raw")],
            slm={"status": "RECOMMENDED", "recommended_fdc_id": 999,
                 "requires_review": False, "unresolved_attributes": []},
        )
        self.assertFalse(result["eligible"])
        self.assertTrue(result["slm_conflict"])

    def test_unavailable_slm_does_not_replace_deterministic_evidence(self):
        result = self.evaluate(
            "raw chicken breast", [_candidate(10, "Chicken breast, raw")],
            slm={"status": "REVIEW_REQUIRED", "reason": "SLM inference is unavailable (ImportError)."},
        )
        self.assertTrue(result["eligible"])
        self.assertTrue(result["slm_failure"])

    def test_no_candidate_stays_unresolved(self):
        result = self.evaluate("raw chicken breast", [])
        self.assertFalse(result["eligible"])
        self.assertEqual(result["status"], "UNRESOLVED")


class AutomaticVerificationAndPremappingTests(unittest.TestCase):
    def setUp(self):
        self.kg = InMemoryFoodGraph({"fdc:1": _food(1, "Chicken breast, raw")})
        self.mapper = USDAIngredientMapper(self.kg, GraphRetriever(self.kg))
        self.policy = AutomaticIngredientMappingPolicy()

    def test_automatic_verification_persists_distinct_provenance_and_no_reviewer(self):
        item = parse_foodcom_ingredient("raw chicken breast", "8 oz")
        mapping = self.mapper.retrieve_candidates(item, dataset_scope="foundation_foods")
        self.assertTrue(self.policy.evaluate(item, mapping)["eligible"])
        saved = self.mapper.verify_candidate_automatically(
            item, mapping, self.policy, enabled=True, dataset_scope="foundation_foods",
        )
        record = self.kg.get_ingredient_mapping_cache(mapping["cache_key"])
        self.assertEqual(saved["verification_method"], "DETERMINISTIC_POLICY")
        self.assertEqual(record["verification_policy_version"], self.policy.VERSION)
        self.assertEqual(record["verification_evidence"]["fdc_id"], 1)
        self.assertIsNone(record.get("reviewer"))
        self.assertIsNone(record.get("validated_by"))

    def test_automatic_verification_requires_explicit_enable(self):
        item = parse_foodcom_ingredient("raw chicken breast", "8 oz")
        mapping = self.mapper.retrieve_candidates(item, dataset_scope="foundation_foods")
        with self.assertRaises(MappingValidationError):
            self.mapper.verify_candidate_automatically(item, mapping, self.policy,
                                                       dataset_scope="foundation_foods")

    def test_human_verified_mapping_is_preserved_by_automatic_path(self):
        item = parse_foodcom_ingredient("raw chicken breast", "8 oz")
        mapping = self.mapper.retrieve_candidates(item, dataset_scope="foundation_foods")
        human = self.mapper.verify_candidate(item, 1, validated_by="reviewer-7",
                                             dataset_scope="foundation_foods")
        outcome = self.mapper.verify_candidate_automatically(
            item, mapping, self.policy, enabled=True, dataset_scope="foundation_foods",
        )
        self.assertEqual(outcome["cache_status"], "HUMAN_VERIFIED_PRESERVED")
        cached = self.kg.get_ingredient_mapping_cache(mapping["cache_key"])
        self.assertEqual(cached["verification_method"], "human_review")
        self.assertEqual(cached["validated_by"], "reviewer-7")

    def test_stale_automatic_policy_is_retrieved_again_then_same_id_provenance_upgrades(self):
        item = parse_foodcom_ingredient("raw chicken breast", "8 oz")
        mapping = self.mapper.retrieve_candidates(item, dataset_scope="foundation_foods")
        self.mapper.verify_candidate_automatically(
            item, mapping, self.policy, enabled=True, dataset_scope="foundation_foods",
        )
        old_key = mapping["cache_key"]
        new_policy = AutomaticIngredientMappingPolicy()
        new_policy.VERSION = self.policy.VERSION + 1
        refreshed = self.mapper.retrieve_candidates(
            item, dataset_scope="foundation_foods", persist=True,
            reuse_verified_policy_version=new_policy.VERSION,
        )
        self.assertEqual(refreshed["cache_status"], "STALE_VERIFIED_POLICY")
        self.assertEqual(refreshed["previous_verified_mapping"]["fdc_id"], 1)
        upgraded = self.mapper.verify_candidate_automatically(
            item, refreshed, new_policy, enabled=True, dataset_scope="foundation_foods",
        )
        self.assertEqual(upgraded["cache_status"], "POLICY_PROVENANCE_UPGRADED")
        self.assertEqual(self.kg.get_ingredient_mapping_cache(old_key)["verification_policy_version"],
                         new_policy.VERSION)

    def test_stale_automatic_mapping_is_not_overwritten_when_retrieval_selects_another_id(self):
        item = parse_foodcom_ingredient("raw chicken breast", "8 oz")
        mapping = self.mapper.retrieve_candidates(item, dataset_scope="foundation_foods")
        self.mapper.verify_candidate_automatically(
            item, mapping, self.policy, enabled=True, dataset_scope="foundation_foods",
        )
        key = mapping["cache_key"]
        original = self.kg.get_ingredient_mapping_cache(key)
        self.kg.ingredients = {"fdc:2": _food(2, "Chicken breast, raw")}
        new_policy = AutomaticIngredientMappingPolicy()
        new_policy.VERSION = self.policy.VERSION + 1
        refreshed = self.mapper.retrieve_candidates(
            item, dataset_scope="foundation_foods", reuse_verified_policy_version=new_policy.VERSION,
        )
        self.assertEqual([candidate["fdc_id"] for candidate in refreshed["candidates"]], [2])
        with self.assertRaises(MappingValidationError):
            self.mapper.verify_candidate_automatically(
                item, refreshed, new_policy, enabled=True, dataset_scope="foundation_foods",
            )
        preserved = self.kg.get_ingredient_mapping_cache(key)
        self.assertEqual(preserved["fdc_id"], original["fdc_id"])
        self.assertEqual(preserved["verification_policy_version"], original["verification_policy_version"])

    def test_stale_automatic_provenance_is_not_upgraded_when_candidate_evidence_changes(self):
        item = parse_foodcom_ingredient("raw chicken breast", "8 oz")
        mapping = self.mapper.retrieve_candidates(item, dataset_scope="foundation_foods")
        self.mapper.verify_candidate_automatically(
            item, mapping, self.policy, enabled=True, dataset_scope="foundation_foods",
        )
        original = self.kg.get_ingredient_mapping_cache(mapping["cache_key"])
        self.kg.ingredients["fdc:1"]["aliases"] = ["other curated label"]
        new_policy = AutomaticIngredientMappingPolicy()
        new_policy.VERSION = self.policy.VERSION + 1
        refreshed = self.mapper.retrieve_candidates(
            item, dataset_scope="foundation_foods", reuse_verified_policy_version=new_policy.VERSION,
        )
        self.assertEqual(refreshed["candidates"][0]["fdc_id"], 1)
        with self.assertRaises(MappingValidationError):
            self.mapper.verify_candidate_automatically(
                item, refreshed, new_policy, enabled=True, dataset_scope="foundation_foods",
            )
        preserved = self.kg.get_ingredient_mapping_cache(mapping["cache_key"])
        self.assertEqual(preserved["verification_policy_version"], original["verification_policy_version"])

    def test_workflow_dry_run_has_zero_persistent_writes_and_separate_coverage(self):
        kg = InMemoryFoodGraph(
            {"fdc:1": _food(1, "Chicken breast, raw")},
            [_recipe(1, "raw chicken breast"), _recipe(2, "raw chicken breast")],
        )
        mapper = USDAIngredientMapper(kg, GraphRetriever(kg))
        report = IngredientPremappingWorkflow(kg, mapper).run(
            dataset_scope="foundation_foods", max_recipes=2, max_ingredients=5,
            batch_size=5, recipe_batch_size=1,
        )
        self.assertTrue(report["dry_run"])
        self.assertEqual(kg.write_calls, [])
        self.assertEqual(report["total_ingredient_occurrences"], 2)
        self.assertEqual(report["unique_context_aware_ingredient_keys"], 1)
        self.assertEqual(report["candidate_retrieval_coverage"]["unique_with_candidates"], 1)
        self.assertEqual(report["verified_mapping_coverage"]["current_verified_unique"], 0)
        self.assertEqual(report["items"][0]["status"], "ELIGIBLE_DRY_RUN")

    def test_max_ingredients_caps_selected_keys_after_frequency_ordering(self):
        kg = InMemoryFoodGraph(
            {
                "fdc:1": _food(1, "Apple"),
                "fdc:2": _food(2, "Banana"),
                "fdc:3": _food(3, "Carrot"),
            },
            [
                _recipe(1, "apple"),
                _recipe(2, "apple"),
                _recipe(3, "banana"),
                _recipe(4, "carrot"),
            ],
        )
        report = IngredientPremappingWorkflow(
            kg, USDAIngredientMapper(kg, GraphRetriever(kg)),
        ).run(dataset_scope="foundation_foods", max_recipes=4,
              max_ingredients=2, batch_size=5)

        self.assertEqual(report["unique_context_aware_ingredient_keys"], 3)
        self.assertEqual(report["selected_ingredient_keys"], 2)
        self.assertEqual(report["processed_unique_ingredient_keys"], 2)
        self.assertEqual(
            [item["normalized_name"] for item in report["items"]],
            ["apple", "banana"],
        )

    def test_report_separates_candidate_coverage_from_ambiguous_and_unresolved_results(self):
        kg = InMemoryFoodGraph(
            {
                "fdc:1": _food(1, "Chicken breast, raw"),
                "fdc:2": _food(2, "Chicken breast, raw"),
            },
            [_recipe(1, "raw chicken breast", "dragonfruit")],
        )
        report = IngredientPremappingWorkflow(kg, USDAIngredientMapper(kg, GraphRetriever(kg))).run(
            dataset_scope="foundation_foods", max_recipes=1, max_ingredients=5,
        )
        self.assertEqual(report["ambiguous_mappings"], 1)
        self.assertEqual(report["unresolved_mappings"], 1)
        self.assertEqual(report["candidate_retrieval_coverage"]["rate_over_processed"], 0.5)
        self.assertEqual(report["verified_mapping_coverage"]["rate_over_processed"], 0.0)

    def test_workflow_requires_write_opt_in_and_enforces_per_invocation_caps(self):
        kg = InMemoryFoodGraph({}, [])
        workflow = IngredientPremappingWorkflow(kg, USDAIngredientMapper(kg, GraphRetriever(kg)))
        with self.assertRaises(ValueError):
            workflow.run(dry_run=False, write_enabled=False)
        with self.assertRaises(ValueError):
            workflow.run(max_recipes=workflow.MAX_SAMPLE_RECIPES + 1)
        with self.assertRaises(ValueError):
            workflow.run(batch_size=workflow.MAX_BATCH_SIZE + 1)
        self.assertEqual(kg.write_calls, [])

    def test_slm_failure_is_counted_while_deterministic_policy_remains_auditable(self):
        kg = InMemoryFoodGraph(
            {"fdc:1": _food(1, "Chicken breast, raw")}, [_recipe(1, "raw chicken breast")],
        )
        mapper = USDAIngredientMapper(kg, GraphRetriever(kg))

        def unavailable(_payload):
            raise ImportError("model runtime unavailable")

        validator = SLMIngredientMappingValidator(evaluator=unavailable)
        report = IngredientPremappingWorkflow(kg, mapper, validator=validator).run(
            dataset_scope="foundation_foods", max_recipes=1, max_ingredients=1,
        )
        self.assertEqual(report["slm_evaluation_failures"], 1)
        self.assertEqual(report["items"][0]["status"], "ELIGIBLE_DRY_RUN")
        self.assertEqual(kg.write_calls, [])

    def test_checkpoint_resumes_bounded_batches_without_reprocessing_completed_items(self):
        kg = InMemoryFoodGraph(
            {"fdc:1": _food(1, "Chicken breast, raw"), "fdc:2": _food(2, "Apple")},
            [_recipe(1, "raw chicken breast", "apple")],
        )
        mapper = USDAIngredientMapper(kg, GraphRetriever(kg))
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "premap.json"
            first = IngredientPremappingWorkflow(kg, mapper).run(
                dataset_scope="foundation_foods", max_recipes=1, max_ingredients=2,
                batch_size=1, checkpoint_path=checkpoint,
            )
            after_first_calls = len(kg.search_calls)
            restarted = IngredientPremappingWorkflow(kg, mapper).run(
                dataset_scope="foundation_foods", max_recipes=1, max_ingredients=2,
                batch_size=1, checkpoint_path=checkpoint,
            )
            done = IngredientPremappingWorkflow(kg, mapper).run(
                dataset_scope="foundation_foods", max_recipes=1, max_ingredients=2,
                batch_size=1, checkpoint_path=checkpoint,
            )
        self.assertEqual(first["batch_processed_this_run"], 1)
        self.assertEqual(restarted["batch_processed_this_run"], 1)
        self.assertEqual(restarted["processed_unique_ingredient_keys"], 2)
        self.assertEqual(done["batch_processed_this_run"], 0)
        self.assertEqual(len(kg.search_calls), after_first_calls + 1)

    def test_persistent_batch_is_idempotent_and_verified_cache_is_reused(self):
        kg = InMemoryFoodGraph(
            {"fdc:1": _food(1, "Chicken breast, raw")}, [_recipe(1, "raw chicken breast")],
        )
        mapper = USDAIngredientMapper(kg, GraphRetriever(kg))
        workflow = IngredientPremappingWorkflow(kg, mapper)
        first = workflow.run(dataset_scope="foundation_foods", max_recipes=1,
                             max_ingredients=1, batch_size=1,
                             dry_run=False, write_enabled=True)
        record = next(iter(kg.records.values()))
        writes_after_first = len(kg.write_calls)
        second = workflow.run(dataset_scope="foundation_foods", max_recipes=1,
                              max_ingredients=1, batch_size=1,
                              dry_run=False, write_enabled=True)
        self.assertEqual(first["automatically_verified_mappings"], 1)
        self.assertEqual(record["verification_method"], "DETERMINISTIC_POLICY")
        self.assertEqual(second["existing_verified_cache_hits"], 1)
        self.assertEqual(len(kg.write_calls), writes_after_first)

    def test_persistent_mode_reprocesses_dry_run_checkpoint_once(self):
        kg = InMemoryFoodGraph(
            {"fdc:1": _food(1, "Chicken breast, raw")}, [_recipe(1, "raw chicken breast")],
        )
        mapper = USDAIngredientMapper(kg, GraphRetriever(kg))
        with tempfile.TemporaryDirectory() as tmp:
            checkpoint = Path(tmp) / "premap.json"
            workflow = IngredientPremappingWorkflow(kg, mapper)
            read_only = workflow.run(dataset_scope="foundation_foods", max_recipes=1,
                                     max_ingredients=1, batch_size=1,
                                     checkpoint_path=checkpoint)
            self.assertEqual(read_only["automatically_verified_mappings"], 0)
            self.assertEqual(kg.write_calls, [])
            persisted = workflow.run(dataset_scope="foundation_foods", max_recipes=1,
                                     max_ingredients=1, batch_size=1,
                                     dry_run=False, write_enabled=True,
                                     checkpoint_path=checkpoint)
            writes_after = len(kg.write_calls)
            resumed = workflow.run(dataset_scope="foundation_foods", max_recipes=1,
                                   max_ingredients=1, batch_size=1,
                                   dry_run=False, write_enabled=True,
                                   checkpoint_path=checkpoint)
        self.assertEqual(persisted["automatically_verified_mappings"], 1)
        self.assertGreater(writes_after, 0)
        self.assertEqual(resumed["batch_processed_this_run"], 0)
        self.assertEqual(len(kg.write_calls), writes_after)

    def test_step_one_parsed_item_and_step_three_validator_remain_compatible(self):
        item = {"name": "raw chicken breast", "quantity_raw": "8 oz",
                "parsed": parse_foodcom_ingredient("raw chicken breast", "8 oz")}
        mapping = {
            "ingredient": "raw chicken breast", "normalized_name": "raw chicken breast",
            "mapping_version": "fdc-mapping-v1", "cache_key": "compatibility",
            "attributes": {"food_state": ["raw"], "cut": ["breast"]},
            "candidates": [_candidate(1, "Chicken breast, raw")],
        }
        validator = SLMIngredientMappingValidator(
            evaluator=lambda _payload: {
                "status": "RECOMMENDED", "recommended_fdc_id": 1,
                "compatible_candidate_ids": [1], "unresolved_attributes": [],
                "reason": "Clear exact candidate.", "requires_review": False,
            },
        )
        evaluation = validator.evaluate(item, mapping, persist=False)
        decision = self.policy.evaluate(item, mapping, slm_evaluation=evaluation)
        self.assertEqual(evaluation["status"], "RECOMMENDED")
        self.assertFalse(evaluation["requires_review"])
        self.assertTrue(decision["eligible"])


if __name__ == "__main__":
    unittest.main()

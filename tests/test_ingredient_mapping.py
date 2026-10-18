import json
import re
import unittest

from scripts.import_foodcom_recipes import _recipe_row
from src.graph.neo4j_graph import Neo4jKnowledgeGraph
from src.recipes.ingredient_mapping import (
    MappingValidationError, USDAIngredientMapper,
)
from src.recipes.ingredient_parser import parse_foodcom_ingredient
from src.retrieval.graph_retriever import GraphRetriever


def _food(food_id, description, *, aliases=(), dataset="foundation_foods", data_type="Foundation"):
    return {
        "name": description,
        "fdc_description": description,
        "aliases": list(aliases),
        "food_state": description,
        "source": {
            "fdc_id": food_id,
            "data_type": data_type,
            "dataset_key": dataset,
            "name": f"USDA FoodData Central, {dataset}",
        },
    }


class MemoryCatalog:
    def __init__(self, ingredients):
        self.ingredients = ingredients
        self.records = {}
        self.search_calls = []
        self.fail_search = False
        self.fail_cache_read = False
        self.fail_cache_write = False

    def search(self, index_name, search_query, k=5, dataset_scope="all"):
        if self.fail_search:
            raise ConnectionError("Neo4j full-text index unavailable")
        self.search_calls.append((search_query, dataset_scope))
        terms = set(re.findall(r"[a-z0-9]+", search_query.casefold()))
        rows = []
        for food_id, data in self.ingredients.items():
            source = data.get("source", {})
            if dataset_scope != "all" and source.get("dataset_key") != dataset_scope:
                continue
            labels = [data.get("name", ""), *(data.get("aliases") or [])]
            label_tokens = set(re.findall(r"[a-z0-9]+", " ".join(labels).casefold()))
            if terms & label_tokens:
                rows.append({
                    "id": food_id,
                    "name": data.get("name"),
                    "aliases": data.get("aliases", []),
                    "dataset_key": source.get("dataset_key"),
                    "score": 2.0,
                })
        return rows[:k]

    def get_ingredient_mapping_cache(self, cache_key):
        if self.fail_cache_read:
            raise ConnectionError("mapping cache unavailable")
        return self.records.get(cache_key)

    def save_ingredient_mapping_cache(self, record):
        if self.fail_cache_write:
            raise ConnectionError("mapping cache write failed")
        old = self.records.get(record["cache_key"])
        if old and old.get("status") == "VERIFIED" and record.get("status") != "VERIFIED":
            return old
        if record.get("status") == "VERIFIED":
            if (record.get("validation_method") != "human_review"
                    or not record.get("validated_by")
                    or not any(str(item.get("fdc_id")) == str(record.get("fdc_id"))
                               for item in record.get("candidates", []))):
                raise ValueError("verification policy not satisfied")
        if old and old.get("candidates") != record.get("candidates"):
            old.pop("slm_evaluation", None)
        self.records[record["cache_key"]] = {**(old or {}), **record}
        return self.records[record["cache_key"]]

    def save_ingredient_mapping_evaluation(self, cache_key, evaluation):
        record = self.records.get(cache_key)
        if not record:
            return None
        current_ids = {str(item.get("fdc_id")) for item in record.get("candidates", [])}
        evaluation_ids = {str(item) for item in evaluation.get("candidate_ids", [])}
        if current_ids != evaluation_ids:
            raise ValueError("candidate IDs differ")
        record["slm_evaluation"] = dict(evaluation)
        return record


def _salmon_catalog():
    return MemoryCatalog({
        "fdc:1": _food(1, "Apple"),
        "fdc:2": _food(2, "Green-Apple, raw", dataset="sr_legacy", data_type="SR Legacy"),
        "fdc:3": _food(3, "Salmon, Atlantic, raw"),
        "fdc:4": _food(4, "Salmon, Atlantic, cooked"),
        "fdc:5": _food(5, "Salmon, pink", aliases=("pink salmon",)),
        "fdc-sr-legacy:6": _food(6, "Salmon, Atlantic, raw", dataset="sr_legacy",
                                 data_type="SR Legacy"),
    })


class USDAIngredientMapperTests(unittest.TestCase):
    def setUp(self):
        self.kg = _salmon_catalog()
        self.retriever = GraphRetriever(self.kg)
        self.mapper = USDAIngredientMapper(self.kg, self.retriever)

    def test_exact_usda_name_candidate_has_source_id_and_real_retrieval_score(self):
        result = self.mapper.retrieve_candidates(parse_foodcom_ingredient("apple", "1"),
                                                 dataset_scope="foundation_foods")
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["candidates"][0]["fdc_id"], 1)
        self.assertEqual(result["candidates"][0]["retrieval_method"], "exact_name")
        self.assertEqual(result["candidates"][0]["retrieval_score"], 1.25)
        self.assertEqual(result["candidates"][0]["data_type"], "Foundation")

    def test_normalized_matching_uses_existing_ranked_resolver(self):
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "green apple", "normalized_name": "green apple",
        }, dataset_scope="sr_legacy")
        self.assertEqual(result["candidates"][0]["fdc_id"], 2)
        self.assertEqual(result["candidates"][0]["retrieval_method"], "normalized_match")
        self.assertGreater(result["candidates"][0]["retrieval_score"], 0)
        self.assertLess(result["candidates"][0]["retrieval_score"], 1.0)

    def test_known_synonym_is_reported_as_synonym_match(self):
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "pink salmon", "normalized_name": "pink salmon",
        })
        candidate = next(item for item in result["candidates"] if item["fdc_id"] == 5)
        self.assertEqual(candidate["retrieval_method"], "synonym_match")

    def test_multiple_candidates_are_preserved_as_ambiguous(self):
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "salmon", "normalized_name": "salmon",
        }, dataset_scope="foundation_foods")
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertGreaterEqual(len(result["candidates"]), 2)
        self.assertIsNone(result.get("fdc_id"))

    def test_no_candidate_is_persisted_as_unresolved(self):
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "dragonfruit", "normalized_name": "dragonfruit",
        })
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["candidates"], [])
        self.assertEqual(result["cache_status"], "SAVED")
        record = self.kg.records[result["cache_key"]]
        self.assertEqual(record["status"], "UNRESOLVED")

    def test_search_failure_is_explicit_and_is_not_cached_as_unresolved(self):
        self.kg.fail_search = True
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "salmon", "normalized_name": "salmon",
        })
        self.assertEqual(result["status"], "RETRIEVAL_ERROR")
        self.assertIn("unavailable", result["error"])
        self.assertEqual(self.kg.records, {})

    def test_cache_read_failure_does_not_masquerade_as_no_food_match(self):
        self.kg.fail_cache_read = True
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "apple", "normalized_name": "apple",
        }, dataset_scope="foundation_foods")
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["cache_status"], "SAVED")
        self.assertIn("cache_read_error", result)

    def test_cache_write_failure_keeps_candidate_result_and_reports_failure(self):
        self.kg.fail_cache_write = True
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "apple", "normalized_name": "apple",
        }, dataset_scope="foundation_foods")
        self.assertEqual(result["status"], "CANDIDATE")
        self.assertEqual(result["cache_status"], "WRITE_ERROR")
        self.assertIn("cache_error", result)

    def test_verified_cache_reuse_and_review_policy(self):
        ingredient = {"ingredient_name": "apple", "normalized_name": "apple"}
        candidate_result = self.mapper.retrieve_candidates(ingredient, dataset_scope="foundation_foods")
        self.assertEqual(candidate_result["status"], "CANDIDATE")
        self.assertNotEqual(candidate_result["status"], "VERIFIED")
        with self.assertRaises(MappingValidationError):
            self.mapper.verify_candidate(ingredient, 999, validated_by="reviewer",
                                         dataset_scope="foundation_foods")
        with self.assertRaises(MappingValidationError):
            self.mapper.verify_candidate(ingredient, 1, validated_by="", validation_method="human_review",
                                         dataset_scope="foundation_foods")
        with self.assertRaises(MappingValidationError):
            self.mapper.verify_candidate(ingredient, 1, validated_by="reviewer-1",
                                         validation_method="retrieval",
                                         dataset_scope="foundation_foods")

        verified = self.mapper.verify_candidate(ingredient, 1, validated_by="reviewer-1",
                                                dataset_scope="foundation_foods")
        self.assertEqual(verified["status"], "VERIFIED")
        self.assertEqual(verified["validation_method"], "human_review")
        search_count = len(self.kg.search_calls)
        reused = self.mapper.retrieve_candidates(ingredient, dataset_scope="foundation_foods")
        self.assertEqual(reused["status"], "VERIFIED")
        self.assertTrue(reused["cache_hit"])
        self.assertEqual(reused["fdc_id"], 1)
        self.assertEqual(len(self.kg.search_calls), search_count)

    def test_mapping_version_changes_cache_key_and_forces_retrieval(self):
        ingredient = {"ingredient_name": "apple", "normalized_name": "apple"}
        first = self.mapper.retrieve_candidates(ingredient)
        second_mapper = USDAIngredientMapper(self.kg, self.retriever, mapping_version="fdc-mapping-v2")
        second = second_mapper.retrieve_candidates(ingredient)
        self.assertNotEqual(first["cache_key"], second["cache_key"])
        self.assertFalse(second["cache_hit"])
        self.assertEqual(second["mapping_version"], "fdc-mapping-v2")

    def test_raw_and_cooked_attributes_use_separate_cache_entries_and_candidates(self):
        raw = {"ingredient_name": "raw salmon", "normalized_name": "raw salmon"}
        cooked = {"ingredient_name": "cooked salmon", "normalized_name": "cooked salmon"}
        raw_result = self.mapper.retrieve_candidates(raw, dataset_scope="foundation_foods")
        cooked_result = self.mapper.retrieve_candidates(cooked, dataset_scope="foundation_foods")
        self.assertNotEqual(raw_result["cache_key"], cooked_result["cache_key"])
        self.assertTrue(all("raw" in item["food_state"].casefold()
                            for item in raw_result["candidates"]))
        self.assertTrue(all("cooked" in item["food_state"].casefold()
                            for item in cooked_result["candidates"]))

    def test_generic_oil_is_never_automatically_verified(self):
        result = self.mapper.retrieve_candidates({
            "ingredient_name": "oil", "normalized_name": "oil",
        })
        self.assertIn(result["status"], {"CANDIDATE", "AMBIGUOUS", "UNRESOLVED"})
        self.assertNotEqual(result["status"], "VERIFIED")

    def test_accepts_step_one_parsed_ingredient_item(self):
        recipe = _recipe_row({
            "RecipeId": "7", "Name": "Fish", "RecipeIngredientParts": '["salmon steaks"]',
            "RecipeIngredientQuantities": '["4 oz"]', "RecipeServings": "1",
            "RecipeInstructions": '["Cook"]',
        })
        parsed_item = json.loads(recipe["properties"]["details_json"])["ingredient_items"][0]
        result = self.mapper.retrieve_candidates(parsed_item, dataset_scope="foundation_foods")
        self.assertEqual(result["normalized_name"], "salmon")
        self.assertEqual(result["attributes"]["cut"], ["steaks"])
        self.assertIn(result["status"], {"CANDIDATE", "AMBIGUOUS"})


class Neo4jFoodComBatchReaderTests(unittest.TestCase):
    def test_foodcom_recipe_reader_is_read_only_keyset_paged_and_bounded(self):
        graph = object.__new__(Neo4jKnowledgeGraph)
        calls = []

        def read_rows(query, **parameters):
            calls.append((query, parameters))
            return [{"recipe_id": "foodcom:8", "details_json": "{}"}]

        graph._rows = read_rows
        result = graph.get_foodcom_recipe_batch(after_recipe_id="foodcom:7", limit=5000)
        self.assertEqual(result[0]["recipe_id"], "foodcom:8")
        query, parameters = calls[0]
        self.assertIn("WHERE recipe.id > $after_recipe_id", query)
        self.assertIn("ORDER BY recipe.id LIMIT $limit", query)
        self.assertEqual(parameters["after_recipe_id"], "foodcom:7")
        self.assertEqual(parameters["limit"], 1000)
        self.assertNotRegex(query, r"\b(CREATE|MERGE|SET|DELETE|REMOVE)\b")


class Neo4jMappingCacheTests(unittest.TestCase):
    class Record:
        def __init__(self, value):
            self.value = value

        def data(self):
            return self.value

    class Session:
        def __init__(self, records):
            self.records = records

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def run(self, query, **parameters):
            if query.startswith("CREATE CONSTRAINT"):
                return []
            if ("mapping.automatic_policy_evaluation_json = $evidence_json" in query
                    and "WHERE mapping.status" in query):
                record = self.records.get(parameters["cache_key"])
                if (record and record.get("status") == "VERIFIED"
                        and record.get("verification_method") == "DETERMINISTIC_POLICY"
                        and str(record.get("fdc_id")) == str(parameters["fdc_id"])):
                    record.update(
                        verification_policy_version=parameters["policy_version"],
                        verification_evidence_json=parameters["evidence_json"],
                        automatic_policy_evaluation_json=parameters["evidence_json"],
                        validated_at=parameters["updated_at"], updated_at=parameters["updated_at"],
                    )
                return [Neo4jMappingCacheTests.Record({"record": dict(record)})] if record else []
            if ("SET mapping.automatic_policy_evaluation_json = $evaluation_json" in query
                    and "WHERE mapping.status" not in query):
                record = self.records.get(parameters["cache_key"])
                if not record:
                    return []
                record["automatic_policy_evaluation_json"] = parameters["evaluation_json"]
                record["automatic_policy_evaluation_updated_at"] = parameters["evaluated_at"]
                return [Neo4jMappingCacheTests.Record({"record": dict(record)})]
            if ("SET mapping.slm_evaluation_json" in query
                    and "MERGE (mapping:IngredientMapping" not in query):
                record = self.records.get(parameters["cache_key"])
                if not record:
                    return []
                record["slm_evaluation_json"] = parameters["evaluation_json"]
                record["slm_evaluation_updated_at"] = parameters["evaluated_at"]
                return [Neo4jMappingCacheTests.Record({"record": dict(record)})]
            if "MATCH (mapping:IngredientMapping" in query:
                record = self.records.get(parameters["cache_key"])
                return [Neo4jMappingCacheTests.Record({"record": dict(record)})] if record else []
            if "MERGE (mapping:IngredientMapping" in query:
                key = parameters["cache_key"]
                record = self.records.setdefault(key, {
                    "cache_key": key, "created_at": parameters["created_at"],
                })
                if record.get("status") != "VERIFIED":
                    if (record.get("status") != "VERIFIED"
                            and record.get("candidates_json") != parameters["candidate_json"]):
                        record.pop("slm_evaluation_json", None)
                        record.pop("slm_evaluation_updated_at", None)
                    record.update(parameters["properties"])
                return [Neo4jMappingCacheTests.Record({"record": dict(record)})]
            raise AssertionError(f"Unexpected Cypher query: {query}")

    class Driver:
        def __init__(self):
            self.records = {}

        def session(self, database=None):
            return Neo4jMappingCacheTests.Session(self.records)

    def test_cache_json_round_trip_and_verified_status_guard(self):
        graph = object.__new__(Neo4jKnowledgeGraph)
        graph.driver = self.Driver()
        graph.database = "neo4j"
        graph._ingredient_mapping_cache_ready = False
        common = {
            "cache_key": "test-context-key", "ingredient_name": "raw salmon",
            "normalized_name": "raw salmon", "attributes": {"food_state": ["raw"]},
            "candidates": [{"fdc_id": 321, "description": "Salmon, raw"}],
            "status": "CANDIDATE", "mapping_version": "fdc-mapping-v1",
            "dataset_scope": "all", "created_at": "2026-10-16T00:00:00+00:00",
            "updated_at": "2026-10-16T00:00:00+00:00",
        }
        graph.save_ingredient_mapping_cache(common)
        loaded = graph.get_ingredient_mapping_cache(common["cache_key"])
        self.assertEqual(loaded["attributes"], common["attributes"])
        self.assertEqual(loaded["candidates"], common["candidates"])
        self.assertEqual(loaded["status"], "CANDIDATE")

        unsafe = {**common, "status": "VERIFIED", "fdc_id": 321,
                  "validation_method": "retrieval", "validated_by": "retriever"}
        with self.assertRaises(ValueError):
            graph.save_ingredient_mapping_cache(unsafe)
        verified = {**common, "status": "VERIFIED", "fdc_id": 321,
                    "fdc_description": "Salmon, raw", "selected_candidate": common["candidates"][0],
                    "validation_method": "human_review", "validated_by": "reviewer"}
        graph.save_ingredient_mapping_cache(verified)
        stale_candidate_update = {**common, "status": "AMBIGUOUS"}
        graph.save_ingredient_mapping_cache(stale_candidate_update)
        self.assertEqual(graph.get_ingredient_mapping_cache(common["cache_key"])["status"], "VERIFIED")

        evaluation = {
            "candidate_ids": [321], "recommended_fdc_id": 321,
            "status": "RECOMMENDED", "evaluated_at": "2026-10-16T00:00:00+00:00",
        }
        saved = graph.save_ingredient_mapping_evaluation(common["cache_key"], evaluation)
        self.assertEqual(saved["slm_evaluation"], evaluation)
        loaded = graph.get_ingredient_mapping_cache(common["cache_key"])
        self.assertEqual(loaded["status"], "VERIFIED")
        self.assertEqual(loaded["fdc_id"], 321)
        self.assertEqual(loaded["validated_by"], "reviewer")
        with self.assertRaises(ValueError):
            graph.save_ingredient_mapping_evaluation(common["cache_key"], {**evaluation, "candidate_ids": [999]})

    def test_candidate_refresh_clears_stale_slm_evaluation(self):
        graph = object.__new__(Neo4jKnowledgeGraph)
        graph.driver = self.Driver()
        graph.database = "neo4j"
        graph._ingredient_mapping_cache_ready = False
        common = {
            "cache_key": "refresh-context-key", "ingredient_name": "apple",
            "normalized_name": "apple", "attributes": {}, "dataset_scope": "all",
            "candidates": [{"fdc_id": 321, "description": "Apple"}],
            "status": "CANDIDATE", "mapping_version": "fdc-mapping-v1",
            "created_at": "2026-10-16T00:00:00+00:00", "updated_at": "2026-10-16T00:00:00+00:00",
        }
        graph.save_ingredient_mapping_cache(common)
        graph.save_ingredient_mapping_evaluation(common["cache_key"], {
            "candidate_ids": [321], "status": "RECOMMENDED",
            "evaluated_at": "2026-10-16T00:00:00+00:00",
        })
        refreshed = {**common, "candidates": [{"fdc_id": 322, "description": "Apple, cooked"}]}
        loaded = graph.save_ingredient_mapping_cache(refreshed)
        self.assertIsNone(loaded.get("slm_evaluation"))

    def test_automatic_provenance_round_trip_policy_evaluation_and_same_id_upgrade(self):
        graph = object.__new__(Neo4jKnowledgeGraph)
        graph.driver = self.Driver()
        graph.database = "neo4j"
        graph._ingredient_mapping_cache_ready = False
        candidate = {"fdc_id": 876, "description": "Chicken breast, raw"}
        common = {
            "cache_key": "automatic-provenance-key", "ingredient_name": "raw chicken breast",
            "normalized_name": "raw chicken breast", "attributes": {"food_state": ["raw"]},
            "candidates": [candidate], "status": "CANDIDATE",
            "mapping_version": "fdc-mapping-v1", "dataset_scope": "all",
            "created_at": "2026-10-16T00:00:00+00:00", "updated_at": "2026-10-16T00:00:00+00:00",
        }
        graph.save_ingredient_mapping_cache(common)
        evidence = {
            "eligible": True, "fdc_id": 876, "policy_version": 1,
            "candidate_ids": [876], "verification_method": "DETERMINISTIC_POLICY",
        }
        automatic = {
            **common, "status": "VERIFIED", "fdc_id": 876,
            "selected_candidate": candidate, "verification_method": "DETERMINISTIC_POLICY",
            "validation_method": "DETERMINISTIC_POLICY", "verification_policy_version": 1,
            "verification_evidence": evidence, "automatic_policy_evaluation": evidence,
            "reviewer": None, "validated_by": None,
        }
        with self.assertRaises(ValueError):
            graph.save_ingredient_mapping_cache({**automatic, "verification_evidence": {"eligible": False}})
        saved = graph.save_ingredient_mapping_cache(automatic)
        self.assertEqual(saved["verification_method"], "DETERMINISTIC_POLICY")
        self.assertEqual(saved["verification_evidence"], evidence)
        self.assertIsNone(saved.get("reviewer"))

        other_candidate = {"fdc_id": 999, "description": "Chicken breast, raw"}
        other_evidence = {"eligible": True, "fdc_id": 999, "policy_version": 2}
        protected = graph.save_ingredient_mapping_cache({
            **automatic, "fdc_id": 999, "selected_candidate": other_candidate,
            "candidates": [candidate, other_candidate], "verification_policy_version": 2,
            "verification_evidence": other_evidence,
            "automatic_policy_evaluation": other_evidence,
        })
        self.assertEqual(protected["fdc_id"], 876)

        audit = {**evidence, "status": "ELIGIBLE", "evaluated_at": "2026-10-16T00:01:00+00:00"}
        graph.save_ingredient_mapping_policy_evaluation(common["cache_key"], audit)
        self.assertEqual(graph.get_ingredient_mapping_cache(common["cache_key"])["status"], "VERIFIED")
        upgraded = graph.upgrade_automatic_mapping_policy(common["cache_key"], 876, 2,
                                                           {**audit, "policy_version": 2})
        self.assertEqual(upgraded["verification_policy_version"], 2)
        with self.assertRaises(ValueError):
            graph.upgrade_automatic_mapping_policy(common["cache_key"], 999, 3,
                                                    {**audit, "fdc_id": 999, "policy_version": 3})


if __name__ == "__main__":
    unittest.main()

import unittest
import json
import io
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from scripts.import_foundation_neo4j import _food_rows, _iter_json_array
from src.retrieval.graph_retriever import GraphRetriever
from src.web.request_planner import RequestPlanner
from src.web.service import RenAllyWebService


class FoodDatasetImportTests(unittest.TestCase):
    def test_sr_legacy_rows_keep_source_identity_and_nutrients(self):
        source = {
            "SRLegacyFoods": [{
                "fdcId": 123456,
                "dataType": "SR Legacy",
                "description": "Salmon, cooked",
                "foodCategory": {"description": "Finfish and Shellfish Products"},
                "foodNutrients": [{
                    "nutrient": {"id": 1008, "name": "Energy", "unitName": "kcal"},
                    "amount": 206,
                }],
            }],
        }

        food_rows, nutrient_rows, invalid_count = _food_rows(
            source, {}, "2018-04", "sr_legacy",
        )

        self.assertEqual(invalid_count, 0)
        self.assertEqual(food_rows[0]["id"], "fdc-sr-legacy:123456")
        self.assertEqual(food_rows[0]["properties"]["dataset_key"], "sr_legacy")
        self.assertEqual(
            food_rows[0]["properties"]["source_dataset"],
            "USDA FoodData Central, SR Legacy",
        )
        self.assertEqual(food_rows[0]["properties"]["release_date"], "2018-04")
        self.assertEqual(nutrient_rows[0]["food_id"], "fdc-sr-legacy:123456")
        self.assertEqual(nutrient_rows[0]["amount"], 206.0)

    def test_streaming_reader_selects_the_top_level_food_array(self):
        payload = {
            "metadata": {"release": "test"},
            "SRLegacyFoods": [{"fdcId": 1}, {"fdcId": 2}],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sr-legacy.json"
            path.write_text(json.dumps(payload), encoding="utf-8")

            rows = list(_iter_json_array(path, "SRLegacyFoods"))

        self.assertEqual([row["fdcId"] for row in rows], [1, 2])

    def test_streaming_reader_handles_chunk_boundaries_without_read_text(self):
        payload = {
            "metadata": {"release": "test", "notes": "x" * 65530},
            "SRLegacyFoods": [
                {"fdcId": index, "description": "é" * 400}
                for index in range(200)
            ],
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sr-legacy.json"
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with patch.object(Path, "read_text", side_effect=AssertionError("full-file read")):
                ids = [row["fdcId"] for row in _iter_json_array(path, "SRLegacyFoods")]
        self.assertEqual(ids, list(range(200)))

    def test_streaming_reader_handles_number_split_across_reads(self):
        class SmallReads(io.StringIO):
            def read(self, size=-1):
                return super().read(min(size, 3))

        source = '{"count":12345,"SRLegacyFoods":[{"fdcId":7}]}'
        with patch.object(Path, "open", return_value=SmallReads(source)):
            rows = list(_iter_json_array("unused.json", "SRLegacyFoods"))
        self.assertEqual(rows, [{"fdcId": 7}])


class DatasetScopedRetrievalTests(unittest.TestCase):
    class FakeGraph:
        def __init__(self):
            self.calls = []

        def search(self, index_name, search_query, k=5, dataset_scope="all"):
            self.calls.append((index_name, search_query, k, dataset_scope))
            rows = [
                {"id": "fdc:1", "name": "Salmon, raw", "aliases": [], "score": 1.0},
                {"id": "fdc-sr-legacy:2", "name": "Salmon, cooked", "aliases": [], "score": 0.9},
            ]
            if dataset_scope == "foundation_foods":
                return rows[:1]
            if dataset_scope == "sr_legacy":
                return rows[1:]
            return rows

    def test_retriever_passes_slm_dataset_scope_to_graph_search(self):
        graph = self.FakeGraph()
        retriever = GraphRetriever(graph)

        matches = retriever.retrieve_ingredients("salmon", k=2, dataset_scope="sr_legacy")

        self.assertEqual([item[0] for item in matches], ["fdc-sr-legacy:2"])
        self.assertEqual(graph.calls[0][0], "renally_food_record_search")
        self.assertEqual(graph.calls[0][3], "sr_legacy")

    def test_unscoped_search_keeps_both_food_sources_in_the_candidate_set(self):
        graph = self.FakeGraph()
        retriever = GraphRetriever(graph)

        matches = retriever.resolve_ingredient_candidates("salmon", k=1, dataset_scope="all")

        self.assertEqual([call[3] for call in graph.calls], ["foundation_foods", "sr_legacy"])
        self.assertEqual([item[0] for item in matches], ["fdc:1", "fdc-sr-legacy:2"])

    def test_dataset_scope_labels_are_normalized_without_food_name_rules(self):
        self.assertEqual(RequestPlanner._normalize_dataset_scope("SR Legacy"), "sr_legacy")
        self.assertEqual(RequestPlanner._normalize_dataset_scope("Foundation Foods"), "foundation_foods")
        self.assertEqual(RequestPlanner._normalize_dataset_scope(None), "all")

    def test_catalog_classification_does_not_pre_search_other_graph_domains(self):
        class Classifier:
            def classify_request(self, query, context):
                return {
                    "intent": "catalog_question",
                    "question_type": "catalog_existence",
                    "ingredient_queries": ["salmon", "oil", "lemon juice", "pepper"],
                    "dataset_scope": "all",
                    "recipe_requests": [],
                }

        graph = SimpleNamespace(recipes={}, ingredients={})
        retriever = self.FakeGraph()
        planner = RequestPlanner(graph, retriever, deterministic_generator=object())
        planner._generator = lambda use_slm: (Classifier(), None)

        plan, _, _, used_slm = planner.plan(
            "Which recipe ingredients are present in the dataset?", True, {},
        )

        self.assertTrue(used_slm)
        self.assertEqual(plan["dataset_scope"], "all")
        self.assertEqual(plan["ingredient_queries"], ["salmon", "oil", "lemon juice", "pepper"])
        self.assertEqual(retriever.calls, [])

    def test_recipe_generation_uses_only_the_classified_dataset_candidates(self):
        class Retriever:
            scopes = []

            def retrieve_ingredients(self, query, k=5, dataset_scope="all"):
                self.scopes.append(dataset_scope)
                return [("fdc-sr-legacy:2", 1.0)]

        class Generator:
            catalog = None

            def classify_request(self, query, context):
                return {
                    "intent": "new_recipe", "dataset_scope": "sr_legacy",
                    "ingredient_queries": [], "recipe_requests": [],
                }

            def interpret_request(self, query, recipe_catalog, ingredient_catalog,
                                 context, classified_request=None):
                self.catalog = ingredient_catalog
                return {
                    "intent": "new_recipe", "recipe_requests": [],
                    "ingredient_queries": [],
                    "generated_composition": [{"ingredient_id": "fdc:1", "amount_g": 100}],
                }

        graph = SimpleNamespace(
            recipes={},
            ingredients={
                "fdc:1": {"source": {"dataset_key": "foundation_foods"}},
                "fdc-sr-legacy:2": {"source": {"dataset_key": "sr_legacy"}},
            },
        )
        retriever, generator = Retriever(), Generator()
        planner = RequestPlanner(graph, retriever, deterministic_generator=object())
        planner._generator = lambda use_slm: (generator, None)

        plan, _, _, _ = planner.plan("Suggest a dish", True, {})

        self.assertEqual(retriever.scopes, ["sr_legacy"])
        self.assertEqual([item["dataset_key"] for item in generator.catalog], ["sr_legacy"])
        self.assertTrue(plan["generated_composition_invalid"])
        self.assertEqual(plan["generated_composition"], [])


class CatalogMembershipResultTests(unittest.TestCase):
    def test_catalog_membership_keeps_source_candidates_visible(self):
        records = [
            ("fdc:1", "Red bell pepper, raw", "foundation_foods"),
            ("fdc:2", "Green bell pepper, raw", "foundation_foods"),
            ("fdc-sr-legacy:3", "Pepper, banana, raw", "sr_legacy"),
            ("fdc-sr-legacy:4", "Spices, pepper, black", "sr_legacy"),
            ("fdc-sr-legacy:5", "Spices, pepper, white", "sr_legacy"),
        ]

        class Retriever:
            def resolve_ingredient_candidates(self, query, k=5, dataset_scope="all"):
                return [(record[0], 0.85) for record in records[:k]]

            def retrieve_ingredients(self, query, k=5, dataset_scope="all"):
                return []

        service = object.__new__(RenAllyWebService)
        service.kg = SimpleNamespace(ingredients={
            identifier: {
                "name": name,
                "fdc_description": name,
                "aliases": [],
                "source": {"name": dataset_key, "dataset_key": dataset_key},
            }
            for identifier, name, dataset_key in records
        })
        service.retriever = Retriever()
        service.planner = SimpleNamespace(MAX_INGREDIENT_QUERIES=5)

        results, _ = service._lookup_catalog_membership(["pepper"], "all")

        self.assertEqual(results[0]["match_status"], "matches_found")
        self.assertEqual(len(results[0]["matches"]), 5)
        self.assertTrue(any(match["name"] == "Spices, pepper, black" for match in results[0]["matches"]))

    def test_unweighted_recipe_names_are_checked_in_both_usda_datasets(self):
        records = {
            "ingredient one": ("fdc:1", "Ingredient one", "foundation_foods"),
            "ingredient two": ("fdc-sr-legacy:2", "Ingredient two", "sr_legacy"),
            "ingredient three": ("fdc:3", "Ingredient three", "foundation_foods"),
            "ingredient four": ("fdc-sr-legacy:4", "Ingredient four", "sr_legacy"),
            "ingredient five": ("fdc:5", "Ingredient five", "foundation_foods"),
            "ingredient six": ("fdc-sr-legacy:6", "Ingredient six", "sr_legacy"),
        }

        class Retriever:
            def __init__(self):
                self.calls = []

            def resolve_ingredient_candidates(self, query, k=5, dataset_scope="all"):
                self.calls.append((query, dataset_scope))
                return [(records[query.casefold()][0], 1.25)]

            def retrieve_ingredients(self, query, k=5, dataset_scope="all"):
                return []

        service = object.__new__(RenAllyWebService)
        service.kg = SimpleNamespace(ingredients={
            identifier: {
                "name": name,
                "fdc_description": name,
                "aliases": [],
                "source": {"name": dataset_key, "dataset_key": dataset_key},
            }
            for identifier, name, dataset_key in records.values()
        })
        service.retriever = Retriever()
        service.planner = SimpleNamespace(MAX_INGREDIENT_QUERIES=5)

        names = list(records)
        results, trace = service._lookup_source_recipe_ingredients({
            "raw_ingredient_names": names,
        })

        self.assertEqual(service.retriever.calls, [(name, "all") for name in names])
        self.assertEqual([result["query"] for result in results], names)
        self.assertEqual({
            match["source"]["dataset_key"]
            for result in results for match in result["matches"]
        }, {"foundation_foods", "sr_legacy"})
        self.assertTrue(all(result["match_status"] == "exact" for result in results))
        self.assertTrue(all("Foundation Foods and SR Legacy" in item for item in trace))

    def test_recipe_response_includes_contextual_ingredient_resolution(self):
        records = {
            "ingredient one": ("fdc:1", "Ingredient one", "foundation_foods"),
            "ingredient two": ("fdc-sr-legacy:2", "Ingredient two", "sr_legacy"),
        }

        class Retriever:
            def __init__(self):
                self.calls = []

            def resolve_ingredient_candidates(self, query, k=5, dataset_scope="all"):
                self.calls.append((query, dataset_scope))
                return [(records[query.casefold()][0], 1.25)]

            def retrieve_ingredients(self, query, k=5, dataset_scope="all"):
                return []

        class Pipeline:
            def run(self, query, constraints, use_llm=True, servings=1):
                return {
                    "answer": "Ingredient amounts are unavailable.",
                    "missing_data": {
                        "source_dataset": "Food.com Recipes and Reviews (Kaggle)",
                        "recipe_name": "Test dish",
                        "raw_ingredient_names": list(records),
                        "message": "Source ingredient amounts are unavailable.",
                    },
                }

        retriever = Retriever()
        service = object.__new__(RenAllyWebService)
        service.profiles = {}
        service.retriever = retriever
        service.kg = SimpleNamespace(ingredients={
            identifier: {
                "name": name,
                "fdc_description": name,
                "aliases": [],
                "source": {"name": dataset_key, "dataset_key": dataset_key},
            }
            for identifier, name, dataset_key in records.values()
        })
        service.planner = SimpleNamespace(
            MAX_INGREDIENT_QUERIES=5,
            plan=lambda query, use_slm, context: ({
                "intent": "single_recipe",
                "recipe_requests": [{"query": "Test dish", "recipe_id": None}],
            }, object(), None, True),
        )
        service.runtime = SimpleNamespace(create_pipeline=lambda generator: Pipeline())
        service.chat_ingredient_mapping_service = SimpleNamespace(resolve_recipe=lambda recipe, **kwargs: {
            "mode": "READ_ONLY",
            "results": [
                {"mapping": {"fdc_id": 11, "resolved_family": None}},
                {"mapping": {"fdc_id": None, "resolved_family": "some_family"}},
            ],
        })

        response = service.chat({"query": "Can I eat Test dish?", "use_slm": True})

        self.assertEqual(retriever.calls, [])
        self.assertEqual(response["ingredient_results"], [])
        self.assertEqual(len(response["ingredient_mapping_enrichment"]["results"]), 2)
        self.assertIn("1 specific USDA food records", response["answer"])
        self.assertTrue(any(
            "Resolved Food.com ingredient identities" in item
            for item in response["tool_trace"]
        ))


if __name__ == "__main__":
    unittest.main()

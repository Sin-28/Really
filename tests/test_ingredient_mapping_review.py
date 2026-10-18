import unittest

from src.recipes.ingredient_mapping_review import IngredientMappingReviewService


FOODCOM = "Food.com Recipes and Reviews (Kaggle)"


class FakeGraph:
    def __init__(self):
        self.recipes = {
            "foodcom:1": {"name": "Salmon supper", "source_dataset": FOODCOM},
            "foodcom:2": {"name": "Salmon lunch", "source_dataset": FOODCOM},
            "curated:1": {"name": "Curated salmon", "source_dataset": "RenAlly"},
        }
        self.details = {
            "foodcom:1": {"ingredient_items": [
                {"name": "table salt", "quantity_raw": "1 tsp"},
                {"name": "salmon steaks", "quantity_raw": "2"},
            ]},
            "foodcom:2": {"ingredient_items": [
                {"name": "table salt", "quantity_raw": "1/2 tsp"},
                {"name": "salmon steaks", "quantity_raw": "1"},
            ]},
        }
        self.cache = {}

    def recipe_details(self, recipe_id):
        return self.details.get(recipe_id)

    def get_ingredient_mapping_cache(self, key):
        return self.cache.get(key)


class FakeRetriever:
    def search_recipes(self, _query, k=3):
        return [("foodcom:1", 1.0), ("curated:1", 0.9), ("foodcom:2", 0.8)][:k]


class FakeMapper:
    def __init__(self, graph):
        self.kg = graph
        self.retrieval_count = 0

    def mapping_cache_key(self, item, *, dataset_scope="all"):
        parsed = item.get("parsed") or {}
        name = parsed.get("normalized_name") or item.get("name", "").casefold()
        return f"{dataset_scope}:{name}"

    def retrieve_candidates(self, item, *, dataset_scope="all", limit=5, persist=True):
        self.retrieval_count += 1
        name = item["name"]
        key = self.mapping_cache_key(item, dataset_scope=dataset_scope)
        candidates = ([{
            "fdc_id": 101, "description": "Table salt", "data_type": "SR Legacy",
            "food_state": "Table salt", "dataset_key": "sr_legacy",
            "approved_aliases": [], "retrieval_method": "exact_name",
        }] if name == "table salt" else [
            {"fdc_id": 201, "description": "Atlantic salmon, raw", "data_type": "SR Legacy",
             "food_state": "Atlantic salmon, raw", "dataset_key": "sr_legacy",
             "approved_aliases": [], "retrieval_method": "ranked_search"},
            {"fdc_id": 202, "description": "Salmon, cooked", "data_type": "Foundation Foods",
             "food_state": "Salmon, cooked", "dataset_key": "foundation_foods",
             "approved_aliases": [], "retrieval_method": "ranked_search"},
        ])
        result = {"cache_key": key, "status": "CANDIDATE", "candidates": candidates}
        if persist:
            parsed = item.get("parsed") or {}
            self.kg.cache[key] = {"cache_key": key, "ingredient_name": name,
                                  "normalized_name": parsed.get("normalized_name") or name,
                                  "attributes": parsed.get("attributes") or {},
                                  "dataset_scope": dataset_scope, "status": "CANDIDATE",
                                  "candidates": candidates}
        return result

    def verify_candidate_automatically(self, item, result, _policy, *, enabled, dataset_scope):
        key = result["cache_key"]
        candidate = result["candidates"][0]
        self.kg.cache[key].update(status="VERIFIED", fdc_id=candidate["fdc_id"],
                                  verification_method="DETERMINISTIC_POLICY")
        return {"status": "VERIFIED", "fdc_id": candidate["fdc_id"],
                "description": candidate["description"],
                "verification_method": "DETERMINISTIC_POLICY",
                "candidates": result["candidates"]}

    def verify_candidate(self, item, fdc_id, *, validated_by, validation_method,
                         validation_note, attributes, dataset_scope):
        key = self.mapping_cache_key(item, dataset_scope=dataset_scope)
        record = self.kg.cache[key]
        candidate = next(row for row in record["candidates"] if row["fdc_id"] == fdc_id)
        record.update(status="VERIFIED", fdc_id=fdc_id, verification_method=validation_method,
                      validated_by=validated_by, validation_note=validation_note,
                      attributes=attributes)
        return {"ingredient": record["ingredient_name"], "fdc_id": fdc_id,
                "description": candidate["description"]}


class FakeResolutionService:
    def __init__(self, mapper):
        self.mapper = mapper
        self.calls = []

    def resolve_ingredient(self, item, recipe_context):
        self.calls.append((item["name"], recipe_context["name"]))
        if item["name"] == "table salt":
            return {"status": "EXACT_IDENTITY", "entity_resolution_status": "RESOLVED",
                    "fdc_id": 101, "usda_description": "Table salt", "candidate_ids": [101],
                    "candidates": [{"fdc_id": 101, "description": "Table salt"}],
                    "slm_inference_performed": False}
        return {"status": "FAMILY_RESOLVED", "entity_resolution_status": "FAMILY_RESOLVED",
                "resolved_family": "salmon", "fdc_id": None, "candidate_ids": [201, 202],
                "candidates": [{"fdc_id": 201, "description": "Atlantic salmon"},
                               {"fdc_id": 202, "description": "Sockeye salmon"}],
                "slm_inference_performed": True}


class IngredientMappingReviewTests(unittest.TestCase):
    def setUp(self):
        self.graph = FakeGraph()
        self.mapper = FakeMapper(self.graph)
        self.mapping_service = FakeResolutionService(self.mapper)
        self.service = IngredientMappingReviewService(self.graph, FakeRetriever(), self.mapping_service)

    def test_recipe_search_only_returns_imported_foodcom_records(self):
        result = self.service.search_recipes("salmon")
        self.assertEqual([row["recipe_id"] for row in result["results"]], ["foodcom:1", "foodcom:2"])

    def test_batch_keeps_recipe_contexts_separate_for_slm(self):
        result = self.service.prepare(["foodcom:1", "foodcom:2"])
        self.assertEqual(result["ingredient_occurrence_count"], 4)
        self.assertEqual(result["unique_context_count"], 4)
        self.assertEqual(result["resolved_count"], 2)
        self.assertEqual(result["family_resolved_count"], 2)
        self.assertEqual(result["slm_inference_calls"], 2)
        self.assertEqual(len(self.mapping_service.calls), 4)
        self.assertIn(("salmon steaks", "Salmon supper"), self.mapping_service.calls)
        self.assertIn(("salmon steaks", "Salmon lunch"), self.mapping_service.calls)

    def test_workspace_does_not_expose_human_approval(self):
        self.assertFalse(hasattr(self.service, "approve"))


if __name__ == "__main__":
    unittest.main()

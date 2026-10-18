import json
import time
import unittest

from scripts.import_foodcom_recipes import _recipe_row
from src.recipes.ingredient_mapping import USDAIngredientMapper
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator
from src.recipes.ingredient_parser import parse_foodcom_ingredient
from src.retrieval.graph_retriever import GraphRetriever


def _candidate(fdc_id, description, food_state=None):
    return {
        "fdc_id": fdc_id,
        "description": description,
        "food_state": food_state or description,
        "data_type": "Foundation",
    }


def _mapping(ingredient, candidates, attributes=None):
    return {
        "ingredient": ingredient,
        "normalized_name": " ".join(ingredient.casefold().split()),
        "attributes": attributes or {},
        "mapping_version": "fdc-mapping-v1",
        "status": "AMBIGUOUS" if len(candidates) > 1 else "CANDIDATE",
        "cache_key": "mapping-key",
        "candidates": candidates,
    }


def _response(status, recommended=None, compatible=(), unresolved=(), reason="Matched candidate.", requires_review=False):
    return {
        "status": status,
        "recommended_fdc_id": recommended,
        "compatible_candidate_ids": list(compatible),
        "unresolved_attributes": list(unresolved),
        "reason": reason,
        "requires_review": requires_review,
    }


class MemoryGraph:
    def __init__(self):
        self.ingredients = {
            "food:1": {"name": "Apple", "fdc_description": "Apple", "aliases": [],
                       "food_state": "Apple", "source": {"fdc_id": 1, "dataset_key": "foundation_foods",
                                                              "data_type": "Foundation"}},
        }
        self.records = {}

    def search(self, index_name, query, k=5, dataset_scope="all"):
        terms = set(query.casefold().split())
        return [{"id": "food:1", "name": "Apple", "aliases": [],
                 "dataset_key": "foundation_foods", "score": 2.0}][:k] if terms & {"apple"} else []

    def get_ingredient_mapping_cache(self, cache_key):
        return self.records.get(cache_key)

    def save_ingredient_mapping_cache(self, record):
        old = self.records.get(record["cache_key"])
        if old and old.get("status") == "VERIFIED" and record.get("status") != "VERIFIED":
            return old
        self.records[record["cache_key"]] = {**(old or {}), **record}
        return self.records[record["cache_key"]]

    def save_ingredient_mapping_evaluation(self, cache_key, evaluation):
        record = self.records.get(cache_key)
        if not record:
            return None
        ids = {str(item.get("fdc_id")) for item in record.get("candidates", [])}
        if ids != {str(item) for item in evaluation.get("candidate_ids", [])}:
            raise ValueError("candidate set changed")
        record["slm_evaluation"] = dict(evaluation)
        return record


class IngredientMappingValidatorTests(unittest.TestCase):
    def test_clear_raw_chicken_match_is_recommended_without_human_verification(self):
        raw = _candidate(11, "Chicken breast, raw")
        cooked = _candidate(12, "Chicken breast, cooked")
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 11, [11]),
        )
        result = validator.evaluate(
            parse_foodcom_ingredient("raw chicken breast", "8 oz"),
            _mapping("raw chicken breast", [raw, cooked], {"food_state": ["raw"], "cut": ["breast"]}),
        )
        self.assertEqual(result["status"], "RECOMMENDED")
        self.assertEqual(result["recommended_fdc_id"], 11)
        self.assertFalse(result["requires_review"])
        self.assertEqual(result["guard_results"][1]["compatible"], False)

    def test_multiple_salmon_candidates_preserve_species_and_state_ambiguity(self):
        salmon = [_candidate(21, "Salmon, Atlantic, raw"), _candidate(22, "Salmon, sockeye, cooked")]
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("AMBIGUOUS", compatible=[21, 22],
                                                unresolved=["species", "cooking_state"],
                                                reason="Species and state are not specified.", requires_review=True),
        )
        result = validator.evaluate(parse_foodcom_ingredient("salmon steaks", "4 oz"),
                                    _mapping("salmon steaks", salmon))
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["recommended_fdc_id"])
        self.assertIn("species", result["unresolved_attributes"])
        self.assertIn("cooking_state", result["unresolved_attributes"])

    def test_generic_salmon_recommendation_is_blocked_when_species_is_unsupported(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 21, [21]),
        )
        result = validator.evaluate(
            parse_foodcom_ingredient("salmon", "4 oz"),
            _mapping("salmon", [_candidate(21, "Salmon, Atlantic, raw")]),
        )
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["recommended_fdc_id"])
        self.assertIn("species", result["unresolved_attributes"])

    def test_raw_input_deterministically_rejects_cooked_candidate(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 31, [31]),
        )
        result = validator.evaluate(parse_foodcom_ingredient("raw chicken breast", "8 oz"),
                                    _mapping("raw chicken breast", [_candidate(31, "Chicken breast, cooked")]))
        self.assertEqual(result["status"], "REJECTED")
        self.assertIsNone(result["recommended_fdc_id"])
        self.assertFalse(result["guard_results"][0]["compatible"])

    def test_smoked_input_deterministically_rejects_raw_fresh_candidate(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 41, [41]),
        )
        result = validator.evaluate(parse_foodcom_ingredient("smoked salmon", "4 oz"),
                                    _mapping("smoked salmon", [_candidate(41, "Salmon, raw")]))
        self.assertEqual(result["status"], "REJECTED")
        self.assertFalse(result["guard_results"][0]["compatible"])

    def test_generic_oil_cannot_be_narrowed_to_one_oil_type(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 51, [51]),
        )
        result = validator.evaluate(parse_foodcom_ingredient("oil", "2 tbsp"), _mapping(
            "oil", [_candidate(51, "Oil, olive"), _candidate(52, "Oil, canola")]))
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertIsNone(result["recommended_fdc_id"])
        self.assertIn("oil_type", result["unresolved_attributes"])

    def test_explicit_olive_oil_match_is_recommended_and_vegetable_oil_is_rejected(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 61, [61]),
        )
        result = validator.evaluate(parse_foodcom_ingredient("olive oil", "2 tbsp"), _mapping(
            "olive oil", [_candidate(61, "Oil, olive"), _candidate(62, "Oil, vegetable")]))
        self.assertEqual(result["status"], "RECOMMENDED")
        self.assertEqual(result["recommended_fdc_id"], 61)
        self.assertFalse(result["guard_results"][1]["compatible"])

    def test_hallucinated_fdc_id_is_never_returned_as_recommendation(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("RECOMMENDED", 999, [999]),
        )
        result = validator.evaluate(parse_foodcom_ingredient("apple", "1"),
                                    _mapping("apple", [_candidate(71, "Apple, raw")]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["recommended_fdc_id"])
        self.assertEqual(result["hallucinated_candidate_ids"], [999])

    def test_invalid_json_returns_review_required(self):
        validator = SLMIngredientMappingValidator(evaluator=lambda payload: "not json")
        result = validator.evaluate(parse_foodcom_ingredient("apple", "1"),
                                    _mapping("apple", [_candidate(71, "Apple, raw")]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["recommended_fdc_id"])

    def test_missing_required_output_fields_returns_review_required(self):
        validator = SLMIngredientMappingValidator(evaluator=lambda payload: {"status": "RECOMMENDED"})
        result = validator.evaluate(parse_foodcom_ingredient("apple", "1"),
                                    _mapping("apple", [_candidate(71, "Apple, raw")]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("omitted required", result["reason"])

    def test_timeout_returns_review_required_without_selecting(self):
        def slow(payload):
            time.sleep(0.08)
            return _response("RECOMMENDED", 71, [71])

        validator = SLMIngredientMappingValidator(evaluator=slow, timeout_seconds=0.01)
        result = validator.evaluate(parse_foodcom_ingredient("apple", "1"),
                                    _mapping("apple", [_candidate(71, "Apple, raw")]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["recommended_fdc_id"])
        self.assertIn("timed out", result["reason"])

    def test_empty_candidate_list_does_not_call_model(self):
        calls = []
        validator = SLMIngredientMappingValidator(evaluator=lambda payload: calls.append(payload))
        result = validator.evaluate(parse_foodcom_ingredient("dragonfruit", "1"),
                                    _mapping("dragonfruit", []))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(calls, [])

    def test_conflicting_slm_recommendation_is_review_required(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: _response("AMBIGUOUS", 71, [71]),
        )
        result = validator.evaluate(parse_foodcom_ingredient("apple", "1"),
                                    _mapping("apple", [_candidate(71, "Apple, raw")]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIsNone(result["recommended_fdc_id"])

    def test_inference_unavailable_returns_review_required(self):
        def unavailable(payload):
            raise ConnectionError("model service unavailable")

        validator = SLMIngredientMappingValidator(evaluator=unavailable)
        result = validator.evaluate(parse_foodcom_ingredient("apple", "1"),
                                    _mapping("apple", [_candidate(71, "Apple, raw")]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("ConnectionError", result["reason"])

    def test_recommendation_and_cache_update_preserve_verified_mapping(self):
        graph = MemoryGraph()
        retriever = GraphRetriever(graph)
        mapper = USDAIngredientMapper(graph, retriever)
        ingredient = parse_foodcom_ingredient("apple", "1")
        candidates = mapper.retrieve_candidates(ingredient, dataset_scope="foundation_foods")
        verified = mapper.verify_candidate(ingredient, 1, validated_by="reviewer-1",
                                           dataset_scope="foundation_foods")
        self.assertEqual(verified["status"], "VERIFIED")
        verified_result = mapper.retrieve_candidates(ingredient, dataset_scope="foundation_foods")
        validator = SLMIngredientMappingValidator(
            graph, evaluator=lambda payload: _response("RECOMMENDED", 1, [1]),
        )
        evaluation = validator.evaluate(ingredient, verified_result)
        stored = graph.get_ingredient_mapping_cache(candidates["cache_key"])
        self.assertEqual(evaluation["status"], "RECOMMENDED")
        self.assertEqual(evaluation["cache_status"], "SAVED")
        self.assertEqual(stored["status"], "VERIFIED")
        self.assertEqual(stored["fdc_id"], 1)
        self.assertEqual(stored["validated_by"], "reviewer-1")
        self.assertEqual(stored["slm_evaluation"]["recommended_fdc_id"], 1)

    def test_mapping_cache_keeps_slm_evaluation_separate_from_candidate_status(self):
        graph = MemoryGraph()
        mapper = USDAIngredientMapper(graph, GraphRetriever(graph))
        ingredient = parse_foodcom_ingredient("apple", "1")
        mapping = mapper.retrieve_candidates(ingredient, dataset_scope="foundation_foods")
        validator = SLMIngredientMappingValidator(
            graph, evaluator=lambda payload: _response("RECOMMENDED", 1, [1]),
        )
        evaluation = validator.evaluate(ingredient, mapping)
        stored = graph.get_ingredient_mapping_cache(mapping["cache_key"])
        self.assertEqual(evaluation["cache_status"], "SAVED")
        self.assertEqual(stored["status"], "CANDIDATE")
        self.assertEqual(stored["slm_evaluation"]["status"], "RECOMMENDED")
        reused = mapper.retrieve_candidates(ingredient, dataset_scope="foundation_foods")
        self.assertEqual(reused["status"], "CANDIDATE")
        self.assertEqual(reused["slm_evaluation"]["recommended_fdc_id"], 1)

    def test_step_one_parsed_foodcom_item_is_accepted_and_quantity_is_passed(self):
        recipe = _recipe_row({
            "RecipeId": "7", "Name": "Salmon", "RecipeIngredientParts": '["salmon steaks"]',
            "RecipeIngredientQuantities": '["4 oz"]', "RecipeServings": "1",
            "RecipeInstructions": '["Bake"]',
        })
        item = json.loads(recipe["properties"]["details_json"])["ingredient_items"][0]
        seen = []
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: (seen.append(payload) or _response(
                "AMBIGUOUS", compatible=[81, 82], unresolved=["species", "cooking_state"],
                reason="Ingredient is underspecified.", requires_review=True)),
        )
        result = validator.evaluate(item, _mapping(
            "salmon steaks", [_candidate(81, "Salmon, Atlantic, raw"),
                              _candidate(82, "Salmon, sockeye, cooked")], {"cut": ["steaks"]}),
            recipe_context={"name": "Salmon", "instructions": ["Bake"]},
        )
        self.assertEqual(result["status"], "AMBIGUOUS")
        self.assertEqual(seen[0]["quantity"], 4.0)
        self.assertEqual(seen[0]["unit"], "oz")
        self.assertEqual(seen[0]["normalized_name"], "salmon")
        self.assertNotIn("recipe_context", result)


if __name__ == "__main__":
    unittest.main()

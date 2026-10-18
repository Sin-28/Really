import unittest

from src.constraints.constraint_engine import ConstraintEngine
from src.pipeline.renally_pipeline import RenAllyPipeline
from src.recipes.candidate_generator import CandidateGenerator
from src.retrieval.graph_retriever import GraphRetriever
from tests.graph_fixtures import FixtureKnowledgeGraph


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.kg = FixtureKnowledgeGraph()
        self.pipeline = RenAllyPipeline(
            self.kg, GraphRetriever(self.kg), ConstraintEngine(self.kg.ingredients),
            CandidateGenerator(self.kg), generator=None,
        )

    def test_pipeline_finds_valid_candidate(self):
        result = self.pipeline.run(
            "Modify chicken rice for my dialysis diet.",
            {"sodium": {"max": 1000, "scope": "meal"},
             "potassium": {"max": 700, "scope": "meal"},
             "phosphorus": {"max": 400, "scope": "meal"}},
            use_llm=False,
        )
        self.assertIsNotNone(result["selected"])
        self.assertTrue(result["selected"]["passed"])

    def test_candidates_include_structured_substitution_positions_without_affecting_scores(self):
        self.kg.substitution_candidates_for = lambda ingredient_id: (
            [{"id": self.kg.RICE_ID, "compatibility": 0.8, "basis": "controlled fixture"}]
            if ingredient_id == self.kg.CHICKEN_ID else []
        )
        candidates = CandidateGenerator(self.kg).generate("chicken_rice_reference")
        replaced = next(candidate for candidate in candidates if candidate["modified_slots"] == 1)
        change = replaced["ingredient_changes"][0]
        self.assertEqual(change["change_type"], "REPLACED")
        self.assertEqual(change["original_position"], 1)
        self.assertEqual(change["adapted_position"], 1)
        self.assertEqual(change["original_ingredient_id"], self.kg.CHICKEN_ID)
        self.assertEqual(change["adapted_ingredient_id"], self.kg.RICE_ID)

    def test_instant_noodle_question_matches_traceable_generic_reference(self):
        result = self.pipeline.run(
            "I want to eat instant noodle but I have hypertension, is it ok?",
            {}, use_llm=False,
        )
        self.assertEqual(result["recipe_id"], "instant_ramen_reference")
        self.assertAlmostEqual(result["selected"]["totals"]["sodium_mg"], 1502.55)

    def test_unsupported_dish_returns_suggestions_instead_of_raising(self):
        result = self.pipeline.run("Can I eat mutton soup?", {}, use_llm=False)
        self.assertTrue(result["unsupported"])
        self.assertTrue(result["suggestions"])
        self.assertIsNone(result["selected"])

    def test_missing_ingredient_is_passed_to_response_layer(self):
        class MissingDataResponder:
            def __init__(self):
                self.received = None

            def respond_missing_data(self, query, missing_data):
                self.received = missing_data
                return "The response layer was told the ingredient data is missing."

        responder = MissingDataResponder()
        pipeline = RenAllyPipeline(
            self.kg, GraphRetriever(self.kg), ConstraintEngine(self.kg.ingredients),
            CandidateGenerator(self.kg), responder,
        )
        result = pipeline.run("Make fish soup", {}, use_llm=False)
        self.assertEqual(result["missing_data"]["kind"], "missing_ingredients")
        self.assertIn(self.kg.MISSING_FISH_ID, responder.received["missing_ingredients"])
        self.assertIn("data is missing", result["answer"])

    def test_fixture_recipes_have_resolved_or_explicitly_missing_ingredients(self):
        valid_recipe_ids = ("chicken_rice_reference", "instant_ramen_reference")
        for recipe_id in valid_recipe_ids:
            recipe = self.kg.recipe_details(recipe_id)
            self.assertTrue(recipe["ingredients"])
            for item in recipe["ingredients"]:
                self.assertIn(item["id"], self.kg.ingredients)
                self.assertIsInstance(item["amount_g"], (int, float))
                self.assertGreaterEqual(item["amount_g"], 0)

        missing_recipe = self.kg.recipe_details("fish_soup_missing_data")
        missing_ids = {item["id"] for item in missing_recipe["ingredients"]} - set(self.kg.ingredients)
        self.assertEqual(missing_ids, {self.kg.MISSING_FISH_ID})


if __name__ == "__main__":
    unittest.main()

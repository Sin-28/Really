import unittest

from src.constraints.constraint_engine import ConstraintEngine
from tests.graph_fixtures import FixtureKnowledgeGraph


class ConstraintTests(unittest.TestCase):
    def setUp(self):
        self.kg = FixtureKnowledgeGraph()
        self.engine = ConstraintEngine(self.kg.ingredients)

    def test_rejects_unrealistic_rice_quantity(self):
        result = self.engine.evaluate(
            [(self.kg.RICE_ID, 500)],
            {"sodium": {"max": 5000, "scope": "meal"}},
        )
        self.assertFalse(result["passed"])
        self.assertTrue(any("outside plausible demo range" in x for x in result["reasons"]))

    def test_valid_candidate(self):
        result = self.engine.evaluate(
            [(self.kg.RICE_ID, 120), (self.kg.CHICKEN_ID, 80)],
            {"sodium": {"max": 1000, "scope": "meal"},
             "potassium": {"max": 700, "scope": "meal"},
             "phosphorus": {"max": 400, "scope": "meal"}},
        )
        self.assertTrue(result["passed"])

    def test_daily_guideline_is_not_applied_to_a_single_meal(self):
        result = self.engine.evaluate(
            [(self.kg.RICE_ID, 120)],
            {"sodium": {"max": 1, "scope": "day"}},
        )
        self.assertTrue(result["passed"])
        self.assertTrue(any("not treated as a per-meal limit" in x for x in result["not_evaluated"]))

    def test_missing_nutrient_is_not_a_pass_for_numeric_meal_limit(self):
        result = self.engine.evaluate(
            [(self.kg.RICE_ID, 27)],
            {"saturated fat": {"max": 10, "scope": "meal"}},
        )
        self.assertIsNone(result["passed"])
        self.assertTrue(any("data missing" in x for x in result["not_evaluated"]))

    def test_traceable_chicken_fixture_keeps_source_id_and_units(self):
        food = self.kg.ingredients[self.kg.CHICKEN_ID]
        self.assertEqual(food["source"]["fdc_id"], 331960)
        self.assertEqual(food["nutrients_per_100g"]["sodium_mg"], 47.0)
        self.assertEqual(food["nutrient_units"]["phosphorus_mg"], "mg")
        self.assertIn("test fixture", food["source"]["name"])


if __name__ == "__main__":
    unittest.main()

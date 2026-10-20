from copy import deepcopy
import unittest

from src.recipes.culinary_quantity_validation import CulinaryQuantityValidationService
from src.recipes.daily_meal_planner import DATASET, DailyMealPlanner
from src.recipes.ingredient_quantity_optimiser import IngredientQuantityOptimiser
from src.web.request_planner import RequestPlanner
from src.web.service import RenAllyWebService
from tests.test_food_recommendation import NutrientEvidenceTests


SLOTS = ("breakfast", "lunch", "dinner")
CULINARY_RULES = {
    "basis": "explicit fixture dish rule", "essential_positions": [0, 1],
    "required_roles": ["vegetable"],
    "ingredient_ratios": [{"numerator_position": 0, "denominator_position": 1,
                           "min": 1, "max": 2}],
}


class Graph:
    def __init__(self):
        self.recipes = {f"foodcom:{slot}": {
            "id": f"foodcom:{slot}", "name": slot.title(), "source_dataset": DATASET,
            "raw_ingredients": ["first food", "second food"],
            "raw_ingredient_quantities": ["100", "250"],
            "method": ["Cook both ingredients."],
        } for slot in SLOTS}
        self.bounds = {i: {"role": "vegetable", "min_amount_g": 50,
                           "max_amount_g": 150, "amount_range_basis": "fixture sourced bounds"}
                       for i in (1, 2)}

    def recipe_details(self, recipe_id):
        return self.recipes[recipe_id]

    def get_usda_food_records_by_fdc_ids(self, ids):
        return [{"food": {"fdc_id": i, **self.bounds[i]}} for i in ids]


class Retriever:
    extra_breakfast = False

    def recommend_foodcom_recipes(self, query, k=7):
        slot = next(slot for slot in SLOTS if slot in query)
        hits = [{"recipe_id": f"foodcom:{slot}", "retrieval_score": 1.0}]
        if slot == "breakfast" and self.extra_breakfast:
            hits.append({"recipe_id": "foodcom:breakfast-alt", "retrieval_score": 0.9})
        return hits


class NutrientService:
    def __init__(self):
        self.missing_phosphorus_for = set()
        self.sodium_for = {}

    def enrich(self, recipe, *, max_ingredients):
        return deepcopy(NutrientEvidenceTests._enrichment(
            sodium=self.sodium_for.get(recipe["id"], 0),
            missing_phosphorus=recipe["id"] in self.missing_phosphorus_for))


class DailyMealPlannerTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph()
        self.retriever = Retriever()
        self.nutrients = NutrientService()
        self.planner = DailyMealPlanner(
            self.graph, self.retriever, self.nutrients,
            IngredientQuantityOptimiser(self.graph), CulinaryQuantityValidationService())
        self.daily = {
            "protein_g": {"min": 30, "unit": "g", "scope": "day"},
            "potassium_mg": {"max": 600, "unit": "mg", "scope": "day"},
        }

    def test_successful_joint_plan_and_protein_minimum(self):
        limits = {**self.daily,
                  "sodium_mg": {"max": 0, "unit": "mg", "scope": "day"},
                  "phosphorus_mg": {"min": 170, "max": 180, "unit": "mg", "scope": "day"}}
        result = self.planner.plan("Plan my meals for tomorrow", limits,
                                   culinary_rules=CULINARY_RULES)
        self.assertEqual(result["status"], "FEASIBLE")
        self.assertEqual([meal["slot"] for meal in result["meals"]], list(SLOTS))
        self.assertEqual([meal["recipe_id"] for meal in result["meals"]],
                         [f"foodcom:{slot}" for slot in SLOTS])
        self.assertGreaterEqual(result["daily_nutrient_totals"]["protein_g"], 30)
        self.assertLessEqual(result["daily_nutrient_totals"]["potassium_mg"], 600)
        self.assertEqual(result["daily_nutrient_totals"]["sodium_mg"], 0)
        self.assertGreaterEqual(result["daily_nutrient_totals"]["phosphorus_mg"], 170)
        self.assertLessEqual(result["daily_nutrient_totals"]["phosphorus_mg"], 180)
        for nutrient in result["daily_nutrient_totals"]:
            self.assertAlmostEqual(result["daily_nutrient_totals"][nutrient],
                                   sum(meal["nutrient_totals"][nutrient]
                                       for meal in result["meals"]))
        self.assertTrue(all(meal["culinary_status"] == "CULINARY_VALID"
                            for meal in result["meals"]))
        self.assertTrue(all(50 <= item["quantity_g"] <= 150
                            for meal in result["meals"] for item in meal["ingredients"]))
        self.assertTrue(all(item["usda_reference"] is not None
                            for meal in result["meals"] for item in meal["ingredients"]))
        self.assertEqual(self.graph.recipes["foodcom:breakfast"]["raw_ingredient_quantities"],
                         ["100", "250"])

    def test_cumulative_maximum_and_conflicting_lower_bound_are_infeasible(self):
        daily = deepcopy(self.daily)
        daily["potassium_mg"]["max"] = 500
        result = self.planner.plan("Plan my meals for tomorrow", daily,
                                   culinary_rules=CULINARY_RULES)
        self.assertEqual(result["status"], "INFEASIBLE_CONSTRAINTS")
        self.assertEqual(result["meals"], [])
        self.assertIsNone(result["daily_nutrient_totals"])

    def test_daily_limit_is_not_applied_separately_to_every_meal(self):
        result = self.planner.plan("Create a daily meal plan", self.daily,
                                   culinary_rules=CULINARY_RULES)
        self.assertEqual(result["status"], "FEASIBLE")
        self.assertTrue(any(meal["nutrient_totals"]["protein_g"] < 30
                            for meal in result["meals"]))
        self.assertGreaterEqual(result["daily_nutrient_totals"]["protein_g"], 30)

    def test_selection_moves_to_next_ranked_recipe_when_top_choice_fails_day_cap(self):
        self.graph.recipes["foodcom:breakfast-alt"] = {
            **self.graph.recipes["foodcom:breakfast"],
            "id": "foodcom:breakfast-alt", "name": "Alternative breakfast"}
        self.retriever.extra_breakfast = True
        self.nutrients.sodium_for["foodcom:breakfast"] = 1000
        limits = {**self.daily, "sodium_mg": {"max": 100, "unit": "mg", "scope": "day"}}
        result = self.planner.plan("Plan my meals for tomorrow", limits,
                                   culinary_rules=CULINARY_RULES)
        self.assertEqual(result["status"], "FEASIBLE")
        self.assertEqual(result["meals"][0]["recipe_id"], "foodcom:breakfast-alt")
        self.assertLessEqual(result["daily_nutrient_totals"]["sodium_mg"], 100)

    def test_explicit_meal_limits_remain_active_during_day_solve(self):
        result = self.planner.plan(
            "Plan my meals for tomorrow", self.daily,
            meal_constraints={"potassium_mg": {"max": 150, "unit": "mg", "scope": "meal"}},
            culinary_rules=CULINARY_RULES)
        self.assertEqual(result["status"], "INFEASIBLE_CONSTRAINTS")

    def test_missing_nutrient_information_and_bounds(self):
        self.nutrients.missing_phosphorus_for.add("foodcom:lunch")
        result = self.planner.plan("Plan my meals for tomorrow", self.daily)
        self.assertEqual(result["status"], "MISSING_NUTRIENT_DATA")
        self.assertIn("foodcom:lunch", [item["recipe_id"] for item in result["candidate_issues"]])
        self.nutrients.missing_phosphorus_for.clear()
        del self.graph.bounds[2]["max_amount_g"]
        result = self.planner.plan("Plan my meals for tomorrow", self.daily)
        self.assertEqual(result["status"], "MISSING_QUANTITY_BOUNDS")

    def test_culinary_ratio_and_meal_weight_preserved(self):
        result = self.planner.plan(
            "Suggest breakfast, lunch, and dinner within my dietary limits", self.daily,
            culinary_rules=CULINARY_RULES,
            total_meal_weight_bounds={"min_g": 120, "max_g": 240,
                                      "basis": "explicit fixture meal policy"})
        self.assertEqual(result["status"], "FEASIBLE")
        for meal in result["meals"]:
            a, b = [row["quantity_g"] for row in meal["ingredients"]]
            self.assertGreaterEqual(a, b)
            self.assertLessEqual(a, 2 * b)
            self.assertGreaterEqual(a + b, 120)
            self.assertLessEqual(a + b, 240)

    def test_missing_culinary_rules_are_not_claimed_valid(self):
        result = self.planner.plan("Plan my meals for tomorrow", self.daily)
        self.assertEqual(result["status"], "CULINARY_UNVERIFIED")
        self.assertEqual(result["nutritional_status"], "FEASIBLE")
        self.assertTrue(all(meal["culinary_status"] == "CULINARY_UNVERIFIED"
                            for meal in result["meals"]))

    def test_no_patient_limits_does_not_create_default_limits(self):
        result = self.planner.plan("Plan my meals for tomorrow", {}, culinary_rules=CULINARY_RULES)
        self.assertEqual(result["status"], "DAILY_LIMITS_UNSPECIFIED")
        self.assertEqual(result["nutritional_status"], "NOT_EVALUATED")
        self.assertEqual(result["applied_daily_constraints"], {})
        self.assertEqual(len(result["meals"]), 3)

    def test_requested_servings_do_not_multiply_per_person_daily_limits(self):
        result = self.planner.plan("Plan my meals for tomorrow", self.daily,
                                   culinary_rules=CULINARY_RULES, servings=2)
        self.assertEqual(result["status"], "FEASIBLE")
        self.assertEqual(result["requested_servings"], 2)
        self.assertAlmostEqual(result["daily_nutrient_totals_for_requested_servings"]["protein_g"],
                               2 * result["daily_nutrient_totals"]["protein_g"])

    def test_daily_requests_route_through_chat(self):
        for query in ("Plan my meals for tomorrow.",
                      "Create a low-sodium daily meal plan.",
                      "Suggest breakfast, lunch, and dinner within my dietary limits."):
            self.assertEqual(RequestPlanner._daily_meal_plan(query)["intent"], "daily_meal_plan")
        class Runtime:
            def __init__(self, planner):
                self.daily_planner = planner

            def create_daily_meal_planner(self):
                return self.daily_planner

        service = object.__new__(RenAllyWebService)
        service.runtime = Runtime(self.planner)
        service.planner = RequestPlanner(self.graph, self.retriever, object())
        service.profiles = {}
        service.daily_meal_planner = None
        response = service.chat({
            "query": "Plan my meals for tomorrow", "use_slm": False,
            "daily_limits": {"protein": {"min": 30}, "potassium": {"max": 600}},
            "culinary_rules": CULINARY_RULES,
        })
        self.assertEqual(response["daily_meal_plan"]["status"], "FEASIBLE")
        self.assertEqual(response["meal_plan"], None)
        self.assertEqual(response["active_daily_budgets"]["protein_g"]["min"], 30)


if __name__ == "__main__":
    unittest.main()

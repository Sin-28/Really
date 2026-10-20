import unittest

from src.recipes.ingredient_quantity_optimiser import IngredientQuantityOptimiser
from src.constraints.inputs import parse_budget_mapping
from tests.test_food_recommendation import DATASET, NutrientEvidenceTests


RECIPE = {"id": "foodcom:42", "name": "Constructed fixture", "source_dataset": DATASET,
          "raw_ingredient_quantities": ["100", "250"]}


class QuantityOptimiserTests(unittest.TestCase):
    def setUp(self):
        class Graph:
            def __init__(self):
                self.bounds = {
                    1: {"role": "protein", "min_amount_g": 50, "max_amount_g": 150,
                        "amount_range_basis": "prototype test range"},
                    2: {"role": "vegetable", "min_amount_g": 50, "max_amount_g": 150,
                        "amount_range_basis": "prototype test range"},
                }

            def get_usda_food_records_by_fdc_ids(self, ids):
                return [{"food": {"fdc_id": fdc_id, **self.bounds[fdc_id]}}
                        for fdc_id in ids]

        self.graph = Graph()
        self.optimiser = IngredientQuantityOptimiser(self.graph)
        self.calculation = NutrientEvidenceTests._enrichment(sodium=0)

    def test_feasible_upper_lower_and_total_weight(self):
        constraints = {"protein": {"min": 14, "unit": "g", "scope": "meal"},
                       "potassium": {"max": 400, "unit": "mg", "scope": "meal"}}
        result = self.optimiser.optimise(
            RECIPE, self.calculation, constraints,
            total_weight_bounds={"min_g": 180, "max_g": 220,
                                 "basis": "explicit test meal weight policy"},
        )
        self.assertEqual(result["optimisation_status"], "OPTIMISED")
        self.assertGreaterEqual(result["nutrient_totals"]["protein_g"], 14 - 1e-7)
        self.assertLessEqual(result["nutrient_totals"]["potassium_mg"], 400 + 1e-7)
        self.assertGreaterEqual(result["total_meal_weight_g"], 180 - 1e-7)
        self.assertLessEqual(result["total_meal_weight_g"], 220 + 1e-7)
        self.assertTrue(all(50 <= row["quantity_g"] <= 150 for row in result["ingredients"]))
        self.assertEqual(result["serving_basis"], "one newly constructed meal serving")
        self.assertEqual(RECIPE["raw_ingredient_quantities"], ["100", "250"])
        self.assertNotEqual([row["quantity_g"] for row in result["ingredients"]], [100, 250])

    def test_infeasible_limit(self):
        result = self.optimiser.optimise(RECIPE, self.calculation,
                                         {"protein": {"min": 100, "unit": "g"}})
        self.assertEqual(result["optimisation_status"], "INFEASIBLE_CONSTRAINTS")
        self.assertEqual(result["ingredients"], [])

    def test_missing_nutrient_data(self):
        calculation = NutrientEvidenceTests._enrichment(missing_phosphorus=True)
        result = self.optimiser.optimise(RECIPE, calculation,
                                         {"protein": {"min": 7, "unit": "g"}})
        self.assertEqual(result["optimisation_status"], "MISSING_NUTRIENT_DATA")
        self.assertEqual(result["missing_or_unresolved_ingredients"][0]["name"], "second food")

    def test_missing_bounds_and_unknown_role(self):
        del self.graph.bounds[2]["max_amount_g"]
        result = self.optimiser.optimise(RECIPE, self.calculation,
                                         {"protein": {"min": 7, "unit": "g"}})
        self.assertEqual(result["optimisation_status"], "MISSING_QUANTITY_BOUNDS")
        self.assertEqual(result["missing_quantity_bounds"][0]["ingredient"], "second food")
        self.graph.bounds[2]["role"] = "unknown"
        result = self.optimiser.optimise(RECIPE, self.calculation,
                                         {"protein": {"min": 7, "unit": "g"}})
        self.assertEqual(result["optimisation_status"], "MISSING_QUANTITY_BOUNDS")

    def test_explicit_role_bounds_can_supply_missing_graph_bounds(self):
        self.graph.bounds[2].pop("min_amount_g")
        self.graph.bounds[2].pop("max_amount_g")
        result = self.optimiser.optimise(
            RECIPE, self.calculation, {"protein": {"min": 7, "unit": "g"}},
            role_bounds={"vegetable": {"min_g": 20, "max_g": 60,
                                       "basis": "user-provided scenario assumption"}},
        )
        self.assertEqual(result["optimisation_status"], "OPTIMISED")
        self.assertEqual(result["ingredients"][1]["minimum_g"], 20)
        self.assertIn("user-provided scenario assumption", result["assumptions"])

    def test_zero_weight_not_permitted_and_remaining_scope_scales(self):
        result = self.optimiser.optimise(RECIPE, self.calculation,
                                         {"protein": {"min": 7, "unit": "g", "scope": "meal"},
                                          "potassium": {"max": 250, "unit": "mg", "scope": "remaining"}},
                                         serving_count=2)
        self.assertEqual(result["optimisation_status"], "OPTIMISED")
        self.assertTrue(all(row["quantity_g"] > 0 for row in result["ingredients"]))
        self.assertLessEqual(result["nutrient_totals"]["potassium_mg"] * 2, 250 + 1e-7)
        self.assertEqual(result["serving_count"], 2)

    def test_incompatible_total_meal_weight_is_infeasible(self):
        result = self.optimiser.optimise(
            RECIPE, self.calculation, {"protein": {"min": 7, "unit": "g"}},
            total_weight_bounds={"min_g": 10, "max_g": 90,
                                 "basis": "explicit test meal weight policy"},
        )
        self.assertEqual(result["optimisation_status"], "INFEASIBLE_CONSTRAINTS")

    def test_zero_minimum_without_other_sourced_bound_is_missing(self):
        self.graph.bounds[1]["min_amount_g"] = 0
        result = self.optimiser.optimise(RECIPE, self.calculation,
                                         {"protein": {"min": 7, "unit": "g"}})
        self.assertEqual(result["optimisation_status"], "MISSING_QUANTITY_BOUNDS")

    def test_missing_constraints_and_unsourced_role_bounds(self):
        self.assertEqual(self.optimiser.optimise(RECIPE, self.calculation, {})["optimisation_status"],
                         "MISSING_CONSTRAINTS")
        self.graph.bounds[2].pop("min_amount_g")
        self.graph.bounds[2].pop("max_amount_g")
        result = self.optimiser.optimise(
            RECIPE, self.calculation, {"protein": {"min": 7, "unit": "g"}},
            role_bounds={"vegetable": {"min_g": 1, "max_g": 100}},
        )
        self.assertEqual(result["optimisation_status"], "MISSING_QUANTITY_BOUNDS")

    def test_web_budget_parser_preserves_lower_limit(self):
        rules = parse_budget_mapping({"protein": {"min": "12"}, "sodium": "500"}, "meal")
        self.assertEqual(rules["protein_g"], {"min": 12.0, "scope": "meal", "unit": "g"})
        self.assertEqual(rules["sodium_mg"]["max"], 500.0)
        with self.assertRaises(ValueError):
            parse_budget_mapping({"protein": {"min": 20, "max": 10}}, "meal")


if __name__ == "__main__":
    unittest.main()

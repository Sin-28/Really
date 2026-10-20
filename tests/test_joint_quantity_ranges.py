import copy
import unittest

from src.constraints.constraint_engine import ConstraintEngine
from src.recipes.culinary_quantity_validation import CulinaryQuantityValidationService
from src.recipes.ingredient_quantity_optimiser import IngredientQuantityOptimiser
from tests.test_food_recommendation import DATASET, NutrientEvidenceTests


RECIPE = {
    "id": "foodcom:42", "name": "Fixture dish", "source_dataset": DATASET,
    "raw_ingredients": ["first food", "second food"],
    "raw_ingredient_quantities": ["100", "250"],
    "method": ["Cook both foods together."],
}
POLICY = {
    "basis": "explicit fixture culinary policy", "essential_positions": [0, 1],
    "required_roles": ["protein_source", "vegetable"],
    "ingredient_ratios": [{"numerator_position": 0, "denominator_position": 1,
                           "min": 1, "max": 4}],
}


class Graph:
    def __init__(self):
        self.bounds = {
            1: {"role": "protein", "min_amount_g": 50, "max_amount_g": 150,
                "amount_range_basis": "explicit fixture range"},
            2: {"role": "vegetable", "min_amount_g": 50, "max_amount_g": 150,
                "amount_range_basis": "explicit fixture range"},
        }

    def get_usda_food_records_by_fdc_ids(self, ids):
        return [{"food": {"fdc_id": i, **self.bounds[i]}} for i in ids]


class JointQuantityRangeTests(unittest.TestCase):
    def setUp(self):
        self.graph = Graph()
        self.optimiser = IngredientQuantityOptimiser(self.graph)
        self.calculation = NutrientEvidenceTests._enrichment(sodium=0)
        self.constraints = {"protein": {"min": 14, "unit": "g", "scope": "meal"}}
        self.weight_bounds = {"min_g": 200, "max_g": 200,
                              "basis": "explicit fixture meal weight"}

    def _ranges(self, *, constraints=None, culinary_rules=None, weight_bounds=None):
        limits = self.constraints if constraints is None else constraints
        initial = self.optimiser.optimise(RECIPE, self.calculation, limits,
                                          total_weight_bounds=weight_bounds)
        if initial["optimisation_status"] != "OPTIMISED":
            return initial, None, self.optimiser.optimise_ranges(
                RECIPE, self.calculation, limits, recommended=initial,
                total_weight_bounds=weight_bounds)
        validation = CulinaryQuantityValidationService().validate_and_reoptimise(
            RECIPE, self.calculation, limits, initial, self.optimiser,
            rules=culinary_rules, total_weight_bounds=weight_bounds,
        )
        recommended = validation["adjusted_optimisation"] or initial
        ranges = self.optimiser.optimise_ranges(
            RECIPE, self.calculation, limits, recommended=recommended,
            total_weight_bounds=weight_bounds,
            culinary_linear_constraints=validation["applied_linear_constraints"],
        )
        return recommended, validation, ranges

    def test_joint_minimum_maximum_and_verified_boundary_witnesses(self):
        recommended, culinary, result = self._ranges(
            culinary_rules=POLICY, weight_bounds=self.weight_bounds)
        self.assertEqual(culinary["status"], "CULINARY_VALID")
        self.assertEqual(result["status"], "FEASIBLE_RANGES")
        self.assertEqual([r["minimum_feasible_g"] for r in result["ingredient_ranges"]], [100, 50])
        self.assertEqual([r["maximum_feasible_g"] for r in result["ingredient_ranges"]], [150, 100])
        self.assertEqual(result["recommended_ingredients"], recommended["ingredients"])
        self.assertEqual(result["nutrient_totals"], recommended["nutrient_totals"])
        engine = ConstraintEngine({
            str(i): {"name": row["ingredient_name"], "min_amount_g": 50,
                     "max_amount_g": 150,
                     "nutrients_per_100g": {key: value["value_per_100g"]
                                            for key, value in row["nutrient_measurements_per_100g"].items()}}
            for i, row in enumerate(self.calculation["ingredients"])
        })
        for ingredient in result["ingredient_ranges"]:
            self.assertLessEqual(ingredient["minimum_feasible_g"], ingredient["recommended_g"])
            self.assertLessEqual(ingredient["recommended_g"], ingredient["maximum_feasible_g"])
            for field in ("minimum_witness_g", "maximum_witness_g"):
                witness = ingredient[field]
                self.assertEqual(sum(witness), 200)
                self.assertTrue(engine.evaluate(
                    [(str(i), amount) for i, amount in enumerate(witness)],
                    self.constraints, servings=1)["passed"])
                self.assertGreaterEqual(witness[0], witness[1])
                self.assertLessEqual(witness[0], 4 * witness[1])
        self.assertEqual(RECIPE["raw_ingredient_quantities"], ["100", "250"])

    def test_conditional_ranges_cannot_be_combined_arbitrarily(self):
        _, _, result = self._ranges(weight_bounds=self.weight_bounds)
        self.assertEqual([row["minimum_feasible_g"] for row in result["ingredient_ranges"]], [50, 50])
        self.assertEqual([row["maximum_feasible_g"] for row in result["ingredient_ranges"]], [150, 150])
        self.assertEqual(result["ingredient_ranges"][0]["maximum_witness_g"], [150, 50])
        self.assertEqual(result["ingredient_ranges"][1]["maximum_witness_g"], [50, 150])
        self.assertGreater(150 + 150, self.weight_bounds["max_g"])
        self.assertIn("cannot necessarily be combined arbitrarily", result["conditional_range_explanation"])

    def test_missing_nutrient_bounds_and_infeasible_statuses(self):
        missing = NutrientEvidenceTests._enrichment(missing_phosphorus=True)
        result = self.optimiser.optimise_ranges(RECIPE, missing, self.constraints,
                                                 recommended={})
        self.assertEqual(result["status"], "MISSING_NUTRIENT_DATA")
        self.graph.bounds[2].pop("max_amount_g")
        result = self.optimiser.optimise_ranges(RECIPE, self.calculation, self.constraints,
                                                 recommended={})
        self.assertEqual(result["status"], "MISSING_QUANTITY_BOUNDS")
        self.graph.bounds[2]["max_amount_g"] = 150
        result = self.optimiser.optimise_ranges(
            RECIPE, self.calculation, {"protein": {"min": 100, "unit": "g"}},
            recommended={})
        self.assertEqual(result["status"], "INFEASIBLE_CONSTRAINTS")

    def test_culinary_conflict_does_not_weaken_nutrient_limit(self):
        limits = {"protein": {"max": 7, "unit": "g", "scope": "meal"}}
        initial = self.optimiser.optimise(RECIPE, self.calculation, limits)
        policy = copy.deepcopy(POLICY)
        policy["ingredient_ratios"][0]["min"] = 2
        culinary = CulinaryQuantityValidationService().validate_and_reoptimise(
            RECIPE, self.calculation, limits, initial, self.optimiser, rules=policy)
        self.assertEqual(culinary["status"], "CULINARY_INVALID")
        result = self.optimiser.optimise_ranges(
            RECIPE, self.calculation, limits, recommended=initial,
            culinary_linear_constraints=culinary["applied_linear_constraints"])
        self.assertEqual(result["status"], "INFEASIBLE_CONSTRAINTS")

    def test_recommendation_must_satisfy_same_culinary_constraints(self):
        initial = self.optimiser.optimise(RECIPE, self.calculation,
                                          {"protein": {"min": 7, "unit": "g"}})
        policy = copy.deepcopy(POLICY)
        policy["ingredient_ratios"][0]["min"] = 2
        culinary = CulinaryQuantityValidationService().validate_and_reoptimise(
            RECIPE, self.calculation, {"protein": {"min": 7, "unit": "g"}},
            initial, self.optimiser, rules=policy)
        bad = self.optimiser.optimise_ranges(
            RECIPE, self.calculation, {"protein": {"min": 7, "unit": "g"}},
            recommended=initial,
            culinary_linear_constraints=culinary["applied_linear_constraints"])
        self.assertEqual(bad["status"], "OPTIMISATION_ERROR")
        good = self.optimiser.optimise_ranges(
            RECIPE, self.calculation, {"protein": {"min": 7, "unit": "g"}},
            recommended=culinary["adjusted_optimisation"],
            culinary_linear_constraints=culinary["applied_linear_constraints"])
        self.assertEqual(good["status"], "FEASIBLE_RANGES")


if __name__ == "__main__":
    unittest.main()

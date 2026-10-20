import copy
import unittest

from src.recipes.culinary_quantity_validation import CulinaryQuantityValidationService
from src.recipes.ingredient_quantity_optimiser import IngredientQuantityOptimiser
from tests.test_food_recommendation import NutrientEvidenceTests, DATASET
from tests.test_ingredient_quantity_optimiser import QuantityOptimiserTests


RECIPE = {"id": "foodcom:42", "name": "Fixture dish", "source_dataset": DATASET,
          "raw_ingredients": ["first food", "second food"],
          "raw_ingredient_quantities": ["100", "250"],
          "method": ["Cook both foods together."]}


def rules(*, ratio_min=1):
    return {"basis": "explicit fixture culinary policy", "essential_positions": [0, 1],
            "required_roles": ["protein_source", "vegetable"],
            "ingredient_ratios": [{"numerator_position": 0, "denominator_position": 1,
                                   "min": ratio_min, "max": 4}]}


class CulinaryQuantityValidationTests(unittest.TestCase):
    def setUp(self):
        fixture = QuantityOptimiserTests()
        fixture.setUp()
        self.graph = fixture.graph
        self.optimiser = IngredientQuantityOptimiser(self.graph)
        self.calculation = NutrientEvidenceTests._enrichment(sodium=0)
        self.constraints = {"protein": {"min": 7, "unit": "g", "scope": "meal"}}
        self.original = self.optimiser.optimise(RECIPE, self.calculation, self.constraints)
        self.assertEqual(self.original["optimisation_status"], "OPTIMISED")
        self.validator = CulinaryQuantityValidationService()

    def test_valid_combination_with_explicit_rules(self):
        result = self.validator.validate(RECIPE, self.original, rules())
        self.assertEqual(result["status"], "CULINARY_VALID")
        self.assertEqual(result["findings"], [])
        self.assertEqual(result["missing_information"], [])

    def test_missing_rules_stay_unverified(self):
        result = self.validator.validate(RECIPE, self.original)
        self.assertEqual(result["status"], "CULINARY_UNVERIFIED")
        self.assertIn("DISH_PROPORTION_RULE_MISSING", result["missing_information"])
        self.assertIn("DISH_IDENTITY_ROLE_POLICY_MISSING", result["missing_information"])

    def test_missing_essential_ingredient_is_invalid(self):
        damaged = copy.deepcopy(self.original)
        damaged["ingredients"].pop()
        result = self.validator.validate(RECIPE, damaged, rules())
        self.assertEqual(result["status"], "CULINARY_INVALID")
        self.assertIn("ESSENTIAL_INGREDIENT_MISSING", {row["code"] for row in result["findings"]})
        repaired = self.validator.validate_and_reoptimise(
            RECIPE, self.calculation, self.constraints, damaged, self.optimiser,
            rules=rules(),
        )
        self.assertEqual(repaired["status"], "CULINARY_VALID")
        self.assertEqual(len(repaired["adjusted_optimisation"]["ingredients"]), 2)

    def test_graph_relationship_mismatch_prevents_verified_status(self):
        class Graph:
            def foodcom_ingredient_links(self, ids):
                return [{"recipe_id": ids[0], "position": 0, "name": "first food"}]

        validator = CulinaryQuantityValidationService(Graph())
        result = validator.validate(RECIPE, self.original, rules())
        self.assertEqual(result["status"], "CULINARY_UNVERIFIED")
        self.assertIn("GRAPH_RECIPE_INGREDIENT_LINKS_INCOMPLETE", result["missing_information"])

    def test_recorded_ingredient_bounds_are_checked(self):
        damaged = copy.deepcopy(self.original)
        damaged["ingredients"][0]["quantity_g"] = 1000
        result = self.validator.validate(RECIPE, damaged, rules())
        self.assertEqual(result["status"], "CULINARY_INVALID")
        self.assertIn("SOURCED_INGREDIENT_BOUND_VIOLATION", {row["code"] for row in result["findings"]})

    def test_excessive_seasoning_and_aromatic_are_invalid_when_sourced(self):
        for role in ("seasoning", "aromatic"):
            with self.subTest(role=role):
                changed = copy.deepcopy(self.original)
                changed["ingredients"][1]["role"] = role
                policy = {"basis": "reviewed fixture role cap", "essential_positions": [0, 1],
                          "required_roles": ["protein_source", role],
                          "role_limits": {role: {"max_g": 10}},
                          "role_proportions": {role: {"max_fraction": 0.2}}}
                result = self.validator.validate(RECIPE, changed, policy)
                self.assertEqual(result["status"], "CULINARY_INVALID")
                self.assertIn("ROLE_AMOUNT_LIMIT", {row["code"] for row in result["findings"]})

    def test_invalid_ratio_reoptimises_and_rechecks_nutrients(self):
        original_copy = copy.deepcopy(self.original)
        result = self.validator.validate_and_reoptimise(
            RECIPE, self.calculation, self.constraints, self.original, self.optimiser,
            rules=rules(ratio_min=2),
        )
        self.assertEqual(result["original_status"], "CULINARY_INVALID")
        self.assertEqual(result["status"], "CULINARY_VALID")
        self.assertEqual(result["adjustment_status"], "NUTRITIONALLY_RECHECKED")
        self.assertIn("INGREDIENT_RATIO", {row["code"] for row in result["original_findings"]})
        self.assertEqual(result["findings"], [])
        adjusted = result["adjusted_optimisation"]
        self.assertEqual(adjusted["optimisation_status"], "OPTIMISED")
        self.assertGreaterEqual(adjusted["ingredients"][0]["quantity_g"],
                                2 * adjusted["ingredients"][1]["quantity_g"])
        self.assertGreaterEqual(adjusted["nutrient_totals"]["protein_g"], 7)
        self.assertEqual(self.original, original_copy)
        self.assertEqual(RECIPE["raw_ingredient_quantities"], ["100", "250"])

    def test_culinary_rule_conflicts_with_nutrient_limit(self):
        constraints = {"protein": {"max": 7, "unit": "g", "scope": "meal"}}
        initial = self.optimiser.optimise(RECIPE, self.calculation, constraints)
        self.assertEqual(initial["optimisation_status"], "OPTIMISED")
        result = self.validator.validate_and_reoptimise(
            RECIPE, self.calculation, constraints, initial, self.optimiser,
            rules=rules(ratio_min=2),
        )
        self.assertEqual(result["status"], "CULINARY_INVALID")
        self.assertEqual(result["adjustment_status"], "NO_FEASIBLE_CULINARY_COMBINATION")
        self.assertEqual(result["adjusted_optimisation"]["optimisation_status"],
                         "INFEASIBLE_CONSTRAINTS")
        self.assertEqual(initial["nutrient_totals"]["protein_g"], 7)

    def test_model_opinion_does_not_upgrade_missing_rules(self):
        class Model:
            def assess_quantity_plausibility(self, evidence):
                return {"status": "PLAUSIBLE", "reason": "Looks familiar."}

        validator = CulinaryQuantityValidationService(model_provider=lambda: (Model(), None))
        result = validator.validate(RECIPE, self.original, assess_with_model=True)
        self.assertEqual(result["status"], "CULINARY_UNVERIFIED")
        self.assertFalse(result["model_assessment"]["authoritative"])
        self.assertEqual(result["model_assessment"]["result"]["status"], "PLAUSIBLE")


if __name__ == "__main__":
    unittest.main()

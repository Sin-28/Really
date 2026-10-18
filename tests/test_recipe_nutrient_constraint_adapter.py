import unittest
from unittest.mock import Mock

from src.constraints.recipe_nutrient_adapter import RecipeNutrientConstraintAdapter
from src.recipes.ingredient_nutrient_calculator import IngredientNutrientCalculator
from src.web.service import (
    RenAllyWebService,
    recipe_constraint_enrichment_configuration,
)


NUTRIENTS = {
    "sodium_mg": 300.0,
    "potassium_mg": 250.0,
    "phosphorus_mg": 120.0,
    "protein_g": 12.0,
}
UNITS = {"sodium_mg": "mg", "potassium_mg": "mg", "phosphorus_mg": "mg", "protein_g": "g"}


def ingredient(position=0, *, status="COMPLETE", nutrients=None, mapping_status="VERIFIED_MAPPING",
               fdc_id=12345, quantity_g=100, reference_default=None, assumed_attributes=None):
    mapping = {"status": mapping_status, "fdc_id": fdc_id,
               "verification_method": "DETERMINISTIC_EXACT_MATCH"}
    if reference_default is not None:
        mapping["reference_default"] = reference_default
    if assumed_attributes is not None:
        mapping["assumed_attributes"] = assumed_attributes
    return {
        "position": position,
        "ingredient_name": f"ingredient {position}",
        "quantity_g": quantity_g,
        "quantity_raw": "100 g",
        "quantity_status": "RESOLVED",
        "calculation_status": status,
        "nutrients": dict(NUTRIENTS if nutrients is None else nutrients),
        "mapping": mapping,
        "block_reasons": [],
    }


def calculation(*items, status="COMPLETE", servings=2, per_serving=None):
    complete = [item for item in items if item["calculation_status"] in {"COMPLETE", "ASSUMPTION_BASED"}]
    return {
        "calculation_status": status,
        "ingredient_count": len(items),
        "calculated_ingredient_count": len(complete),
        "unresolved_ingredient_count": len(items) - len(complete),
        "ingredients": list(items),
        "servings": servings,
        "per_serving": dict(NUTRIENTS if per_serving is None else per_serving),
        "recipe_totals": dict(NUTRIENTS),
        "unresolved_ingredients": ([{"position": item["position"], "ingredient": item["ingredient_name"],
                                      "quantity_status": item["quantity_status"]}
                                     for item in items if item["calculation_status"] not in {"COMPLETE", "ASSUMPTION_BASED"}]),
        "usda_references_used": [{"fdc_id": item["mapping"].get("fdc_id")} for item in items
                                 if item["mapping"].get("fdc_id")],
        "missing_nutrient_information": [],
    }


class RecipeNutrientConstraintAdapterTests(unittest.TestCase):
    def setUp(self):
        self.adapter = RecipeNutrientConstraintAdapter()

    def evaluate(self, calc, constraints, **kwargs):
        return self.adapter.evaluate(calc, constraints, **kwargs)

    def test_complete_calculation_satisfying_all_four_active_constraints_passes(self):
        rules = {
            "sodium": {"max": 400, "unit": "mg", "scope": "meal"},
            "potassium": {"max": 300, "unit": "mg", "scope": "meal"},
            "phosphorus": {"max": 150, "unit": "mg", "scope": "meal"},
            "protein": {"min": 10, "unit": "g", "scope": "meal"},
        }
        result = self.evaluate(calculation(ingredient()), rules)
        self.assertEqual(result["constraint_status"], "PASS")
        self.assertEqual({item["status"] for item in result["evaluations"]}, {"PASS"})
        self.assertEqual(result["evaluations"][0]["basis"], "per_serving")

    def test_complete_calculation_violating_sodium_fails(self):
        result = self.evaluate(calculation(ingredient()), {"sodium": {"max": 250, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "FAIL")
        self.assertEqual(result["evaluations"][0]["observed_value"], 300)

    def test_complete_calculation_violating_potassium_fails(self):
        result = self.evaluate(calculation(ingredient()), {"potassium": {"max": 200, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "FAIL")

    def test_complete_calculation_violating_phosphorus_fails(self):
        result = self.evaluate(calculation(ingredient()), {"phosphorus": {"max": 100, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "FAIL")

    def test_complete_calculation_violating_protein_lower_bound_fails(self):
        result = self.evaluate(calculation(ingredient()), {"protein": {"min": 13, "unit": "g"}})
        self.assertEqual(result["constraint_status"], "FAIL")
        self.assertEqual(result["evaluations"][0]["operator"], ">=")

    def test_assumption_based_calculation_requires_review_and_never_passes(self):
        item = ingredient(status="ASSUMPTION_BASED", mapping_status="REFERENCE_DEFAULT",
                          reference_default=True, assumed_attributes={"fat_content": "unspecified"})
        result = self.evaluate(calculation(item, status="ASSUMPTION_BASED"),
                               {"sodium": {"max": 400, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "REVIEW_REQUIRED")
        self.assertTrue(result["assumption_dependent"])
        self.assertEqual(result["reference_default_assumptions"][0]["fdc_id"], 12345)

    def test_in_scope_approved_reference_without_assumed_attributes_can_pass(self):
        item = ingredient(mapping_status="REFERENCE_DEFAULT", reference_default=True,
                          assumed_attributes={})
        item["mapping"]["unresolved_attributes"] = []
        item["mapping"]["nutritional_uncertainty"] = "IDENTITY_EXACT"
        result = self.evaluate(calculation(item), {"sodium": {"max": 400, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "PASS")
        self.assertEqual(result["approved_reference_defaults_used"][0]["fdc_id"], 12345)
        self.assertEqual(result["reference_default_assumptions"], [])

    def test_incomplete_calculation_cannot_pass(self):
        item = ingredient(status="INCOMPLETE")
        result = self.evaluate(calculation(item, status="INCOMPLETE"),
                               {"sodium": {"max": 400, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")

    def test_unavailable_calculation_is_unknown(self):
        result = self.evaluate({"calculation_status": "UNAVAILABLE"},
                               {"sodium": {"max": 400, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")

    def test_missing_serving_count_is_unknown(self):
        result = self.evaluate(calculation(ingredient(), servings=None),
                               {"sodium": {"max": 400, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")
        self.assertIn("source_serving_count", result["missing_evidence"])

    def test_whole_recipe_basis_does_not_match_meal_per_serving_constraint(self):
        result = self.evaluate(calculation(ingredient()), {
            "sodium": {"max": 400, "unit": "mg", "scope": "meal", "basis": "whole_recipe"},
        })
        self.assertEqual(result["constraint_status"], "UNKNOWN")
        self.assertIn("do not match", result["evaluations"][0]["reason"])

    def test_missing_nutrient_value_is_unknown_while_recorded_zero_is_valid(self):
        missing = dict(NUTRIENTS)
        missing.pop("sodium_mg")
        no_value = self.evaluate(calculation(ingredient(nutrients=missing)),
                                 {"sodium": {"max": 400, "unit": "mg"}})
        self.assertEqual(no_value["constraint_status"], "UNKNOWN")
        zero_values = dict(NUTRIENTS, sodium_mg=0)
        zero = self.evaluate(calculation(ingredient(nutrients=zero_values), per_serving=zero_values),
                             {"sodium": {"max": 0, "unit": "mg"}})
        self.assertEqual(zero["constraint_status"], "PASS")
        self.assertEqual(zero["evaluations"][0]["observed_value"], 0)

    def test_verified_partial_upper_bound_subtotal_proves_failure(self):
        known = ingredient(0, nutrients=dict(NUTRIENTS, sodium_mg=1200), quantity_g=200)
        unknown = ingredient(1, status="INCOMPLETE", mapping_status="UNRESOLVED", fdc_id=None,
                             quantity_g=None)
        calc = calculation(known, unknown, status="INCOMPLETE", servings=1)
        result = self.evaluate(calc, {"sodium": {"max": 1000, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "FAIL")
        self.assertIsNone(result["evaluations"][0]["observed_value"])
        self.assertEqual(result["evaluations"][0]["known_subtotal"], 1200)

    def test_partial_subtotal_below_upper_bound_remains_unknown(self):
        known = ingredient(0, nutrients=dict(NUTRIENTS, sodium_mg=100))
        unknown = ingredient(1, status="INCOMPLETE", mapping_status="UNRESOLVED", fdc_id=None,
                             quantity_g=None)
        result = self.evaluate(calculation(known, unknown, status="INCOMPLETE", servings=1),
                               {"sodium": {"max": 1000, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")
        self.assertIsNone(result["evaluations"][0]["observed_value"])
        self.assertEqual(result["evaluations"][0]["known_subtotal"], 100)

    def test_incomplete_lower_bound_never_fails_from_partial_subtotal(self):
        known = ingredient(0, nutrients=dict(NUTRIENTS, protein_g=0))
        unknown = ingredient(1, status="INCOMPLETE", mapping_status="UNRESOLVED", fdc_id=None,
                             quantity_g=None)
        result = self.evaluate(calculation(known, unknown, status="INCOMPLETE", servings=1),
                               {"protein": {"min": 10, "unit": "g"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")
        self.assertNotEqual(result["evaluations"][0]["status"], "FAIL")

    def test_mixed_pass_and_fail_results_in_fail(self):
        result = self.evaluate(calculation(ingredient()), {
            "sodium": {"max": 400, "unit": "mg"},
            "potassium": {"max": 200, "unit": "mg"},
        })
        self.assertEqual([item["status"] for item in result["evaluations"]], ["PASS", "FAIL"])
        self.assertEqual(result["constraint_status"], "FAIL")

    def test_review_and_missing_conditions_are_both_preserved(self):
        assumed = ingredient(0, status="ASSUMPTION_BASED", mapping_status="REFERENCE_DEFAULT",
                             reference_default=True, assumed_attributes={"state": "unknown"})
        calc = calculation(assumed, status="ASSUMPTION_BASED", per_serving={"sodium_mg": 300})
        result = self.evaluate(calc, {
            "sodium": {"max": 400, "unit": "mg"},
            "potassium": {"max": 400, "unit": "mg/day"},
        })
        self.assertEqual(result["constraint_status"], "REVIEW_REQUIRED")
        self.assertTrue(result["assumption_dependent"])
        self.assertTrue(result["missing_evidence"])
        self.assertTrue(any(item["status"] == "REVIEW_REQUIRED" for item in result["evaluations"]))
        self.assertTrue(any(item["status"] == "UNKNOWN" for item in result["evaluations"]))

    def test_empty_active_constraint_set_is_not_applicable(self):
        result = self.evaluate(calculation(ingredient()), {})
        self.assertEqual(result["constraint_status"], "NOT_APPLICABLE")

    def test_incompatible_units_are_unknown(self):
        result = self.evaluate(calculation(ingredient()), {"sodium": {"max": 1, "unit": "g"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")

    def test_remaining_constraint_uses_requested_servings_basis(self):
        result = self.evaluate(calculation(ingredient()), {
            "sodium": {"max": 700, "unit": "mg", "scope": "remaining"},
        }, requested_servings=2)
        self.assertEqual(result["evaluations"][0]["basis"], "requested_servings")
        self.assertEqual(result["evaluations"][0]["observed_value"], 600)
        self.assertEqual(result["constraint_status"], "PASS")

    def test_duplicate_positions_disable_partial_subtotal_failure(self):
        first = ingredient(0, nutrients=dict(NUTRIENTS, sodium_mg=700))
        duplicate_position = ingredient(0, nutrients=dict(NUTRIENTS, sodium_mg=700))
        unknown = ingredient(2, status="INCOMPLETE", mapping_status="UNRESOLVED", fdc_id=None,
                             quantity_g=None)
        result = self.evaluate(calculation(first, duplicate_position, unknown, status="INCOMPLETE", servings=1),
                               {"sodium": {"max": 1000, "unit": "mg"}})
        self.assertEqual(result["constraint_status"], "UNKNOWN")
        self.assertIsNone(result["evaluations"][0]["known_subtotal"])


class ConstraintEnrichmentIntegrationTests(unittest.TestCase):
    def test_feature_flag_defaults_off_and_can_be_enabled(self):
        self.assertFalse(recipe_constraint_enrichment_configuration(environ={}))
        self.assertTrue(recipe_constraint_enrichment_configuration(
            environ={"RENALLY_RECIPE_CONSTRAINT_ENRICHMENT": "true"},
        ))
        self.assertFalse(recipe_constraint_enrichment_configuration(enabled=False,
                                                                      environ={"RENALLY_RECIPE_CONSTRAINT_ENRICHMENT": "true"}))

    def test_feature_flag_disabled_does_not_attach_new_metadata(self):
        service = object.__new__(RenAllyWebService)
        service.recipe_mapping_config = {"enabled": False}
        service.recipe_nutrition_enrichment_enabled = False
        service.recipe_constraint_enrichment_enabled = False
        service.recipe_nutrient_enrichment_service = None
        service.ingredient_mapping_service = None
        response = {"evaluations": [{"status": "PASS"}], "answer": "unchanged"}
        service._attach_ingredient_mapping_enrichment(response, {})
        self.assertNotIn("ingredient_constraint_enrichment", response)
        self.assertEqual(response["evaluations"], [{"status": "PASS"}])

    def test_feature_flag_enabled_attaches_informational_metadata_and_preserves_legacy_fields(self):
        service = object.__new__(RenAllyWebService)
        service.recipe_mapping_config = {"enabled": False}
        service.recipe_nutrition_enrichment_enabled = False
        service.recipe_constraint_enrichment_enabled = True
        service.recipe_nutrient_constraint_adapter = RecipeNutrientConstraintAdapter()
        service.ingredient_mapping_service = None
        service.recipe_nutrient_enrichment_service = Mock()
        service.recipe_nutrient_enrichment_service.enrich.return_value = calculation(ingredient())
        response = {"evaluations": [{"status": "LEGACY_UNVERIFIED"}], "selected": {"status": "PASS"}}
        service._attach_ingredient_mapping_enrichment(
            response, {}, constraints={"sodium": {"max": 400, "unit": "mg"}},
            requested_servings=1,
        )
        self.assertEqual(response["ingredient_constraint_enrichment"]["constraint_status"], "PASS")
        self.assertEqual(response["evaluations"], [{"status": "LEGACY_UNVERIFIED"}])
        self.assertEqual(response["selected"], {"status": "PASS"})
        self.assertNotIn("ingredient_nutrient_enrichment", response)
        service.recipe_nutrient_enrichment_service.enrich.assert_called_once()

    def test_missing_nutrient_enrichment_dependency_returns_unknown(self):
        service = object.__new__(RenAllyWebService)
        service.recipe_mapping_config = {"enabled": False}
        service.recipe_nutrition_enrichment_enabled = False
        service.recipe_constraint_enrichment_enabled = True
        service.recipe_nutrient_constraint_adapter = RecipeNutrientConstraintAdapter()
        service.ingredient_mapping_service = None
        service.recipe_nutrient_enrichment_service = None
        response = {}
        service._attach_ingredient_mapping_enrichment(
            response, {}, constraints={"sodium": {"max": 400, "unit": "mg"}},
        )
        self.assertEqual(response["ingredient_constraint_enrichment"]["constraint_status"], "UNKNOWN")
        self.assertFalse(response["ingredient_constraint_enrichment"]["dependency_available"])

    def test_step_5b1_calculation_result_shape_is_consumed_without_schema_change(self):
        class Catalog:
            def get_usda_food_records_by_fdc_ids(self, ids):
                return [{
                    "food": {"fdc_id": 12345, "description": "USDA fixture",
                             "nutrition_basis": "per 100 g", "food_portions_json": "[]"},
                    "nutrients": [
                        {"fdc_nutrient_id": nutrient_id, "unit": unit, "amount": amount}
                        for nutrient_id, unit, amount in [
                            ("1093", "mg", 100), ("1092", "mg", 80),
                            ("1091", "mg", 40), ("1003", "g", 10),
                        ]
                    ],
                }]

        result = IngredientNutrientCalculator(Catalog()).calculate_recipe(
            {"recipe_servings": 2, "raw_ingredient_items": [
                {"position": 0, "name": "fixture food", "quantity_raw": "100 g"},
            ]},
            {"results": [{"position": 0, "mapping": {
                "status": "VERIFIED_MAPPING", "fdc_id": 12345,
                "nutritional_uncertainty": "VERIFIED_IDENTITY", "unresolved_attributes": [],
            }}]},
        )
        self.assertEqual(result["calculation_status"], "COMPLETE")
        evaluated = RecipeNutrientConstraintAdapter().evaluate(
            result, {"sodium": {"max": 60, "unit": "mg"}},
        )
        self.assertEqual(evaluated["constraint_status"], "PASS")
        self.assertEqual(evaluated["usda_fdc_ids_used"], [12345])


if __name__ == "__main__":
    unittest.main()

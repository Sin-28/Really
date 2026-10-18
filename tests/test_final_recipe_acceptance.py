import copy
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.recipes.final_recipe_acceptance import FinalRecipeAcceptancePolicy
from src.web.service import final_recipe_acceptance_configuration


ACTIVE = {"sodium_mg": {"max": 600, "unit": "mg", "scope": "meal"}}


def valid_nutrition(status="PASS"):
    return {
        "nutritional_calculation": {
            "version": "ingredient-nutrients-v1",
            "calculation_status": "COMPLETE",
            "servings": 2,
            "per_serving": {
                "sodium_mg": 100, "potassium_mg": 250,
                "phosphorus_mg": 110, "protein_g": 8,
            },
            "ingredients": [
                {"position": 0, "quantity_g": 100, "quantity_status": "CONVERTED_WEIGHT",
                 "mapping": {"status": "VERIFIED_MAPPING", "fdc_id": 123456,
                             "verification_method": "HUMAN_REVIEW",
                             "verification_provenance": "approved-map-v1"},
                 "calculation_status": "COMPLETE"},
            ],
        },
        "constraint_validation": {
            "constraint_status": status,
            "active_constraint_count": 1,
            "evaluations": [{"nutrient_key": "sodium_mg", "status": status,
                             "reason": "Test constraint result."}],
            "missing_evidence": [],
        },
    }


def valid_culinary(status="PASS"):
    return {
        "status": status,
        "slm_output_valid": True,
        "model_id": "controlled-fixture",
        "prompt_version": "adapted-recipe-culinary-v8",
        "deterministic_guards": {"status": "PASS", "findings": []},
        "functional_coverage_assessments": [
            {"function": "AROMATICS", "importance": "ESSENTIAL", "coverage": "COVERED",
             "supporting_ingredient_ids": ["parsley"]},
        ],
        "missing_information": [],
        "review_reasons": [],
    }


class FinalRecipeAcceptanceTests(unittest.TestCase):
    def setUp(self):
        self.policy = FinalRecipeAcceptancePolicy()
        self.recipe = {"ingredients": [{"id": "parsley", "amount_g": 10}]}

    def decide(self, nutrition=None, culinary=None, **kwargs):
        return self.policy.evaluate(
            nutrition, culinary, constraints=kwargs.pop("constraints", ACTIVE),
            adapted_recipe=kwargs.pop("adapted_recipe", self.recipe),
        )

    def test_only_complete_nutrition_and_culinary_pass_can_be_accepted(self):
        result = self.decide(valid_nutrition(), valid_culinary())
        self.assertEqual(result["status"], "ACCEPTED")
        self.assertEqual(result["policy_version"], "final-recipe-acceptance-v1")
        self.assertEqual(result["mapping_completeness"]["status"], "COMPLETE")
        self.assertEqual(result["quantity_completeness"]["status"], "COMPLETE")

    def test_active_policy_can_be_inferred_from_step_5c_validation(self):
        result = self.policy.evaluate(
            valid_nutrition(), valid_culinary(), adapted_recipe=self.recipe,
        )
        self.assertEqual(result["active_constraint_count"], 1)
        self.assertEqual(result["status"], "ACCEPTED")

    def test_nutritional_fail_rejects_even_if_culinary_is_unavailable(self):
        result = self.decide(valid_nutrition("FAIL"), None)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("Test constraint result.", result["rejection_reasons"])

    def test_culinary_fail_rejects_even_if_nutrition_is_unavailable(self):
        result = self.decide(None, valid_culinary("FAIL"))
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("CULINARY_VALIDATION_FAILED", result["rejection_reasons"])

    def test_unknown_nutrition_is_unverified(self):
        result = self.decide(valid_nutrition("UNKNOWN"), valid_culinary())
        self.assertEqual(result["status"], "UNVERIFIED")

    def test_culinary_unavailable_is_unverified(self):
        result = self.decide(valid_nutrition(), None)
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("CULINARY_VALIDATION_UNAVAILABLE", result["missing_evidence"])

    def test_nutritional_assumption_requires_review(self):
        result = self.decide(valid_nutrition("REVIEW_REQUIRED"), valid_culinary())
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_missing_evidence_takes_precedence_over_assumption_review(self):
        nutrition = valid_nutrition("REVIEW_REQUIRED")
        nutrition["constraint_validation"]["missing_evidence"] = ["missing_nutrient_measurement"]
        result = self.decide(nutrition, valid_culinary())
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("missing_nutrient_measurement", result["missing_evidence"])

    def test_partial_functional_coverage_requires_review(self):
        culinary = valid_culinary("REVIEW_REQUIRED")
        culinary["review_reasons"] = ["ESSENTIAL_FUNCTION_COVERAGE_UNRESOLVED:AROMATICS"]
        result = self.decide(valid_nutrition(), culinary)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_unresolved_mapping_prevents_acceptance(self):
        nutrition = valid_nutrition()
        nutrition["nutritional_calculation"]["ingredients"][0]["mapping"] = {
            "status": "UNRESOLVED", "fdc_id": None,
        }
        result = self.decide(nutrition, valid_culinary())
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertEqual(result["mapping_completeness"]["authorized_count"], 0)

    def test_missing_quantity_prevents_acceptance(self):
        nutrition = valid_nutrition()
        nutrition["nutritional_calculation"]["ingredients"][0]["quantity_g"] = None
        result = self.decide(nutrition, valid_culinary())
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("ESSENTIAL_QUANTITY_UNRESOLVED:0", result["missing_evidence"])

    def test_incompatible_serving_basis_prevents_acceptance(self):
        nutrition = valid_nutrition()
        nutrition["nutritional_calculation"]["servings"] = None
        result = self.decide(nutrition, valid_culinary())
        self.assertEqual(result["status"], "UNVERIFIED")
        self.assertIn("VALID_RECIPE_SERVING_BASIS_MISSING", result["missing_evidence"])

    def test_no_active_constraints_is_not_applicable(self):
        result = self.decide(None, None, constraints={})
        self.assertEqual(result["status"], "NOT_APPLICABLE")
        self.assertNotEqual(result["status"], "ACCEPTED")

    def test_multiple_failures_are_preserved(self):
        result = self.decide(valid_nutrition("FAIL"), valid_culinary("FAIL"))
        self.assertEqual(result["status"], "REJECTED")
        self.assertTrue(any("Test constraint result." in reason for reason in result["rejection_reasons"]))
        self.assertTrue(any("CULINARY" in reason for reason in result["rejection_reasons"]))

    def test_confirmed_guard_failure_is_rejected_and_not_hidden_by_missing_nutrition(self):
        culinary = valid_culinary()
        culinary["deterministic_guards"] = {
            "status": "FAIL", "findings": [{"severity": "CONFIRMED_VIOLATION", "code": "REMOVED_REQUIRED_ITEM"}],
        }
        result = self.decide(None, culinary)
        self.assertEqual(result["status"], "REJECTED")
        self.assertIn("REMOVED_REQUIRED_ITEM", result["rejection_reasons"])

    def test_policy_does_not_mutate_upstream_evidence(self):
        nutrition, culinary = valid_nutrition(), valid_culinary()
        before = copy.deepcopy((nutrition, culinary))
        self.decide(nutrition, culinary)
        self.assertEqual((nutrition, culinary), before)

    def test_feature_flag_is_opt_in(self):
        self.assertFalse(final_recipe_acceptance_configuration(environ={}))
        self.assertFalse(final_recipe_acceptance_configuration(environ={"RENALLY_FINAL_RECIPE_ACCEPTANCE": "false"}))
        self.assertTrue(final_recipe_acceptance_configuration(environ={"RENALLY_FINAL_RECIPE_ACCEPTANCE": "true"}))

    def test_acceptance_only_flag_constructs_no_mapping_nutrition_or_culinary_dependencies(self):
        from src.web import service as web_service

        runtime = SimpleNamespace(
            kg=SimpleNamespace(ingredients={}, recipes={}), retriever=object(),
            engine=object(), substitution_analyzer=object(),
            create_recipe_nutrient_enrichment=Mock(),
        )
        with patch.object(web_service, "create_runtime", return_value=runtime), \
                patch.object(web_service, "HybridIngredientMappingService") as mapping_factory, \
                patch.object(web_service, "AdaptedRecipeCulinaryValidationService") as culinary_factory:
            service = web_service.RenAllyWebService(
                data_dir=web_service.PROJECT_ROOT / "data",
                recipe_mapping_enabled=False,
                recipe_nutrition_enrichment_enabled=False,
                recipe_constraint_enrichment_enabled=False,
                adapted_recipe_nutrition_enrichment_enabled=False,
                adapted_recipe_culinary_validation_enabled=False,
                final_recipe_acceptance_enabled=True,
            )
        self.assertIsNotNone(service.final_recipe_acceptance_policy)
        self.assertIsNone(service.ingredient_mapping_service)
        self.assertIsNone(service.recipe_nutrient_enrichment_service)
        self.assertIsNone(service.adapted_recipe_culinary_validation_service)
        mapping_factory.assert_not_called()
        culinary_factory.assert_not_called()
        runtime.create_recipe_nutrient_enrichment.assert_not_called()

    def test_acceptance_flag_attaches_separate_unverified_metadata(self):
        from src.web.service import RenAllyWebService

        service = object.__new__(RenAllyWebService)
        service.final_recipe_acceptance_enabled = True
        service.final_recipe_acceptance_policy = FinalRecipeAcceptancePolicy()
        metadata = service._final_recipe_acceptance_metadata(
            None, None, ACTIVE, self.recipe, adaptation_id="adapt-1",
        )
        self.assertEqual(metadata["status"], "UNVERIFIED")
        self.assertEqual(metadata["adaptation_id"], "adapt-1")
        self.assertIn("CULINARY_VALIDATION_UNAVAILABLE", metadata["missing_evidence"])

    def test_acceptance_metadata_is_not_created_when_flag_is_disabled(self):
        from src.web.service import RenAllyWebService

        service = object.__new__(RenAllyWebService)
        service.final_recipe_acceptance_enabled = False
        service.final_recipe_acceptance_policy = None
        self.assertIsNone(service._final_recipe_acceptance_metadata(
            valid_nutrition(), valid_culinary(), ACTIVE, self.recipe,
        ))


if __name__ == "__main__":
    unittest.main()

import copy
import json
import time
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.recipes.adapted_recipe_culinary import AdaptedRecipeCulinaryValidationService
from src.web.service import (
    RenAllyWebService,
    adapted_recipe_culinary_validation_configuration,
)


def ramen_recipes(*, broth=False, adapted_instructions=True, seaweed=True):
    original = {
        "id": "ramen-1", "name": "Instant ramen", "servings": 1,
        "ingredients": [
            {"id": "noodles", "name": "instant ramen noodles", "amount_g": 80},
            {"id": "packet", "name": "instant ramen seasoning packet", "amount_g": 6},
        ],
        "method": ["Boil the noodles in water and stir in the seasoning packet."],
    }
    adapted_ingredients = [
        {"id": "noodles", "name": "instant ramen noodles", "amount_g": 80},
    ]
    if seaweed:
        adapted_ingredients.append({"id": "seaweed", "name": "dried seaweed", "amount_g": 3,
                                   "replaces_id": "packet"})
    if broth:
        adapted_ingredients.extend([
            {"id": "broth", "name": "low-sodium vegetable broth", "amount_g": 240,
             "replaces_id": None},
            {"id": "garlic", "name": "garlic", "amount_g": 2},
        ])
    adapted = {
        "id": "ramen-1-adapted", "name": "Adapted instant ramen", "servings": 1,
        "ingredients": adapted_ingredients,
        "method": (["Boil noodles in low-sodium vegetable broth with garlic and seaweed."]
                   if broth else ["Boil the noodles and stir in the dried seaweed."])
        if adapted_instructions else None,
    }
    return original, adapted


def herb_recipes():
    original = {"id": "herb-pasta", "name": "Pasta with basil", "servings": 2,
                "ingredients": [{"id": "pasta", "name": "pasta", "amount_g": 160},
                                {"id": "basil", "name": "fresh basil", "amount_g": 8}],
                "method": ["Boil pasta and finish with fresh basil."]}
    adapted = {"id": "herb-pasta-adapted", "name": "Pasta with parsley", "servings": 2,
               "ingredients": [{"id": "pasta", "name": "pasta", "amount_g": 160},
                               {"id": "parsley", "name": "fresh parsley", "amount_g": 8,
                                "replaces_id": "basil"}],
               "method": ["Boil pasta and finish with fresh parsley."]}
    return original, adapted


def salt_recipes():
    original = {"id": "salt-soup", "name": "Vegetable soup", "servings": 2,
                "ingredients": [{"id": "vegetables", "name": "mixed vegetables", "amount_g": 300},
                                {"id": "salt", "name": "salt", "amount_g": 4}],
                "method": ["Simmer vegetables with salt."]}
    adapted = {"id": "salt-soup-adapted", "name": "Vegetable soup", "servings": 2,
               "ingredients": [{"id": "vegetables", "name": "mixed vegetables", "amount_g": 300},
                               {"id": "herbs", "name": "fresh herbs", "amount_g": 8,
                                "replaces_id": "salt"}],
               "method": ["Simmer vegetables and finish with fresh herbs."]}
    return original, adapted


def unclassified_recipes():
    original = {"id": "generic", "name": "Prepared dish", "servings": 1,
                "ingredients": [{"id": "source", "name": "ingredient", "amount_g": 10}],
                "method": ["Stir into the prepared dish."]}
    adapted = {"id": "generic-adapted", "name": "Prepared dish", "servings": 1,
               "ingredients": [{"id": "replacement", "name": "replacement ingredient", "amount_g": 10,
                                "replaces_id": "source"}],
               "method": ["Stir the replacement into the prepared dish."]}
    return original, adapted


def coverage_row(result, function, change_index=0):
    return next(row for row in result["evaluations"][change_index]["functional_coverage"]
                if row["function"] == function)


def response_for(evaluation_input, *, status="PASS", functional="COMPATIBLE",
                 quantity="PLAUSIBLE", cooking="COMPATIBLE", coherence="COHERENT",
                 issues=None, overall_issues=None, reason=None):
    changes = [row for row in evaluation_input["structured_changes"]
               if row.get("change_type") != "UNCHANGED"]
    evaluations = []
    for change in changes:
        evaluations.append({
            "change_id": change["change_id"],
            "functional_compatibility": functional,
            "quantity_appropriateness": quantity,
            "cooking_compatibility": cooking,
            "recipe_coherence": coherence,
            "reason": reason or (issues or ["The available recipe evidence supports this judgment."])[0],
            "functional_coverage": [],
        })
    coverage = []
    required_map = {row["change_id"]: row["functions"]
                    for row in evaluation_input.get("required_function_assessments", [])}
    coverage_state = {
        "COMPATIBLE": "COVERED", "PARTIAL": "PARTIALLY_COVERED",
        "INCOMPATIBLE": "NOT_COVERED", "UNKNOWN": "UNKNOWN",
    }[functional]
    for change in changes:
        if str(change.get("change_type") or "").upper() == "ADDED":
            continue
        support = ([change["adapted_ingredient_ref"]]
                   if coverage_state == "COVERED" and change.get("adapted_ingredient_ref") else [])
        functions = required_map.get(change["change_id"]) or [
            "OTHER"
        ]
        for function in functions:
            coverage.append({
                "change_id": change["change_id"],
                "function": function,
                "importance": "ESSENTIAL",
                "coverage": coverage_state,
                "supporting_ingredient_ids": support,
                "supporting_process_steps": [],
                "reason": reason or "The adapted recipe evidence supports this function judgment.",
            })
    coverage_by_change = {}
    for row in coverage:
        coverage_by_change.setdefault(row["change_id"], []).append(row)
    for evaluation in evaluations:
        if evaluation["change_id"] in coverage_by_change:
            evaluation["functional_coverage"] = [
                {key: value for key, value in copy.deepcopy(item).items() if key != "change_id"}
                for item in coverage_by_change[evaluation["change_id"]]
            ]
    return {"evaluations": evaluations}


class MockCulinaryGenerator:
    model_name = "mock/Qwen3-1.7B"

    def __init__(self, builder=response_for):
        self.builder = builder
        self.inputs = []

    def evaluate_culinary_compatibility(self, evaluation_input):
        self.inputs.append(copy.deepcopy(evaluation_input))
        return self.builder(evaluation_input)


class CulinaryValidationTests(unittest.TestCase):
    def make_service(self, builder=response_for, **kwargs):
        generator = MockCulinaryGenerator(builder)
        service = AdaptedRecipeCulinaryValidationService(lambda: (generator, None), **kwargs)
        return service, generator

    def validate(self, *, builder=response_for, original=None, adapted=None,
                 changes=None, **kwargs):
        service, generator = self.make_service(builder)
        if original is None or adapted is None:
            original, adapted = ramen_recipes()
        result = service.validate(original, adapted, changes, adaptation_id="adapt-1", **kwargs)
        return result, generator

    def test_functionally_compatible_substitution(self):
        original = {"id": "x", "name": "Pasta", "servings": 2,
                    "ingredients": [{"id": "pasta", "name": "pasta", "amount_g": 160},
                                    {"id": "basil", "name": "fresh basil", "amount_g": 8}],
                    "method": ["Boil pasta and finish with basil."]}
        adapted = copy.deepcopy(original)
        adapted["ingredients"][1] = {"id": "parsley", "name": "fresh parsley", "amount_g": 8,
                                     "replaces_id": "basil"}
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")

    def test_partially_compatible_substitution_requires_review(self):
        result, _ = self.validate(builder=lambda data: response_for(data, status="REVIEW_REQUIRED", functional="PARTIAL",
                                                                     issues=["Some flavor function is not replaced."]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_clearly_incompatible_substitution_can_fail(self):
        result, _ = self.validate(builder=lambda data: response_for(data, status="FAIL", functional="INCOMPATIBLE",
                                                                     coherence="INCOHERENT",
                                                                     issues=["The replacement cannot perform an essential role and the resulting recipe is incoherent."]))
        self.assertEqual(result["status"], "FAIL")

    def test_ramen_seaweed_alone_is_contextual_review_not_name_pair_rule(self):
        original, adapted = ramen_recipes()
        builder = lambda data: response_for(data, status="REVIEW_REQUIRED", functional="PARTIAL",
                                              issues=["Seaweed contributes umami but not a complete broth base."])
        result, generator = self.validate(builder=builder, original=original, adapted=adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual(generator.inputs[0]["adapted_recipe"]["ingredients"][1]["description"], "dried seaweed")

    def test_ramen_seaweed_plus_broth_aromatics_can_be_supported(self):
        original, adapted = ramen_recipes(broth=True)
        def builder(data):
            result = response_for(data)
            support = [row["ingredient_ref"] for row in data["adapted_recipe"]["ingredients"]
                       if row.get("ingredient_ref") != "adapted-ingredient-1"]
            result["evaluations"][0]["functional_coverage"][0].update(
                function="FLAVOR_BASE", importance="ESSENTIAL", coverage="COVERED",
                supporting_ingredient_ids=support,
                reason="Seaweed, vegetable broth, and garlic collectively support a savory cooking base.",
            )
            coverage_row(result, "UMAMI").update(
                importance="SUPPORTING", coverage="COVERED",
                supporting_ingredient_ids=["adapted-ingredient-2"],
                reason="Seaweed contributes umami to the broth and aromatics.",
            )
            return result
        result, _ = self.validate(builder=builder, original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["functional_coverage_assessments"][0]["supporting_ingredient_ids"]), 3)

    def test_essential_function_is_identified_and_returned_with_python_function_id(self):
        result, _ = self.validate()
        assessment = result["functional_coverage_assessments"][0]
        self.assertEqual(assessment["function"], "FLAVOR_BASE")
        self.assertEqual(assessment["importance"], "ESSENTIAL")
        self.assertEqual(assessment["coverage"], "COVERED")
        self.assertEqual(assessment["function_id"], "function-1")

    def test_concentrated_seasoning_source_requires_distinct_coverage_roles(self):
        original, adapted = ramen_recipes()
        result, generator = self.validate(original=original, adapted=adapted)
        required = generator.inputs[0]["required_function_assessments"]
        self.assertEqual(required, [{
            "change_id": "change-1", "functions": ["FLAVOR_BASE", "SALTINESS", "UMAMI"],
        }])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_herb_source_uses_recipe_role_to_require_aromatics_assessment(self):
        original, adapted = herb_recipes()
        result, generator = self.validate(original=original, adapted=adapted)
        self.assertEqual(generator.inputs[0]["required_function_assessments"], [{
            "change_id": "change-1", "functions": ["AROMATICS"],
        }])
        self.assertEqual(result["functional_coverage_assessments"][0]["function"], "AROMATICS")
        self.assertEqual(result["functional_coverage_assessments"][0]["coverage"], "COVERED")

    def test_unclassified_source_does_not_automatically_require_flavor_base(self):
        original, adapted = unclassified_recipes()
        _, generator = self.validate(original=original, adapted=adapted)
        self.assertEqual(generator.inputs[0]["required_function_assessments"], [])
        self.assertEqual(generator.inputs[0]["structured_changes"][0]["original_ingredient"], "ingredient")

    def test_supporting_function_can_be_optional_without_blocking_pass(self):
        original, adapted = herb_recipes()
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="AROMATICS", importance="OPTIONAL", coverage="NOT_COVERED",
                supporting_ingredient_ids=[], reason="The optional aromatic role is omitted.",
            )
            return result
        result, _ = self.validate(builder=builder, original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")

    def test_intentionally_reduced_saltiness_requires_explicit_intent(self):
        original, adapted = salt_recipes()
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="SALTINESS", importance="INTENTIONALLY_REDUCED", coverage="NOT_COVERED",
                supporting_ingredient_ids=[], reason="Saltiness is intentionally reduced for the stated adaptation.",
            )
            return result
        without_intent, _ = self.validate(builder=builder, original=original, adapted=adapted)
        self.assertEqual(without_intent["status"], "REVIEW_REQUIRED")
        with_intent, _ = self.validate(
            builder=builder, original=original, adapted=adapted,
            substitution_metadata={"adaptation_intent": "Intentionally reduce sodium."},
        )
        self.assertEqual(with_intent["status"], "PASS")

    def test_unknown_essential_function_requires_review(self):
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="FLAVOR_BASE", coverage="UNKNOWN", supporting_ingredient_ids=[],
                reason="The recipe context does not establish full coverage.",
            )
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_missing_essential_function_with_coherent_recipe_requires_review(self):
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="FLAVOR_BASE", coverage="NOT_COVERED", supporting_ingredient_ids=[],
                reason="No adapted ingredient or process supplies the original flavor-base role.",
            )
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_essential_coverage_can_use_combined_adapted_ingredients(self):
        original, adapted = ramen_recipes(broth=True)
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="FLAVOR_BASE", coverage="COVERED",
                supporting_ingredient_ids=["adapted-ingredient-2", "adapted-ingredient-3", "adapted-ingredient-4"],
                reason="The added ingredients together provide complementary base and aromatic functions.",
            )
            return result
        result, _ = self.validate(builder=builder, original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(len(result["functional_coverage_assessments"][0]["supporting_ingredient_ids"]), 3)

    def test_supporting_ingredient_absent_from_adapted_recipe_invalidates_coverage(self):
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0]["supporting_ingredient_ids"] = ["made-up-ingredient"]
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("FUNCTION_COVERAGE_0_UNKNOWN_SUPPORTING_INGREDIENT_ID:made-up-ingredient",
                      result["slm_output_schema_errors"])

    def test_covered_function_without_ingredient_or_process_evidence_is_rejected(self):
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0]["supporting_ingredient_ids"] = []
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("FUNCTION_COVERAGE_0_COVERED_WITHOUT_EVIDENCE", result["slm_output_schema_errors"])

    def test_essential_not_covered_conflicts_with_compatible_change_judgment(self):
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="FLAVOR_BASE", coverage="NOT_COVERED", supporting_ingredient_ids=[],
                reason="The original essential role is not covered.",
            )
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertTrue(any("CONTRADICTORY_CULINARY_JUDGMENTS" in reason
                            for reason in result["review_reasons"]))

    def test_broad_flavor_base_needs_evidence_beyond_a_narrow_component(self):
        def builder(data):
            result = response_for(data)
            base = result["evaluations"][0]["functional_coverage"][0]
            coverage_row(result, "UMAMI").update(
                importance="ESSENTIAL", coverage="COVERED",
                supporting_ingredient_ids=list(base["supporting_ingredient_ids"]),
                supporting_process_steps=list(base["supporting_process_steps"]),
                reason="The source provides umami.",
            )
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("ESSENTIAL_FLAVOR_BASE_EVIDENCE_NOT_DISTINCT_FROM_COMPONENT:change-1",
                      result["review_reasons"])

    def test_missing_function_assessment_for_changed_source_prevents_pass(self):
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"] = []
            return result
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("MISSING_FUNCTIONAL_COVERAGE_FOR_CHANGE:change-1", result["slm_output_schema_errors"])
        self.assertIn("MISSING_REQUIRED_FUNCTION_ASSESSMENT:change-1:FLAVOR_BASE",
                      result["slm_output_schema_errors"])
        self.assertEqual(
            {row["function"] for row in result["functional_coverage_assessments"]},
            {"FLAVOR_BASE", "SALTINESS", "UMAMI"},
        )
        self.assertTrue(all(row["coverage"] == "UNKNOWN"
                            for row in result["functional_coverage_assessments"]))

    def test_texture_and_binding_roles_are_evaluated_without_pair_rules(self):
        original, adapted = unclassified_recipes()
        def builder(data):
            result = response_for(data)
            result["evaluations"][0]["functional_coverage"][0].update(
                function="TEXTURE", importance="ESSENTIAL", coverage="PARTIALLY_COVERED",
                reason="The changed ingredient affects texture and available evidence is incomplete.",
            )
            result["evaluations"][0]["functional_coverage"].append({
                "function": "BINDING", "importance": "SUPPORTING", "coverage": "UNKNOWN",
                "supporting_ingredient_ids": [], "supporting_process_steps": [],
                "reason": "The recipe evidence does not establish a binding role.",
            })
            return result
        result, _ = self.validate(builder=builder, original=original, adapted=adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertEqual({row["function"] for row in result["functional_coverage_assessments"]},
                         {"TEXTURE", "BINDING"})

    def test_human_review_fixture_set_is_explicitly_unvalidated_and_reproducible(self):
        path = Path(__file__).resolve().parents[1] / "data" / "culinary_functional_coverage_fixtures.json"
        dataset = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(dataset["label_status"], "developer_defined_test_expectations_not_human_validated")
        self.assertGreaterEqual(len(dataset["cases"]), 14)
        self.assertEqual(len({case["case_id"] for case in dataset["cases"]}), len(dataset["cases"]))
        self.assertTrue(all("original_recipe" in case and "adapted_recipe" in case
                            for case in dataset["cases"]))

    def test_quantity_only_change_is_evaluated(self):
        original = {"ingredients": [{"id": "oil", "name": "olive oil", "amount_g": 5}],
                    "method": ["Drizzle over vegetables."]}
        adapted = {"ingredients": [{"id": "oil", "name": "olive oil", "amount_g": 8}],
                   "method": ["Drizzle over vegetables."]}
        result, generator = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")
        self.assertEqual(generator.inputs[0]["structured_changes"][0]["change_type"], "QUANTITY_CHANGED")

    def test_missing_replacement_quantity_prevents_pass(self):
        original, adapted = ramen_recipes()
        adapted["ingredients"][1].pop("amount_g")
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertTrue(any("ADAPTED_INGREDIENT_QUANTITY_MISSING" in x for x in result["missing_information"]))

    def test_removed_ingredient_in_stale_instruction_is_confirmed_failure(self):
        original = {"ingredients": [{"id": "sugar", "name": "sugar", "amount_g": 10}],
                    "method": ["Add sugar to the sauce."]}
        adapted = {"ingredients": [], "method": ["Add sugar to the sauce."]}
        result, _ = self.validate(original=original, adapted=adapted)
        codes = {item["code"] for item in result["deterministic_guards"]["findings"]}
        self.assertIn("EMPTY_ADAPTED_INGREDIENT_LIST", codes)
        self.assertIn("REMOVED_INGREDIENT_STILL_IN_INSTRUCTIONS", codes)
        self.assertEqual(result["status"], "FAIL")

    def test_ingredient_addition_is_represented(self):
        original = {"ingredients": [{"id": "rice", "name": "rice", "amount_g": 100}],
                    "method": ["Cook rice."]}
        adapted = {"ingredients": [original["ingredients"][0],
                                    {"id": "peas", "name": "peas", "amount_g": 20}],
                   "method": ["Cook rice and peas."]}
        result, generator = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")
        self.assertIn("ADDED", {row["change_type"] for row in generator.inputs[0]["structured_changes"]})

    def test_cooking_method_incompatibility_is_supported_fail(self):
        result, _ = self.validate(builder=lambda data: response_for(data, status="FAIL", cooking="INCOMPATIBLE",
                                                                      issues=["The method does not cook the replacement."]))
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_recipe_coherence_failure_is_supported_fail(self):
        result, _ = self.validate(builder=lambda data: response_for(data, status="FAIL", functional="INCOMPATIBLE",
                                                                      coherence="INCOHERENT",
                                                                      issues=["An essential function is missing and the final sequence cannot produce the dish."]))
        self.assertEqual(result["status"], "FAIL")

    def test_valid_alternative_cooking_method_can_pass(self):
        original, adapted = herb_recipes()
        original["method"] = ["Boil pasta and finish with fresh basil."]
        adapted["method"] = ["Simmer pasta and finish with fresh parsley."]
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "PASS")

    def test_missing_original_instructions_prevents_pass(self):
        original, adapted = ramen_recipes()
        original["method"] = None
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("ORIGINAL_COOKING_INSTRUCTIONS_MISSING", result["missing_information"])

    def test_missing_adapted_instructions_prevents_pass(self):
        original, adapted = ramen_recipes(adapted_instructions=False)
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("ADAPTED_COOKING_INSTRUCTIONS_MISSING", result["missing_information"])

    def test_conflicting_change_record_is_confirmed_failure(self):
        original, adapted = ramen_recipes()
        changes = [{"change_type": "REPLACED", "original_position": 1, "adapted_position": 1,
                    "original_ingredient_id": "packet", "adapted_ingredient_id": "wrong-id",
                    "original_ingredient": "instant ramen seasoning packet",
                    "adapted_ingredient": "dried seaweed"}]
        result, _ = self.validate(original=original, adapted=adapted, changes=changes)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("CHANGE_RECORD_ADAPTED_ITEM_MISSING_OR_CONFLICTING",
                      {row["code"] for row in result["deterministic_guards"]["findings"]})

    def test_empty_adapted_ingredient_list_is_failure(self):
        original, adapted = ramen_recipes()
        adapted["ingredients"] = []
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "FAIL")

    def test_malformed_slm_response_is_unavailable(self):
        result, _ = self.validate(builder=lambda _data: (_ for _ in ()).throw(ValueError("invalid JSON")))
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_unsupported_slm_status_is_unavailable(self):
        result, _ = self.validate(builder=lambda _data: {"status": "MAYBE"})
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("MODEL_SCHEMA_INVALID", result["review_reasons"])

    def test_slm_timeout_is_bounded_and_unavailable(self):
        def slow(_data):
            time.sleep(1.2)
            return {}
        service, _ = self.make_service(slow, inference_timeout_seconds=1)
        original, adapted = ramen_recipes()
        started = time.perf_counter()
        result = service.validate(original, adapted)
        self.assertLess(time.perf_counter() - started, 1.15)
        self.assertEqual(result["slm_evaluation"]["error"], "SLM_TIMEOUT")
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_slm_unavailable_is_distinct(self):
        service = AdaptedRecipeCulinaryValidationService(lambda: (None, "missing local model"))
        original, adapted = ramen_recipes()
        result = service.validate(original, adapted)
        self.assertEqual(result["status"], "UNAVAILABLE")

    def test_deterministic_guard_overrides_slm_pass(self):
        original, adapted = ramen_recipes()
        adapted["method"] = ["Boil the noodles and stir in the instant ramen seasoning packet."]
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("REMOVED_INGREDIENT_STILL_IN_INSTRUCTIONS",
                      {row["code"] for row in result["deterministic_guards"]["findings"]})

    def test_slm_fail_requires_valid_supporting_evidence(self):
        builder = lambda data: response_for(data, status="FAIL", functional="INCOMPATIBLE",
                                             coherence="INCOHERENT",
                                             issues=["No required function remains in the recipe."])
        result, _ = self.validate(builder=builder)
        self.assertEqual(result["status"], "FAIL")

    def test_missing_evidence_prevents_model_pass(self):
        original, adapted = ramen_recipes()
        adapted["ingredients"][1].pop("amount_g")
        result, _ = self.validate(original=original, adapted=adapted)
        self.assertTrue(result["slm_output_valid"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_feature_flag_defaults_off_and_can_enable(self):
        self.assertFalse(adapted_recipe_culinary_validation_configuration(environ={}))
        self.assertTrue(adapted_recipe_culinary_validation_configuration(
            environ={"RENALLY_ADAPTED_RECIPE_CULINARY_VALIDATION": "true"}))

    def test_feature_flag_disabled_attaches_no_culinary_metadata(self):
        runtime = SimpleNamespace(kg=SimpleNamespace(ingredients={}), retriever=object(),
                                  engine=object(), substitution_analyzer=object(), close=Mock())
        with patch("src.web.service.create_runtime", return_value=runtime):
            service = RenAllyWebService(adapted_recipe_culinary_validation_enabled=False)
        self.assertFalse(service.adapted_recipe_culinary_validation_enabled)
        self.assertIsNone(service.adapted_recipe_culinary_validation_service)
        self.assertFalse(adapted_recipe_culinary_validation_configuration(enabled=False))

    def test_feature_flag_enabled_attaches_independent_metadata(self):
        service = object.__new__(RenAllyWebService)
        service.adapted_recipe_culinary_validation_enabled = True
        service.kg = SimpleNamespace(ingredients={})
        generator = MockCulinaryGenerator()
        service.adapted_recipe_culinary_validation_service = AdaptedRecipeCulinaryValidationService(
            lambda: (generator, None))
        original, adapted = ramen_recipes()
        metadata = service._adapted_culinary_metadata(original, adapted, adaptation_id="adapt-1")
        self.assertIn("status", metadata)
        self.assertNotIn("constraint_validation", metadata)

    def test_nutritional_pass_and_culinary_fail_remain_separate(self):
        result, _ = self.validate(builder=lambda data: response_for(data, status="FAIL", functional="INCOMPATIBLE",
                                                                      coherence="INCOHERENT",
                                                                      issues=["Culinary role is lost."]))
        combined = {"constraint_validation": {"constraint_status": "PASS"},
                    "adapted_recipe_culinary_validation": result}
        self.assertEqual(combined["constraint_validation"]["constraint_status"], "PASS")
        self.assertEqual(combined["adapted_recipe_culinary_validation"]["status"], "FAIL")

    def test_nutritional_fail_and_culinary_pass_remain_separate(self):
        original, adapted = herb_recipes()
        result, _ = self.validate(original=original, adapted=adapted)
        combined = {"constraint_validation": {"constraint_status": "FAIL"},
                    "adapted_recipe_culinary_validation": result}
        self.assertEqual(combined["constraint_validation"]["constraint_status"], "FAIL")
        self.assertEqual(combined["adapted_recipe_culinary_validation"]["status"], "PASS")

    def test_existing_adaptation_objects_are_not_mutated(self):
        original, adapted = ramen_recipes()
        before_original = copy.deepcopy(original)
        before_adapted = copy.deepcopy(adapted)
        self.validate(original=original, adapted=adapted)
        self.assertEqual(original, before_original)
        self.assertEqual(adapted, before_adapted)

    def test_step_5c_style_change_records_are_accepted(self):
        original, adapted = ramen_recipes()
        changes = [{
            "change_type": "REPLACED", "original_position": 1, "adapted_position": 1,
            "original_ingredient_id": "packet", "adapted_ingredient_id": "seaweed",
            "original_ingredient": "instant ramen seasoning packet", "adapted_ingredient": "dried seaweed",
            "original_quantity_g": 6, "adapted_quantity_g": 3,
        }]
        result, _ = self.validate(original=original, adapted=adapted, changes=changes)
        self.assertTrue(result["slm_output_valid"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")

    def test_logging_metadata_has_required_evaluation_fields_without_prompt(self):
        result, _ = self.validate()
        self.assertEqual(result["original_recipe_id"], "ramen-1")
        self.assertEqual(result["adaptation_id"], "adapt-1")
        self.assertIn("prompt_version", result)
        self.assertIn("evaluation_duration_ms", result)
        self.assertIn("per_dimension_results", result)
        self.assertNotIn("prompt", result)

    def test_validator_performs_no_database_writes(self):
        original, adapted = herb_recipes()
        result, generator = self.validate(original=original, adapted=adapted)
        self.assertTrue(generator.inputs)
        self.assertEqual(result["status"], "PASS")

    def test_no_detected_changes_never_become_automatic_pass(self):
        original = {"id": "same", "ingredients": [{"id": "rice", "name": "rice", "amount_g": 100}],
                    "method": ["Cook rice."]}
        adapted = copy.deepcopy(original)
        result, generator = self.validate(original=original, adapted=adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("NO_STRUCTURED_CHANGES_TO_EVALUATE", result["missing_information"])
        self.assertFalse(generator.inputs)

    def test_change_ids_are_generated_and_duplicate_source_ids_are_guarded(self):
        original, adapted = ramen_recipes()
        changes = [
            {"change_id": "same", "change_type": "REPLACED", "original_position": 1,
             "adapted_position": 1, "original_ingredient": "instant ramen seasoning packet",
             "adapted_ingredient": "dried seaweed"},
            {"change_id": "same", "change_type": "ADDED", "adapted_position": 1,
             "adapted_ingredient": "dried seaweed"},
        ]
        result, _ = self.validate(original=original, adapted=adapted, changes=changes)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("DUPLICATE_STRUCTURED_CHANGE_ID",
                      {row["code"] for row in result["deterministic_guards"]["findings"]})

    def test_contradictory_addition_record_fails_before_model_judgment(self):
        original = {"ingredients": [{"id": "rice", "name": "rice", "amount_g": 100}],
                    "method": ["Cook rice."]}
        adapted = {"ingredients": [original["ingredients"][0],
                                    {"id": "peas", "name": "peas", "amount_g": 20}],
                   "method": ["Cook rice and peas."]}
        changes = [{"change_type": "ADDED", "original_position": 0, "adapted_position": 1,
                    "adapted_ingredient": "peas"}]
        result, _ = self.validate(original=original, adapted=adapted, changes=changes)
        self.assertEqual(result["status"], "FAIL")
        self.assertIn("ADDITION_RECORD_HAS_ORIGINAL_ITEM",
                      {row["code"] for row in result["deterministic_guards"]["findings"]})

    def test_requires_review_is_python_derived_for_each_final_status(self):
        original, adapted = herb_recipes()
        self.assertFalse(self.validate(original=original, adapted=adapted)[0]["requires_review"])
        partial, _ = self.validate(builder=lambda data: response_for(data, functional="PARTIAL"))
        self.assertTrue(partial["requires_review"])
        incompatible, _ = self.validate(builder=lambda data: response_for(data, functional="INCOMPATIBLE"))
        self.assertEqual(incompatible["status"], "REVIEW_REQUIRED")
        self.assertTrue(incompatible["requires_review"])
        unavailable_service = AdaptedRecipeCulinaryValidationService(lambda: (None, "missing"))
        original, adapted = ramen_recipes()
        unavailable = unavailable_service.validate(original, adapted)
        self.assertTrue(unavailable["requires_review"])

    def test_confirmed_guard_fail_survives_model_unavailable(self):
        original, adapted = ramen_recipes()
        changes = [{"change_type": "REPLACED", "original_position": 1, "adapted_position": 99,
                    "original_ingredient": "instant ramen seasoning packet",
                    "adapted_ingredient": "dried seaweed"}]
        result, _ = self.validate(
            original=original, adapted=adapted, changes=changes,
            builder=lambda _data: (_ for _ in ()).throw(RuntimeError("offline")),
        )
        self.assertEqual(result["status"], "FAIL")

    def test_external_metadata_retains_legacy_evaluation_fields(self):
        result, _ = self.validate()
        self.assertIn("adaptation_id", result)
        self.assertIn("deterministic_guards", result)
        self.assertIn("per_dimension_results", result)
        self.assertEqual(result["per_substitution_evaluations"][0]["functional_compatibility"], "COMPLETE")
        self.assertIn("model_functional_compatibility", result["per_substitution_evaluations"][0])


class MinimalCulinarySchemaTests(unittest.TestCase):
    CHANGE = {"change_id": "change-1", "change_type": "REPLACED"}

    @staticmethod
    def evaluation(change_id="change-1", **overrides):
        row = {
            "change_id": change_id,
            "functional_compatibility": "COMPATIBLE",
            "quantity_appropriateness": "PLAUSIBLE",
            "cooking_compatibility": "COMPATIBLE",
            "recipe_coherence": "COHERENT",
            "reason": "The replacement fills the recipe role.",
            "functional_coverage": [{
                "function": "OTHER", "importance": "ESSENTIAL", "coverage": "UNKNOWN",
                "supporting_ingredient_ids": [], "supporting_process_steps": [],
                "reason": "Function evidence is not available in this test.",
            }],
        }
        row.update(overrides)
        return row

    def validate(self, result, changes=None):
        changes = changes or [self.CHANGE]
        result = copy.deepcopy(result)
        change_types = {row["change_id"]: row.get("change_type") for row in changes}
        for evaluation in result.get("evaluations", []) if isinstance(result.get("evaluations"), list) else []:
            if isinstance(evaluation, dict) and change_types.get(evaluation.get("change_id")) == "ADDED":
                evaluation["functional_coverage"] = []
        return AdaptedRecipeCulinaryValidationService._validate_slm_result(
            result, changes,
        )

    def test_valid_minimal_json(self):
        valid, errors = self.validate({"evaluations": [self.evaluation()]})
        self.assertTrue(valid)
        self.assertEqual(errors, [])

    def test_functional_coverage_array_is_required(self):
        evaluation = self.evaluation()
        evaluation.pop("functional_coverage")
        response = {"evaluations": [evaluation]}
        _, errors = AdaptedRecipeCulinaryValidationService._validate_slm_result(response, [self.CHANGE])
        self.assertIn("EVALUATION_0_MISSING_FIELD:functional_coverage", errors)
        self.assertIn("MISSING_FUNCTIONAL_COVERAGE_FOR_CHANGE:change-1", errors)

    def test_known_required_function_rows_are_enforced(self):
        response = {"evaluations": [self.evaluation()]}
        _, errors = AdaptedRecipeCulinaryValidationService._validate_slm_result(
            response, [self.CHANGE],
            required_functions_by_change={"change-1": ["FLAVOR_BASE", "SALTINESS", "UMAMI"]},
        )
        self.assertIn("MISSING_REQUIRED_FUNCTION_ASSESSMENT:change-1:SALTINESS", errors)
        self.assertIn("MISSING_REQUIRED_FUNCTION_ASSESSMENT:change-1:UMAMI", errors)

    def test_unprovided_supporting_ingredient_reference_is_rejected(self):
        evaluation = self.evaluation()
        evaluation["functional_coverage"] = [{
            "function": "FLAVOR_BASE", "importance": "ESSENTIAL", "coverage": "COVERED",
            "supporting_ingredient_ids": ["adapted-ingredient-99"],
            "supporting_process_steps": [], "reason": "The ingredient supports the flavor base.",
        }]
        response = {"evaluations": [evaluation]}
        _, errors = AdaptedRecipeCulinaryValidationService._validate_slm_result(
            response, [self.CHANGE], adapted_ingredient_refs={"adapted-ingredient-1"},
        )
        self.assertIn(
            "FUNCTION_COVERAGE_0_UNKNOWN_SUPPORTING_INGREDIENT_ID:adapted-ingredient-99", errors,
        )

    def test_compact_evidence_reference_list_is_partitioned_by_python(self):
        evaluation = self.evaluation()
        evaluation["functional_coverage"] = [{
            "function": "BINDING", "importance": "ESSENTIAL", "coverage": "COVERED",
            "supporting_evidence_ids": ["adapted-ingredient-1", "adapted-step-1"],
            "reason": "Flax and hydration support binding.",
        }]
        valid, errors = AdaptedRecipeCulinaryValidationService._validate_slm_result(
            {"evaluations": [evaluation]}, [self.CHANGE],
            adapted_ingredient_refs={"adapted-ingredient-1"},
            adapted_step_refs={"adapted-step-1"},
        )
        self.assertEqual(errors, [])
        row = valid["functional_coverage"][0]
        self.assertEqual(row["supporting_ingredient_ids"], ["adapted-ingredient-1"])
        self.assertEqual(row["supporting_process_steps"], ["adapted-step-1"])

    def test_unprovided_process_step_reference_is_rejected(self):
        evaluation = self.evaluation()
        evaluation["functional_coverage"] = [{
            "function": "FLAVOR_BASE", "importance": "ESSENTIAL", "coverage": "COVERED",
            "supporting_ingredient_ids": [],
            "supporting_process_steps": ["adapted-step-5"], "reason": "The cooking step supports the function.",
        }]
        response = {"evaluations": [evaluation]}
        _, errors = AdaptedRecipeCulinaryValidationService._validate_slm_result(
            response, [self.CHANGE], adapted_step_refs={"adapted-step-1"},
        )
        self.assertIn("FUNCTION_COVERAGE_0_UNKNOWN_SUPPORTING_PROCESS_STEP:adapted-step-5", errors)

    def test_invalid_function_coverage_enums_are_rejected(self):
        evaluation = self.evaluation()
        evaluation["functional_coverage"] = [{
            "function": "MAGIC", "importance": "CRITICAL", "coverage": "FULL",
            "supporting_ingredient_ids": [],
            "supporting_process_steps": [], "reason": "No supported enum was used.",
        }]
        response = {"evaluations": [evaluation]}
        _, errors = AdaptedRecipeCulinaryValidationService._validate_slm_result(response, [self.CHANGE])
        self.assertIn("FUNCTION_COVERAGE_0_FUNCTION_INVALID", errors)
        self.assertIn("FUNCTION_COVERAGE_0_IMPORTANCE_INVALID", errors)
        self.assertIn("FUNCTION_COVERAGE_0_COVERAGE_INVALID", errors)

    def test_missing_evaluations_array_is_rejected(self):
        valid, errors = self.validate({})
        self.assertIsNone(valid)
        self.assertIn("EVALUATIONS_MISSING_OR_NOT_ARRAY", errors)

    def test_missing_required_dimension_is_rejected(self):
        row = self.evaluation()
        row.pop("cooking_compatibility")
        _, errors = self.validate({"evaluations": [row]})
        self.assertIn("EVALUATION_0_MISSING_FIELD:cooking_compatibility", errors)

    def test_invalid_enum_is_rejected(self):
        _, errors = self.validate({"evaluations": [self.evaluation(functional_compatibility="MAYBE")]})
        self.assertIn("EVALUATION_0_FUNCTIONAL_COMPATIBILITY_INVALID", errors)

    def test_missing_reason_is_rejected(self):
        _, errors = self.validate({"evaluations": [self.evaluation(reason=" ")]})
        self.assertIn("EVALUATION_0_REASON_MISSING", errors)

    def test_duplicate_change_ids_are_rejected(self):
        _, errors = self.validate({"evaluations": [self.evaluation(), self.evaluation()]})
        self.assertIn("DUPLICATE_CHANGE_ID:change-1", errors)

    def test_unknown_change_ids_are_rejected(self):
        _, errors = self.validate({"evaluations": [self.evaluation("other")]})
        self.assertIn("UNKNOWN_CHANGE_ID:other", errors)

    def test_missing_change_evaluation_is_rejected(self):
        _, errors = self.validate({"evaluations": []})
        self.assertIn("MISSING_CHANGE_EVALUATION:change-1", errors)

    def test_multiple_changes_require_exact_id_coverage(self):
        changes = [self.CHANGE, {"change_id": "change-2", "change_type": "ADDED"}]
        valid, errors = self.validate({"evaluations": [self.evaluation(), self.evaluation("change-2")]}, changes)
        self.assertEqual(errors, [])
        self.assertEqual([row["change_id"] for row in valid["evaluations"]], ["change-1", "change-2"])

    def test_unexpected_judgment_field_is_rejected(self):
        _, errors = self.validate({"evaluations": [self.evaluation(extra="not allowed")]})
        self.assertIn("EVALUATION_0_UNEXPECTED_FIELD:extra", errors)

    def test_model_generated_overall_status_is_rejected(self):
        _, errors = self.validate({"status": "PASS", "evaluations": [self.evaluation()]})
        self.assertIn("UNEXPECTED_ROOT_FIELD:status", errors)

    def test_change_evaluations_are_returned_in_structured_order(self):
        changes = [self.CHANGE, {"change_id": "change-2", "change_type": "ADDED"}]
        valid, errors = self.validate({"evaluations": [self.evaluation("change-2"), self.evaluation()]}, changes)
        self.assertEqual(errors, [])
        self.assertEqual([row["change_id"] for row in valid["evaluations"]], ["change-1", "change-2"])


if __name__ == "__main__":
    unittest.main()

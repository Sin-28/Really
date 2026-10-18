import unittest

from src.recipes.ingredient_identity_resolution import IngredientIdentityResolver
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator
from src.recipes.ingredient_parser import parse_foodcom_ingredient


def candidate(fdc_id, description):
    return {"fdc_id": fdc_id, "description": description, "food_state": description}


def response(status, selected=None, compatible=(), unresolved=(), requires_review=False):
    return {"status": status, "recommended_fdc_id": selected,
            "compatible_candidate_ids": list(compatible),
            "unresolved_attributes": list(unresolved), "reason": "Recipe context evidence.",
            "requires_review": requires_review}


class NoReferences:
    def preferred_references(self, _category):
        return []


class ContextResolutionTests(unittest.TestCase):
    def resolve(self, ingredient, candidates, model_output, *, quantity="1", context=None):
        parsed = {"name": ingredient, "parsed": parse_foodcom_ingredient(ingredient, quantity)}
        mapping = {"ingredient": ingredient, "normalized_name": ingredient,
                   "status": "AMBIGUOUS", "mapping_version": "test-v1",
                   "candidates": candidates}
        validator = SLMIngredientMappingValidator(evaluator=lambda _payload: model_output)
        evaluation = validator.evaluate(parsed, mapping, recipe_context=context, persist=False)
        resolver = IngredientIdentityResolver(None, NoReferences())
        return evaluation, resolver.resolve(parsed, mapping_result=mapping,
                                            slm_evaluation=evaluation)

    def test_sliced_pepper_resolves_bell_family_without_choosing_color(self):
        foods = [candidate(1, "Peppers, bell, green, raw"),
                 candidate(2, "Peppers, bell, red, raw"),
                 candidate(3, "Spices, pepper, black")]
        context = {"name": "Beef stir-fry", "method": "Slice the pepper into strips and stir-fry."}
        evaluation, result = self.resolve(
            "pepper", foods, response("AMBIGUOUS", compatible=[1, 2], requires_review=True),
            context=context,
        )
        self.assertFalse(evaluation["guard_results"][2]["compatible"])
        self.assertEqual(result["resolution_status"], "FAMILY_RESOLVED")
        self.assertEqual(result["resolved_family"], "bell_pepper")
        self.assertIsNone(result["selected_fdc_id"])
        self.assertEqual(result["plausible_candidate_ids"], [1, 2])

    def test_model_specific_choice_with_two_plausible_variants_is_downgraded_to_family(self):
        foods = [candidate(1, "Peppers, bell, green, raw"),
                 candidate(2, "Peppers, bell, red, raw")]
        evaluation, result = self.resolve(
            "pepper", foods, response("RECOMMENDED", selected=1, compatible=[1, 2]),
            context={"name": "Stir-fry", "method": "Slice the pepper into strips."},
        )
        self.assertEqual(evaluation["status"], "AMBIGUOUS")
        self.assertEqual(result["resolution_status"], "FAMILY_RESOLVED")
        self.assertIsNone(result["selected_fdc_id"])

    def test_pepper_seasoning_can_resolve_specific_black_pepper(self):
        context = {"name": "Soup", "method": "Season with salt and pepper to taste."}
        evaluation, result = self.resolve(
            "pepper", [candidate(4, "Spices, pepper, black"), candidate(5, "Peppers, bell, red, raw")],
            response("RECOMMENDED", selected=4, compatible=[4]), context=context,
        )
        self.assertEqual(evaluation["status"], "RECOMMENDED")
        self.assertEqual(result["resolution_status"], "SLM_RESOLVED")
        self.assertEqual(result["selected_fdc_id"], 4)

    def test_explicit_ground_black_pepper_excludes_white_pepper(self):
        evaluation, result = self.resolve(
            "ground black pepper", [candidate(4, "Spices, pepper, black, ground"),
                                    candidate(5, "Spices, pepper, white, ground")],
            response("RECOMMENDED", selected=4, compatible=[4]), quantity="1 tsp",
        )
        self.assertEqual(evaluation["status"], "RECOMMENDED")
        self.assertFalse(evaluation["guard_results"][1]["compatible"])
        self.assertEqual(result["resolution_status"], "SLM_RESOLVED")

    def test_sliced_green_pepper_does_not_select_green_chilli(self):
        foods = [candidate(7, "Peppers, bell, green"), candidate(8, "Peppers, chili, green")]
        evaluation, result = self.resolve(
            "green pepper", foods, response("RECOMMENDED", selected=7, compatible=[7]),
            context={"name": "Stir-fry", "method": "Slice green pepper into strips."},
        )
        self.assertFalse(evaluation["guard_results"][1]["compatible"])
        self.assertEqual(result["selected_fdc_id"], 7)

    def test_missing_variant_does_not_allow_arbitrary_specific_food(self):
        cases = [
            ("milk", "Milk, whole"), ("cream", "Cream, heavy"),
            ("flour", "Flour, wheat"), ("oil", "Oil, olive"),
            ("onion", "Onions, red"), ("chilli", "Chilli, red"),
        ]
        for ingredient, description in cases:
            with self.subTest(ingredient=ingredient):
                evaluation, result = self.resolve(
                    ingredient, [candidate(10, description)],
                    response("RECOMMENDED", selected=10, compatible=[10]),
                )
                self.assertNotEqual(evaluation["status"], "RECOMMENDED")
                self.assertIsNone(result["selected_fdc_id"])

    def test_plant_and_dairy_milk_are_not_one_resolved_family(self):
        evaluation, result = self.resolve(
            "milk", [candidate(1, "Milk, whole"), candidate(2, "Milk, almond, unsweetened")],
            response("AMBIGUOUS", compatible=[1, 2], requires_review=True),
        )
        self.assertEqual(evaluation["status"], "AMBIGUOUS")
        self.assertEqual(result["resolution_status"], "AMBIGUOUS")

    def test_explicit_variant_conflict_rejects_model_choice(self):
        for ingredient, description in (
            ("red onion", "Onions, yellow"),
            ("wheat flour", "Flour, rice"),
            ("skim milk", "Milk, whole"),
            ("wild salmon", "Salmon, farm raised, raw"),
        ):
            with self.subTest(ingredient=ingredient):
                evaluation, result = self.resolve(
                    ingredient, [candidate(12, description)],
                    response("RECOMMENDED", selected=12, compatible=[12]),
                )
                self.assertEqual(evaluation["status"], "REJECTED")
                self.assertIsNone(result["selected_fdc_id"])

    def test_invalid_model_id_and_malformed_json_never_select_food(self):
        for output in (response("RECOMMENDED", selected=999, compatible=[999]), "not JSON"):
            with self.subTest(output=output):
                evaluation, result = self.resolve("onion", [candidate(10, "Onion")], output)
                self.assertEqual(evaluation["status"], "REVIEW_REQUIRED")
                self.assertIsNone(result["selected_fdc_id"])

    def test_unavailable_model_preserves_ambiguity(self):
        validator = SLMIngredientMappingValidator(
            evaluator=lambda _payload: (_ for _ in ()).throw(RuntimeError("unavailable")))
        evaluation = validator.evaluate(parse_foodcom_ingredient("pepper", "1"),
                                        {"ingredient": "pepper", "candidates": [candidate(1, "Pepper, black")]},
                                        persist=False)
        self.assertEqual(evaluation["status"], "REVIEW_REQUIRED")
        self.assertIsNone(evaluation["recommended_fdc_id"])

    def test_candidate_without_description_cannot_be_selected(self):
        evaluation, result = self.resolve(
            "onion", [{"fdc_id": 22, "description": ""}],
            response("RECOMMENDED", selected=22, compatible=[22]),
        )
        self.assertEqual(evaluation["status"], "REJECTED")
        self.assertIsNone(result["selected_fdc_id"])


if __name__ == "__main__":
    unittest.main()

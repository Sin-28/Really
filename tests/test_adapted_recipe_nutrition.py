import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src.constraints.recipe_nutrient_adapter import RecipeNutrientConstraintAdapter
from src.recipes.adapted_recipe_nutrition import AdaptedRecipeNutritionEnrichmentService
from src.recipes.final_recipe_acceptance import FinalRecipeAcceptancePolicy
from src.recipes.ingredient_mapping import MAPPING_VERSION
from src.recipes.recipe_change_detection import RecipeChangeDetector
from src.recipes.recipe_nutrient_enrichment import RecipeNutrientEnrichmentService
from src.web.service import adapted_recipe_nutrition_enrichment_configuration


def usda_record(fdc_id, sodium=100, potassium=200):
    values = {"1093": sodium, "1092": potassium, "1091": 30, "1003": 10}
    return {
        "food": {
            "fdc_id": fdc_id, "description": f"Fixture food {fdc_id}",
            "source_dataset": "Controlled Step 5C fixture", "nutrition_basis": "per 100 g",
            "source_url": f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
            "food_portions_json": json.dumps([]),
        },
        "nutrients": [
            {"fdc_nutrient_id": key, "unit": "g" if key == "1003" else "mg", "amount": amount}
            for key, amount in values.items()
        ],
    }


def verified_mapping(fdc_id, status="VERIFIED_MAPPING"):
    return {
        "status": status, "fdc_id": fdc_id,
        "mapping_version": MAPPING_VERSION,
        "verification_method": "HUMAN_REVIEW" if status == "VERIFIED_MAPPING" else None,
        "verification_provenance": {"reviewer": "controlled-fixture"} if status == "VERIFIED_MAPPING" else None,
        "nutritional_uncertainty": "VERIFIED_IDENTITY",
        "candidate_ids": [fdc_id] if fdc_id else [],
        "unresolved_attributes": [], "ambiguity_reasons": [],
    }


class FixtureCatalog:
    def __init__(self):
        self.records = {fdc: usda_record(fdc) for fdc in (101, 202)}
        self.lookups = []

    def get_usda_food_records_by_fdc_ids(self, ids):
        self.lookups.append(list(ids))
        return [self.records[fdc] for fdc in ids if fdc in self.records]


class FixtureMapper:
    mode = "READ_ONLY"
    mapper = SimpleNamespace(mapping_version=MAPPING_VERSION)

    def __init__(self, name_to_mapping=None):
        self.name_to_mapping = name_to_mapping or {}
        self.calls = []

    def resolve_recipe(self, recipe):
        rows = recipe.get("raw_ingredient_items") or recipe.get("ingredients") or []
        results = []
        for index, item in enumerate(rows):
            name = item.get("name") or item.get("ingredient_name") or ""
            mapping = self.name_to_mapping.get(name, verified_mapping(202))
            self.calls.append(name)
            results.append({
                "position": index, "original_name": name,
                "mapping": dict(mapping),
            })
        return {
            "mode": self.mode, "ingredient_count": len(results),
            "unique_ingredient_contexts": len(results),
            "mapping_cache_lookups": len(results), "usda_candidate_retrievals": 0,
            "slm_inference_calls": 0, "unresolved_ingredients": 0,
            "results": results,
        }


def build_service(name_to_mapping=None):
    catalog = FixtureCatalog()
    mapper = FixtureMapper(name_to_mapping)
    nutrient_service = RecipeNutrientEnrichmentService(catalog, mapper)
    adapter = RecipeNutrientConstraintAdapter()
    service = AdaptedRecipeNutritionEnrichmentService(nutrient_service, adapter)
    return service, mapper, catalog


class RecipeChangeDetectionTests(unittest.TestCase):
    def setUp(self):
        self.detector = RecipeChangeDetector()

    def test_unchanged_stable_identity_preserves_position(self):
        result = self.detector.compare(
            {"ingredients": [{"id": "salmon", "name": "salmon", "amount_g": 100}]},
            {"ingredients": [{"id": "salmon", "name": "salmon", "amount_g": 100}]},
        )
        self.assertEqual(result["changes"][0]["change_type"], "UNCHANGED")
        self.assertEqual(result["changes"][0]["original_position"], 0)
        self.assertTrue(result["changes"][0]["identity_unchanged"])

    def test_quantity_and_unit_changes_are_distinguished(self):
        quantity = self.detector.compare(
            {"ingredients": [{"name": "salmon", "quantity_raw": "100 g"}]},
            {"ingredients": [{"name": "salmon", "quantity_raw": "200 g"}]},
        )
        self.assertEqual(quantity["changes"][0]["change_type"], "QUANTITY_CHANGED")
        unit = self.detector.compare(
            {"ingredients": [{"name": "oil", "quantity_raw": "1 tbsp"}]},
            {"ingredients": [{"name": "oil", "quantity_raw": "15 ml"}]},
        )
        self.assertEqual(unit["changes"][0]["change_type"], "UNIT_CHANGED")

    def test_explicit_substitution_relation_is_replacement(self):
        result = self.detector.compare(
            {"ingredients": [{"id": "chicken", "name": "chicken breast", "amount_g": 100}]},
            {"ingredients": [{"id": "tofu", "name": "tofu", "amount_g": 100,
                              "replaces_id": "chicken"}]},
        )
        self.assertEqual(result["changes"][0]["change_type"], "REPLACED")
        self.assertEqual(result["changes"][0]["original_ingredient_id"], "chicken")

    def test_additions_and_removals_are_not_falsely_paired(self):
        result = self.detector.compare(
            {"ingredients": [{"id": "chicken", "name": "chicken", "amount_g": 100}]},
            {"ingredients": [{"id": "tofu", "name": "tofu", "amount_g": 100}]},
        )
        self.assertEqual({item["change_type"] for item in result["changes"]}, {"ADDED", "REMOVED"})
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["unresolved_changes"][0]["reason"], "REPLACEMENT_RELATIONSHIP_NOT_EXPLICIT")

    def test_preparation_and_processing_changes_are_detected_with_stable_id(self):
        raw = {"ingredients": [{"id": "rice", "name": "rice", "food_state": "raw", "amount_g": 100}]}
        cooked = {"ingredients": [{"id": "rice", "name": "rice", "food_state": "cooked", "amount_g": 100}]}
        preparation = self.detector.compare(raw, cooked)
        self.assertIn("PREPARATION_CHANGED", {item["change_type"] for item in preparation["changes"]})
        smoke = {"ingredients": [{"id": "salmon", "name": "salmon", "processing_state": "raw", "amount_g": 100}]}
        smoked = {"ingredients": [{"id": "salmon", "name": "salmon", "processing_state": "smoked", "amount_g": 100}]}
        processing = self.detector.compare(smoke, smoked)
        self.assertIn("PROCESSING_CHANGED", {item["change_type"] for item in processing["changes"]})
        regular_soy = {"ingredients": [{"id": "soy", "name": "regular soy sauce", "amount_g": 10}]}
        low_sodium_soy = {"ingredients": [{"id": "soy", "name": "low sodium soy sauce", "amount_g": 10}]}
        soy_change = self.detector.compare(regular_soy, low_sodium_soy)
        self.assertIn("PROCESSING_CHANGED", {item["change_type"] for item in soy_change["changes"]})

    def test_similar_but_unlinked_names_remain_unresolved(self):
        result = self.detector.compare(
            {"ingredients": [{"name": "chicken breast", "quantity_raw": "100 g"}]},
            {"ingredients": [{"name": "chicken thigh", "quantity_raw": "100 g"}]},
        )
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertNotIn("REPLACED", {item["change_type"] for item in result["changes"]})


class AdaptedRecipeNutritionTests(unittest.TestCase):
    def test_verified_unchanged_mapping_reused_and_quantity_recalculated(self):
        service, mapper, _catalog = build_service({"salmon": verified_mapping(101)})
        original = {"recipe_servings": 2,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        adapted = {"recipe_servings": 2,
                   "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "200 g"}]}
        result = service.enrich(original, adapted, {"sodium_mg": {"max": 150, "unit": "mg"}})
        ingredient = result["nutritional_calculation"]["ingredients"][0]
        self.assertTrue(result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]["mapping_reuse"]["reused"])
        self.assertEqual(ingredient["mapping"]["fdc_id"], 101)
        self.assertAlmostEqual(ingredient["nutrients"]["sodium_mg"], 200)
        self.assertEqual(result["original_nutritional_calculation"]["recipe_totals"]["sodium_mg"], 100)
        self.assertEqual(mapper.calls.count("salmon"), 1)

    def test_replaced_ingredient_gets_independent_mapping(self):
        service, _mapper, _catalog = build_service({
            "chicken breast": verified_mapping(101), "tofu": verified_mapping(202),
        })
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "chicken breast", "quantity_raw": "100 g"}]}
        adapted = {"recipe_servings": 1,
                   "raw_ingredient_items": [{"name": "tofu", "quantity_raw": "100 g",
                                              "replaces_position": 0}]}
        result = service.enrich(original, adapted, {})
        row = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
        self.assertEqual(row["mapping"]["fdc_id"], 202)
        self.assertFalse(row["mapping_reuse"]["reused"])
        self.assertEqual(result["changes"][0]["change_type"], "REPLACED")

    def test_unlinked_catalog_identifier_falls_back_to_hybrid_name_mapping(self):
        service, mapper, _catalog = build_service({"tofu": verified_mapping(202)})
        mapper.kg = SimpleNamespace(ingredients={})
        recipe = {"recipe_servings": 1, "ingredients": [
            {"id": "unlinked-catalog-id", "name": "tofu", "amount_g": 100},
        ]}
        original_enrichment = {
            "calculation_status": "UNAVAILABLE",
            "ingredient_mapping_enrichment": {"results": [{
                "position": 0,
                "mapping": {"status": "UNRESOLVED", "fdc_id": None, "mapping_version": MAPPING_VERSION},
            }]},
        }
        result = service.enrich(recipe, recipe, {}, original_enrichment=original_enrichment)
        mapped = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
        self.assertEqual(mapper.calls, ["tofu"])
        self.assertEqual(mapped["mapping"]["fdc_id"], 202)

    def test_incompatible_replacements_never_inherit_original_fdc_id(self):
        pairs = [
            ("regular soy sauce", "low-sodium soy sauce"),
            ("salted butter", "unsalted butter"),
            ("raw salmon", "smoked salmon"),
            ("raw rice", "cooked rice"),
            ("chicken breast", "tofu"),
            ("seasoning packet", "seaweed"),
        ]
        for old_name, new_name in pairs:
            with self.subTest(old_name=old_name, new_name=new_name):
                service, _mapper, _catalog = build_service({
                    old_name: verified_mapping(101), new_name: verified_mapping(202),
                })
                original = {"recipe_servings": 1,
                            "raw_ingredient_items": [{"name": old_name, "quantity_raw": "100 g"}]}
                adapted = {"recipe_servings": 1,
                           "raw_ingredient_items": [{"name": new_name, "quantity_raw": "100 g",
                                                      "replaces_position": 0}]}
                result = service.enrich(original, adapted, {})
                row = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
                self.assertEqual(row["mapping"]["fdc_id"], 202)
                self.assertFalse(row["mapping_reuse"]["reused"])

    def test_food_state_change_forces_remapping_even_with_same_identifier(self):
        service, mapper, _catalog = build_service({
            "salmon": verified_mapping(101),
        })
        original = {"recipe_servings": 1, "ingredients": [
            {"id": "salmon", "name": "salmon", "food_state": "raw", "amount_g": 100},
        ]}
        adapted = {"recipe_servings": 1, "ingredients": [
            {"id": "salmon", "name": "salmon", "food_state": "smoked", "amount_g": 100},
        ]}
        result = service.enrich(original, adapted, {})
        changes = {item["change_type"] for item in result["changes"]}
        self.assertIn("PROCESSING_CHANGED", changes)
        self.assertFalse(result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]["mapping_reuse"]["reused"])
        self.assertIn("smoked salmon", mapper.calls)

    def test_low_sodium_attribute_change_blocks_same_id_verified_reuse(self):
        service, _mapper, _catalog = build_service({
            "regular soy sauce": verified_mapping(101),
            "low sodium soy sauce": verified_mapping(202),
        })
        original = {"recipe_servings": 1, "ingredients": [
            {"id": "soy-sauce", "name": "regular soy sauce", "amount_g": 10},
        ]}
        adapted = {"recipe_servings": 1, "ingredients": [
            {"id": "soy-sauce", "name": "low sodium soy sauce", "amount_g": 20},
        ]}
        result = service.enrich(original, adapted, {})
        row = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
        self.assertIn("PROCESSING_CHANGED", {item["change_type"] for item in result["changes"]})
        self.assertFalse(row["mapping_reuse"]["reused"])
        self.assertEqual(row["mapping"]["fdc_id"], 202)

    def test_approved_reference_is_not_reused_as_verified_mapping(self):
        service, mapper, _catalog = build_service({"cream": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "cream", "quantity_raw": "100 g"}]}
        original_enrichment = {
            "calculation_status": "ASSUMPTION_BASED",
            "recipe_totals": {"sodium_mg": 1},
            "ingredient_mapping_enrichment": {"results": [{
                "position": 0, "mapping": verified_mapping(101, "REFERENCE_DEFAULT"),
            }]},
        }
        result = service.enrich(original, original, {}, original_enrichment=original_enrichment)
        row = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
        self.assertFalse(row["mapping_reuse"]["reused"])
        self.assertEqual(row["mapping"]["fdc_id"], 101)
        self.assertIn("cream", mapper.calls)

    def test_stale_mapping_version_is_remapped(self):
        stale = verified_mapping(101)
        stale["mapping_version"] = "old-mapping-version"
        service, mapper, _catalog = build_service({"salmon": verified_mapping(202)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        original_enrichment = {
            "calculation_status": "COMPLETE",
            "ingredient_mapping_enrichment": {"results": [{"position": 0, "mapping": stale}]},
        }
        result = service.enrich(original, original, {}, original_enrichment=original_enrichment)
        row = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
        self.assertFalse(row["mapping_reuse"]["reused"])
        self.assertEqual(row["mapping"]["fdc_id"], 202)
        self.assertIn("salmon", mapper.calls)

    def test_ambiguous_original_mapping_is_resolved_again(self):
        service, mapper, _catalog = build_service({"oil": verified_mapping(202)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "oil", "quantity_raw": "100 g"}]}
        ambiguous = verified_mapping(None, "AMBIGUOUS")
        ambiguous["candidate_ids"] = [101, 202]
        original_enrichment = {
            "calculation_status": "UNAVAILABLE",
            "ingredient_mapping_enrichment": {"results": [{"position": 0, "mapping": ambiguous}]},
        }
        result = service.enrich(original, original, {}, original_enrichment=original_enrichment)
        row = result["nutritional_calculation"]["ingredient_mapping_enrichment"]["results"][0]
        self.assertFalse(row["mapping_reuse"]["reused"])
        self.assertEqual(row["mapping"]["fdc_id"], 202)
        self.assertIn("oil", mapper.calls)

    def test_added_and_removed_ingredients_use_final_composition_totals(self):
        mappings = {"salmon": verified_mapping(101), "tofu": verified_mapping(202)}
        service, _mapper, _catalog = build_service(mappings)
        original = {"recipe_servings": 1, "raw_ingredient_items": [
            {"name": "salmon", "quantity_raw": "100 g"},
            {"name": "tofu", "quantity_raw": "100 g"},
        ]}
        adapted_removed = {"recipe_servings": 1, "raw_ingredient_items": [
            {"name": "salmon", "quantity_raw": "100 g"},
        ]}
        removed = service.enrich(original, adapted_removed, {})
        self.assertEqual(removed["nutritional_calculation"]["recipe_totals"]["sodium_mg"], 100)
        self.assertEqual(removed["original_nutritional_calculation"]["recipe_totals"]["sodium_mg"], 200)
        self.assertIn("REMOVED", {item["change_type"] for item in removed["changes"]})

        original_for_addition = {"recipe_servings": 1, "raw_ingredient_items": [
            {"name": "salmon", "quantity_raw": "100 g"},
        ]}
        adapted_added = {"recipe_servings": 1, "raw_ingredient_items": [
            {"name": "salmon", "quantity_raw": "100 g"},
            {"name": "tofu", "quantity_raw": "100 g"},
        ]}
        added = service.enrich(original_for_addition, adapted_added, {})
        self.assertEqual(added["nutritional_calculation"]["recipe_totals"]["sodium_mg"], 200)
        self.assertIn("ADDED", {item["change_type"] for item in added["changes"]})

    def test_adaptation_does_not_mutate_original_recipe_or_mapping(self):
        service, _mapper, _catalog = build_service({"salmon": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        before = json.loads(json.dumps(original))
        original_mapping = verified_mapping(101)
        original_enrichment = {
            "calculation_status": "COMPLETE",
            "ingredient_mapping_enrichment": {"results": [{"position": 0, "mapping": original_mapping}]},
        }
        enrichment_before = json.loads(json.dumps(original_enrichment))
        service.enrich(original, original, {}, original_enrichment=original_enrichment)
        self.assertEqual(original, before)
        self.assertEqual(original_enrichment, enrichment_before)

    def test_reference_default_adapted_calculation_remains_assumption_based(self):
        reference = verified_mapping(101, "REFERENCE_DEFAULT")
        reference.update({
            "reference_default": True, "reference_fdc_id": 101,
            "reference_policy_version": 2, "nutritional_uncertainty": "ASSUMPTION_BASED",
            "assumed_attributes": {"food_form": "unspecified"},
        })
        service, _mapper, _catalog = build_service({"salmon": reference})
        recipe = {"recipe_servings": 1,
                  "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        result = service.enrich(recipe, recipe,
                                {"sodium_mg": {"max": 150, "unit": "mg", "scope": "meal"}})
        self.assertEqual(result["nutritional_calculation"]["calculation_status"], "ASSUMPTION_BASED")
        self.assertEqual(result["constraint_validation"]["constraint_status"], "REVIEW_REQUIRED")

    def test_missing_quantity_stays_incomplete_and_constraint_is_not_pass(self):
        service, _mapper, _catalog = build_service({"salt": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "salt", "quantity_raw": "100 g"}]}
        adapted = {"recipe_servings": 1,
                   "raw_ingredient_items": [{"name": "salt", "quantity_raw": None}]}
        result = service.enrich(original, adapted, {"sodium_mg": {"max": 100, "unit": "mg"}})
        self.assertIn(result["nutritional_calculation"]["calculation_status"], {"INCOMPLETE", "UNAVAILABLE"})
        self.assertNotEqual(result["constraint_validation"]["constraint_status"], "PASS")

    def test_missing_unit_and_quantity_range_are_not_collapsed(self):
        service, _mapper, _catalog = build_service({"oil": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "oil", "quantity_raw": "100 g"}]}
        missing_unit = service.enrich(original, {
            "recipe_servings": 1,
            "raw_ingredient_items": [{"name": "oil", "quantity_raw": "2"}],
        }, {})
        ranged = service.enrich(original, {
            "recipe_servings": 1,
            "raw_ingredient_items": [{"name": "oil", "quantity_raw": "1-2 tbsp"}],
        }, {})
        self.assertNotEqual(missing_unit["nutritional_calculation"]["calculation_status"], "COMPLETE")
        self.assertNotEqual(ranged["nutritional_calculation"]["calculation_status"], "COMPLETE")
        self.assertEqual(ranged["nutritional_calculation"]["ingredients"][0]["quantity_status"], "UNRESOLVED_RANGE")

    def test_servings_are_preserved_separately_and_missing_adapted_servings_stay_unknown(self):
        service, _mapper, _catalog = build_service({"salmon": verified_mapping(101)})
        original = {"recipe_servings": 4,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        adapted = {"raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        result = service.enrich(original, adapted, {"sodium_mg": {"max": 100, "unit": "mg"}})
        self.assertEqual(result["original_servings"], 4)
        self.assertIsNone(result["adapted_servings"])
        self.assertEqual(result["constraint_validation"]["constraint_status"], "UNKNOWN")

    def test_controlled_complete_adaptation_calculates_and_revalidates(self):
        service, _mapper, _catalog = build_service({"salmon": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        result = service.enrich(original, original,
                                {"sodium_mg": {"max": 150, "unit": "mg", "scope": "meal"}})
        self.assertEqual(result["nutritional_calculation"]["calculation_status"], "COMPLETE")
        self.assertEqual(result["constraint_validation"]["constraint_status"], "PASS")
        self.assertEqual(result["original_recipe"]["recipe_servings"], 1)
        self.assertEqual(result["adapted_recipe"]["recipe_servings"], 1)

    def test_step_5c_result_and_step_6a_pass_combine_to_acceptance(self):
        service, _mapper, _catalog = build_service({"salmon": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        adapted = {"recipe_servings": 1,
                   "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "80 g"}]}
        nutrition = service.enrich(
            original, adapted,
            {"sodium_mg": {"max": 100, "unit": "mg", "scope": "meal"}},
        )
        culinary = {
            "status": "PASS", "slm_output_valid": True,
            "model_id": "controlled-culinary-fixture",
            "prompt_version": "adapted-recipe-culinary-v8",
            "deterministic_guards": {"status": "PASS", "findings": []},
            "functional_coverage_assessments": [],
            "missing_information": [], "review_reasons": [],
        }
        accepted = FinalRecipeAcceptancePolicy().evaluate(
            nutrition, culinary,
            constraints={"sodium_mg": {"max": 100, "unit": "mg", "scope": "meal"}},
            adapted_recipe=adapted,
        )
        self.assertEqual(nutrition["constraint_validation"]["constraint_status"], "PASS")
        self.assertEqual(nutrition["nutritional_calculation"]["calculation_status"], "COMPLETE")
        self.assertEqual(nutrition["nutritional_calculation"]["per_serving"]["sodium_mg"], 80)
        self.assertEqual(accepted["status"], "ACCEPTED")

    def test_candidate_mapping_and_nutrient_read_caches_are_request_scoped(self):
        service, mapper, catalog = build_service({"salmon": verified_mapping(101)})
        original = {"recipe_servings": 1,
                    "raw_ingredient_items": [{"name": "salmon", "quantity_raw": "100 g"}]}
        original_enrichment = service.nutrient_service.enrich(original)
        mapping_cache, nutrient_cache = {}, {}
        first = service.enrich(original, original, {}, original_enrichment=original_enrichment,
                               mapping_cache=mapping_cache, nutrient_record_cache=nutrient_cache)
        second = service.enrich(original, original, {}, original_enrichment=original_enrichment,
                                mapping_cache=mapping_cache, nutrient_record_cache=nutrient_cache)
        self.assertIn("ingredient_remapping", first["performance_ms"])
        self.assertEqual(mapper.calls.count("salmon"), 1)
        self.assertEqual(catalog.lookups.count([101]), 2)
        self.assertEqual(second["nutritional_calculation"]["calculation_status"], "COMPLETE")


class AdaptedNutritionFlagTests(unittest.TestCase):
    def test_feature_flag_is_off_by_default_and_opt_in(self):
        self.assertFalse(adapted_recipe_nutrition_enrichment_configuration(environ={}))
        self.assertFalse(adapted_recipe_nutrition_enrichment_configuration(environ={
            "RENALLY_ADAPTED_RECIPE_NUTRITION_ENRICHMENT": "false",
        }))
        self.assertTrue(adapted_recipe_nutrition_enrichment_configuration(environ={
            "RENALLY_ADAPTED_RECIPE_NUTRITION_ENRICHMENT": "true",
        }))

    def test_disabled_feature_constructs_no_additional_mapping_or_nutrient_service(self):
        from src.web import service as web_service

        runtime = SimpleNamespace(
            kg=SimpleNamespace(ingredients={}, recipes={}),
            retriever=object(), engine=object(), substitution_analyzer=object(),
            create_recipe_nutrient_enrichment=Mock(),
        )
        with patch.object(web_service, "create_runtime", return_value=runtime), \
                patch.object(web_service, "HybridIngredientMappingService") as mapping_factory:
            service = web_service.RenAllyWebService(
                data_dir=web_service.PROJECT_ROOT / "data",
                recipe_mapping_enabled=False,
                recipe_nutrition_enrichment_enabled=False,
                recipe_constraint_enrichment_enabled=False,
                adapted_recipe_nutrition_enrichment_enabled=False,
            )
        self.assertIsNone(service.ingredient_mapping_service)
        self.assertIsNone(service.recipe_nutrient_enrichment_service)
        self.assertIsNone(service.adapted_recipe_nutrition_enrichment_service)
        mapping_factory.assert_not_called()
        runtime.create_recipe_nutrient_enrichment.assert_not_called()

    def test_enabled_feature_constructs_read_only_enrichment_dependencies(self):
        from src.web import service as web_service

        runtime = SimpleNamespace(
            kg=SimpleNamespace(ingredients={}, recipes={}),
            retriever=object(), engine=object(), substitution_analyzer=object(),
            create_recipe_nutrient_enrichment=Mock(return_value=object()),
        )
        with patch.object(web_service, "create_runtime", return_value=runtime), \
                patch.object(web_service, "HybridIngredientMappingService", return_value="mapping") as mapping_factory:
            service = web_service.RenAllyWebService(
                data_dir=web_service.PROJECT_ROOT / "data",
                recipe_mapping_enabled=False,
                recipe_nutrition_enrichment_enabled=False,
                recipe_constraint_enrichment_enabled=False,
                adapted_recipe_nutrition_enrichment_enabled=True,
            )
        self.assertTrue(service.adapted_recipe_nutrition_enrichment_enabled)
        mapping_factory.assert_called_once_with(
            runtime.kg, runtime.retriever, mode="READ_ONLY",
            persist_mappings=False, use_slm=False,
        )
        runtime.create_recipe_nutrient_enrichment.assert_called_once_with(mapping_service="mapping")
        self.assertIsNotNone(service.adapted_recipe_nutrition_enrichment_service)

    def test_adapted_flag_does_not_run_original_enrichment_on_non_adaptation_route(self):
        from src.web.service import RenAllyWebService

        service = object.__new__(RenAllyWebService)
        mapping = Mock()
        nutrient = Mock()
        service.ingredient_mapping_service = mapping
        service.recipe_nutrient_enrichment_service = nutrient
        service.recipe_mapping_config = {"enabled": False}
        service.recipe_nutrition_enrichment_enabled = False
        service.recipe_constraint_enrichment_enabled = False
        service.adapted_recipe_nutrition_enrichment_enabled = True
        response = {"recipe": {"id": "controlled"}}
        self.assertIs(service._attach_ingredient_mapping_enrichment(response, {}), response)
        mapping.resolve_recipe.assert_not_called()
        nutrient.enrich.assert_not_called()
        self.assertEqual(response, {"recipe": {"id": "controlled"}})


if __name__ == "__main__":
    unittest.main()

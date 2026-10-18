import json
import unittest
from unittest.mock import Mock

from src.graph.neo4j_graph import Neo4jKnowledgeGraph
from src.recipes.ingredient_nutrient_calculator import (
    IngredientNutrientCalculator,
    USDAFoodPortionConversionStore,
)
from src.recipes.quantity_resolution import IngredientQuantityResolver
from src.recipes.recipe_nutrient_enrichment import RecipeNutrientEnrichmentService
from src.web.service import recipe_nutrition_enrichment_configuration
from src.web.service import RenAllyWebService


def food_record(fdc_id=2684441, amounts=None, units=None, basis="per 100 g", portions=None):
    amounts = amounts or {"1093": 49.5, "1092": 378, "1091": 230, "1003": 20.3}
    units = units or {"1093": "mg", "1092": "mg", "1091": "mg", "1003": "g"}
    return {
        "food": {
            "fdc_id": fdc_id,
            "description": "Test USDA food",
            "source_dataset": "USDA FoodData Central",
            "dataset_key": "foundation_foods",
            "data_type": "Foundation",
            "nutrition_basis": basis,
            "source_url": f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients",
            "food_portions_json": json.dumps(portions or []),
        },
        "nutrients": [
            {"fdc_nutrient_id": key, "name": key, "unit": units.get(key), "amount": amount}
            for key, amount in amounts.items()
        ],
    }


def mapping(position=0, status="VERIFIED_MAPPING", fdc_id=2684441, **fields):
    return {
        "position": position,
        "mapping": {
            "status": status,
            "fdc_id": fdc_id,
            "usda_description": "Test USDA food",
            "nutritional_uncertainty": "VERIFIED_IDENTITY" if status == "VERIFIED_MAPPING" else "IDENTITY_EXACT",
            "unresolved_attributes": [],
            **fields,
        },
    }


class ReadOnlyFoodCatalog:
    def __init__(self, records):
        self.records = records
        self.lookups = []

    def get_usda_food_records_by_fdc_ids(self, ids):
        self.lookups.append(list(ids))
        return [record for record in self.records if record["food"]["fdc_id"] in ids]


class IngredientQuantityResolutionTests(unittest.TestCase):
    def setUp(self):
        self.resolver = IngredientQuantityResolver()

    def test_weight_units_and_fractional_quantities_resolve_to_grams(self):
        recipe = {"raw_ingredient_items": [
            {"name": "salmon", "quantity_raw": "4 oz"},
            {"name": "flour", "quantity_raw": "1½ lb"},
            {"name": "water", "quantity_raw": "250 g"},
        ]}
        result = self.resolver.resolve_recipe(recipe)
        grams = [item["quantity_g"] for item in result["ingredients"]]
        self.assertAlmostEqual(grams[0], 113.3980925)
        self.assertAlmostEqual(grams[1], 1.5 * 453.59237)
        self.assertEqual(grams[2], 250)

    def test_legacy_separate_fields_are_combined_only_when_aligned(self):
        aligned = self.resolver.resolve_recipe({
            "raw_ingredients": ["olive oil", "rice"],
            "raw_ingredient_quantities": ["2 tbsp", "100 g"],
        })
        self.assertEqual(aligned["alignment_status"], "ALIGNED_PARALLEL_ARRAYS")
        self.assertEqual([item["quantity_raw"] for item in aligned["ingredients"]], ["2 tbsp", "100 g"])
        self.assertEqual(aligned["ingredients"][1]["quantity_g"], 100)

        mismatched = self.resolver.resolve_recipe({
            "raw_ingredients": ["olive oil", "rice"],
            "raw_ingredient_quantities": ["2 tbsp"],
        })
        self.assertEqual(mismatched["alignment_status"], "MISMATCHED_ARRAY_LENGTHS")
        self.assertTrue(all(item["quantity_raw"] is None for item in mismatched["ingredients"]))
        self.assertTrue(all(item["quantity_status"] == "MISSING" for item in mismatched["ingredients"]))

    def test_empty_legacy_quantity_array_is_missing_not_paired(self):
        result = self.resolver.resolve_recipe({
            "raw_ingredients": ["milk", "eggs"], "raw_ingredient_quantities": [],
        })
        self.assertEqual(result["alignment_status"], "QUANTITY_ARRAY_EMPTY")
        self.assertTrue(all(item["quantity_status"] == "MISSING" for item in result["ingredients"]))

    def test_combined_description_fraction_and_range_are_preserved(self):
        result = self.resolver.resolve_recipe({"ingredients": [
            "1⅕ oz salmon", "2-3 oz chicken", "salt to taste",
        ]})
        self.assertAlmostEqual(result["ingredients"][0]["quantity"], 1.2)
        self.assertEqual(result["ingredients"][1]["quantity_status"], "CONVERTED_RANGE")
        self.assertEqual(result["ingredients"][1]["quantity_g_min"], 2 * 28.349523125)
        self.assertEqual(result["ingredients"][2]["quantity_status"], "UNRESOLVED")

    def test_step1_parsed_rows_can_prove_their_own_alignment(self):
        result = self.resolver.resolve_recipe({
            "raw_ingredients": ["salmon"],
            "raw_ingredient_items": [{
                "position": 0, "name": "salmon", "quantity_raw": "4 oz",
                "parsed": {"original_text": "4 oz salmon"},
            }],
        })
        self.assertEqual(result["alignment_status"], "ORDERED_STEP1_PARSED_ITEMS")
        self.assertAlmostEqual(result["ingredients"][0]["quantity_g"], 113.3980925)

    def test_quantity_ranges_are_not_collapsed_to_a_single_amount(self):
        result = self.resolver.resolve_recipe({
            "raw_ingredient_items": [{"name": "olive oil", "quantity_raw": "1-2 tbsp"}],
        })["ingredients"][0]
        self.assertEqual(result["quantity_status"], "UNRESOLVED_RANGE")
        self.assertIsNone(result["quantity_g"])
        self.assertEqual((result["quantity_min"], result["quantity_max"]), (1, 2))


class USDAConversionReferenceTests(unittest.TestCase):
    def setUp(self):
        self.store = USDAFoodPortionConversionStore()

    def test_ingredient_specific_volume_factor_comes_from_usda_portion(self):
        food = food_record(171413, portions=[{
            "id": 88667, "amount": 1, "gramWeight": 13.5, "modifier": "tablespoon",
        }])["food"]
        ref, reason = self.store.find(food, "tbsp")
        self.assertIsNone(reason)
        self.assertEqual(ref.fdc_id, 171413)
        self.assertEqual(ref.grams_per_unit, 13.5)
        self.assertIn("Food Portion 88667", ref.source)
        self.assertEqual(ref.reference_version, "usda-food-portion-v1")

    def test_count_conversion_requires_matching_explicit_egg_size(self):
        food = food_record(171287, portions=[
            {"id": 1, "amount": 1, "gramWeight": 50, "modifier": "large"},
            {"id": 2, "amount": 1, "gramWeight": 44, "modifier": "medium"},
        ])["food"]
        food["description"] = "Egg, whole, raw, fresh"
        self.assertEqual(self.store.find(food, "count")[1], "REQUIRED_PORTION_ATTRIBUTES_NOT_STATED")
        ref, reason = self.store.find(food, "count", {"egg_size": "large"})
        self.assertIsNone(reason)
        self.assertEqual(ref.grams_per_unit, 50)

    def test_no_generic_conversion_for_unknown_food_or_unit(self):
        food = food_record(1, portions=[])["food"]
        self.assertEqual(self.store.find(food, "cup")[1], "NO_INGREDIENT_SPECIFIC_FACTOR")


class IngredientNutrientCalculationTests(unittest.TestCase):
    def calculator(self, *records):
        return IngredientNutrientCalculator(ReadOnlyFoodCatalog(records))

    @staticmethod
    def recipe(name="salmon", quantity="113.398 g", servings=2):
        return {
            "id": "fixture:recipe", "recipe_servings": servings,
            "raw_ingredient_items": [{"position": 0, "name": name, "quantity_raw": quantity}],
        }

    def test_verified_mapping_uses_deterministic_per_100g_formula(self):
        calculator = self.calculator(food_record())
        result = calculator.calculate_recipe(
            self.recipe(), {"results": [mapping()]},
        )
        self.assertEqual(result["calculation_status"], "COMPLETE")
        self.assertAlmostEqual(result["ingredients"][0]["nutrients"]["sodium_mg"], 56.13, places=2)
        self.assertAlmostEqual(result["ingredients"][0]["nutrients"]["potassium_mg"], 428.64, places=2)
        self.assertAlmostEqual(result["ingredients"][0]["nutrients"]["phosphorus_mg"], 260.82, places=2)
        self.assertAlmostEqual(result["ingredients"][0]["nutrients"]["protein_g"], 23.02, places=2)
        self.assertEqual(result["usda_references_used"][0]["fdc_id"], 2684441)

    def test_approved_reference_preserves_assumption_provenance(self):
        record = food_record(173418, amounts={"1093": 314, "1092": 132, "1091": 107, "1003": 6.15})
        approved = mapping(
            status="REFERENCE_DEFAULT", fdc_id=173418,
            reference_default=True, reference_fdc_id=173418,
            reference_policy_version=2,
            nutritional_uncertainty="ASSUMPTION_BASED",
            assumed_attributes={"fat_content": "unspecified"},
            unresolved_attributes=["fat_content"],
        )
        result = self.calculator(record).calculate_recipe(
            self.recipe("cream cheese", "100 g", 1), {"results": [approved]},
        )
        self.assertEqual(result["calculation_status"], "ASSUMPTION_BASED")
        self.assertEqual(result["ingredients"][0]["mapping"]["reference_policy_version"], 2)
        self.assertEqual(result["mapping_uncertainty"][0]["assumed_attributes"], {"fat_content": "unspecified"})

    def test_ambiguous_mapping_does_not_select_a_candidate_or_calculate(self):
        catalog = ReadOnlyFoodCatalog([food_record()])
        result = IngredientNutrientCalculator(catalog).calculate_recipe(
            self.recipe(), {"results": [mapping(status="AMBIGUOUS", fdc_id=None, candidate_ids=[2684441])]},
        )
        self.assertEqual(result["calculation_status"], "UNAVAILABLE")
        self.assertIsNone(result["recipe_totals"])
        self.assertEqual(catalog.lookups, [])

    def test_guarded_slm_identity_uses_usda_values_but_family_result_does_not(self):
        catalog = ReadOnlyFoodCatalog([food_record()])
        calculator = IngredientNutrientCalculator(catalog)
        resolved = calculator.calculate_recipe(self.recipe(), {"results": [
            mapping(status="SLM_RESOLVED", nutritional_uncertainty="CONTEXT_VALIDATED"),
        ]})
        self.assertEqual(resolved["calculation_status"], "COMPLETE")
        self.assertAlmostEqual(resolved["ingredients"][0]["nutrients"]["sodium_mg"], 56.13, places=2)

        catalog.lookups.clear()
        family = calculator.calculate_recipe(self.recipe(), {"results": [
            mapping(status="FAMILY_RESOLVED", fdc_id=None, candidate_ids=[2684441]),
        ]})
        self.assertEqual(family["calculation_status"], "UNAVAILABLE")
        self.assertIsNone(family["recipe_totals"])
        self.assertEqual(catalog.lookups, [])

    def test_missing_nutrient_is_not_zero_but_recorded_zero_is_valid(self):
        missing = food_record(amounts={"1093": 0, "1092": 0, "1003": 0})
        result = self.calculator(missing).calculate_recipe(self.recipe("salmon", "100 g", 1),
                                                           {"results": [mapping()]})
        self.assertEqual(result["calculation_status"], "UNAVAILABLE")
        self.assertIn("MISSING", {item["status"] for item in result["missing_nutrient_information"][0]["issues"]})

        zero = food_record(amounts={"1093": 0, "1092": 0, "1091": 0, "1003": 0})
        valid = self.calculator(zero).calculate_recipe(self.recipe("salmon", "100 g", 1),
                                                       {"results": [mapping()]})
        self.assertEqual(valid["calculation_status"], "COMPLETE")
        self.assertEqual(valid["recipe_totals"]["sodium_mg"], 0)
        self.assertEqual(valid["ingredients"][0]["nutrient_measurements_per_100g"]["sodium_mg"]["status"],
                         "RECORDED_ZERO")

    def test_unsupported_unit_and_invalid_measurement_basis_block_calculation(self):
        units = {"1093": "g", "1092": "mg", "1091": "mg", "1003": "g"}
        unsupported = self.calculator(food_record(units=units)).calculate_recipe(
            self.recipe("salmon", "100 g", 1), {"results": [mapping()]},
        )
        self.assertIn("UNSUPPORTED_UNIT", {i["status"] for i in unsupported["missing_nutrient_information"][0]["issues"]})

        invalid_basis = self.calculator(food_record(basis="per serving")).calculate_recipe(
            self.recipe("salmon", "100 g", 1), {"results": [mapping()]},
        )
        self.assertEqual(invalid_basis["missing_nutrient_information"][0]["issues"][0]["status"],
                         "INVALID_MEASUREMENT_BASIS")

    def test_recipe_aggregation_and_per_serving_calculation(self):
        second = food_record(2684442, amounts={"1093": 10, "1092": 20, "1091": 30, "1003": 40})
        recipe = {
            "recipe_servings": 2,
            "raw_ingredient_items": [
                {"name": "salmon", "quantity_raw": "100 g"},
                {"name": "fish", "quantity_raw": "50 g"},
            ],
        }
        result = self.calculator(food_record(), second).calculate_recipe(recipe, {
            "results": [mapping(position=0), mapping(position=1, fdc_id=2684442)],
        })
        self.assertEqual(result["calculation_status"], "COMPLETE")
        self.assertAlmostEqual(result["recipe_totals"]["sodium_mg"], 54.5)
        self.assertAlmostEqual(result["per_serving"]["sodium_mg"], 27.25)

    def test_partial_aggregation_withholds_recipe_totals(self):
        recipe = {"raw_ingredient_items": [
            {"name": "salmon", "quantity_raw": "100 g"},
            {"name": "salt", "quantity_raw": None},
        ]}
        result = self.calculator(food_record()).calculate_recipe(recipe, {
            "results": [mapping(position=0), mapping(position=1, status="AMBIGUOUS", fdc_id=None)],
        })
        self.assertEqual(result["calculation_status"], "INCOMPLETE")
        self.assertEqual(result["calculated_ingredient_count"], 1)
        self.assertIsNone(result["recipe_totals"])
        self.assertIsNotNone(result["calculated_subtotals"])
        self.assertIsNone(result["per_serving"])

    def test_missing_servings_withholds_per_serving_only(self):
        result = self.calculator(food_record()).calculate_recipe(
            self.recipe(servings=None), {"results": [mapping()]},
        )
        self.assertEqual(result["calculation_status"], "COMPLETE")
        self.assertIsNotNone(result["recipe_totals"])
        self.assertIsNone(result["per_serving"])

    def test_usda_portion_converts_olive_oil_and_size_qualified_egg_count(self):
        olive = food_record(171413, portions=[{
            "id": 88667, "amount": 1, "gramWeight": 13.5, "modifier": "tablespoon",
        }])
        olive_result = self.calculator(olive).calculate_recipe(
            self.recipe("olive oil", "2 tbsp", 1), {"results": [mapping(fdc_id=171413)]},
        )
        self.assertAlmostEqual(olive_result["ingredients"][0]["quantity_g"], 27)
        self.assertTrue(olive_result["quantity_conversion_assumptions"])

        egg = food_record(171287, portions=[{
            "id": 88374, "amount": 1, "gramWeight": 50, "modifier": "large",
        }])
        egg["food"]["description"] = "Egg, whole, raw, fresh"
        egg_result = self.calculator(egg).calculate_recipe(
            self.recipe("large eggs", "2", 1), {"results": [mapping(fdc_id=171287)]},
        )
        self.assertAlmostEqual(egg_result["ingredients"][0]["quantity_g"], 100)
        generic = self.calculator(egg).calculate_recipe(
            self.recipe("eggs", "2", 1), {"results": [mapping(fdc_id=171287)]},
        )
        self.assertEqual(generic["calculation_status"], "UNAVAILABLE")

    def test_volume_ranges_can_be_resolved_as_ranges_without_definitive_nutrients(self):
        olive = food_record(171413, portions=[{
            "id": 88667, "amount": 1, "gramWeight": 13.5, "modifier": "tablespoon",
        }])
        result = self.calculator(olive).calculate_recipe(
            self.recipe("olive oil", "1-2 tbsp", 1), {"results": [mapping(fdc_id=171413)]},
        )
        self.assertEqual(result["calculation_status"], "UNAVAILABLE")
        self.assertIsNone(result["recipe_totals"])
        self.assertEqual(result["ingredients"][0]["quantity_g_range"], {"minimum": 13.5, "maximum": 27.0})

    def test_disabled_web_enrichment_leaves_response_unchanged(self):
        service = object.__new__(RenAllyWebService)
        service.ingredient_mapping_service = None
        service.recipe_nutrient_enrichment_service = None
        service.recipe_mapping_config = {"enabled": False}
        response = {"recipe": {"ingredients": ["salmon"]}}
        returned = service._attach_ingredient_mapping_enrichment(response, {})
        self.assertIs(returned, response)
        self.assertEqual(set(response), {"recipe"})

    def test_enabled_enrichment_is_attached_as_separate_response_metadata(self):
        class EnricherStub:
            def enrich(self, recipe):
                return {
                    "calculation_status": "INCOMPLETE",
                    "recipe_totals": None,
                    "ingredient_mapping_enrichment": {"results": [{"mapping": {"status": "UNRESOLVED"}}]},
                }

        service = object.__new__(RenAllyWebService)
        service.ingredient_mapping_service = None
        service.recipe_nutrient_enrichment_service = EnricherStub()
        service.recipe_mapping_config = {"enabled": False}
        source = {"raw_ingredients": ["salt"], "raw_ingredient_quantities": []}
        response = {"recipe": {"name": "Unchanged"}}
        service._attach_ingredient_mapping_enrichment(response, source)
        self.assertEqual(response["ingredient_nutrient_enrichment"]["calculation_status"], "INCOMPLETE")
        self.assertNotIn("ingredient_mapping_enrichment", response)
        self.assertEqual(source["raw_ingredient_quantities"], [])

    def test_mixed_assumption_and_unresolved_items_stays_incomplete(self):
        recipe = {"recipe_servings": 1, "raw_ingredient_items": [
            {"name": "cream cheese", "quantity_raw": "100 g"},
            {"name": "unknown", "quantity_raw": "1 cup"},
        ]}
        approved = mapping(position=0, status="REFERENCE_DEFAULT", fdc_id=173418,
                           reference_default=True, reference_fdc_id=173418,
                           nutritional_uncertainty="ASSUMPTION_BASED",
                           assumed_attributes={"type": "generic"}, unresolved_attributes=["type"])
        result = self.calculator(food_record(173418)).calculate_recipe(
            recipe, {"results": [approved, mapping(position=1, status="UNRESOLVED", fdc_id=None)]},
        )
        self.assertEqual(result["calculation_status"], "INCOMPLETE")
        self.assertEqual(result["calculated_ingredient_count"], 1)
        self.assertEqual(result["ingredients"][0]["calculation_status"], "ASSUMPTION_BASED")


class Neo4jNutrientRepositoryTests(unittest.TestCase):
    def test_existing_bounded_usda_reader_preserves_raw_schema(self):
        kg = object.__new__(Neo4jKnowledgeGraph)
        kg._rows = Mock(return_value=[{"food": {"fdc_id": 1}, "nutrients": [
            {"fdc_nutrient_id": "1093", "name": "Sodium, Na", "unit": "mg", "amount": 0},
        ]}])
        result = kg.get_usda_food_records_by_fdc_ids([1])
        self.assertEqual(result[0]["nutrients"][0]["amount"], 0)
        self.assertEqual(result[0]["nutrients"][0]["unit"], "mg")
        self.assertIn("OPTIONAL MATCH", kg._rows.call_args.args[0])


class HybridRecipeNutrientEnrichmentTests(unittest.TestCase):
    def test_enrichment_flag_is_disabled_by_default(self):
        self.assertFalse(recipe_nutrition_enrichment_configuration(environ={}))
        self.assertFalse(recipe_nutrition_enrichment_configuration(environ={
            "RENALLY_RECIPE_NUTRITION_ENRICHMENT": "false",
        }))
        self.assertTrue(recipe_nutrition_enrichment_configuration(environ={
            "RENALLY_RECIPE_NUTRITION_ENRICHMENT": "yes",
        }))

    def test_step5a_mapping_receives_unpaired_names_without_guessed_quantities(self):
        class MappingStub:
            def resolve_recipe(self, recipe):
                self.recipe = recipe
                return {"results": [mapping(position=0, status="UNRESOLVED", fdc_id=None)]}

        kg = ReadOnlyFoodCatalog([])
        mapper = MappingStub()
        service = RecipeNutrientEnrichmentService(kg, mapper)
        source = {
            "raw_ingredients": ["cream cheese", "eggs"],
            "raw_ingredient_quantities": ["1"],
        }
        service.enrich(source)
        mapped_items = mapper.recipe["raw_ingredient_items"]
        self.assertEqual([item["name"] for item in mapped_items], ["cream cheese", "eggs"])
        self.assertTrue(all(item["quantity_raw"] is None for item in mapped_items))
        self.assertEqual(source["raw_ingredient_quantities"], ["1"])


if __name__ == "__main__":
    unittest.main()

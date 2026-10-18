import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.import_foodcom_recipes import _recipe_row, import_recipes
from src.graph.neo4j_graph import Neo4jKnowledgeGraph
from src.recipes.ingredient_parser import (
    ConversionFactor, parse_foodcom_ingredient, parse_ingredient_text,
)


class IngredientParserTests(unittest.TestCase):
    def test_weight_units_and_decimal(self):
        cases = (
            ("4 oz salmon steaks", 113.3980925, "salmon"),
            ("1 lb chicken breast", 453.59237, "chicken breast"),
            ("1.5 kg carrots", 1500.0, "carrots"),
            ("25 g salt", 25.0, "salt"),
        )
        for text, grams, normalized in cases:
            with self.subTest(text=text):
                parsed = parse_ingredient_text(text)
                self.assertEqual(parsed["original_text"], text)
                self.assertEqual(parsed["quantity_status"], "CONVERTED")
                self.assertAlmostEqual(parsed["quantity_g"], grams)
                self.assertEqual(parsed["normalized_name"], normalized)
        self.assertEqual(parse_ingredient_text("4 oz salmon steaks")["ingredient_name"],
                         "salmon steaks")

    def test_fraction_mixed_fraction_and_unicode_fraction(self):
        for text, expected in (("1/2 lb chicken", 0.5),
                               ("1 1/2 lb chicken", 1.5),
                               ("1½ lb chicken", 1.5),
                               ("½ lb chicken", 0.5)):
            with self.subTest(text=text):
                parsed = parse_ingredient_text(text)
                self.assertEqual(parsed["quantity"], expected)
                self.assertAlmostEqual(parsed["quantity_g"], expected * 453.59237)

    def test_ranges_preserve_both_endpoints(self):
        converted = parse_ingredient_text("2-3 oz salmon")
        self.assertEqual(converted["quantity_status"], "CONVERTED_RANGE")
        self.assertIsNone(converted["quantity"])
        self.assertEqual((converted["quantity_min"], converted["quantity_max"]), (2, 3))
        self.assertAlmostEqual(converted["quantity_g_min"], 2 * 28.349523125)
        self.assertAlmostEqual(converted["quantity_g_max"], 3 * 28.349523125)

        unresolved = parse_ingredient_text("1-2 tbsp oil")
        self.assertEqual(unresolved["quantity_status"], "UNRESOLVED_RANGE")
        self.assertEqual((unresolved["quantity_min"], unresolved["quantity_max"]), (1, 2))
        self.assertIsNone(unresolved["quantity_g"])

    def test_volume_and_count_require_exact_sourced_reference(self):
        self.assertEqual(parse_ingredient_text("2 tbsp olive oil")["quantity_status"], "UNRESOLVED")
        self.assertEqual(parse_ingredient_text("1 cup cooked rice")["normalized_name"],
                         "cooked rice")
        self.assertEqual(parse_ingredient_text("2 eggs")["unit"], "count")
        self.assertIsNone(parse_ingredient_text("2 eggs")["quantity_g"])
        factors = {
            ("olive oil", "tbsp"): ConversionFactor(13.5, "test portion reference"),
            ("cooked rice", "cup"): ConversionFactor(158, "test portion reference"),
            ("egg", "count"): ConversionFactor(50, "test portion reference"),
        }
        self.assertEqual(parse_ingredient_text("2 tbsp olive oil", factors)["quantity_g"], 27)
        self.assertEqual(parse_ingredient_text("1 cup cooked rice", factors)["quantity_g"], 158)
        self.assertEqual(parse_ingredient_text("2 eggs", factors)["quantity_g"], 100)
        self.assertEqual(parse_ingredient_text("1 cup raw rice", factors)["quantity_status"],
                         "UNRESOLVED")
        self.assertEqual(parse_ingredient_text("2 tbsp oil", factors)["quantity_status"],
                         "UNRESOLVED")
        self.assertEqual(parse_ingredient_text("2 tbsp olive oil", factors)["conversion_source"],
                         "test portion reference")

    def test_missing_ambiguous_and_per_item_quantities(self):
        missing = parse_ingredient_text("salt to taste")
        self.assertEqual(missing["quantity_status"], "UNRESOLVED")
        self.assertIsNone(missing["quantity"])
        self.assertEqual(missing["ingredient_name"], "salt to taste")

        per_item = parse_ingredient_text("4 oz salmon steaks, each")
        self.assertEqual(per_item["quantity_scope"], "PER_ITEM")
        self.assertEqual(per_item["quantity_status"], "UNRESOLVED_PER_ITEM")
        self.assertIsNone(per_item["quantity_g"])
        self.assertAlmostEqual(per_item["quantity_g_per_item"], 113.3980925)
        self.assertEqual(per_item["ingredient_name"], "salmon steaks, each")
        self.assertEqual(per_item["normalized_name"], "salmon")

        self.assertEqual(parse_ingredient_text("1/0 cup rice")["quantity_status"], "UNPARSED")
        self.assertEqual(parse_ingredient_text("2-1 oz salmon")["quantity_status"], "UNPARSED")

    def test_foodcom_split_fields_do_not_invent_a_quantity(self):
        parsed = parse_foodcom_ingredient("2 percent milk", None)
        self.assertEqual(parsed["ingredient_name"], "2 percent milk")
        self.assertIsNone(parsed["quantity"])
        self.assertEqual(parsed["quantity_status"], "UNRESOLVED")


class FoodComImportRegressionTests(unittest.TestCase):
    RAW_RECIPE = {
        "RecipeId": "42", "Name": "Salmon supper",
        "RecipeIngredientParts": 'c("salmon steaks", "salt")',
        "RecipeIngredientQuantities": 'c("4 oz", "to taste")',
        "RecipeServings": "2", "RecipeInstructions": 'c("Cook salmon")',
    }

    def test_recipe_row_preserves_raw_fields_and_adds_parsing(self):
        row = _recipe_row(self.RAW_RECIPE)
        details = json.loads(row["properties"]["details_json"])
        self.assertEqual(details["ingredients"], ["salmon steaks", "salt"])
        self.assertEqual(details["ingredient_quantities"], ["4 oz", "to taste"])
        self.assertEqual(details["ingredient_items"][0]["name"], "salmon steaks")
        self.assertEqual(details["ingredient_items"][0]["quantity_raw"], "4 oz")
        self.assertEqual(details["ingredient_items"][0]["parsed"]["quantity_status"], "CONVERTED")
        self.assertEqual(details["ingredient_items"][1]["parsed"]["quantity_status"], "UNRESOLVED")
        self.assertEqual(row["references"][0]["quantity_raw"], "4 oz")
        self.assertFalse(row["properties"]["nutrition_complete"])

    def test_old_recipe_details_remain_readable(self):
        graph = object.__new__(Neo4jKnowledgeGraph)
        graph.recipes = {"foodcom:42": {"id": "foodcom:42", "source_dataset":
                         "Food.com Recipes and Reviews (Kaggle)"}}
        old_details = {"ingredients": ["salmon"], "ingredient_quantities": ["4 oz"],
                       "ingredient_items": [{"name": "salmon", "quantity_raw": "4 oz"}]}
        graph._rows = lambda *args, **kwargs: [{"details_json": json.dumps(old_details)}]
        result = graph.recipe_details("foodcom:42")
        self.assertEqual(result["raw_ingredient_items"], old_details["ingredient_items"])

    def test_parser_error_does_not_abort_recipe_row(self):
        with patch("scripts.import_foodcom_recipes.parse_foodcom_ingredient",
                   side_effect=ValueError("malformed source")):
            row = _recipe_row(self.RAW_RECIPE)
        details = json.loads(row["properties"]["details_json"])
        self.assertEqual(details["ingredient_items"][0]["quantity_raw"], "4 oz")
        self.assertEqual(details["ingredient_items"][0]["parsed"]["quantity_status"], "UNPARSED")

    def test_csv_import_pipeline_stores_parsed_details(self):
        class Result:
            def consume(self):
                return None

        class Session:
            def __init__(self):
                self.calls = []

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def run(self, query, **kwargs):
                self.calls.append((query, kwargs))
                return Result()

        class Driver:
            def __init__(self):
                self.session_instance = Session()

            def session(self, database):
                return self.session_instance

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "recipes.csv"
            path.write_text(
                'RecipeId,Name,RecipeIngredientParts,RecipeIngredientQuantities,RecipeServings,RecipeInstructions\n'
                '42,Salmon supper,"c(""salmon steaks"")","c(""4 oz"")",2,"c(""Cook salmon"")"\n',
                encoding="utf-8",
            )
            driver = Driver()
            summary = import_recipes(driver, "neo4j", path, limit=1)
        self.assertEqual(summary["recipes_imported_or_updated"], 1)
        recipe_write = next(kwargs["rows"] for query, kwargs in driver.session_instance.calls
                            if "SET recipe += row.properties" in query)
        details = json.loads(recipe_write[0]["properties"]["details_json"])
        self.assertEqual(details["ingredient_items"][0]["parsed"]["quantity_status"], "CONVERTED")


if __name__ == "__main__":
    unittest.main()

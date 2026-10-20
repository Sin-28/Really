import unittest

from src.graph.neo4j_graph import Neo4jKnowledgeGraph
from src.retrieval.graph_retriever import GraphRetriever
from src.web.request_planner import RequestPlanner
from src.web.service import RenAllyWebService
from src.recipes.recipe_nutrient_enrichment import (
    RecipeNutrientEnrichmentService, assess_foodcom_nutrient_evidence,
    assess_verified_nutritional_feasibility,
)


DATASET = "Food.com Recipes and Reviews (Kaggle)"


class Catalog:
    def __init__(self):
        self.recipes = {
            "foodcom:1": {"name": "Chicken Soup", "source_dataset": DATASET,
                          "tags": ["Dinner"], "ingredient_names": ["chicken", "carrot"]},
            "foodcom:2": {"name": "Creamy Pasta", "source_dataset": DATASET,
                          "tags": ["Dinner"], "ingredient_names": ["chicken", "pasta"]},
            "foodcom:3": {"name": "Low Sodium Vegetable Stew", "source_dataset": DATASET,
                          "tags": ["Dinner"], "ingredient_names": ["carrot", "potato"]},
            "foodcom:4": {"name": "Fried Rice", "source_dataset": DATASET,
                          "tags": ["Dinner"], "ingredient_names": ["rice", "egg"]},
        }
        self.links = {
            rid: [{"recipe_id": rid, "name": name} for name in row["ingredient_names"]]
            for rid, row in self.recipes.items()
        }

    def search(self, index_name, search_query, k=5, dataset_scope="all"):
        terms = search_query.casefold().split(" or ")
        rows = []
        for rid, recipe in self.recipes.items():
            text = " ".join([recipe["name"], *recipe["tags"], *recipe["ingredient_names"]]).casefold()
            if any(term in text for term in terms):
                rows.append({"id": rid, "name": recipe["name"], "aliases": [], "score": 2.0})
        return rows[:k]

    def foodcom_ingredient_links(self, recipe_ids):
        return [row for rid in recipe_ids for row in self.links[rid]]

    def recipe_details(self, recipe_id):
        recipe = self.recipes[recipe_id]
        return {**recipe, "raw_ingredients": recipe["ingredient_names"],
                "raw_ingredient_quantities": ["2", "1"],
                "method": ["Cook ingredients together."]}


class FoodRecommendationTests(unittest.TestCase):
    def setUp(self):
        self.catalog = Catalog()
        self.retriever = GraphRetriever(self.catalog)

    def test_sample_requests_route_to_recommendations(self):
        planner = RequestPlanner(self.catalog, self.retriever, object())
        for query in ("Suggest some chicken dishes.", "What can I eat for dinner?",
                      "Recommend some low-sodium meals.", "Can I eat fried rice?"):
            with self.subTest(query=query):
                plan, _, _, _ = planner.plan(query, False, {})
                self.assertEqual(plan["intent"], "food_recommendation")
        self.assertNotEqual(planner.plan("Plan my meals for the day", False, {})[0]["intent"],
                            "food_recommendation")
        optimisation = planner.plan("Optimise ingredient quantities for fried rice", False, {})[0]
        self.assertEqual(optimisation["intent"], "food_recommendation")
        self.assertTrue(optimisation["optimise_quantities"])

    def test_recipe_title_and_graph_ingredient_links_rank_chicken_dishes(self):
        results = self.retriever.recommend_foodcom_recipes("Suggest some chicken dishes", k=3)
        self.assertEqual([item["recipe_id"] for item in results[:2]], ["foodcom:1", "foodcom:2"])
        self.assertIn("chicken", results[1]["matched_ingredients"])

    def test_low_sodium_is_a_label_preference_only(self):
        service = object.__new__(RenAllyWebService)
        service.retriever = self.retriever
        service.kg = self.catalog
        response = service._recommend_food("Recommend some low-sodium meals", {
            "recommendation_query": "Recommend some low-sodium meals", "requested_count": 5,
        }, {"active_budgets": {"sodium_mg": {"max": 500, "unit": "mg", "scope": "meal"}}}, [])
        self.assertEqual([row["recipe_id"] for row in response["food_recommendations"]],
                         ["foodcom:3"])
        self.assertEqual(response["food_recommendations"][0]["nutritional_status"], "INSUFFICIENT_DATA")
        self.assertFalse(response["constraints_applied"])
        self.assertEqual(response["food_recommendations"][0]["constraint_assessment"]["constraint_status"],
                         "UNKNOWN")
        self.assertIn("not a verified nutrient claim", response["answer"])
        self.assertNotIn("nutrients", response["food_recommendations"][0])

    def test_response_has_multiple_dishes_and_raw_source_metadata(self):
        service = object.__new__(RenAllyWebService)
        service.retriever = self.retriever
        service.kg = self.catalog
        response = service._recommend_food("What can I eat for dinner?", {
            "recommendation_query": "What can I eat for dinner?", "requested_count": 3,
        }, {}, [])
        self.assertEqual(len(response["food_recommendations"]), 3)
        for row in response["food_recommendations"]:
            self.assertTrue(row["recipe_id"].startswith("foodcom:"))
            self.assertTrue(row["name"])
            self.assertTrue(row["ingredients"])
            self.assertIsInstance(row["retrieval_score"], float)
            self.assertEqual(row["instructions"], ["Cook ingredients together."])
            self.assertEqual(row["source_quantities"], ["2", "1"])
            self.assertEqual(row["nutritional_status"], "INSUFFICIENT_DATA")
        self.assertEqual(self.retriever.recommend_foodcom_recipes("Can I eat fried rice?", k=3)[0]["recipe_id"],
                         "foodcom:4")

    def test_chat_routes_recommendations_without_nutrient_calculation(self):
        service = object.__new__(RenAllyWebService)
        service.kg = self.catalog
        service.retriever = self.retriever
        service.profiles = {}
        service.planner = RequestPlanner(self.catalog, self.retriever, object())
        response = service.chat({"query": "Suggest some chicken dishes", "use_slm": False,
                                 "meal_limits": {"sodium": 500}})
        self.assertGreaterEqual(len(response["food_recommendations"]), 2)
        self.assertFalse(response["constraints_applied"])
        self.assertTrue(all(row["nutritional_status"] == "INSUFFICIENT_DATA"
                            for row in response["food_recommendations"]))

    def test_graph_reader_uses_existing_ingredient_relationships(self):
        graph = object.__new__(Neo4jKnowledgeGraph)
        captured = {}
        def rows(query, **params):
            captured.update(query=query, params=params)
            return []
        graph._rows = rows
        graph.foodcom_ingredient_links(["foodcom:1"])
        self.assertIn("LISTS_INGREDIENT_TEXT", captured["query"])
        self.assertEqual(captured["params"]["recipe_ids"], ["foodcom:1"])


class NutrientEvidenceTests(unittest.TestCase):
    @staticmethod
    def _enrichment(*, second_mapping=True, missing_phosphorus=False, sodium=0):
        class NutrientCatalog:
            def get_usda_food_records_by_fdc_ids(self, ids):
                rows = []
                for fdc_id in ids:
                    nutrients = [
                        {"fdc_nutrient_id": "1093", "amount": sodium, "unit": "mg"},
                        {"fdc_nutrient_id": "1092", "amount": 120, "unit": "mg"},
                        {"fdc_nutrient_id": "1003", "amount": 7, "unit": "g"},
                    ]
                    if not missing_phosphorus or fdc_id == 1:
                        nutrients.append({"fdc_nutrient_id": "1091", "amount": 40, "unit": "mg"})
                    rows.append({"food": {"fdc_id": fdc_id, "description": f"Food {fdc_id}",
                                           "nutrition_basis": "per 100 g"}, "nutrients": nutrients})
                return rows

        class Mapper:
            def resolve_recipe(self, recipe, max_ingredients=None):
                return {"results": [
                    {"position": 0, "mapping": {"status": "EXACT_IDENTITY", "fdc_id": 1,
                                                 "unresolved_attributes": []}},
                    {"position": 1, "mapping": {"status": "EXACT_IDENTITY" if second_mapping else "UNRESOLVED",
                                                 "fdc_id": 2 if second_mapping else None,
                                                 "unresolved_attributes": []}},
                ]}

        recipe = {"id": "foodcom:42", "source_dataset": DATASET,
                  "raw_ingredients": ["first food", "second food"],
                  "raw_ingredient_quantities": ["100", "250"],
                  "recipe_servings": 2}
        return RecipeNutrientEnrichmentService(NutrientCatalog(), Mapper()).enrich(
            recipe, max_ingredients=20,
        )

    def test_complete_profiles_require_optimisation_without_foodcom_totals(self):
        calculation = self._enrichment(sodium=0)
        result = assess_foodcom_nutrient_evidence(calculation)
        self.assertEqual(result["nutritional_status"], "REQUIRES_OPTIMISATION")
        self.assertEqual(result["ingredient_matching_coverage"],
                         {"matched": 2, "total": 2, "fraction": 1.0})
        self.assertEqual(result["nutrient_data_availability"]["sodium_mg"]["available_ingredients"], 2)
        self.assertEqual(result["nutrient_data_availability"]["sodium_mg"]["recorded_zero_ingredients"],
                         ["first food", "second food"])
        self.assertTrue(result["can_proceed_to_quantity_optimisation"])
        self.assertEqual(calculation["ingredients"][0]["nutrient_measurements_per_100g"]["sodium_mg"]["status"],
                         "RECORDED_ZERO")
        self.assertIsNone(calculation["recipe_totals"])
        self.assertTrue(all(row["quantity_g"] is None for row in calculation["ingredients"]))

    def test_partial_mapping_is_insufficient_data(self):
        result = assess_foodcom_nutrient_evidence(self._enrichment(second_mapping=False))
        self.assertEqual(result["nutritional_status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["ingredient_matching_coverage"]["matched"], 1)
        self.assertFalse(result["can_proceed_to_quantity_optimisation"])
        self.assertEqual(result["missing_or_unresolved_ingredients"][0]["name"], "second food")

    def test_missing_nutrient_is_not_a_zero(self):
        result = assess_foodcom_nutrient_evidence(self._enrichment(missing_phosphorus=True))
        self.assertEqual(result["nutritional_status"], "INSUFFICIENT_DATA")
        self.assertEqual(result["nutrient_data_availability"]["phosphorus_mg"]["available_ingredients"], 1)
        self.assertEqual(result["nutrient_data_availability"]["phosphorus_mg"]["missing_ingredients"],
                         ["second food"])
        self.assertEqual(result["nutrient_data_availability"]["phosphorus_mg"]["recorded_zero_ingredients"], [])

    def test_recommendations_keep_retrieval_order_and_expose_assessment(self):
        class Runtime:
            def create_recipe_nutrient_enrichment(self):
                class Enricher:
                    def enrich(self, recipe, *, max_ingredients):
                        self.max_ingredients = max_ingredients
                        return NutrientEvidenceTests._enrichment()
                return Enricher()

        catalog = Catalog()
        service = object.__new__(RenAllyWebService)
        service.runtime = Runtime()
        service.kg = catalog
        service.retriever = GraphRetriever(catalog)
        ranked = service.retriever.recommend_foodcom_recipes("Suggest some chicken dishes", k=3)
        response = service._recommend_food("Suggest some chicken dishes", {
            "recommendation_query": "Suggest some chicken dishes", "requested_count": 3,
        }, {"active_budgets": {"sodium_mg": {"max": 0, "unit": "mg", "scope": "meal"}}}, [])
        self.assertEqual([item["recipe_id"] for item in response["food_recommendations"]],
                         [item["recipe_id"] for item in ranked])
        for candidate in response["food_recommendations"]:
            self.assertEqual(candidate["nutritional_status"], "REQUIRES_OPTIMISATION")
            self.assertEqual(candidate["ingredient_matching_coverage"]["matched"], 2)
            self.assertEqual(candidate["missing_or_unresolved_ingredients"], [])
            self.assertTrue(candidate["can_proceed_to_quantity_optimisation"])
            self.assertEqual(candidate["quantity_basis"], "FOODCOM_SOURCE_METADATA_ONLY")
            self.assertNotIn("recipe_totals", candidate)
            self.assertEqual(candidate["constraint_assessment"]["constraint_status"], "UNKNOWN")
        self.assertFalse(response["constraints_applied"])

    def test_verified_statuses_are_gated_off_from_foodcom_source_amounts(self):
        from tests.test_recipe_nutrient_constraint_adapter import calculation, ingredient

        rules = {"sodium": {"max": 400, "unit": "mg", "scope": "meal"}}
        verified = calculation(ingredient())
        self.assertEqual(assess_verified_nutritional_feasibility(verified, rules)["nutritional_status"],
                         "INSUFFICIENT_DATA")
        verified["quantity_source"] = "INDEPENDENTLY_VERIFIED"
        verified["serving_basis_verified"] = True
        self.assertEqual(assess_verified_nutritional_feasibility(verified, rules)["nutritional_status"],
                         "FEASIBLE")
        rules["sodium"]["max"] = 250
        self.assertEqual(assess_verified_nutritional_feasibility(verified, rules)["nutritional_status"],
                         "INFEASIBLE")
        verified["serving_basis_verified"] = False
        self.assertEqual(assess_verified_nutritional_feasibility(verified, rules)["nutritional_status"],
                         "INSUFFICIENT_DATA")

    def test_opt_in_recommendation_returns_constructed_quantities(self):
        from src.recipes.ingredient_quantity_optimiser import IngredientQuantityOptimiser

        class Graph:
            def get_usda_food_records_by_fdc_ids(self, ids):
                return [{"food": {"fdc_id": fdc_id, "role": "vegetable",
                                  "min_amount_g": 50, "max_amount_g": 150,
                                  "amount_range_basis": "test prototype"}} for fdc_id in ids]

        class Runtime:
            def create_recipe_nutrient_enrichment(self):
                class Enricher:
                    def enrich(self, recipe, *, max_ingredients):
                        calculation = NutrientEvidenceTests._enrichment()
                        for row, name in zip(calculation["ingredients"], recipe["raw_ingredients"]):
                            row["ingredient_name"] = name
                        return calculation
                return Enricher()

            def create_ingredient_quantity_optimiser(self):
                return IngredientQuantityOptimiser(Graph())

            def create_culinary_quantity_validator(self, *, model_provider=None):
                from src.recipes.culinary_quantity_validation import CulinaryQuantityValidationService
                return CulinaryQuantityValidationService(model_provider=model_provider)

        catalog = Catalog()
        service = object.__new__(RenAllyWebService)
        service.runtime = Runtime()
        service.kg = catalog
        service.retriever = GraphRetriever(catalog)
        response = service._recommend_food(
            "Can I eat fried rice?", {"recommendation_query": "Can I eat fried rice?", "requested_count": 1},
            {"active_budgets": {"protein": {"min": 7, "unit": "g", "scope": "meal"}}, "servings": 1},
            [], optimisation_request={"optimise_quantities": True},
        )
        candidate = response["food_recommendations"][0]
        self.assertEqual(candidate["nutritional_status"], "REQUIRES_OPTIMISATION")
        self.assertEqual(candidate["quantity_optimisation"]["optimisation_status"], "OPTIMISED")
        self.assertEqual(candidate["culinary_validation"]["status"], "CULINARY_UNVERIFIED")
        self.assertEqual(candidate["quantity_ranges"]["status"], "CULINARY_UNVERIFIED")
        self.assertEqual(len(candidate["quantity_ranges"]["ingredient_ranges"]), 2)
        self.assertEqual(candidate["quantity_ranges"]["recommended_ingredients"],
                         candidate["quantity_optimisation"]["ingredients"])
        self.assertEqual(len(candidate["quantity_optimisation"]["ingredients"]), 2)
        self.assertFalse(response["constraints_applied"])
        service.profiles = {}
        service.planner = RequestPlanner(catalog, service.retriever, object())
        chat = service.chat({"query": "Optimise ingredient quantities for fried rice",
                             "meal_limits": {"protein": {"min": "7"}}, "use_slm": False})
        self.assertEqual(chat["food_recommendations"][0]["quantity_optimisation"]["optimisation_status"],
                         "OPTIMISED")
        culinary_request = {"optimise_quantities": True, "culinary_rules": {
            "basis": "fixture dish rule", "essential_positions": [0, 1],
            "required_roles": ["vegetable"],
            "ingredient_ratios": [{"numerator_position": 0, "denominator_position": 1,
                                   "min": 2}],
        }}
        with_rules = service._recommend_food(
            "Can I eat fried rice?", {"recommendation_query": "Can I eat fried rice?", "requested_count": 1},
            {"active_budgets": {"protein": {"min": 7, "unit": "g", "scope": "meal"}}, "servings": 1},
            [], optimisation_request=culinary_request,
        )["food_recommendations"][0]
        self.assertEqual(with_rules["culinary_validation"]["original_status"], "CULINARY_INVALID")
        self.assertEqual(with_rules["culinary_validation"]["status"], "CULINARY_VALID")
        self.assertEqual(with_rules["culinary_validation"]["adjusted_optimisation"]["optimisation_status"],
                         "OPTIMISED")
        self.assertEqual(with_rules["quantity_ranges"]["status"], "FEASIBLE_RANGES")
        self.assertEqual(with_rules["quantity_ranges"]["culinary_status"], "CULINARY_VALID")
        self.assertEqual(with_rules["quantity_ranges"]["recommended_ingredients"],
                         with_rules["culinary_validation"]["adjusted_optimisation"]["ingredients"])


if __name__ == "__main__":
    unittest.main()

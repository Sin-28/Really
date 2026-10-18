"""Behavior checks for catalog-sized request paths."""

import unittest
from types import SimpleNamespace

from src.constraints.constraint_engine import ConstraintEngine
from src.retrieval.graph_retriever import GraphRetriever, _lucene_query
from src.web.request_planner import RequestPlanner
from src.web.service import RenAllyWebService


class CompositionRecipeTests(unittest.TestCase):
    def setUp(self):
        self.recipes = {
            "first": {"ingredients": [("rice", 100), ("fish", 50)]},
            "second": {"ingredients": [("rice", 100), ("fish", 50)]},
            "invalid": {"ingredients": [("rice", -1)]},
        }

        class Graph:
            def __init__(graph, recipes):
                graph.recipes = recipes
                graph.visited = []

            def recipe_ingredients(graph, recipe_id):
                graph.visited.append(recipe_id)
                return graph.recipes[recipe_id]["ingredients"]

        class Retriever:
            def retrieve_nodes(retriever, query, k):
                return [("second", 0.8), ("first", 0.6), ("invalid", 1.0)]

        self.service = RenAllyWebService.__new__(RenAllyWebService)
        self.service.kg = Graph(self.recipes)
        self.service.retriever = Retriever()
        self.service.planner = SimpleNamespace(MAX_RECIPE_CANDIDATES=60)

    def test_named_recipe_checks_only_retrieved_candidates(self):
        result = self.service._composition_recipe("fish and rice", "fish dish", {"fish"})
        self.assertEqual(result, "second")
        self.assertEqual(self.service.kg.visited, ["second", "first", "invalid"])

    def test_unnamed_recipe_preserves_catalog_order_on_equal_scores(self):
        self.service.retriever.retrieve_nodes = lambda query, k: [
            ("second", 0.8), ("first", 0.8), ("invalid", 1.0),
        ]
        result = self.service._composition_recipe("fish and rice", None, {"fish"})
        self.assertEqual(result, "first")
        self.assertEqual(self.service.kg.visited, ["first", "second", "invalid"])


class CompositionResolutionTests(unittest.TestCase):
    def test_missing_and_ambiguous_foods_remain_visible(self):
        class Retriever:
            def resolve_ingredient_candidates(self, query, k, dataset_scope):
                if query == "rice":
                    return [("rice_a", 1.0), ("rice_b", 1.0)]
                return []

        service = RenAllyWebService.__new__(RenAllyWebService)
        service.kg = SimpleNamespace(ingredients={
            "rice_a": {"name": "Rice A", "source": {}},
            "rice_b": {"name": "Rice B", "source": {}},
        })
        service.retriever = Retriever()
        service.profiles = {}
        service.planner = SimpleNamespace(MAX_COMPOSITION_INGREDIENTS=8)
        response = service._execute_composition_question(
            "rice and unknown", {
                "composition_ingredients": [
                    {"query": "rice", "amount_g": 40},
                    {"query": "unknown", "amount_g": 30},
                ],
                "dataset_scope": "all",
            }, {}, 1, None, False, {
                "query": "rice and unknown", "servings": 1,
                "conditions": [], "reference_guidance": [], "profile_conflicts": [],
                "active_budgets": {}, "slm_warning": None,
            },
        )
        assessment = response["composition_assessment"]
        self.assertEqual(assessment["status"], "AMBIGUOUS_INGREDIENT")
        self.assertEqual(len(assessment["ambiguous_ingredients"]), 1)
        self.assertEqual(assessment["ingredients"][1], {
            "query": "unknown", "amount_g": 30, "found": False,
        })
        self.assertIn("unknown", response["tool_trace"][-1])


class CatalogParsingTests(unittest.TestCase):
    def setUp(self):
        ingredients = {
            "rice": {"name": "White rice", "aliases": []},
            "fish": {"name": "Fish", "aliases": []},
            "shell": {"name": "Shellfish", "aliases": []},
        }
        self.planner = RequestPlanner(
            SimpleNamespace(ingredients=ingredients, recipes={}),
            retriever=object(), deterministic_generator=object(),
        )

    def test_catalog_weights_keep_label_boundaries_and_order(self):
        self.assertEqual(
            self.planner._catalog_weight_items("100 g white rice and 40 g fish"),
            [{"query": "White rice", "amount_g": 100.0},
             {"query": "Fish", "amount_g": 40.0}],
        )
        self.assertIsNone(self.planner._catalog_weight_items("100 g shellfishes"))

    def test_catalog_food_phrase_keeps_earliest_match(self):
        self.planner._recipe_catalog_candidates = lambda phrase: []
        self.assertEqual(self.planner._catalog_food_phrase("Try white rice with fish"), "white rice")

    def test_lucene_query_keeps_first_twelve_unique_words(self):
        query = " ".join(f"word{i} word{i}" for i in range(20))
        self.assertEqual(_lucene_query(query), " OR ".join(f"word{i}" for i in range(12)))

    def test_recipe_fallback_keeps_first_catalog_entries(self):
        graph = SimpleNamespace(recipes={"first": {}, "second": {}, "third": {}}, search=lambda *a, **kw: [])
        self.assertEqual(GraphRetriever(graph).retrieve_nodes("unknown", k=2),
                         [("first", 0.0), ("second", 0.0)])


class PlannerNormalizationTests(unittest.TestCase):
    def test_request_lists_keep_limits_and_catalog_id_validation(self):
        planner = RequestPlanner(
            SimpleNamespace(recipes={"known": {}}, ingredients={}), object(), object(),
        )
        plan = {
            "recipe_requests": [
                {"recipe_id": "known", "slot": "x" * 50},
                {"recipe_id": "missing", "query": "dish"},
            ],
            "ingredient_queries": [" Salmon ", "salmon", "COD"] * 4,
        }
        planner._normalize_request_lists(plan, "fallback")
        self.assertEqual(plan["ingredient_queries"], ["Salmon", "COD"])
        self.assertEqual(plan["recipe_requests"][0], {
            "recipe_id": "known", "query": "known", "slot": "x" * 40,
        })
        self.assertEqual(plan["recipe_requests"][1], {
            "recipe_id": None, "query": "dish", "slot": "",
        })

    def test_both_slm_passes_normalize_request_lists(self):
        class Generator:
            def classify_request(self, query, context):
                return {"intent": "new_recipe", "ingredient_queries": [" Salmon ", "salmon"]}

            def interpret_request(self, query, recipes, ingredients, context, classified_request):
                self.classified = classified_request
                return {"ingredient_queries": [" Cod ", "cod"],
                        "generated_composition": [{"ingredient_id": "food", "amount_g": 30}]}

        generator = Generator()
        graph = SimpleNamespace(recipes={}, ingredients={"food": {}})
        planner = RequestPlanner(graph, object(), object())
        planner._generator = lambda use_slm: (generator, None)
        catalog_queries = []
        def ingredient_catalog(query, names, **kwargs):
            catalog_queries.extend(names)
            return [{"id": "food"}]
        planner._ingredient_catalog_candidates = ingredient_catalog
        plan, _, warning, used_slm = planner.plan("make a meal", True, {})
        self.assertTrue(used_slm)
        self.assertIsNone(warning)
        self.assertEqual(catalog_queries, ["Salmon"])
        self.assertEqual(plan["ingredient_queries"], ["Cod"])
        self.assertEqual(plan["generated_composition"], [{"ingredient_id": "food", "amount_g": 30.0}])


class NutrientEvaluationTests(unittest.TestCase):
    def test_single_serving_keeps_independent_equivalent_totals(self):
        engine = ConstraintEngine({
            "rice": {"nutrients_per_100g": {"sodium_mg": 10.0}},
        })
        result = engine.evaluate([("rice", 50)], {}, servings=1)
        self.assertEqual(result["totals"], result["totals_for_servings"])
        self.assertIsNot(result["totals"], result["totals_for_servings"])
        self.assertEqual(result["totals"]["sodium_mg"], 5.0)
        self.assertEqual(
            engine.evaluate([("rice", 50)], {}, servings=2)["totals_for_servings"]["sodium_mg"],
            10.0,
        )


if __name__ == "__main__":
    unittest.main()

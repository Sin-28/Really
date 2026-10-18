"""Small deterministic graph fixtures for constraint and pipeline tests.

These fixture IDs are test-local and are never imported into Neo4j. The one
traceable FDC ID mirrors the real chicken record asserted by the original
constraint test; all other fixture records are explicitly marked as fixtures.
"""


class FixtureKnowledgeGraph:
    CHICKEN_ID = "test:foundation_chicken_breast_braised"
    RICE_ID = "test:cooked_white_rice"
    NOODLE_ID = "test:instant_noodles"
    MISSING_FISH_ID = "test:missing_tilapia_raw"

    def __init__(self):
        self.ingredients = {
            self.CHICKEN_ID: {
                "name": "Chicken, breast, braised",
                "fdc_description": "Chicken, broilers or fryers, breast, meat only, cooked, braised",
                "aliases": [], "role": "protein", "ingredient_type": "poultry",
                "nutrients_per_100g": {
                    "sodium_mg": 47.0, "potassium_mg": 355.0,
                    "phosphorus_mg": 228.0, "protein_g": 31.02,
                },
                "nutrient_units": {"sodium_mg": "mg", "potassium_mg": "mg",
                                    "phosphorus_mg": "mg", "protein_g": "g"},
                "nutrient_names": {"sodium_mg": "Sodium", "potassium_mg": "Potassium",
                                    "phosphorus_mg": "Phosphorus", "protein_g": "Protein"},
                "typical_amount_g": 80.0, "min_amount_g": 30.0, "max_amount_g": 250.0,
                "food_state": "cooked, braised",
                "source": {"name": "USDA FoodData Central, Foundation Foods (test fixture)",
                           "dataset_key": "test_fixture", "data_type": "Foundation",
                           "fdc_id": 331960},
            },
            self.RICE_ID: {
                "name": "Rice, white, long-grain, cooked",
                "fdc_description": "Rice, white, long-grain, regular, enriched, cooked",
                "aliases": [], "role": "grain", "ingredient_type": "grain",
                "nutrients_per_100g": {
                    "sodium_mg": 5.0, "potassium_mg": 35.0,
                    "phosphorus_mg": 43.0, "protein_g": 2.69,
                },
                "nutrient_units": {"sodium_mg": "mg", "potassium_mg": "mg",
                                    "phosphorus_mg": "mg", "protein_g": "g"},
                "nutrient_names": {"sodium_mg": "Sodium", "potassium_mg": "Potassium",
                                    "phosphorus_mg": "Phosphorus", "protein_g": "Protein"},
                "typical_amount_g": 120.0, "min_amount_g": 20.0, "max_amount_g": 300.0,
                "food_state": "cooked",
                "source": {"name": "USDA FoodData Central (test fixture)",
                           "dataset_key": "test_fixture", "data_type": "Foundation",
                           "fdc_id": None},
            },
            self.NOODLE_ID: {
                "name": "Instant ramen noodles, prepared",
                "fdc_description": "Noodles, instant ramen, prepared, test fixture",
                "aliases": ["instant noodle"], "role": "grain", "ingredient_type": "grain",
                "nutrients_per_100g": {"sodium_mg": 1502.55},
                "nutrient_units": {"sodium_mg": "mg"}, "nutrient_names": {"sodium_mg": "Sodium"},
                "typical_amount_g": 100.0, "min_amount_g": 10.0, "max_amount_g": 400.0,
                "food_state": "prepared",
                "source": {"name": "test fixture", "dataset_key": "test_fixture",
                           "data_type": "Fixture", "fdc_id": None},
            },
        }
        self.recipes = {
            "chicken_rice_reference": {
                "id": "chicken_rice_reference", "name": "Chicken rice",
                "aliases": [], "description": "Deterministic test recipe.",
                "ingredients": [
                    {"id": self.RICE_ID, "amount_g": 120.0},
                    {"id": self.CHICKEN_ID, "amount_g": 80.0},
                ],
                "method": ["Serve the cooked rice with braised chicken."],
                "servings": 1, "source_dataset": "test_fixture",
                "source": {"name": "deterministic test fixture"},
            },
            "instant_ramen_reference": {
                "id": "instant_ramen_reference", "name": "Instant noodle",
                "aliases": ["instant noodles", "instant ramen"],
                "description": "Deterministic sodium fixture.",
                "ingredients": [{"id": self.NOODLE_ID, "amount_g": 100.0}],
                "method": ["Prepare the noodles."], "servings": 1,
                "source_dataset": "test_fixture", "source": {"name": "deterministic test fixture"},
            },
            "fish_soup_missing_data": {
                "id": "fish_soup_missing_data", "name": "Fish soup",
                "aliases": [], "description": "Fixture recipe with a deliberately absent ingredient.",
                "ingredients": [{"id": self.MISSING_FISH_ID, "amount_g": 100.0}],
                "method": ["Cook the fish in soup."], "servings": 1,
                "source_dataset": "test_fixture", "source": {"name": "deterministic test fixture"},
            },
            "chicken_rice_soup_suggestion": {
                "id": "chicken_rice_soup_suggestion", "name": "Chicken rice soup",
                "aliases": [], "description": "Fixture recipe for suggestion-path coverage.",
                "ingredients": [
                    {"id": self.RICE_ID, "amount_g": 100.0},
                    {"id": self.CHICKEN_ID, "amount_g": 60.0},
                ],
                "method": ["Simmer the chicken and rice."], "servings": 1,
                "source_dataset": "test_fixture", "source": {"name": "deterministic test fixture"},
            },
        }

    def search(self, index_name, search_query, k=5, dataset_scope="all"):
        terms = set(search_query.casefold().split()) - {"or"}
        if index_name == "renally_recipe_search":
            rows = []
            for recipe in self.recipes.values():
                words = set((recipe["name"] + " " + " ".join(recipe.get("aliases", []))).casefold().split())
                overlap = len(terms & words)
                if overlap:
                    rows.append({"id": recipe["id"], "name": recipe["name"],
                                 "aliases": recipe.get("aliases", []), "score": float(overlap)})
            return sorted(rows, key=lambda row: (-row["score"], row["id"]))[:k]
        rows = []
        for food_id, food in self.ingredients.items():
            words = set((food["name"] + " " + " ".join(food.get("aliases", []))).casefold().split())
            overlap = len(terms & words)
            if overlap:
                rows.append({"id": food_id, "name": food["name"],
                             "aliases": food.get("aliases", []), "score": float(overlap)})
        return sorted(rows, key=lambda row: (-row["score"], row["id"]))[:k]

    def recipe_details(self, recipe_id):
        return self.recipes.get(recipe_id)

    def recipe_ingredients(self, recipe_id):
        return [(item["id"], item["amount_g"])
                for item in self.recipes[recipe_id].get("ingredients", [])]

    def substitution_candidates_for(self, ingredient_id):
        return []

    def graph_context(self, recipe_id):
        return {"recipe_id": recipe_id, "nodes": [], "edges": []}

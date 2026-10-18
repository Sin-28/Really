import unittest

from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy
from src.recipes.ingredient_mapping import MAPPING_VERSION, USDAIngredientMapper
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator
from src.retrieval.graph_retriever import (
    GraphRetriever, INGREDIENT_RETRIEVAL_VERSION, _normalized_tokens,
)


def _food(fdc_id, description, *, aliases=(), dataset="foundation_foods", neo4j_score=1.0):
    return {
        "name": description,
        "fdc_description": description,
        "food_state": description,
        "aliases": list(aliases),
        "source": {
            "fdc_id": fdc_id,
            "data_type": "Foundation" if dataset == "foundation_foods" else "SR Legacy",
            "dataset_key": dataset,
            "name": f"USDA FoodData Central, {dataset}",
        },
        "_neo4j_score": neo4j_score,
    }


class MemoryCatalog:
    """Deterministic full-text/cache fixture; no Neo4j or production catalog."""

    def __init__(self, ingredients):
        self.ingredients = ingredients
        self.records = {}
        self.search_calls = []

    def search(self, index_name, search_query, k=5, dataset_scope="all"):
        self.search_calls.append((index_name, search_query, dataset_scope))
        terms = set(search_query.casefold().split()) - {"or"}
        rows = []
        for food_id, food in self.ingredients.items():
            if dataset_scope != "all" and food["source"]["dataset_key"] != dataset_scope:
                continue
            words = set((food["fdc_description"] + " " + " ".join(food["aliases"])).casefold().replace(",", " ").split())
            if terms & words:
                rows.append({
                    "id": food_id,
                    "name": food["name"],
                    "aliases": food["aliases"],
                    "dataset_key": food["source"]["dataset_key"],
                    "score": food["_neo4j_score"],
                })
        return rows[:k]

    def get_ingredient_mapping_cache(self, cache_key):
        return self.records.get(cache_key)

    def save_ingredient_mapping_cache(self, record):
        old = self.records.get(record["cache_key"])
        if old and old.get("status") == "VERIFIED" and record.get("status") != "VERIFIED":
            return old
        self.records[record["cache_key"]] = {**(old or {}), **record}
        return self.records[record["cache_key"]]

    def save_ingredient_mapping_evaluation(self, cache_key, evaluation):
        record = self.records.get(cache_key)
        if record:
            record["slm_evaluation"] = dict(evaluation)
        return record


def _catalog(*descriptions):
    return MemoryCatalog({
        f"food:{index}": _food(index, description, aliases=aliases)
        for index, (description, aliases) in enumerate(descriptions, start=1)
    })


def _ingredient(name):
    return {"ingredient_name": name, "normalized_name": name.casefold()}


def _retrieve(catalog, name, *, k=10):
    mapper = USDAIngredientMapper(catalog, GraphRetriever(catalog))
    return mapper.retrieve_candidates(_ingredient(name), limit=k, persist=False)


class IngredientAwareRetrievalTests(unittest.TestCase):
    def test_plain_salt_outranks_and_filters_salted_products(self):
        catalog = _catalog(
            ("Green beans, canned, with salt added", ()),
            ("Salt, table, iodized", ()),
            ("Pretzels, hard, salted", ()),
            ("Salt, table", ()),
        )
        candidates = _retrieve(catalog, "salt")["candidates"]
        self.assertEqual({item["description"] for item in candidates}, {"Salt, table, iodized", "Salt, table"})

    def test_generic_dairy_butter_excludes_nut_and_seed_butters(self):
        catalog = _catalog(
            ("Butter, salted", ()),
            ("Butter, unsalted", ()),
            ("Butter oil, anhydrous", ()),
            ("Butter, clarified butter (ghee)", ()),
            ("Butter replacement, without fat, powder", ()),
            ("Peanut butter, smooth", ()),
            ("Sesame butter, creamy", ()),
            ("Almond butter, plain", ()),
        )
        candidates = _retrieve(catalog, "butter")["candidates"]
        self.assertEqual({item["description"] for item in candidates}, {"Butter, salted", "Butter, unsalted"})

    def test_butter_salt_phrases_normalize_and_explicit_unsalted_query_matches(self):
        self.assertEqual(_normalized_tokens("without salt, no salt, with salt"), [
            "unsalted", "unsalted", "salted",
        ])
        catalog = _catalog(("Butter, without salt", ()), ("Butter, salted", ()))
        candidates = _retrieve(catalog, "unsalted butter")["candidates"]
        self.assertEqual([item["description"] for item in candidates], ["Butter, without salt"])

    def test_generic_milk_prefers_dairy_and_keeps_fat_variants(self):
        catalog = _catalog(
            ("Milk, whole, 3.25% milkfat, fluid", ()),
            ("Milk, nonfat, fluid", ()),
            ("Soy milk, unsweetened", ()),
            ("Almond milk, vanilla", ()),
        )
        candidates = _retrieve(catalog, "milk")["candidates"]
        self.assertEqual({item["description"] for item in candidates}, {
            "Milk, whole, 3.25% milkfat, fluid", "Milk, nonfat, fluid",
        })

    def test_milk_fat_modifiers_are_normalized_without_matching_low_sodium(self):
        self.assertEqual(_normalized_tokens("low-fat and reduced fat milk"), [
            "lowfat", "and", "reducedfat", "milk",
        ])
        catalog = _catalog(
            ("Milk, low sodium, fluid", ()),
            ("Milk, producer, fluid, 3.7% milkfat", ()),
            ("Milk, low-fat, fluid", ()),
            ("Milk, chocolate, reduced fat, fluid", ()),
        )
        candidates = {
            item["description"]: item["retrieval_evidence"]
            for item in _retrieve(catalog, "milk")["candidates"]
        }
        self.assertIn("milk_sodium_modifier", candidates["Milk, low sodium, fluid"]["unresolved_attributes"])
        self.assertNotIn("milk_fat_percentage", candidates["Milk, low sodium, fluid"]["unresolved_attributes"])
        self.assertIn("milk_fat_percentage", candidates["Milk, producer, fluid, 3.7% milkfat"]["unresolved_attributes"])
        self.assertIn("milk_fat_percentage", candidates["Milk, low-fat, fluid"]["unresolved_attributes"])
        self.assertIn("milk_fat_percentage", candidates["Milk, chocolate, reduced fat, fluid"]["unresolved_attributes"])
        self.assertIn("milk_flavor", candidates["Milk, chocolate, reduced fat, fluid"]["unresolved_attributes"])

    def test_milk_does_not_match_yogurt_or_milk_containing_crackers(self):
        catalog = _catalog(
            ("Milk, whole, fluid", ()),
            ("Yogurt, plain, whole milk", ()),
            ("Crackers, cheese, milk", ()),
            ("Milk and cereal bar", ()),
            ("Milk shakes, thick vanilla", ()),
            ("Milk, dry, nonfat, calcium reduced", ()),
        )
        candidates = _retrieve(catalog, "milk")["candidates"]
        self.assertEqual([item["description"] for item in candidates], ["Milk, whole, fluid"])

    def test_salmon_does_not_match_salmon_oil(self):
        catalog = _catalog(
            ("Salmon, Atlantic, raw", ()),
            ("Salmon, pink, cooked", ()),
            ("Fish, salmon, sockeye, wild caught, raw", ()),
            ("Fish, salmon, king, with skin, kippered", ()),
            ("Fish, salmon, king, chinook, liver", ()),
            ("Fish oil, salmon", ()),
        )
        candidates = _retrieve(catalog, "salmon")["candidates"]
        self.assertEqual({item["description"] for item in candidates}, {
            "Salmon, Atlantic, raw", "Salmon, pink, cooked",
            "Fish, salmon, sockeye, wild caught, raw",
            "Fish, salmon, king, with skin, kippered",
        })

    def test_olive_oil_excludes_foods_packed_or_dressed_in_olive_oil(self):
        catalog = _catalog(
            ("Oil, olive, extra virgin", ()),
            ("Oil, olive, salad or cooking", ()),
            ("Oil, corn, peanut, and olive", ()),
            ("Anchovies, canned, in olive oil", ()),
            ("Salad dressing, olive oil", ()),
        )
        candidates = _retrieve(catalog, "olive oil")["candidates"]
        self.assertEqual({item["description"] for item in candidates}, {
            "Oil, olive, extra virgin", "Oil, olive, salad or cooking",
        })

    def test_raw_chicken_breast_excludes_roasted_and_unspecified_state(self):
        catalog = _catalog(
            ("Chicken, broilers or fryers, breast, meat only, raw", ()),
            ("Chicken, broilers or fryers, breast, meat only, roasted", ()),
            ("Chicken, breast, meat only", ()),
            ("Chicken breast tenders, breaded, uncooked", ()),
        )
        candidates = _retrieve(catalog, "raw chicken breast")["candidates"]
        self.assertEqual([item["description"] for item in candidates], [
            "Chicken, broilers or fryers, breast, meat only, raw",
        ])

    def test_cooked_rice_excludes_uncooked_rice(self):
        catalog = _catalog(
            ("Rice, white, long-grain, regular, enriched, cooked", ()),
            ("Rice, white, long-grain, regular, raw", ()),
            ("Rice, brown, long-grain, cooked", ()),
        )
        candidates = _retrieve(catalog, "cooked rice")["candidates"]
        self.assertEqual({item["description"] for item in candidates}, {
            "Rice, white, long-grain, regular, enriched, cooked",
            "Rice, brown, long-grain, cooked",
        })

    def test_explicit_varieties_filter_conflicting_species_and_rice_types(self):
        salmon_catalog = _catalog(
            ("Salmon, Atlantic, raw", ()),
            ("Salmon, pink, raw", ()),
            ("Salmon, raw", ()),
        )
        salmon = _retrieve(salmon_catalog, "raw Atlantic salmon")["candidates"]
        self.assertEqual([item["description"] for item in salmon], ["Salmon, Atlantic, raw"])

        rice_catalog = _catalog(
            ("Rice, brown, long-grain, regular, raw", ()),
            ("Rice, white, long-grain, regular, raw", ()),
        )
        rice = _retrieve(rice_catalog, "brown rice")["candidates"]
        self.assertEqual([item["description"] for item in rice], ["Rice, brown, long-grain, regular, raw"])

    def test_generic_salmon_and_milk_remain_unresolved_ambiguities(self):
        catalog = _catalog(
            ("Salmon, Atlantic, raw", ()),
            ("Salmon, pink, raw", ()),
            ("Milk, whole, fluid", ()),
            ("Milk, nonfat, fluid", ()),
        )
        mapper = USDAIngredientMapper(catalog, GraphRetriever(catalog))
        policy = AutomaticIngredientMappingPolicy()
        salmon = _ingredient("salmon")
        salmon_result = mapper.retrieve_candidates(salmon, persist=False)
        salmon_decision = policy.evaluate(salmon, salmon_result)
        self.assertGreaterEqual(len(salmon_result["candidates"]), 2)
        self.assertFalse(salmon_decision["eligible"])
        self.assertIsNone(salmon_decision["fdc_id"])
        self.assertIn("species", salmon_decision["unresolved_attributes"])

        milk = _ingredient("milk")
        milk_result = mapper.retrieve_candidates(milk, persist=False)
        milk_decision = policy.evaluate(milk, milk_result)
        self.assertGreaterEqual(len(milk_result["candidates"]), 2)
        self.assertFalse(milk_decision["eligible"])
        self.assertIsNone(milk_decision["fdc_id"])
        self.assertIn("milk_fat_percentage", milk_decision["unresolved_attributes"])

    def test_generic_egg_excludes_whites_yolks_and_egg_substitutes(self):
        catalog = _catalog(
            ("Eggs, Grade A, Large, egg whole", ()),
            ("Egg, white, dried", ()),
            ("Egg, yolk, dried", ()),
            ("Egg substitute, powder", ()),
            ("Potato salad with egg", ()),
            ("Egg custards, dry mix", ()),
        )
        candidates = _retrieve(catalog, "egg")["candidates"]
        self.assertEqual([item["description"] for item in candidates], [
            "Eggs, Grade A, Large, egg whole",
        ])

    def test_sugar_excludes_sugar_added_foods_and_sugar_apple_fruit(self):
        catalog = _catalog(
            ("Sugar, turbinado", ()),
            ("Peas, green, canned, sugar added", ()),
            ("Sugar-apples, (sweetsop), raw", ()),
        )
        candidates = _retrieve(catalog, "sugar")["candidates"]
        self.assertEqual([item["description"] for item in candidates], ["Sugar, turbinado"])

    def test_unstated_chicken_cut_details_block_alias_based_auto_verification(self):
        catalog = _catalog(
            ("Chicken, breast, boneless, skinless, raw", ("raw chicken breast",)),
            ("Chicken, breast, meat and skin, raw", ()),
        )
        ingredient = _ingredient("raw chicken breast")
        mapper = USDAIngredientMapper(catalog, GraphRetriever(catalog))
        mapping = mapper.retrieve_candidates(ingredient, persist=False)
        decision = AutomaticIngredientMappingPolicy().evaluate(ingredient, mapping)
        self.assertFalse(decision["eligible"])
        self.assertTrue({"bone_state", "skin_state"} & set(decision["unresolved_attributes"]))

    def test_exact_food_identity_beats_a_high_neo4j_score(self):
        catalog = _catalog(
            ("Butter, salted", ()),
            ("Butter", ()),
            ("Peanut butter, smooth", ()),
        )
        catalog.ingredients["food:1"]["_neo4j_score"] = 200.0
        catalog.ingredients["food:2"]["_neo4j_score"] = 0.01
        result = _retrieve(catalog, "butter")
        self.assertEqual(result["candidates"][0]["description"], "Butter")
        self.assertGreater(result["candidates"][0]["relevance_score"], result["candidates"][1]["relevance_score"])

    def test_curated_alias_is_reported_as_an_approved_synonym(self):
        catalog = _catalog(("Eggplant, raw", ("aubergine",)))
        candidate = _retrieve(catalog, "aubergine")["candidates"][0]
        self.assertEqual(candidate["retrieval_method"], "synonym_match")
        self.assertEqual(candidate["retrieval_evidence"]["identity_match"], "approved_synonym")

    def test_no_suitable_dairy_milk_candidate_returns_unresolved(self):
        catalog = _catalog(("Soy milk, unsweetened", ()), ("Almond milk, plain", ()))
        result = _retrieve(catalog, "milk")
        self.assertEqual(result["status"], "UNRESOLVED")
        self.assertEqual(result["candidates"], [])

    def test_candidate_order_is_independent_of_neo4j_tie_order(self):
        foods = [
            ("food:a", _food(1, "Butter, salted", neo4j_score=1.0)),
            ("food:b", _food(2, "Butter, unsalted", neo4j_score=1.0)),
            ("food:c", _food(3, "Butter", neo4j_score=1.0)),
        ]
        forward = MemoryCatalog(dict(foods))
        reversed_catalog = MemoryCatalog(dict(reversed(foods)))
        first = [item["fdc_id"] for item in _retrieve(forward, "butter")["candidates"]]
        second = [item["fdc_id"] for item in _retrieve(reversed_catalog, "butter")["candidates"]]
        self.assertEqual(first, second)

    def test_candidate_scores_keep_neo4j_and_relevance_separate(self):
        catalog = _catalog(("Salt, table", ()))
        candidate = _retrieve(catalog, "salt")["candidates"][0]
        self.assertEqual(candidate["neo4j_retrieval_score"], 1.0)
        self.assertGreater(candidate["relevance_score"], 1000)
        self.assertEqual(candidate["retrieval_score"], 0.895)
        self.assertEqual(candidate["retrieval_version"], INGREDIENT_RETRIEVAL_VERSION)

    def test_pre_reranker_human_verified_cache_record_is_reused_unchanged(self):
        catalog = _catalog(("Butter, salted", ()))
        mapper = USDAIngredientMapper(catalog, GraphRetriever(catalog))
        ingredient = _ingredient("butter")
        key = mapper.mapping_cache_key(ingredient)
        verified = {
            "cache_key": key, "normalized_name": "butter", "attributes": {},
            "dataset_scope": "all", "mapping_version": MAPPING_VERSION,
            "status": "VERIFIED", "fdc_id": 44,
            "fdc_description": "Butter, salted", "fdc_data_type": "Foundation",
            "selected_candidate": {"fdc_id": 44, "description": "Butter, salted"},
            "candidates": [{"fdc_id": 44, "description": "Butter, salted"}],
            "verification_method": "human_review", "validated_by": "reviewer-a",
        }
        catalog.records[key] = dict(verified)
        result = mapper.retrieve_candidates(ingredient)
        self.assertEqual(result["status"], "VERIFIED")
        self.assertEqual(result["fdc_id"], 44)
        self.assertEqual(result["retrieval_version"], "pre-reranker")
        self.assertEqual(catalog.records[key], verified)
        self.assertEqual(catalog.search_calls, [])

    def test_slm_recommendation_remains_separate_from_mapping_verification(self):
        catalog = _catalog(("Butter, salted", ()), ("Butter, unsalted", ()))
        ingredient = _ingredient("butter")
        mapping = USDAIngredientMapper(catalog, GraphRetriever(catalog)).retrieve_candidates(
            ingredient, persist=False,
        )
        recommended_id = mapping["candidates"][0]["fdc_id"]
        validator = SLMIngredientMappingValidator(
            evaluator=lambda payload: {
                "status": "RECOMMENDED", "recommended_fdc_id": recommended_id,
                "compatible_candidate_ids": [recommended_id], "unresolved_attributes": [],
                "reason": "Candidate appears to match.", "requires_review": True,
            },
        )
        evaluation = validator.evaluate(ingredient, mapping, persist=False)
        self.assertEqual(evaluation["status"], "RECOMMENDED")
        self.assertEqual(mapping["status"], "AMBIGUOUS")
        self.assertIsNone(mapping.get("fdc_id"))


if __name__ == "__main__":
    unittest.main()

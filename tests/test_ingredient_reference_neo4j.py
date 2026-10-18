import json
import unittest

from src.graph.neo4j_graph import Neo4jKnowledgeGraph


class IngredientReferenceNeo4jStorageTests(unittest.TestCase):
    def setUp(self):
        self.kg = Neo4jKnowledgeGraph.__new__(Neo4jKnowledgeGraph)
        self.kg._ingredient_reference_store_ready = False
        self.queries = []
        self.rows = []

        def rows(query, **parameters):
            self.queries.append((query, parameters))
            return self.rows.pop(0) if self.rows else []

        self.kg._rows = rows

    def test_reference_reads_are_read_only_and_decode_review_metadata(self):
        self.rows.append([{"record": {
            "canonical_ingredient_name": "milk",
            "reference_status": "APPROVED",
            "attributes_json": json.dumps({"species": "cow", "fat_content": "whole"}),
            "review_provenance_json": json.dumps({"source": "review packet"}),
        }}])
        rows = self.kg.list_ingredient_references("milk")
        self.assertEqual(rows[0]["attributes"]["species"], "cow")
        self.assertEqual(rows[0]["review_provenance"]["source"], "review packet")
        self.assertEqual(len(self.queries), 1)
        self.assertIn("MATCH (reference:USDAIngredientReference)", self.queries[0][0])
        self.assertNotIn("CREATE", self.queries[0][0])

    def test_reviewed_reference_writes_use_separate_additive_labels(self):
        self.rows.extend([[], [], [{"record": {
            "reference_id": "ref-key", "canonical_ingredient_name": "milk",
            "fdc_id": 171266, "reference_status": "APPROVED",
            "attributes_json": json.dumps({"species": "cow", "fat_content": "whole"}),
            "review_provenance_json": json.dumps({"source": "manual review"}),
            "reference_policy_version": 1,
        }}]])
        result = self.kg.save_ingredient_reference_decision(
            {
                "reference_id": "ref-key", "canonical_ingredient_name": "milk",
                "fdc_id": 171266, "fdc_description": "Milk, producer, fluid, 3.7% milkfat",
                "food_record_id": "fdc:171266", "dataset_key": "foundation_foods",
                "attributes": {"species": "cow", "fat_content": "whole"},
                "reference_status": "APPROVED", "allow_as_default": True,
                "reviewer_identity": "reviewer-a", "review_provenance": {"source": "manual review"},
                "reference_policy_version": 1, "updated_at": "2026-10-16T00:00:00+00:00",
            },
            {
                "event_id": "event-key", "reference_id": "ref-key", "action": "APPROVED",
                "reviewer_identity": "reviewer-a", "provenance": {"source": "manual review"},
                "reference_policy_version": 1, "created_at": "2026-10-16T00:00:00+00:00",
            },
        )
        self.assertEqual(result["reference_status"], "APPROVED")
        self.assertEqual(result["attributes"]["fat_content"], "whole")
        writes = [query for query, _ in self.queries if "CREATE CONSTRAINT" not in query]
        self.assertEqual(len(writes), 1)
        self.assertIn("USDAIngredientReference", writes[0])
        self.assertIn("USDAIngredientReferenceReview", writes[0])
        self.assertNotIn("IngredientMapping", writes[0])
        self.assertNotIn("FoodRecord", writes[0])

    def test_non_reference_review_actions_are_append_only_events(self):
        self.rows.extend([[], [], [{"record": {
            "event_id": "event-key", "action": "REQUEST_ATTRIBUTES",
            "canonical_ingredient_name": "salmon", "reviewer_identity": "reviewer-a",
            "provenance_json": json.dumps({"source": "review"}),
            "details_json": json.dumps({"requested_attributes": ["species"]}),
        }}]])
        event = self.kg.save_ingredient_reference_review_action({
            "event_id": "event-key", "action": "REQUEST_ATTRIBUTES",
            "canonical_ingredient_name": "salmon", "reviewer_identity": "reviewer-a",
            "provenance": {"source": "review"}, "details": {"requested_attributes": ["species"]},
        })
        self.assertEqual(event["provenance"]["source"], "review")
        self.assertEqual(event["details"]["requested_attributes"], ["species"])
        write = self.queries[-1][0]
        self.assertIn("CREATE (review:USDAIngredientReferenceReview", write)
        self.assertNotIn("IngredientMapping", write)

    def test_farm_raised_identity_attribute_round_trips_in_reference_storage(self):
        self.rows.extend([[], [], [{"record": {
            "reference_id": "typed-ref", "canonical_ingredient_name": "salmon",
            "fdc_id": 2684441, "reference_status": "APPROVED",
            "attributes_json": json.dumps({"farm_raised": "yes"}),
            "farm_raised": "yes",
            "review_provenance_json": json.dumps({"source": "test review"}),
            "reference_policy_version": 1,
        }}]])
        result = self.kg.save_ingredient_reference_decision(
            {
                "reference_id": "typed-ref", "canonical_ingredient_name": "salmon",
                "fdc_id": 2684441, "fdc_description": "Fish, salmon, Atlantic, farm raised, raw",
                "food_record_id": "fdc:2684441", "dataset_key": "foundation_foods",
                "attributes": {"farm_raised": "yes"}, "farm_raised": "yes",
                "reference_status": "APPROVED", "allow_as_default": False,
                "reviewer_identity": "reviewer", "review_provenance": {"source": "test review"},
                "reference_policy_version": 1, "updated_at": "2026-10-16T00:00:00+00:00",
            },
            {
                "event_id": "typed-event", "reference_id": "typed-ref", "action": "APPROVED",
                "reviewer_identity": "reviewer", "provenance": {"source": "test review"},
                "reference_policy_version": 1, "created_at": "2026-10-16T00:00:00+00:00",
            },
        )
        self.assertEqual(result["attributes"]["farm_raised"], "yes")
        params = self.queries[-1][1]["reference_properties"]
        self.assertEqual(params["farm_raised"], "yes")

    def test_rice_enrichment_identity_attribute_round_trips_in_reference_storage(self):
        self.rows.extend([[], [], [{"record": {
            "reference_id": "rice-ref", "canonical_ingredient_name": "rice",
            "fdc_id": 2512381, "reference_status": "APPROVED",
            "attributes_json": json.dumps({"enrichment_state": "unenriched"}),
            "enrichment_state": "unenriched",
            "review_provenance_json": json.dumps({"source": "test review"}),
            "reference_policy_version": 1,
        }}]])
        self.kg._ingredient_reference_store_ready = False
        self.kg.save_ingredient_reference_decision(
            {
                "reference_id": "rice-ref", "canonical_ingredient_name": "rice",
                "fdc_id": 2512381, "fdc_description": "Rice, white, long grain, unenriched, raw",
                "food_record_id": "fdc:2512381", "dataset_key": "foundation_foods",
                "attributes": {"enrichment_state": "unenriched"}, "enrichment_state": "unenriched",
                "reference_status": "APPROVED", "allow_as_default": False,
                "reviewer_identity": "reviewer", "review_provenance": {"source": "test review"},
                "reference_policy_version": 1, "updated_at": "2026-10-16T00:00:00+00:00",
            },
            {
                "event_id": "rice-event", "reference_id": "rice-ref", "action": "APPROVED",
                "reviewer_identity": "reviewer", "provenance": {"source": "test review"},
                "reference_policy_version": 1, "created_at": "2026-10-16T00:00:00+00:00",
            },
        )
        self.assertEqual(self.queries[-1][1]["reference_properties"]["enrichment_state"], "unenriched")

    def test_bounded_catalog_nutrient_read_preserves_source_identity_and_units(self):
        self.rows.append([{"food": {"fdc_id": 171287, "description": "Egg, whole, raw, fresh"},
                          "nutrients": [{"fdc_nutrient_id": "1091", "name": "Phosphorus, P",
                                         "unit": "mg", "amount": 198.0}]}])
        result = self.kg.get_usda_food_records_by_fdc_ids([171287])
        self.assertEqual(result[0]["nutrients"][0]["fdc_nutrient_id"], "1091")
        self.assertEqual(result[0]["nutrients"][0]["name"], "Phosphorus, P")
        self.assertEqual(result[0]["nutrients"][0]["unit"], "mg")
        query, params = self.queries[0]
        self.assertIn("MATCH (food:FoodRecord)", query)
        self.assertIn("fdc_nutrient_id: nutrient.fdc_nutrient_id", query)
        self.assertNotIn("CREATE", query)
        self.assertEqual(params["fdc_ids"], [171287])

    def test_catalog_nutrient_read_rejects_invalid_and_over_bound_ids(self):
        with self.assertRaisesRegex(ValueError, "positive integers"):
            self.kg.get_usda_food_records_by_fdc_ids([0])
        with self.assertRaisesRegex(ValueError, "At most 256"):
            self.kg.get_usda_food_records_by_fdc_ids(range(1, 258))


if __name__ == "__main__":
    unittest.main()

import copy
import unittest
from types import SimpleNamespace

from src.recipes.reference_review_workflow import (
    _category_match_reason,
    attributes_from_usda_description,
    apply_manifest_decision,
    build_reference_review_manifest,
    render_review_report,
    validate_approval_scope,
)


def _egg_runtime(description="Egg, whole, raw, fresh", nutrients=None):
    nutrients = nutrients if nutrients is not None else [
        {"fdc_nutrient_id": "1093", "name": "Sodium, Na", "unit": "mg", "amount": 124.0},
        {"fdc_nutrient_id": "1092", "name": "Potassium, K", "unit": "mg", "amount": 126.0},
        {"fdc_nutrient_id": "1091", "name": "Phosphorus, P", "unit": "mg", "amount": 198.0},
        {"fdc_nutrient_id": "1003", "name": "Protein", "unit": "g", "amount": 12.6},
    ]

    class Retriever:
        def __init__(self):
            self.queries = []

        def resolve_ingredient_candidates_with_evidence(self, query, *, k, dataset_scope):
            self.queries.append((query, k, dataset_scope))
            if query != "raw whole egg":
                return []
            return [{
                "id": "fdc:171287", "identity_match": "exact_identity",
                "variety_compatibility": "explicit_match",
                "preparation_state_compatibility": "explicit_match",
                "processing_state_compatibility": "query_unspecified",
                "unresolved_attributes": [],
            }]

    class KG:
        ingredients = {
            "fdc:171287": {
                "fdc_description": description,
                "name": description,
                "source": {"fdc_id": 171287, "dataset_key": "sr_legacy", "data_type": "SR Legacy"},
            },
        }

        def __init__(self):
            self.fetched = []

        def get_usda_food_records_by_fdc_ids(self, ids):
            self.fetched.append(list(ids))
            return [{
                "food": {
                    "id": "fdc:171287", "fdc_id": 171287,
                    "description": description, "data_type": "SR Legacy",
                    "dataset_key": "sr_legacy", "nutrition_basis": "per 100 g",
                    "source_url": "https://fdc.nal.usda.gov/food-details/171287/nutrients",
                },
                "nutrients": copy.deepcopy(nutrients),
            }]

        def search(self, index_name, search_query, *, k, dataset_scope):
            return []

    return SimpleNamespace(retriever=Retriever(), kg=KG())


class FakeReferences:
    policy_version = 1

    def __init__(self, *, existing=None):
        self.local_records = {"171287": (
            "fdc:171287",
            {"fdc_description": "Egg, whole, raw, fresh", "name": "Egg, whole, raw, fresh"},
        )}
        self.rows = list(existing or [])
        self.write_calls = []

    def _local_food(self, fdc_id, food_record_id=None):
        found = self.local_records.get(str(fdc_id))
        if found is None:
            raise ValueError("The selected USDA FDC ID is not present in the locally imported catalog.")
        record_id, food = found
        if food_record_id is not None and str(food_record_id) != str(record_id):
            raise ValueError("The selected food record does not match the local catalog.")
        return record_id, food, {"fdc_id": int(fdc_id), "dataset_key": "sr_legacy", "data_type": "SR Legacy"}

    def list_references(self, category, *, include_rejected=False):
        rows = [row for row in self.rows if row["canonical_ingredient_name"] == category]
        return copy.deepcopy(rows if include_rejected else [row for row in rows if row["reference_status"] == "APPROVED"])

    def approve_reference(self, category, fdc_id, **kwargs):
        return self._save(category, fdc_id, "APPROVED", kwargs)

    def reject_reference(self, category, fdc_id, **kwargs):
        return self._save(category, fdc_id, "REJECTED", kwargs)

    def _save(self, category, fdc_id, status, kwargs):
        self.write_calls.append(status)
        row = {
            "canonical_ingredient_name": category,
            "fdc_id": int(fdc_id), "food_record_id": kwargs["food_record_id"],
            "reference_status": status,
            "reference_policy_version": self.policy_version,
            "attributes": kwargs.get("attributes") or {},
            "reviewer_identity": kwargs.get("reviewer_identity"),
            "review_provenance": kwargs.get("provenance"),
            "updated_at": "2026-10-16T00:00:00+00:00",
            "allow_as_default": kwargs.get("allow_as_default", False),
        }
        self.rows.append(row)
        return copy.deepcopy(row)


def _manifest():
    return {
        "reference_policy_version": 1,
        "entries": [{
            "review_entry_id": "entry-1", "canonical_ingredient_category": "egg",
            "proposed_fdc_id": 171287, "food_record_id": "fdc:171287",
            "usda_description": "Egg, whole, raw, fresh", "dataset_key": "sr_legacy",
            "usda_data_type": "SR Legacy", "review_status": "PENDING_REVIEW",
        }],
    }


def _decision_args(**overrides):
    values = {
        "category": "egg", "fdc_id": 171287, "decision": "approve",
        "reviewer_identity": "human-reviewer", "provenance": {"source": "review session notes"},
        "attributes": {"egg_part": "whole", "species": "unspecified", "preparation_state": "raw"},
        "allow_as_default": False, "rationale": "Specific raw whole egg record.",
        "confirm_persist": False,
    }
    values.update(overrides)
    return values


class ReferenceReviewWorkflowTests(unittest.TestCase):
    def test_approval_scope_must_match_source_and_explicitly_label_unknown_required_attributes(self):
        with self.assertRaisesRegex(ValueError, "must cover every identity-critical attribute"):
            validate_approval_scope("egg", "Egg, whole, raw, fresh", {
                "egg_part": "whole", "preparation_state": "raw",
            })
        with self.assertRaisesRegex(ValueError, "does not establish"):
            validate_approval_scope("egg", "Egg, whole, raw, fresh", {
                "egg_part": "whole", "species": "chicken", "preparation_state": "raw",
            })
        with self.assertRaisesRegex(ValueError, "conflicts with the USDA-stated"):
            validate_approval_scope("egg", "Egg, whole, raw, fresh", {
                "egg_part": "white", "species": "unspecified", "preparation_state": "raw",
            })
        scope = validate_approval_scope("egg", "Egg, whole, raw, fresh", {
            "egg_part": "whole", "species": "unknown", "preparation_state": "raw",
        })
        self.assertEqual(scope["species"], "unspecified")

    def test_category_filter_excludes_prepared_foods_from_egg_and_rice_candidates(self):
        self.assertIsNotNone(_category_match_reason("egg", "Potato salad with egg"))
        self.assertIsNotNone(_category_match_reason("egg", "Egg custards, dry mix"))
        self.assertIsNone(_category_match_reason("egg", "Egg, whole, raw, fresh"))
        self.assertIsNotNone(_category_match_reason("rice", "Rice bowl with chicken"))
        self.assertIsNone(_category_match_reason("rice", "Rice, brown, long-grain, raw"))

    def test_review_attributes_preserve_explicit_human_milk_and_maple_sugar(self):
        milk = attributes_from_usda_description("milk", "Milk, human, mature, fluid")
        sugar = attributes_from_usda_description("sugar", "Sugars, maple")
        self.assertEqual(milk["species"], "human")
        self.assertEqual(sugar["sugar_type"], "maple")

    def test_review_attributes_distinguish_farmed_wild_and_enrichment_states(self):
        farmed = attributes_from_usda_description("salmon", "Fish, salmon, Atlantic, farm raised, raw")
        wild = attributes_from_usda_description("salmon", "Fish, salmon, Atlantic, wild, raw")
        unenriched = attributes_from_usda_description("rice", "Rice, white, long grain, unenriched, raw")
        enriched = attributes_from_usda_description("rice", "Rice, white, long grain, enriched, cooked")
        self.assertEqual(farmed["farm_raised"], "yes")
        self.assertEqual(wild["farm_raised"], "no")
        self.assertEqual(unenriched["enrichment_state"], "unenriched")
        self.assertEqual(enriched["enrichment_state"], "enriched")

    def test_manifest_generation_uses_multiple_bounded_searches_and_pending_status(self):
        runtime = _egg_runtime()
        manifest = build_reference_review_manifest(runtime)
        self.assertGreater(len(runtime.retriever.queries), 20)
        self.assertIn(("raw whole egg", 12, "all"), runtime.retriever.queries)
        self.assertEqual(len(manifest["entries"]), 1)
        self.assertEqual(manifest["entries"][0]["review_status"], "PENDING_REVIEW")
        self.assertFalse(manifest["persistent_writes_performed"])

    def test_manifest_preserves_source_nutrient_ids_units_and_normalized_per_100g_values(self):
        manifest = build_reference_review_manifest(_egg_runtime())
        nutrients = manifest["entries"][0]["nutrients_per_100g"]
        self.assertEqual(nutrients["phosphorus"]["fdc_nutrient_id"], "1091")
        self.assertEqual(nutrients["phosphorus"]["name"], "Phosphorus, P")
        self.assertEqual(nutrients["phosphorus"]["unit"], "mg")
        self.assertEqual(nutrients["phosphorus"]["normalized_amount_per_100g"], 198.0)
        self.assertEqual(nutrients["protein"]["normalized_unit"], "g")

    def test_manifest_reports_missing_nutrients_as_missing_not_zero(self):
        nutrients = [
            {"fdc_nutrient_id": "1093", "name": "Sodium, Na", "unit": "mg", "amount": 124.0},
            {"fdc_nutrient_id": "1092", "name": "Potassium, K", "unit": "mg", "amount": 126.0},
            {"fdc_nutrient_id": "1003", "name": "Protein", "unit": "g", "amount": 12.6},
        ]
        manifest = build_reference_review_manifest(_egg_runtime(nutrients=nutrients))
        phosphorus = manifest["entries"][0]["nutrients_per_100g"]["phosphorus"]
        self.assertEqual(phosphorus["availability"], "MISSING")
        self.assertIsNone(phosphorus["amount"])
        self.assertIsNone(phosphorus["normalized_amount_per_100g"])

    def test_manifest_never_generates_reviewer_identity_or_approval(self):
        manifest = build_reference_review_manifest(_egg_runtime())
        entry = manifest["entries"][0]
        self.assertEqual(entry["review_status"], "PENDING_REVIEW")
        self.assertIsNone(entry["reviewer_identity"])
        self.assertIsNone(entry["review_provenance"])
        self.assertIsNone(entry["reviewed_at"])

    def test_report_marks_all_rows_pending_and_explains_phosphorus_source_id(self):
        manifest = build_reference_review_manifest(_egg_runtime())
        report = render_review_report(manifest)
        self.assertIn("PENDING_REVIEW", report)
        self.assertIn("1091", report)
        self.assertIn("Phosphate is not treated as phosphorus", report)

    def test_valid_manifest_candidate_can_be_selected_for_preview(self):
        result = apply_manifest_decision(
            SimpleNamespace(references=FakeReferences()), _manifest(), **_decision_args(),
        )
        self.assertEqual(result["fdc_id"], 171287)
        self.assertFalse(result["write_performed"])

    def test_invalid_or_unlisted_fdc_id_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "exactly one entry"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), _manifest(),
                **_decision_args(fdc_id=999999),
            )

    def test_manifest_fdc_id_must_still_exist_in_the_local_catalog(self):
        manifest = _manifest()
        manifest["entries"][0]["proposed_fdc_id"] = 999999
        with self.assertRaisesRegex(ValueError, "not present in the locally imported catalog"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), manifest,
                **_decision_args(fdc_id=999999),
            )

    def test_read_only_default_does_not_call_persistent_interface(self):
        references = FakeReferences()
        result = apply_manifest_decision(
            SimpleNamespace(references=references), _manifest(), **_decision_args(),
        )
        self.assertFalse(result["write_performed"])
        self.assertEqual(references.write_calls, [])

    def test_approval_requires_explicit_persistent_confirmation(self):
        references = FakeReferences()
        manifest = _manifest()
        result = apply_manifest_decision(
            SimpleNamespace(references=references), manifest,
            **_decision_args(confirm_persist=True),
        )
        self.assertTrue(result["write_performed"])
        self.assertEqual(references.write_calls, ["APPROVED"])
        self.assertEqual(manifest["entries"][0]["review_status"], "APPROVED")
        self.assertFalse(references.rows[-1]["allow_as_default"])
        self.assertEqual(references.rows[-1]["attributes"]["species"], "unspecified")

    def test_explicit_rejection_uses_existing_reference_manager(self):
        references = FakeReferences()
        result = apply_manifest_decision(
            SimpleNamespace(references=references), _manifest(),
            **_decision_args(decision="reject", confirm_persist=True),
        )
        self.assertEqual(result["review_status"], "REJECTED")
        self.assertEqual(references.write_calls, ["REJECTED"])

    def test_missing_reviewer_identity_blocks_persistence(self):
        with self.assertRaisesRegex(ValueError, "reviewer identity"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), _manifest(),
                **_decision_args(reviewer_identity="", confirm_persist=True),
            )

    def test_missing_provenance_blocks_persistence(self):
        with self.assertRaisesRegex(ValueError, "provenance"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), _manifest(),
                **_decision_args(provenance=None, confirm_persist=True),
            )

    def test_duplicate_approval_is_blocked(self):
        existing = [{
            "canonical_ingredient_name": "egg", "fdc_id": 171287,
            "food_record_id": "fdc:171287", "reference_status": "APPROVED",
            "reference_policy_version": 1,
            "attributes": {"egg_part": "whole", "species": "unspecified", "preparation_state": "raw"},
        }]
        with self.assertRaisesRegex(ValueError, "duplicate"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences(existing=existing)), _manifest(),
                **_decision_args(confirm_persist=True),
            )

    def test_policy_version_mismatch_blocks_decision(self):
        manifest = _manifest()
        manifest["reference_policy_version"] = 999
        with self.assertRaisesRegex(ValueError, "different reference-policy"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), manifest, **_decision_args(),
            )

    def test_incompatible_manifest_candidate_cannot_be_approved(self):
        manifest = _manifest()
        manifest["entries"][0]["identity_compatibility"] = "incompatible"
        with self.assertRaisesRegex(ValueError, "marked incompatible"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), manifest,
                **_decision_args(confirm_persist=True),
            )

    def test_nonready_proposal_recommendation_cannot_be_approved(self):
        manifest = _manifest()
        manifest["entries"][0]["review_recommendation"] = "INSUFFICIENT_NUTRIENT_DATA"
        with self.assertRaisesRegex(ValueError, "READY_FOR_HUMAN_APPROVAL"):
            apply_manifest_decision(
                SimpleNamespace(references=FakeReferences()), manifest,
                **_decision_args(confirm_persist=True),
            )

    def test_explicit_query_conflicts_are_marked_incompatible_for_human_review(self):
        runtime = _egg_runtime("Chicken, breast, meat and skin, cooked")
        # Route the candidate only through the query that explicitly requests
        # raw, boneless, skinless chicken breast.
        original = runtime.retriever.resolve_ingredient_candidates_with_evidence

        def chicken_hit(query, *, k, dataset_scope):
            runtime.retriever.queries.append((query, k, dataset_scope))
            if query != "raw boneless skinless chicken breast":
                return []
            runtime.kg.ingredients = {
                "fdc:171474": {
                    "fdc_description": "Chicken, breast, meat and skin, cooked",
                    "name": "Chicken, breast, meat and skin, cooked",
                    "source": {"fdc_id": 171474, "dataset_key": "sr_legacy", "data_type": "SR Legacy"},
                },
            }
            return [{"id": "fdc:171474", "identity_match": "exact_identity", "unresolved_attributes": []}]

        runtime.retriever.resolve_ingredient_candidates_with_evidence = chicken_hit
        runtime.kg.get_usda_food_records_by_fdc_ids = lambda ids: [{
            "food": {"id": "fdc:171474", "fdc_id": 171474,
                     "description": "Chicken, breast, meat and skin, cooked",
                     "data_type": "SR Legacy", "dataset_key": "sr_legacy", "nutrition_basis": "per 100 g"},
            "nutrients": [],
        }]
        manifest = build_reference_review_manifest(runtime)
        excluded = [row for row in manifest["excluded_candidates"]
                    if row["canonical_ingredient_category"] == "chicken breast"]
        self.assertEqual(excluded[0]["status"], "EXCLUDED_INCOMPATIBLE")
        self.assertIn("preparation_state", excluded[0]["reason"])

    def test_approved_reference_decision_remains_separate_from_mapping_records(self):
        references = FakeReferences()
        runtime = SimpleNamespace(references=references)
        apply_manifest_decision(
            runtime, _manifest(), **_decision_args(confirm_persist=True),
        )
        self.assertEqual(references.write_calls, ["APPROVED"])
        self.assertFalse(hasattr(runtime, "mapping_cache"))
        self.assertEqual(references.rows[0]["reference_status"], "APPROVED")

    def test_context_incompatible_reference_is_not_created_from_mismatched_candidate(self):
        result = build_reference_review_manifest(_egg_runtime("Egg, white, cooked"))
        rows = [row for row in result["excluded_candidates"]
                if row["canonical_ingredient_category"] == "egg"]
        self.assertEqual(rows[0]["status"], "EXCLUDED_INCOMPATIBLE")


if __name__ == "__main__":
    unittest.main()

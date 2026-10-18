import copy
import unittest

from src.recipes.initial_reference_approval import (
    INITIAL_REFERENCE_SELECTION,
    build_initial_reference_proposal,
    render_initial_reference_proposal,
)


_NUTRIENT_IDS = {"sodium": "1093", "potassium": "1092", "phosphorus": "1091", "protein": "1003"}
_NUTRIENT_NAMES = {
    "sodium": "Sodium, Na", "potassium": "Potassium, K",
    "phosphorus": "Phosphorus, P", "protein": "Protein",
}
_UNITS = {"sodium": "mg", "potassium": "mg", "phosphorus": "mg", "protein": "g"}


def _entry(fdc_id, category, description, values, *, data_type="SR Legacy", dataset_key="sr_legacy"):
    nutrients = {}
    for name, amount in values.items():
        nutrients[name] = {
            "fdc_nutrient_id": _NUTRIENT_IDS[name], "name": _NUTRIENT_NAMES[name],
            "amount": amount, "unit": _UNITS[name], "normalized_amount_per_100g": amount,
            "normalized_unit": _UNITS[name], "availability": "AVAILABLE", "basis": "per 100 g",
        }
    return {
        "review_entry_id": f"entry-{fdc_id}",
        "canonical_ingredient_category": category, "proposed_fdc_id": fdc_id,
        "food_record_id": f"fdc:{fdc_id}", "usda_description": description,
        "usda_data_type": data_type, "dataset_key": dataset_key,
        "nutrition_basis": "per 100 g", "nutrients_per_100g": nutrients,
        "candidate_description_missing_attributes": [],
        "identity_compatibility": "ambiguous" if category == "olive oil" else "exact",
        "review_status": "PENDING_REVIEW",
    }


def _manifest():
    descriptions = {
        173468: ("salt", "Salt, table", {"sodium": 38800.0, "potassium": 8.0, "phosphorus": 0.0, "protein": 0.0}),
        171287: ("egg", "Egg, whole, raw, fresh", {"sodium": 142.0, "potassium": 138.0, "phosphorus": 198.0, "protein": 12.6}),
        173410: ("butter", "Butter, salted", {"sodium": 643.0, "potassium": 24.0, "phosphorus": 24.0, "protein": 0.85}),
        173430: ("butter", "Butter, without salt", {"sodium": 11.0, "potassium": 24.0, "phosphorus": 24.0, "protein": 0.85}),
        169655: ("sugar", "Sugars, granulated", {"sodium": 1.0, "potassium": 2.0, "phosphorus": 0.0, "protein": 0.0}),
        171413: ("olive oil", "Oil, olive, salad or cooking", {"sodium": 2.0, "potassium": 1.0, "phosphorus": 0.0, "protein": 0.0}),
        2646170: ("chicken breast", "Chicken, breast, boneless, skinless, raw", {"sodium": 65.8, "potassium": 330.0, "phosphorus": 215.0, "protein": 22.5}),
        2684441: ("salmon", "Fish, salmon, Atlantic, farm raised, raw", {"sodium": 49.5, "potassium": 378.0, "phosphorus": 230.0, "protein": 20.3}),
        2512381: ("rice", "Rice, white, long grain, unenriched, raw", {"sodium": 0.462, "potassium": 82.3, "phosphorus": 108.0, "protein": 7.04}),
    }
    entries = [_entry(fdc_id, *record) for fdc_id, record in descriptions.items()]
    for entry in entries:
        if entry["proposed_fdc_id"] in {2646170, 2684441, 2512381}:
            entry.update(usda_data_type="Foundation", dataset_key="foundation_foods")
        missing = {
            171287: ["species"],
            173410: ["processing_state"],
            173430: ["processing_state"],
            171413: ["processing_state"],
            2646170: ["added_solution"],
            2684441: ["processing_state"],
        }.get(entry["proposed_fdc_id"], [])
        entry["candidate_description_missing_attributes"] = missing
    entries.extend([
        _entry(173686, "salmon", "Fish, salmon, Atlantic, wild, raw", {"sodium": 44.0, "potassium": 490.0, "phosphorus": 200.0, "protein": 19.8}),
        _entry(171509, "chicken breast", "Chicken with added solution", {"sodium": 173.0, "potassium": 332.0, "phosphorus": 198.0, "protein": 20.3}),
        _entry(169757, "rice", "Rice, white, long-grain, regular, unenriched, cooked without salt", {"sodium": 2.0, "potassium": 29.0, "phosphorus": 37.0, "protein": 2.7}),
        _entry(748608, "olive oil", "Oil, olive, extra virgin", {}, data_type="Foundation", dataset_key="foundation_foods"),
        _entry(1750351, "olive oil", "Oil, olive, extra light", {}, data_type="Foundation", dataset_key="foundation_foods"),
    ])
    for entry in entries:
        for nutrient in (entry["nutrients_per_100g"] or {}).values():
            if nutrient["amount"] is None:
                nutrient["availability"] = "MISSING"
    return {"manifest_version": 1, "reference_policy_version": 1, "entries": entries}


class InitialReferenceApprovalProposalTests(unittest.TestCase):
    def test_proposal_preserves_nine_manifest_ids_and_leaves_all_decisions_pending(self):
        proposal = build_initial_reference_proposal(_manifest())
        expected = {row["fdc_id"] for row in INITIAL_REFERENCE_SELECTION}
        self.assertEqual({row["proposed_fdc_id"] for row in proposal["entries"]}, expected)
        self.assertEqual(proposal["recommendation_counts"]["READY_FOR_HUMAN_APPROVAL"], 9)
        self.assertEqual(proposal["actually_approved_count"], 0)
        self.assertFalse(proposal["persistent_database_writes"])
        self.assertEqual(proposal["reference_policy_version"], 2)
        self.assertEqual(proposal["source_manifest_reference_policy_version"], 1)
        for row in proposal["entries"]:
            self.assertEqual(row["review_entry_id"], f"entry-{row['proposed_fdc_id']}")
            self.assertEqual(row["decision"], "PENDING_HUMAN_CONFIRMATION")
            self.assertEqual(row["review_status"], "PENDING_REVIEW")
            self.assertIsNone(row["reviewer_identity"])
            self.assertIsNone(row["approval_timestamp"])
            self.assertIsNone(row["approval_provenance"])
            self.assertFalse(row["allow_as_default"])

    def test_complete_zero_values_are_available_not_missing(self):
        proposal = build_initial_reference_proposal(_manifest())
        salt = next(row for row in proposal["entries"] if row["proposed_fdc_id"] == 173468)
        self.assertEqual(salt["nutrient_validation"]["phosphorus"]["status"], "AVAILABLE")
        self.assertEqual(salt["nutrient_validation"]["phosphorus"]["amount"], 0.0)

    def test_missing_unsupported_and_invalid_basis_data_need_additional_review(self):
        for failure, expected in (
            ("missing", "MISSING_NUTRIENT"),
            ("unit", "UNSUPPORTED_UNIT"),
            ("basis", "INVALID_NUTRITION_BASIS"),
        ):
            with self.subTest(failure=failure):
                manifest = copy.deepcopy(_manifest())
                entry = next(row for row in manifest["entries"] if row["proposed_fdc_id"] == 171287)
                row = entry["nutrients_per_100g"]["protein"]
                if failure == "missing":
                    row.update(availability="MISSING", amount=None, normalized_amount_per_100g=None)
                elif failure == "unit":
                    row["unit"] = "kcal"
                else:
                    row["basis"] = "per serving"
                proposal = build_initial_reference_proposal(manifest)
                egg = next(item for item in proposal["entries"] if item["proposed_fdc_id"] == 171287)
                self.assertEqual(egg["review_recommendation"], "INSUFFICIENT_NUTRIENT_DATA")
                self.assertEqual(egg["nutrient_validation"]["protein"]["status"], expected)

    def test_wrong_candidate_description_is_not_invented_or_substituted(self):
        manifest = _manifest()
        salt = next(row for row in manifest["entries"] if row["proposed_fdc_id"] == 173468)
        salt["usda_description"] = "Salt substitute"
        with self.assertRaisesRegex(ValueError, "unexpected USDA description"):
            build_initial_reference_proposal(manifest)

    def test_rendered_report_separates_recommendations_and_compares_alternatives(self):
        report = render_initial_reference_proposal(build_initial_reference_proposal(_manifest()))
        for heading in (
            "1. Ready for human approval", "2. Requires scope refinement",
            "3. Requires additional nutrient data", "4. Not recommended",
        ):
            self.assertIn(heading, report)
        self.assertIn("PENDING_HUMAN_CONFIRMATION", report)
        self.assertIn("raw boneless skinless chicken breast", report)
        self.assertIn("farm-raised status", report)
        self.assertIn("unenriched white long-grain", report)
        self.assertIn("Foundation extra-virgin and extra-light records", report)
        self.assertIn("sodium 38800 mg", report)


if __name__ == "__main__":
    unittest.main()

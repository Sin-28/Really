#!/usr/bin/env python3
"""Build the Step 4.8A proposal from the Step 4.8 manifest and local Neo4j."""

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.recipes.initial_reference_approval import (
    COMPARISON_IDS,
    INITIAL_REFERENCE_SELECTION,
    build_initial_reference_proposal,
    render_initial_reference_proposal,
)
from src.recipes.reference_review_workflow import _nutrient_summary
from src.runtime import create_ingredient_identity_runtime


def _entry_by_fdc_id(manifest, fdc_id):
    matches = [entry for entry in manifest.get("entries", [])
               if int(entry.get("proposed_fdc_id") or 0) == int(fdc_id)]
    if len(matches) != 1:
        raise ValueError(f"The Step 4.8 manifest must contain exactly one entry for FDC ID {fdc_id}.")
    return matches[0]


def _compare_manifest_to_local(entry, row):
    food = row.get("food") or {}
    expected = {
        "food_record_id": food.get("id"),
        "usda_description": food.get("description"),
        "dataset_key": food.get("dataset_key"),
        "usda_data_type": food.get("data_type"),
        "nutrition_basis": food.get("nutrition_basis"),
    }
    for field, local_value in expected.items():
        if local_value is not None and entry.get(field) != local_value:
            raise ValueError(f"Manifest {field} does not match the local Neo4j record for FDC ID {entry['proposed_fdc_id']}.")

    local_nutrients = {name: item for name, item in _nutrient_summary(
        row.get("nutrients") or [], food.get("nutrition_basis"),
    ).items()}
    for name in ("sodium", "potassium", "phosphorus", "protein"):
        manifest_nutrient = (entry.get("nutrients_per_100g") or {}).get(name) or {}
        local_nutrient = local_nutrients[name]
        fields = ("fdc_nutrient_id", "name", "unit", "amount", "availability", "basis")
        if any(manifest_nutrient.get(field) != local_nutrient.get(field) for field in fields):
            raise ValueError(f"Manifest {name} values do not match local Neo4j for FDC ID {entry['proposed_fdc_id']}.")


def _comparison_entry(row):
    food = row.get("food") or {}
    return {
        "proposed_fdc_id": int(food["fdc_id"]),
        "food_record_id": food.get("id"),
        "usda_description": food.get("description"),
        "usda_data_type": food.get("data_type"),
        "dataset_key": food.get("dataset_key"),
        "nutrition_basis": food.get("nutrition_basis"),
        "nutrients_per_100g": _nutrient_summary(
            row.get("nutrients") or [], food.get("nutrition_basis"),
        ),
    }


def _write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def generate_proposal(manifest, runtime):
    selected_ids = {selection["fdc_id"] for selection in INITIAL_REFERENCE_SELECTION}
    comparison_ids = {fdc_id for ids in COMPARISON_IDS.values() for fdc_id in ids}
    ids = sorted(selected_ids | comparison_ids)
    records = runtime.kg.get_usda_food_records_by_fdc_ids(ids)
    by_fdc_id = {int(item["food"]["fdc_id"]): item for item in records}
    missing_ids = sorted(set(ids) - set(by_fdc_id))
    if missing_ids:
        raise ValueError(f"Local Neo4j is missing required selected/comparison FDC IDs: {missing_ids}.")

    for selection in INITIAL_REFERENCE_SELECTION:
        entry = _entry_by_fdc_id(manifest, selection["fdc_id"])
        if entry.get("canonical_ingredient_category") != selection["category"]:
            raise ValueError(f"Manifest category mismatch for selected FDC ID {selection['fdc_id']}.")
        if entry.get("usda_description") != selection["description"]:
            raise ValueError(f"Manifest description mismatch for selected FDC ID {selection['fdc_id']}.")
        _compare_manifest_to_local(entry, by_fdc_id[selection["fdc_id"]])

    manifest_ids = {int(entry.get("proposed_fdc_id") or 0) for entry in manifest.get("entries", [])}
    additional_comparisons = [
        _comparison_entry(by_fdc_id[fdc_id])
        for fdc_id in sorted(comparison_ids - manifest_ids)
    ]
    existing = runtime.kg.list_ingredient_references()
    selected_approved = [reference for reference in existing
                         if reference.get("reference_status") == "APPROVED"
                         and int(reference.get("fdc_id") or 0) in selected_ids]
    proposal = build_initial_reference_proposal(
        manifest,
        additional_comparisons=additional_comparisons,
        existing_approved_references=selected_approved,
    )
    return proposal, len(selected_approved), len(ids)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default="reports/usda_reference_review_manifest_20261016.json")
    parser.add_argument("--report", default="data/reviews/usda_initial_reference_approval_proposal.md")
    parser.add_argument("--decisions", default="data/reviews/usda_initial_reference_decisions.json")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    runtime = create_ingredient_identity_runtime(PROJECT_ROOT / "data", auto_verify_exact=False)
    try:
        proposal, existing_approved, verified_record_count = generate_proposal(manifest, runtime)
        report_path, decisions_path = Path(args.report), Path(args.decisions)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_initial_reference_proposal(proposal), encoding="utf-8")
        _write_json(decisions_path, proposal)
        print(json.dumps({
            "report": str(report_path.resolve()),
            "decisions": str(decisions_path.resolve()),
            "candidates_reviewed": len(proposal["entries"]),
            "recommendations": proposal["recommendation_counts"],
            "preexisting_approved_references_for_selected_ids": existing_approved,
            "actually_approved_by_this_run": 0,
            "persistent_database_writes": False,
            "local_records_verified": verified_record_count,
        }, indent=2))
    finally:
        runtime.close()


if __name__ == "__main__":
    main()

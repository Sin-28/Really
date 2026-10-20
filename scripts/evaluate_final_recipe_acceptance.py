"""Bounded Step 6C evaluation for controlled acceptance cases."""

import argparse
import json
import statistics
import sys
from pathlib import Path
from time import perf_counter

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.recipes.adapted_recipe_culinary import CULINARY_PROMPT_VERSION
from src.recipes.final_recipe_acceptance import FinalRecipeAcceptancePolicy


FIXTURE_PATH = PROJECT_ROOT / "data" / "culinary_functional_coverage_fixtures.json"
CONTROLLED_PATH = PROJECT_ROOT / "data" / "final_recipe_acceptance_controlled_cases.json"
LIVE_RESULT_PATH = Path("/private/tmp/step6a3_live_results.json")
HUMAN_REVIEW_PATH = PROJECT_ROOT / "data" / "culinary_human_review_dataset.json"


def _controlled_payload(case, fixture):
    recipe = fixture.get("adapted_recipe") if fixture else {
        "id": case["case_id"], "name": case["title"],
        "ingredients": [{"id": "controlled-food", "name": "controlled food", "amount_g": 100}],
    }
    recipe = recipe if isinstance(recipe, dict) else {"ingredients": []}
    source_rows = recipe.get("ingredients") or []
    if not source_rows:
        source_rows = [{"id": "controlled-food", "name": "controlled food", "amount_g": 100}]
        recipe = {**recipe, "ingredients": source_rows}
    rows = []
    for index, source in enumerate(source_rows):
        source = source if isinstance(source, dict) else {"name": str(source)}
        mapped = case["mapping_complete"] or index > 0
        rows.append({
            "position": index,
            "quantity_g": (source.get("amount_g") or 100) if case["quantity_complete"] or index > 0 else None,
            "quantity_status": "CONTROLLED_FIXTURE" if case["quantity_complete"] or index > 0 else "MISSING",
            "mapping": ({
                "status": "VERIFIED_MAPPING", "fdc_id": 910000 + index,
                "verification_method": "CONTROLLED_TEST_FIXTURE",
                "verification_provenance": "synthetic-not-human-reviewed",
            } if mapped else {"status": "UNRESOLVED", "fdc_id": None}),
            "calculation_status": "COMPLETE" if mapped and case["quantity_complete"] else "INCOMPLETE",
        })
    complete = case["mapping_complete"] and case["quantity_complete"]
    calculation = {
        "version": "controlled-final-acceptance-fixture-v1",
        "calculation_status": "COMPLETE" if complete else "INCOMPLETE",
        "servings": 1,
        "per_serving": {"sodium_mg": 100, "potassium_mg": 200,
                        "phosphorus_mg": 90, "protein_g": 8} if complete else None,
        "ingredients": rows,
    }
    validation = {
        "constraint_status": case["nutrition_status"],
        "active_constraint_count": 1,
        "evaluations": ([{"nutrient_key": "sodium_mg", "status": "FAIL",
                          "reason": "Controlled fixture asserts a numeric budget violation."}]
                        if case["nutrition_status"] == "FAIL" else []),
        "missing_evidence": (["CONTROLLED_NUTRIENT_EVIDENCE_INCOMPLETE"]
                             if case["nutrition_status"] == "UNKNOWN" else []),
    }
    coverage = []
    if case["culinary_status"] == "PASS":
        coverage.append({
            "function": "CONTROLLED_RECIPE_FUNCTION", "importance": "ESSENTIAL",
            "coverage": "COVERED",
            "supporting_ingredient_ids": [str(source_rows[0].get("id") or "controlled-food")],
            "supporting_process_steps": [],
        })
    culinary = {
        "status": case["culinary_status"], "slm_output_valid": True,
        "model_id": "CONTROLLED_FIXTURE_NO_MODEL_CALL",
        "prompt_version": CULINARY_PROMPT_VERSION,
        "deterministic_guards": {"status": "PASS", "findings": []},
        "functional_coverage_assessments": coverage,
        "missing_information": (["ADAPTED_COOKING_INSTRUCTIONS_MISSING"]
                                if case["case_id"] == "C09" else []),
        "review_reasons": (["CONTROLLED_PARTIAL_OR_UNCERTAIN_FUNCTIONAL_COVERAGE"]
                           if case["culinary_status"] == "REVIEW_REQUIRED" else []),
    }
    return recipe, {"nutritional_calculation": calculation,
                    "constraint_validation": validation}, culinary


def run_controlled_cases():
    data = json.loads(CONTROLLED_PATH.read_text(encoding="utf-8"))
    fixture_data = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixtures = {row["case_id"]: row for row in fixture_data.get("cases", [])}
    policy = FinalRecipeAcceptancePolicy()
    results = []
    for case in data["cases"]:
        recipe, nutrition, culinary = _controlled_payload(
            case, fixtures.get(case.get("culinary_fixture_id")),
        )
        started = perf_counter()
        result = policy.evaluate(
            nutrition, culinary,
            constraints={"sodium_mg": {"max": 600, "unit": "mg", "scope": "meal"}},
            adapted_recipe=recipe,
        )
        elapsed_ms = (perf_counter() - started) * 1000
        results.append({
            "case_id": case["case_id"], "title": case["title"],
            "fixture_id": case.get("culinary_fixture_id"),
            "evidence_class": "CONTROLLED_SYNTHETIC_POLICY_FIXTURE",
            "nutrition_status": result["nutritional_status"],
            "culinary_status": result["culinary_status"],
            "mapping_completeness": result["mapping_completeness"]["status"],
            "quantity_completeness": result["quantity_completeness"]["status"],
            "final_status": result["status"],
            "expected_final_status": case["expected_final_status"],
            "expectation_match": result["status"] == case["expected_final_status"],
            "missing_evidence": result["missing_evidence"],
            "rejection_reasons": result["rejection_reasons"],
            "review_reasons": result["review_reasons"],
            "acceptance_latency_ms": elapsed_ms,
            "nutrition_recalculation_test": case.get("nutrient_recalculation_test"),
            "step5c_remapping_test": case.get("step5c_remapping_test"),
        })
    status_counts = {}
    for row in results:
        status_counts[row["final_status"]] = status_counts.get(row["final_status"], 0) + 1
    latencies = [row["acceptance_latency_ms"] for row in results]
    metrics = {
        "sample_size": len(results),
        "controlled_expectation_matches": sum(row["expectation_match"] for row in results),
        "controlled_expectation_match_rate": (
            sum(row["expectation_match"] for row in results) / len(results) if results else None
        ),
        "final_status_distribution": status_counts,
        "acceptance_latency_ms": {
            "mean": statistics.mean(latencies) if latencies else None,
            "median": statistics.median(latencies) if latencies else None,
            "max": max(latencies) if latencies else None,
        },
        "note": "Expectation agreement is against developer-authored synthetic policy cases, not human accuracy.",
    }
    return {"dataset": data["dataset"], "metrics": metrics, "cases": results}


def write_human_review_dataset():
    fixtures = json.loads(FIXTURE_PATH.read_text(encoding="utf-8")).get("cases", [])
    selected_ids = ["A", "B", "C", "D", "E", "F", "G", "H", "I", "K", "L", "O", "P"]
    live_rows = []
    if LIVE_RESULT_PATH.exists():
        try:
            live_rows = json.loads(LIVE_RESULT_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            live_rows = []
    live_by_id = {row.get("case_id"): row for row in live_rows if isinstance(row, dict)}
    rows = []
    for fixture in fixtures:
        if fixture.get("case_id") not in selected_ids:
            continue
        live = live_by_id.get(fixture["case_id"], {})
        model_evaluations = live.get("ingredient_judgments") or []
        model_coverage = live.get("model_reported_functional_coverage") or []
        rows.append({
            "case_id": fixture["case_id"], "title": fixture["title"],
            "source_fixture_id": fixture["case_id"],
            "original_recipe": fixture.get("original_recipe"),
            "adapted_recipe": fixture.get("adapted_recipe"),
            "ingredient_changes": [],
            "model_judgment": {
                "run_status": live.get("status", "NOT_RUN"),
                "ingredient_judgments": model_evaluations,
                "functional_coverage": model_coverage,
                "input_tokens": live.get("input_tokens"),
                "output_tokens": live.get("output_tokens"),
                "prompt_version": CULINARY_PROMPT_VERSION if live else None,
            },
            "deterministic_findings": live.get("deterministic_findings") or [],
            "final_culinary_status": live.get("status", "NOT_RUN"),
            "human_label": "PENDING_REVIEW",
            "reviewer_notes": "",
        })
    try:
        from src.recipes.recipe_change_detection import RecipeChangeDetector
        detector = RecipeChangeDetector()
        for row in rows:
            detected = detector.compare(row["original_recipe"], row["adapted_recipe"])
            row["ingredient_changes"] = detected.get("changes", [])
    except Exception:
        # Preserve source fixtures if a detector implementation changes; never invent labels.
        for row in rows:
            row["ingredient_changes"] = []
    dataset = {
        "dataset": "RenAlly representative culinary human review set",
        "label_status": "PENDING_REVIEW; no human labels were supplied or inferred",
        "source_fixture_label_status": "developer_defined_test_expectations_not_human_validated",
        "sample_size": len(rows),
        "cases": rows,
    }
    HUMAN_REVIEW_PATH.write_text(json.dumps(dataset, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return dataset


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path,
                        default=PROJECT_ROOT / "reports" / "step_6c_evaluation_results.json")
    args = parser.parse_args()
    output = {
        "controlled": run_controlled_cases(),
        "live_qwen": {
            "source_report": "reports/step_6a3_culinary_functional_coverage.md",
            "source_results": str(LIVE_RESULT_PATH),
            "prompt_version": CULINARY_PROMPT_VERSION,
            "original_step_6a2_cases_included": ["A", "B", "C", "D"],
            "full_sample_size": 6,
            "full_sample_json_schema_valid": 2,
            "full_sample_unavailable": 4,
            "mean_inference_seconds": 88.66,
            "median_inference_seconds": 90.09,
            "note": "Reused the already completed bounded Step 6A.3 v8 run; no new model call was started.",
        },
    }
    review = write_human_review_dataset()
    output["human_review_dataset"] = {
        "path": str(HUMAN_REVIEW_PATH.relative_to(PROJECT_ROOT)),
        "sample_size": review["sample_size"],
        "human_labels_pending": review["sample_size"],
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({
        "output": str(args.output),
        "controlled_metrics": output["controlled"]["metrics"],
        "human_review_sample_size": review["sample_size"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

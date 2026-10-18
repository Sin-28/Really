"""Small reproducible benchmark; distinguishes labelled coverage from unscored cases."""
import json
from pathlib import Path

from src.constraints.profiles import resolve_reference_rules
from src.generation.grounded_generator import GroundedRecipeGenerator, check_recipe_faithfulness
from src.runtime import create_runtime

ROOT = Path(__file__).parent


def main():
    runtime = create_runtime(ROOT / "data")
    kg = runtime.kg
    retriever = runtime.retriever
    engine = runtime.engine
    generator = GroundedRecipeGenerator()
    test_cases = json.loads((ROOT / "data/evaluation_test_set.json").read_text())
    aliases = {
        recipe_id: [alias.casefold() for alias in [recipe["name"], *recipe.get("aliases", [])]]
        for recipe_id, recipe in kg.recipes.items()
    }

    supported, recall_hits, reciprocal_ranks = [], 0, []
    for case in test_cases:
        dish = str(case["Dish"]).casefold()
        expected = [recipe_id for recipe_id, names in aliases.items() if any(name in dish or dish in name for name in names)]
        if not expected:
            continue
        supported.append(case)
        ranked = [item[0] for item in retriever.retrieve_nodes(case["User Query"], k=5)]
        ranks = [ranked.index(recipe_id) + 1 for recipe_id in expected if recipe_id in ranked]
        if ranks:
            recall_hits += 1
            reciprocal_ranks.append(1 / min(ranks))
        else:
            reciprocal_ranks.append(0.0)

    candidate_generator = runtime.candidate_generator
    fixture = json.loads((ROOT / "data/evaluation_profiles.json").read_text())
    fixture_results = []
    faithfulness_checks = []
    for sample in fixture["cases"]:
        candidates = candidate_generator.generate(sample["recipe_id"])
        evaluated = [
            {**candidate, **engine.evaluate(candidate["ingredients"], sample["constraints"])}
            for candidate in candidates
        ]
        passing = [item for item in evaluated if item["passed"]]
        selected = max(passing, key=lambda item: item["compatibility"]) if passing else None
        fixture_results.append(selected is not None)
        if selected:
            text = generator.generate(
                "benchmark", selected["ingredients"], selected["totals"], kg.ingredients,
                recipe_data=kg.recipes[sample["recipe_id"]],
            )
            faithfulness_checks.append(check_recipe_faithfulness(text, selected["ingredients"], kg.ingredients)["passed"])

    profiles = json.loads((ROOT / "data/clinical_constraints.json").read_text())
    conflict_cases = [case for case in test_cases if "haemodialysis" in str(case["Disease Profile"]).casefold()
                      and "hypertension" in str(case["Disease Profile"]).casefold()]
    conflict_passes = 0
    for _case in conflict_cases:
        rules, _ = resolve_reference_rules(["haemodialysis", "hypertension"], profiles)
        renal_k_kept = any(rule["nutrient"] == "potassium_mg" and rule["type"] == "INDIVIDUALIZED" for rule in rules)
        dash_k_suppressed = not any(rule["nutrient"] == "potassium_mg" and rule["source"] == "NHLBI DASH Eating Plan" for rule in rules)
        conflict_passes += renal_k_kept and dash_k_suppressed

    supported_ratio = len(supported) / len(test_cases) if test_cases else 0
    recall_at_5 = recall_hits / len(supported) if supported else 0
    mrr = sum(reciprocal_ranks) / len(reciprocal_ranks) if reciprocal_ranks else 0
    fixture_csr = sum(fixture_results) / len(fixture_results) if fixture_results else 0
    faithfulness = sum(faithfulness_checks) / len(faithfulness_checks) if faithfulness_checks else 0
    print("RenAlly compact evaluation")
    print(f"Test-set dish coverage: {len(supported)}/{len(test_cases)} ({supported_ratio:.1%})")
    print(f"Retrieval Recall@5 on covered cases: {recall_hits}/{len(supported)} ({recall_at_5:.1%})")
    print(f"Retrieval MRR on covered cases: {mrr:.3f}")
    print(f"Constraint pass rate on synthetic engineering fixtures: {sum(fixture_results)}/{len(fixture_results)} ({fixture_csr:.1%})")
    print(f"Grounded instruction ingredient/quantity listing check: {sum(faithfulness_checks)}/{len(faithfulness_checks)} ({faithfulness:.1%})")
    if conflict_cases:
        print(f"Renal + DASH potassium conflict handling: {conflict_passes}/{len(conflict_cases)} ({conflict_passes / len(conflict_cases):.1%})")
    print("Original 60-case clinical CSR, conflict, and temporal scores: not scoreable from the workbook alone; case rows do not supply complete numeric patient limits or prior-intake values.")
    print("Synthetic constraint numbers are code-path fixtures only, not clinical targets.")


if __name__ == "__main__":
    main()

"""RenAlly local prototype. Inputs and outputs are research demonstrations only."""
import argparse
import json
from pathlib import Path

from src.constraints.profiles import resolve_reference_rules
from src.constraints.inputs import parse_budget_expression
from src.generation.grounded_generator import GroundedRecipeGenerator
from src.runtime import create_runtime

ROOT = Path(__file__).parent


def parse_limit(value, scope):
    try:
        return parse_budget_expression(value, scope)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def main():
    parser = argparse.ArgumentParser(description="RenAlly recipe adaptation research prototype")
    parser.add_argument("--query", default="Make me a lower-sodium chicken rice.")
    parser.add_argument("--condition", action="append", choices=["haemodialysis", "diabetes", "hypertension"])
    parser.add_argument("--meal-limit", action="append", default=[], metavar="NUTRIENT=VALUE",
                        help="Explicit per-serving cap, e.g. sodium=700 (mg for minerals, g otherwise)")
    parser.add_argument("--remaining", action="append", default=[], metavar="NUTRIENT=VALUE",
                        help="Explicit remaining daily budget, e.g. sodium=500; never inferred from a daily guideline")
    parser.add_argument("--servings", type=int, default=1, help="Number of servings requested")
    parser.add_argument("--use-slm", action="store_true",
                        help="Use optional local Qwen for validated cooking-step generation")
    args = parser.parse_args()
    if args.servings < 1:
        parser.error("--servings must be at least 1")

    profiles = json.loads((ROOT / "data/clinical_constraints.json").read_text())
    conditions = args.condition or []
    rules, conflicts = resolve_reference_rules(conditions, profiles)
    for conflict in conflicts:
        print("PROFILE CONFLICT:", conflict)
    print("Conditions:", ", ".join(profiles[c]["label"] for c in conditions) if conditions else "none selected")
    print("Clinical references (context only; daily values are not meal limits):")
    if not rules:
        print("- None selected. You can add a condition with --condition.")
    else:
        for item in rules:
            nutrient = item["nutrient"]
            low, high = item.get("min"), item.get("max")
            if low is not None and high is not None:
                val = f" {low:g}–{high:g} {item['unit']}"
            elif high is not None:
                val = f" {high:g} {item['unit']}"
            elif low is not None:
                val = f" {low:g} {item['unit']}"
            else:
                val = " (individualized; enter a clinician-supplied value if applicable)"
            print(f"- {nutrient}: {item['type']}{val} [{item['scope']}; {item['source']}]")

    constraints = {}
    for text in args.meal_limit:
        name, rule = parse_limit(text, "meal")
        constraints[name] = rule
    for text in args.remaining:
        name, rule = parse_limit(text, "remaining")
        constraints[name] = rule
    print("Active meal budgets:", constraints if constraints else "none (reference daily guidance is displayed but not applied per meal)")

    runtime = create_runtime(ROOT / "data")
    kg = runtime.kg
    generator = GroundedRecipeGenerator()
    if args.use_slm:
        try:
            from src.generation.qwen_generator import QwenRecipeGenerator
            generator = QwenRecipeGenerator()
        except Exception as exc:
            print(f"SLM unavailable ({exc}); using deterministic cooking steps instead.")
    pipeline = runtime.create_pipeline(generator)
    result = pipeline.run(args.query, constraints, servings=args.servings)
    if result.get("missing_data"):
        print("\n" + result["answer"])
        return
    if result.get("unsupported"):
        print("\nI couldn't confidently match that dish to the prototype catalogue, so I haven't estimated its nutrients.")
        print("Supported dishes:")
        for name in result["suggestions"]:
            print("-", name)
        print("Try naming one of these dishes in your question, or add a sourced recipe first.")
        return
    recipe = kg.recipes[result["recipe_id"]]
    print(f"\nMatched dish: {recipe['name']} ({result['recipe_id']}; score {result['retrieval_score']:.2f})")
    print(f"Serving basis: {args.servings} serving(s); listed grams are per serving.")
    print("Data note:", recipe["description"])
    print("Candidates:")
    for item in result["evaluations"]:
        foods = ", ".join(f"{kg.ingredients.get(iid, {}).get('name', iid)} {amount:g} g" for iid, amount in item["ingredients"])
        status = ("NOT_EVALUATED" if not constraints else
                  "UNVERIFIED" if item.get("not_evaluated") else
                  "PASS" if item["passed"] is True else
                  "UNVERIFIED" if item["passed"] is None else "FAIL")
        print(f"- {foods}: {status}; compatibility {item['compatibility']:.2f}")
        for reason in item["reasons"]:
            print("  -", reason)
        for note in item["not_evaluated"]:
            print("  - Not scored:", note)
    if result["selected"] is None:
        print("\nNo candidate is verified to satisfy the explicit meal budgets.")
        return
    print("\n" + result["answer"])
    totals = result["selected"]["totals_for_servings"]
    print(f"\nComputed totals for {args.servings} serving(s) (ingredient-sum estimate):")
    print(", ".join(f"{key.replace('_', ' ')} {value:.1f}" for key, value in totals.items()))
    query_lower = args.query.casefold()
    if not args.meal_limit and not args.remaining and any(token in query_lower for token in ("is it ok", "is it okay", "can i eat", "suitable for me")):
        print("\nPersonal suitability: this generic reference and your daily condition guidance aren't enough to give a personal yes/no. Check the nutrition label for your exact product and compare it with your clinician-provided meal/daily advice; the prototype has not been given a personal sodium allowance.")


if __name__ == "__main__":
    main()

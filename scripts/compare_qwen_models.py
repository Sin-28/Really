"""Compare Qwen sizes on RenAlly's request-planning and grounded-response paths.

The parent process runs one isolated worker per model so peak RSS is comparable.
Both workers use the same Neo4j catalog, evaluation rows, prompts, and decoding
settings. The benchmark is an engineering comparison, not clinical validation.
"""
import argparse
import csv
import hashlib
import json
import platform
import random
import resource
import statistics
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MODELS = ["Qwen/Qwen3-0.6B", "Qwen/Qwen3-1.7B"]
VALID_INTENTS = {
    "single_recipe", "meal_plan", "ingredient_question", "composition_question",
    "substitution_question", "catalog_question", "unsupported",
}


def _json_write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")


def _csv_write(path, rows, fields):
    with path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _expected_recipe_ids(case, recipes):
    """Use the same transparent Dish/name/alias overlap rule as evaluate.py."""
    dish = str(case.get("Dish", "")).casefold().strip()
    matches = []
    for recipe_id, recipe in recipes.items():
        labels = [recipe.get("name", recipe_id), *(recipe.get("aliases") or [])]
        if any(str(label).casefold().strip() and
               (str(label).casefold().strip() in dish or dish in str(label).casefold().strip())
               for label in labels):
            matches.append(recipe_id)
    return matches


def _schema_valid(plan):
    if not isinstance(plan, dict) or plan.get("intent") not in VALID_INTENTS:
        return False
    recipe_requests = plan.get("recipe_requests")
    ingredient_queries = plan.get("ingredient_queries")
    if not isinstance(recipe_requests, list) or not isinstance(ingredient_queries, list):
        return False
    for request in recipe_requests:
        if not isinstance(request, dict) or not isinstance(request.get("query"), str):
            return False
        recipe_id = request.get("recipe_id")
        if recipe_id is not None and not isinstance(recipe_id, str):
            return False
    try:
        count = int(plan.get("requested_count", 1))
    except (TypeError, ValueError):
        return False
    return 1 <= count <= 7 and all(isinstance(item, str) for item in ingredient_queries)


def _conditions(case):
    text = str(case.get("Disease Profile", "")).casefold()
    mapping = (
        ("haemodialysis", "haemodialysis"),
        ("diabetes", "diabetes"),
        ("hypertension", "hypertension"),
    )
    return [condition for label, condition in mapping if label in text]


def _route_prediction(plan, query, kg, retriever):
    """Mirror the web app's single-recipe lookup and return the routed node."""
    requests = plan.get("recipe_requests") if isinstance(plan, dict) else None
    first = requests[0] if isinstance(requests, list) and requests and isinstance(requests[0], dict) else {}
    recipe_id = first.get("recipe_id")
    if recipe_id not in kg.recipes:
        recipe_id = None
    search_text = recipe_id or first.get("query") or query
    if recipe_id:
        return recipe_id, 1.25, search_text
    match = retriever.retrieve_recipe(search_text)
    return (match[0], match[1], search_text) if match else (None, 0.0, search_text)


def _response_evidence(recipe_id, kg, engine, plan_error=None):
    if plan_error == "needs_details":
        return {
            "lookup_status": "NEEDS_DETAILS",
            "message": "The plan asks about a food composition but no explicit food amounts were provided.",
            "active_limits": {},
        }
    if not recipe_id:
        return {"lookup_status": "NO_MATCHING_LOCAL_RECIPE", "active_limits": {}}
    composition = kg.recipe_ingredients(recipe_id)
    recipe = kg.recipes[recipe_id]
    return {
        "lookup_status": "FOUND",
        "recipe_id": recipe_id,
        "recipe_name": recipe.get("name", recipe_id),
        "ingredients_per_serving": [
            {"name": kg.ingredients[iid].get("name", iid), "amount_g": amount}
            for iid, amount in composition if iid in kg.ingredients
        ],
        "nutrients_per_serving": engine.nutrient_totals(composition),
        "active_limits": {},
        "constraint_status": "NOT_EVALUATED_NO_EXPLICIT_LIMITS",
    }


def _schema_and_route_metrics(rows):
    total = len(rows)
    supported = [row for row in rows if row["expected_recipe_ids"]]
    unsupported = [row for row in rows if not row["expected_recipe_ids"]]
    valid_ids = [row for row in rows for recipe_id in row["raw_recipe_ids"] if recipe_id]
    correct = [row for row in rows if row["route_correct"]]
    return {
        "cases": total,
        "schema_valid_count": sum(row["schema_valid"] for row in rows),
        "schema_valid_rate": sum(row["schema_valid"] for row in rows) / total if total else None,
        "intent_correct_count": sum(row["intent_correct"] for row in rows),
        "intent_accuracy": sum(row["intent_correct"] for row in rows) / total if total else None,
        "supported_catalog_cases": len(supported),
        "supported_recipe_hit_at_1_count": sum(row["supported_hit_at_1"] for row in supported),
        "supported_recipe_hit_at_1": (sum(row["supported_hit_at_1"] for row in supported) / len(supported)
                                       if supported else None),
        "unsupported_catalog_cases": len(unsupported),
        "unsupported_abstention_count": sum(row["unsupported_abstained"] for row in unsupported),
        "unsupported_abstention_rate": (sum(row["unsupported_abstained"] for row in unsupported) / len(unsupported)
                                        if unsupported else None),
        "overall_route_correct_count": len(correct),
        "overall_route_accuracy": len(correct) / total if total else None,
        "raw_recipe_id_count": len(valid_ids),
        "invalid_recipe_id_count": sum(
            recipe_id not in row["catalog_recipe_ids"]
            for row in rows for recipe_id in row["raw_recipe_ids"] if recipe_id
        ),
        "invalid_recipe_id_rate": (
            sum(recipe_id not in row["catalog_recipe_ids"]
                for row in rows for recipe_id in row["raw_recipe_ids"] if recipe_id) / len(valid_ids)
            if valid_ids else 0.0
        ),
    }


def _percentile(values, pct):
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def _peak_rss_mb():
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value / (1024 * 1024) if platform.system() == "Darwin" else value / 1024


def _constraint_fixture_rate(runtime):
    fixture = json.loads((ROOT / "data/evaluation_profiles.json").read_text(encoding="utf-8"))
    outcomes = []
    for sample in fixture["cases"]:
        candidates = runtime.candidate_generator.generate(sample["recipe_id"])
        evaluations = [runtime.engine.evaluate(candidate["ingredients"], sample["constraints"])
                       for candidate in candidates]
        outcomes.append(any(result.get("passed") is True for result in evaluations))
    return {
        "passed": sum(outcomes), "total": len(outcomes),
        "rate": sum(outcomes) / len(outcomes) if outcomes else None,
        "scope": "shared deterministic engine; synthetic engineering fixtures, not clinical targets",
    }


def _worker(model_name, output_path, max_cases, warmup_calls, method_cases, review_cases, review_seed):
    from src.generation.grounded_generator import check_method_faithfulness
    from src.generation.qwen_generator import QwenRecipeGenerator
    from src.runtime import create_runtime

    runtime = create_runtime(ROOT / "data")
    try:
        test_cases = json.loads((ROOT / "data/evaluation_test_set.json").read_text(encoding="utf-8"))
        if max_cases:
            test_cases = _review_sample(test_cases, min(max_cases, len(test_cases)), seed=review_seed)
        response_case_ids = {
            case.get("Case ID")
            for case in _review_sample(test_cases, min(review_cases, len(test_cases)), seed=review_seed)
        }
        recipes = runtime.kg.recipes
        ingredient_catalog = [
            {"id": iid, "name": data.get("name", iid), "aliases": data.get("aliases", [])}
            for iid, data in runtime.kg.ingredients.items()
        ]
        recipe_catalog = [
            {"id": rid, "name": data.get("name", rid), "aliases": data.get("aliases", []),
             "nutrition_complete": data.get("nutrition_complete", True)}
            for rid, data in recipes.items()
        ]

        print(f"[{model_name}] Loading model; the first run may download weights.", flush=True)
        load_started = time.perf_counter()
        generator = QwenRecipeGenerator(model_name=model_name)
        load_seconds = time.perf_counter() - load_started
        # Run unscored warm-up calls for consistent latency measurements.
        for case in test_cases[:max(0, warmup_calls)]:
            context = {"conditions": _conditions(case), "explicit_limits": {}, "servings": 1}
            try:
                generator.interpret_request(case["User Query"], recipe_catalog, ingredient_catalog, context)
            except Exception:
                # A malformed warm-up still exercises model generation; it is
                # intentionally excluded from the scored rows.
                pass

        case_rows, raw_rows, plan_times, response_times = [], [], [], []
        for index, case in enumerate(test_cases, start=1):
            print(f"[{model_name}] Planning and responding {index}/{len(test_cases)}", flush=True)
            query = str(case["User Query"])
            context = {"conditions": _conditions(case), "explicit_limits": {}, "servings": 1}
            plan_started = time.perf_counter()
            try:
                raw_plan = generator.interpret_request(query, recipe_catalog, ingredient_catalog, context)
                planning_error = None
            except Exception as exc:
                raw_plan = {}
                planning_error = f"{type(exc).__name__}: {exc}"
            plan_ms = (time.perf_counter() - plan_started) * 1000
            plan_times.append(plan_ms)
            plan = raw_plan if isinstance(raw_plan, dict) else {}
            expected = _expected_recipe_ids(case, recipes)
            raw_requests = plan.get("recipe_requests") if isinstance(plan.get("recipe_requests"), list) else []
            raw_ids = [str(item.get("recipe_id")) for item in raw_requests
                       if isinstance(item, dict) and item.get("recipe_id") is not None]
            candidate_prediction, retrieval_score, routed_query = _route_prediction(
                plan, query, runtime.kg, runtime.retriever,
            )
            intent = plan.get("intent")
            intent_correct = intent == "single_recipe"
            schema_valid = _schema_valid(plan)
            supported_hit = bool(expected and candidate_prediction in expected)
            unsupported_abstained = bool(not expected and candidate_prediction is None and intent_correct)
            route_correct = (supported_hit and intent_correct) if expected else unsupported_abstained
            effective_prediction = candidate_prediction if intent_correct else None
            plan_error_kind = "needs_details" if intent == "composition_question" else None
            evidence = _response_evidence(
                effective_prediction, runtime.kg, runtime.engine, plan_error=plan_error_kind,
            )
            response_evaluated = case.get("Case ID") in response_case_ids
            response_ms = None
            response_error = None
            response = ""
            if response_evaluated:
                response_started = time.perf_counter()
                try:
                    response = generator.respond_with_evidence(query, plan, evidence)
                except Exception as exc:
                    response_error = f"{type(exc).__name__}: {exc}"
                response_ms = (time.perf_counter() - response_started) * 1000
                response_times.append(response_ms)
            case_rows.append({
                "case_id": case.get("Case ID"), "dish": case.get("Dish"),
                "difficulty": case.get("Difficulty"), "disease_profile": case.get("Disease Profile"),
                "expected_intent": "single_recipe", "predicted_intent": intent,
                "schema_valid": schema_valid, "intent_correct": intent_correct,
                "expected_recipe_ids": expected, "predicted_recipe_id": candidate_prediction,
                "effective_recipe_id": effective_prediction,
                "retrieval_score": retrieval_score, "routed_query": routed_query,
                "supported_hit_at_1": supported_hit,
                "unsupported_abstained": unsupported_abstained,
                "route_correct": route_correct,
                "planning_latency_ms": round(plan_ms, 2),
                "response_evaluated": response_evaluated,
                "response_latency_ms": round(response_ms, 2) if response_ms is not None else None,
                "response": response, "response_error": response_error,
                "planning_error": planning_error,
            })
            raw_rows.append({
                "case_id": case.get("Case ID"), "query": query,
                "raw_plan": plan, "evidence": evidence,
                "response": response, "response_error": response_error,
                "planning_error": planning_error,
                "raw_recipe_ids": raw_ids,
            })

        method_rows = []
        recipe_candidates = []
        for recipe_id, recipe in sorted(recipes.items()):
            composition = runtime.kg.recipe_ingredients(recipe_id)
            if not composition or any(iid not in runtime.kg.ingredients or
                                      not isinstance(amount, (int, float)) or amount <= 0
                                      for iid, amount in composition):
                continue
            recipe_candidates.append((recipe_id, recipe, composition))
        for index, (recipe_id, recipe, composition) in enumerate(recipe_candidates[:method_cases], start=1):
            print(f"[{model_name}] Checking grounded method {index}/{min(method_cases, len(recipe_candidates))}", flush=True)
            method_started = time.perf_counter()
            try:
                method = generator.generate_method(
                    f"Prepare {recipe.get('name', recipe_id)}.", composition, runtime.kg.ingredients,
                    recipe.get("name", recipe_id), base_method=recipe.get("method") or [],
                )
                check = check_method_faithfulness(method, composition, runtime.kg.ingredients)
                error = None
            except Exception as exc:
                method, check = "", {"passed": False, "issues": [str(exc)]}
                error = f"{type(exc).__name__}: {exc}"
            method_ms = (time.perf_counter() - method_started) * 1000
            method_rows.append({
                "recipe_id": recipe_id, "recipe_name": recipe.get("name", recipe_id),
                "passed": bool(check.get("passed")), "issues": check.get("issues", []),
                "latency_ms": round(method_ms, 2), "method": method, "error": error,
            })

        import torch
        import transformers
        try:
            import huggingface_hub
            hub_version = huggingface_hub.__version__
        except Exception:
            hub_version = None
        parameters = sum(parameter.numel() for parameter in generator.model.parameters())
        catalog_fingerprint = hashlib.sha256(
            json.dumps({"recipes": recipe_catalog, "ingredients": ingredient_catalog},
                       sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()
        source_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        result = {
            "model": model_name,
            "model_revision": getattr(generator.model.config, "_commit_hash", None),
            "parameter_count": parameters,
            "device": generator.device,
            "dtype": str(generator.model.dtype),
            "model_load_seconds": round(load_seconds, 3),
            "peak_rss_mb": round(_peak_rss_mb(), 1),
            "planning_latency_ms": {
                "mean": statistics.mean(plan_times) if plan_times else None,
                "median": statistics.median(plan_times) if plan_times else None,
                "p95": _percentile(plan_times, 0.95),
            },
            "response_latency_ms": {
                "mean": statistics.mean(response_times) if response_times else None,
                "median": statistics.median(response_times) if response_times else None,
                "p95": _percentile(response_times, 0.95),
            },
            "planning_metrics": _schema_and_route_metrics([
                {**row,
                 "raw_recipe_ids": raw["raw_recipe_ids"],
                 "expected_recipe_ids": row["expected_recipe_ids"],
                 "catalog_recipe_ids": set(recipes)}
                for row, raw in zip(case_rows, raw_rows)
            ]),
            "method_faithfulness": {
                "passed": sum(row["passed"] for row in method_rows),
                "total": len(method_rows),
                "rate": sum(row["passed"] for row in method_rows) / len(method_rows) if method_rows else None,
            },
            "constraint_fixture": _constraint_fixture_rate(runtime),
            "runtime": {
                "python": platform.python_version(), "torch": torch.__version__,
                "transformers": transformers.__version__, "huggingface_hub": hub_version,
                "platform": platform.platform(), "catalog_sha256": catalog_fingerprint,
                "benchmark_sha256": source_hash,
                "test_cases": len(test_cases), "warmup_calls": warmup_calls,
                "response_review_cases": sum(row["response_evaluated"] for row in case_rows),
                "method_cases": len(method_rows),
                "decoding": {"do_sample": False, "max_new_tokens_plan": 420,
                             "max_new_tokens_response": 220, "max_new_tokens_method": 180},
                "clinical_numeric_limits_passed_to_planner": False,
            },
            "cases": case_rows,
            "raw_outputs": raw_rows,
            "methods": method_rows,
        }
        _json_write(output_path, result)
        print(f"[{model_name}] Wrote {output_path}", flush=True)
    finally:
        runtime.close()


def _review_sample(all_cases, limit=20, seed=41):
    """Deterministically choose cases round-robin across difficulty/profile strata."""
    buckets = {}
    for case in all_cases:
        key = (str(case.get("Difficulty", case.get("difficulty", ""))),
               str(case.get("Disease Profile", case.get("disease_profile", ""))))
        buckets.setdefault(key, []).append(case)
    rng = random.Random(seed)
    for values in buckets.values():
        rng.shuffle(values)
    keys = sorted(buckets)
    selected = []
    while len(selected) < min(limit, len(all_cases)):
        progressed = False
        for key in keys:
            if buckets[key] and len(selected) < min(limit, len(all_cases)):
                selected.append(buckets[key].pop())
                progressed = True
        if not progressed:
            break
    return selected


def _markdown_report(run_dir, outcomes, errors, rating_rows):
    models = [item for item in outcomes if item]
    names = [item["model"] for item in models]
    sample_size = models[0]["planning_metrics"]["cases"] if models else 0
    lines = [
        "# Qwen model comparison for RenAlly", "",
        f"Run directory: `{run_dir}`", "",
        "## Method", "",
        "Both models were run sequentially in isolated processes against the same Neo4j catalog and all selected evaluation rows. "
        "The application prompt, greedy decoding, context fields, and evidence rules were held constant. Plans are scored over all selected rows; evidence-based answers are generated only for the stratified human-review sample to reduce runtime. The 60-case set describes expected behavior but does not contain complete gold model responses or patient-specific numeric limits.", "",
        "All evaluation queries ask for one named dish, so `single_recipe` is the expected intent. A dish is considered supported when its `Dish` text overlaps a recipe name or alias in the Neo4j catalog using the same substring rule as `evaluate.py`. For supported cases, route success means the model-assisted app lookup returns one of those recipe IDs. For unsupported cases, success means the lookup abstains.", "",
        "The response-quality sheet is blinded (`blind_response_review.csv`). Rate only the sampled cases and then unblind with `blind_response_key.csv`. Automated method faithfulness checks for catalog foods or amounts outside the validated composition; it does not judge culinary quality.", "",
    ]
    if sample_size and sample_size < 60:
        response_count = models[0]["runtime"].get("response_review_cases", 0)
        method_count = models[0]["runtime"].get("method_cases", 0)
        lines.extend([
            "## Pilot scope", "",
            f"This run scored {sample_size} stratified cases from the 60-case evaluation set. "
            f"Generated responses were reviewed for {response_count} cases and method faithfulness was checked on "
            f"{method_count} recipes per model. Treat the results as preliminary; use the full 60-case run and "
            "complete blinded response ratings for final report claims.", "",
        ])
    lines.extend(["## Results", ""])
    if models:
        lines.extend([
            "| Model | Parameters | Revision | Device / dtype | Load time (s) | Peak RSS (MB) |",
            "|---|---:|---|---|---:|---:|",
        ])
        for item in models:
            lines.append(
                f"| {item['model']} | {item['parameter_count']:,} | "
                f"`{item.get('model_revision') or 'not reported'}` | {item['device']} / {item['dtype']} | "
                f"{item['model_load_seconds']:.1f} | {item['peak_rss_mb']:.0f} |"
            )
        lines.append("")
    metric_names = [
        ("Schema-valid plans", "schema_valid_count", "cases"),
        ("Single-recipe intent accuracy", "intent_correct_count", "cases"),
        ("Supported recipe Hit@1", "supported_recipe_hit_at_1_count", "supported_catalog_cases"),
        ("Unsupported-dish abstention", "unsupported_abstention_count", "unsupported_catalog_cases"),
        ("Overall route accuracy", "overall_route_correct_count", "cases"),
        ("Invalid recipe IDs", "invalid_recipe_id_count", "raw_recipe_id_count"),
        ("Method faithfulness", "method_faithfulness", None),
    ]
    if models:
        lines.extend(["| Metric | " + " | ".join(names) + " |", "|---|" + "|".join(["---:"] * len(names)) + "|"])
        for label, key, denominator_key in metric_names:
            values = []
            for item in models:
                if key == "method_faithfulness":
                    metric = item[key]
                    values.append(
                        f"{metric['passed']}/{metric['total']} ({metric['rate']:.1%})"
                        if metric["total"] else "n/a"
                    )
                else:
                    metric = item["planning_metrics"]
                    denominator = metric.get(denominator_key, 0)
                    numerator = metric.get(key, 0)
                    rate_key = {
                        "schema_valid_count": "schema_valid_rate",
                        "intent_correct_count": "intent_accuracy",
                        "supported_recipe_hit_at_1_count": "supported_recipe_hit_at_1",
                        "unsupported_abstention_count": "unsupported_abstention_rate",
                        "overall_route_correct_count": "overall_route_accuracy",
                        "invalid_recipe_id_count": "invalid_recipe_id_rate",
                    }[key]
                    rate = metric.get(rate_key)
                    values.append(f"{numerator}/{denominator} ({rate:.1%})" if denominator and rate is not None
                                  else f"{numerator}/{denominator}" if denominator else "n/a")
            lines.append(f"| {label} | " + " | ".join(values) + " |")
        for stage in ("planning_latency_ms", "response_latency_ms"):
            title = "Planning latency (ms)" if stage == "planning_latency_ms" else "Grounded response latency (ms)"
            values = [f"median {item[stage]['median']:.0f}; p95 {item[stage]['p95']:.0f}" for item in models]
            lines.append(f"\n**{title}:** " + "; ".join(f"{name}: {value}" for name, value in zip(names, values)) + ".")
        lines.append("\n**Peak resident memory:** " + "; ".join(
            f"{item['model']}: {item['peak_rss_mb']:.0f} MB" for item in models
        ) + ".")
        fixture = models[0].get("constraint_fixture", {})
        if fixture:
            lines.append(
                f"\n**Shared constraint-engine fixture:** {fixture.get('passed')}/{fixture.get('total')} "
                f"({fixture.get('rate', 0):.1%}); identical by design for both models. {fixture.get('scope', '')}."
            )
        if len(models) >= 2:
            baseline, candidate = models[0], models[1]
            base_metrics = baseline["planning_metrics"]
            candidate_metrics = candidate["planning_metrics"]
            base_route = base_metrics.get("overall_route_accuracy")
            candidate_route = candidate_metrics.get("overall_route_accuracy")
            base_intent = base_metrics.get("intent_accuracy")
            candidate_intent = candidate_metrics.get("intent_accuracy")
            base_latency = baseline["planning_latency_ms"].get("median")
            candidate_latency = candidate["planning_latency_ms"].get("median")
            base_memory = baseline.get("peak_rss_mb")
            candidate_memory = candidate.get("peak_rss_mb")
            lines.extend(["", "## Observed differences", ""])
            if base_intent is not None and candidate_intent is not None:
                lines.append(
                    f"- Intent accuracy changed from {base_intent:.1%} with `{baseline['model']}` "
                    f"to {candidate_intent:.1%} with `{candidate['model']}` "
                    f"({(candidate_intent - base_intent) * 100:+.1f} percentage points)."
                )
            if base_route is not None and candidate_route is not None:
                lines.append(
                    f"- Overall route accuracy changed from {base_route:.1%} to {candidate_route:.1%} "
                    f"({(candidate_route - base_route) * 100:+.1f} percentage points)."
                )
            if base_latency and candidate_latency:
                lines.append(
                    f"- Median planning latency changed from {base_latency:.0f} ms to "
                    f"{candidate_latency:.0f} ms ({(candidate_latency / base_latency - 1) * 100:+.1f}%)."
                )
            if base_memory and candidate_memory:
                lines.append(
                    f"- Peak process memory changed from {base_memory:.0f} MB to {candidate_memory:.0f} MB "
                    f"({(candidate_memory / base_memory - 1) * 100:+.1f}%)."
                )
            lines.append(
                "- Treat these as descriptive results for this sample only. They are not a significance test; "
                "run the full test set and review the blinded answer sheet before drawing a final conclusion."
            )
    if errors:
        lines.extend(["", "## Runs that did not complete", ""])
        lines.extend(f"- `{item['model']}`: {item['error']}" for item in errors)
    lines.extend([
        "", "## Interpretation limits", "",
        "- The recipe-hit and abstention scores measure routing to this local prototype catalog, not general nutritional reasoning.",
        "- The deterministic constraint engine is shared; a larger SLM cannot improve its arithmetic or missing-data handling. The synthetic constraint fixture is an engineering check, not a clinical score.",
        "- The response ratings require human review. Model size alone does not establish clinical safety or factual accuracy.",
        "- Model revision, library versions, device, decoding settings, and catalog fingerprint are recorded in `metrics.csv` and `raw_outputs.json`.",
    ])
    (run_dir / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _parent(args):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = Path(args.output_dir) if args.output_dir else ROOT / "reports" / "model_comparison" / f"run_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    outcomes, errors = [], []
    for index, model_name in enumerate(args.models):
        output_path = run_dir / f"model_{index + 1}.json"
        print(f"Starting isolated run for {model_name}", flush=True)
        command = [sys.executable, str(Path(__file__).resolve()), "--worker-model", model_name,
                   "--worker-output", str(output_path), "--max-cases", str(args.max_cases),
                   "--warmup-calls", str(args.warmup_calls), "--method-cases", str(args.method_cases),
                   "--review-cases", str(args.review_cases), "--review-seed", str(args.review_seed)]
        completed = subprocess.run(command, cwd=ROOT, check=False)
        if completed.returncode != 0 or not output_path.exists():
            errors.append({"model": model_name, "error": f"worker exited with status {completed.returncode}"})
            continue
        try:
            outcomes.append(json.loads(output_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append({"model": model_name, "error": f"could not read worker output: {exc}"})

    if not outcomes:
        _json_write(run_dir / "run_errors.json", errors)
        _markdown_report(run_dir, outcomes, errors, [])
        print(f"No model completed. See {run_dir / 'summary.md'}", flush=True)
        return 1

    metric_rows = []
    case_rows = []
    method_rows = []
    raw_outputs = []
    for item in outcomes:
        pm = item["planning_metrics"]
        metric_rows.append({
            "model": item["model"], "revision": item.get("model_revision"),
            "parameters": item.get("parameter_count"), "device": item.get("device"),
            "dtype": item.get("dtype"), "load_seconds": item.get("model_load_seconds"),
            "peak_rss_mb": item.get("peak_rss_mb"),
            "schema_valid_rate": pm.get("schema_valid_rate"),
            "intent_accuracy": pm.get("intent_accuracy"),
            "supported_recipe_hit_at_1": pm.get("supported_recipe_hit_at_1"),
            "unsupported_abstention_rate": pm.get("unsupported_abstention_rate"),
            "overall_route_accuracy": pm.get("overall_route_accuracy"),
            "invalid_recipe_id_rate": pm.get("invalid_recipe_id_rate"),
            "method_faithfulness_rate": item["method_faithfulness"].get("rate"),
            "planning_latency_median_ms": item["planning_latency_ms"].get("median"),
            "planning_latency_p95_ms": item["planning_latency_ms"].get("p95"),
            "response_latency_median_ms": item["response_latency_ms"].get("median"),
            "response_latency_p95_ms": item["response_latency_ms"].get("p95"),
            "constraint_fixture_rate_shared": item["constraint_fixture"].get("rate"),
            "catalog_sha256": item["runtime"].get("catalog_sha256"),
            "benchmark_sha256": item["runtime"].get("benchmark_sha256"),
        })
        for row in item["cases"]:
            case_rows.append({"model": item["model"], **row})
        for row in item["methods"]:
            method_rows.append({"model": item["model"], **row})
        raw_outputs.append({key: value for key, value in item.items() if key in {
            "model", "model_revision", "runtime", "raw_outputs", "methods"
        }})

    _csv_write(run_dir / "metrics.csv", metric_rows, list(metric_rows[0]))
    _csv_write(run_dir / "case_results.csv", case_rows, list(case_rows[0]))
    _csv_write(run_dir / "method_results.csv", method_rows,
               list(method_rows[0]) if method_rows else ["model", "recipe_id", "passed", "method"])
    _json_write(run_dir / "raw_outputs.json", raw_outputs)

    if len(outcomes) >= 2:
        by_model = {item["model"]: {row["case_id"]: row for row in item["cases"]} for item in outcomes}
        review_cases = _review_sample(outcomes[0]["cases"], args.review_cases, seed=args.review_seed)
        labels_by_model = {item["model"]: ("A" if index % 2 == 0 else "B")
                           for index, item in enumerate(outcomes[:2])}
        review_rows, key_rows = [], []
        for case in review_cases:
            case_id = case["case_id"]
            first, second = outcomes[0]["model"], outcomes[1]["model"]
            order = [first, second] if random.Random(f"{args.review_seed}:{case_id}").random() < 0.5 else [second, first]
            label_for = {order[0]: "A", order[1]: "B"}
            for model in order:
                review_rows.append({
                    "case_id": case_id, "dish": case["dish"], "difficulty": case["difficulty"],
                    "disease_profile": case["disease_profile"], "blind_model": label_for[model],
                    "response": by_model[model][case_id]["response"],
                    "relevance_1_to_5": "", "evidence_grounding_1_to_5": "",
                    "constraint_safety_1_to_5": "", "clarity_1_to_5": "",
                    "material_error_yes_no": "", "review_notes": "",
                })
                key_rows.append({"case_id": case_id, "blind_model": label_for[model], "model": model})
        _csv_write(run_dir / "blind_response_review.csv", review_rows, list(review_rows[0]))
        _csv_write(run_dir / "blind_response_key.csv", key_rows, list(key_rows[0]))
    _markdown_report(run_dir, outcomes, errors, [])
    if errors:
        _json_write(run_dir / "run_errors.json", errors)
    print(f"Comparison report written to {run_dir / 'summary.md'}", flush=True)
    return 0 if not errors else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--models", nargs="+", default=DEFAULT_MODELS, help="Model repository IDs to compare")
    parser.add_argument("--max-cases", type=int, default=0, help="Optional stratified sample size; 0 uses the full test set")
    parser.add_argument("--warmup-calls", type=int, default=1, help="Unscored request-planning warm-ups per model")
    parser.add_argument("--method-cases", type=int, default=10, help="Number of local recipe methods to ground-check")
    parser.add_argument("--review-cases", type=int, default=20, help="Stratified sample for blinded human response ratings")
    parser.add_argument("--review-seed", type=int, default=41)
    parser.add_argument("--output-dir", help="Report folder (default: reports/model_comparison/run_<timestamp>)")
    parser.add_argument("--worker-model", help=argparse.SUPPRESS)
    parser.add_argument("--worker-output", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.worker_model:
        if not args.worker_output:
            parser.error("--worker-output is required for a worker run")
        _worker(args.worker_model, Path(args.worker_output), args.max_cases,
                args.warmup_calls, args.method_cases, args.review_cases, args.review_seed)
        return 0
    if args.max_cases < 0 or args.method_cases < 0 or args.review_cases < 0 or args.warmup_calls < 0:
        parser.error("case counts must be zero or positive")
    return _parent(args)


if __name__ == "__main__":
    raise SystemExit(main())

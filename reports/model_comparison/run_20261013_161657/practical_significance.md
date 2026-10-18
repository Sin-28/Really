# Full evaluation and practical significance

Run: `run_20261013_161657`  
Models: Qwen3-0.6B and Qwen3-1.7B, local MPS, greedy decoding  
Scope: all 60 evaluation requests per model; 20 answer-generation cases per model; 10 generated-method checks per model.

Both runs used the same benchmark and Neo4j catalog fingerprints. This compares the SLM-enabled request-planning and answer-writing path. It does not compare the app's deterministic default parser against an SLM.

## Results

| Measure | Qwen3-0.6B | Qwen3-1.7B | Change |
|---|---:|---:|---:|
| Schema-valid plans | 58/60 (96.7%) | 57/60 (95.0%) | −1.7 pp |
| Correct `single_recipe` intent | 0/60 (0.0%) | 57/60 (95.0%) | +95.0 pp |
| Recipe Hit@1, supported cases only | 10/20 (50.0%) | 18/20 (90.0%) | +40.0 pp |
| Abstention, unsupported cases | 0/40 (0.0%) | 32/40 (80.0%) | +80.0 pp |
| Overall route accuracy | 0/60 (0.0%) | 50/60 (83.3%) | +83.3 pp |
| Generated-method ingredient/quantity faithfulness | 6/10 (60.0%) | 4/10 (40.0%) | −20.0 pp |
| Planning latency, median | 39.5 s | 60.6 s | +53.4% |
| Planning latency, p95 | 44.8 s | 75.0 s | +67.4% |
| Answer-generation latency, median | 1.01 s | 3.81 s | 3.8× |
| Peak process RSS | 1.05 GB | 1.83 GB | +73.6% |

The deterministic constraint-engine fixture scored 2/4 (50%) for both models. That is a shared engineering fixture, not an SLM result or a clinical pass rate. No invalid recipe ID was directly proposed in either run.

### 1.7B route accuracy by case group

| Group | Correct routes |
|---|---:|
| Easy | 15/15 |
| Medium | 14/20 |
| Hard | 21/25 |
| Haemodialysis | 11/15 |
| Diabetes | 13/15 |
| Hypertension / DASH | 12/15 |
| Haemodialysis + hypertension | 5/5 |
| Haemodialysis + diabetes | 5/5 |
| Diabetes + hypertension | 3/4 |
| All three profiles | 1/1 |

The combination-profile groups are very small; 5/5 or 1/1 should not be read as reliable evidence of general performance. The 0.6B model routed 0/60 correctly in every group because it classified every request as a composition question, although the questions did not provide weighted ingredients.

## What the numbers mean

The 1.7B model is materially better at interpreting this test set's one-dish requests. It takes about 21 seconds longer at the median planning step, uses about 774 MB more process memory, and its generated-method faithfulness is lower in the 10 checked recipes. The larger model is a better planner in this benchmark, but it is not a better verified recipe writer based on these results.

The retrieval baseline is strong when a matching recipe exists: the deterministic graph retriever achieved Recall@5 of 20/20 and MRR 1.000 on those covered cases. Coverage is the larger limitation: only 20 of the 60 test dishes match a stored recipe name or alias. The prototype has 18 stored recipe compositions. Forty cases have no matching recipe in that catalog.

The overall 1.7B route score treats a correct abstention on an unsupported menu as success. That is appropriate for the current catalog-only route. It is not equivalent to fulfilling the product goal of generating a new recipe for an unlisted dish. The current service can create a cooking method from a composition when the user supplies explicit ingredient weights; it does not generally construct a complete new recipe for a broad request such as “make a lower-sodium nasi lemak.” Thus 80% unsupported-menu abstention is safe routing behavior but still means the requested dish was not provided.

## Output evidence screen

I checked the sampled generated answers against the evidence passed to the answer model. This is an evidence-consistency screen, not a clinical review or the still-pending blinded human ratings.

- In 19 of 20 sampled 0.6B answers, the answer says a limit failed even though the executed evidence had no active numeric limit. Most of those requests were also misrouted to a composition path that asked for ingredient weights.
- In 5 of the 10 sampled 1.7B answers with a found recipe and no active limits, the model made a positive suitability, “normal/typical range,” or remaining-budget claim that the supplied evidence did not establish. Examples include claims that a recipe is suitable for dialysis or diabetes and that a meal fits a remaining allowance.
- The 1.7B model's method checker failed 6 of 10 methods due to quantities or ingredients absent from the validated composition. This checker only catches ingredient/amount mismatches; it does not judge cooking quality or clinical appropriateness.

These issues matter more for patient use than the intent-accuracy gain. The output must not turn “nutrients calculated” or “recipe found” into “medically suitable” when no patient-specific limit was evaluated.

## Baseline system evaluation

`evaluate.py` reported:

- Recipe-name coverage: 20/60 (33.3%).
- Recall@5 and MRR on covered recipes: 20/20 (100%) and 1.000.
- Synthetic constraint fixture: 2/4 (50%).
- Deterministic grounded instruction ingredient/quantity check: 2/2 (100%).
- Renal + DASH potassium conflict resolver check: 6/6 (100%).

The last two are narrow engineering checks. The conflict resolver check applies the same rule resolution over six combined-profile rows; it is not six independent clinical validations. No clinical constraint-satisfaction or temporal remaining-budget score can be computed because the test rows do not provide the promised patient-specific numeric targets or prior-meal intake values. For example, the context says a carbohydrate target or remaining daily allowance is supplied, but the dataset contains no corresponding target number, unit, and scope.

## Practical-use assessment

**Useful today as a prototype demonstration:** it can show graph-backed food/recipe retrieval, sourced nutrient values for matched Foundation Foods records, deterministic totals for explicit compositions, and explicit missing-data behavior. The 1.7B model substantially improves intent routing over 0.6B on this 60-case set.

**Not ready as personalized clinical meal advice:** the recipe catalog is small, its compositions are labeled illustrative, and recipe totals can omit cooking yield, water, oil, salt, seasoning, or branded-product variation. Foundation Foods provides food records, not a complete, clinically validated recipe set. The evaluation does not test real patient-specific constraints or health outcomes.

This boundary is consistent with the source guidance: ADA's 2026 Standards call for individualized meal planning and medical nutrition therapy; KDOQI's CKD nutrition guideline includes individualized electrolyte management; NHLBI's DASH sodium figures are daily pattern targets, not automatic meal limits. [ADA Standards of Care—2026](https://diabetesjournals.org/care/article/49/Supplement_1/S89/163932), [KDOQI Nutrition Guideline—2020 Update](https://pubmed.ncbi.nlm.nih.gov/32829751/), [NHLBI DASH Eating Plan](https://www.nhlbi.nih.gov/health/dash-eating-plan).

## What is needed before a real-use claim

1. Put actual per-case limits in the evaluation data: nutrient, value, unit, scope (meal/day/remaining), selected target, and prior intake. Do not infer missing clinical targets.
2. Add explicit expected recipe IDs or acceptable validated ingredient compositions, and define whether an absent named dish should trigger clarification, safe abstention, or a constrained new-recipe flow.
3. Expand recipe coverage with validated amounts, preparation state, yield, and sources. Keep Foundation Foods ingredient data distinct from complete recipe data.
4. Add test cases for ingredient lookups, substitutions, explicit weighed compositions, meal plans, ambiguous foods, missing records, and open-ended requests. The current 60 requests all ask for one named dish.
5. Complete the blinded 20-pair answer review with a dietitian or other qualified reviewer, focusing on evidence grounding, constraint safety, dish identity, and material errors.
6. Prevent the response layer from asserting “suitable,” “within range,” “passes,” or “fits the remaining allowance” unless the deterministic evidence explicitly supports that claim. Keep nutrition totals and constraint outcomes generated by the deterministic engine.

## Artifacts

- `summary.md`: primary model comparison table and run metadata.
- `metrics.csv`: machine-readable aggregate metrics and fingerprints.
- `case_results.csv`: all 60 cases for both models, including failures and 20 sampled answers per model.
- `stratified_metrics.csv`: results by model, difficulty, and disease-profile group.
- `method_results.csv`: the 10 recipe-method checks per model.
- `evidence_claim_screen.csv`: sampled answers flagged by the evidence-consistency screen.
- `blind_response_review.csv`: blank blinded human-rating form; complete this before reporting human quality scores.
- `blind_response_key.csv`: A/B mapping; keep separate from reviewers until they submit ratings.
- `raw_outputs.json`, `model_1.json`, and `model_2.json`: raw plans, evidence, responses, revisions, and runtime details.

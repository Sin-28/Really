# Step 6C — End-to-End Testing and Evaluation

**Evaluation date:** 2026-10-18  
**Human labels:** None supplied; all 13 review records remain `PENDING_REVIEW`.  
**Clinical status:** This evaluation does not establish clinical safety or suitability.

## Summary

Step 6B adds a deterministic final acceptance policy over existing Step 5C nutritional metadata and Step 6A.3 culinary metadata. The policy is opt-in through `RENALLY_FINAL_RECIPE_ACCEPTANCE=true`, defaults off, does not start missing enrichments, and does not change adaptation candidate selection or legacy results.

Ten developer-authored controlled cases produced 10/10 expected policy statuses. The controlled distribution was **4 ACCEPTED, 1 REJECTED, 1 REVIEW_REQUIRED, and 4 UNVERIFIED**. These are synthetic policy checks, not real recipe results or human-validated accuracy. A separate Step 5C integration test uses a controlled nutrient record to verify that reducing salmon from 100 g to 80 g recalculates sodium to 80 mg, passes a 100 mg meal limit, and combines with a structured culinary PASS to produce `ACCEPTED`.

The live Food.com evaluation completed on 2026-10-18 after local Neo4j access was restored. It scanned 200 records and evaluated 10 recipes (74 ingredients) in 64.9 seconds. The bounded sample prioritizes recipes with missing or range quantities, so it is diagnostic and not representative of the full catalog. Mapping persistence and SLM inference were disabled.

The completed Step 6A.3 v8 live run already included the four original Step 6A.2 cases. Across those four: two outputs were complete/schema-valid and two timed out. Across all six calls, 2/6 were complete/schema-valid; mean inference time was 88.66 seconds and median was 90.09 seconds. No new Qwen call was started for Step 6C.

## Final acceptance policy

`src/recipes/final_recipe_acceptance.py` emits a separate `final_recipe_acceptance` result with policy version, nutrition and culinary statuses, mapping and quantity completeness, valid nutrient basis, missing evidence, rejection/review reasons, and verification provenance.

Status precedence is deterministic:

1. Confirmed nutritional failure, culinary failure, or deterministic guard failure → `REJECTED`.
2. Missing required evidence or unavailable evaluation → `UNVERIFIED`.
3. Assumption-dependent or uncertain evaluation → `REVIEW_REQUIRED`.
4. Complete nutritional PASS and culinary PASS, with authorized mappings, resolved quantities, valid serving/nutrient basis, and no critical evidence gap → `ACCEPTED`.
5. No active numeric dietary constraint → `NOT_APPLICABLE` unless a confirmed failure already requires `REJECTED`.

The acceptance flag alone creates no mapper, nutrient calculator, or culinary model call. It only consumes metadata produced under the existing independent enrichment flags. An absent dependency with active constraints cannot produce `ACCEPTED`.

## Controlled evaluation

The 10 cases in `data/final_recipe_acceptance_controlled_cases.json` cover seaweed-only ramen, compensating broth/seaweed/aromatics, basil-to-parsley, quantity change, preparation-state remapping, missing quantity, missing USDA mapping, nutrient violation, missing instructions, and a complete passing adaptation. Expected statuses are developer-authored assertions, not ground truth.

| Metric | Result |
|---|---:|
| Controlled cases | 10 |
| Expected policy outcomes matched | 10/10 |
| ACCEPTED | 4 |
| REJECTED | 1 |
| REVIEW_REQUIRED | 1 |
| UNVERIFIED | 4 |
| Mean acceptance-policy time | 0.008 ms per case |

The mean is local deterministic policy evaluation time over these small synthetic payloads; it excludes retrieval, mapping, nutrient calculation, and Qwen inference. No false-acceptance or false-rejection rate is reported because there are no independent labels.

The quantity-change integration check uses existing Step 5C calculation services and a controlled USDA-shaped fixture. It confirms 80 g × 100 mg sodium per 100 g = **80 mg** for the one-serving adaptation. The test also verifies constraint PASS and final ACCEPTED when the supplied culinary metadata is a complete, valid PASS. This is arithmetic verification against the controlled fixture, not an external USDA accuracy study.

## Real Food.com sample

The read-only run of `scripts/evaluate_final_recipe_acceptance.py` read 200 source rows and selected 10 recipes with 74 ingredients. It attempted mapping for every ingredient without persisting mappings or invoking SLM/culinary inference.

| Metric | Result |
|---|---:|
| Source rows scanned / recipes evaluated | 200 / 10 |
| Ingredients evaluated | 74 |
| Gram-resolved quantities | 0/74 (0%) |
| Source quantity alignment | 6 empty arrays, 3 aligned arrays, 1 mismatched pair |
| Authorized mappings | 3/74 (4.1%) |
| Human-verified mappings | 0/74 (0%) |
| Candidate retrieval hits | 64/74 (86.5%) |
| Ingredients with ambiguity reasons | 74/74 (100%) |
| Nutrient calculations available | 0/10 |
| Final acceptance | 10 `NOT_APPLICABLE` |
| Total sample time | 64.9 s |

The zero gram-resolution result means no sampled ingredient had a complete gram amount suitable for calculation. Six recipes had empty quantity arrays; one had mismatched ingredient and quantity arrays. The three aligned arrays did not produce gram quantities, because volume/count units require a matching USDA portion reference and source quantities are not assumed to be weights. Candidate hits indicate retrieval coverage only: none of the 74 ingredients had a human-verified mapping. Three mappings met the broader authorized calculation statuses, including one approved reference default, but quantities still prevented nutrient totals.

All ten calculations were `UNAVAILABLE`. Constraint validation and final acceptance were `NOT_APPLICABLE` because the run supplied no user meal budgets; it also had no adapted recipes for culinary validation. This is not a dietary pass. The sample prioritizes unresolved and range quantities, then fills from source order, so its coverage figures should not be generalized to the full Food.com catalog.

## Human culinary review set

`data/culinary_human_review_dataset.json` contains 13 representative examples from the existing controlled culinary fixtures: compatible substitutions, partial/uncertain cases, removed binders/leaveners, missing instructions, intentional salt reduction, quantity changes, and multiple compensating ingredients. It records the original/adapted recipe, detected ingredient changes, available live model output, deterministic findings, and final culinary status. Examples without a live call are marked `NOT_RUN`. Every `human_label` is `PENDING_REVIEW`; every reviewer note is blank. No expected status was copied into a human-label field.

## Live Qwen evidence

The existing completed Step 6A.3 v8 evaluation is in `reports/step_6a3_culinary_functional_coverage.md` and `/private/tmp/step6a3_live_results.json`. It used cached `Qwen/Qwen3-1.7B`, CPU, offline mode, and a 90-second generation deadline. Cases A–D are the original four Step 6A.2 comparisons. The six-case results were:

| Metric | Result |
|---|---:|
| JSON and schema complete | 2/6 (33.3%) |
| Required function rows returned | 6/15 |
| Final outcomes | 0 PASS, 0 FAIL, 2 REVIEW_REQUIRED, 4 UNAVAILABLE |
| Mean / median inference time | 88.66 s / 90.09 s |

The low completion rate and long latency are operational limits. No human false-PASS/false-FAIL metric is available. The culinary flag remains off by default.

## Verification

- Full test suite: **411 passed** via `python -m unittest discover -s tests` in the existing `fyp-local` environment.
- Controlled evaluator: **10/10 expected statuses matched**.
- Step 5C + acceptance integration test: complete calculation, 80 mg sodium, constraint PASS, final ACCEPTED.
- Read-only Food.com evaluation: **10 recipes / 74 ingredients evaluated**, scanning 200 records; 0 gram quantities and 0 human-verified mappings in this diagnostic sample.
- Human review set: 13 cases, **0 human labels**.

Automated tests confirm implementation behavior under the supplied fixtures. They do not prove clinical safety, culinary accuracy, or representative Food.com performance.

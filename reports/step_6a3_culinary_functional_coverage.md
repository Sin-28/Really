# Step 6A.3 — Culinary Functional Coverage Validation

**Review state:** No human labels were collected. Live outputs and fixture expectations below are not culinary ground truth. This report records the Step 6A.3 implementation and live assessment; follow-on Step 6B and Step 6C work is summarized below and in the linked evaluation report.

## Summary

Step 6A.2 could return `PASS` for the ramen seasoning-packet substitution because it judged the ingredient change as compatible without checking the several roles the packet served. Step 6A.3 adds recipe-context function requirements, whole-recipe support references, strict evidence validation, and Python-derived status aggregation. The final live Qwen sample prevented a `PASS` for Case B, but only two of six final calls completed with valid JSON and schema. The current live inference reliability and latency are not sufficient for Step 6B.

## Files changed

- `src/recipes/adapted_recipe_culinary.py` — function-role identification, reference validation and partitioning, unknown placeholders, and status guards.
- `src/generation/qwen_generator.py` — v8 single-pass prompt, compact evidence-reference field, and coverage response schema.
- `tests/test_adapted_recipe_culinary.py` — role, evidence, missing-coverage, intentional reduction, and status tests.
- `tests/test_qwen_inference_configuration.py` — prompt/schema, compact reference, and token-budget tests.
- `data/culinary_functional_coverage_fixtures.json` — 16 reproducible cases with developer-defined expectations and an explicit non-human-label marker.
- `README.md` — feature and policy documentation.
- `reports/step_6a3_culinary_functional_coverage.md` — this implementation and evaluation report.

## Architecture and policy

The existing informational culinary validator remains the single system. The feature stays opt-in, reuses the existing lazy Qwen instance, makes one inference call, and does not change nutrition, recipe generation, adaptation choices, ranking, or database state.

Python assigns change IDs, adapted ingredient references, and adapted step references. It identifies functions to assess from explicit role/function metadata and narrow source/context cues: concentrated seasoning components require `FLAVOR_BASE`, `SALTINESS`, and `UMAMI`; herbs cue `AROMATICS`; eggs in baking cue `BINDING` and `STRUCTURAL_INTEGRITY`; leaveners, thickeners, salt, fats, cream in soup/sauce, and cooking liquids cue their corresponding functions. These cues identify assessment candidates; Qwen still classifies importance and coverage. Unclassified changes do not default to `FLAVOR_BASE`, and Qwen may add other functions supported by the complete recipe context.

Qwen returns per-change judgments and nested function rows. Each row cites `supporting_evidence_ids`, a compact list that may contain adapted ingredient and step references. Python partitions and validates those references against the adapted recipe. An added ingredient can support a replaced source’s row, but must not own a source-function row. Missing required model assessments are exposed as Python-originated `UNKNOWN` placeholders with no fabricated evidence.

Python returns `PASS` only when all checks, required functions, evidence, and recipe context support it. Partial, unknown, unsupported, or contradictory essential-function evidence requires review. A missing essential function becomes `FAIL` only when the complete assessment also finds the recipe incoherent; confirmed deterministic violations remain `FAIL` even when the model is unavailable. Missing recipe information remains unresolved. An intentional reduction requires explicit `adaptation_intent` to avoid a review solely for that reduction.

The broad-flavor consistency guard flags an essential `FLAVOR_BASE` row when it cites only the same evidence already used for a narrower covered function such as umami or saltiness. It asks for review; it does not declare an ingredient pair incompatible. This guard and the role cues are policy heuristics that still need human review.

## Why the previous Case B returned `PASS`

Step 6A.2 asked Qwen for a single ingredient-change judgment. Qwen marked seaweed compatible, quantity plausible, cooking compatible, and the recipe coherent. The Python validator had no separate required assessments for the seasoning packet’s flavor-base, saltiness, and umami functions, so those positive per-change judgments were enough for `PASS`.

Step 6A.3 requires those three distinct rows when the source is identified as a concentrated seasoning component. In the final live Case B, Qwen marked all three covered but cited the same seaweed ingredient and step for the broad flavor base and the narrower components. Python returned `REVIEW_REQUIRED` with `ESSENTIAL_FLAVOR_BASE_EVIDENCE_NOT_DISTINCT_FROM_COMPONENT`. This decision came from evidence consistency, not a seaweed-specific fail rule.

## Fixtures

The 16-case JSON fixture set contains the original basil/parsley, ramen seaweed-only, ramen with broth and garlic, and missing-instructions cases, plus binding, thickening, fat/texture, sodium-reduction, quantity, protein, cooking-method, ambiguous-identity, and other role examples. `label_status` is `developer_defined_test_expectations_not_human_validated`; these expected statuses are for deterministic test design only.

The final live sample used A–D unchanged from the Step 6A.2 report, plus fixture E (egg replaced by flax and water in a loaf) and fixture G (cream replaced by oat milk and cornstarch in soup). Model observations are separate from fixture expectations.

## Final live Qwen run

Setup: cached `Qwen/Qwen3-1.7B`, offline mode, CPU, BF16, 8 threads, non-thinking mode, one shared model instance, and a 90-second generation deadline. No model weights were downloaded. Four calls hit the generation deadline; the service returned `UNAVAILABLE` and preserved missing expected function rows as `UNKNOWN`.

| Case | Input / output tokens | JSON / schema | Change IDs | Required function rows | Model coverage and evidence | Guard / final status | Inference |
|---|---:|---|---:|---:|---|---|---:|
| A — Basil to parsley | 887 / 17 | Invalid / unavailable | 0/1 | AROMATICS: UNKNOWN placeholder | No usable model judgment; no support refs accepted | No deterministic failure; **UNAVAILABLE** (`GENERATION_DEADLINE_EXCEEDED`) | 94.42 s |
| B — Seasoning packet to seaweed 3 g | 1063 / 298 | Valid / complete | 1/1 | 3/3 returned | Qwen marked FLAVOR_BASE, SALTINESS, and UMAMI essential and covered; each cited adapted seaweed and the adapted step | Broad flavor evidence duplicated narrow component evidence; **REVIEW_REQUIRED** | 82.01 s |
| C — Seasoning packet to seaweed, broth, and garlic | 1430 / 282 | Invalid / unavailable | 0/3 | FLAVOR_BASE, SALTINESS, UMAMI: UNKNOWN placeholders | No complete model judgment or accepted support refs | No deterministic failure; **UNAVAILABLE** (`GENERATION_DEADLINE_EXCEEDED`) | 90.09 s |
| D — Missing adapted instructions | 1027 / 281 | Valid / complete | 1/1 | 3/3 returned | FLAVOR_BASE covered, SALTINESS partially covered, UMAMI covered; seaweed cited, no process step cited | `ADAPTED_COOKING_INSTRUCTIONS_MISSING`; **REVIEW_REQUIRED** | 85.22 s |
| E — Egg to flax and water in loaf | 1144 / 262 | Invalid / unavailable | 0/2 | BINDING, STRUCTURAL_INTEGRITY: UNKNOWN placeholders | No complete model judgment or accepted support refs | No deterministic failure; **UNAVAILABLE** (`GENERATION_DEADLINE_EXCEEDED`) | 90.09 s |
| G — Cream to oat milk and cornstarch | 1222 / 174 | Invalid / unavailable | 0/2 | FAT, LIQUID, TEXTURE: UNKNOWN placeholders | No complete model judgment or accepted support refs | No deterministic failure; **UNAVAILABLE** (`GENERATION_DEADLINE_EXCEEDED`) | 90.13 s |

### Metrics

- Raw JSON syntax validity: **2/6 (33.3%)**.
- Complete required schema: **2/6 (33.3%)**.
- Change-ID coverage: **2/2** in the schema-valid outputs; **2/10** expected IDs were returned across all six calls because four responses were unavailable.
- Required function assessment coverage: **6/6** in the schema-valid outputs; **6/15** expected rows returned across the full sample. The nine absent rows remain `UNKNOWN` in metadata.
- Unsupported evidence references: **0** among nine evidence references in the six returned function rows from the two schema-valid cases. The four unavailable calls supplied no validated evidence.
- Contradictory functional judgments: **0** detected in the two complete outputs.
- Final status distribution: **0 PASS, 0 FAIL, 2 REVIEW_REQUIRED, 4 UNAVAILABLE**.
- Inference latency: **88.66 s mean; 90.09 s median** across all six calls. The two schema-valid calls averaged **83.61 s**.

Step 6A.2 reported 4/4 JSON-valid responses, 4/4 complete schemas, 6/6 change IDs, 20.95 s mean inference, and 19.31 s median. The case sets and output schemas differ, so this is directional rather than a controlled latency comparison. Step 6A.3 adds substantial latency and did not preserve the earlier reliability rate in this live run.

## Potential false results and limits

No model-generated `PASS` or `FAIL` occurred in the final six-case sample, so it provides no estimate of false-pass or false-fail rates. Case B’s broad flavor claim was conservatively sent to review, matching its developer-defined expectation; it is not a human-validated result. A, C, E, and G were unavailable, so their culinary judgments remain unknown. Case C’s combined broth-and-aromatics replacement has not been validated live in this final run. The fixture expectations are not gold labels, and no culinary accuracy claim is warranted.

The function cues are intentionally narrow and do not cover every ingredient role. Some cues may miss context or overstate relevance; the model can still misclassify importance, coverage, or broader flavor contributions. The 1.7B model’s one-pass CPU latency and deadline failures on larger prompts are the main operational limit. The broad-flavor evidence-distinctness guard is also an unvalidated policy choice.

## Automated verification

- Focused culinary and Qwen configuration tests: **91 passed**.
- Full test suite: **390 passed, 0 failed**.
- Live evaluation: **6 attempted**; **2 complete schema-valid outputs, 4 generation-deadline failures**.
- No live evaluation is part of the ordinary test suite.

## Follow-on Steps 6B and 6C

The final implementation now includes the deterministic Step 6B acceptance policy and Step 6C evaluation workflow. See [Step 6C evaluation report](step_6c_end_to_end_evaluation.md) for the policy integration, 10 controlled cases, pending human-review set, full-suite test result, and Food.com evaluation limitation. The low live Qwen completion rate and latency recorded above remain unchanged; culinary validation and final acceptance remain opt-in.

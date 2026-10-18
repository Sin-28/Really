# Step 6A.2 — Culinary Structured Output Evaluation

**Review state:** All four cases are not yet human-validated. This report records model judgments and deterministic checks only.

## Scope and implementation

Prompt `adapted-recipe-culinary-v5` asks Qwen for one minimal judgment per Python-assigned change ID: functional compatibility, quantity appropriateness, cooking compatibility, recipe coherence, and a short reason. Python owns recipe/change metadata, strict schema validation, missing-evidence records, final status, and `requires_review`. The model does not provide overall status or review flags.

When the recipe has no detected ingredient changes, the service returns `REVIEW_REQUIRED` with `NO_STRUCTURED_CHANGES_TO_EVALUATE`; the current schema cannot assess instruction-only edits. Parseable but incomplete output is `REVIEW_REQUIRED`; malformed output or inference failure is `UNAVAILABLE`; a confirmed deterministic failure keeps `FAIL` even when inference is unavailable.

The public metadata retains status, guard findings, per-change results, missing information, model, prompt version, and timestamp. It adds Python-assigned change IDs, structured changes, schema errors, and raw model judgments for review. Nutrition, adaptation generation, ranking, USDA policy, and global Qwen settings were not changed.

## Live setup

- Model: `Qwen/Qwen3-1.7B`, cached weights in offline mode; CPU, BF16, 8 threads.
- Prompt: `adapted-recipe-culinary-v5`; non-thinking mode; 90-second generation deadline; one shared model instance.
- The four Step 6A.1 representative cases were reused without changes.

## Reliability metrics

| Measure | Step 6A.1 | Step 6A.2 final prompt |
|---|---:|---:|
| Valid JSON | 4/4 (100%) | 4/4 (100%) |
| Complete required schema | 0/4 (0%) | 4/4 (100%) |
| Change-ID coverage | Not available in old schema | 6/6 (100%) |
| Invalid enum values | Not available from incomplete old schema | 0/6 (0%) |
| Contradictory status/review fields | 2/4 observed | 0/4; fields are no longer model-generated |
| Timeouts | 0/4 | 0/4 |
| Final status distribution | 4 REVIEW_REQUIRED | 3 PASS, 1 REVIEW_REQUIRED, 0 FAIL, 0 UNAVAILABLE |

Step 6A.1 input counts were 649, 671, 774, and 664 tokens (mean 689.5). Step 6A.1 first-token latencies were 11.90, 12.89, 15.82, and 14.01 seconds; generation times were 21.27, 25.05, 36.98, and 24.77 seconds. Step 6A.2 used 653, 673, 788, and 666 input tokens (mean 695; median 669.5), a 0.8% increase in the mean, and generated 86, 70, 177, and 96 tokens (mean 107.25; median 91).
Step 6A.2 service inference duration averaged **20.95 s** (median 19.31 s); model generation averaged **20.94 s** (median 19.30 s). Mean generation time was about 22.5% lower than Step 6A.1. First-token latency averaged 14.62 s (median 14.41 s), about 7% higher. Four examples do not establish statistically reliable model performance.

## Case results and human-review material

| Case | Input / output tokens | JSON / schema | ID coverage | Guards | Final status | Missing evidence | Inference |
|---|---:|---|---:|---|---|---|---:|
| A — Basil to parsley | 653 / 86 | valid / complete | 1/1 | PASS (0 findings) | **PASS** | None | 18.17 s |
| B — Seasoning packet to seaweed 3 g | 673 / 70 | valid / complete | 1/1 | PASS (0 findings) | **PASS** | None | 18.54 s |
| C — Seasoning packet to seaweed, broth, and garlic | 788 / 177 | valid / complete | 3/3 | PASS (0 findings) | **PASS** | None | 27.02 s |
| D — Missing adapted instructions | 666 / 96 | valid / complete | 1/1 | PASS (0 findings) | **REVIEW_REQUIRED** | ADAPTED_COOKING_INSTRUCTIONS_MISSING | 20.08 s |

### A — Basil to parsley — not human-validated

Original **Pasta with basil** (`herb-pasta`, 2 serving(s)): pasta: 160 g; fresh basil: 8 g.
Original instructions: Boil pasta and finish with fresh basil.
Adapted **Pasta with parsley** (`herb-pasta-adapted`, 2 serving(s)): pasta: 160 g; fresh parsley: 8 g.
Adapted instructions: Boil pasta and finish with fresh parsley.
Structured changes:
- `change-1` REPLACED: fresh basil [id `basil`] (8 g) → fresh parsley [id `parsley`] (8 g)
Actual Qwen judgments:
- `change-1`: function **COMPATIBLE**; quantity **PLAUSIBLE**; cooking **COMPATIBLE**; coherence **COHERENT**. Reason: “The herb performs the same finishing role.”
Deterministic findings: none; guards passed.
Final validator status: **PASS**; `requires_review=false`. Review reasons: none.

### B — Seasoning packet to seaweed 3 g — not human-validated

Original **Instant ramen** (`ramen-1`, 1 serving(s)): instant ramen noodles: 80 g; instant ramen seasoning packet: 6 g.
Original instructions: Boil the noodles in water and stir in the seasoning packet.
Adapted **Adapted instant ramen** (`ramen-1-adapted`, 1 serving(s)): instant ramen noodles: 80 g; dried seaweed: 3 g.
Adapted instructions: Boil the noodles and stir in the dried seaweed.
Structured changes:
- `change-1` REPLACED: instant ramen seasoning packet [id `packet`] (6 g) → dried seaweed [id `seaweed`] (3 g)
Actual Qwen judgments:
- `change-1`: function **COMPATIBLE**; quantity **PLAUSIBLE**; cooking **COMPATIBLE**; coherence **COHERENT**. Reason: “Dried seaweed serves a similar function to instant ramen seasoning in the final dish.”
Deterministic findings: none; guards passed.
Final validator status: **PASS**; `requires_review=false`. Review reasons: none.

### C — Seasoning packet to seaweed, broth, and garlic — not human-validated

Original **Instant ramen** (`ramen-1`, 1 serving(s)): instant ramen noodles: 80 g; instant ramen seasoning packet: 6 g.
Original instructions: Boil the noodles in water and stir in the seasoning packet.
Adapted **Adapted instant ramen** (`ramen-1-adapted`, 1 serving(s)): instant ramen noodles: 80 g; dried seaweed: 3 g; low-sodium vegetable broth: 240 g; garlic: 2 g.
Adapted instructions: Boil noodles in low-sodium vegetable broth with garlic and seaweed.
Structured changes:
- `change-1` REPLACED: instant ramen seasoning packet [id `packet`] (6 g) → dried seaweed [id `seaweed`] (3 g)
- `change-2` ADDED: ∅ → low-sodium vegetable broth [id `broth`] (240 g)
- `change-3` ADDED: ∅ → garlic [id `garlic`] (2 g)
Actual Qwen judgments:
- `change-1`: function **COMPATIBLE**; quantity **PLAUSIBLE**; cooking **COMPATIBLE**; coherence **COHERENT**. Reason: “Dried seaweed replaces seasoning packet, maintaining functional role.”
- `change-2`: function **COMPATIBLE**; quantity **PLAUSIBLE**; cooking **COMPATIBLE**; coherence **COHERENT**. Reason: “Low-sodium vegetable broth is added in appropriate quantity.”
- `change-3`: function **COMPATIBLE**; quantity **PLAUSIBLE**; cooking **COMPATIBLE**; coherence **COHERENT**. Reason: “Garlic is added in appropriate quantity and function.”
Deterministic findings: none; guards passed.
Final validator status: **PASS**; `requires_review=false`. Review reasons: none.

### D — Missing adapted instructions — not human-validated

Original **Instant ramen** (`ramen-1`, 1 serving(s)): instant ramen noodles: 80 g; instant ramen seasoning packet: 6 g.
Original instructions: Boil the noodles in water and stir in the seasoning packet.
Adapted **Adapted instant ramen** (`ramen-1-adapted`, 1 serving(s)): instant ramen noodles: 80 g; dried seaweed: 3 g.
Adapted instructions: Missing
Structured changes:
- `change-1` REPLACED: instant ramen seasoning packet [id `packet`] (6 g) → dried seaweed [id `seaweed`] (3 g)
Actual Qwen judgments:
- `change-1`: function **COMPATIBLE**; quantity **PLAUSIBLE**; cooking **COMPATIBLE**; coherence **COHERENT**. Reason: “Dried seaweed serves a similar function to instant ramen seasoning in the adapted recipe.”
Deterministic findings: none; guards passed.
Final validator status: **REVIEW_REQUIRED**; `requires_review=true`. Review reasons: ADAPTED_COOKING_INSTRUCTIONS_MISSING.

## Unexpected judgments and limitations

- **Case B:** Qwen marked dried seaweed 3 g compatible and plausible as a replacement for a 6 g concentrated ramen seasoning packet, saying it serves a similar function. The fixture has no replacement broth-seasoning base. Deterministic guards check data consistency, not this culinary role, so the validator returned `PASS`. This is a material false-positive risk pending human review and evidence-backed policy work.
- **Case C:** The two-change example improved coverage from 1/3 in the first prompt draft to 3/3 in the final prompt. Qwen judged seaweed, broth, and garlic compatible and coherent. The broth supports a base role, but these judgments remain unvalidated.
- **Case D:** Qwen reported compatible cooking and coherent recipe despite absent adapted instructions. Python preserved `ADAPTED_COOKING_INSTRUCTIONS_MISSING` and returned `REVIEW_REQUIRED`.
- Schema correctness measures shape and ID coverage only. It does not establish culinary accuracy. No confidence score or human-review label was generated.

## Automated verification

- Focused culinary schema, existing culinary, Qwen configuration, and nutrition validation tests: **95 passed**.
- Full suite: **367 passed, 0 failed, 0 errors, 0 skipped**.
- `compileall` for `src` and `tests`: passed.
- Live model cases: **4 completed, 0 timeouts**; these are observations, not unit tests.

## Step 6B readiness

**Not ready.** The model met the structured-output contract in these four examples, but the Case B false-positive `PASS` shows schema compliance is insufficient. Complete human review and evidence-backed handling for seasoning/base substitutions before Step 6B acceptance-policy evaluation. Step 6B was not implemented.

# Qwen model comparison for RenAlly

Run directory: `/Users/sin/Documents/ChatGPT/EE_FYP/RenAlly/reports/model_comparison/run_20261013_033016`

## Method

Both models were run sequentially in isolated processes against the same Neo4j catalog and all selected evaluation rows. The application prompt, greedy decoding, context fields, and evidence rules were held constant. Plans are scored over all selected rows; evidence-based answers are generated only for the stratified human-review sample to reduce runtime. The 60-case set describes expected behavior but does not contain complete gold model responses or patient-specific numeric limits.

All evaluation queries ask for one named dish, so `single_recipe` is the expected intent. A dish is considered supported when its `Dish` text overlaps a recipe name or alias in the Neo4j catalog using the same substring rule as `evaluate.py`. For supported cases, route success means the model-assisted app lookup returns one of those recipe IDs. For unsupported cases, success means the lookup abstains.

The response-quality sheet is blinded (`blind_response_review.csv`). Rate only the sampled cases and then unblind with `blind_response_key.csv`. Automated method faithfulness checks for catalog foods or amounts outside the validated composition; it does not judge culinary quality.

## Pilot scope

This run scored 6 stratified cases from the 60-case evaluation set. Generated responses were reviewed for 3 cases and method faithfulness was checked on 2 recipes per model. Treat the results as preliminary; use the full 60-case run and complete blinded response ratings for final report claims.

## Results

| Model | Parameters | Revision | Device / dtype | Load time (s) | Peak RSS (MB) |
|---|---:|---|---|---:|---:|
| Qwen/Qwen3-0.6B | 596,049,920 | `c1899de289a04d12100db370d81485cdf75e47ca` | mps / torch.bfloat16 | 3.9 | 1228 |
| Qwen/Qwen3-1.7B | 1,720,574,976 | `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` | mps / torch.bfloat16 | 6.5 | 1822 |

| Metric | Qwen/Qwen3-0.6B | Qwen/Qwen3-1.7B |
|---|---:|---:|
| Schema-valid plans | 6/6 (100.0%) | 5/6 (83.3%) |
| Single-recipe intent accuracy | 0/6 (0.0%) | 5/6 (83.3%) |
| Supported recipe Hit@1 | 2/2 (100.0%) | 2/2 (100.0%) |
| Unsupported-dish abstention | 0/4 (0.0%) | 2/4 (50.0%) |
| Overall route accuracy | 0/6 (0.0%) | 4/6 (66.7%) |
| Invalid recipe IDs | 0/6 (0.0%) | 0/2 (0.0%) |
| Method faithfulness | 0/2 (0.0%) | 0/2 (0.0%) |

**Planning latency (ms):** Qwen/Qwen3-0.6B: median 41960; p95 49976; Qwen/Qwen3-1.7B: median 71480; p95 120661.

**Grounded response latency (ms):** Qwen/Qwen3-0.6B: median 1041; p95 1183; Qwen/Qwen3-1.7B: median 8274; p95 14136.

**Peak resident memory:** Qwen/Qwen3-0.6B: 1228 MB; Qwen/Qwen3-1.7B: 1822 MB.

**Shared constraint-engine fixture:** 2/4 (50.0%); identical by design for both models. shared deterministic engine; synthetic engineering fixtures, not clinical targets.

## Observed differences

- Intent accuracy changed from 0.0% with `Qwen/Qwen3-0.6B` to 83.3% with `Qwen/Qwen3-1.7B` (+83.3 percentage points).
- Overall route accuracy changed from 0.0% to 66.7% (+66.7 percentage points).
- Median planning latency changed from 41960 ms to 71480 ms (+70.4%).
- Peak process memory changed from 1228 MB to 1822 MB (+48.4%).
- Treat these as descriptive results for this sample only. They are not a significance test; run the full test set and review the blinded answer sheet before drawing a final conclusion.

## Interpretation limits

- The recipe-hit and abstention scores measure routing to this local prototype catalog, not general nutritional reasoning.
- The deterministic constraint engine is shared; a larger SLM cannot improve its arithmetic or missing-data handling. The synthetic constraint fixture is an engineering check, not a clinical score.
- The response ratings require human review. Model size alone does not establish clinical safety or factual accuracy.
- Model revision, library versions, device, decoding settings, and catalog fingerprint are recorded in `metrics.csv` and `raw_outputs.json`.

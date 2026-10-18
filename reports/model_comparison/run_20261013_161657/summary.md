# Qwen model comparison for RenAlly

Run directory: `/Users/sin/Documents/ChatGPT/EE_FYP/RenAlly/reports/model_comparison/run_20261013_161657`

## Method

Both models were run sequentially in isolated processes against the same Neo4j catalog and all selected evaluation rows. The application prompt, greedy decoding, context fields, and evidence rules were held constant. Plans are scored over all selected rows; evidence-based answers are generated only for the stratified human-review sample to reduce runtime. The 60-case set describes expected behavior but does not contain complete gold model responses or patient-specific numeric limits.

All evaluation queries ask for one named dish, so `single_recipe` is the expected intent. A dish is considered supported when its `Dish` text overlaps a recipe name or alias in the Neo4j catalog using the same substring rule as `evaluate.py`. For supported cases, route success means the model-assisted app lookup returns one of those recipe IDs. For unsupported cases, success means the lookup abstains.

The response-quality sheet is blinded (`blind_response_review.csv`). Rate only the sampled cases and then unblind with `blind_response_key.csv`. Automated method faithfulness checks for catalog foods or amounts outside the validated composition; it does not judge culinary quality.

## Results

| Model | Parameters | Revision | Device / dtype | Load time (s) | Peak RSS (MB) |
|---|---:|---|---|---:|---:|
| Qwen/Qwen3-0.6B | 596,049,920 | `c1899de289a04d12100db370d81485cdf75e47ca` | mps / torch.bfloat16 | 4.8 | 1051 |
| Qwen/Qwen3-1.7B | 1,720,574,976 | `70d244cc86ccca08cf5af4e1e306ecf908b1ad5e` | mps / torch.bfloat16 | 7.1 | 1825 |

| Metric | Qwen/Qwen3-0.6B | Qwen/Qwen3-1.7B |
|---|---:|---:|
| Schema-valid plans | 58/60 (96.7%) | 57/60 (95.0%) |
| Single-recipe intent accuracy | 0/60 (0.0%) | 57/60 (95.0%) |
| Supported recipe Hit@1 | 10/20 (50.0%) | 18/20 (90.0%) |
| Unsupported-dish abstention | 0/40 (0.0%) | 32/40 (80.0%) |
| Overall route accuracy | 0/60 (0.0%) | 50/60 (83.3%) |
| Invalid recipe IDs | 0/60 (0.0%) | 0/12 (0.0%) |
| Method faithfulness | 6/10 (60.0%) | 4/10 (40.0%) |

**Planning latency (ms):** Qwen/Qwen3-0.6B: median 39541; p95 44820; Qwen/Qwen3-1.7B: median 60642; p95 75047.

**Grounded response latency (ms):** Qwen/Qwen3-0.6B: median 1012; p95 1234; Qwen/Qwen3-1.7B: median 3811; p95 13790.

**Peak resident memory:** Qwen/Qwen3-0.6B: 1051 MB; Qwen/Qwen3-1.7B: 1825 MB.

**Shared constraint-engine fixture:** 2/4 (50.0%); identical by design for both models. shared deterministic engine; synthetic engineering fixtures, not clinical targets.

## Observed differences

- Intent accuracy changed from 0.0% with `Qwen/Qwen3-0.6B` to 95.0% with `Qwen/Qwen3-1.7B` (+95.0 percentage points).
- Overall route accuracy changed from 0.0% to 83.3% (+83.3 percentage points).
- Median planning latency changed from 39541 ms to 60642 ms (+53.4%).
- Peak process memory changed from 1051 MB to 1825 MB (+73.6%).
- Treat these as descriptive results for this sample only. They are not a significance test; run the full test set and review the blinded answer sheet before drawing a final conclusion.

## Interpretation limits

- The recipe-hit and abstention scores measure routing to this local prototype catalog, not general nutritional reasoning.
- The deterministic constraint engine is shared; a larger SLM cannot improve its arithmetic or missing-data handling. The synthetic constraint fixture is an engineering check, not a clinical score.
- The response ratings require human review. Model size alone does not establish clinical safety or factual accuracy.
- Model revision, library versions, device, decoding settings, and catalog fingerprint are recorded in `metrics.csv` and `raw_outputs.json`.

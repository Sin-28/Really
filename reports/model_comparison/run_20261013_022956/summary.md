# Qwen model comparison for RenAlly

Run directory: `/Users/sin/Documents/ChatGPT/EE_FYP/RenAlly/reports/model_comparison/run_20261013_022956`

## Method

Both models were run sequentially in isolated processes against the same Neo4j catalog and all selected evaluation rows. The application prompt, greedy decoding, context fields, and evidence rules were held constant. The 60-case set describes expected behavior but does not contain complete gold model responses or patient-specific numeric limits.

All evaluation queries ask for one named dish, so `single_recipe` is the expected intent. A dish is considered supported when its `Dish` text overlaps a recipe name or alias in the Neo4j catalog using the same substring rule as `evaluate.py`. For supported cases, route success means the model-assisted app lookup returns one of those recipe IDs. For unsupported cases, success means the lookup abstains.

The response-quality sheet is blinded (`blind_response_review.csv`). Rate only the sampled cases and then unblind with `blind_response_key.csv`. Automated method faithfulness checks for catalog foods or amounts outside the validated composition; it does not judge culinary quality.

## Results


## Runs that did not complete

- `Qwen/Qwen3-0.6B`: worker exited with status 1
- `Qwen/Qwen3-1.7B`: worker exited with status 1

## Interpretation limits

- The recipe-hit and abstention scores measure routing to this local prototype catalog, not general nutritional reasoning.
- The deterministic constraint engine is shared; a larger SLM cannot improve its arithmetic or missing-data handling. The synthetic constraint fixture is an engineering check, not a clinical score.
- The response ratings require human review. Model size alone does not establish clinical safety or factual accuracy.
- Model revision, library versions, device, decoding settings, and catalog fingerprint are recorded in `metrics.csv` and `raw_outputs.json`.

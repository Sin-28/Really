# Qwen size comparison

This benchmark compares the local RenAlly Qwen request planner and evidence-based response writer using Qwen3-0.6B and Qwen3-1.7B. It plans against the existing 60-case evaluation set, generates evidence-based answers for a stratified human-review sample, uses the live Neo4j catalog and greedy decoding, and runs isolated sequential workers so peak process memory is measurable per model.

## Run

Start Neo4j and activate the Conda environment that has the Neo4j driver, PyTorch, and Transformers installed. From the RenAlly project directory run:

```bash
python scripts/compare_qwen_models.py
```

The first run may download the uncached model weights. Reports are written to a timestamped folder under `reports/model_comparison/`. To do a short smoke run before the full comparison:

```bash
python scripts/compare_qwen_models.py --max-cases 3 --method-cases 2 --review-cases 2
```

The full run plans all 60 requests and generates answers for the 20-case review sample. It creates:

- `summary.md`: report-ready metric definitions and comparison table.
- `metrics.csv`: one row per model with task, latency, model, and runtime measurements.
- `case_results.csv`: per-case routing results and generated answers for the review sample.
- `method_results.csv`: generated cooking methods with automated ingredient/amount checks.
- `blind_response_review.csv`: blinded A/B answer-rating form for a stratified 20-case sample.
- `blind_response_key.csv`: mapping from A/B labels to model IDs; keep this separate while rating.
- `raw_outputs.json` and `model_*.json`: raw plans, evidence, answers, and run metadata for reproducibility.

## Metrics and limits

- **Schema-valid plan rate:** raw model output meets the planner's required JSON fields and types.
- **Intent accuracy:** whether each one-dish request was classified as `single_recipe`.
- **Supported recipe Hit@1:** exact local recipe match among evaluation rows whose `Dish` value overlaps a local recipe name or alias.
- **Unsupported-dish abstention:** no recipe is selected when the local catalog has no matching `Dish` name or alias.
- **Overall route accuracy:** correct recipe selection for supported dishes and abstention for unsupported dishes.
- **Invalid recipe-ID rate:** the model directly proposed an ID absent from the Neo4j catalog, before the web app's safety normalization.
- **Method faithfulness:** the generated cooking method contains no unvalidated catalog food or new ingredient quantity. This checker does not score culinary realism.
- **Planning/response latency:** per-request generation latency after one unscored planning warm-up, plus model load time. Answer latency is measured only on the review sample.
- **Peak RSS:** maximum resident memory for the isolated worker, including the Neo4j projection and model.
- **Constraint fixture pass rate:** the same deterministic constraint engine and synthetic engineering fixtures run in each worker. This is a shared system control, not an SLM score and not clinical validation.

All 60 test rows request one dish, so the benchmark labels `single_recipe` as the expected intent. Supported/unsupported labels are derived from the existing `Dish` field using the same substring-overlap rule as `evaluate.py`; inspect the per-case CSV before citing aggregate routing accuracy. The test set does not provide complete patient-specific numeric limits, so no clinical numeric limits are passed to either model. Constraint results remain deterministic and should be reported separately from any SLM comparison.

The blinded response form is for human review. Suggested 1–5 scales are relevance, evidence grounding, constraint safety, and clarity, plus a material-error yes/no flag. Do not treat a higher automated score as evidence of clinical safety.

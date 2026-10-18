# Interim Evaluation: Qwen3-0.6B vs Qwen3-1.7B

## Purpose and setup

This section compares the two local language models currently considered for RenAlly: Qwen3-0.6B and Qwen3-1.7B. The comparison focuses on request interpretation, recipe routing, generated cooking instructions, runtime, and memory. It is an interim, descriptive evaluation rather than a final assessment of the RenAlly system.

Both models were evaluated on the same 60 requests and the same Neo4j catalog, using the same application prompts, context fields, evidence rules, and greedy decoding. The models ran sequentially in isolated processes on Apple MPS with bfloat16. All 60 test requests ask for one named dish, so the expected intent in this benchmark is `single_recipe`. Evidence-based answers were generated for a 20-case sample, and generated cooking methods were checked in 10 cases per model. The model revisions and full run data are recorded in the accompanying benchmark artifacts.

## Results

| Metric | Qwen3-0.6B | Qwen3-1.7B |
|---|---:|---:|
| Parameters | 596.0 million | 1,720.6 million |
| Schema-valid plans | 58/60 (96.7%) | 57/60 (95.0%) |
| Correct `single_recipe` intent | 0/60 (0.0%) | 57/60 (95.0%) |
| Recipe Hit@1 when a catalog match exists | 10/20 (50.0%) | 18/20 (90.0%) |
| Correct abstention when no catalog match exists | 0/40 (0.0%) | 32/40 (80.0%) |
| Overall route accuracy | 0/60 (0.0%) | 50/60 (83.3%) |
| Invalid recipe IDs proposed directly | 0/60 | 0/12 |
| Ingredient and quantity faithfulness in generated methods | 6/10 (60.0%) | 4/10 (40.0%) |
| Planning latency, median | 39.5 s | 60.6 s |
| Planning latency, p95 | 44.8 s | 75.0 s |
| Answer-generation latency, median (20-case sample) | 1.01 s | 3.81 s |
| Peak process memory | 1.05 GB | 1.83 GB |

## Interpretation

The clearest difference is request interpretation. The 0.6B model classified all 60 one-dish requests as composition questions, so it did not route any request correctly. Although 58 of its plans were structurally valid JSON, they represented the wrong intent. The 1.7B model correctly classified 57 of 60 requests and achieved 83.3% overall route accuracy. Among the 20 requests with a matching recipe in the catalog, its top-ranked recipe was correct in 18 cases, compared with 10 for the 0.6B model. For the 40 requests without a catalog match, the 1.7B model abstained in 32 cases; the 0.6B model did not abstain correctly in any case.

These routing improvements came with higher resource use. The 1.7B model has about 2.9 times as many parameters, used 73.6% more peak process memory, and had 53.4% higher median planning latency. Its answer-generation median was approximately 3.8 times that of the 0.6B model in the sampled cases. The planning p95 of 75 seconds may feel slow in an interactive chat, although observed latency depends on the local hardware and software setup.

Larger size did not improve every measured behavior. The 1.7B model's generated methods passed the ingredient-and-quantity faithfulness check in 4 of 10 cases, compared with 6 of 10 for the 0.6B model. This automated check detects unvalidated ingredients or amounts; it does not assess culinary quality or nutritional appropriateness. The structural validity rate was also slightly lower for the 1.7B model (95.0% versus 96.7%). Thus, in this comparison, the larger model was substantially better at selecting the request route, but was not more reliable at staying within validated recipe quantities when writing instructions.

The deterministic constraint-engine fixture scored 2/4 for both models. Because the same engine and synthetic fixture were used in both runs, this result is not an advantage or disadvantage of either SLM and should not be interpreted as a clinical performance score. Likewise, the absence of directly proposed invalid recipe IDs is a useful observation for this run, but it does not establish general reliability.

## Interim conclusion and limitations

For the tested SLM-enabled planner, Qwen3-1.7B is the stronger choice when the main requirement is interpreting a one-dish request and routing it to the local recipe catalog. That gain is substantial, but it costs more time and memory. The results do not support replacing deterministic validation with the larger model: generated ingredient amounts still require validation against retrieved data and application constraints.

The findings are limited to this 60-request set, in which every request asks for one named dish. Recipe routing scores measure behavior against the current local catalog, not general nutrition reasoning. Only 20 answer outputs and 10 generated methods per model were sampled; blinded human ratings have not yet been completed. The scores are descriptive results for this benchmark and should not be generalized to arbitrary user conversations or treated as a statistically significant difference. The constraint fixture is shared deterministic system behavior, not an SLM comparison.

**Interim finding:** Qwen3-1.7B is markedly better than Qwen3-0.6B at request classification and catalog routing on this test set, while Qwen3-0.6B is faster, uses less memory, and scored higher on the small automated method-faithfulness sample. A broader, human-reviewed evaluation is needed before drawing conclusions about answer quality.

## Benchmark source

Results are from `run_20261013_161657`. See `summary.md`, `metrics.csv`, `case_results.csv`, `method_results.csv`, and `blind_response_review.csv` in that run directory for metric definitions and underlying records. The blinded human-review form is pending ratings.

# USDA ingredient retrieval quality and integration report

**Date:** 2026-10-16  
**Scope:** Candidate retrieval quality and deterministic test stability. Step 5 was not started.

## Result

The ingredient resolver now applies a bounded, deterministic reranker to USDA full-text results. It filters clear food-category conflicts, distinguishes stated from unstated varieties and preparation details, and preserves the original Neo4j and legacy resolver scores as separate evidence. The candidate tuple API remains compatible with existing callers.

For this run, the retrieval version is `ingredient-rerank-v4`, the automatic mapping policy version is `2`, and the mapping cache identity remains `fdc-mapping-v1`. Retrieval changes refresh unverified candidate sets without invalidating or overwriting human-verified mappings.

The local regression suite passes. The live sample is promising for category filtering, but it does not justify automatic user-facing mapping: the five-item pre-mapping dry-run produced zero verified mappings, and generic ingredients remain ambiguous. **Step 5 should wait for a reviewed set of preferred USDA identities and variants.**

## Why the ranking changed

The previous resolver treated query words appearing anywhere in a USDA label as sufficient relevance. That ranked foods containing or mentioning an ingredient alongside the ingredient itself. Examples in the live baseline included tomatoes “with salt added” for `salt`, egg breads and bagels for `eggs`, nut butters and croissants for `butter`, crackers and yogurt for `milk`, mixed oils and anchovies packed in olive oil for `olive oil`, fish oil for `salmon`, and rice noodles for `cooked rice`.

The reranker now checks the primary food identity and applies ingredient-specific compatibility rules before ranking. Ranking evidence includes identity, explicit variety, preparation state, processing state, lexical overlap, and the original retrieval score. It also records unresolved candidate details such as butter saltedness, milk fat/flavor/species, chicken skin/bone/solution, salmon species and state, and rice variety. These scores are ordering evidence, not confidence values or claims of nutritional equivalence.

Token handling now normalizes `low-fat`/`reduced fat` and butter labels such as `without salt` into comparable variant markers. This prevents “low sodium” from being treated as “low fat” and makes unsalted butter evidence consistent across USDA phrasings.

## Live Neo4j comparison

The baseline column reports how many items in the old top-five sample passed the current clear identity/category matcher. This is a diagnostic filter, not a human gold label. The reranked column gives the final bounded top-five results for each query; all IDs are FDC IDs.

| Query | Baseline passing matcher | Reranked candidates |
|---|---:|---|
| `salt` | 2/5 | 173468 Salt, table; 746775 Salt, table, iodized |
| `eggs` | 2/5 | 748967 whole large egg; 173423 whole egg, fried; 172186 whole egg, poached; 171287 whole egg, raw; 329490 whole egg, dried |
| `butter` | 2/5 | 173410 salted butter; 173430 butter without salt; 790508 salted butter stick; 789828 unsalted butter stick; 173411 whipped butter with salt |
| `sugar` | 1/5 | 170674 Sugar, turbinado |
| `milk` | 0/5 | 170882 sheep milk; 171280 Indian buffalo milk; 170875 low-sodium milk; 170910 chocolate low-fat milk; 171266 milk with 3.7% milkfat |
| `olive oil` | 3/5 | 171413 olive salad/cooking oil; 748608 extra-virgin olive oil; 1750351 extra-light olive oil |
| `raw chicken breast` | 5/5 | 2646170 boneless skinless raw breast; 2727569 breast meat and skin, raw; 171474 broiler breast meat and skin, raw; 171077 skinless boneless breast meat, raw; 171509 skinless boneless breast with added solution, raw |
| `salmon` | 4/5 | 168023 fermented tipnuk salmon; 167647 king salmon, kippered; 167642 red salmon, canned; 175138 pink salmon, raw; 173691 sockeye salmon, raw |
| `cooked rice` | 4/5 | 168897 wild rice, cooked; 169711 glutinous rice, cooked; 173263 brown parboiled rice, cooked; 168932 white short-grain rice, cooked; 168930 white medium-grain rice, cooked |

Across the 45 baseline items, 23 passed the current category matcher and 22 were clear false positives under that filter. The reranker returned 36 candidates across these nine queries. The reduction in false matches is useful, but candidates still vary in important ways. In particular, the generic salmon results include fermented, kippered, and canned foods ahead of some raw choices; the milk results contain species and fat/flavor variants; and the chicken breast candidates differ in skin, bone, and added solution. Those cases remain ambiguous rather than being automatically selected.

The bounded local search surfaced only turbinado sugar for the generic `sugar` query. This is a catalog/search-coverage observation for this query, not evidence that granulated sugar is absent from the full USDA catalog.

## Read-only pre-mapping dry-run

The offline workflow scanned a capped sample of 100 recipes and selected 25 ingredient keys, processing only the first five with a batch size of five. It used the cached local SLM and did not write mapping records or a checkpoint.

| Measure | Result |
|---|---:|
| Recipes scanned | 100 (sample limited) |
| Ingredient occurrences / unparsed occurrences | 812 / 73 |
| Unique context-aware ingredient keys | 297 |
| Selected / processed ingredient keys | 25 / 5 |
| Candidate retrieval coverage | 5/5 (100%) |
| Automatically verified / previously human-reviewed mappings reused | 0 / 0 |
| Ambiguous / review-required unresolved mappings | 3 / 2 |
| Retrieval failures / SLM failures | 0 / 0 |
| Processing duration | 50.39 seconds |
| Persistent writes / checkpoint | Disabled / none |

The five processed ingredients were salt (37 occurrences), eggs (31), butter (29), sugar (26), and milk (21). The SLM recommended the single turbinado sugar result, but the deterministic policy still required review. This confirms that SLM output and retrieval order do not independently verify a USDA mapping.

## Implementation and regression-test changes

- Added a quality-evidence retrieval method while preserving the existing candidate tuple API and legacy `retrieval_score` field. Raw Neo4j score and reranker relevance score are stored separately.
- Versioned retrieval rules as `ingredient-rerank-v4`; kept `MAPPING_VERSION` unchanged so human review decisions remain reusable. Tightened the conservative automatic policy to version 2.
- Added deterministic retrieval cases for category false positives, variants, state, score separation, aliases, and cache behavior.
- Replaced production-graph assumptions in constraint and pipeline regression tests with a small test-only graph fixture. The live Neo4j check above is separate from the unit suite.
- Fixed the missing-data response path so it calls a configured `respond_missing_data` handler when available and falls back safely if it fails or returns no text.

## Verification

All commands ran in the `fyp-local` environment:

- `python -m unittest discover -s tests` — **125 tests passed**.
- Ingredient retrieval, mapping, pre-mapping, and validator group — **83 tests passed**.
- Fixture-backed constraint and pipeline group — **10 tests passed**.
- Live Neo4j evaluation — all nine candidate queries completed; the capped offline dry-run processed five items with 100% candidate coverage, zero automatic verifications, zero retrieval failures, zero SLM failures, and persistent writes disabled.

The automated tests use deterministic fixtures; they do not claim to exercise the live Neo4j instance. The separate live run supplies that integration evidence for the requested sample only.

## Remaining review before Step 5

The retrieval and policy paths are safe to keep evaluating, but the evidence is not enough to connect mappings to user-facing recipe adaptation. Reviewers should select preferred records for generic milk, salmon, chicken breast, rice, and butter; decide whether specific states or varieties should be requested from users; and check that commonly used sugar forms are present in the local catalog. Keep automatic verification disabled for generic ingredient names until those choices are represented in explicit, reviewed aliases or ingredient attributes.

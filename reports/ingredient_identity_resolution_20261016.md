# Step 4.7 — Ingredient Identity Resolution & Reference Mapping Policy

**Date:** 2026-10-16  
**Scope:** Ingredient identity resolution and separately reviewed USDA reference records. Step 5, recipe adaptation, nutrient validators, clinical thresholds, and user-facing confirmation flows were not changed.

## Design and behavior

Step 4.7 adds an opt-in resolution layer after existing mapping-cache and candidate retrieval. It preserves the established meaning of `VERIFIED` in `IngredientMapping` and reports identity separately with `EXACT_IDENTITY`, `VERIFIED_MAPPING`, `REFERENCE_DEFAULT`, `AMBIGUOUS`, `UNRESOLVED`, and `REVIEW_REQUIRED`.

Resolution order is:

1. Reuse an existing `VERIFIED` mapping as authoritative.
2. Apply the existing deterministic automatic-mapping policy. A unique exact result is `EXACT_IDENTITY`; it is only written as a `VERIFIED` mapping when the runtime is explicitly configured for automatic exact verification and the caller opts into persistence.
3. Look for an approved, current preferred reference. It must point to a USDA ID in the local catalog, pass the deterministic candidate guard, and satisfy the reference policy. A reference can be used only as a distinct `REFERENCE_DEFAULT`, with assumptions and uncertainty returned as structured metadata.
4. Otherwise preserve ambiguity, unresolved state, or need for review.

Reference defaults do not write or overwrite ingredient mappings, do not supersede an existing verified mapping, and never confirm recipe constraints. The default runtime is read-only. It does not invoke the SLM as an authority or allow an SLM result to bypass the deterministic guard.

The reference policy supports nine categories: salt, egg, butter, sugar, milk, olive oil, chicken breast, salmon, and rice. It records the canonical category, local FDC ID and description, attributes, preparation/species/variety details where relevant, approval status, reviewer and provenance, policy version, and timestamps. Approved and rejected references are kept separate from `IngredientMapping`; review events are traceable and append-only. Rejected or stale-policy references are excluded. Explicitly stated attributes are checked for contradictions before a reference can apply. Unstated identity details remain unresolved unless both the reference and runtime permit an explicit default; any such assumption is returned to the caller.

Nutritional certainty is represented with categorical values (`VERIFIED_IDENTITY`, `IDENTITY_EXACT`, `REVIEWED_REFERENCE_MATCH`, `ASSUMPTION_BASED`, or `UNRELIABLE`), never probabilities. No nutrient validator or clinical threshold was changed.

The programmatic review queue can approve or reject a reference, request more attributes, or leave an ingredient unresolved. Approval and rejection require reviewer identity and provenance, and approval is limited to a candidate present in the current local USDA results. No GUI was added.

## Implementation

- `src/recipes/ingredient_reference.py` — category normalization, source-attribute extraction, reviewed reference store, local-catalog ID checks, and context-aware default policy.
- `src/recipes/ingredient_identity_resolution.py` — ordered resolver, identity statuses, uncertainty metadata, and programmatic review queue.
- `src/graph/neo4j_graph.py` — additive `USDAIngredientReference` and `USDAIngredientReferenceReview` labels, read methods, explicit review writes, and review-history links. Reads do not create schema. Review writes do not modify ingredient mappings, USDA catalog food records, recipes, nutrients, or constraints.
- `src/runtime.py` — opt-in runtime factory; automatic exact verification is disabled by default.
- `tests/test_ingredient_identity_resolution.py` — deterministic identity, contradiction, uncertainty, persistence boundary, and review-queue cases.
- `tests/test_ingredient_reference_neo4j.py` — Neo4j read/write boundary and additive-label checks.
- `README.md` — Step 4.7 behavior and opt-in usage notes.

## Validation

- Focused Step 4.7 tests: **31 passed**.
- Full suite: `conda run --no-capture-output -n fyp-local python -m unittest discover -s tests` — **156 passed**.
- Live Neo4j evaluation: completed read-only retrieval and identity resolution for all nine categories. The run explicitly used `persist=False` and `auto_verify_exact=False`; no mapping, reference, schema, or approval writes were made. The SLM was not available for these evaluations.
- `git diff --check` and `git status` could not run because the system Git launcher requires acceptance of the Xcode license. This did not affect Python tests or the Neo4j evaluation.

## Live local-catalog evaluation

No preferred reference records were present for any category. No default was eligible, no reference approval was recorded, and all nine cases remain for human review. Candidate lists below are the bounded top-five results returned by the current local USDA catalog.

| Query / category | Local candidate IDs and descriptions | Resolution |
|---|---|---|
| `salt` | 173468 Salt, table; 746775 Salt, table, iodized | `AMBIGUOUS`; no preferred reference; review required |
| `eggs` | 748967 Eggs, Grade A, Large, egg whole; 173423 Egg, whole, cooked, fried; 172186 Egg, whole, cooked, poached; 171287 Egg, whole, raw, fresh; 329490 Egg, whole, dried | `AMBIGUOUS`; no preferred reference; review required |
| `butter` | 173410 Butter, salted; 173430 Butter, without salt; 790508 Butter, stick, salted; 789828 Butter, stick, unsalted; 173411 Butter, whipped, with salt | `AMBIGUOUS`; no preferred reference; review required |
| `sugar` | 170674 Sugar, turbinado | `REVIEW_REQUIRED`; a lone retrieved candidate was not treated as generic sugar identity |
| `milk` | 170882 Milk, sheep, fluid; 171280 Milk, indian buffalo, fluid; 170875 Milk, low sodium, fluid; 170910 Milk, chocolate, lowfat, reduced sugar; 171266 Milk, producer, fluid, 3.7% milkfat | `AMBIGUOUS`; species/fat/flavor distinctions remain unresolved; review required |
| `olive oil` | 171413 Oil, olive, salad or cooking; 748608 Oil, olive, extra virgin; 1750351 Oil, olive, extra light | `AMBIGUOUS`; processing distinctions remain unresolved; review required |
| `raw chicken breast` | 2646170 Chicken, breast, boneless, skinless, raw; 2727569 Chicken, breast, meat and skin, raw; 171474 Chicken, broilers or fryers, breast, meat and skin, raw; 171077 Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw; 171509 Chicken, broilers or fryers, breast, skinless, boneless, meat only, with added solution, raw | `AMBIGUOUS`; skin, bone, composition, and added-solution details distinguish candidates |
| `salmon` | 168023 Fish, salmon, tipnuk, fermented (Alaska Native); 167647 Fish, salmon, king, with skin, kippered, (Alaska Native); 167642 Fish, salmon, red, canned, bones removed (Alaska Native); 175138 Fish, salmon, pink, raw; 173691 Fish, salmon, sockeye, raw | `AMBIGUOUS`; species and processing distinctions remain unresolved |
| `cooked rice` | 168897 Wild rice, cooked; 169711 Rice, white, glutinous, unenriched, cooked; 173263 Rice, brown, parboiled, cooked, UNCLE BENS; 168932 Rice, white, short-grain, cooked, unenriched; 168930 Rice, white, medium-grain, cooked, unenriched | `AMBIGUOUS`; variety/grain distinctions remain unresolved |

The live run confirms the catalog has usable candidates for each category, but does not provide reviewer-approved defaults. Generic sugar is a useful retrieval-quality warning: the current candidate list has one result, turbinado sugar, yet deterministic identity resolution correctly leaves it for review rather than assuming that it represents generic sugar.

## Limitations and Step 5 readiness

The live database does not yet contain the reference labels or records, so live execution could validate candidate retrieval and absence of references, but could not exercise an approved reference against live data. Reference approval, rejection, attribute-request, and unresolved-action paths are covered by deterministic tests and mocked Neo4j persistence tests. No human reviewer identity or approval was fabricated.

**Step 5 is not ready to consume reference defaults.** All nine live categories remain ambiguous or review-required, with zero approved references and zero eligible defaults. The resolver can be integrated later as an explicit identity signal, but this Step 4.7 change does not authorize recipe adaptation to treat assumptions as verified identity or as constraint satisfaction.

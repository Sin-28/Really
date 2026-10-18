# Initial USDA Reference Approval Proposal

Reference-policy version: 2
Source Step 4.8 manifest policy version: 1
Candidates reviewed: 9
Ready for human approval: 9; requiring scope refinement: 0; insufficient nutrient data: 0; not recommended: 0.
Actual approvals: 0. Persistent database writes: 0.
Pre-existing approved references for the selected FDC IDs: 0.

These are recommendations for a human reviewer. No entry is approved, and none may be used as a generic default unless a reviewer explicitly grants that permission in a later decision.
Nutrients use local USDA FDC IDs 1093 (sodium), 1092 (potassium), 1091 (phosphorus), and 1003 (protein). Values are per 100 g. Recorded zero remains distinct from a missing measurement.

## 1. Ready for human approval

### Salt — FDC 173468

- USDA description: Salt, table
- Dataset: SR Legacy (`sr_legacy`)
- Nutrients per 100 g: sodium 38800 mg; potassium 8 mg; phosphorus 0 mg; protein 0 g.
- Proposed reference scope: table salt.
- Scope attributes: `{'salt_type': 'table'}`.
- Attributes missing from the USDA description: none.
- Scope attributes deliberately left unspecified: none.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local SR Legacy record explicitly identifies table salt and contains all four required nutrients.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Egg — FDC 171287

- USDA description: Egg, whole, raw, fresh
- Dataset: SR Legacy (`sr_legacy`)
- Nutrients per 100 g: sodium 142 mg; potassium 138 mg; phosphorus 198 mg; protein 12.6 g.
- Proposed reference scope: raw whole egg with species left unspecified.
- Scope attributes: `{'egg_part': 'whole', 'species': 'unspecified', 'preparation_state': 'raw'}`.
- Attributes missing from the USDA description: species.
- Scope attributes deliberately left unspecified: species.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local SR Legacy record identifies whole, raw egg but does not state species; the proposed scope preserves that uncertainty.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Butter — FDC 173410

- USDA description: Butter, salted
- Dataset: SR Legacy (`sr_legacy`)
- Nutrients per 100 g: sodium 643 mg; potassium 24 mg; phosphorus 24 mg; protein 0.85 g.
- Proposed reference scope: salted dairy butter.
- Scope attributes: `{'saltedness': 'salted', 'processing_state': 'unspecified'}`.
- Attributes missing from the USDA description: processing_state.
- Scope attributes deliberately left unspecified: processing_state.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The source explicitly identifies salted butter. Its processing form is not stated and remains unspecified.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Butter — FDC 173430

- USDA description: Butter, without salt
- Dataset: SR Legacy (`sr_legacy`)
- Nutrients per 100 g: sodium 11 mg; potassium 24 mg; phosphorus 24 mg; protein 0.85 g.
- Proposed reference scope: unsalted dairy butter.
- Scope attributes: `{'saltedness': 'unsalted', 'processing_state': 'unspecified'}`.
- Attributes missing from the USDA description: processing_state.
- Scope attributes deliberately left unspecified: processing_state.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The source explicitly identifies butter without salt. Its processing form is not stated and remains unspecified.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Sugar — FDC 169655

- USDA description: Sugars, granulated
- Dataset: SR Legacy (`sr_legacy`)
- Nutrients per 100 g: sodium 1 mg; potassium 2 mg; phosphorus 0 mg; protein 0 g.
- Proposed reference scope: granulated sugar.
- Scope attributes: `{'sugar_type': 'granulated'}`.
- Attributes missing from the USDA description: none.
- Scope attributes deliberately left unspecified: none.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local SR Legacy record explicitly identifies granulated sugar and contains all four required nutrients.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Olive Oil — FDC 171413

- USDA description: Oil, olive, salad or cooking
- Dataset: SR Legacy (`sr_legacy`)
- Nutrients per 100 g: sodium 2 mg; potassium 1 mg; phosphorus 0 mg; protein 0 g.
- Proposed reference scope: olive oil described as salad or cooking oil, with processing unspecified.
- Scope attributes: `{'oil_type': 'olive', 'processing_state': 'unspecified'}`.
- Attributes missing from the USDA description: processing_state.
- Scope attributes deliberately left unspecified: processing_state.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local SR Legacy record is nutrient-complete but does not state all processing attributes; the narrower scope preserves that uncertainty.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Chicken Breast — FDC 2646170

- USDA description: Chicken, breast, boneless, skinless, raw
- Dataset: Foundation (`foundation_foods`)
- Nutrients per 100 g: sodium 65.8 mg; potassium 330 mg; phosphorus 215 mg; protein 22.5 g.
- Proposed reference scope: raw boneless skinless chicken breast with solution status unspecified.
- Scope attributes: `{'preparation_state': 'raw', 'skin_state': 'skinless', 'bone_state': 'boneless', 'added_solution': 'unspecified'}`.
- Attributes missing from the USDA description: added_solution.
- Scope attributes deliberately left unspecified: added_solution.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local Foundation record establishes raw, boneless, skinless breast. It does not state solution status, so the proposal does not assume added_solution=no.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Salmon — FDC 2684441

- USDA description: Fish, salmon, Atlantic, farm raised, raw
- Dataset: Foundation (`foundation_foods`)
- Nutrients per 100 g: sodium 49.5 mg; potassium 378 mg; phosphorus 230 mg; protein 20.3 g.
- Proposed reference scope: raw farm-raised Atlantic salmon.
- Scope attributes: `{'species': 'atlantic', 'farm_raised': 'yes', 'preparation_state': 'raw', 'processing_state': 'unspecified'}`.
- Attributes missing from the USDA description: processing_state.
- Scope attributes deliberately left unspecified: processing_state.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local Foundation record states Atlantic, farm-raised, and raw. Farm-raising is preserved as a distinct identity attribute; processing beyond the stated raw form remains unspecified.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

### Rice — FDC 2512381

- USDA description: Rice, white, long grain, unenriched, raw
- Dataset: Foundation (`foundation_foods`)
- Nutrients per 100 g: sodium 0.462 mg; potassium 82.3 mg; phosphorus 108 mg; protein 7.04 g.
- Proposed reference scope: raw unenriched white long-grain rice.
- Scope attributes: `{'variety': 'white', 'grain': 'long', 'enrichment_state': 'unenriched', 'preparation_state': 'raw'}`.
- Attributes missing from the USDA description: none.
- Scope attributes deliberately left unspecified: none.
- Nutritional completeness: COMPLETE.
- Recommendation: `READY_FOR_HUMAN_APPROVAL`. The local Foundation record states white, long-grain, unenriched, and raw. Cooking state and enrichment are retained as identity attributes.
- Required human decision: Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used as a default; generic use remains disabled unless the reviewer separately opts in.
- Decision: `PENDING_HUMAN_CONFIRMATION`.

## 2. Requires scope refinement

None.

## 3. Requires additional nutrient data

None.

## 4. Not recommended

None.

## Nutritional and identity comparisons

### Butter

| FDC ID | Dataset | USDA description | Sodium mg | Potassium mg | Phosphorus mg | Protein g | Completeness |
|---:|---|---|---:|---:|---:|---:|---|
| 173410 | SR Legacy | Butter, salted | 643 | 24 | 24 | 0.85 | complete |
| 173430 | SR Legacy | Butter, without salt | 11 | 24 | 24 | 0.85 | complete |

### Salmon

| FDC ID | Dataset | USDA description | Sodium mg | Potassium mg | Phosphorus mg | Protein g | Completeness |
|---:|---|---|---:|---:|---:|---:|---|
| 2684441 | Foundation | Fish, salmon, Atlantic, farm raised, raw | 49.5 | 378 | 230 | 20.3 | complete |
| 173686 | SR Legacy | Fish, salmon, Atlantic, wild, raw | 44 | 490 | 200 | 19.8 | complete |

### Chicken Breast

| FDC ID | Dataset | USDA description | Sodium mg | Potassium mg | Phosphorus mg | Protein g | Completeness |
|---:|---|---|---:|---:|---:|---:|---|
| 2646170 | Foundation | Chicken, breast, boneless, skinless, raw | 65.8 | 330 | 215 | 22.5 | complete |
| 171509 | SR Legacy | Chicken, broilers or fryers, breast, skinless, boneless, meat only, with added solution, raw | 173 | 332 | 198 | 20.3 | complete |

### Rice

| FDC ID | Dataset | USDA description | Sodium mg | Potassium mg | Phosphorus mg | Protein g | Completeness |
|---:|---|---|---:|---:|---:|---:|---|
| 2512381 | Foundation | Rice, white, long grain, unenriched, raw | 0.462 | 82.3 | 108 | 7.04 | complete |
| 169757 | SR Legacy | Rice, white, long-grain, regular, unenriched, cooked without salt | 1 | 35 | 43 | 2.69 | complete |

### Olive Oil

| FDC ID | Dataset | USDA description | Sodium mg | Potassium mg | Phosphorus mg | Protein g | Completeness |
|---:|---|---|---:|---:|---:|---:|---|
| 171413 | SR Legacy | Oil, olive, salad or cooking | 2 | 1 | 0 | 0 | complete |
| 748608 | Foundation | Oil, olive, extra virgin | — | — | — | — | incomplete |
| 1750351 | Foundation | Oil, olive, extra light | — | — | — | — | incomplete |

- **Butter:** salted and unsalted records contain 643 mg and 11 mg sodium per 100 g (difference 632 mg); retain separate references.
- **Salmon:** farm-raised Atlantic vs wild Atlantic records have potassium 378 mg vs 490 mg, phosphorus 230 mg vs 200 mg, and protein 20.3 g vs 19.8 g. Preserve farm-raised status; these records also come from different datasets, so this is not a controlled causal comparison.
- **Chicken breast:** the raw added-solution alternative has 173 mg sodium per 100 g versus 65.8 mg for the selected record. The selected USDA description does not establish solution status; it stays unspecified and cannot match an explicit added-solution ingredient. The records are from different USDA datasets, so the difference is not attributable only to solution treatment.
- **Rice:** compare the selected raw, unenriched white long-grain record with the local cooked, unenriched white long-grain record. Their per-100-g values reflect different water content and source datasets; never reuse one preparation state as the other.
- **Olive oil:** the SR Legacy salad/cooking record has all four required nutrients. The local Foundation extra-virgin and extra-light records have all four nutrients missing. The incomplete Foundation records are not preferred just because they are Foundation; the selected SR Legacy scope leaves processing unspecified and is not an exact substitute for either explicit Foundation variant.

## Decision and implementation boundary

The nine machine-readable entries preserve the original Step 4.8 review-entry IDs and carry `decision: PENDING_HUMAN_CONFIRMATION`. Reviewer identity, approval timestamp, and approval provenance are null. `allow_as_default` is false for every entry.
A reviewer must confirm the exact FDC ID and scope, then separately decide whether generic default use is permitted. These references remain separate from IngredientMapping and never create or overwrite a VERIFIED mapping.

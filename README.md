# RenAlly prototype v3

RenAlly uses Neo4j as the source of truth for recipe relationships and the loaded USDA FoodData Central food datasets. The SLM classifies each request, resolves conversational references, and selects a dataset scope before graph retrieval. Neo4j retrieval, ingredient selection, nutrient arithmetic, and numeric constraint checks remain explicit and auditable.

## Graph model

- `(:Recipe)-[:CONTAINS {amount_g, position}]->(:FoundationFood)` records curated recipe ingredients and their per-serving amounts.
- `(:FoodRecord)-[:HAS_NUTRIENT {amount, unit, basis}]->(:Nutrient)` stores each source record's nutrient measurements, FDC nutrient ID, and per-100-g basis.
- `(:FoodRecord)-[:IS_A]->(:IngredientType)` records USDA food categories and curated Foundation Foods ingredient types.
- `(:FoundationFood)-[:SUBSTITUTE_FOR]->(:FoundationFood)` contains the prototype's reviewed substitution fixtures.
- `(:FoodRecord)-[:FROM_RELEASE]->(:FoodDataRelease)` preserves each record's USDA dataset and release.

Neo4j full-text indexes retrieve recipes and all loaded food records. The SLM supplies the dataset scope; an unspecified scope searches both Foundation Foods and SR Legacy. Separate FDC IDs, source labels, and release metadata are retained, and the app never merges duplicate food records or nutrient values. The application reads connected recipe ingredients, nutrient records, categories, and substitutions from graph relationships. It does not ask the SLM to invent Cypher or nutrient values.

## Data scope

The supplied April 30, 2026 Foundation Foods file contains 363 valid food records in this local copy; the importer reports and skips non-record `null` entries. It loads every valid Foundation Foods record and its numeric nutrient measurements, with FDC IDs, food state, units, release date, and source URLs. SR Legacy can be loaded from USDA's April 2018 release and is identified separately as historical source data. Missing nutrient amounts stay missing; explicit zero values stay zero.

The current 55 curated Foundation Foods entries contribute prototype aliases, types, substitution groups, and portion ranges. Other records stay searchable by their USDA descriptions and food categories. Recipe compositions remain linked to curated Foundation Foods; SR Legacy records are not automatically substituted into recipe compositions.

Food.com recipe discovery uses the expanded [Food.com Recipes and Reviews dataset](https://www.kaggle.com/datasets/irkaal/foodcom-recipes-and-reviews). Download `recipes.csv` from Kaggle, extract it from `recipes.zip`, and import it with `python scripts/import_foodcom_recipes.py <path-to-recipes.csv>`. A full import replaces the previous Food.com catalog after processing completes. Source ingredient quantities and serving counts remain verbatim. Each ingredient item also includes parsed quantity fields in `details_json`: weight units convert to grams, ranges keep both endpoints, and volume/count measures stay unresolved unless an exact, source-labelled ingredient factor is supplied to the parser. These parsed amounts are not linked to USDA foods or used in nutrient calculations.

## USDA ingredient candidates and mapping cache

`src/recipes/ingredient_mapping.py` accepts a Step 1 parsed ingredient item and uses the existing Neo4j full-text indexes and `GraphRetriever` to find local Foundation Foods and SR Legacy candidates. A bounded deterministic reranker prioritizes exact identity and approved aliases, then checks the primary food name, explicit variety, preparation and processing state, lexical overlap, and the original Neo4j score. Clear category conflicts are excluded, including prepared foods that merely contain salt or foods packed in olive oil. Candidate records expose the legacy resolver score, raw `neo4j_retrieval_score`, separate `relevance_score`, feature evidence, and `retrieval_version`. These ranking values are evidence for ordering, not confidence probabilities or nutrition equivalence. Generic queries keep relevant variants visible and unresolved. No API, embedding model, or new database is added.

Candidate and status records can be stored as `(:IngredientMapping)` nodes in Neo4j. The cache key includes normalized name, explicit state/cut attributes, dataset scope, and mapping version. A compatible verified record is reused; unverified candidate sets are refreshed on lookup. The historical `verify_candidate` API remains for older audited data workflows, but the web ingredient-resolution flow has no human-approval action. Deterministic policy verification is separate from contextual SLM resolution. Retrieval scores never verify a food. Ranking changes bump `INGREDIENT_RETRIEVAL_VERSION`; mapping semantics changes require `MAPPING_VERSION` review.

```python
from src.recipes.ingredient_mapping import USDAIngredientMapper

mapper = USDAIngredientMapper(kg, retriever)
result = mapper.retrieve_candidates(parsed_ingredient_item)
```

### SLM candidate validation (Step 3)

`src/recipes/ingredient_mapping_validator.py` evaluates only the USDA candidates returned by the Step 2 mapper. It reuses the optional local `QwenRecipeGenerator` (`Qwen/Qwen3-1.7B`, overridden with `RENALLY_QWEN_MODEL`) and its chat-template inference and JSON-object decoding. The model is imported and loaded only when a non-empty candidate set is evaluated. Inference has a 45-second default timeout; unavailable, malformed, or timed-out inference returns `REVIEW_REQUIRED` without selecting a food. A timed-out local generation call cannot be interrupted; additional evaluations return `REVIEW_REQUIRED` until that call finishes.

The validator sends the original ingredient, normalized name, parsed quantity/unit, attributes, recipe name and method, other ingredients, and retrieved candidate IDs/descriptions to Qwen. It requires JSON with `RECOMMENDED`, `AMBIGUOUS`, `REJECTED`, or `REVIEW_REQUIRED`. Programmatic checks reject invalid IDs, explicit state/species/oil conflicts, and unsupported pepper colour, milk fat, cream, flour, onion, and chilli variants. A clear recommendation can become `SLM_RESOLVED` for that recipe context; this does not change the persistent mapping to `VERIFIED`. The legacy model field `requires_review` indicates unresolved evidence; it no longer triggers a human mapping approval step.

```python
from src.recipes.ingredient_mapping_validator import SLMIngredientMappingValidator

evaluation = SLMIngredientMappingValidator(kg).evaluate(parsed_ingredient_item, result)
```

The standalone validator can persist an evaluation audit record in `slm_evaluation_json`, but recipe-context resolutions use `persist=False`. The ingredient cache key does not include recipe context, so a model choice from one recipe is not reused in another. The web service shares its existing lazy Qwen instance with ingredient disambiguation. Missing or timed-out Qwen inference leaves the ingredient unresolved.

### Conservative automatic mapping and offline pre-mapping (Step 4)

`src/recipes/automatic_mapping_policy.py` accepts exactly one candidate only when its complete normalized label-token set or a curated exact alias matches the ingredient and the explicit preparation, species, and variety checks pass. Candidate rank is ignored. Generic salmon and generic oil stay unresolved when the USDA record adds an unsupported species or variety. An SLM recommendation cannot make a non-matching candidate eligible; a conflicting or uncertain SLM result blocks automatic verification. If inference is unavailable or times out, a deterministic match may still qualify and the failure is included in the report.

The automatic path is explicit and separate from human review. It stores `verification_method="DETERMINISTIC_POLICY"`, `verification_policy_version`, and policy evidence; reviewer fields remain empty. Candidate descriptions that add unstated variety or composition details (such as milk fat percentage or chicken skin/bone state) remain unresolved even when a curated alias matches. Existing human-reviewed mappings are preserved. On a policy version change, prior automatic records are retrieved and re-evaluated. Provenance is upgraded only when the same FDC ID remains eligible; a changed candidate set is left for review. Candidate, SLM, and policy evaluations remain separate cache evidence.

`IngredientPremappingWorkflow` scans Food.com recipe details in bounded, keyset-paginated reads, groups duplicate ingredients by the Step 2 context-aware cache key, counts occurrences, and processes frequent ingredients first. Dry-run is the default. It reads the graph and can write a local checkpoint, but does not create the mapping constraint or write candidate, SLM, policy, or verification data. Each invocation is capped at 1,000 recipes scanned, 25 unique ingredient keys selected, and 5 keys processed; the workflow does not start a full-catalog batch. A checkpoint resumes scanning and mapping in later invocations without repeating completed keys. Switching a dry-run checkpoint to persistent mode reprocesses those keys once; later persistent resumes are idempotent.

```bash
# Read-only sample; writes only the optional local checkpoint and JSON report.
python scripts/premap_foodcom_ingredients.py \
  --max-recipes 100 --max-ingredients 10 --batch-size 3 \
  --checkpoint /tmp/renally-foodcom-premapping.json

# Explicitly persist one small batch; Qwen is still optional.
python scripts/premap_foodcom_ingredients.py --write \
  --max-recipes 100 --max-ingredients 10 --batch-size 3 \
  --checkpoint /tmp/renally-foodcom-premapping.json
```

Add `--use-slm` to evaluate candidates with the configured local Qwen model. The report separates candidate retrieval coverage from verified mapping coverage and includes ingredient frequency, verified-cache hits, automatic mappings, human-reviewed cache reuse, ambiguity/unresolved counts, retrieval and SLM failures, and elapsed time. Persistent mapping requires the CLI `--write` flag plus the workflow's write-enabled mode. This offline tool is not connected to recipe retrieval or adaptation.

### Ingredient identity resolution and reviewed references (Step 4.7)

`src/recipes/ingredient_identity_resolution.py` keeps the recipe-described ingredient separate from the USDA record chosen as a calculation basis. It first uses an existing compatible verified mapping or exact deterministic identity, then accepts a guarded Qwen choice as `SLM_RESOLVED` when one current candidate remains supported. If Qwen identifies a family but not a specific variant, `FAMILY_RESOLVED` carries plausible FDC IDs and no selected ID. Otherwise the result is `AMBIGUOUS` or `UNRESOLVED`. These statuses do not change the persistent mapping `VERIFIED` status. Existing approved references remain available under their separate policy.

`src/recipes/ingredient_reference.py` stores preferred references separately as `(:USDAIngredientReference)` nodes. Review decisions and queue actions are appended to `(:USDAIngredientReferenceReview)` nodes. Approval requires an existing locally imported FDC ID, a reviewer identity, provenance, explicit attributes, and an explicit `allow_as_default` boolean. Rejections update the current reference status and retain the review event, so rejected references are excluded from lookup. `REFERENCE_POLICY_VERSION` gates reuse; old-policy records remain stored but are ignored until explicitly re-reviewed.

`ReferenceDefaultPolicy` checks ingredient category and explicit state/species/variety or other attributes against the reviewed reference. Missing attributes can be filled only when a current approved reference explicitly permits default use. A `REFERENCE_DEFAULT` result includes the selected local FDC ID, assumed and unresolved attributes, nutritional certainty, and a confirmation requirement. It never creates or changes an `IngredientMapping`, never sets mapping status to `VERIFIED`, and does not confirm kidney-related constraint satisfaction. No reviewed reference approvals are seeded by this implementation.

`IngredientReferenceReviewQueue` builds structured review items with candidate records, SLM evidence when available, ambiguity reasons, missing attributes, and available references. Its programmatic actions support reference approval/rejection, requesting more attributes, and leaving an item unresolved; review actions require an identified reviewer and are auditable. `create_ingredient_identity_runtime` exposes these opt-in services without connecting them to recipe adaptation or changing the nutrient constraint validator. The resolver is read-only by default; exact automatic verification is a separate explicit option and reference defaults never use that path.

### USDA reference review packet (Step 4.8)

`python scripts/review_usda_references.py generate` performs bounded searches over targeted ingredient descriptions and the existing Neo4j USDA full-text index, then fetches nutrient rows by FDC nutrient ID. It writes a `PENDING_REVIEW` JSON manifest and Markdown comparison report under `reports/`; generation does not write to Neo4j or approve candidates. The manifest retains source nutrient IDs, USDA names, original units, and the imported per-100-g basis. Missing nutrient rows stay missing.

The same script provides an explicit decision interface. `decide` is a read-only preview unless `--confirm-persist` is supplied. A confirmed approval or rejection must identify exactly one pending manifest entry, pass local catalog ID validation, provide reviewer identity and provenance, and then is stored through `PreferredUSDAReferenceManager`; the result is read back before the manifest is updated. Approval additionally requires reviewer-confirmed attributes. `--allow-as-default` is opt-in and does not turn a reference into a VERIFIED ingredient mapping.

```bash
python scripts/review_usda_references.py generate
python scripts/review_usda_references.py decide \
  --manifest data/reviews/usda_initial_reference_decisions.json \
  --category milk --fdc-id "REPLACE_WITH_REVIEWED_FDC_ID" --decision approve \
  --reviewer-identity "<human reviewer>" \
  --provenance-json '{"source":"<review evidence>"}' \
  --attributes-json '{"species":"cow","fat_content":"3.25%","processing_state":"fluid"}' \
  --confirm-persist
```

Replace every placeholder with an actual reviewer decision and evidence. Never run the confirmed command merely because a candidate is present in the manifest.

### Initial reference selection proposal (Step 4.8A)

`python scripts/build_usda_initial_reference_proposal.py` verifies the nine named FDC records against the local catalog and creates `data/reviews/usda_initial_reference_approval_proposal.md` plus `data/reviews/usda_initial_reference_decisions.json`. It reads comparison records and current references but does not write to Neo4j or alter the original Step 4.8 manifest. The machine-readable proposal preserves each Step 4.8 `review_entry_id`; all decisions start at `PENDING_HUMAN_CONFIRMATION`.

The current reference-policy version distinguishes farm-raised salmon from wild salmon and enriched rice from unenriched rice. Older approved reference records remain stored but are ignored by the newer policy until reviewed under it. Use the Step 4.8A decisions proposal for later CLI decisions; the older Step 4.8 manifest is retained unchanged as source evidence.

### Hybrid recipe ingredient mapping (Step 5A)

`src/recipes/hybrid_ingredient_mapping.py` composes the existing Step 1 parser, context-aware USDA mapping cache and retriever, deterministic mapping policy, optional SLM validator, approved-reference resolver, and checkpointed offline pre-mapping workflow. The chat invokes read-only contextual mapping after a Food.com source recipe is retrieved. Other recipe enrichment remains optional. Mapping details are returned in `ingredient_mapping_enrichment`; original source ingredient fields are preserved.

Food.com ingredient identity resolution runs in the chat response when a recipe is retrieved. Up to 20 source ingredient rows are evaluated per response; repeated ingredient contexts share a lookup. Exact matches skip Qwen, while ambiguous candidates use the shared local Qwen model. The chat displays specific USDA IDs, resolved families, or remaining candidates beside the source amount. This path has no human-approval action and performs no mapping writes. Source quantity validation remains separate, so Food.com nutrient totals and dietary checks remain unavailable. Run the focused tests with `python -m unittest discover -s tests -p 'test_contextual_ingredient_resolution.py'`; the full suite is `python -m unittest discover -s tests`.

Optional enrichment for other recipe flows is disabled by default; Food.com chat identity resolution runs automatically. Set `RENALLY_RECIPE_MAPPING_ENRICHMENT=true` to enable the other enrichment and `RENALLY_INGREDIENT_MAPPING_MODE=READ_ONLY|ON_DEMAND|OFFLINE` to select a mode. `READ_ONLY` is the default mode: it may read the mapping cache and approved references and retrieve bounded USDA candidates, but it does not create cache schema or write mapping/evaluation records. In `ON_DEMAND`, candidate/cache writes and eligible deterministic verification additionally require `RENALLY_INGREDIENT_MAPPING_PERSIST=true`; SLM use remains separately opt-in for optional enrichment. `OFFLINE` recipe responses are cache/reference-only; batch work runs only through the explicit `run_offline_batch` delegation to `IngredientPremappingWorkflow`, retaining its existing batch caps and checkpoint behavior. No background job is started.

Deterministic USDA nutrient enrichment is a separate optional feature. It is disabled by default; set `RENALLY_RECIPE_NUTRITION_ENRICHMENT=true` to attach `ingredient_nutrient_enrichment` metadata to retrieved recipe responses. It uses the existing hybrid mapping resolver in read-only mode when mapping enrichment is otherwise disabled, retrieves sodium, potassium, phosphorus, and protein from selected USDA records, and calculates recipe totals only when quantities, mappings, and nutrient measurements are complete. Volume and count units require a matching USDA FoodData Central portion reference; ambiguous mappings, mismatched quantity arrays, and missing values remain unresolved. Partial sums are labeled as subtotals and never replace the existing nutritional constraint results or adaptation behavior.

Informational recipe constraint validation is independently opt-in through `RENALLY_RECIPE_CONSTRAINT_ENRICHMENT=true`. It evaluates only the request's existing explicit meal or remaining-intake budgets against the USDA nutrient calculation and attaches a separate `ingredient_constraint_enrichment` result. It reports `PASS`, `FAIL`, `REVIEW_REQUIRED`, `UNKNOWN`, or `NOT_APPLICABLE`, validates units and serving basis, and may prove an upper-bound failure from a verified nonnegative subtotal. It does not apply daily profile guidance as a meal threshold or change legacy recipe evaluations, recommendations, or adaptation decisions. When this flag is enabled without the separate nutrient metadata flag, the nutrient calculation runs as its read-only dependency but is returned only inside the constraint evidence.

Adapted recipe nutrient validation is independently opt-in through `RENALLY_ADAPTED_RECIPE_NUTRITION_ENRICHMENT=true`. It attaches `adapted_recipe_nutrition_enrichment` to generated catalog candidates and explicit local-recipe composition edits, while leaving candidate ranking and legacy adaptation decisions unchanged. It compares structured ingredient identities and states, reuses only compatible current-version `VERIFIED_MAPPING` results, remaps other adapted ingredients through the existing hybrid resolver, and recalculates the final adapted ingredient list with the Step 5B-1 calculator and Step 5B-2 adapter. Adapted serving counts are used only when the adaptation representation supplies them; otherwise serving-dependent constraint results remain `UNKNOWN`. The flag is off by default and performs no added USDA retrieval or model inference while disabled.

Adapted recipe culinary review is separately opt-in through `RENALLY_ADAPTED_RECIPE_CULINARY_VALIDATION=true`. It adds `adapted_recipe_culinary_validation` metadata for the selected adaptation without changing ingredients, adaptation choices, candidate ranking, or nutritional results. Prompt `adapted-recipe-culinary-v8` uses one Qwen inference pass for per-change compatibility plus recipe-level functional coverage. Python identifies assessment candidates from explicit ingredient roles and narrow recipe-context cues (for example, seasoning components, herbs, eggs in baking, leaveners, thickeners, salt, fats, and cooking liquids); the model classifies their importance and coverage and can add other recipe-supported functions. Unclassified changes do not default to flavor-base coverage. Evidence uses one compact list of Python-assigned adapted ingredient and cooking-step references, which Python validates and separates in returned metadata. Additions support source-change assessments rather than owning source-function rows. Python validates exact change coverage, controlled enums, evidence references, and required function assessments; omitted required functions are preserved as `UNKNOWN` in review metadata. It assigns public function IDs and derives overall status and `requires_review`. Unsupported, partial, unknown, or contradictory essential-function evidence cannot produce `PASS`. A model-only missing essential function is `FAIL` only when the complete assessment also identifies an incoherent recipe; otherwise it requires review. Incomplete but parseable output becomes `REVIEW_REQUIRED` with schema errors; malformed output or inference failure is `UNAVAILABLE`; confirmed deterministic failures retain `FAIL` even when Qwen is unavailable. Recipes with no detected ingredient changes return `REVIEW_REQUIRED` because the schema does not evaluate instruction-only edits. Missing instructions and quantities stay missing. The flag is off by default, reuses the existing lazy local Qwen instance when enabled, bounds inference time and output length, and performs no database writes. Culinary results do not establish nutritional suitability or final recipe acceptance.

Final recipe acceptance is a separate opt-in layer through `RENALLY_FINAL_RECIPE_ACCEPTANCE=true` (default `false`). It adds `final_recipe_acceptance` metadata without changing legacy validation, candidate ranking, adaptation choices, or upstream results. The deterministic Step 6B policy consumes only existing Step 5C and Step 6A.3 outputs; enabling it alone does not trigger mapping, nutrient calculation, or Qwen inference. `ACCEPTED` requires an active constraint, nutritional `PASS`, culinary `PASS`, complete authorized mappings, resolved quantities, a valid per-serving nutrient basis, valid culinary evidence, and no critical missing information. Confirmed nutrition or culinary failures take precedence and return `REJECTED`; missing evidence returns `UNVERIFIED`; uncertain or assumption-dependent results return `REVIEW_REQUIRED`; no active constraint returns `NOT_APPLICABLE`. These statuses describe this implemented validation policy and do not mean clinically approved or guaranteed safe.

Step 6C's bounded evaluator is `scripts/evaluate_final_recipe_acceptance.py`. It evaluates 10 controlled final-acceptance cases and writes a 13-case `PENDING_REVIEW` culinary set to `data/culinary_human_review_dataset.json`. By default it also attempts a read-only sample of up to 10 imported Food.com recipes, scanning at most 200 source rows and mapping at most 20 ingredients per recipe, with mapping writes and SLM inference disabled. It writes detailed results to `reports/step_6c_evaluation_results.json`; the evaluation and environment limitations are summarized in `reports/step_6c_end_to_end_evaluation.md`. Run `python scripts/evaluate_final_recipe_acceptance.py --skip-foodcom` for controlled cases without a Neo4j connection.

Qwen selects MPS only when PyTorch reports it both built and available; otherwise it uses CPU and keeps the checkpoint's native dtype. MPS loading uses FP16 and runs a one-token generation probe before serving; a load or probe failure falls back to CPU. Culinary generation defaults to non-thinking chat-template mode, a 90-second Transformers generation deadline, and a token budget that starts at 448 for one change, grows by 128 tokens per additional change, and caps at 1024; the request wait is bounded at 105 seconds. The library cannot safely kill a thread while it is inside model prefill, so a timed-out call returns `UNAVAILABLE` and holds the shared inference gate until it exits. This prevents timed-out culinary requests from piling up. Safe stage diagnostics include device/dtype, prompt version, token counts, timings, timeout, JSON/schema validity, and final status; prompt text is not persisted.

Each enrichment result preserves source text and quantity status and reports the mapping resolution, FDC ID and description when resolved, verification provenance, candidate IDs, unresolved attributes, reference assumptions, nutritional uncertainty, and retrieval/validation errors. Deterministic policy eligibility without configured persistence remains `EXACT_IDENTITY`; guarded recipe-context Qwen choices are `SLM_RESOLVED`. Neither becomes a persistent `VERIFIED` mapping. Numeric nutrients still come only from the selected USDA record, and an unresolved family or quantity cannot produce a complete nutritional result.

Recipe rice amounts now use the Foundation Foods raw white-rice record and are measured dry before cooking. They are illustrative prototype portions, not USDA cooking-yield conversions. Cooking water, final cooked yield, and nutrient changes during cooking are excluded from totals. Tofu and generic instant noodles do not have matching records in the supplied Foundation Foods release, so recipes that require them remain marked incomplete until an explicit curated mapping is made. Loading SR Legacy does not automatically change those recipe links.

## Start Neo4j locally

The included Compose file runs a local Neo4j Community database bound to this computer. Docker Desktop is required. Neo4j Desktop also works; set the URI and credentials in `.env` to match that database.

```bash
cp .env.example .env
# Edit NEO4J_PASSWORD in .env before starting Neo4j.
docker compose up -d
```

Install the Neo4j Python driver in the Conda environment used to run RenAlly:

```bash
conda activate fyp
pip install -r requirements.txt
```

Import either or both USDA datasets with the prototype's recipe catalog. The importer is additive by default, so an SR Legacy import retains the existing Foundation Foods records. USDA records remain separate and carry their dataset, release, food state, source ID, URL, and per-100-g nutrient basis.

```bash
python scripts/import_foundation_neo4j.py \
  "/path/to/FoodData_Central_foundation_food_json_2026-04-30.json" \
  --sr-legacy-json "/path/to/FoodData_Central_sr_legacy_food_json_2018-04.json"
```

USDA publishes the SR Legacy JSON archive on its [download page](https://fdc.nal.usda.gov/download-datasets/). Unzip the April 2018 JSON archive before importing it. To add SR Legacy to an already populated graph, omit the Foundation Foods path and pass only `--sr-legacy-json`; the importer preserves existing records by default. Use `--replace-existing` only for an intentional full RenAlly graph rebuild.

Then run the local chat page:

```bash
python web_app.py
```

Open `http://127.0.0.1:8000`. The graph page reads its visualization from Neo4j. The CLI and evaluator use the same Neo4j database. To use Qwen, install `requirements-llm.txt`; the local model remains optional.

## Configuration

The app reads these keys from `.env` or the shell environment. Shell values take precedence.

| Key | Default |
|---|---|
| `NEO4J_URI` | `bolt://127.0.0.1:7687` |
| `NEO4J_USERNAME` | `neo4j` |
| `NEO4J_PASSWORD` | Required |
| `NEO4J_DATABASE` | `neo4j` |
| `RENALLY_QWEN_MODEL` | `Qwen/Qwen3-1.7B` |
| `RENALLY_QWEN_CPU_THREADS` | Up to 8 available CPU cores |
| `RENALLY_ADAPTED_RECIPE_CULINARY_VALIDATION` | `false` |
| `RENALLY_FINAL_RECIPE_ACCEPTANCE` | `false` |
| `RENALLY_QWEN_CULINARY_ENABLE_THINKING` | `false` |
| `RENALLY_QWEN_CULINARY_MAX_TIME_SECONDS` | `90` |
| `RENALLY_QWEN_CPU_THREADS` | `8` by default |

Re-run the importer after changing a USDA release or the recipe fixture data. Existing food records stay separated by source and are not used to auto-fill the curated Foundation Foods recipe links.

## Run the prototype

```bash
python main.py --query "Modify chicken rice for haemodialysis" --condition haemodialysis --meal-limit sodium=350 --servings 1
python main.py --condition haemodialysis --condition hypertension --remaining sodium=500
python evaluate.py
```

Limits supplied by `--meal-limit` or `--remaining` are explicit user inputs. Clinical profile references are contextual and are not converted into meal thresholds. Recipe ingredient grams are per serving; nutrient totals are deterministic sums of linked Foundation Foods values. Unknown ingredients or missing nutrients remain unverified rather than being treated as zero.

## Prototype limits

The supplied 60-case evaluation set does not provide complete patient-specific nutrient budgets or prior-intake totals for every case. The evaluation therefore reports catalog coverage and engineering checks; it is not a clinical validation. Recipe compositions and substitution compatibility remain prototype fixtures that require human review. The output is not a personalized medical plan.

import copy
import unittest

from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy
from src.recipes.ingredient_identity_resolution import (
    IngredientIdentityResolver,
    IngredientReferenceReviewQueue,
)
from src.recipes.ingredient_parser import parse_ingredient_text
from src.recipes.ingredient_reference import (
    REFERENCE_POLICY_VERSION,
    PreferredUSDAReferenceManager,
    ReferenceDefaultPolicy,
    canonical_ingredient_name,
    extract_explicit_ingredient_attributes,
)


def _food(fdc_id, description, *, food_record_id=None, dataset="foundation_foods"):
    food_record_id = food_record_id or f"fdc:{fdc_id}"
    data_type = "Foundation" if dataset == "foundation_foods" else "SR Legacy"
    return food_record_id, {
        "name": description,
        "fdc_description": description,
        "food_state": description,
        "aliases": [],
        "source": {
            "fdc_id": fdc_id, "dataset_key": dataset,
            "data_type": data_type,
        },
    }


class MemoryReferenceStore:
    """Mocked reference persistence; unit tests never write to production Neo4j."""

    def __init__(self, foods=()):
        self.ingredients = dict(foods)
        self.references = {}
        self.review_events = []
        self.write_calls = []

    def list_ingredient_references(self, canonical_ingredient_name=None):
        return [copy.deepcopy(row) for row in self.references.values()
                if canonical_ingredient_name is None
                or row.get("canonical_ingredient_name") == canonical_ingredient_name]

    def list_ingredient_reference_reviews(self, reference_id=None):
        return [copy.deepcopy(event) for event in self.review_events
                if reference_id is None or event.get("reference_id") == reference_id]

    def save_ingredient_reference_decision(self, reference, review_event):
        self.write_calls.append("reference_decision")
        previous = self.references.get(reference["reference_id"])
        stored = {**(previous or {}), **copy.deepcopy(reference)}
        stored.setdefault("created_at", reference.get("updated_at"))
        stored["review_revision"] = int((previous or {}).get("review_revision", 0)) + 1
        self.references[reference["reference_id"]] = stored
        self.review_events.append(copy.deepcopy(review_event))
        return copy.deepcopy(stored)

    def save_ingredient_reference_review_action(self, event):
        self.write_calls.append("review_action")
        self.review_events.append(copy.deepcopy(event))
        return copy.deepcopy(event)


class StubMapper:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.automatic_verifications = []

    def retrieve_candidates(self, ingredient, **kwargs):
        self.calls.append((ingredient, kwargs))
        return copy.deepcopy(self.result)

    def verify_candidate_automatically(self, ingredient, mapping_result, policy, **kwargs):
        self.automatic_verifications.append((ingredient, mapping_result, kwargs))
        candidate = mapping_result["candidates"][0]
        return {
            "status": "VERIFIED", "fdc_id": candidate["fdc_id"],
            "description": candidate["description"],
            "verification_method": "DETERMINISTIC_POLICY",
        }


def _candidate(fdc_id, description, *, aliases=(), record_id=None):
    return {
        "fdc_id": fdc_id,
        "food_record_id": record_id or f"fdc:{fdc_id}",
        "description": description,
        "food_state": description,
        "dataset_key": "foundation_foods",
        "data_type": "Foundation",
        "approved_aliases": list(aliases),
    }


def _mapping(candidates, status=None, **extra):
    return {
        "status": status or ("AMBIGUOUS" if len(candidates) > 1 else "CANDIDATE" if candidates else "UNRESOLVED"),
        "normalized_name": "fixture ingredient",
        "mapping_version": "fdc-mapping-v1",
        "retrieval_version": "ingredient-rerank-v4",
        "cache_key": "test-cache-key",
        "candidates": list(candidates),
        **extra,
    }


class IngredientIdentityResolutionTests(unittest.TestCase):
    def test_explicit_human_milk_and_maple_sugar_attributes_are_extracted(self):
        self.assertEqual(
            extract_explicit_ingredient_attributes({"ingredient_name": "human milk"})["species"],
            "human",
        )
        self.assertEqual(
            extract_explicit_ingredient_attributes({"ingredient_name": "maple sugar"})["sugar_type"],
            "maple",
        )

    def setUp(self):
        foods = [
            _food(173468, "Salt, table"),
            _food(171287, "Egg, whole, raw, fresh", dataset="sr_legacy"),
            _food(748967, "Eggs, Grade A, Large, egg whole"),
            _food(173430, "Butter, without salt"),
            _food(170674, "Sugar, turbinado"),
            _food(171266, "Milk, producer, fluid, 3.7% milkfat"),
            _food(748608, "Oil, olive, extra virgin"),
            _food(2646170, "Chicken, breast, boneless, skinless, raw"),
            _food(173691, "Fish, salmon, sockeye, raw"),
            _food(168897, "Wild rice, cooked"),
            _food(170882, "Milk, sheep, fluid"),
            _food(173410, "Butter, salted"),
            _food(169655, "Sugars, granulated", dataset="sr_legacy"),
            _food(171413, "Oil, olive, salad or cooking", dataset="sr_legacy"),
            _food(2646170, "Chicken, breast, boneless, skinless, raw"),
            _food(171509, "Chicken, broilers or fryers, breast, skinless, boneless, meat only, with added solution, raw", dataset="sr_legacy"),
            _food(2684441, "Fish, salmon, Atlantic, farm raised, raw"),
            _food(173686, "Fish, salmon, Atlantic, wild, raw", dataset="sr_legacy"),
            _food(2512381, "Rice, white, long grain, unenriched, raw"),
            _food(169757, "Rice, white, long-grain, regular, unenriched, cooked without salt", dataset="sr_legacy"),
            _food(168880, "Rice, white, medium-grain, cooked"),
        ]
        self.store = MemoryReferenceStore(foods)
        self.references = PreferredUSDAReferenceManager(self.store)
        self.reference_policy = ReferenceDefaultPolicy()

    def approve(self, ingredient, fdc_id, attributes, *, allow_as_default=False):
        return self.references.approve_reference(
            ingredient, fdc_id, reviewer_identity="test-reviewer",
            provenance={"source": "deterministic unit-test review fixture"},
            attributes=attributes, allow_as_default=allow_as_default,
        )

    def resolver(self, mapping, *, allow_reference_defaults=True, auto_verify_exact=False):
        return IngredientIdentityResolver(
            StubMapper(mapping), self.references,
            mapping_policy=AutomaticIngredientMappingPolicy(),
            reference_policy=self.reference_policy,
            allow_reference_defaults=allow_reference_defaults,
            auto_verify_exact=auto_verify_exact,
        )

    def test_exact_ingredient_identity_is_distinct_from_verified_mapping(self):
        label = "Milk, producer, fluid, 3.7% milkfat"
        candidate = _candidate(171266, label)
        result = self.resolver(_mapping([candidate])).resolve(parse_ingredient_text(label))
        self.assertEqual(result["resolution_status"], "EXACT_IDENTITY")
        self.assertEqual(result["selected_fdc_id"], 171266)
        self.assertEqual(result["nutritional_certainty"], "IDENTITY_EXACT")
        self.assertEqual(result["mapping_status"], "CANDIDATE")

    def test_existing_verified_mapping_is_reused_as_authoritative(self):
        mapping = _mapping([], "VERIFIED", fdc_id=173468,
                           description="Salt, table", verification_method="human_review")
        result = self.resolver(mapping).resolve(parse_ingredient_text("salt"))
        self.assertEqual(result["resolution_status"], "VERIFIED_MAPPING")
        self.assertEqual(result["selected_fdc_id"], 173468)
        self.assertEqual(result["nutritional_certainty"], "VERIFIED_IDENTITY")

    def test_reviewed_reference_lookup_returns_reference_default_not_verified(self):
        self.approve("whole milk", 171266, {
            "species": "cow", "fat_content": "whole", "flavor": "plain",
            "processing_state": "fluid",
        }, allow_as_default=True)
        candidates = [
            _candidate(171266, "Milk, producer, fluid, 3.7% milkfat"),
            _candidate(170882, "Milk, sheep, fluid"),
        ]
        result = self.resolver(_mapping(candidates)).resolve(parse_ingredient_text("whole milk"))
        self.assertEqual(result["resolution_status"], "REFERENCE_DEFAULT")
        self.assertNotEqual(result["mapping_status"], "VERIFIED")
        self.assertEqual(result["selected_fdc_id"], 171266)

    def test_reference_approval_requires_reviewer_and_provenance(self):
        with self.assertRaisesRegex(ValueError, "reviewer identity"):
            self.references.approve_reference(
                "table salt", 173468, reviewer_identity="", provenance={"source": "review"},
                attributes={"salt_type": "table"},
            )
        with self.assertRaisesRegex(ValueError, "provenance"):
            self.references.approve_reference(
                "table salt", 173468, reviewer_identity="reviewer-a", provenance={},
                attributes={"salt_type": "table"},
            )

    def test_reference_approval_records_provenance_timestamps_and_separate_status(self):
        reference = self.approve("table salt", 173468, {"salt_type": "table"})
        self.assertEqual(reference["reference_status"], "APPROVED")
        self.assertEqual(reference["reviewer_identity"], "test-reviewer")
        self.assertEqual(reference["reference_policy_version"], REFERENCE_POLICY_VERSION)
        self.assertTrue(reference["created_at"])
        self.assertTrue(reference["updated_at"])
        self.assertEqual(reference["fdc_description"], "Salt, table")
        self.assertNotIn(reference["reference_id"], {"cache_key", "mapping_cache_key"})
        self.assertEqual(self.store.review_events[-1]["action"], "APPROVED")

    def test_rejected_reference_is_retained_but_not_reused(self):
        attributes = {"salt_type": "table"}
        approved = self.approve("table salt", 173468, attributes)
        rejected = self.references.reject_reference(
            "table salt", 173468, reviewer_identity="test-reviewer",
            provenance={"source": "second review"}, attributes=attributes,
        )
        self.assertEqual(rejected["reference_id"], approved["reference_id"])
        self.assertEqual(rejected["reference_status"], "REJECTED")
        self.assertEqual(self.references.preferred_references("table salt"), [])
        self.assertEqual(len(self.store.list_ingredient_reference_reviews(approved["reference_id"])), 2)

    def test_generic_milk_stays_ambiguous_without_reference(self):
        mapping = _mapping([
            _candidate(171266, "Milk, producer, fluid, 3.7% milkfat"),
            _candidate(170882, "Milk, sheep, fluid"),
        ])
        result = self.resolver(mapping).resolve(parse_ingredient_text("milk"))
        self.assertEqual(result["resolution_status"], "AMBIGUOUS")
        self.assertEqual(result["nutritional_certainty"], "UNRELIABLE")
        self.assertTrue(result["requires_confirmation"])

    def test_initial_nine_human_reviewed_scopes_resolve_as_reference_defaults_only(self):
        cases = [
            ("table salt", 173468, "Salt, table", {"salt_type": "table"}),
            ("raw whole egg", 171287, "Egg, whole, raw, fresh", {
                "egg_part": "whole", "species": "unspecified", "preparation_state": "raw",
            }),
            ("salted butter", 173410, "Butter, salted", {
                "saltedness": "salted", "processing_state": "unspecified",
            }),
            ("unsalted butter", 173430, "Butter, without salt", {
                "saltedness": "unsalted", "processing_state": "unspecified",
            }),
            ("granulated sugar", 169655, "Sugars, granulated", {"sugar_type": "granulated"}),
            ("olive oil for salad or cooking", 171413, "Oil, olive, salad or cooking", {
                "oil_type": "olive", "processing_state": "unspecified",
            }),
            ("raw boneless skinless chicken breast", 2646170, "Chicken, breast, boneless, skinless, raw", {
                "preparation_state": "raw", "skin_state": "skinless", "bone_state": "boneless",
                "added_solution": "unspecified",
            }),
            ("raw farm raised Atlantic salmon", 2684441, "Fish, salmon, Atlantic, farm raised, raw", {
                "species": "atlantic", "farm_raised": "yes", "preparation_state": "raw",
                "processing_state": "unspecified",
            }),
            ("raw white long grain unenriched rice", 2512381, "Rice, white, long grain, unenriched, raw", {
                "variety": "white", "grain": "long", "enrichment_state": "unenriched",
                "preparation_state": "raw",
            }),
        ]
        for ingredient, fdc_id, description, scope in cases:
            with self.subTest(ingredient=ingredient):
                self.approve(ingredient, fdc_id, scope, allow_as_default=True)
                candidate = _candidate(fdc_id, description)
                candidate["retrieval_evidence"] = {"unresolved_attributes": ["review-path test"]}
                result = self.resolver(_mapping([candidate])).resolve(parse_ingredient_text(ingredient))
                self.assertEqual(result["resolution_status"], "REFERENCE_DEFAULT")
                self.assertNotEqual(result["mapping_status"], "VERIFIED")
                self.assertFalse(result["constraint_satisfaction_confirmed"])
                self.assertTrue(result["preferred_reference"]["allow_as_default"])

    def test_farm_raised_salmon_and_unenriched_rice_conflicts_are_blocked(self):
        salmon = self.approve("salmon", 2684441, {
            "species": "atlantic", "farm_raised": "yes", "preparation_state": "raw",
            "processing_state": "unspecified",
        }, allow_as_default=True)
        wild = self.reference_policy.evaluate(parse_ingredient_text("raw wild Atlantic salmon"), salmon)
        farmed = self.reference_policy.evaluate(parse_ingredient_text("raw farm-raised Atlantic salmon"), salmon)
        self.assertEqual(wild["status"], "INCOMPATIBLE")
        self.assertIn("farm_raised", wild["unresolved_attributes"])
        self.assertTrue(farmed["eligible"])

        rice = self.approve("rice", 2512381, {
            "variety": "white", "grain": "long", "enrichment_state": "unenriched",
            "preparation_state": "raw",
        }, allow_as_default=True)
        enriched = self.reference_policy.evaluate(
            parse_ingredient_text("raw white long grain enriched rice"), rice,
        )
        cooked = self.reference_policy.evaluate(
            parse_ingredient_text("cooked white long grain unenriched rice"), rice,
        )
        self.assertEqual(enriched["status"], "INCOMPATIBLE")
        self.assertIn("enrichment_state", enriched["unresolved_attributes"])
        self.assertEqual(cooked["status"], "INCOMPATIBLE")
        self.assertIn("preparation_state", cooked["unresolved_attributes"])

    def test_added_solution_chicken_and_nut_butter_cannot_reuse_plain_dairy_reference(self):
        chicken = self.approve("chicken breast", 2646170, {
            "preparation_state": "raw", "skin_state": "skinless", "bone_state": "boneless",
            "added_solution": "unspecified",
        }, allow_as_default=True)
        added = self.reference_policy.evaluate(
            parse_ingredient_text("raw boneless skinless chicken breast with added solution"), chicken,
        )
        self.assertEqual(added["status"], "INCOMPATIBLE")
        self.assertIn("added_solution", added["unresolved_attributes"])

        butter = self.approve("butter", 173410, {
            "saltedness": "salted", "processing_state": "unspecified",
        }, allow_as_default=True)
        nut_butter = self.reference_policy.evaluate(parse_ingredient_text("almond butter"), butter)
        self.assertEqual(nut_butter["status"], "INCOMPATIBLE")
        self.assertIsNone(canonical_ingredient_name("peanut butter"))

    def test_specific_reference_does_not_default_generic_butter_or_salmon_without_permission(self):
        butter = self.approve("butter", 173410, {
            "saltedness": "salted", "processing_state": "unspecified",
        }, allow_as_default=False)
        salmon = self.approve("salmon", 2684441, {
            "species": "atlantic", "farm_raised": "yes", "preparation_state": "raw",
            "processing_state": "unspecified",
        }, allow_as_default=False)
        self.assertEqual(self.reference_policy.evaluate(parse_ingredient_text("butter"), butter)["status"], "DEFAULT_NOT_PERMITTED")
        self.assertEqual(self.reference_policy.evaluate(parse_ingredient_text("salmon"), salmon)["status"], "DEFAULT_NOT_PERMITTED")

    def test_explicit_unsweetened_milk_cannot_use_sweetened_reference(self):
        reference = self.approve("whole milk", 171266, {
            "species": "cow", "fat_content": "whole", "flavor": "plain",
            "processing_state": "fluid", "sweetenedness": "sweetened",
        }, allow_as_default=True)
        decision = self.reference_policy.evaluate(
            parse_ingredient_text("unsweetened milk"), reference,
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["status"], "INCOMPATIBLE")
        self.assertIn("sweetenedness", decision["unresolved_attributes"])

    def test_whole_milk_reference_is_compatible_and_records_species_assumption(self):
        self.approve("whole milk", 171266, {
            "species": "cow", "fat_content": "whole", "flavor": "plain",
            "processing_state": "fluid",
        }, allow_as_default=True)
        reference = self.references.preferred_references("milk")[0]
        decision = self.reference_policy.evaluate(parse_ingredient_text("whole milk"), reference)
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["assumed_attributes"]["species"], "cow")
        self.assertNotIn("fat_content", decision["assumed_attributes"])

    def test_generic_milk_default_records_uncertainty_and_needs_confirmation(self):
        self.approve("whole milk", 171266, {
            "species": "cow", "fat_content": "whole", "flavor": "plain",
            "processing_state": "fluid",
        }, allow_as_default=True)
        mapping = _mapping([
            _candidate(171266, "Milk, producer, fluid, 3.7% milkfat"),
            _candidate(170882, "Milk, sheep, fluid"),
        ])
        result = self.resolver(mapping).resolve(parse_ingredient_text("milk"))
        self.assertEqual(result["resolution_status"], "REFERENCE_DEFAULT")
        self.assertEqual(result["assumed_attributes"]["species"], "cow")
        self.assertIn("fat_content", result["unresolved_attributes"])
        self.assertEqual(result["nutritional_certainty"], "ASSUMPTION_BASED")
        self.assertTrue(result["requires_confirmation"])
        self.assertFalse(result["constraint_satisfaction_confirmed"])

    def test_unsalted_butter_never_uses_a_salted_reference(self):
        reference = self.approve("butter", 173410, {
            "saltedness": "salted", "processing_state": "regular",
        }, allow_as_default=True)
        decision = self.reference_policy.evaluate(parse_ingredient_text("unsalted butter"), reference)
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["status"], "INCOMPATIBLE")
        self.assertIn("saltedness", decision["unresolved_attributes"])

    def test_generic_butter_default_preserves_saltedness_assumption(self):
        self.approve("butter", 173430, {
            "saltedness": "unsalted", "processing_state": "regular",
        }, allow_as_default=True)
        result = self.resolver(_mapping([
            _candidate(173430, "Butter, without salt"),
            _candidate(173410, "Butter, salted"),
        ])).resolve(parse_ingredient_text("butter"))
        self.assertEqual(result["resolution_status"], "REFERENCE_DEFAULT")
        self.assertEqual(result["assumed_attributes"]["saltedness"], "unsalted")
        self.assertTrue(result["requires_confirmation"])

    def test_generic_salmon_does_not_resolve_to_a_species_without_reviewed_default(self):
        result = self.resolver(_mapping([
            _candidate(173691, "Fish, salmon, sockeye, raw"),
            _candidate(173692, "Fish, salmon, Atlantic, raw"),
        ])).resolve(parse_ingredient_text("salmon"))
        self.assertEqual(result["resolution_status"], "AMBIGUOUS")
        self.assertIsNone(result["selected_fdc_id"])
        self.assertEqual(result["nutritional_certainty"], "UNRELIABLE")

    def test_explicit_sockeye_salmon_only_accepts_compatible_species_reference(self):
        reference = self.approve("salmon", 173691, {
            "species": "sockeye", "farm_raised": "unspecified", "preparation_state": "raw",
            "processing_state": "fresh",
        }, allow_as_default=True)
        decision = self.reference_policy.evaluate(
            parse_ingredient_text("raw sockeye salmon"), reference,
        )
        self.assertTrue(decision["eligible"])
        self.assertEqual(decision["assumed_attributes"], {
            "farm_raised": "unspecified", "processing_state": "fresh",
        })
        mismatch = self.reference_policy.evaluate(
            parse_ingredient_text("raw Atlantic salmon"), reference,
        )
        self.assertFalse(mismatch["eligible"])
        self.assertEqual(mismatch["status"], "INCOMPATIBLE")

    def test_cooked_rice_cannot_use_an_uncooked_rice_reference(self):
        reference = self.approve("rice", 168897, {
            "variety": "wild", "grain": "long", "preparation_state": "raw",
        })
        decision = self.reference_policy.evaluate(parse_ingredient_text("cooked wild rice"), reference)
        self.assertFalse(decision["eligible"])
        self.assertIn("preparation_state", decision["unresolved_attributes"])

    def test_explicit_olive_oil_does_not_match_generic_oil_reference(self):
        reference = self.approve("olive oil", 748608, {
            "oil_type": "olive", "processing_state": "extra virgin",
        }, allow_as_default=True)
        generic_oil = self.reference_policy.evaluate(parse_ingredient_text("oil"), reference)
        self.assertFalse(generic_oil["eligible"])
        self.assertEqual(generic_oil["status"], "INCOMPATIBLE")

    def test_missing_local_usda_record_cannot_be_approved(self):
        with self.assertRaisesRegex(ValueError, "not present"):
            self.references.approve_reference(
                "table salt", 999999999, reviewer_identity="test-reviewer",
                provenance={"source": "test"}, attributes={"salt_type": "table"},
            )

    def test_conflicting_context_attributes_block_default_use(self):
        reference = self.approve("rice", 168897, {
            "variety": "wild", "grain": "long", "preparation_state": "cooked",
        }, allow_as_default=True)
        decision = self.reference_policy.evaluate(
            parse_ingredient_text("raw white rice"), reference,
        )
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["status"], "INCOMPATIBLE")

    def test_reference_policy_version_changes_make_old_reference_unavailable(self):
        reference = self.approve("table salt", 173468, {"salt_type": "table"})
        reference["reference_policy_version"] = REFERENCE_POLICY_VERSION - 1
        decision = self.reference_policy.evaluate(parse_ingredient_text("table salt"), reference)
        self.assertFalse(decision["eligible"])
        self.assertEqual(decision["status"], "STALE_POLICY")

    def test_reference_default_never_changes_mapping_to_verified(self):
        self.approve("whole milk", 171266, {
            "species": "cow", "fat_content": "whole", "flavor": "plain",
            "processing_state": "fluid",
        }, allow_as_default=True)
        mapping = _mapping([
            _candidate(171266, "Milk, producer, fluid, 3.7% milkfat"),
            _candidate(170882, "Milk, sheep, fluid"),
        ])
        result = self.resolver(mapping).resolve(parse_ingredient_text("milk"))
        self.assertEqual(result["resolution_status"], "REFERENCE_DEFAULT")
        self.assertEqual(result["mapping_status"], "AMBIGUOUS")
        self.assertEqual(result["selected_fdc_id"], 171266)

    def test_human_verified_mapping_remains_authoritative_over_rejected_reference(self):
        self.references.reject_reference(
            "milk", 171266, reviewer_identity="test-reviewer",
            provenance={"source": "test rejection"},
            attributes={"species": "cow", "fat_content": "whole", "flavor": "plain",
                        "processing_state": "fluid"},
        )
        mapping = _mapping([], "VERIFIED", fdc_id=171266,
                           description="Milk, producer, fluid, 3.7% milkfat",
                           verification_method="human_review", validated_by="reviewer-human")
        result = self.resolver(mapping).resolve(parse_ingredient_text("milk"))
        self.assertEqual(result["resolution_status"], "VERIFIED_MAPPING")
        self.assertEqual(result["selected_fdc_id"], 171266)

    def test_review_queue_contains_candidates_evaluation_missing_fields_and_action(self):
        resolution = self.resolver(_mapping([
            _candidate(173691, "Fish, salmon, sockeye, raw"),
            _candidate(173692, "Fish, salmon, Atlantic, raw"),
        ])).resolve(parse_ingredient_text("salmon"), slm_evaluation={"status": "AMBIGUOUS"})
        item = IngredientReferenceReviewQueue(self.references).build_item(resolution)
        self.assertEqual(item["original_ingredient"], "salmon")
        self.assertEqual(len(item["candidate_usda_records"]), 2)
        self.assertEqual(item["slm_evaluation"]["status"], "AMBIGUOUS")
        self.assertIn("species", item["missing_attributes"])
        self.assertEqual(item["suggested_review_action"], "REQUEST_ATTRIBUTES")
        exact = {**resolution, "resolution_status": "EXACT_IDENTITY"}
        self.assertEqual(len(IngredientReferenceReviewQueue(self.references).build_queue([exact, resolution])), 1)

    def test_structured_attribute_synonyms_are_compared_conservatively(self):
        reference = self.approve("butter", 173430, {
            "saltedness": "without salt", "processing_state": "regular",
        }, allow_as_default=True)
        decision = self.reference_policy.evaluate(
            {"ingredient_name": "unsalted butter", "normalized_name": "unsalted butter",
             "attributes": {"saltedness": "no salt"}},
            reference,
        )
        self.assertEqual(decision["status"], "REFERENCE_DEFAULT")

    def test_review_queue_actions_are_audited_and_require_identity(self):
        resolution = self.resolver(_mapping([
            _candidate(173468, "Salt, table"),
        ])).resolve(parse_ingredient_text("salt"))
        queue = IngredientReferenceReviewQueue(self.references)
        item = queue.build_item(resolution)
        with self.assertRaisesRegex(ValueError, "reviewer identity"):
            queue.request_additional_attributes(item, ["salt type"], reviewer_identity="")
        event = queue.request_additional_attributes(
            item, ["salt type"], reviewer_identity="test-reviewer",
            provenance={"source": "review meeting"},
        )
        self.assertEqual(event["action"], "REQUEST_ATTRIBUTES")
        unresolved = queue.leave_unresolved(
            item, reviewer_identity="test-reviewer", rationale="Need recipe author input.",
        )
        self.assertEqual(unresolved["action"], "LEAVE_UNRESOLVED")
        self.assertEqual(len(self.store.review_events), 2)

    def test_review_queue_approval_requires_current_candidate_and_is_audited(self):
        resolution = self.resolver(_mapping([
            _candidate(173468, "Salt, table"),
        ])).resolve(parse_ingredient_text("table salt"))
        queue = IngredientReferenceReviewQueue(self.references)
        item = queue.build_item(resolution)
        with self.assertRaisesRegex(ValueError, "current USDA candidates"):
            queue.approve_reference(
                item, 748608, reviewer_identity="test-reviewer",
                provenance={"source": "review"}, attributes={"salt_type": "table"},
            )
        approved = queue.approve_reference(
            item, 173468, reviewer_identity="test-reviewer",
            provenance={"source": "review"}, attributes={"salt_type": "table"},
        )
        self.assertEqual(approved["reference_status"], "APPROVED")
        self.assertEqual(self.store.review_events[-1]["fdc_id"], 173468)

    def test_slm_recommendation_does_not_make_generic_ingredient_exact(self):
        mapping = _mapping([_candidate(173691, "Fish, salmon, sockeye, raw")])
        result = self.resolver(mapping).resolve(
            parse_ingredient_text("salmon"),
            slm_evaluation={"status": "RECOMMENDED", "recommended_fdc_id": 173691},
        )
        self.assertNotEqual(result["resolution_status"], "EXACT_IDENTITY")
        self.assertNotEqual(result["resolution_status"], "VERIFIED_MAPPING")
        self.assertEqual(result["nutritional_certainty"], "UNRELIABLE")

    def test_exact_auto_verification_is_explicit_opt_in_and_separate_from_default(self):
        label = "Milk, producer, fluid, 3.7% milkfat"
        candidate = _candidate(171266, label)
        mapper = StubMapper(_mapping([candidate]))
        resolver = IngredientIdentityResolver(
            mapper, self.references, auto_verify_exact=True,
        )
        with self.assertRaisesRegex(ValueError, "persist=True"):
            resolver.resolve(parse_ingredient_text(label))
        result = resolver.resolve(parse_ingredient_text(label), persist=True)
        self.assertEqual(result["resolution_status"], "VERIFIED_MAPPING")
        self.assertEqual(len(mapper.automatic_verifications), 1)

    def test_resolution_keeps_step_2_mapping_fields_and_version_unchanged(self):
        mapping = _mapping([_candidate(173468, "Salt, table")])
        before = copy.deepcopy(mapping)
        result = self.resolver(mapping).resolve(parse_ingredient_text("salt"))
        self.assertEqual(result["mapping_version"], "fdc-mapping-v1")
        self.assertEqual(result["retrieval_version"], "ingredient-rerank-v4")
        self.assertEqual(mapping, before)
        self.assertEqual(result["mapping_status"], mapping["status"])

    def test_supported_identity_detection_does_not_map_composite_foods_to_defaults(self):
        self.assertIsNone(canonical_ingredient_name("almond milk"))
        self.assertIsNone(canonical_ingredient_name("egg noodles"))
        self.assertIsNone(canonical_ingredient_name("rice noodles"))
        self.assertEqual(canonical_ingredient_name("whole milk"), "milk")
        self.assertEqual(canonical_ingredient_name("cooked white rice"), "rice")


if __name__ == "__main__":
    unittest.main()

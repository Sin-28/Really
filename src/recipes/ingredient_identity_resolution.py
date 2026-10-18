"""Ingredient identity resolution with guarded recipe-context disambiguation."""

from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy
from src.recipes.ingredient_context import candidate_family
from src.recipes.ingredient_mapping import MappingValidationError
from src.recipes.ingredient_reference import (
    REFERENCE_CATEGORIES,
    REFERENCE_POLICY_VERSION,
    ReferenceDefaultPolicy,
    canonical_ingredient_name,
    extract_explicit_ingredient_attributes,
)


RESOLUTION_STATUSES = {
    "EXACT_IDENTITY", "VERIFIED_MAPPING", "REFERENCE_DEFAULT",
    "SLM_RESOLVED", "FAMILY_RESOLVED", "AMBIGUOUS", "UNRESOLVED", "REVIEW_REQUIRED",
}
NUTRITIONAL_CERTAINTIES = {
    "VERIFIED_IDENTITY", "IDENTITY_EXACT", "REVIEWED_REFERENCE_MATCH",
    "CONTEXT_VALIDATED", "ASSUMPTION_BASED", "UNRELIABLE",
}


class IngredientIdentityResolver:
    """Resolve an ingredient using exact policy, guarded Qwen, then references.

    The resolver is opt-in and independent from user-facing adaptation. By
    default it only reads the existing mapping cache and reference store; it
    does not persist candidate mappings or convert assumptions to VERIFIED.
    """

    def __init__(self, mapper, reference_manager, *, mapping_policy=None,
                 reference_policy=None, allow_reference_defaults=True,
                 auto_verify_exact=False):
        self.mapper = mapper
        self.reference_manager = reference_manager
        self.mapping_policy = mapping_policy or AutomaticIngredientMappingPolicy()
        self.reference_policy = reference_policy or ReferenceDefaultPolicy()
        self.allow_reference_defaults = bool(allow_reference_defaults)
        self.auto_verify_exact = bool(auto_verify_exact)

    def resolve(self, ingredient, *, mapping_result=None, slm_evaluation=None,
                attributes=None, dataset_scope="all", persist=False):
        if self.auto_verify_exact and not persist:
            raise ValueError("persist=True is required when exact automatic verification is enabled.")
        if mapping_result is None:
            mapping_result = self.mapper.retrieve_candidates(
                ingredient, attributes=attributes, dataset_scope=dataset_scope,
                persist=persist,
            )
        mapping_result = mapping_result if isinstance(mapping_result, dict) else {}
        slm_evaluation = (slm_evaluation if slm_evaluation is not None
                          else mapping_result.get("slm_evaluation"))
        source_category = canonical_ingredient_name(ingredient)
        explicit_attributes = extract_explicit_ingredient_attributes(ingredient, attributes)
        base = self._base_result(ingredient, mapping_result, source_category, explicit_attributes)
        base["slm_evaluation"] = slm_evaluation

        if mapping_result.get("status") == "VERIFIED" and mapping_result.get("fdc_id") is not None:
            base.update(
                resolution_status="VERIFIED_MAPPING",
                selected_fdc_id=mapping_result.get("fdc_id"),
                selected_usda_description=mapping_result.get("description"),
                verification_method=(mapping_result.get("verification_method")
                                     or mapping_result.get("validation_method")),
                verification_policy_version=mapping_result.get("verification_policy_version"),
                verification_evidence=mapping_result.get("verification_evidence"),
                verification_reviewer=(mapping_result.get("reviewer")
                                       or mapping_result.get("validated_by")),
                verification_timestamp=mapping_result.get("validated_at"),
                verification_note=mapping_result.get("validation_note"),
                nutritional_certainty="VERIFIED_IDENTITY",
                calculation_basis="VERIFIED_INGREDIENT_IDENTITY",
                requires_confirmation=False,
                resolution_reason="An existing VERIFIED mapping is authoritative for this context.",
            )
            return self._finish(base)

        if mapping_result.get("status") in {"RETRIEVAL_ERROR", "CACHE_ERROR"}:
            base.update(
                resolution_status="UNRESOLVED",
                nutritional_certainty="UNRELIABLE",
                calculation_basis="UNRESOLVED",
                requires_confirmation=True,
                resolution_reason="USDA candidate retrieval failed; a reference was not applied.",
            )
            return self._finish(base)

        identity_evaluation = self.mapping_policy.evaluate(
            ingredient, mapping_result, slm_evaluation=slm_evaluation,
        )
        base["identity_policy_evaluation"] = identity_evaluation
        if identity_evaluation.get("eligible") is True:
            if self.auto_verify_exact and not mapping_result.get("stale_cache_record"):
                try:
                    verified = self.mapper.verify_candidate_automatically(
                        ingredient, mapping_result, self.mapping_policy,
                        slm_evaluation=slm_evaluation, enabled=True,
                        attributes=attributes, dataset_scope=dataset_scope,
                    )
                except MappingValidationError as exc:
                    base["auto_verification_error"] = str(exc)
                else:
                    if verified.get("status") == "VERIFIED":
                        base.update(
                            resolution_status="VERIFIED_MAPPING",
                            selected_fdc_id=verified.get("fdc_id"),
                            selected_usda_description=verified.get("description"),
                            verification_method=verified.get("verification_method"),
                            verification_policy_version=verified.get("verification_policy_version"),
                            verification_evidence=verified.get("verification_evidence"),
                            verification_reviewer=verified.get("reviewer"),
                            verification_timestamp=verified.get("validated_at"),
                            nutritional_certainty="VERIFIED_IDENTITY",
                            calculation_basis="VERIFIED_INGREDIENT_IDENTITY",
                            requires_confirmation=False,
                            resolution_reason="The current deterministic mapping policy verified one exact candidate.",
                        )
                        return self._finish(base)
            base.update(
                resolution_status="EXACT_IDENTITY",
                selected_fdc_id=identity_evaluation.get("fdc_id"),
                selected_usda_description=self._candidate_description(
                    mapping_result, identity_evaluation.get("fdc_id"),
                ),
                nutritional_certainty="IDENTITY_EXACT",
                calculation_basis="EXACT_IDENTITY_NOT_PERSISTED_AS_VERIFIED",
                requires_confirmation=False,
                resolution_reason="One candidate passed deterministic exact-identity checks.",
            )
            if mapping_result.get("stale_cache_record"):
                base["resolution_reason"] = (
                    "One candidate passed deterministic exact-identity checks, but a stale cache record was found; "
                    "the result was not persisted as VERIFIED."
                )
            return self._finish(base)

        slm_outcome = self._resolve_slm(mapping_result, slm_evaluation)
        if slm_outcome is not None:
            base.update(slm_outcome)
            return self._finish(base)

        reference_outcome = self._resolve_reference(
            ingredient, mapping_result, source_category, attributes,
        )
        if reference_outcome is not None:
            reference, decision = reference_outcome
            if decision.get("eligible") is True:
                assumed = decision.get("assumed_attributes") or {}
                base.update(
                    resolution_status="REFERENCE_DEFAULT",
                    selected_fdc_id=reference.get("fdc_id"),
                    selected_usda_description=reference.get("fdc_description"),
                    preferred_reference=reference,
                    assumed_attributes=assumed,
                    unresolved_attributes=decision.get("unresolved_attributes", []),
                    nutritional_certainty="ASSUMPTION_BASED" if assumed else "REVIEWED_REFERENCE_MATCH",
                    calculation_basis="REVIEWED_REFERENCE_ASSUMPTION" if assumed else "REVIEWED_REFERENCE_CONTEXT_MATCH",
                    requires_confirmation=bool(assumed),
                    constraint_satisfaction_confirmed=False,
                    reference_policy_evaluation=decision,
                    resolution_reason=("A unique, reviewed reference was applied with explicit assumption metadata."
                                       if assumed else "A unique reviewed reference matches the explicit ingredient context."),
                )
                return self._finish(base)
            base["reference_policy_evaluation"] = decision

        candidates = mapping_result.get("candidates") or []
        if not candidates or mapping_result.get("status") == "UNRESOLVED":
            status = "UNRESOLVED"
            reason = "No suitable USDA candidate or compatible reviewed reference is available."
        elif identity_evaluation.get("status") == "AMBIGUOUS" or len(candidates) > 1:
            status = "AMBIGUOUS"
            reason = "Multiple plausible USDA identities remain and no reviewed reference resolved them."
        else:
            status = "REVIEW_REQUIRED"
            reason = "A candidate exists, but deterministic identity checks did not resolve it."
        base.update(
            resolution_status=status,
            nutritional_certainty="UNRELIABLE",
            calculation_basis="UNRESOLVED",
            requires_confirmation=True,
            resolution_reason=reason,
        )
        return self._finish(base)

    @staticmethod
    def _resolve_slm(mapping_result, evaluation):
        """Accept a current, guarded model choice without marking it VERIFIED."""
        if not isinstance(evaluation, dict):
            return None
        candidates = [item for item in mapping_result.get("candidates") or []
                      if isinstance(item, dict) and item.get("fdc_id") is not None]
        current_ids = {str(item["fdc_id"]) for item in candidates}
        if not current_ids or current_ids != {str(value) for value in evaluation.get("candidate_ids") or []}:
            return None
        by_id = {str(item["fdc_id"]): item for item in candidates}
        guards = {str(item.get("fdc_id")): item for item in evaluation.get("guard_results") or []
                  if isinstance(item, dict)}
        if set(guards) != current_ids:
            return None
        status = evaluation.get("status")
        if status == "RECOMMENDED" and evaluation.get("requires_review") is False:
            selected_id = str(evaluation.get("recommended_fdc_id"))
            guard = guards.get(selected_id) or {}
            if (selected_id in by_id and guard.get("compatible") is True
                    and not guard.get("unresolved_attributes")
                    and not evaluation.get("unresolved_attributes")
                    and [str(value) for value in evaluation.get("compatible_candidate_ids") or []] == [selected_id]):
                selected = by_id[selected_id]
                return {
                    "resolution_status": "SLM_RESOLVED",
                    "selected_fdc_id": selected["fdc_id"],
                    "selected_usda_description": selected.get("description"),
                    "plausible_candidate_ids": [selected["fdc_id"]],
                    "verification_method": "SLM_CONTEXT",
                    "nutritional_certainty": "CONTEXT_VALIDATED",
                    "calculation_basis": "GUARDED_SLM_INGREDIENT_IDENTITY",
                    "requires_confirmation": False,
                    "resolution_reason": "Qwen selected one retrieved USDA record; programmatic ID and attribute checks passed.",
                }
        if status == "AMBIGUOUS":
            ids = [str(value) for value in evaluation.get("compatible_candidate_ids") or []]
            if len(ids) > 1 and len(ids) == len(set(ids)) and all(value in by_id and guards[value].get("compatible") is True for value in ids):
                families = {candidate_family(by_id[value]) for value in ids}
                if len(families) == 1 and None not in families:
                    return {
                        "resolution_status": "FAMILY_RESOLVED",
                        "resolved_family": families.pop(),
                        "selected_fdc_id": None,
                        "plausible_candidate_ids": [by_id[value]["fdc_id"] for value in ids],
                        "unresolved_attributes": evaluation.get("unresolved_attributes") or [],
                        "nutritional_certainty": "UNRELIABLE",
                        "calculation_basis": "UNRESOLVED",
                        "requires_confirmation": True,
                        "resolution_reason": "Qwen identified a shared ingredient family; no specific USDA record was selected.",
                    }
        return None

    def _resolve_reference(self, ingredient, mapping_result, source_category, attributes):
        if source_category not in REFERENCE_CATEGORIES:
            return None
        references = self.reference_manager.preferred_references(source_category)
        candidates_by_id = {
            str(item.get("fdc_id")): item
            for item in mapping_result.get("candidates", [])
            if isinstance(item, dict) and item.get("fdc_id") is not None
        }
        eligible = []
        evaluated = []
        for reference in references:
            candidate = candidates_by_id.get(str(reference.get("fdc_id")))
            if candidate is None:
                candidate = self.reference_manager.local_candidate(reference)
            if candidate is None:
                continue
            guard_mapping = {**mapping_result, "candidates": [candidate]}
            guard = self.mapping_policy.evaluate(ingredient, guard_mapping)
            evidence = (guard.get("candidate_evidence") or [{}])[0]
            if evidence.get("deterministically_compatible") is False:
                evaluated.append((reference, {
                    "eligible": False, "status": "INCOMPATIBLE",
                    "unresolved_attributes": evidence.get("unresolved_attributes") or [],
                    "reasons": evidence.get("reasons") or ["The deterministic mapping guard rejected this USDA record."],
                }))
                continue
            decision = self.reference_policy.evaluate(
                ingredient, reference, attributes=attributes,
                allow_defaults=self.allow_reference_defaults,
            )
            decision["deterministic_guard"] = evidence
            evaluated.append((reference, decision))
            if decision.get("eligible") is True:
                eligible.append((reference, decision))
        if len(eligible) == 1:
            return eligible[0]
        if len(eligible) > 1:
            return None, {
                "eligible": False, "status": "AMBIGUOUS_REFERENCES",
                "policy_version": REFERENCE_POLICY_VERSION,
                "unresolved_attributes": [],
                "reasons": ["More than one approved reference is compatible; no record was selected."],
            }
        return evaluated[0] if evaluated else None

    @staticmethod
    def _candidate_description(mapping_result, fdc_id):
        for candidate in mapping_result.get("candidates", []):
            if str(candidate.get("fdc_id")) == str(fdc_id):
                return candidate.get("description")
        return None

    def _base_result(self, ingredient, mapping_result, category, explicit):
        parsed = ingredient.get("parsed") if isinstance(ingredient, dict) else None
        source = parsed if isinstance(parsed, dict) else ingredient if isinstance(ingredient, dict) else {}
        original = (source.get("ingredient_name") or source.get("original_text")
                    or source.get("normalized_name") or "")
        normalized = (source.get("normalized_name") or mapping_result.get("normalized_name")
                      or str(original).casefold())
        return {
            "original_ingredient": str(original),
            "normalized_ingredient": str(normalized),
            "canonical_ingredient_name": category,
            "explicit_attributes": explicit,
            "mapping_status": mapping_result.get("status"),
            "mapping_version": mapping_result.get("mapping_version"),
            "retrieval_version": mapping_result.get("retrieval_version"),
            "mapping_cache_key": mapping_result.get("cache_key"),
            "candidate_usda_records": list(mapping_result.get("candidates") or []),
            "slm_evaluation": mapping_result.get("slm_evaluation"),
            "selected_fdc_id": None,
            "selected_usda_description": None,
            "verification_method": (mapping_result.get("verification_method")
                                    or mapping_result.get("validation_method")),
            "verification_policy_version": mapping_result.get("verification_policy_version"),
            "verification_evidence": mapping_result.get("verification_evidence"),
            "verification_reviewer": (mapping_result.get("reviewer")
                                      or mapping_result.get("validated_by")),
            "verification_timestamp": mapping_result.get("validated_at"),
            "verification_note": mapping_result.get("validation_note"),
            "assumed_attributes": {},
            "unresolved_attributes": [],
            "preferred_reference": None,
            "reference_policy_version": REFERENCE_POLICY_VERSION,
            "constraint_satisfaction_confirmed": False,
        }

    @staticmethod
    def _finish(result):
        status = result.get("resolution_status")
        if status not in RESOLUTION_STATUSES:
            raise ValueError(f"Unsupported ingredient resolution status: {status!r}")
        if result.get("nutritional_certainty") not in NUTRITIONAL_CERTAINTIES:
            raise ValueError("Ingredient resolution is missing a recognized nutritional certainty.")
        return result


class IngredientReferenceReviewQueue:
    """Structured, programmatic review items and audited human actions."""

    def __init__(self, reference_manager):
        self.reference_manager = reference_manager

    def build_item(self, resolution):
        resolution = resolution if isinstance(resolution, dict) else {}
        status = resolution.get("resolution_status")
        category = resolution.get("canonical_ingredient_name")
        explicit = resolution.get("explicit_attributes") or {}
        policy = resolution.get("identity_policy_evaluation") or {}
        reference_policy = resolution.get("reference_policy_evaluation") or {}
        missing = sorted(set(
            (policy.get("unresolved_attributes") or [])
            + (reference_policy.get("unresolved_attributes") or [])
        ))
        candidates = resolution.get("candidate_usda_records") or []
        if not candidates:
            suggested = "LEAVE_UNRESOLVED"
        elif missing:
            suggested = "REQUEST_ATTRIBUTES"
        else:
            suggested = "APPROVE_REFERENCE"
        return {
            "original_ingredient": resolution.get("original_ingredient"),
            "normalized_ingredient": resolution.get("normalized_ingredient"),
            "canonical_ingredient_name": category,
            "context_attributes": explicit,
            "candidate_usda_records": candidates,
            "slm_evaluation": resolution.get("slm_evaluation"),
            "reason_for_ambiguity": resolution.get("resolution_reason"),
            "available_preferred_references": self.reference_manager.preferred_references(category),
            "missing_attributes": missing,
            "suggested_review_action": suggested,
            "resolution_status": status,
            "mapping_cache_key": resolution.get("mapping_cache_key"),
        }

    def build_queue(self, resolutions):
        """Build only items that still need explicit resolution or review."""
        actionable = {"AMBIGUOUS", "UNRESOLVED", "REVIEW_REQUIRED"}
        return [self.build_item(result) for result in resolutions
                if isinstance(result, dict) and result.get("resolution_status") in actionable]

    @staticmethod
    def _candidate(item, fdc_id):
        return next((candidate for candidate in item.get("candidate_usda_records", [])
                     if str(candidate.get("fdc_id")) == str(fdc_id)), None)

    def approve_reference(self, item, fdc_id, *, reviewer_identity, provenance,
                          attributes, allow_as_default=False, rationale=None):
        if not self._candidate(item, fdc_id):
            raise ValueError("A reference can only be approved from this item's current USDA candidates.")
        return self.reference_manager.approve_reference(
            item.get("canonical_ingredient_name"), fdc_id,
            food_record_id=self._candidate(item, fdc_id).get("food_record_id"),
            reviewer_identity=reviewer_identity, provenance=provenance,
            attributes=attributes, allow_as_default=allow_as_default,
            rationale=rationale,
        )

    def reject_reference(self, item, fdc_id, *, reviewer_identity, provenance,
                         attributes, rationale=None):
        if not self._candidate(item, fdc_id):
            raise ValueError("A reference can only be rejected from this item's current USDA candidates.")
        return self.reference_manager.reject_reference(
            item.get("canonical_ingredient_name"), fdc_id,
            food_record_id=self._candidate(item, fdc_id).get("food_record_id"),
            reviewer_identity=reviewer_identity, provenance=provenance,
            attributes=attributes, rationale=rationale,
        )

    def request_additional_attributes(self, item, attributes, *, reviewer_identity, provenance=None):
        values = sorted({str(value).strip() for value in attributes if str(value).strip()})
        if not values:
            raise ValueError("At least one requested ingredient attribute is required.")
        return self.reference_manager.record_review_action(
            "REQUEST_ATTRIBUTES", item.get("canonical_ingredient_name"),
            reviewer_identity=reviewer_identity, provenance=provenance,
            context=item.get("context_attributes"),
            details={"requested_attributes": values, "mapping_cache_key": item.get("mapping_cache_key")},
        )

    def leave_unresolved(self, item, *, reviewer_identity, rationale=None, provenance=None):
        return self.reference_manager.record_review_action(
            "LEAVE_UNRESOLVED", item.get("canonical_ingredient_name"),
            reviewer_identity=reviewer_identity, provenance=provenance,
            context=item.get("context_attributes"),
            details={"rationale": rationale, "mapping_cache_key": item.get("mapping_cache_key")},
        )

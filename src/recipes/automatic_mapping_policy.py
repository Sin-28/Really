"""Conservative deterministic acceptance policy for retrieved USDA candidates."""

import re
from datetime import datetime, timezone

from src.recipes.ingredient_mapping_validator import (
    _explicit_mismatches,
    _explicit_text,
    _oil_type,
    _salmon_species,
)


class AutomaticIngredientMappingPolicy:
    """Accept only unique exact-label or explicitly curated-alias matches.

    Retrieval rank and SLM output are recorded as evidence, never used as the
    deterministic identity test. Increment ``VERSION`` whenever these rules
    change so older automatic verifications are re-evaluated explicitly.
    """

    VERSION = 2
    METHOD = "DETERMINISTIC_POLICY"

    @staticmethod
    def _canonical_label(value):
        return tuple(sorted(re.findall(r"[a-z0-9]+", str(value or "").casefold())))

    @staticmethod
    def _parts(parsed_ingredient):
        if not isinstance(parsed_ingredient, dict):
            return {}, {}
        nested = parsed_ingredient.get("parsed")
        if isinstance(nested, dict):
            return nested, parsed_ingredient
        return parsed_ingredient, {}

    @classmethod
    def _source_name(cls, parsed_ingredient, mapping_result):
        parsed, outer = cls._parts(parsed_ingredient)
        return (parsed.get("ingredient_name") or outer.get("name")
                or parsed.get("normalized_name") or mapping_result.get("normalized_name") or "")

    @staticmethod
    def _candidate_text(candidate):
        return " ".join(str(candidate.get(key) or "")
                        for key in ("description", "food_state"))

    def _identity_match(self, source_name, candidate):
        description = candidate.get("description") or ""
        if self._canonical_label(source_name) and (
                self._canonical_label(source_name) == self._canonical_label(description)):
            return "EXACT_LABEL"
        aliases = candidate.get("approved_aliases") or []
        if any(self._canonical_label(source_name) == self._canonical_label(alias)
               for alias in aliases if self._canonical_label(alias)):
            return "APPROVED_SYNONYM"
        return None

    @staticmethod
    def _slm_failure(evaluation):
        reason = str((evaluation or {}).get("reason") or "").casefold()
        return any(marker in reason for marker in (
            "inference is unavailable", "timed out", "still handling an earlier request",
        ))

    def evaluate(self, parsed_ingredient, mapping_result, *, slm_evaluation=None):
        """Return an auditable policy decision; this method performs no writes."""
        mapping_result = mapping_result if isinstance(mapping_result, dict) else {}
        candidates = [item for item in mapping_result.get("candidates", []) if isinstance(item, dict)]
        source_name = str(self._source_name(parsed_ingredient, mapping_result) or "").strip()
        parsed, outer = self._parts(parsed_ingredient)
        source_text = _explicit_text(parsed, outer, mapping_result)
        guard_results = []
        acceptable = []
        unresolved = set()
        observed_unresolved = set()
        deterministic_conflicts = []

        if not candidates:
            return {
                "eligible": False, "status": "UNRESOLVED", "fdc_id": None,
                "policy_version": self.VERSION, "verification_method": self.METHOD,
                "candidate_ids": [], "candidate_evidence": [],
                "unresolved_attributes": [],
                "reasons": ["No USDA candidate was retrieved for this ingredient."],
                "slm_status": (slm_evaluation or {}).get("status", "NOT_CONFIGURED"),
                "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }

        source_tokens = self._canonical_label(source_name)
        is_salmon = "salmon" in source_tokens
        is_generic_salmon = is_salmon and not _salmon_species(source_text)
        is_generic_oil = "oil" in source_tokens and _oil_type(source_text) is None

        for candidate in candidates:
            fdc_id = candidate.get("fdc_id")
            identity_basis = self._identity_match(source_name, candidate)
            incompatible, candidate_unresolved, guard_reasons = _explicit_mismatches(
                source_text, self._candidate_text(candidate),
            )
            candidate_unresolved = set(candidate_unresolved)
            reasons = list(guard_reasons)
            retrieval_evidence = candidate.get("retrieval_evidence")
            if isinstance(retrieval_evidence, dict):
                added_specificity = retrieval_evidence.get("unresolved_attributes") or []
                candidate_unresolved.update(
                    value for value in added_specificity if isinstance(value, str) and value
                )
                if added_specificity:
                    reasons.append(
                        "The USDA description adds attributes the source ingredient does not establish: "
                        + ", ".join(sorted(set(added_specificity))) + "."
                    )
            if not identity_basis:
                reasons.append("Ingredient identity is not an exact label or an approved exact synonym.")
            if is_generic_salmon:
                candidate_unresolved.add("species")
                reasons.append("Generic salmon does not identify a species.")
            if is_generic_oil:
                candidate_unresolved.add("oil_type")
                reasons.append("Generic oil does not identify an oil variety.")
            if incompatible:
                deterministic_conflicts.extend(guard_reasons or ["A deterministic compatibility check failed."])
            if identity_basis and not incompatible and not candidate_unresolved:
                acceptable.append((candidate, identity_basis))
            observed_unresolved.update(candidate_unresolved)
            # Attribute gaps matter for an identity-matching candidate. A
            # clearly incompatible or unrelated alternative is excluded from
            # the acceptable set instead of vetoing a unique safe candidate.
            if identity_basis:
                unresolved.update(candidate_unresolved)
            guard_results.append({
                "fdc_id": fdc_id,
                "identity_basis": identity_basis,
                "deterministically_compatible": not incompatible,
                "unresolved_attributes": sorted(candidate_unresolved),
                "reasons": reasons,
            })

        if is_generic_salmon:
            unresolved.add("species")
        if is_generic_oil:
            unresolved.add("oil_type")

        unique_ids = {str(item[0].get("fdc_id")) for item in acceptable
                      if item[0].get("fdc_id") is not None}
        eligible_candidate = acceptable[0][0] if len(unique_ids) == 1 and len(acceptable) == 1 else None
        if eligible_candidate is None:
            unresolved.update(observed_unresolved)
        reasons = []
        if deterministic_conflicts:
            reasons.extend(sorted(set(deterministic_conflicts)))
        if unresolved:
            reasons.append("Identity-critical attributes remain unresolved: " + ", ".join(sorted(unresolved)) + ".")
        if not acceptable:
            reasons.append("No candidate satisfies exact identity, state, variety, and processing checks.")
        elif len(unique_ids) > 1 or len(acceptable) > 1:
            reasons.append("Multiple acceptable USDA records remain; the policy requires one unique candidate.")

        slm_status = (slm_evaluation or {}).get("status", "NOT_CONFIGURED")
        slm_failure = bool(slm_evaluation and slm_status == "REVIEW_REQUIRED"
                           and self._slm_failure(slm_evaluation))
        slm_conflict = False
        if slm_evaluation and not slm_failure:
            if slm_status == "RECOMMENDED" and eligible_candidate is not None:
                slm_id = (slm_evaluation.get("recommended_fdc_id")
                          or slm_evaluation.get("slm_recommended_fdc_id"))
                slm_unresolved = slm_evaluation.get("unresolved_attributes") or []
                if (str(slm_id) != str(eligible_candidate.get("fdc_id"))
                        or slm_unresolved or slm_evaluation.get("requires_review") is True):
                    slm_conflict = True
                    reasons.append("SLM evidence conflicts with or remains uncertain about the deterministic match.")
            else:
                slm_conflict = True
                reasons.append(f"SLM status {slm_status!r} does not support automatic verification.")
        if slm_failure:
            reasons.append("SLM inference failed; eligibility relies entirely on the deterministic policy.")

        eligible = bool(eligible_candidate is not None and not unresolved and not slm_conflict)
        if eligible:
            status = "ELIGIBLE"
            reasons.append("One exact or approved-synonym candidate passed all deterministic checks.")
        elif not candidates:
            status = "UNRESOLVED"
        elif len(unique_ids) > 1 or unresolved:
            status = "AMBIGUOUS"
        elif deterministic_conflicts and not acceptable:
            status = "REJECTED"
        else:
            status = "REVIEW_REQUIRED"

        return {
            "eligible": eligible,
            "status": status,
            "fdc_id": eligible_candidate.get("fdc_id") if eligible_candidate else None,
            "policy_version": self.VERSION,
            "verification_method": self.METHOD,
            "candidate_ids": [item.get("fdc_id") for item in candidates],
            "candidate_evidence": guard_results,
            "unresolved_attributes": sorted(unresolved),
            "reasons": reasons,
            "slm_status": slm_status,
            "slm_failure": slm_failure,
            "slm_conflict": slm_conflict,
            "retrieval_scores_ignored": True,
            "evaluated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }

"""Local USDA FoodData Central candidate lookup and context-aware mapping cache.

Candidate ranking comes from RenAlly's existing Neo4j full-text resolver.
Persistent VERIFIED mappings require either the conservative exact-match
policy or the historical explicit-review API. Recipe-context Qwen choices are
returned by the identity resolver without changing the persistent cache.
"""

import hashlib
import json
import re
from datetime import datetime, timezone

from src.retrieval.graph_retriever import INGREDIENT_RETRIEVAL_VERSION


MAPPING_VERSION = "fdc-mapping-v1"
DATASET_SCOPES = {"all", "foundation_foods", "sr_legacy"}
FOOD_STATES = {
    "raw", "cooked", "smoked", "dried", "canned", "frozen", "boiled",
    "grilled", "baked", "fried", "roasted", "steamed", "poached", "fermented",
}
CUTS = {
    "steak", "steaks", "breast", "breasts", "fillet", "fillets", "thigh",
    "thighs", "leg", "legs", "drumstick", "drumsticks", "wing", "wings",
    "loin", "loins", "chop", "chops", "tenderloin", "tenderloins", "whole",
}


class MappingValidationError(ValueError):
    """Raised when a requested mapping review does not satisfy policy."""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(value):
    return " ".join(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _canonical_attributes(attributes):
    canonical = {}
    if not isinstance(attributes, dict):
        return canonical
    for key, value in attributes.items():
        name = _tokens(key)
        if not name or value is None:
            continue
        values = value if isinstance(value, (list, tuple, set)) else [value]
        normalized = sorted({text for item in values if (text := _tokens(item))})
        if normalized:
            canonical[name] = normalized
    return dict(sorted(canonical.items()))


def _ingredient_context(parsed_ingredient, attributes=None):
    if not isinstance(parsed_ingredient, dict):
        raise TypeError("A parsed ingredient dictionary is required.")
    parsed = parsed_ingredient.get("parsed")
    if isinstance(parsed, dict):
        outer = parsed_ingredient
    else:
        parsed = parsed_ingredient
        outer = {}
    original_name = (parsed.get("ingredient_name") or outer.get("name")
                     or parsed.get("normalized_name") or outer.get("normalized_name")
                     or parsed.get("original_text") or "")
    normalized_name = (parsed.get("normalized_name")
                       or " ".join(str(original_name).casefold().split()))
    normalized_name = _tokens(normalized_name)
    if not normalized_name:
        raise ValueError("The ingredient has no normalized name to retrieve.")

    text = _tokens(original_name)
    detected = {}
    states = sorted(set(text.split()) & FOOD_STATES)
    cuts = sorted(set(text.split()) & CUTS)
    if states:
        detected["food_state"] = states
    if cuts:
        detected["cut"] = cuts
    if isinstance(parsed.get("attributes"), dict):
        detected.update(parsed["attributes"])
    if isinstance(attributes, dict):
        detected.update(attributes)
    return {
        "ingredient_name": str(original_name),
        "normalized_name": normalized_name,
        "attributes": _canonical_attributes(detected),
    }


def _cache_key(context, dataset_scope, mapping_version):
    payload = {
        "normalized_name": context["normalized_name"],
        "attributes": context["attributes"],
        "dataset_scope": dataset_scope,
        "mapping_version": mapping_version,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _recognized_states(attributes):
    return set(attributes.get("food_state", []))


def _compatible_candidate(candidate, attributes):
    """Reject only known contradictory preparation states; unknown stays unknown."""
    requested = _recognized_states(attributes)
    if not requested:
        return True
    description = _tokens(candidate.get("food_state") or candidate.get("description"))
    candidate_states = set(description.split()) & FOOD_STATES
    return not candidate_states or candidate_states.issubset(requested)


def _retrieval_method(query, data):
    primary_labels = [data.get("fdc_description"), data.get("name")]
    aliases = data.get("aliases") or []
    raw_query = " ".join(str(query).casefold().split())
    if any(label and raw_query == " ".join(str(label).casefold().split())
           for label in primary_labels):
        return "exact_name"
    normalized_query = _tokens(query)
    query_tokens = set(normalized_query.split())
    if any(label and normalized_query == _tokens(label) for label in primary_labels):
        return "normalized_match"
    if any(alias and normalized_query == _tokens(alias) for alias in aliases):
        return "synonym_match"
    if query_tokens and any(query_tokens.issubset(set(_tokens(label).split()))
                            for label in primary_labels if label):
        return "normalized_match"
    if query_tokens and any(query_tokens.issubset(set(_tokens(alias).split()))
                            for alias in aliases if alias):
        return "synonym_match"
    return "ranked_search"


class USDAIngredientMapper:
    """Retrieve USDA candidates and persist decisions by ingredient context.

    ``kg`` is the existing Neo4j knowledge graph and ``retriever`` is its
    existing :class:`GraphRetriever`. No external USDA API or model is used.
    """

    def __init__(self, kg, retriever, mapping_version=MAPPING_VERSION):
        self.kg = kg
        self.retriever = retriever
        self.mapping_version = str(mapping_version)

    @staticmethod
    def _scope(dataset_scope):
        scope = str(dataset_scope or "all").casefold()
        if scope not in DATASET_SCOPES:
            raise ValueError(f"Unsupported USDA dataset scope: {dataset_scope!r}")
        return scope

    def _record_base(self, context, dataset_scope, key):
        now = _now()
        return {
            "cache_key": key,
            "ingredient_name": context["ingredient_name"],
            "normalized_name": context["normalized_name"],
            "attributes": context["attributes"],
            "dataset_scope": dataset_scope,
            "mapping_version": self.mapping_version,
            "retrieval_version": INGREDIENT_RETRIEVAL_VERSION,
            "created_at": now,
            "updated_at": now,
        }

    def mapping_cache_key(self, parsed_ingredient, *, attributes=None, dataset_scope="all"):
        """Return the Step 2 context key without reading or writing the cache."""
        context = _ingredient_context(parsed_ingredient, attributes)
        scope = self._scope(dataset_scope)
        return _cache_key(context, scope, self.mapping_version)

    def retrieve_candidates(self, parsed_ingredient, *, attributes=None,
                            dataset_scope="all", limit=5, persist=True,
                            reuse_verified_policy_version=None):
        """Return a compatible verified mapping or refreshed local FDC candidates.

        CANDIDATE, AMBIGUOUS, and UNRESOLVED results are persisted as cache
        records, but are retrieved again on the next request. This keeps the
        local catalog search fresh while allowing a later explicit review to
        validate one of the stored candidates.
        """
        context = _ingredient_context(parsed_ingredient, attributes)
        scope = self._scope(dataset_scope)
        limit = max(1, min(int(limit), 25))
        key = _cache_key(context, scope, self.mapping_version)
        cache_read_error = None
        try:
            cached = self.kg.get_ingredient_mapping_cache(key)
        except Exception as exc:
            cached = None
            cache_read_error = str(exc)

        cached_context_matches = bool(
            cached
            and cached.get("mapping_version") == self.mapping_version
            and cached.get("normalized_name") == context["normalized_name"]
            and cached.get("attributes") == context["attributes"]
            and cached.get("dataset_scope") == scope
        )
        stale_cache_record = bool(cached and not cached_context_matches)
        stale_verified_record = bool(
            cached and cached.get("status") == "VERIFIED" and stale_cache_record
        )
        stale_verified_policy = False
        if cached_context_matches and cached.get("status") == "VERIFIED":
            verification_method = (cached.get("verification_method")
                                   or cached.get("validation_method"))
            stale_verified_policy = (
                verification_method == "DETERMINISTIC_POLICY"
                and reuse_verified_policy_version is not None
                and cached.get("verification_policy_version") != reuse_verified_policy_version
            )
            if not stale_verified_policy:
                selected = cached.get("selected_candidate") or {}
                return {
                    "ingredient": context["ingredient_name"],
                    "normalized_name": context["normalized_name"],
                    "attributes": context["attributes"],
                    "dataset_scope": scope,
                    "mapping_version": self.mapping_version,
                    "retrieval_version": cached.get("retrieval_version", "pre-reranker"),
                    "cache_key": key,
                    "status": "VERIFIED",
                    "fdc_id": cached.get("fdc_id"),
                    "description": cached.get("fdc_description"),
                    "data_type": cached.get("fdc_data_type"),
                    "food_state": cached.get("fdc_food_state"),
                    "selected_candidate": selected,
                    "candidates": cached.get("candidates", []),
                    "validation_method": cached.get("validation_method"),
                    "verification_method": verification_method,
                    "verification_policy_version": cached.get("verification_policy_version"),
                    "verification_evidence": cached.get("verification_evidence"),
                    "validated_by": cached.get("validated_by"),
                    "reviewer": cached.get("reviewer") or cached.get("validated_by"),
                    "validated_at": cached.get("validated_at"),
                    "validation_note": cached.get("validation_note"),
                    "slm_evaluation": cached.get("slm_evaluation"),
                    "automatic_policy_evaluation": cached.get("automatic_policy_evaluation"),
                    "cache_hit": True,
                    "cache_status": "HIT",
                }

        # Include an explicitly stated state in search while retaining the
        # parser's normalized name and using its actual resolver for ranking.
        states = sorted(_recognized_states(context["attributes"]))
        contextual_tokens = context["normalized_name"].split()
        contextual_tokens.extend(state for state in states if state not in contextual_tokens)
        search_query = " ".join(contextual_tokens)
        try:
            def retrieve(query):
                if not query:
                    return [], {}
                evidence_method = getattr(
                    self.retriever, "resolve_ingredient_candidates_with_evidence", None,
                )
                if callable(evidence_method):
                    evidence = evidence_method(query, k=limit, dataset_scope=scope)
                    return [
                        (item["id"], item.get("legacy_resolver_score", 0.0))
                        for item in evidence
                    ], {item["id"]: item for item in evidence}
                return self.retriever.resolve_ingredient_candidates(
                    query, k=limit, dataset_scope=scope,
                ), {}

            ranked, ranking_evidence = retrieve(search_query)
            if not ranked and search_query != context["normalized_name"]:
                ranked, ranking_evidence = retrieve(context["normalized_name"])
        except Exception as exc:
            result = {
                "ingredient": context["ingredient_name"],
                "normalized_name": context["normalized_name"],
                "attributes": context["attributes"],
                "dataset_scope": scope,
                "mapping_version": self.mapping_version,
                "retrieval_version": INGREDIENT_RETRIEVAL_VERSION,
                "status": "RETRIEVAL_ERROR",
                "candidates": [],
                "cache_hit": False,
                "cache_status": "READ_ERROR" if cache_read_error else "NOT_WRITTEN",
                "error": str(exc),
            }
            if cache_read_error:
                result["cache_error"] = cache_read_error
            return result

        candidates, seen = [], set()
        for food_id, score in ranked:
            if food_id in seen:
                continue
            seen.add(food_id)
            data = self.kg.ingredients.get(food_id)
            if not data:
                continue
            source = data.get("source") or {}
            fdc_id = source.get("fdc_id")
            description = data.get("fdc_description") or data.get("name") or str(food_id)
            candidate = {
                "fdc_id": fdc_id,
                "food_record_id": food_id,
                "description": description,
                "data_type": source.get("data_type"),
                "food_state": data.get("food_state") or description,
                "dataset_key": source.get("dataset_key"),
                "retrieval_method": _retrieval_method(search_query, data),
                # Preserve the legacy resolver score field while carrying the
                # original Neo4j score and the new ranking score separately.
                "retrieval_score": float(score),
                "neo4j_retrieval_score": (ranking_evidence.get(food_id) or {}).get(
                    "neo4j_retrieval_score", float(score),
                ),
                "relevance_score": (ranking_evidence.get(food_id) or {}).get(
                    "relevance_score", float(score),
                ),
                "retrieval_evidence": {
                    key: value for key, value in (ranking_evidence.get(food_id) or {}).items()
                    if key not in {"id", "legacy_resolver_score", "neo4j_retrieval_score", "relevance_score"}
                },
                "retrieval_version": INGREDIENT_RETRIEVAL_VERSION,
                # Imported aliases are curated local labels; they are the only
                # synonym evidence the deterministic automatic policy accepts.
                "approved_aliases": list(data.get("aliases") or []),
            }
            if fdc_id is None or not _compatible_candidate(candidate, context["attributes"]):
                continue
            candidates.append(candidate)
            if len(candidates) >= limit:
                break

        if not candidates:
            status = "UNRESOLVED"
            message = "No candidate matched in the selected locally imported USDA catalog."
        elif len(candidates) == 1:
            status = "CANDIDATE"
            message = "A USDA candidate was found and requires validation."
        else:
            status = "AMBIGUOUS"
            message = "Multiple USDA candidates were found; none was selected."

        record = self._record_base(context, scope, key)
        if cached and cached.get("created_at"):
            record["created_at"] = cached["created_at"]
        record.update({"status": status, "candidates": candidates})
        stored = None
        if not persist:
            cache_status = "DRY_RUN"
        elif stale_verified_policy:
            # Keep the old verified record intact while returning current
            # candidates for explicit policy re-evaluation.
            cache_status = "STALE_VERIFIED_POLICY"
        elif stale_verified_record:
            # A conflicting verified record under this key is never demoted or
            # replaced by a fresh candidate set.
            cache_status = "STALE_VERIFIED_CACHE"
        else:
            cache_status = "SAVED"
            try:
                stored = self.kg.save_ingredient_mapping_cache(record)
            except Exception as exc:
                cache_status = "WRITE_ERROR"
                cache_write_error = str(exc)
                stored = None
            if cache_status == "SAVED" and not stored:
                cache_status = "WRITE_ERROR"
                cache_write_error = "Cache write returned no saved mapping record."
        if stored and stored.get("status") == "VERIFIED":
            selected = stored.get("selected_candidate") or {}
            return {
                "ingredient": context["ingredient_name"],
                "normalized_name": context["normalized_name"],
                "attributes": context["attributes"],
                "dataset_scope": scope,
                "mapping_version": self.mapping_version,
                "retrieval_version": stored.get("retrieval_version", INGREDIENT_RETRIEVAL_VERSION),
                "cache_key": key,
                "status": "VERIFIED",
                "fdc_id": stored.get("fdc_id"),
                "description": stored.get("fdc_description"),
                "data_type": stored.get("fdc_data_type"),
                "food_state": stored.get("fdc_food_state"),
                "selected_candidate": selected,
                "candidates": stored.get("candidates", []),
                "validation_method": stored.get("validation_method"),
                "verification_method": (stored.get("verification_method")
                                        or stored.get("validation_method")),
                "verification_policy_version": stored.get("verification_policy_version"),
                "verification_evidence": stored.get("verification_evidence"),
                "validated_by": stored.get("validated_by"),
                "reviewer": stored.get("reviewer") or stored.get("validated_by"),
                "validated_at": stored.get("validated_at"),
                "slm_evaluation": stored.get("slm_evaluation"),
                "cache_hit": True,
                "cache_status": "HIT",
            }
        result = {
            "ingredient": context["ingredient_name"],
            "normalized_name": context["normalized_name"],
            "attributes": context["attributes"],
            "dataset_scope": scope,
            "mapping_version": self.mapping_version,
            "retrieval_version": INGREDIENT_RETRIEVAL_VERSION,
            "status": status,
            "candidates": candidates,
            "cache_key": key,
            "cache_hit": False,
            "cache_status": cache_status,
            "cache_record_found": bool(cached),
            "stale_cache_record": stale_cache_record,
            "message": message,
            "slm_evaluation": ((stored or cached or {}).get("slm_evaluation")
                               if {str(item.get("fdc_id")) for item in candidates}
                               == {str(item.get("fdc_id")) for item in (cached or {}).get("candidates", [])}
                               else None),
            "automatic_policy_evaluation": (stored or cached or {}).get("automatic_policy_evaluation"),
        }
        if stale_verified_policy and cached:
            result["previous_verified_mapping"] = {
                "fdc_id": cached.get("fdc_id"),
                "verification_method": cached.get("verification_method") or cached.get("validation_method"),
                "verification_policy_version": cached.get("verification_policy_version"),
            }
        elif stale_verified_record and cached:
            result["previous_verified_mapping"] = {
                "fdc_id": cached.get("fdc_id"),
                "verification_method": cached.get("verification_method") or cached.get("validation_method"),
                "mapping_version": cached.get("mapping_version"),
            }
        if cache_read_error:
            result["cache_read_error"] = cache_read_error
        if cache_status == "WRITE_ERROR":
            result["cache_error"] = cache_write_error
        return result

    def verify_candidate(self, parsed_ingredient, fdc_id, *, validated_by,
                         validation_method="human_review", validation_note=None,
                         attributes=None, dataset_scope="all"):
        """Explicitly approve a previously retrieved candidate after human review.

        Policy: the candidate must have been stored for this exact ingredient
        context, scope, and mapping version; validation_method must be
        ``human_review``; and the reviewer must be identified. Retrieval score
        alone never satisfies this policy.
        """
        context = _ingredient_context(parsed_ingredient, attributes)
        scope = self._scope(dataset_scope)
        key = _cache_key(context, scope, self.mapping_version)
        if validation_method != "human_review":
            raise MappingValidationError("Only the human_review validation method is enabled in this step.")
        reviewer = str(validated_by or "").strip()
        if not reviewer:
            raise MappingValidationError("A reviewer identity is required for verification.")
        try:
            cached = self.kg.get_ingredient_mapping_cache(key)
        except Exception as exc:
            raise MappingValidationError(f"Could not read the candidate cache: {exc}") from exc
        if (not cached or cached.get("mapping_version") != self.mapping_version
                or cached.get("normalized_name") != context["normalized_name"]
                or cached.get("attributes") != context["attributes"]
                or cached.get("dataset_scope") != scope
                or cached.get("status") not in {"CANDIDATE", "AMBIGUOUS"}):
            raise MappingValidationError("No current candidate set exists for this ingredient context.")
        candidate = next((item for item in cached.get("candidates", [])
                          if str(item.get("fdc_id")) == str(fdc_id)), None)
        if candidate is None:
            raise MappingValidationError("The selected FDC ID is not among the cached candidates.")

        now = _now()
        record = {
            **cached,
            "status": "VERIFIED",
            "fdc_id": candidate["fdc_id"],
            "fdc_description": candidate.get("description"),
            "fdc_data_type": candidate.get("data_type"),
            "fdc_food_state": candidate.get("food_state"),
            "selected_candidate": candidate,
            "validation_method": "human_review",
            "verification_method": "human_review",
            "reviewer": reviewer,
            "validated_by": reviewer,
            "validation_note": str(validation_note).strip() if validation_note else None,
            "validated_at": now,
            "updated_at": now,
            "created_at": cached.get("created_at") or now,
        }
        try:
            stored = self.kg.save_ingredient_mapping_cache(record)
        except Exception as exc:
            raise MappingValidationError(f"Could not persist the verified mapping: {exc}") from exc
        if not stored or stored.get("status") != "VERIFIED":
            raise MappingValidationError("The verified mapping was not confirmed by the persistent cache.")
        if str((stored.get("selected_candidate") or {}).get("fdc_id")) != str(candidate["fdc_id"]):
            raise MappingValidationError("Another review updated this context to a different FDC candidate.")
        return {
            "ingredient": context["ingredient_name"],
            "normalized_name": context["normalized_name"],
            "attributes": context["attributes"],
            "dataset_scope": scope,
            "mapping_version": self.mapping_version,
            "retrieval_version": stored.get("retrieval_version", INGREDIENT_RETRIEVAL_VERSION),
            "status": "VERIFIED",
            "fdc_id": stored.get("fdc_id"),
            "description": stored.get("fdc_description"),
            "data_type": stored.get("fdc_data_type"),
            "food_state": stored.get("fdc_food_state"),
            "selected_candidate": stored.get("selected_candidate"),
            "candidates": stored.get("candidates", []),
            "validation_method": stored.get("validation_method"),
            "verification_method": stored.get("verification_method") or stored.get("validation_method"),
            "verification_policy_version": stored.get("verification_policy_version"),
            "verification_evidence": stored.get("verification_evidence"),
            "validated_by": stored.get("validated_by"),
            "reviewer": stored.get("reviewer") or stored.get("validated_by"),
            "validated_at": stored.get("validated_at"),
            "cache_hit": False,
            "cache_status": "SAVED" if stored else "WRITE_ERROR",
        }

    def verify_candidate_automatically(self, parsed_ingredient, mapping_result, policy, *,
                                      slm_evaluation=None, enabled=False,
                                      attributes=None, dataset_scope="all"):
        """Persist a deterministic-policy verification after an explicit opt-in.

        The policy is re-evaluated here; callers cannot pass an arbitrary FDC
        ID or a pre-forged eligible decision. Existing human-reviewed records
        are returned unchanged. An older automatic verification can only have
        its policy provenance upgraded when the current decision selects the
        same FDC ID.
        """
        if enabled is not True:
            raise MappingValidationError("Automatic verification requires explicit enabled=True.")
        if not isinstance(mapping_result, dict):
            raise MappingValidationError("A current Step 2 candidate result is required.")
        from src.recipes.automatic_mapping_policy import AutomaticIngredientMappingPolicy

        if type(policy) is not AutomaticIngredientMappingPolicy:
            raise MappingValidationError("A deterministic automatic mapping policy is required.")
        context = _ingredient_context(parsed_ingredient, attributes)
        scope = self._scope(dataset_scope)
        key = _cache_key(context, scope, self.mapping_version)
        if mapping_result.get("cache_key") != key:
            raise MappingValidationError("The candidate result does not match this ingredient context.")
        decision = AutomaticIngredientMappingPolicy.evaluate(
            policy, parsed_ingredient, mapping_result, slm_evaluation=slm_evaluation,
        )
        if not isinstance(decision, dict) or decision.get("eligible") is not True:
            raise MappingValidationError("The deterministic policy did not authorize automatic verification.")
        policy_version = getattr(policy, "VERSION", None)
        if decision.get("policy_version") != policy_version or not isinstance(policy_version, int):
            raise MappingValidationError("The policy decision has a missing or mismatched policy version.")
        fdc_id = decision.get("fdc_id")
        candidate = next((item for item in mapping_result.get("candidates", [])
                          if str(item.get("fdc_id")) == str(fdc_id)), None)
        if candidate is None:
            raise MappingValidationError("The policy candidate is absent from the retrieved candidate set.")

        try:
            cached = self.kg.get_ingredient_mapping_cache(key)
        except Exception as exc:
            raise MappingValidationError(f"Could not read the candidate cache: {exc}") from exc
        if not cached:
            raise MappingValidationError("No persistent candidate cache record exists for automatic verification.")
        cached_ids = {str(item.get("fdc_id")) for item in cached.get("candidates", [])}
        result_ids = {str(item.get("fdc_id")) for item in mapping_result.get("candidates", [])}
        def candidate_snapshot(candidates):
            return sorted((
                str(item.get("fdc_id")),
                str(item.get("food_record_id") or ""),
                str(item.get("description") or ""),
                str(item.get("food_state") or ""),
                str(item.get("data_type") or ""),
                str(item.get("dataset_key") or ""),
                tuple(sorted(str(alias) for alias in (item.get("approved_aliases") or []))),
            ) for item in candidates if isinstance(item, dict))

        candidates_unchanged = (
            candidate_snapshot(cached.get("candidates") or [])
            == candidate_snapshot(mapping_result.get("candidates") or [])
        )
        if cached_ids != result_ids or not candidates_unchanged:
            raise MappingValidationError("The cached candidates changed during automatic evaluation.")

        now = _now()
        existing_method = cached.get("verification_method") or cached.get("validation_method")
        if cached.get("status") == "VERIFIED":
            if existing_method == "human_review":
                return {
                    "status": "VERIFIED", "fdc_id": cached.get("fdc_id"),
                    "verification_method": "human_review",
                    "verification_policy_version": cached.get("verification_policy_version"),
                    "reviewer": cached.get("reviewer") or cached.get("validated_by"),
                    "cache_status": "HUMAN_VERIFIED_PRESERVED",
                }
            if existing_method == "DETERMINISTIC_POLICY":
                if cached.get("verification_policy_version") == policy_version:
                    return {
                        "status": "VERIFIED", "fdc_id": cached.get("fdc_id"),
                        "verification_method": existing_method,
                        "verification_policy_version": policy_version,
                        "reviewer": None, "cache_status": "POLICY_VERIFIED_REUSED",
                    }
                if str(cached.get("fdc_id")) != str(fdc_id):
                    raise MappingValidationError(
                        "A stale automatic mapping selected a different FDC ID; human review is required."
                    )
                upgrader = getattr(self.kg, "upgrade_automatic_mapping_policy", None)
                if not callable(upgrader):
                    raise MappingValidationError("The graph cache cannot safely upgrade automatic policy provenance.")
                try:
                    stored = upgrader(key, fdc_id, policy_version, decision)
                except Exception as exc:
                    raise MappingValidationError(f"Could not upgrade automatic policy provenance: {exc}") from exc
                if not stored or stored.get("status") != "VERIFIED":
                    raise MappingValidationError("The stale automatic mapping was not upgraded in the cache.")
                return {
                    "status": "VERIFIED", "fdc_id": stored.get("fdc_id"),
                    "verification_method": "DETERMINISTIC_POLICY",
                    "verification_policy_version": policy_version,
                    "verification_evidence": stored.get("verification_evidence"),
                    "reviewer": None, "cache_status": "POLICY_PROVENANCE_UPGRADED",
                }
            raise MappingValidationError(
                "An existing VERIFIED mapping with unknown provenance cannot be overwritten."
            )
        if cached.get("status") not in {"CANDIDATE", "AMBIGUOUS", "UNRESOLVED"}:
            raise MappingValidationError("No current unverified candidate set exists for automatic verification.")
        if str(fdc_id) not in cached_ids:
            raise MappingValidationError("The selected FDC ID is not among the cached candidates.")

        record = {
            **cached,
            "status": "VERIFIED",
            "fdc_id": candidate["fdc_id"],
            "fdc_description": candidate.get("description"),
            "fdc_data_type": candidate.get("data_type"),
            "fdc_food_state": candidate.get("food_state"),
            "selected_candidate": candidate,
            "validation_method": "DETERMINISTIC_POLICY",
            "verification_method": "DETERMINISTIC_POLICY",
            "verification_policy_version": policy_version,
            "verification_evidence": decision,
            "automatic_policy_evaluation": decision,
            "reviewer": None,
            "validated_by": None,
            "validated_at": now,
            "updated_at": now,
            "created_at": cached.get("created_at") or now,
        }
        try:
            stored = self.kg.save_ingredient_mapping_cache(record)
        except Exception as exc:
            raise MappingValidationError(f"Could not persist automatic verification: {exc}") from exc
        if (not stored or stored.get("status") != "VERIFIED"
                or str(stored.get("fdc_id")) != str(candidate["fdc_id"])
                or (stored.get("verification_method") or stored.get("validation_method")) != "DETERMINISTIC_POLICY"):
            raise MappingValidationError("The automatic verification was not confirmed by the mapping cache.")
        return {
            "status": "VERIFIED", "fdc_id": stored.get("fdc_id"),
            "verification_method": "DETERMINISTIC_POLICY",
            "verification_policy_version": policy_version,
            "verification_evidence": stored.get("verification_evidence"),
            "reviewer": None, "validated_at": stored.get("validated_at"),
            "cache_status": "SAVED",
        }

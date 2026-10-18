"""Read-only USDA reference review manifest and explicitly gated decisions."""

import hashlib
import re
from datetime import datetime, timezone

from src.recipes.ingredient_reference import REFERENCE_POLICY_VERSION, _normalize_attribute_map


REVIEW_SEARCH_QUERIES = {
    "salt": ["salt", "table salt", "iodized salt", "sea salt", "kosher salt"],
    "egg": [
        "egg", "raw whole egg", "cooked whole egg", "raw egg white",
        "cooked egg white", "raw egg yolk", "cooked egg yolk",
    ],
    "butter": ["butter", "salted butter", "unsalted butter", "whipped butter"],
    "sugar": ["sugar", "granulated white sugar", "sugars granulated", "brown sugar", "sugars brown", "turbinado sugar"],
    "milk": ["milk", "whole cow milk", "reduced fat milk", "skim milk", "unsweetened milk"],
    "olive oil": ["olive oil", "extra virgin olive oil", "extra light olive oil", "refined olive oil"],
    "chicken breast": [
        "chicken breast", "raw boneless skinless chicken breast",
        "raw chicken breast with skin", "cooked chicken breast",
        "chicken breast added solution",
    ],
    "salmon": [
        "salmon", "Atlantic salmon raw", "sockeye salmon raw", "pink salmon raw",
        "Atlantic salmon cooked", "sockeye salmon cooked", "pink salmon cooked", "cooked salmon",
    ],
    "rice": [
        "rice", "white long grain rice raw", "white long grain rice cooked",
        "brown rice raw", "brown rice cooked", "wild rice cooked",
    ],
}

_MAX_RESULTS_PER_QUERY = 12
_MAX_CANDIDATES_PER_CATEGORY = 10
_MAX_CATALOG_IDS_PER_READ = 256
_NUTRIENTS = {
    "sodium": ("1093", "mg"),
    "potassium": ("1092", "mg"),
    "phosphorus": ("1091", "mg"),
    "protein": ("1003", "g"),
}
_REQUIRED_ATTRIBUTES = {
    "salt": ("salt_type",),
    "egg": ("egg_part", "species", "preparation_state"),
    "butter": ("saltedness", "processing_state"),
    "sugar": ("sugar_type",),
    "milk": ("species", "fat_content", "flavor", "processing_state"),
    "olive oil": ("oil_type", "processing_state"),
    "chicken breast": ("preparation_state", "skin_state", "bone_state", "added_solution"),
    "salmon": ("species", "farm_raised", "preparation_state", "processing_state"),
    "rice": ("variety", "grain", "enrichment_state", "preparation_state"),
}
_STATE_WORDS = {
    "raw", "uncooked", "cooked", "fried", "boiled", "poached", "grilled",
    "baked", "roasted", "steamed", "smoked", "dried", "canned", "frozen",
    "fermented", "pickled", "cured", "kippered",
}
_PROCESSING_WORDS = {
    "smoked", "dried", "canned", "frozen", "fermented", "pickled", "cured",
    "kippered", "powder", "powdered", "dry", "condensed", "evaporated",
    "clarified", "whipped", "refined", "virgin", "fluid", "light",
}
_INGREDIENT_HEAD = {
    "salt": "salt", "egg": "egg", "butter": "butter", "sugar": "sugar",
    "milk": "milk", "olive oil": "oil", "chicken breast": "chicken",
    "salmon": "salmon", "rice": "rice",
}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _tokens(text):
    return set(re.findall(r"[a-z0-9.]+", str(text or "").casefold()))


def attributes_from_usda_description(category, description):
    """Extract only attributes literally supported by a USDA description."""
    tokens = _tokens(description)
    attrs = {}
    states = sorted(tokens & _STATE_WORDS)
    if "uncooked" in states:
        states = sorted((set(states) - {"uncooked"}) | {"raw"})
    if states:
        attrs["preparation_state"] = states if len(states) > 1 else states[0]
    processing = sorted(tokens & _PROCESSING_WORDS)
    if processing:
        attrs["processing_state"] = processing if len(processing) > 1 else processing[0]

    if category == "salt":
        values = sorted(tokens & {"table", "iodized", "sea", "kosher", "rock", "himalayan"})
        if values:
            attrs["salt_type"] = values if len(values) > 1 else values[0]
    elif category == "egg":
        parts = sorted(tokens & {"whole", "white", "yolk"})
        if parts:
            attrs["egg_part"] = parts if len(parts) > 1 else parts[0]
        species = sorted(tokens & {"chicken", "duck", "goose", "quail", "turkey"})
        if species:
            attrs["species"] = species if len(species) > 1 else species[0]
    elif category == "butter":
        if "unsalted" in tokens or {"without", "salt"}.issubset(tokens):
            attrs["saltedness"] = "unsalted"
        elif "salted" in tokens or {"with", "salt"}.issubset(tokens):
            attrs["saltedness"] = "salted"
    elif category == "sugar":
        values = sorted(tokens & {"white", "brown", "granulated", "cane", "turbinado", "raw", "powdered", "maple"})
        if values:
            attrs["sugar_type"] = values if len(values) > 1 else values[0]
    elif category == "milk":
        species = sorted(tokens & {"cow", "bovine", "sheep", "goat", "buffalo", "camel", "human"})
        if species:
            attrs["species"] = species if len(species) > 1 else species[0]
        match = re.search(r"\b(\d+(?:\.\d+)?)\s*%\s*(?:milk\s*)?fat", str(description).casefold())
        if match:
            attrs["fat_content"] = match.group(1) + "%"
        elif "whole" in tokens:
            attrs["fat_content"] = "whole"
        elif "skim" in tokens or "nonfat" in tokens:
            attrs["fat_content"] = "skim" if "skim" in tokens else "nonfat"
        elif "lowfat" in tokens or {"low", "fat"}.issubset(tokens):
            attrs["fat_content"] = "low fat"
        elif "reducedfat" in tokens or {"reduced", "fat"}.issubset(tokens):
            attrs["fat_content"] = "reduced fat"
        if "chocolate" in tokens:
            attrs["flavor"] = "chocolate"
        if "unsweetened" in tokens:
            attrs["sweetenedness"] = "unsweetened"
        elif "sweetened" in tokens:
            attrs["sweetenedness"] = "sweetened"
        if "buttermilk" in tokens:
            attrs["milk_product_type"] = "buttermilk"
    elif category == "olive oil":
        attrs["oil_type"] = "olive"
        variants = sorted(tokens & {"extra", "virgin", "light", "refined"})
        if variants:
            attrs["processing_state"] = variants if len(variants) > 1 else variants[0]
    elif category == "chicken breast":
        if "skinless" in tokens:
            attrs["skin_state"] = "skinless"
        elif "skin" in tokens:
            attrs["skin_state"] = "with skin"
        if "boneless" in tokens:
            attrs["bone_state"] = "boneless"
        elif "bone" in tokens:
            attrs["bone_state"] = "bone-in"
        if {"added", "solution"}.issubset(tokens):
            attrs["added_solution"] = "yes"
        elif "solution" in tokens and "with" in tokens:
            attrs["added_solution"] = "yes"
        elif "unenhanced" in tokens:
            attrs["added_solution"] = "no"
    elif category == "salmon":
        species = sorted(tokens & {"atlantic", "sockeye", "pink", "chinook", "king", "coho", "chum", "steelhead"})
        if species:
            attrs["species"] = species if len(species) > 1 else species[0]
        if {"farm", "raised"}.issubset(tokens):
            attrs["farm_raised"] = "yes"
        elif "wild" in tokens:
            attrs["farm_raised"] = "no"
    elif category == "rice":
        varieties = sorted(tokens & {"white", "brown", "wild", "red", "black"})
        if varieties:
            attrs["variety"] = varieties if len(varieties) > 1 else varieties[0]
        grains = sorted(tokens & {"long", "medium", "short"})
        if grains:
            attrs["grain"] = grains if len(grains) > 1 else grains[0]
        if "unenriched" in tokens:
            attrs["enrichment_state"] = "unenriched"
        elif "enriched" in tokens:
            attrs["enrichment_state"] = "enriched"
    return attrs


def _convert_nutrient(amount, source_unit, target_unit):
    if not isinstance(amount, (int, float)) or isinstance(amount, bool):
        return None
    unit = str(source_unit or "").strip().casefold().replace("μ", "µ")
    factors_to_g = {
        "g": 1.0, "gram": 1.0, "grams": 1.0,
        "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
        "µg": 0.000001, "mcg": 0.000001, "microgram": 0.000001,
        "micrograms": 0.000001,
    }
    if unit not in factors_to_g:
        return None
    grams = float(amount) * factors_to_g[unit]
    return grams * 1000.0 if target_unit == "mg" else grams


def _nutrient_summary(raw_nutrients, nutrition_basis):
    by_id = {}
    for nutrient in raw_nutrients or []:
        if not isinstance(nutrient, dict) or nutrient.get("fdc_nutrient_id") is None:
            continue
        by_id.setdefault(str(nutrient["fdc_nutrient_id"]), []).append(nutrient)
    per_100g = str(nutrition_basis or "").strip().casefold() == "per 100 g"
    result = {}
    for canonical, (fdc_id, target_unit) in _NUTRIENTS.items():
        rows = by_id.get(fdc_id) or []
        selected = rows[0] if rows else None
        if selected is None:
            result[canonical] = {
                "fdc_nutrient_id": fdc_id,
                "name": None,
                "amount": None,
                "unit": None,
                "normalized_amount_per_100g": None,
                "normalized_unit": target_unit,
                "availability": "MISSING",
                "basis": nutrition_basis or None,
            }
            continue
        amount = selected.get("amount")
        unit = selected.get("unit")
        normalized = _convert_nutrient(amount, unit, target_unit) if per_100g else None
        result[canonical] = {
            "fdc_nutrient_id": str(selected.get("fdc_nutrient_id")),
            "name": selected.get("name"),
            "amount": amount,
            "unit": unit,
            "normalized_amount_per_100g": normalized,
            "normalized_unit": target_unit,
            "availability": ("AVAILABLE" if normalized is not None else
                             "UNSUPPORTED_UNIT" if per_100g else "UNKNOWN_BASIS"),
            "basis": nutrition_basis or None,
        }
    return result


def _query_attribute_conflicts(category, query, description):
    requested = attributes_from_usda_description(category, query)
    observed = attributes_from_usda_description(category, description)
    conflicts = []
    # These are categorical attributes whose literal values are mutually
    # exclusive. Fat percentages and broad labels are left for human review.
    strict = {
        "preparation_state", "species", "variety", "grain", "saltedness",
        "egg_part", "skin_state", "bone_state", "added_solution", "salt_type",
        "sugar_type", "processing_state", "sweetenedness",
    }
    for key in strict & requested.keys() & observed.keys():
        left = set(requested[key] if isinstance(requested[key], list) else [requested[key]])
        right = set(observed[key] if isinstance(observed[key], list) else [observed[key]])
        if not left.intersection(right):
            # Table salt may also be iodized; type facets can coexist.
            if key == "salt_type" and left | right <= {"table", "iodized"}:
                continue
            conflicts.append(key)
    return sorted(conflicts), requested, observed


def _scope(category, description, attributes, missing):
    if attributes:
        scope = ", ".join(
            f"{key}={', '.join(value) if isinstance(value, list) else value}"
            for key, value in sorted(attributes.items())
        )
    else:
        scope = "USDA description only; identity details need reviewer annotation"
    missing_text = f" Candidate description also omits: {', '.join(missing)}." if missing else ""
    return (f"Only for {category} explicitly matching {scope}; this is not a universal "
            f"default for a generic ingredient.{missing_text}")


def _lucene_or_query(query):
    words = []
    seen = set()
    for token in re.findall(r"[a-z0-9]+", str(query or "").casefold()):
        variants = [token]
        if token == "egg":
            variants.append("eggs")
        elif token == "eggs":
            variants.insert(0, "egg")
        for variant in variants:
            if len(variant) >= 2 and variant not in seen:
                seen.add(variant)
                words.append(variant)
    return " OR ".join(words[:12])


def _category_match_reason(category, description):
    """Return a rejection reason for a product whose primary identity is not the ingredient."""
    text = str(description or "").casefold()
    primary = text.split(",", 1)[0].strip()
    words = _tokens(primary)
    all_words = _tokens(text)
    if category == "salt":
        if "salt" not in words:
            return "The USDA primary food identity is not salt."
        if "substitute" in all_words:
            return "This is a salt substitute, not a table or culinary salt reference."
    if category == "egg":
        if not text.casefold().lstrip().startswith(("egg,", "eggs,")):
            return "The USDA primary food identity is not an egg."
        if all_words.intersection({"roll", "rolls", "noodle", "noodles", "bagel", "bagels"}):
            return "This is an egg-containing prepared food, not an egg reference."
    if category == "butter" and ("butter" not in words or "oil" in words
                                  or all_words.intersection({"peanut", "almond", "cashew", "sesame", "sunflower", "seed"})):
        return "The record is not dairy butter as its primary food identity."
    if category == "butter" and all_words.intersection({"replacement", "imitation", "spread"}):
        return "This is a butter replacement or spread, not a dairy-butter reference."
    if category == "sugar" and (not words.intersection({"sugar", "sugars"})
                                 or all_words.intersection({"apple", "apples", "sweetsop"})):
        return "The USDA primary food identity is not sugar."
    if category == "sugar" and "powdered" in all_words:
        return "Powdered sugar is outside the initial granulated, brown, and turbinado reference set."
    if category == "milk":
        if "milk" not in words or all_words.intersection({"soy", "soya", "almond", "oat", "coconut", "rice"}):
            return "The record is not unmodified dairy milk as its primary food identity."
        if all_words.intersection({"buttermilk", "filled", "cereal", "bar", "shake", "shakes"}):
            return "This is a milk-derived or prepared product, not a plain milk reference."
    if category == "olive oil":
        if not ("oil" in words and "olive" in text):
            return "The USDA primary food identity is not olive oil."
        if all_words.intersection({"corn", "peanut", "canola", "soy", "sunflower"}):
            return "This is a blended oil record, not a single olive-oil reference."
    if category == "chicken breast":
        if not ("chicken" in words and "breast" in text):
            return "The record is not chicken breast as its primary food identity."
        if all_words.intersection({"tender", "tenders", "breaded", "battered", "nugget", "nuggets", "deli", "roll", "rolls", "patty", "patties", "sliced", "flavor", "rotisserie", "bbq"}):
            return "This is a prepared/breaded chicken product, not a plain breast reference."
    if category == "salmon" and not ("salmon" in text and "oil" not in words and "salmon oil" not in primary):
        return "The record is not salmon as a whole-food identity."
    if category == "salmon" and all_words.intersection({"nugget", "nuggets", "burger", "burgers", "patty", "patties"}):
        return "This is a prepared salmon product, not a salmon fish reference."
    if category == "rice":
        unsuitable = ("rice bran", "rice cake", "rice cracker", "rice flour", "rice noodle", "rice beverage", "rice mix", "rice-a-roni", "rice bowl")
        if any(primary.startswith(label) for label in unsuitable):
            return "This is a rice-derived product, not a rice grain reference."
        if all_words.intersection({"vermicelli", "pilaf", "flavored", "flavor", "mix", "bowl", "salad", "soup", "casserole", "entree", "fried"}):
            return "This is a prepared rice dish or mixture, not a plain rice-grain reference."
        if not primary.startswith(("rice", "wild rice")):
            return "The USDA primary food identity is not rice."
    return None


def _candidate_signature(category, attributes):
    keys = {
        "salt": ("salt_type",), "egg": ("egg_part", "species", "preparation_state", "processing_state"),
        "butter": ("saltedness", "processing_state"), "sugar": ("sugar_type",),
        "milk": ("species", "fat_content", "flavor", "processing_state"),
        "olive oil": ("processing_state",),
        "chicken breast": ("preparation_state", "skin_state", "bone_state", "added_solution"),
        "salmon": ("species", "farm_raised", "preparation_state", "processing_state"),
        "rice": ("variety", "grain", "enrichment_state", "preparation_state"),
    }[category]
    return tuple((key, str(attributes.get(key) or "unspecified")) for key in keys)


def build_reference_review_manifest(runtime, *, max_candidates_per_category=_MAX_CANDIDATES_PER_CATEGORY,
                                    query_limit=_MAX_RESULTS_PER_QUERY):
    """Search multiple targeted local-catalog queries and return pending rows."""
    query_limit = max(1, min(int(query_limit), 25))
    max_candidates_per_category = max(1, min(int(max_candidates_per_category), 25))
    found = {category: {} for category in REVIEW_SEARCH_QUERIES}
    excluded = {category: {} for category in REVIEW_SEARCH_QUERIES}

    def record_hit(category, query, hit, *, source_kind):
        food_id = hit.get("id")
        food = (getattr(runtime.kg, "ingredients", {}) or {}).get(food_id)
        if not food:
            return
        source = food.get("source") or {}
        fdc_id = source.get("fdc_id")
        if fdc_id is None:
            return
        description = food.get("fdc_description") or food.get("name") or hit.get("name") or ""
        key = str(fdc_id)
        reason = _category_match_reason(category, description)
        if reason:
            excluded[category].setdefault(key, {
                "canonical_ingredient_category": category,
                "fdc_id": int(fdc_id),
                "food_record_id": str(food_id),
                "usda_description": description,
                "usda_data_type": source.get("data_type"),
                "dataset_key": source.get("dataset_key"),
                "status": "EXCLUDED_INCOMPATIBLE",
                "reason": reason,
                "search_query": query,
            })
            return
        slot = found[category].setdefault(key, {
            "fdc_id": int(fdc_id),
            "food_record_id": str(food_id),
            "description": description,
            "search_evidence": [],
            "dataset_key": source.get("dataset_key"),
            "data_type": source.get("data_type"),
        })
        evidence = {
            "query": query,
            "source": source_kind,
            "identity_match": hit.get("identity_match"),
            "variety_compatibility": hit.get("variety_compatibility"),
            "preparation_state_compatibility": hit.get("preparation_state_compatibility"),
            "processing_state_compatibility": hit.get("processing_state_compatibility"),
            "unresolved_attributes": hit.get("unresolved_attributes") or [],
        }
        if not evidence["identity_match"]:
            query_words = _tokens(query) - _STATE_WORDS
            description_words = _tokens(description)
            if query_words and query_words.issubset(description_words):
                evidence["identity_match"] = "exact_identity"
            else:
                evidence["identity_match"] = "fulltext_candidate"
        if evidence not in slot["search_evidence"]:
            slot["search_evidence"].append(evidence)

    for category, queries in REVIEW_SEARCH_QUERIES.items():
        for query in queries:
            # Existing deterministic reranking remains one discovery source.
            hits = runtime.retriever.resolve_ingredient_candidates_with_evidence(
                query, k=query_limit, dataset_scope="all",
            )
            for hit in hits:
                record_hit(category, query, hit, source_kind="ingredient_reranker")
            # Also search the local USDA full-text index directly by each
            # bounded, more specific query so records outside the old top five
            # (or the reranker's conservative candidate set) can be reviewed.
            lucene_query = _lucene_or_query(query)
            if not lucene_query:
                continue
            for dataset_scope in ("foundation_foods", "sr_legacy"):
                raw_hits = runtime.kg.search(
                    "renally_food_record_search", lucene_query,
                    k=query_limit, dataset_scope=dataset_scope,
                )
                for hit in raw_hits:
                    record_hit(category, query, hit, source_kind="bounded_fulltext_search")

    truncated = {}
    eligible_candidate_counts = {}
    selected_by_category = {}
    baseline_attributes = {
        category: attributes_from_usda_description(category, category)
        for category in REVIEW_SEARCH_QUERIES
    }

    def evaluate_candidate(category, candidate, description):
        scored = []
        for item in candidate["search_evidence"]:
            conflicts, requested, observed = _query_attribute_conflicts(category, item["query"], description)
            unmatched = []
            for key, expected in requested.items():
                wanted = set(expected if isinstance(expected, list) else [expected])
                actual = observed.get(key)
                got = set(actual if isinstance(actual, list) else [actual]) if actual is not None else set()
                if not wanted or not wanted.issubset(got):
                    unmatched.append(key)
            scored.append({
                **item,
                "conflicts": conflicts,
                "requested_attributes": requested,
                "unmatched_requested_attributes": sorted(unmatched),
                "specificity": len(requested),
            })
        scored.sort(key=lambda item: (
            bool(item["conflicts"]), -item["specificity"],
            bool(item["unmatched_requested_attributes"]),
            item.get("identity_match") != "exact_identity",
            len(item.get("unresolved_attributes") or []),
            -len(item["query"]),
        ))
        best = scored[0] if scored else None
        if best is None or best["conflicts"]:
            compatibility = "incompatible"
        elif (best["specificity"] == 0
              or best["unmatched_requested_attributes"]
              or best.get("unresolved_attributes")):
            compatibility = "ambiguous"
        elif best.get("identity_match") == "exact_identity":
            compatibility = "exact"
        else:
            compatibility = "compatible"
        return compatibility, best, scored

    for category in REVIEW_SEARCH_QUERIES:
        candidates = list(found[category].values())

        screened = []
        for candidate in candidates:
            compatibility, evidence, _ = evaluate_candidate(category, candidate, candidate["description"])
            if compatibility == "incompatible":
                excluded[category].setdefault(str(candidate["fdc_id"]), {
                    "canonical_ingredient_category": category,
                    "fdc_id": int(candidate["fdc_id"]),
                    "food_record_id": str(candidate["food_record_id"]),
                    "usda_description": candidate["description"],
                    "usda_data_type": candidate.get("data_type"),
                    "dataset_key": candidate.get("dataset_key"),
                    "status": "EXCLUDED_INCOMPATIBLE",
                    "reason": "Conflicts with all relevant targeted query contexts: "
                             + ", ".join(evidence.get("conflicts", [])) + ".",
                    "search_query": evidence.get("query") if evidence else None,
                })
            else:
                screened.append(candidate)
        candidates = screened
        eligible_candidate_counts[category] = len(candidates)
        def rank(candidate):
            compatibility, best, _ = evaluate_candidate(category, candidate, candidate["description"])
            return (
                compatibility == "incompatible",
                compatibility == "ambiguous",
                -(best["specificity"] if best else 0),
                bool(best and best.get("identity_match") != "exact_identity"),
                len(best.get("unresolved_attributes") or []) if best else 99,
                candidate["description"].casefold(),
                candidate["fdc_id"],
            )
        candidates.sort(key=rank)

        # Select a representative candidate for each distinct explicitly
        # searched context first (raw/cooked, white/brown, species, etc.).
        # Then fill remaining slots by deterministic compatibility rank.
        queries = sorted(
            REVIEW_SEARCH_QUERIES[category],
            key=lambda query: (-len(attributes_from_usda_description(category, query)), -len(query), query),
        )
        selected, signatures = [], set()
        for query in queries:
            matching = [candidate for candidate in candidates
                        if any(item["query"] == query for item in candidate["search_evidence"])]
            requested = attributes_from_usda_description(category, query)
            matching.sort(key=lambda candidate: (
                len(set(attributes_from_usda_description(category, candidate["description"])) - set(requested)),
                candidate["description"].casefold(),
                candidate["fdc_id"],
            ))
            for candidate in matching:
                compatibility, best, _ = evaluate_candidate(category, candidate, candidate["description"])
                if compatibility == "incompatible" or not best or best["conflicts"]:
                    continue
                attrs = attributes_from_usda_description(category, candidate["description"])
                signature = (_candidate_signature(category, attrs), candidate.get("dataset_key"))
                if signature in signatures:
                    continue
                selected.append(candidate)
                signatures.add(signature)
                break
            if len(selected) >= max_candidates_per_category:
                break
        for candidate in candidates:
            if len(selected) >= max_candidates_per_category:
                break
            if candidate not in selected:
                selected.append(candidate)

        truncated[category] = max(0, len(candidates) - len(selected))
        selected_by_category[category] = selected

    fdc_ids = sorted({int(candidate["fdc_id"])
                      for rows in selected_by_category.values() for candidate in rows})
    if len(fdc_ids) > _MAX_CATALOG_IDS_PER_READ:
        raise ValueError("Candidate retrieval exceeded the bounded 256-record nutrient read.")
    raw_records = runtime.kg.get_usda_food_records_by_fdc_ids(fdc_ids)
    records_by_id = {}
    for row in raw_records:
        food = row.get("food") or {}
        if food.get("fdc_id") is not None:
            records_by_id[str(food["fdc_id"])] = {"food": food, "nutrients": row.get("nutrients") or []}

    entries = []
    for category in REVIEW_SEARCH_QUERIES:
        for candidate in selected_by_category[category]:
            record = records_by_id.get(str(candidate["fdc_id"]))
            if record is None:
                # Search index IDs must still resolve to a local FoodRecord.
                continue
            food = record["food"]
            description = food.get("description") or candidate["description"]
            attributes = attributes_from_usda_description(category, description)
            candidate_missing = sorted(set(_REQUIRED_ATTRIBUTES[category]) - set(attributes))
            recipe_missing = sorted(set(_REQUIRED_ATTRIBUTES[category]) - set(baseline_attributes[category]))
            compatibility, best_evidence, _ = evaluate_candidate(category, candidate, description)
            notes = []
            if compatibility == "incompatible":
                conflicts = best_evidence.get("conflicts", []) if best_evidence else []
                notes.append("Conflicts with all specific search contexts: " + ", ".join(conflicts) + ".")
            if candidate_missing:
                notes.append("USDA description does not state: " + ", ".join(candidate_missing) + ".")
            if recipe_missing:
                notes.append("Generic ingredient wording leaves unspecified: " + ", ".join(recipe_missing) + ".")
            if compatibility == "ambiguous":
                notes.append("The candidate is suitable only for a narrower reviewer-defined scope.")
            if not notes:
                notes.append("Catalog description and targeted search are compatible; human approval is still required.")
            nutrient_summary = _nutrient_summary(record["nutrients"], food.get("nutrition_basis"))
            row_id = hashlib.sha256(
                f"{category}|{candidate['fdc_id']}|{candidate['food_record_id']}".encode("utf-8")
            ).hexdigest()[:20]
            entries.append({
                "review_entry_id": row_id,
                "canonical_ingredient_category": category,
                "proposed_fdc_id": int(candidate["fdc_id"]),
                "food_record_id": str(food.get("id") or candidate["food_record_id"]),
                "usda_description": description,
                "usda_data_type": food.get("data_type") or candidate.get("data_type"),
                "dataset_key": food.get("dataset_key") or candidate.get("dataset_key"),
                "nutrition_basis": food.get("nutrition_basis") or None,
                "source_url": food.get("source_url") or None,
                "relevant_attributes": attributes,
                "proposed_reference_scope": _scope(category, description, attributes, candidate_missing),
                "nutritional_data_availability": {
                    name: {key: value for key, value in nutrient.items()
                           if key in {"fdc_nutrient_id", "name", "availability", "unit", "normalized_unit"}}
                    for name, nutrient in nutrient_summary.items()
                },
                "nutrients_per_100g": nutrient_summary,
                "identity_compatibility": compatibility,
                "missing_attributes": recipe_missing,
                "candidate_description_missing_attributes": candidate_missing,
                "generic_default_consideration": "NOT_PROPOSED_PENDING_HUMAN_DECISION",
                "review_status": "PENDING_REVIEW",
                "review_notes": " ".join(notes),
                "reference_policy_version": REFERENCE_POLICY_VERSION,
                "best_search_evidence": best_evidence,
                "search_evidence": candidate["search_evidence"],
                "reviewer_identity": None,
                "review_provenance": None,
                "reviewed_at": None,
            })

    return {
        "manifest_version": 1,
        "reference_policy_version": REFERENCE_POLICY_VERSION,
        "generated_at": _now(),
        "source": "Read-only searches and nutrient reads from the local Neo4j USDA FoodData Central catalog.",
        "max_candidates_per_category": int(max_candidates_per_category),
        "query_limit_per_search": int(query_limit),
        "persistent_writes_performed": False,
        "slm_used_for_nutrients_or_approval": False,
        "candidate_counts_by_category": eligible_candidate_counts,
        "retrieved_candidate_counts_by_category": {category: len(found[category]) for category in REVIEW_SEARCH_QUERIES},
        "excluded_candidate_counts_by_category": {category: len(excluded[category]) for category in REVIEW_SEARCH_QUERIES},
        "additional_candidates_omitted_by_bound": truncated,
        "excluded_candidates": [item for category in REVIEW_SEARCH_QUERIES
                                for item in list(excluded[category].values())[:5]],
        "entries": entries,
    }


def validate_approval_scope(category, usda_description, attributes):
    """Require a narrow scope that matches USDA-stated attributes and labels gaps."""
    if category not in _REQUIRED_ATTRIBUTES:
        raise ValueError("The ingredient category has no reference approval scope policy.")
    if not isinstance(attributes, dict) or not attributes:
        raise ValueError("Approval requires a non-empty reviewer-confirmed reference scope.")
    scope = _normalize_attribute_map(attributes)
    missing = sorted(set(_REQUIRED_ATTRIBUTES[category]) - set(scope))
    if missing:
        raise ValueError(
            "Approval scope must cover every identity-critical attribute; use 'unspecified' "
            "when the USDA description does not state one: " + ", ".join(missing) + "."
        )

    source = _normalize_attribute_map(attributes_from_usda_description(category, usda_description))
    for key, value in source.items():
        if key not in scope:
            raise ValueError(f"Approval scope omits the USDA-stated attribute {key!r}.")
        if scope[key] != value:
            raise ValueError(f"Approval scope conflicts with the USDA-stated attribute {key!r}.")
    for key, value in scope.items():
        if key not in source and value != "unspecified":
            raise ValueError(
                f"Approval scope asserts {key!r}={value!r}, which the USDA description does not establish; "
                "use 'unspecified' or provide a different candidate."
            )
    return scope


def apply_manifest_decision(runtime, manifest, *, category, fdc_id, decision,
                            reviewer_identity=None, provenance=None, attributes=None,
                            allow_as_default=False, rationale=None, confirm_persist=False):
    """Preview by default; persist one explicit human decision after confirmation."""
    if manifest.get("reference_policy_version") != runtime.references.policy_version:
        raise ValueError("The review manifest uses a different reference-policy version.")
    if decision not in {"approve", "reject"}:
        raise ValueError("Decision must be approve or reject.")
    target_status = "APPROVED" if decision == "approve" else "REJECTED"
    matches = [entry for entry in manifest.get("entries", [])
               if entry.get("canonical_ingredient_category") == category
               and str(entry.get("proposed_fdc_id")) == str(fdc_id)]
    if len(matches) != 1:
        raise ValueError("The selected FDC ID must match exactly one entry in this manifest.")
    entry = matches[0]
    if entry.get("review_status") != "PENDING_REVIEW":
        raise ValueError("This manifest entry already has a recorded decision.")
    if decision == "approve" and entry.get("identity_compatibility") == "incompatible":
        raise ValueError("A candidate marked incompatible cannot be approved from this manifest.")
    if (decision == "approve" and entry.get("review_recommendation")
            and entry.get("review_recommendation") != "READY_FOR_HUMAN_APPROVAL"):
        raise ValueError("Only a candidate recommended READY_FOR_HUMAN_APPROVAL can be approved from this proposal.")
    record_id, local_food, local_source = runtime.references._local_food(
        fdc_id, entry.get("food_record_id"),
    )
    local_description = local_food.get("fdc_description") or local_food.get("name")
    if local_description and entry.get("usda_description") != local_description:
        raise ValueError("The manifest USDA description no longer matches the selected local FDC record.")
    if (entry.get("dataset_key") and local_source.get("dataset_key")
            and entry.get("dataset_key") != local_source.get("dataset_key")):
        raise ValueError("The manifest dataset no longer matches the selected local FDC record.")
    if (entry.get("usda_data_type") and local_source.get("data_type")
            and entry.get("usda_data_type") != local_source.get("data_type")):
        raise ValueError("The manifest USDA data type no longer matches the selected local FDC record.")
    approved_scope = (validate_approval_scope(category, local_description or entry.get("usda_description"), attributes)
                      if decision == "approve" else {})
    if not confirm_persist:
        return {
            "write_performed": False,
            "review_status": "PENDING_REVIEW",
            "message": "Read-only preview. Re-run with --confirm-persist to apply this explicit decision.",
            "canonical_ingredient_category": category,
            "fdc_id": int(fdc_id),
            "food_record_id": str(record_id),
            "decision": decision.upper(),
        }
    if not str(reviewer_identity or "").strip():
        raise ValueError("An explicit reviewer identity is required for persistence.")
    if provenance is None or provenance == "" or provenance == {} or provenance == []:
        raise ValueError("Explicit review provenance is required for persistence.")
    if decision == "reject" and allow_as_default:
        raise ValueError("Rejected references cannot be enabled as defaults.")

    for current in runtime.references.list_references(category, include_rejected=True):
        same_record = str(current.get("fdc_id")) == str(fdc_id) and str(current.get("food_record_id")) == str(record_id)
        decision_attributes = approved_scope if decision == "approve" else _normalize_attribute_map(attributes or {})
        same_attributes = (current.get("attributes") or {}) == decision_attributes
        if same_record and same_attributes and current.get("reference_status") == target_status:
            raise ValueError("This exact reference decision already exists; duplicate approval/action blocked.")

    review = getattr(runtime.references, "approve_reference" if decision == "approve" else "reject_reference")
    stored = review(
        category, fdc_id, food_record_id=str(record_id),
        reviewer_identity=reviewer_identity, provenance=provenance,
        attributes=approved_scope, allow_as_default=bool(allow_as_default),
        rationale=rationale,
    )
    if not stored or stored.get("reference_status") != target_status:
        raise RuntimeError("Neo4j did not confirm the selected reference decision.")
    persisted = runtime.references.list_references(category, include_rejected=True)
    if not any(str(row.get("fdc_id")) == str(fdc_id)
               and str(row.get("food_record_id")) == str(record_id)
               and row.get("reference_status") == target_status
               and row.get("reference_policy_version") == runtime.references.policy_version
               for row in persisted):
        raise RuntimeError("The applied reference decision could not be retrieved from Neo4j.")
    entry.update({
        "review_status": target_status,
        "reviewer_identity": reviewer_identity,
        "review_provenance": provenance,
        "reviewed_at": stored.get("updated_at"),
        "review_notes": rationale or entry.get("review_notes"),
        "reviewer_confirmed_attributes": approved_scope,
        "allow_as_default": bool(allow_as_default) if decision == "approve" else False,
    })
    return {"write_performed": True, "review_status": target_status, "reference": stored}


def render_review_report(manifest):
    """Render the manifest as a compact human-review Markdown report."""
    entries = manifest.get("entries", [])
    lines = [
        "# USDA Reference Review — Initial Candidate Set",
        "",
        f"Generated: {manifest.get('generated_at')}",
        f"Reference-policy version: {manifest.get('reference_policy_version')}",
        f"Review entries: {sum(row.get('review_status') == 'PENDING_REVIEW' for row in entries)} pending; {sum(row.get('review_status') == 'APPROVED' for row in entries)} approved.",
        f"Searches per query: bounded to {manifest.get('query_limit_per_search')} per dataset/source; candidate rows per category: {manifest.get('max_candidates_per_category')}.",
        "",
        "This report contains candidates from the local Neo4j USDA catalog. All entries begin as `PENDING_REVIEW`. "
        "No candidate has been automatically approved, and no reference or mapping writes are made during generation.",
        "Candidate identity labels describe fit with targeted search wording; they are not approval decisions. "
        "No generic ingredient is assigned a default by this review packet.",
        "Nutrient values are sourced from the local catalog by FDC nutrient ID and shown per 100 g only when the "
        "imported basis and units support that normalization. Missing values are shown as `—`, never zero.",
        "",
    ]
    for category in REVIEW_SEARCH_QUERIES:
        rows = [entry for entry in entries if entry["canonical_ingredient_category"] == category]
        lines.extend([f"## {category.title()}", ""])
        lines.extend([
            f"Reviewable candidates found: {manifest.get('candidate_counts_by_category', {}).get(category, len(rows))}; "
            f"shown: {len(rows)}; additional reviewable candidates omitted by the table bound: "
            f"{manifest.get('additional_candidates_omitted_by_bound', {}).get(category, 0)}.",
            "",
        ])
        if not rows:
            lines.extend(["No matching local USDA records were returned by the bounded targeted searches.", ""])
            continue
        lines.extend([
            "| FDC ID / USDA description / type | Candidate attributes | Na mg/100g | K mg/100g | P mg/100g | Protein g/100g | Compatibility | Missing from generic recipe wording | Missing from USDA description | Proposed scope / notes | Review |",
            "|---|---|---:|---:|---:|---:|---|---|---|---|---|",
        ])
        for entry in rows:
            nutrients = entry["nutrients_per_100g"]
            def val(key):
                item = nutrients[key]
                if item["availability"] != "AVAILABLE":
                    return "—"
                return f"{item['normalized_amount_per_100g']:.4g}"
            attrs = entry["relevant_attributes"]
            attr_text = "; ".join(
                f"{key}={'/'.join(value) if isinstance(value, list) else value}"
                for key, value in sorted(attrs.items())
            ) or "none stated"
            lines.append(
                f"| {entry['proposed_fdc_id']} — {entry['usda_description']} ({entry.get('usda_data_type') or 'type missing'}) "
                f"| {attr_text} | {val('sodium')} | {val('potassium')} | {val('phosphorus')} | {val('protein')} "
                f"| {entry['identity_compatibility']} | {', '.join(entry['missing_attributes']) or 'none'} "
                f"| {', '.join(entry['candidate_description_missing_attributes']) or 'none'} "
                f"| {entry['proposed_reference_scope']} {entry['review_notes']} "
                f"| {entry['review_status']} |"
            )
        lines.append("")
    excluded = manifest.get("excluded_candidates", [])
    lines.extend(["## Excluded incompatible products", ""])
    if excluded:
        lines.extend([
        "These local USDA records appeared in bounded full-text results but were excluded because the primary food identity or the explicit ingredient context did not match the reference category. They are not approval candidates.",
            "",
            "| Category | FDC ID | USDA description | Reason |",
            "|---|---:|---|---|",
        ])
        for item in excluded:
            lines.append(
                f"| {item['canonical_ingredient_category']} | {item['fdc_id']} | "
                f"{item['usda_description']} | {item['reason']} |"
            )
        lines.append("")
    else:
        lines.extend(["No clearly incompatible food products were returned within the bounded search results.", ""])
    lines.extend([
        "## Nutrient provenance and approval boundary",
        "",
        "The JSON manifest preserves each measured nutrient's FDC nutrient identifier, USDA nutrient name, original unit, "
        "amount, and imported nutrition basis. Sodium, potassium, and phosphorus use only FDC nutrient IDs 1093, 1092, "
        "and 1091 respectively; protein uses ID 1003. Phosphate is not treated as phosphorus. A nutrient absent from "
        "the catalog or recorded in an unsupported unit is not used as a normalized comparison value.",
        "",
        "The `decide` CLI previews by default. A persistent approval or rejection requires `--confirm-persist`, an explicit "
        "reviewer identity, provenance, and an entry in the generated manifest. Even an approved reference remains separate "
        "from a VERIFIED ingredient mapping and cannot confirm recipe constraints.",
        "",
    ])
    return "\n".join(lines)

"""Neo4j full-text retrieval for USDA FoodData Central foods and recipes."""
import re
from itertools import islice


STOP_WORDS = {
    "a", "an", "and", "are", "can", "could", "do", "does", "for", "from",
    "have", "i", "in", "is", "it", "me", "my", "of", "on", "please", "recommend",
    "the", "to", "want", "what", "with", "would", "you", "eat", "food", "foods",
    "recipe", "recipes", "dish", "dishes", "meal", "meals", "today", "healthy",
}

# Bump when the deterministic ingredient-to-USDA ranking rules change. This is
# intentionally separate from the mapping decision version: previously
# human-reviewed mappings remain valid and are never overwritten by reranking.
INGREDIENT_RETRIEVAL_VERSION = "ingredient-rerank-v4"
_STATE_WORDS = {
    "raw", "uncooked", "cooked", "smoked", "dried", "canned", "frozen", "kippered",
    "boiled", "grilled", "baked", "fried", "roasted", "steamed",
    "poached", "fermented", "pickled", "cured",
}
_PROCESSING_WORDS = {
    "smoked", "dried", "canned", "frozen", "fermented", "pickled", "cured", "kippered",
}
_SINGULARS = {"eggs": "egg", "oils": "oil", "apples": "apple",
             "breasts": "breast", "steaks": "steak"}
_ALT_BUTTER_MODIFIERS = {
    "almond", "cashew", "peanut", "sesame", "sunflower", "seed", "tahini",
}
_PLANT_MILK_MODIFIERS = {
    "almond", "buttermilk", "cashew", "coconut", "hemp", "imitation",
    "oat", "rice", "soy", "soya",
}
_EGG_PARTS = {"white", "yolk", "substitute"}
_PRODUCT_PROCESSING_MARKERS = {
    "bar", "bars", "battered", "beverage", "beverages", "breaded", "burger", "burgers",
    "cocoa", "custard", "custards", "dessert", "desserts", "imitation", "milkshake",
    "nugget", "nuggets", "omelet", "omelette", "patty", "patties",
    "replacement", "sausage", "sausages", "scrambled", "shake", "shakes",
    "substitute", "substitutes", "mix",
}
_MILK_PROCESSED_FORMS = {"condensed", "dry", "evaporated", "powder", "powdered", "sweetened"}
_SALMON_PARTS = {"liver", "roe"}
_MILK_ANIMAL_SPECIES = {"bovine", "buffalo", "camel", "cow", "goat", "sheep"}
_OIL_VARIETIES = {
    "avocado", "canola", "coconut", "corn", "grapeseed", "peanut",
    "rapeseed", "safflower", "sesame", "soy", "soybean", "sunflower", "vegetable",
}
_LEADING_VARIETY_MODIFIERS = {
    "butter": {"clarified", "salted", "unsalted", "whipped"},
    "egg": {"whole", "white", "yolk"},
    "milk": {"condensed", "evaporated", "lowfat", "nonfat", "reducedfat", "skim", "whole"},
    "oil": {"avocado", "canola", "coconut", "corn", "grapeseed", "olive",
            "peanut", "rapeseed", "safflower", "sesame", "soy", "soybean",
            "sunflower", "vegetable"},
    "rice": {"brown", "red", "white", "wild"},
    "salmon": {"atlantic", "chinook", "coho", "chum", "pink", "sockeye", "steelhead"},
    "salt": {"iodized", "kosher", "rock", "sea", "table"},
    "sugar": {"brown", "cane", "confectioners", "granulated", "raw", "turbinado", "white"},
}
_VARIETY_GROUPS = {
    "rice_color": {"white", "brown", "wild", "red", "black"},
    "rice_grain": {"long", "medium", "short"},
    "salmon_species": {"atlantic", "sockeye", "pink", "chinook", "coho", "chum", "steelhead"},
    "milk_fat": {"whole", "skim", "nonfat", "lowfat", "reducedfat", "2"},
    "butter_salt": {"salted", "unsalted"},
    "oil_processing": {"extra", "virgin", "refined", "light"},
}
_VARIETY_HEADS = {
    "rice_color": "rice", "rice_grain": "rice", "salmon_species": "salmon",
    "milk_fat": "milk", "butter_salt": "butter", "oil_processing": "oil",
}
_KNOWN_INGREDIENT_HEADS = {
    "butter", "chicken", "egg", "milk", "oil", "rice", "salmon", "salt", "sugar",
}


def _normalized_tokens(value):
    raw_tokens = re.findall(r"[a-z0-9]+", str(value or "").casefold())
    tokens = []
    index = 0
    while index < len(raw_tokens):
        token = raw_tokens[index]
        if token in {"low", "reduced"} and index + 1 < len(raw_tokens) and raw_tokens[index + 1] == "fat":
            token += "fat"
            index += 1
        elif token in {"without", "no"} and index + 1 < len(raw_tokens) and raw_tokens[index + 1] == "salt":
            token = "unsalted"
            index += 1
        elif token == "with" and index + 1 < len(raw_tokens) and raw_tokens[index + 1] == "salt":
            token = "salted"
            index += 1
        tokens.append(_SINGULARS.get(token, token))
        index += 1
    return tokens


def _legacy_resolver_score(query, labels):
    """Keep the pre-reranker resolver score for cache/API compatibility."""
    normalized_query = " ".join(_normalized_tokens(query))
    query_tokens = set(normalized_query.split()) - STOP_WORDS
    best = 0.0
    for label in labels:
        normalized_label = " ".join(_normalized_tokens(label))
        label_tokens = set(normalized_label.split()) - STOP_WORDS
        if normalized_query == normalized_label:
            best = max(best, 1.25)
        elif query_tokens and query_tokens.issubset(label_tokens):
            best = max(best, 0.8 + 0.19 * len(query_tokens) / max(1, len(label_tokens)))
    return best


def _query_profile(query):
    tokens = _normalized_tokens(query)
    token_set = set(tokens)
    heads = token_set & _KNOWN_INGREDIENT_HEADS
    if len(heads) != 1:
        return None
    head = next(iter(heads))
    # "soy milk" and "peanut butter" are explicit plant ingredients. Keep
    # those specific requests distinct from generic dairy milk or butter.
    return {
        "head": head,
        "tokens": token_set,
        "states": token_set & _STATE_WORDS,
        "processing": token_set & _PROCESSING_WORDS,
        "varieties": {
            group: token_set & members for group, members in _VARIETY_GROUPS.items()
            if head == _VARIETY_HEADS[group] and token_set & members
        },
    }


def _unresolved_specificity(profile, description_tokens):
    """Identify candidate attributes that the source ingredient did not state."""
    head = profile["head"]
    unresolved = set()
    groups_by_head = {
        "butter": ("butter_salt",), "milk": ("milk_fat",),
        "oil": ("oil_processing",), "rice": ("rice_color", "rice_grain"),
        "salmon": ("salmon_species",),
    }
    attribute_names = {
        "butter_salt": "saltedness", "milk_fat": "milk_fat_percentage",
        "oil_processing": "oil_processing", "rice_color": "rice_variety",
        "rice_grain": "rice_grain", "salmon_species": "species",
    }
    for group in groups_by_head.get(head, ()):
        observed = description_tokens & _VARIETY_GROUPS[group]
        if observed and group not in profile["varieties"]:
            unresolved.add(attribute_names[group])

    if head == "egg":
        observed_parts = description_tokens & _EGG_PARTS
        if observed_parts and not (profile["tokens"] & _EGG_PARTS):
            unresolved.add("egg_part")
        egg_species = description_tokens & {"duck", "goose", "quail", "turkey"}
        if egg_species and not (profile["tokens"] & {"duck", "goose", "quail", "turkey"}):
            unresolved.add("egg_species")
    if head == "butter":
        if description_tokens & {"clarified", "ghee", "whipped", "light"} and not profile["tokens"] & {"clarified", "ghee", "whipped", "light"}:
            unresolved.add("butter_processing")
    if head == "chicken":
        requested = profile["tokens"]
        if description_tokens & {"skin", "skinless"} and not requested & {"skin", "skinless"}:
            unresolved.add("skin_state")
        if description_tokens & {"bone", "boneless"} and not requested & {"bone", "boneless"}:
            unresolved.add("bone_state")
        if {"added", "solution"}.issubset(description_tokens) and "solution" not in requested:
            unresolved.add("added_solution")
        if "meat" in description_tokens and "skin" not in description_tokens and not requested & {"skin", "skinless"}:
            unresolved.add("meat_and_skin_composition")
    if head == "milk":
        if "milkfat" in description_tokens and "milkfat" not in profile["tokens"]:
            unresolved.add("milk_fat_percentage")
        if {"low", "sodium"}.issubset(description_tokens) and not {"low", "sodium"}.issubset(profile["tokens"]):
            unresolved.add("milk_sodium_modifier")
        observed_species = description_tokens & _MILK_ANIMAL_SPECIES
        if observed_species and not (profile["tokens"] & _MILK_ANIMAL_SPECIES):
            unresolved.add("milk_species")
        if "chocolate" in description_tokens and "chocolate" not in profile["tokens"]:
            unresolved.add("milk_flavor")
    return sorted(unresolved)


def _candidate_match(query, row, data):
    """Return deterministic match features, or None for a clear wrong product."""
    description = (data.get("fdc_description") or row.get("name")
                   or data.get("name") or "")
    aliases = list(data.get("aliases") or row.get("aliases") or [])
    labels = [description, *aliases]
    query_tokens = set(_normalized_tokens(query)) - STOP_WORDS
    description_tokens = set(_normalized_tokens(description))
    primary = description.split(",", 1)[0]
    primary_tokens = set(_normalized_tokens(primary))
    profile = _query_profile(query)
    unresolved_specificity = []

    if profile:
        head = profile["head"]
        # Requiring the USDA description's leading food identity removes
        # "with salt added", milk-containing crackers, and foods packed in oil.
        primary_words = _normalized_tokens(primary)
        primary_identity_matches = head in primary_tokens and bool(primary_words)
        if (primary_identity_matches and primary_words[0] != head
                and primary_words[0] not in profile["tokens"]
                and primary_words[0] not in _LEADING_VARIETY_MODIFIERS.get(head, set())):
            primary_identity_matches = False
        # USDA often prefixes finfish descriptions with the broad class
        # "Fish," before naming salmon; fish oil remains a different primary
        # food identity ("Fish oil,").
        if head == "salmon" and primary_tokens == {"fish"} and "salmon" in description_tokens:
            primary_identity_matches = True
        if not primary_identity_matches:
            return None
        if "salad" in primary_tokens and "salad" not in profile["tokens"]:
            return None
        if head == "butter" and not (profile["tokens"] & _ALT_BUTTER_MODIFIERS):
            if description_tokens & _ALT_BUTTER_MODIFIERS:
                return None
        if head == "butter" and description_tokens & {"clarified", "ghee"}:
            if not profile["tokens"] & {"clarified", "ghee"}:
                return None
        if head == "milk" and not (profile["tokens"] & _PLANT_MILK_MODIFIERS):
            if description_tokens & _PLANT_MILK_MODIFIERS:
                return None
            if description_tokens & _MILK_PROCESSED_FORMS and not profile["tokens"] & _MILK_PROCESSED_FORMS:
                return None
        if head == "egg" and not (profile["tokens"] & _EGG_PARTS):
            if description_tokens & _EGG_PARTS:
                return None
        if head == "rice" and {"noodle", "noodles"} & set(_normalized_tokens(primary)):
            return None
        if head == "salmon" and description_tokens & _SALMON_PARTS:
            if not profile["tokens"] & _SALMON_PARTS:
                return None

        # Do not let a generic olive-oil query match fish or prepared food that
        # merely lists olive oil as an ingredient.
        if head == "oil" and "olive" in profile["tokens"]:
            if "olive" not in description_tokens:
                return None
            other_oils = (description_tokens & _OIL_VARIETIES) - {"olive"} - (profile["tokens"] & _OIL_VARIETIES)
            if other_oils:
                return None

        if head == "butter" and "oil" in primary_tokens and "oil" not in profile["tokens"]:
            return None
        if head == "sugar" and "apple" in primary_tokens and "apple" not in profile["tokens"]:
            return None

        if not (profile["tokens"] & _PRODUCT_PROCESSING_MARKERS):
            if description_tokens & _PRODUCT_PROCESSING_MARKERS:
                return None

        # An explicitly requested cut must be present in the USDA description.
        requested_cuts = profile["tokens"] & {"breast", "steak", "fillet", "thigh", "wing"}
        if requested_cuts and not requested_cuts.issubset(description_tokens):
            return None

        unresolved_specificity = _unresolved_specificity(profile, description_tokens)
        variety_match = "candidate_more_specific" if unresolved_specificity else "unspecified"
        for group, requested in profile["varieties"].items():
            observed = description_tokens & _VARIETY_GROUPS[group]
            if not observed or requested != observed:
                return None
            variety_match = "explicit_match"

        requested_state = profile["states"]
        observed_state = description_tokens & _STATE_WORDS
        if "uncooked" in requested_state:
            requested_state.add("raw")
        if "uncooked" in observed_state:
            observed_state.add("raw")
        if requested_state and (
                not observed_state or not requested_state.intersection(observed_state)):
            return None
        state_match = (
            "explicit_match" if requested_state and requested_state & observed_state
            else "description_unspecified" if requested_state
            else "candidate_more_specific" if observed_state
            else "query_unspecified"
        )

        requested_processing = profile["processing"]
        observed_processing = description_tokens & _PROCESSING_WORDS
        if requested_processing and (
                not observed_processing or not requested_processing.intersection(observed_processing)):
            return None
        processing_match = (
            "explicit_match" if requested_processing and requested_processing & observed_processing
            else "description_unspecified" if requested_processing
            else "candidate_more_specific" if observed_processing
            else "query_unspecified"
        )

        identity_tokens = query_tokens - _STATE_WORDS
        if identity_tokens and identity_tokens.issubset(description_tokens):
            identity = "exact_identity"
        elif any(set(_normalized_tokens(alias)) - STOP_WORDS == query_tokens for alias in aliases):
            identity = "approved_synonym"
        else:
            identity = "head_noun"
    else:
        # Preserve the general resolver's historical behavior for ingredients
        # without a special product taxonomy, while still ranking full-label
        # and approved-alias matches ahead of incidental lexical overlap.
        score = _legacy_resolver_score(query, labels)
        if score <= 0:
            return None
        if query_tokens and query_tokens.issubset(description_tokens):
            identity = "exact_identity"
        elif any(set(_normalized_tokens(alias)) - STOP_WORDS == query_tokens for alias in aliases):
            identity = "approved_synonym"
        else:
            identity = "head_noun"
        variety_match = state_match = processing_match = "not_applicable"

    legacy_score = _legacy_resolver_score(query, labels)
    overlap = len(query_tokens & description_tokens)
    lexical_similarity = overlap / max(1, len(query_tokens | description_tokens))
    raw_neo4j_score = float(row.get("score") or 0.0)
    identity_rank = {"exact_identity": 4, "approved_synonym": 3, "head_noun": 2}.get(identity, 1)
    variety_rank = {"explicit_match": 2, "unspecified": 1, "candidate_more_specific": 0}.get(variety_match, 0)
    state_rank = {"explicit_match": 2, "description_unspecified": 1,
                  "query_unspecified": 1, "candidate_more_specific": 0}.get(state_match, 0)
    processing_rank = {"explicit_match": 2, "description_unspecified": 1,
                       "query_unspecified": 1, "candidate_more_specific": 0}.get(processing_match, 0)
    neo4j_tiebreak = max(0.0, min(raw_neo4j_score, 999.0)) / 1000.0
    relevance_score = (
        identity_rank * 1000.0 + variety_rank * 100.0 + state_rank * 10.0
        + processing_rank + lexical_similarity / 10.0 + neo4j_tiebreak / 100.0
    )
    return {
        "identity_match": identity,
        "variety_compatibility": variety_match,
        "preparation_state_compatibility": state_match,
        "processing_state_compatibility": processing_match,
        "unresolved_attributes": unresolved_specificity,
        "lexical_similarity": round(lexical_similarity, 6),
        "legacy_resolver_score": legacy_score,
        "neo4j_retrieval_score": raw_neo4j_score,
        "relevance_score": round(relevance_score, 6),
    }


def _lucene_query(text):
    """Build a safe OR query from user words; never pass Lucene operators through."""
    tokens = []
    seen = set()
    for raw_token in re.findall(r"[a-z0-9]+", str(text).casefold()):
        token = _SINGULARS.get(raw_token, raw_token)
        variants = [token]
        if token == "egg":
            variants.append("eggs")
        elif raw_token != token:
            variants.append(raw_token)
        for variant in variants:
            if len(variant) < 2 or variant in STOP_WORDS or variant in seen:
                continue
            tokens.append(variant)
            seen.add(variant)
            if len(tokens) == 12:
                return " OR ".join(tokens)
    return " OR ".join(tokens)


def _score(row, query):
    query_text = " ".join(str(query).casefold().split())
    labels = [str(row.get("name") or "").casefold()]
    labels.extend(str(value).casefold() for value in (row.get("aliases") or []))
    phrase = max((len(label) for label in labels if label and label in query_text), default=0)
    if phrase:
        return 1.0 + min(phrase / 100.0, 0.25)
    raw_score = max(0.0, float(row.get("score") or 0.0))
    return 0.8 * raw_score / (1.0 + raw_score)


class GraphRetriever:
    """Retrieve catalog entries through Neo4j indexes and expand graph context."""

    def __init__(self, kg):
        self.kg = kg

    def _retrieve(self, query, index_name, k, dataset_scope="all"):
        search_query = _lucene_query(query)
        if not search_query:
            return []
        rows = self.kg.search(
            index_name, search_query, k=max(k, 12), dataset_scope=dataset_scope,
        )
        ranked = [(row["id"], _score(row, query)) for row in rows]
        ranked.sort(key=lambda pair: pair[1], reverse=True)
        return ranked[:max(0, k)]

    def retrieve_ingredients(self, query, k=5, dataset_scope="all"):
        return self._retrieve(
            query, "renally_food_record_search", k, dataset_scope=dataset_scope,
        )

    def resolve_ingredient_candidates_with_evidence(self, query, k=5, dataset_scope="all"):
        """Return bounded ingredient candidates with auditable reranking evidence.

        Neo4j full-text search remains the discovery source. This pass removes
        clear product-category conflicts and orders the surviving records by
        identity, explicit variety/state matches, lexical overlap, then the
        original Neo4j score. Scores are ranking evidence, never probabilities.
        """
        search_query = _lucene_query(query)
        if not search_query:
            return []
        scope = str(dataset_scope or "all").casefold()
        scopes = ("foundation_foods", "sr_legacy") if scope == "all" else (scope,)
        search_limit = min(256, max(32, max(1, int(k)) * 8))
        candidates = []
        seen = set()
        for source_scope in scopes:
            rows = self.kg.search(
                "renally_food_record_search", search_query, k=search_limit,
                dataset_scope=source_scope,
            )
            for row in rows:
                food_id = row.get("id")
                if food_id is None or food_id in seen:
                    continue
                seen.add(food_id)
                data = (getattr(self.kg, "ingredients", {}) or {}).get(food_id, {})
                features = _candidate_match(query, row, data)
                if features is None:
                    continue
                candidates.append({"id": food_id, **features})
        # Final ID tiebreak makes results stable even if Neo4j returns equal
        # scores in a different order on a later run.
        candidates.sort(key=lambda item: (
            item["relevance_score"], item["legacy_resolver_score"], str(item["id"]),
        ), reverse=True)
        result_limit = max(0, int(k)) * len(scopes) if scope == "all" else max(0, int(k))
        return candidates[:result_limit]

    def resolve_ingredient_candidates(self, query, k=5, dataset_scope="all"):
        """Return ``(food_record_id, legacy_resolver_score)`` pairs.

        The tuple shape and legacy score are kept for existing callers. New
        mapping code uses ``resolve_ingredient_candidates_with_evidence`` to
        retain the Neo4j score and the separate relevance ranking.
        """
        return [
            (item["id"], item["legacy_resolver_score"])
            for item in self.resolve_ingredient_candidates_with_evidence(
                query, k=k, dataset_scope=dataset_scope,
            )
        ]

    def retrieve_nodes(self, query, k=3):
        if query in self.kg.recipes:
            return [(query, 1.25)]
        ranked = self._retrieve(query, "renally_recipe_search", k)
        if ranked:
            return ranked
        # Keep a deterministic catalog order for meal-plan requests with no
        # named dish; the pipeline will still validate every selected record.
        return [(recipe_id, 0.0) for recipe_id in islice(self.kg.recipes, max(0, k))]

    def search_recipes(self, query, k=3):
        """Search recipe records without the broad fallback used for meal plans."""
        if query in self.kg.recipes:
            return [(query, 1.25)]
        return self._retrieve(query, "renally_recipe_search", k)

    def recommend_foodcom_recipes(self, query, k=5):
        """Rank bounded Food.com hits using titles, tags, and listed ingredients.

        Scores express retrieval relevance only. They contain no nutritional
        estimate or dietary feasibility assessment.
        """
        text = str(query or "").casefold()
        low_sodium = bool(re.search(r"\b(?:low[ -]sodium|reduced[ -]sodium|low[ -]salt)\b", text))
        focus = re.sub(r"\b(?:suggest|recommend|some|what|can|i|eat|for|the|a|an|me|please|dishes|dish|recipes|recipe|meals|meal)\b", " ", text)
        focus = " ".join(re.findall(r"[a-z0-9]+", focus))
        focus = focus or "dinner"
        broad = focus in {"dinner", "lunch", "breakfast", "food"}
        search_text = "low sodium unsalted" if low_sodium else focus
        ranked = self.search_recipes(search_text, k=80)
        if broad and len(ranked) < max(1, k):
            seen = {recipe_id for recipe_id, _ in ranked}
            ranked.extend((recipe_id, score) for recipe_id, score in self.retrieve_nodes(focus, k=80)
                          if recipe_id not in seen)
        ids = [recipe_id for recipe_id, _ in ranked if self.kg.recipes.get(recipe_id, {}).get("source_dataset")
               == "Food.com Recipes and Reviews (Kaggle)"][:80]
        links = {}
        reader = getattr(self.kg, "foodcom_ingredient_links", None)
        if callable(reader) and ids:
            for row in reader(ids):
                if row.get("recipe_id") in ids and row.get("name"):
                    links.setdefault(row["recipe_id"], []).append(str(row["name"]))
        requested = set(re.findall(r"[a-z0-9]+", focus)) - {"dinner", "lunch", "breakfast", "low", "sodium", "salt"}
        results = []
        for recipe_id, base_score in ranked:
            if recipe_id not in ids:
                continue
            recipe = self.kg.recipes[recipe_id]
            name = str(recipe.get("name") or "")
            tags = [str(value) for value in (recipe.get("tags") or recipe.get("foodcom_tags") or [])]
            ingredients = links.get(recipe_id) or [str(value) for value in (recipe.get("ingredient_names") or
                                                                            recipe.get("foodcom_ingredient_names") or [])]
            name_tokens = set(_normalized_tokens(name))
            tag_text = " ".join(tags).casefold()
            tag_tokens = set(_normalized_tokens(tag_text))
            ingredient_tokens = set(_normalized_tokens(" ".join(ingredients)))
            label_text = f"{name} {tag_text}".casefold().replace("-", " ")
            if low_sodium and not any(term in label_text for term in ("low sodium", "reduced sodium", "low salt", "unsalted", "no salt")):
                continue
            overlap = requested & (name_tokens | tag_tokens | ingredient_tokens)
            if requested and not overlap and not low_sodium:
                continue
            title_hits = len(requested & name_tokens)
            tag_hits = len(requested & tag_tokens)
            ingredient_hits = len(requested & ingredient_tokens)
            score = round(float(base_score) + 0.3 * title_hits + 0.15 * tag_hits
                          + 0.1 * ingredient_hits + (0.2 if low_sodium else 0), 6)
            results.append({"recipe_id": recipe_id, "retrieval_score": score,
                            "ingredient_names": ingredients, "matched_ingredients": sorted(requested & ingredient_tokens)})
        results.sort(key=lambda row: (-row["retrieval_score"], row["recipe_id"]))
        return results[:max(1, min(int(k), 7))]

    def retrieve_recipe(self, query):
        if query in self.kg.recipes:
            return query, 1.25
        ranked = self._retrieve(query, "renally_recipe_search", 1)
        if not ranked or ranked[0][1] < 0.4:
            return None
        recipe_id, score = ranked[0]
        recipe = self.kg.recipes.get(recipe_id, {})
        query_text = " ".join(re.findall(r"[a-z0-9]+", str(query).casefold()))
        query_tokens = set(query_text.split()) - STOP_WORDS
        labels = [recipe.get("name", recipe_id), *(recipe.get("aliases") or [])]
        grounded = False
        for label in labels:
            normalized_label = " ".join(re.findall(r"[a-z0-9]+", str(label).casefold()))
            label_tokens = set(normalized_label.split()) - STOP_WORDS
            if not label_tokens:
                continue
            if normalized_label and re.search(
                r"(?<![a-z0-9])" + re.escape(normalized_label) + r"(?![a-z0-9])",
                query_text,
            ):
                grounded = True
                break
            overlap = query_tokens & label_tokens
            if len(label_tokens) == 1:
                if overlap and re.search(r"\b(?:recipe|dish|cook|cooking|prepare|make)\b", query_text):
                    grounded = True
                    break
            # A shared adjective or ingredient is not enough to identify a
            # recipe. Require all meaningful title tokens unless the complete
            # label was present above; the planner adds conversation grounding.
            elif label_tokens and label_tokens.issubset(query_tokens):
                grounded = True
                break
        # Full-text indexes return a ranked result for partial word overlap;
        # require name-level evidence before treating it as the requested dish.
        return (recipe_id, score) if grounded else None

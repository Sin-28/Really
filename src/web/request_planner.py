"""Bounded request planning over the local recipe and ingredient catalog."""
import json
import math
import re
from threading import Lock

from src.retrieval.graph_retriever import STOP_WORDS


class RequestPlanner:
    """Interpret free text with Qwen when available; otherwise route deterministically."""

    DATASET_SCOPES = {"all", "foundation_foods", "sr_legacy"}
    DATASET_SCOPE_ALIASES = {
        "all": "all",
        "foundation": "foundation_foods",
        "foundation foods": "foundation_foods",
        "foundation_foods": "foundation_foods",
        "foundation-foods": "foundation_foods",
        "sr legacy": "sr_legacy",
        "sr_legacy": "sr_legacy",
        "srlegacy": "sr_legacy",
    }
    MAX_MEALS = 7
    MAX_RECIPE_CANDIDATES = 60
    MAX_INGREDIENT_CANDIDATES = 100
    MAX_INGREDIENT_QUERIES = 5
    MAX_COMPOSITION_INGREDIENTS = 8
    MAX_GENERATED_AMOUNT_G = 2000.0
    WEIGHT_PATTERN = re.compile(
        r"(?P<amount>\d+(?:\.\d+)?)\s*(?P<unit>milligrams?|mg|kilograms?|kg|grams?|g|ounces?|oz|pounds?|lbs?)\b\s*(?:of\s+)?",
        re.I,
    )

    def __init__(self, kg, retriever, deterministic_generator):
        self.kg = kg
        self.retriever = retriever
        self.deterministic_generator = deterministic_generator
        self._slm_generator = None
        self._slm_error = None
        self._slm_init_lock = Lock()

    @classmethod
    def _normalize_dataset_scope(cls, value):
        normalized = " ".join(str(value or "all").casefold().replace("-", " ").split())
        normalized = normalized.replace(" ", "_") if normalized == "sr legacy" else normalized
        if normalized == "foundation foods":
            normalized = "foundation_foods"
        elif normalized == "foundation":
            normalized = "foundation_foods"
        return cls.DATASET_SCOPE_ALIASES.get(normalized, "all")

    def _generator(self, use_slm):
        if not use_slm:
            return self.deterministic_generator, None
        generator, error = self.get_slm_generator()
        if generator is not None:
            return generator, None
        return self.deterministic_generator, error

    def get_slm_generator(self):
        """Return the shared local Qwen instance without a deterministic fallback.

        Optional evaluators can reuse the same lazily loaded model while still
        distinguishing model unavailability from a successful deterministic
        fallback used for ordinary request planning.
        """
        if self._slm_generator is None and self._slm_error is None:
            with self._slm_init_lock:
                if self._slm_generator is None and self._slm_error is None:
                    try:
                        from src.generation.qwen_generator import QwenRecipeGenerator
                        self._slm_generator = QwenRecipeGenerator()
                    except Exception as exc:  # Optional model/runtime; keep the local demo usable.
                        self._slm_error = str(exc)
        if self._slm_generator is not None:
            return self._slm_generator, None
        return None, f"Local SLM unavailable ({self._slm_error})."

    @staticmethod
    def _count_from_text(text):
        words = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
                 "six": 6, "seven": 7, "한": 1, "두": 2, "세": 3, "네": 4, "다섯": 5,
                 "여섯": 6, "일곱": 7}
        match = re.search(r"\b(\d+|one|two|three|four|five|six|seven)\s+(?:meals?|dishes|recipes)\b",
                          text.casefold())
        if match:
            raw = match.group(1)
            return int(raw) if raw.isdigit() else words[raw]
        match = re.search(r"(\d+|한|두|세|네|다섯|여섯|일곱)\s*(?:끼니|끼|식사)", text.casefold())
        if match:
            raw = match.group(1)
            return int(raw) if raw.isdigit() else words[raw]
        return None

    @classmethod
    def _daily_meal_plan(cls, query):
        text = str(query or "").casefold()
        asks_day = bool(re.search(
            r"\b(?:daily\s+meal\s+plan|plan\s+my\s+meals|meals?\s+for\s+tomorrow|"
            r"breakfast\b.*\blunch\b.*\bdinner|breakfast\b.*\bdinner)\b", text))
        if not asks_day or not re.search(r"\b(?:plan|suggest|recommend|create)\b", text):
            return None
        return {"intent": "daily_meal_plan", "requested_count": 3,
                "recipe_requests": [], "ingredient_queries": [], "question_type": "recipe"}

    @classmethod
    def _food_recommendation_plan(cls, query):
        """Route dish suggestions and eatability questions away from meal planning."""
        text = str(query).strip()
        lowered = text.casefold()
        asks_optimisation = bool(re.search(
            r"\b(?:optimise|optimize)\b.*\b(?:ingredients?|quantit(?:y|ies)|recipe|dish|meal)\b",
            lowered,
        ))
        if not (re.search(r"\b(?:suggest|recommend)\b", lowered)
                or re.search(r"\bwhat\s+can\s+i\s+eat\b", lowered)
                or re.search(r"\bcan\s+i\s+eat\b", lowered)
                or asks_optimisation):
            return None
        if re.fullmatch(r"(?:please\s+)?(?:suggest|recommend)\s+(?:a|one)\s+(?:dish|meal|recipe)[.!?]?", lowered):
            return None
        if re.search(r"\b(?:meal\s+plan|plan\s+my\s+meals|daily\s+plan|for\s+(?:the\s+)?(?:day|week))\b", lowered):
            return None
        if cls.WEIGHT_PATTERN.search(text):
            return None
        count = cls._count_from_text(text)
        return {
            "intent": "food_recommendation", "requested_count": max(1, min(count or 5, cls.MAX_MEALS)),
            "recommendation_query": text[:240], "recipe_requests": [],
            "ingredient_queries": [], "question_type": "recipe",
            "optimise_quantities": asks_optimisation,
        }

    def _ingredient_lookup_plan(self, query):
        """Route explicit nutrient questions about a known food to food lookup.

        The ingredient catalog includes the configured FoodData Central releases,
        while the recipe catalog is intentionally much smaller. This guard prevents
        a food-only nutrient question from being treated as a missing-recipe
        request. The food itself is resolved through graph retrieval; no
        ingredient-specific rules are used here.
        """
        text = query.casefold()
        asks_nutrient = any(term in text for term in (
            "potassium", "sodium", "phosphorus", "protein", "carbohydrate",
            "nutrition", "nutrient",
        ))
        if not asks_nutrient:
            return None

        ingredient_match = self.retriever.retrieve_ingredients(query, k=1)
        if not ingredient_match or ingredient_match[0][1] < 0.8:
            return None
        recipe_match = self.retriever.retrieve_nodes(query, k=1)
        if recipe_match and ingredient_match[0][1] <= recipe_match[0][1]:
            return None
        return {"intent": "ingredient_question", "requested_count": 1,
                "recipe_requests": [], "ingredient_queries": [query],
                "question_type": "nutrition"}

    @classmethod
    def _ingredient_catalog_plan(cls, query):
        """Route explicit dataset-existence questions to ingredient lookup.

        This is a semantic guard for requests about whether named foods are in
        the ingredient catalog. It runs before recipe retrieval so a food name
        that is also a recipe title cannot hijack the question.
        """
        text = str(query).strip()
        lowered = text.casefold()
        mentions_catalog = re.search(
            r"\b(?:ingredients?|foods?)\s+(?:data(?:set|base)|catalog(?:ue)?|records?)\b"
            r"|\b(?:data(?:set|base)|catalog(?:ue))\b", lowered,
        )
        asks_existence = re.search(
            r"\b(?:which\s+(?:ones|ingredients?|foods?|of)|whether|exist(?:s)?|"
            r"available|present|included|found|are\s+in|is\s+in|"
            r"in\s+the\s+(?:ingredient|food))\b", lowered,
        )
        if not mentions_catalog or not asks_existence:
            return None

        # Most users provide a list first, then ask what is present. If the list
        # is inside a direct "which of ..." question, capture that list instead.
        question_clause = re.search(r"\bwhich\s+(?:ones|ingredients?|foods?)\b", lowered)
        if question_clause and question_clause.start() > 0:
            list_text = text[:question_clause.start()]
        else:
            direct_list = re.search(
                r"\bwhich\s+of\s+(?:(?:the|these)\s+)?(?:ingredients?\s+|foods?\s+)?(.+?)\s+"
                r"(?:exist|are\s+available|are\s+present|can\s+be\s+found|are\s+in)\b",
                text, re.I,
            )
            if direct_list:
                list_text = direct_list.group(1)
            else:
                # Also accept a comma-separated first clause followed by a
                # dataset question with other wording (e.g. "check these").
                list_text = re.split(
                    r"\b(?:in\s+the\s+)?(?:ingredients?\s+)?(?:data(?:set|base)|catalog(?:ue)?)\b",
                    text, maxsplit=1, flags=re.I,
                )[0]
                list_text = re.split(r"[.!?;]\s*", list_text, maxsplit=1)[0]

        list_text = re.sub(r"^\s*(?:and\s+)?", "", list_text, flags=re.I)
        list_text = re.sub(
            r"^\s*(?:(?:these|the following)\s+)?(?:ingredients?|foods?)\s*(?::|are\s+)?\s*",
            "", list_text, flags=re.I,
        )
        list_text = re.sub(
            r"^\s*(?:(?:do|does|are|is|can|could)\s+)?"
            r"(?:(?:please\s+)?(?:check|tell me|show me)\s+)?",
            "", list_text, flags=re.I,
        )
        list_text = re.sub(r"^\s*(?:I have|I listed|these are|they are)\s+", "", list_text, flags=re.I)
        list_text = re.sub(r"\s+(?:exist|are available|are present|in the dataset)\s*$", "", list_text, flags=re.I)
        queries = [
            item.strip(" \t\r\n,;:.!?()[]{}\"'")
            for item in re.split(r"\s*(?:,|;|\band\b|&)\s*", list_text, flags=re.I)
        ]
        queries = [item[:120] for item in queries if item and item.casefold() not in {"the", "these"}]
        if not queries:
            return {
                "intent": "catalog_question", "requested_count": 1,
                "recipe_requests": [], "ingredient_queries": [],
                "question_type": "catalog_existence",
                "clarification_question": (
                    "Which ingredient names should I check in USDA FoodData Central?"
                ),
            }
        return {
            "intent": "catalog_question", "requested_count": 1,
            "recipe_requests": [], "ingredient_queries": queries[:cls.MAX_INGREDIENT_QUERIES],
            "question_type": "catalog_existence",
        }

    @staticmethod
    def _recipe_label_matches_text(labels, text):
        """Check that a selected recipe name is actually grounded in the chat."""
        normalized_text = " ".join(re.findall(r"[a-z0-9]+", str(text).casefold()))
        text_tokens = set(normalized_text.split()) - STOP_WORDS
        for raw_label in labels:
            label = str(raw_label or "").strip()
            normalized_label = " ".join(re.findall(r"[a-z0-9]+", label.casefold()))
            label_tokens = set(normalized_label.split()) - STOP_WORDS
            if not label_tokens:
                continue
            if normalized_label and re.search(
                r"(?<![a-z0-9])" + re.escape(normalized_label) + r"(?![a-z0-9])",
                normalized_text,
            ):
                return True
            overlap = text_tokens & label_tokens
            if len(label_tokens) == 1:
                # A single generic food word is not enough to select a recipe
                # unless the user explicitly asked for a recipe or dish.
                asks_for_recipe = re.search(
                    r"\b(?:recipe|dish|cook|cooking|prepare|make)\b",
                    normalized_text,
                )
                if overlap and asks_for_recipe:
                    return True
            # Partial title overlap is too weak for recipe selection: a follow-up
            # listing "lemon juice and pepper" must not select a "lemon pepper
            # shrimp" recipe. Require every meaningful title token unless the
            # complete label or alias appeared above.
            elif label_tokens.issubset(text_tokens):
                return True
        return False

    def _single_recipe_is_grounded(self, plan, query, context):
        """Reject a model-selected recipe that has no name-level support in chat."""
        history = context.get("conversation_history", []) if isinstance(context, dict) else []
        history_parts = [str(query)]
        if isinstance(history, list):
            for turn in history:
                if not isinstance(turn, dict):
                    continue
                if turn.get("role") == "user":
                    history_parts.append(str(turn.get("content") or ""))
                elif turn.get("role") == "assistant" and isinstance(turn.get("context"), dict):
                    # Include structured catalog facts, not free-form assistant
                    # text, so a previous bad answer cannot anchor another one.
                    history_parts.extend(str(value) for key, value in turn["context"].items()
                                         if key in {"recipe", "recipe_ingredient_names", "composition_ingredients"})
                    for lookup in turn["context"].get("ingredient_lookups", []):
                        if isinstance(lookup, dict):
                            history_parts.append(str(lookup.get("name") or ""))
                            history_parts.extend(str(value) for value in lookup.get("records", []))
        conversation_text = " ".join(history_parts)

        requests = plan.get("recipe_requests") or []
        request = requests[0] if requests and isinstance(requests[0], dict) else {}
        recipe_id = request.get("recipe_id")
        recipe = self.kg.recipes.get(recipe_id) if recipe_id in self.kg.recipes else None
        if recipe is None:
            search_query = request.get("query") or query
            match = self.retriever.retrieve_recipe(search_query)
            recipe = self.kg.recipes.get(match[0]) if match else None
        if recipe is None:
            extracted_phrase = request.get("query") or plan.get("recipe_query")
            if (extracted_phrase
                    and " ".join(str(extracted_phrase).casefold().split())
                    != " ".join(str(query).casefold().split())):
                return self._recipe_label_matches_text([extracted_phrase], conversation_text)
            return False
        labels = [recipe.get("name", ""), *(recipe.get("aliases") or [])]
        return self._recipe_label_matches_text(labels, conversation_text)

    @classmethod
    def _composition_ingredients(cls, query):
        """Extract explicit weights and the food phrase that follows each weight."""
        text = str(query)
        matches = list(cls.WEIGHT_PATTERN.finditer(text))
        if not matches:
            return []
        multipliers = {
            "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
            "g": 1.0, "gram": 1.0, "grams": 1.0,
            "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
            "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
            "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
        }
        items = []
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            phrase = text[match.end():end]
            phrase = re.sub(r"^[\s,;]*(?:(?:and|plus|with)\s+)+", "", phrase, flags=re.I)
            phrase = phrase.strip(" \t\r\n,;:.!?()[]{}\"'")
            phrase = re.sub(
                r"\s+(?:(?:and|plus|as\s+well\s+as|together|combined|in\s+total|"
                r"in\s+the\s+same\s+(?:dish|meal|bowl)|for\s+this\s+(?:dish|meal|bowl))\s*)+$",
                "", phrase, flags=re.I,
            )
            phrase = re.sub(r"\s+(?:in|inside)\s+(?:the\s+)?(?:recipe|dish|meal|bowl)\b.*$", "", phrase, flags=re.I)
            phrase = phrase.strip(" \t\r\n,;:.!?()[]{}\"'")
            phrase = re.sub(r"^(?:the|a|an|some)\s+", "", phrase, flags=re.I)
            if not phrase:
                continue
            unit = match.group("unit").casefold()
            amount_g = float(match.group("amount")) * multipliers[unit]
            if amount_g <= 0:
                continue
            items.append({"query": phrase[:120], "amount_g": amount_g})
        return items

    def _recipe_catalog_candidates(self, query, k=None, allow_broad_fallback=False):
        """Build a bounded SLM catalog from graph-retrieved recipe candidates."""
        limit = max(1, int(k or self.MAX_RECIPE_CANDIDATES))
        ranked = self.retriever.search_recipes(query, k=limit)
        if not ranked and allow_broad_fallback:
            ranked = self.retriever.retrieve_nodes(query, k=limit)
        candidates = []
        seen = set()
        for recipe_id, score in ranked:
            recipe = self.kg.recipes.get(recipe_id)
            if recipe is None or recipe_id in seen:
                continue
            seen.add(recipe_id)
            candidates.append({
                "id": recipe_id,
                "name": recipe.get("name", recipe_id),
                "aliases": recipe.get("aliases", []),
                "tags": recipe.get("foodcom_tags", recipe.get("tags", [])),
                "source_dataset": recipe.get("source_dataset"),
                "nutrition_complete": recipe.get("nutrition_complete", True),
                "retrieval_score": score,
            })
        return candidates

    def _ingredient_catalog_candidates(self, query, requested_names=None, k=None,
                                       dataset_scope="all"):
        """Build an ingredient candidate set only after the SLM requests that domain."""
        limit = max(1, int(k or self.MAX_INGREDIENT_CANDIDATES))
        names = [str(name).strip() for name in (requested_names or []) if str(name).strip()]
        search_text = " ".join(names) if names else str(query)
        ranked = self.retriever.retrieve_ingredients(
            search_text, k=limit, dataset_scope=dataset_scope,
        )
        ingredient_ids = [ingredient_id for ingredient_id, _ in ranked]
        # A new-recipe request may be open-ended. Give the model a bounded set of
        # graph-backed choices only in that case, after it selected this intent.
        if not ingredient_ids and not names:
            ingredient_ids = [
                ingredient_id for ingredient_id, data in self.kg.ingredients.items()
                if dataset_scope == "all"
                or data.get("source", {}).get("dataset_key") == dataset_scope
            ][:limit]
        return [{"id": iid, "name": self.kg.ingredients[iid].get("name", iid),
                 "aliases": self.kg.ingredients[iid].get("aliases", []),
                 "source_dataset": self.kg.ingredients[iid].get("source", {}).get("name"),
                 "dataset_key": self.kg.ingredients[iid].get("source", {}).get("dataset_key"),
                 "food_state": self.kg.ingredients[iid].get("food_state"),
                 "min_amount_g": self.kg.ingredients[iid].get("min_amount_g"),
                 "max_amount_g": self.kg.ingredients[iid].get("max_amount_g"),
                 "typical_amount_g": self.kg.ingredients[iid].get("typical_amount_g")}
                for iid in ingredient_ids if iid in self.kg.ingredients]

    def _composition_plan(self, query, recipe_hint=None, infer_recipe=True):
        weight_count = len(self.WEIGHT_PATTERN.findall(str(query)))
        items = self._composition_ingredients(query)
        if len(items) != weight_count:
            catalog_items = self._catalog_weight_items(query)
            if catalog_items is not None:
                items = catalog_items
        parse_error = bool(weight_count and len(items) != weight_count)
        if parse_error:
            items = []
        if not items and not parse_error:
            return None
        recipe_query = None
        lowered = str(query).casefold()
        recipe_mentions = []
        if recipe_hint:
            recipe_match = self.retriever.retrieve_recipe(recipe_hint)
            if recipe_match:
                recipe_query = self.kg.recipes.get(recipe_match[0], {}).get("name", recipe_match[0])
        elif infer_recipe:
            for candidate in self._recipe_catalog_candidates(query):
                recipe_id = candidate["id"]
                recipe = self.kg.recipes[recipe_id]
                for label in [recipe.get("name", recipe_id), *(recipe.get("aliases") or [])]:
                    label = str(label).strip()
                    if label and re.search(r"(?<!\w)" + re.escape(label.casefold()) + r"(?!\w)", lowered):
                        recipe_mentions.append((len(label), label))
        if not recipe_query and recipe_mentions:
            recipe_query = max(recipe_mentions, key=lambda item: item[0])[1]
        elif not recipe_query and weight_count and infer_recipe:
            weights = list(self.WEIGHT_PATTERN.finditer(str(query)))
            dish_phrase = str(query)[:weights[0].start()]
            dish_phrase = re.sub(
                r"^\s*(?:(?:can|could|would)\s+you\s+)?(?:make|cook|prepare|create|suggest|recommend)\s+",
                "", dish_phrase, flags=re.I,
            )
            dish_phrase = re.sub(r"\b(?:with|using|including|that\s+includes)\s*$", "", dish_phrase, flags=re.I)
            dish_phrase = re.sub(r"^\s*(?:a|an|the)\s+", "", dish_phrase, flags=re.I)
            dish_phrase = dish_phrase.strip(" \t\r\n,;:.!?()[]{}\"'")
            match = self.retriever.retrieve_recipe(dish_phrase) if dish_phrase else None
            if match:
                recipe_query = self.kg.recipes.get(match[0], {}).get("name", match[0])
        return {
            "intent": "composition_question", "requested_count": 1,
            "recipe_requests": [], "ingredient_queries": [],
            "composition_ingredients": items[:self.MAX_COMPOSITION_INGREDIENTS],
            "composition_overflow": len(items) > self.MAX_COMPOSITION_INGREDIENTS,
            "composition_parse_error": parse_error,
            "recipe_query": recipe_query,
            "question_type": "nutrition",
        }

    def _catalog_weight_items(self, query):
        """Handle food-name-before-weight phrasing using labels in the local catalog."""
        text = str(query)
        weights = list(self.WEIGHT_PATTERN.finditer(text))
        ascii_text = text.isascii()
        folded_text = text.casefold() if ascii_text else None
        mentions = {}
        for ingredient_id, data in self.kg.ingredients.items():
            labels = [data.get("name", ingredient_id), *(data.get("aliases") or [])]
            for raw_label in labels:
                label = str(raw_label).strip()
                if len(label) < 3:
                    continue
                if ascii_text and label.isascii() and label.casefold() not in folded_text:
                    continue
                for match in re.finditer(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text, flags=re.I):
                    key = (match.start(), match.end())
                    if key not in mentions or len(label) > len(mentions[key][0]):
                        mentions[key] = (label, match.start(), match.end())
        if not mentions:
            return None

        food_weights = []
        first_weight_start = weights[0].start() if weights else len(text)
        for label, start, end in mentions.values():
            nearby = []
            for index, weight in enumerate(weights):
                if weight.end() <= start:
                    gap = start - weight.end()
                elif end <= weight.start():
                    gap = weight.start() - end
                else:
                    gap = 0
                if gap <= 24:
                    nearby.append((gap, index))
            if nearby:
                gap, weight_index = min(nearby)
                food_weights.append((start, end, label, weight_index))
            elif start >= first_weight_start:
                return []

        # A quantity must map to exactly one catalog food mention, and each
        # catalog food mention in the weighed composition must have a quantity.
        by_weight = {}
        for start, end, label, weight_index in food_weights:
            by_weight.setdefault(weight_index, []).append((start, end, label))
        if len(by_weight) != len(weights) or any(len(items) != 1 for items in by_weight.values()):
            return []

        multipliers = {
            "mg": 0.001, "milligram": 0.001, "milligrams": 0.001,
            "g": 1.0, "gram": 1.0, "grams": 1.0,
            "kg": 1000.0, "kilogram": 1000.0, "kilograms": 1000.0,
            "oz": 28.349523125, "ounce": 28.349523125, "ounces": 28.349523125,
            "lb": 453.59237, "lbs": 453.59237, "pound": 453.59237, "pounds": 453.59237,
        }
        items = []
        for weight_index, weight in enumerate(weights):
            _, _, label = by_weight[weight_index][0]
            amount_g = float(weight.group("amount")) * multipliers[weight.group("unit").casefold()]
            if amount_g <= 0:
                return []
            items.append({"query": label, "amount_g": amount_g})
        return items

    @staticmethod
    def _clean_ingredient_phrase(value):
        value = re.sub(r"\s+", " ", str(value)).strip(" \t\r\n.,!?;:'\"()[]{}")
        value = re.sub(r"^(?:the|a|an|some)\s+", "", value, flags=re.I)
        value = re.sub(
            r"\s+(?:in|from|inside)\s+(?:the\s+)?(?:recipe|dish|meal|bowl|soup)\b.*$",
            "", value, flags=re.I,
        )
        return value.strip(" \t\r\n.,!?;:'\"()[]{}")

    @classmethod
    def _substitution_phrases(cls, query):
        """Extract the two sides of common substitution questions generically."""
        text = str(query).strip()
        patterns = (
            r"\breplace\s+(?P<original>.+?)\s+(?:with|by)\s+(?P<substitute>.+?)(?:[?.!]|$)",
            r"\bsubstitute\s+(?P<original>.+?)\s+with\s+(?P<substitute>.+?)(?:[?.!]|$)",
            r"\bsubstitute\s+(?P<substitute>.+?)\s+for\s+(?P<original>.+?)(?:[?.!]|$)",
            r"(?P<substitute>.+?)\s+(?:is\s+)?(?:a\s+)?(?:substitute|replacement)\s+for\s+(?P<original>.+?)(?:[?.!]|$)",
            r"\bswap\s+(?P<original>.+?)\s+for\s+(?P<substitute>.+?)(?:[?.!]|$)",
            r"\buse\s+(?P<substitute>.+?)\s+instead\s+of\s+(?P<original>.+?)(?:[?.!]|$)",
            r"\b(?:use|swap)\s+(?P<substitute>.+?)\s+(?:to\s+replace|in\s+place\s+of)\s+(?P<original>.+?)(?:[?.!]|$)",
            r"(?P<substitute>.+?)\s+instead\s+of\s+(?P<original>.+?)(?:[?.!]|$)",
            r"\b(?P<original>.+?)\s+(?:be\s+)?(?:replaced|substituted)\s+(?:with|by)\s+(?P<substitute>.+?)(?:[?.!]|$)",
            r"\bcan\s+(?P<substitute>.+?)\s+replace\s+(?P<original>.+?)(?:[?.!]|$)",
        )
        for pattern in patterns:
            match = re.search(pattern, text, flags=re.I)
            if not match:
                continue
            original = cls._clean_ingredient_phrase(match.group("original"))
            substitute = cls._clean_ingredient_phrase(match.group("substitute"))
            if original and substitute:
                return original, substitute
        return None

    def _catalog_food_phrase(self, phrase):
        """Trim surrounding prose to the earliest catalog label in a phrase."""
        text = str(phrase).casefold()
        # A trailing named dish is context, not part of the ingredient. Strip
        # it before trying to match the food so a known food in that dish
        # cannot accidentally stand in for an unknown proposed replacement.
        recipe_mentions = []
        for candidate in self._recipe_catalog_candidates(phrase):
            recipe_id = candidate["id"]
            recipe = self.kg.recipes[recipe_id]
            for label in [recipe.get("name", recipe_id), *(recipe.get("aliases") or [])]:
                label = str(label).strip().casefold()
                if not label:
                    continue
                found = re.search(r"\bin\s+(?:the\s+)?" + re.escape(label), text)
                if found:
                    recipe_mentions.append((found.start(), len(label)))
        if recipe_mentions:
            text = text[:min(recipe_mentions)[0]].strip()
        ascii_text = text.isascii()
        matches = []
        for ingredient_id, ingredient in self.kg.ingredients.items():
            labels = [ingredient.get("name", ingredient_id), *(ingredient.get("aliases") or [])]
            for label in labels:
                label = str(label).strip().casefold()
                if not label:
                    continue
                if ascii_text and label.isascii() and label not in text:
                    continue
                found = re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", text)
                if found:
                    matches.append((found.start(), -len(label), label))
        return min(matches)[2] if matches else phrase

    def _substitution_plan(self, query):
        phrases = self._substitution_phrases(query)
        if not phrases:
            return None
        original_query, substitute_query = (
            self._catalog_food_phrase(phrase) for phrase in phrases
        )
        lowered = str(query).casefold()
        recipe_query = None
        # Keep a named recipe from the graph when the user mentions one. Names
        # and aliases come from the catalog, so this does not enumerate dishes.
        recipe_mentions = []
        for candidate in self._recipe_catalog_candidates(query):
            recipe_id = candidate["id"]
            recipe = self.kg.recipes[recipe_id]
            labels = [recipe.get("name", recipe_id), *(recipe.get("aliases") or [])]
            for label in labels:
                label = str(label).strip()
                if label and re.search(r"(?<!\w)" + re.escape(label.casefold()) + r"(?!\w)", lowered):
                    recipe_mentions.append((len(label), label))
        if recipe_mentions:
            recipe_query = max(recipe_mentions, key=lambda item: item[0])[1]
        return {
            "intent": "substitution_question", "requested_count": 1,
            "recipe_requests": [], "ingredient_queries": [],
            "original_ingredient_query": original_query,
            "substitute_ingredient_query": substitute_query,
            "recipe_query": recipe_query,
            "question_type": "substitution",
        }

    def fallback_plan(self, query):
        text = query.casefold()
        daily = self._daily_meal_plan(query)
        if daily:
            return daily
        catalog_plan = self._ingredient_catalog_plan(query)
        if catalog_plan:
            return catalog_plan
        substitution_plan = self._substitution_plan(query)
        if substitution_plan:
            return substitution_plan
        composition_plan = self._composition_plan(query)
        if composition_plan:
            return composition_plan
        ingredient_plan = self._ingredient_lookup_plan(query)
        if ingredient_plan:
            return ingredient_plan
        recommendation_plan = self._food_recommendation_plan(query)
        if recommendation_plan:
            return recommendation_plan
        count = self._count_from_text(text)
        asks_for_many = count is not None or any(term in text for term in (
            "meal plan", "plan my meals", "what to eat today", "breakfast", "lunch", "dinner",
            "식단", "끼니", "아침", "점심", "저녁",
        )) and any(term in text for term in (
            "suggest", "recommend", "plan", "what to eat", "today", "daily",
            "추천", "계획", "먹을", "오늘",
        ))
        if not asks_for_many:
            match = self.retriever.retrieve_recipe(query)
            if match:
                recipe_id = match[0]
                recipe = self.kg.recipes.get(recipe_id, {})
                labels = [recipe.get("name", recipe_id), *(recipe.get("aliases") or [])]
                if self._recipe_label_matches_text(labels, query):
                    return {"intent": "single_recipe", "requested_count": 1,
                            "recipe_requests": [{"slot": "", "recipe_id": recipe_id,
                                                 "query": recipe.get("name", recipe_id)}],
                            "question_type": "recipe"}
            return {
                "intent": "unsupported", "requested_count": 1,
                "recipe_requests": [], "ingredient_queries": [],
                "question_type": "other",
                "clarification_question": (
                    "I couldn't identify a specific recipe or ingredient request. Please name the dish, "
                    "list the foods you want checked, or describe the meal you want."
                ),
            }

        count = max(1, min(count or 3, self.MAX_MEALS))
        ranked = self.retriever.retrieve_nodes(query, k=min(80, max(24, count * 8)))
        # If the user named an exact catalog dish, keep it first. Otherwise the
        # fallback shows a varied catalog sample; the SLM can choose more relevant
        # items when enabled.
        ordered_ids = [rid for rid, score in ranked if score >= 1.0]
        complete_ids = [rid for rid, _ in ranked
                        if self.kg.recipes.get(rid, {}).get("nutrition_complete", True)]
        incomplete_ids = [rid for rid, _ in ranked if rid not in complete_ids]
        ordered_ids.extend(rid for rid in complete_ids if rid not in ordered_ids)
        ordered_ids.extend(rid for rid in incomplete_ids if rid not in ordered_ids)
        requests = [{"slot": f"Meal {index + 1}", "recipe_id": rid,
                     "query": self.kg.recipes[rid].get("name", rid)}
                    for index, rid in enumerate(ordered_ids[:count])]
        return {"intent": "meal_plan", "requested_count": count,
                "recipe_requests": requests, "question_type": "recipe"}

    def _normalize_request_lists(self, plan, query):
        """Validate and bound the request lists returned by either SLM pass."""
        recipe_requests = plan.get("recipe_requests", [])
        ingredient_queries = plan.get("ingredient_queries", [])
        if not isinstance(recipe_requests, list):
            raise ValueError("Invalid recipe request list")
        if not isinstance(ingredient_queries, list):
            raise ValueError("Invalid ingredient query list")

        plan["recipe_requests"] = recipe_requests[:self.MAX_MEALS]
        unique_queries = []
        seen_queries = set()
        for item in ingredient_queries:
            name = str(item).strip()[:120]
            normalized = " ".join(name.casefold().split())
            if normalized and normalized not in seen_queries:
                unique_queries.append(name)
                seen_queries.add(normalized)
            if len(unique_queries) >= self.MAX_INGREDIENT_QUERIES:
                break
        plan["ingredient_queries"] = unique_queries

        for request in plan["recipe_requests"]:
            if not isinstance(request, dict):
                raise ValueError("Invalid recipe request")
            recipe_id = request.get("recipe_id")
            request["recipe_id"] = recipe_id if recipe_id in self.kg.recipes else None
            request["query"] = str(request.get("query") or recipe_id or query)[:240]
            request["slot"] = str(request.get("slot") or "")[:40]

    def _validate_generated_composition(self, plan, ingredient_catalog):
        """Keep only bounded, catalog-backed food amounts from the SLM plan."""
        generated = plan.get("generated_composition", [])
        invalid = not isinstance(generated, list)
        if invalid:
            generated = []
        overflow = len(generated) > self.MAX_COMPOSITION_INGREDIENTS
        allowed_ids = {item["id"] for item in ingredient_catalog}
        validated_amounts = {}

        for item in generated[:self.MAX_COMPOSITION_INGREDIENTS]:
            if not isinstance(item, dict):
                invalid = True
                continue
            ingredient_id = item.get("ingredient_id")
            amount_g = item.get("amount_g")
            try:
                amount = float(amount_g) if not isinstance(amount_g, bool) else math.nan
            except (TypeError, ValueError, OverflowError):
                amount = math.nan
            if (not isinstance(ingredient_id, str)
                    or ingredient_id not in self.kg.ingredients
                    or (plan["intent"] == "new_recipe" and ingredient_id not in allowed_ids)
                    or not math.isfinite(amount)
                    or not 0 < amount <= self.MAX_GENERATED_AMOUNT_G):
                invalid = True
                continue
            ingredient = self.kg.ingredients[ingredient_id]
            minimum = ingredient.get("min_amount_g")
            maximum = ingredient.get("max_amount_g")
            if ((minimum is not None and amount < minimum)
                    or (maximum is not None and amount > maximum)):
                invalid = True
                continue
            validated_amounts[ingredient_id] = validated_amounts.get(ingredient_id, 0.0) + amount

        plan["generated_composition"] = [
            {"ingredient_id": ingredient_id, "amount_g": amount}
            for ingredient_id, amount in validated_amounts.items()
        ]
        plan["generated_composition_overflow"] = overflow
        plan["generated_composition_invalid"] = invalid

    def plan(self, query, use_slm, context):
        daily = self._daily_meal_plan(query)
        if daily:
            return daily, self.deterministic_generator, None, False
        recommendation_plan = self._food_recommendation_plan(query)
        if recommendation_plan:
            return recommendation_plan, self.deterministic_generator, None, False
        generator, warning = self._generator(use_slm)
        if use_slm and generator is not self.deterministic_generator:
            try:
                classification = generator.classify_request(query, context)
                if not isinstance(classification, dict):
                    raise ValueError("Invalid SLM classification")
                plan = dict(classification)
                self._normalize_request_lists(plan, query)

                plan["intent"] = str(plan.get("intent", "unsupported"))
                plan["dataset_scope"] = self._normalize_dataset_scope(plan.get("dataset_scope"))
                allowed_intents = {"single_recipe", "new_recipe", "meal_plan", "ingredient_question",
                                   "composition_question", "substitution_question", "catalog_question",
                                   "unsupported"}
                if plan["intent"] not in allowed_intents:
                    plan["intent"] = "unsupported"
                if (plan["intent"] == "catalog_question"
                        or (plan["intent"] == "ingredient_question"
                            and str(plan.get("question_type", "")).casefold()
                            in {"catalog_existence", "catalog_membership"})):
                    plan["intent"] = "catalog_question"
                    plan["question_type"] = "catalog_existence"
                    plan["requested_count"] = 1
                    if not plan["ingredient_queries"]:
                        plan["clarification_question"] = str(plan.get("clarification_question") or (
                            "Which ingredient names should I check in USDA FoodData Central?"
                        ))[:500]
                    return plan, generator, None, True

                if plan["intent"] == "ingredient_question":
                    if not plan["ingredient_queries"]:
                        plan["intent"] = "unsupported"
                        plan["clarification_question"] = str(plan.get("clarification_question") or (
                            "Which ingredient's nutrient information would you like me to look up?"
                        ))[:500]
                        return plan, generator, None, True
                    return plan, generator, None, True

                if plan["intent"] == "unsupported":
                    plan["clarification_question"] = str(plan.get("clarification_question") or (
                        "I couldn't identify what you want me to check. Please name a dish or list the ingredients."
                    ))[:500]
                    return plan, generator, None, True

                if plan["intent"] == "substitution_question":
                    plan["original_ingredient_query"] = str(
                        plan.get("original_ingredient_query") or ""
                    )[:120]
                    plan["substitute_ingredient_query"] = str(
                        plan.get("substitute_ingredient_query") or ""
                    )[:120]
                    if not plan["original_ingredient_query"] or not plan["substitute_ingredient_query"]:
                        plan["intent"] = "unsupported"
                        plan["clarification_question"] = str(plan.get("clarification_question") or (
                            "Tell me which ingredient to replace and what ingredient you want to use instead."
                        ))[:500]
                    return plan, generator, None, True

                if plan["intent"] == "composition_question":
                    composition_plan = self._composition_plan(
                        query, recipe_hint=plan.get("recipe_query"), infer_recipe=False,
                    )
                    if composition_plan:
                        plan.update(composition_plan)
                    else:
                        plan["composition_ingredients"] = []
                        plan["composition_parse_error"] = True
                    return plan, generator, None, True

                # Search only the catalog implied by the first-pass SLM intent.
                recipe_catalog = []
                ingredient_catalog = []
                if plan["intent"] in {"single_recipe", "meal_plan"}:
                    recipe_query = query if plan["intent"] == "meal_plan" else str(
                        plan.get("recipe_query") or ""
                    ).strip()
                    if not recipe_query and plan["recipe_requests"]:
                        recipe_query = plan["recipe_requests"][0]["query"]
                    if not recipe_query:
                        recipe_query = query
                    recipe_catalog = self._recipe_catalog_candidates(
                        recipe_query,
                        allow_broad_fallback=plan["intent"] == "meal_plan",
                    )
                    if plan["intent"] == "single_recipe" and not recipe_catalog:
                        plan["intent"] = "unsupported"
                        plan["clarification_question"] = (
                            "I couldn't find that dish in the recipe catalog. Please check the dish name or list the ingredients."
                        )
                        return plan, generator, None, True
                elif plan["intent"] == "new_recipe":
                    ingredient_catalog = self._ingredient_catalog_candidates(
                        query, plan["ingredient_queries"],
                        dataset_scope=plan["dataset_scope"],
                    )
                else:
                    raise ValueError("The SLM selected an unsupported request intent")

                plan = generator.interpret_request(
                    query, recipe_catalog, ingredient_catalog, context,
                    classified_request=classification,
                )
                if not isinstance(plan, dict):
                    raise ValueError("Invalid SLM plan")
                plan["intent"] = classification["intent"]
                plan["dataset_scope"] = self._normalize_dataset_scope(
                    classification.get("dataset_scope")
                )
                self._normalize_request_lists(plan, query)
                if not plan["recipe_requests"] and classification.get("recipe_requests"):
                    for request in classification["recipe_requests"][:self.MAX_MEALS]:
                        if not isinstance(request, dict):
                            continue
                        rid = request.get("recipe_id")
                        plan["recipe_requests"].append({
                            "slot": str(request.get("slot") or "")[:40],
                            "recipe_id": rid if rid in self.kg.recipes else None,
                            "query": str(request.get("query") or rid or query)[:240],
                        })
                if not plan["ingredient_queries"]:
                    plan["ingredient_queries"] = classification["ingredient_queries"]

                if plan["intent"] == "single_recipe" and not plan["recipe_requests"]:
                    extracted_recipe = str(plan.get("recipe_query") or query).strip()[:240]
                    plan["recipe_requests"] = [{
                        "slot": "", "recipe_id": None, "query": extracted_recipe,
                    }]
                if plan["intent"] == "single_recipe" and not self._single_recipe_is_grounded(
                    plan, query, context,
                ):
                    rejected_request = (plan.get("recipe_requests") or [{}])[0]
                    plan["rejected_recipe_target"] = str(
                        rejected_request.get("query") or rejected_request.get("recipe_id") or ""
                    )[:160]
                    plan["intent"] = "unsupported"
                    plan["rejected_intent"] = "single_recipe"
                    plan["clarification_question"] = (
                        "I couldn't identify which recipe you mean from this message and the recent conversation. "
                        "Please name the dish or tell me which ingredients you want checked."
                    )
                    plan["recipe_requests"] = []
                    return plan, generator, None, True
                self._validate_generated_composition(plan, ingredient_catalog)
                plan["recipe_query"] = str(plan.get("recipe_query") or "")[:160] or None
                if plan["intent"] == "new_recipe" and not ingredient_catalog:
                    raise ValueError("No graph-backed ingredient choices were retrieved for the new recipe")
                count = plan.get("requested_count", len(plan["recipe_requests"]) or 1)
                try:
                    plan["requested_count"] = max(1, min(int(count), self.MAX_MEALS))
                except (TypeError, ValueError):
                    plan["requested_count"] = max(1, len(plan["recipe_requests"]))
                if plan["intent"] == "meal_plan" and not plan["recipe_requests"]:
                    raise ValueError("SLM returned an empty meal plan")
                return plan, generator, None, True
            except Exception as exc:
                warning = f"Local SLM could not interpret this request ({exc}); deterministic routing used."
        catalog_plan = self._ingredient_catalog_plan(query)
        return (catalog_plan or self.fallback_plan(query)), self.deterministic_generator, warning, False

"""Bounded Food.com workspace for contextual ingredient resolution."""

from src.recipes.ingredient_parser import parse_foodcom_ingredient


FOODCOM_DATASET = "Food.com Recipes and Reviews (Kaggle)"


class IngredientMappingReviewService:
    """Resolve recipe ingredient contexts with deterministic rules and Qwen."""

    MAX_RECIPES = 10
    MAX_INGREDIENTS_PER_RECIPE = 20
    MAX_UNIQUE_CONTEXTS = 50

    def __init__(self, kg, retriever, mapping_service):
        self.kg = kg
        self.retriever = retriever
        self.mapping_service = mapping_service
        self.mapper = mapping_service.mapper

    def search_recipes(self, query, *, limit=10):
        query = str(query or "").strip()
        if len(query) < 2:
            raise ValueError("Enter at least two characters to search Food.com recipes.")
        if len(query) > 160:
            raise ValueError("Recipe search is limited to 160 characters.")
        limit = max(1, min(int(limit), self.MAX_RECIPES))
        results = []
        for recipe_id, score in self.retriever.search_recipes(query, k=min(limit * 3, 30)):
            recipe = self.kg.recipes.get(recipe_id, {})
            if recipe.get("source_dataset") != FOODCOM_DATASET:
                continue
            details = self.kg.recipe_details(recipe_id) or recipe
            items = details.get("raw_ingredient_items") or details.get("ingredient_items") or []
            results.append({
                "recipe_id": recipe_id,
                "recipe_name": recipe.get("name") or recipe_id,
                "ingredient_count": len(items) or len(details.get("raw_ingredients") or []),
                "servings": details.get("recipe_servings_raw") or details.get("recipe_servings"),
                "retrieval_score": score,
            })
            if len(results) >= limit:
                break
        return {"query": query, "results": results}

    def prepare(self, recipe_ids):
        if not isinstance(recipe_ids, list) or not recipe_ids:
            raise ValueError("Select at least one Food.com recipe.")
        ids = list(dict.fromkeys(str(value).strip() for value in recipe_ids if str(value).strip()))
        if len(ids) > self.MAX_RECIPES:
            raise ValueError(f"Select no more than {self.MAX_RECIPES} recipes per batch.")

        groups = {}
        recipe_summaries = []
        for recipe_id in ids:
            recipe = self.kg.recipes.get(recipe_id)
            if not recipe or recipe.get("source_dataset") != FOODCOM_DATASET:
                raise ValueError(f"Recipe {recipe_id!r} is not an imported Food.com recipe.")
            details = self.kg.recipe_details(recipe_id) or recipe
            items = self._ingredient_items(details)
            considered = items[:self.MAX_INGREDIENTS_PER_RECIPE]
            recipe_context = {
                key: details[key] for key in ("name", "description", "method", "instructions")
                if key in details
            }
            recipe_context["name"] = recipe_context.get("name") or recipe.get("name")
            recipe_context["ingredients"] = [str(item.get("name") or "") for item in considered]
            recipe_summaries.append({
                "recipe_id": recipe_id,
                "recipe_name": recipe.get("name") or recipe_id,
                "ingredient_count": len(items),
                "ingredients_considered": len(considered),
                "truncated": len(items) > len(considered),
            })
            for item in considered:
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                parsed_item = {
                    "position": item.get("position"),
                    "name": name,
                    "quantity_raw": item.get("quantity_raw"),
                    "parsed": parse_foodcom_ingredient(name, item.get("quantity_raw")),
                }
                ingredient_key = self.mapper.mapping_cache_key(parsed_item, dataset_scope="all")
                key = f"{recipe_id}:{ingredient_key}"
                group = groups.get(key)
                if group is None:
                    group = groups[key] = {
                        "context_key": key,
                        "ingredient": name,
                        "normalized_name": parsed_item["parsed"].get("normalized_name"),
                        "parsed_item": parsed_item,
                        "recipe_context": recipe_context,
                        "occurrences": 0,
                        "source_recipes": [],
                        "source_quantities": [],
                    }
                group["occurrences"] += 1
                recipe_name = recipe.get("name") or recipe_id
                if recipe_name not in group["source_recipes"]:
                    group["source_recipes"].append(recipe_name)
                quantity = item.get("quantity_raw")
                if quantity is not None and str(quantity).strip():
                    example = {"recipe_name": recipe_name, "quantity_raw": str(quantity)}
                    if example not in group["source_quantities"] and len(group["source_quantities"]) < 4:
                        group["source_quantities"].append(example)

        ranked_groups = sorted(groups.values(), key=lambda group: (
            -group["occurrences"], group["normalized_name"] or "", group["context_key"],
        ))
        selected = ranked_groups[:self.MAX_UNIQUE_CONTEXTS]
        results = [self._resolve_group(group) for group in selected]
        return {
            "recipe_count": len(recipe_summaries),
            "recipes": recipe_summaries,
            "ingredient_occurrence_count": sum(row["ingredients_considered"] for row in recipe_summaries),
            "unique_context_count": len(groups),
            "processed_context_count": len(selected),
            "omitted_context_count": max(0, len(groups) - len(selected)),
            "resolved_count": sum(row["entity_resolution_status"] == "RESOLVED" for row in results),
            "family_resolved_count": sum(row["status"] == "FAMILY_RESOLVED" for row in results),
            "ambiguous_count": sum(row["entity_resolution_status"] == "AMBIGUOUS" for row in results),
            "slm_inference_calls": sum(bool(row.get("slm_inference_performed")) for row in results),
            "results": results,
        }

    @staticmethod
    def _ingredient_items(details):
        items = details.get("raw_ingredient_items") or details.get("ingredient_items")
        if isinstance(items, list) and items:
            return [item for item in items if isinstance(item, dict)]
        names = details.get("raw_ingredients") or details.get("raw_ingredient_names") or []
        quantities = details.get("raw_ingredient_quantities") or details.get("ingredient_quantities") or []
        if not isinstance(names, list):
            return []
        return [
            {"position": index, "name": name,
             "quantity_raw": quantities[index] if index < len(quantities) else None}
            for index, name in enumerate(names)
        ]

    def _resolve_group(self, group):
        parsed_item = group["parsed_item"]
        try:
            mapping = self.mapping_service.resolve_ingredient(
                parsed_item, group["recipe_context"],
            )
            return self._result(group, mapping)
        except Exception as exc:
            return {
                **self._public_group(group), "status": "UNRESOLVED",
                "entity_resolution_status": "AMBIGUOUS",
                "resolution_reason": f"Ingredient resolution failed ({type(exc).__name__}).",
                "candidates": [],
            }

    @staticmethod
    def _public_group(group):
        return {key: group[key] for key in (
            "context_key", "ingredient", "normalized_name", "occurrences",
            "source_recipes", "source_quantities",
        )}

    def _result(self, group, mapping):
        return {
            **self._public_group(group),
            "status": mapping.get("status"),
            "entity_resolution_status": mapping.get("entity_resolution_status"),
            "resolved_family": mapping.get("resolved_family"),
            "verification_method": mapping.get("verification_method"),
            "fdc_id": mapping.get("fdc_id"),
            "description": mapping.get("usda_description"),
            "candidate_ids": mapping.get("candidate_ids") or [],
            "candidates": mapping.get("candidates") or [],
            "resolution_reason": mapping.get("resolution_reason"),
            "unresolved_attributes": mapping.get("unresolved_attributes") or [],
            "slm_validation": mapping.get("slm_validation"),
            "slm_inference_performed": mapping.get("slm_inference_performed", False),
            "errors": mapping.get("errors") or [],
        }


__all__ = ["IngredientMappingReviewService"]

from src.generation.grounded_generator import GroundedRecipeGenerator, check_method_faithfulness


class RenAllyPipeline:
    def __init__(self, kg, retriever, engine, candidate_generator, generator=None):
        self.kg = kg
        self.retriever = retriever
        self.engine = engine
        self.candidate_generator = candidate_generator
        self.generator = generator
        self.safe_generator = GroundedRecipeGenerator()

    def _missing_data_response(self, query, missing_data):
        responder = getattr(self.generator, "respond_missing_data", None)
        if callable(responder):
            try:
                response = responder(query, missing_data)
                if isinstance(response, str) and response.strip():
                    return response
            except Exception:
                pass
        if missing_data["kind"] == "unsupported_recipe":
            choices = missing_data.get("supported_recipes", [])[:3]
            options = f" A few catalog options are: {'; '.join(choices)}." if choices else ""
            return (f"I couldn't find a stored recipe matching that dish, so I can't calculate its nutrients."
                    f"{options} Ask for a catalog meal plan, or provide foods and amounts for a calculation.")
        if missing_data.get("source_dataset") == "Food.com Recipes and Reviews (Kaggle)":
            return (
                f'I found "{missing_data.get("recipe_name", query)}" in the Food.com recipe catalog. '
                "The source lists ingredient quantity strings and servings, but they are not validated or normalized "
                "to USDA food records and gram amounts. I cannot calculate nutrient totals or check dietary limits. "
                "The source ingredients, quantities, serving count, and cooking steps are shown below as provided; "
                "missing units are not inferred."
            )
        missing = ", ".join(missing_data["missing_ingredients"])
        return (f"I found the recipe, but ingredient nutrition data is missing for: {missing}. "
                "I can't verify nutrient totals or dietary constraints for this recipe yet.")

    def _missing_data_result(self, query, missing_data):
        return {"recipe_id": None, "retrieval_score": 0.0, "evaluations": [],
                "selected": None, "answer": self._missing_data_response(query, missing_data),
                "missing_data": missing_data,
                "unsupported": missing_data["kind"] == "unsupported_recipe",
                "suggestions": missing_data.get("supported_recipes", [])}

    def run(self, query, constraints, use_llm=True, servings=1):
        match = self.retriever.retrieve_recipe(query)
        if match is None:
            suggestions = [self.kg.recipes[node_id]["name"]
                           for node_id, score in self.retriever.retrieve_nodes(query, k=3)
                           if score >= 0.2]
            # Don't turn a failed single-dish lookup into a dump of the entire
            # catalog. Only show a few retrieval matches when they are relevant.
            suggestions = suggestions[:3]
            return self._missing_data_result(query, {
                "kind": "unsupported_recipe", "requested_query": query,
                "message": "No matching recipe record exists in the current catalogue.",
                "supported_recipes": suggestions,
            })
        recipe_id, retrieval_score = match
        recipe_data = self.kg.recipes.get(recipe_id)
        if recipe_data is None:
            return self._missing_data_result(query, {
                "kind": "unsupported_recipe", "requested_query": query,
                "message": f"Recipe record '{recipe_id}' is missing.",
                "supported_recipes": [],
            })
        recipe_data = self.kg.recipe_details(recipe_id) or recipe_data
        recipe_items = recipe_data.get("ingredients") or []
        missing_ingredients = {item.get("id") or "<missing ingredient id>"
                               for item in recipe_items
                               if not item.get("id") or item.get("id") not in self.kg.ingredients}
        missing_details = {f"valid gram quantity for {item.get('id', '<unknown ingredient>')}"
                           for item in recipe_items
                           if not isinstance(item.get("amount_g"), (int, float)) or item.get("amount_g") < 0}
        if not recipe_items:
            missing_details.add("recipe ingredient list")
        missing_ingredients = sorted(missing_ingredients | missing_details)
        if missing_ingredients:
            missing_data = {
                "kind": "missing_ingredients", "requested_query": query,
                "recipe_id": recipe_id, "recipe_name": recipe_data.get("name", recipe_id),
                "message": "One or more recipe ingredient records are absent from the nutrient database.",
                "missing_ingredients": missing_ingredients,
            }
            if recipe_data.get("source_dataset") == "Food.com Recipes and Reviews (Kaggle)":
                missing_data.update({
                    "source_dataset": recipe_data.get("source_dataset"),
                    "source": recipe_data.get("source", {}),
                    "description": recipe_data.get("description", ""),
                    "raw_ingredient_names": recipe_data.get("raw_ingredients", []),
                    "raw_ingredient_quantities": recipe_data.get("raw_ingredient_quantities", []),
                    "raw_ingredient_items": recipe_data.get("raw_ingredient_items", []),
                    "recipe_servings": recipe_data.get("recipe_servings"),
                    "recipe_servings_raw": recipe_data.get("recipe_servings_raw"),
                    "recipe_yield": recipe_data.get("recipe_yield"),
                    "method": recipe_data.get("method", []),
                    "cook_time": recipe_data.get("cook_time"),
                    "prep_time": recipe_data.get("prep_time"),
                    "total_time": recipe_data.get("total_time"),
                    "nutrition_status": recipe_data.get("nutrition_status"),
                })
                missing_data["message"] = (
                    "Food.com source quantities and servings were retained, but they have no validated mapping to "
                    "USDA FoodData Central food records and gram amounts; nutrient totals and constraint checks "
                    "are unavailable."
                )
            return self._missing_data_result(query, missing_data)
        retrieved_context = self.kg.graph_context(recipe_id)
        candidates = self.candidate_generator.generate(recipe_id)
        evaluations = []
        for candidate in candidates:
            result = self.engine.evaluate(candidate["ingredients"], constraints, servings=servings)
            evaluations.append({**candidate, **result})

        valid = [item for item in evaluations
                 if item["passed"] is True and not item.get("not_evaluated")]
        if not valid:
            return {"recipe_id": recipe_id, "retrieval_score": retrieval_score,
                    "evaluations": evaluations, "selected": None, "answer": None,
                    "retrieval_context": retrieved_context}

        selected = max(valid, key=lambda item: item["compatibility"])
        method_steps = None
        if use_llm and self.generator and hasattr(self.generator, "generate_method"):
            try:
                proposed = self.generator.generate_method(
                    query, selected["ingredients"], self.kg.ingredients,
                    self.kg.recipes[recipe_id].get("name", recipe_id),
                    base_method=self.kg.recipes[recipe_id].get("method", []),
                )
                check = check_method_faithfulness(proposed, selected["ingredients"], self.kg.ingredients)
                if proposed and check["passed"]:
                    method_steps = proposed.splitlines()
            except Exception:
                method_steps = None
        if not method_steps:
            method_steps = [self.safe_generator.generate_method(
                query, selected["ingredients"], self.kg.ingredients,
                self.kg.recipes[recipe_id].get("name", recipe_id),
            )]
        answer = self.safe_generator.generate(
            query, selected["ingredients"], selected["totals"], self.kg.ingredients,
            servings=servings, recipe_data=self.kg.recipes[recipe_id],
            method_steps=method_steps,
        )
        if not constraints:
            answer += "\n\nNo numeric meal limit was entered, so suitability against a personal dietary limit was not evaluated."
        elif selected.get("not_evaluated"):
            answer += "\n\nAt least one entered limit could not be evaluated from the available nutrient data."
        elif selected.get("passed") is True:
            answer += (
                "\n\nThe calculated composition meets the explicit numeric limits entered. "
                "This is a calculation against those limits, not a personalized clinical recommendation."
            )
        return {"recipe_id": recipe_id, "retrieval_score": retrieval_score,
                "evaluations": evaluations, "selected": selected, "answer": answer,
                "retrieval_context": retrieved_context}

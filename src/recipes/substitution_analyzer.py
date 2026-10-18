"""Database-grounded nutrient comparisons for ingredient substitutions."""

from src.nutrition import NUTRIENT_UNITS


class SubstitutionAnalyzer:
    """Assess a proposed swap using retrieved foods, recipe amounts, and limits."""

    def __init__(self, kg, retriever, constraint_engine):
        self.kg = kg
        self.retriever = retriever
        self.engine = constraint_engine

    def _lookup(self, query):
        # Substitution and recipe membership relationships are curated only for
        # Foundation Foods; do not silently pull an SR Legacy node into that graph.
        ranked = self.retriever.resolve_ingredient_candidates(
            query, k=5, dataset_scope="foundation_foods",
        )
        if not ranked:
            return None
        ingredient_id, score = ranked[0]
        tied = [(candidate_id, candidate_score) for candidate_id, candidate_score in ranked
                if abs(candidate_score - score) < 1e-9]
        if len(tied) > 1:
            return {
                "ambiguous_options": [
                    {"id": candidate_id,
                     "name": self.kg.ingredients.get(candidate_id, {}).get("name", candidate_id),
                     "description": self.kg.ingredients.get(candidate_id, {}).get("fdc_description", "")}
                    for candidate_id, _ in tied
                ]
            }
        data = self.kg.ingredients.get(ingredient_id)
        if not data:
            return None
        nutrients = {key: value for key, value in data.get("nutrients_per_100g", {}).items()
                     if value is not None}
        return {
            "id": ingredient_id,
            "name": data.get("name", ingredient_id),
            "description": data.get("fdc_description", data.get("name", ingredient_id)),
            "match_score": score,
            "ingredient_type": data.get("ingredient_type"),
            "nutrients_per_100g": nutrients,
            "nutrient_units": {**NUTRIENT_UNITS, **data.get("nutrient_units", {})},
            "nutrient_names": data.get("nutrient_names", {}),
            "source": data.get("source", {}),
            "min_amount_g": data.get("min_amount_g"),
            "max_amount_g": data.get("max_amount_g"),
        }

    @staticmethod
    def _nutrient_rows(original_totals, replacement_totals, original_amount, replacement_amount,
                       units=None, names=None):
        units, names = units or {}, names or {}
        rows = []
        for nutrient in NUTRIENT_UNITS:
            if nutrient not in original_totals or nutrient not in replacement_totals:
                continue
            before = original_totals[nutrient]
            after = replacement_totals[nutrient]
            rows.append({
                "nutrient": nutrient,
                "label": names.get(nutrient, nutrient.replace("_", " ")),
                "unit": units.get(nutrient, NUTRIENT_UNITS[nutrient]),
                "original": before,
                "replacement": after,
                "delta": after - before,
                "original_ingredient_amount_g": original_amount,
                "replacement_ingredient_amount_g": replacement_amount,
            })
        return rows

    def _recipe_for(self, original_id, recipe_query):
        recipe_ids = self.kg.recipes_containing_ingredient(original_id)
        if recipe_query:
            match = self.retriever.retrieve_recipe(recipe_query)
            if not match:
                return None, recipe_ids, "recipe_not_found"
            recipe_id = match[0]
            if recipe_id not in recipe_ids:
                return recipe_id, recipe_ids, "ingredient_not_in_recipe"
            return recipe_id, recipe_ids, None
        if len(recipe_ids) == 1:
            return recipe_ids[0], recipe_ids, None
        if not recipe_ids:
            return None, recipe_ids, "no_recipe"
        return None, recipe_ids, "ambiguous_recipe"

    @staticmethod
    def _ingredient_comparison(original, substitute):
        rows = []
        left, right = original["nutrients_per_100g"], substitute["nutrients_per_100g"]
        for nutrient in NUTRIENT_UNITS:
            if nutrient not in left or nutrient not in right:
                continue
            rows.append({
                "nutrient": nutrient,
                "label": original["nutrient_names"].get(nutrient, nutrient.replace("_", " ")),
                "unit": original["nutrient_units"].get(nutrient, NUTRIENT_UNITS[nutrient]),
                "original": left[nutrient],
                "replacement": right[nutrient],
                "delta": right[nutrient] - left[nutrient],
                "basis": "per 100 g",
            })
        return rows

    def assess(self, original_query, substitute_query, constraints=None, servings=1, recipe_query=None):
        constraints = constraints or {}
        original = self._lookup(original_query)
        substitute = self._lookup(substitute_query)
        assessment = {
            "status": "NOT_ASSESSABLE", "original": original, "substitute": substitute,
            "recipe": None, "same_amount_g": None, "recommended_amount_g": None,
            "amount_basis": None, "nutrient_comparison": [], "limit_check": None,
            "candidate_recipes": [], "ambiguous_ingredients": [],
        }
        for side, query, item in (
            ("original", original_query, original),
            ("substitute", substitute_query, substitute),
        ):
            if item and item.get("ambiguous_options"):
                assessment["ambiguous_ingredients"].append({
                    "side": side, "query": query, "options": item["ambiguous_options"],
                })
        if assessment["ambiguous_ingredients"]:
            assessment["status"] = "AMBIGUOUS_INGREDIENT"
            sides = ", ".join(
                f"{item['side']} ({', '.join(option['name'] for option in item['options'])})"
                for item in assessment["ambiguous_ingredients"]
            )
            assessment["original"] = None if original and original.get("ambiguous_options") else original
            assessment["substitute"] = None if substitute and substitute.get("ambiguous_options") else substitute
            assessment["answer"] = (
                f"I found multiple Foundation Foods records for {sides}. Specify the exact food state or record "
                "so I don't calculate the substitution using the wrong item."
            )
            return assessment
        missing = []
        if original is None:
            missing.append(original_query)
        if substitute is None:
            missing.append(substitute_query)
        if missing:
            assessment["missing_ingredients"] = missing
            assessment["answer"] = (
                f"I couldn't find a matching Foundation Foods record for {', '.join(missing)}. "
                "I can't compare the nutrient values or recommend a replacement amount without those records."
            )
            return assessment

        assessment["unavailable_nutrients"] = [
            nutrient for nutrient in NUTRIENT_UNITS
            if nutrient not in original["nutrients_per_100g"]
            or nutrient not in substitute["nutrients_per_100g"]
        ]
        assessment["nutrient_comparison"] = self._ingredient_comparison(original, substitute)
        recipe_id, recipe_ids, recipe_issue = self._recipe_for(original["id"], recipe_query)
        assessment["candidate_recipes"] = [
            {"id": rid, "name": self.kg.recipes.get(rid, {}).get("name", rid)} for rid in recipe_ids
        ]
        if recipe_issue:
            if recipe_issue == "ingredient_not_in_recipe":
                recipe = self.kg.recipes.get(recipe_id, {})
                assessment["recipe"] = {"id": recipe_id, "name": recipe.get("name", recipe_id)}
                assessment["status"] = "SOURCE_NOT_IN_RECIPE"
                assessment["answer"] = (
                    f"I found Foundation Foods records for {original['name']} and {substitute['name']}, "
                    f"but {original['name']} is not an ingredient in {recipe.get('name', recipe_id)}. "
                    "I can't evaluate that swap in the named recipe."
                )
            elif recipe_issue == "recipe_not_found":
                assessment["status"] = "RECIPE_NOT_FOUND"
                assessment["answer"] = (
                    f"I found both ingredient records, but couldn't find the named recipe in the local recipe graph. "
                    "I can compare the foods per 100 g, but can't calculate a recipe-level replacement amount."
                )
            elif recipe_issue == "ambiguous_recipe":
                assessment["status"] = "AMBIGUOUS_RECIPE"
                names = ", ".join(item["name"] for item in assessment["candidate_recipes"])
                assessment["answer"] = (
                    f"I found both ingredient records, and {original['name']} appears in multiple prototype recipes: "
                    f"{names}. The per-100 g nutrient comparison is shown below; name the recipe to check its portion and limits."
                )
            else:
                assessment["status"] = "NO_RECIPE_CONTEXT"
                assessment["answer"] = (
                    f"I found both ingredient records, but no local recipe contains {original['name']}. "
                    "The per-100 g comparison is shown below; a recipe and portion are needed for a serving-level assessment."
                )
            return assessment

        recipe_data = self.kg.recipes.get(recipe_id)
        if not recipe_data:
            assessment["answer"] = "The recipe graph returned a record that is unavailable in the local catalog; I can't calculate this swap."
            return assessment
        recipe = self.kg.recipe_ingredients(recipe_id)
        original_amount = sum(amount for ingredient_id, amount in recipe if ingredient_id == original["id"])
        if original_amount <= 0:
            assessment["status"] = "SOURCE_NOT_IN_RECIPE"
            assessment["recipe"] = {"id": recipe_id, "name": recipe_data.get("name", recipe_id)}
            assessment["answer"] = f"{original['name']} is not present in {recipe_data.get('name', recipe_id)}, so I can't assess it as a replacement there."
            return assessment

        assessment["recipe"] = {
            "id": recipe_id, "name": recipe_data.get("name", recipe_id),
            "description": recipe_data.get("description", ""),
            "source": recipe_data.get("source", {}),
        }
        assessment["same_amount_g"] = original_amount
        substitute_data = self.kg.ingredients.get(substitute["id"], {})
        assessment["same_amount_within_prototype_range"] = (
            (substitute_data.get("min_amount_g") is None or original_amount >= substitute_data["min_amount_g"])
            and (substitute_data.get("max_amount_g") is None or original_amount <= substitute_data["max_amount_g"])
        )
        same_amount_recipe = self.engine.replace_ingredient(
            recipe, original["id"], substitute["id"], original_amount,
        )
        if same_amount_recipe is None:
            assessment["answer"] = "The source ingredient was not present in the retrieved recipe, so I can't assess this swap."
            return assessment
        original_totals = self.engine.nutrient_totals(recipe)
        same_amount_totals = self.engine.nutrient_totals(same_amount_recipe)
        assessment["unavailable_nutrients"] = [
            nutrient for nutrient in NUTRIENT_UNITS
            if nutrient not in original_totals or nutrient not in same_amount_totals
        ]
        units = {**NUTRIENT_UNITS, **original["nutrient_units"]}
        assessment["nutrient_comparison"] = self._nutrient_rows(
            original_totals, same_amount_totals, original_amount, original_amount,
            units, original["nutrient_names"],
        )

        if constraints:
            evaluation = self.engine.evaluate(same_amount_recipe, constraints, servings=servings)
            if evaluation["passed"] is True and not evaluation["not_evaluated"]:
                assessment.update({
                    "status": "PASS_SAME_AMOUNT", "recommended_amount_g": original_amount,
                    "amount_basis": "same portion; full recipe passes entered numeric limits",
                    "limit_check": evaluation,
                })
                assessment["answer"] = (
                    f"Yes, the database contains both foods. In {recipe_data.get('name', recipe_id)}, "
                    f"replacing {original_amount:g} g of {original['name']} with {original_amount:g} g of "
                    f"{substitute['name']} per serving keeps the calculated recipe within the numeric limits entered. "
                    "The nutrient comparison below shows how the totals change; this is not an identical nutrient profile."
                )
                return assessment
            if evaluation["not_evaluated"]:
                assessment["status"] = "NOT_ASSESSABLE"
                assessment["limit_check"] = evaluation
                assessment["answer"] = (
                    "Both food records are present, but at least one entered nutrient limit cannot be checked because "
                    "the required nutrient data is missing or unsupported. I can't confirm this substitution against those limits."
                )
                return assessment

            adjusted = self.engine.find_feasible_substitution_amount(
                recipe, original["id"], substitute["id"], constraints, servings, original_amount,
            )
            if adjusted["status"] == "feasible":
                amount = adjusted["amount_g"]
                adjusted_evaluation = adjusted["evaluation"]
                limit_reasons = [reason for reason in evaluation["reasons"]
                                 if "outside plausible demo range" not in reason]
                assessment.update({
                    "status": "PASS_ADJUSTED_AMOUNT", "recommended_amount_g": amount,
                    "amount_basis": "adjusted portion; full recipe passes entered numeric limits",
                    "limit_check": adjusted_evaluation,
                    "nutrient_comparison": self._nutrient_rows(
                        original_totals, adjusted_evaluation["totals"], original_amount, amount,
                        units, original["nutrient_names"],
                    ),
                })
                if limit_reasons:
                    assessment["answer"] = (
                        f"At the original {original_amount:g} g portion, the swap fails an entered numeric limit. "
                        f"Using {amount:g} g of {substitute['name']} per serving keeps the calculated recipe within the "
                        "limits, with all other listed ingredients unchanged. The adjusted totals are shown below."
                    )
                else:
                    assessment["answer"] = (
                        f"The equal-weight portion is outside the substitute's recorded prototype range. Using "
                        f"{amount:g} g of {substitute['name']} per serving is within that range and meets the "
                        "entered numeric limits, with all other listed ingredients unchanged. The adjusted totals are shown below."
                    )
                return assessment

            if adjusted["status"] == "not_verifiable":
                assessment["status"] = "ADJUSTMENT_NOT_VERIFIABLE"
                assessment["limit_check"] = {
                    **evaluation, "adjustment_issue": adjusted["reason"],
                    "adjustment_status": adjusted["status"],
                }
                assessment["answer"] = (
                    "The equal-weight replacement does not meet the entered limits, and the available data cannot "
                    f"validate an adjusted quantity: {adjusted['reason']} I can't recommend an amount from the available data."
                )
                return assessment

            assessment["status"] = "FAIL_LIMITS"
            assessment["limit_check"] = {
                **evaluation, "adjustment_issue": adjusted["reason"],
                "adjustment_status": adjusted["status"],
            }
            assessment["answer"] = (
                f"Both foods are in the database, but no portion within the recorded prototype range for "
                f"{substitute['name']} makes {recipe_data.get('name', recipe_id)} meet all entered numeric limits "
                "while keeping the other ingredients unchanged. See the failed checks below."
            )
            return assessment

        # No meal budget was entered, so only compare verified totals; do not
        # label a food as clinically suitable or unsuitable without a criterion.
        source_protein = original["nutrients_per_100g"].get("protein_g")
        target_protein = substitute["nutrients_per_100g"].get("protein_g")
        protein_amount = None
        if source_protein is not None and target_protein is not None and target_protein > 0:
            protein_amount = original_amount * source_protein / target_protein
        assessment["status"] = "COMPARISON_ONLY"
        if protein_amount is not None:
            assessment["protein_equivalent_amount_g"] = protein_amount
            assessment["protein_equivalent_within_prototype_range"] = (
                (substitute.get("min_amount_g") is None or protein_amount >= substitute["min_amount_g"])
                and (substitute.get("max_amount_g") is None or protein_amount <= substitute["max_amount_g"])
            )
        changes = [
            f"{row['label']}: {row['original']:.1f} to {row['replacement']:.1f} {row['unit']}"
            for row in assessment["nutrient_comparison"]
            if row["nutrient"] in {"sodium_mg", "potassium_mg", "phosphorus_mg", "protein_g"}
        ]
        change_text = "; ".join(changes) if changes else "some nutrient values are missing"
        answer = (
            f"Both foods have Foundation Foods records. In {recipe_data.get('name', recipe_id)}, replacing "
            f"{original_amount:g} g of {original['name']} with the same amount of {substitute['name']} per serving "
            f"changes the calculated recipe totals as follows: {change_text}."
        )
        if protein_amount is not None:
            answer += f" About {protein_amount:.1f} g of {substitute['name']} would match the original portion's protein amount only."
            if not assessment["protein_equivalent_within_prototype_range"]:
                answer += " That protein-matching portion is outside the recorded prototype range."
        if not assessment["same_amount_within_prototype_range"]:
            answer += " The equal-weight replacement is outside the recorded prototype range."
        answer += " No numeric meal limit was entered, so I can't classify the swap as passing or failing a dietary target."
        assessment["answer"] = answer
        if protein_amount is not None:
            protein_recipe = self.engine.replace_ingredient(
                recipe, original["id"], substitute["id"], protein_amount,
            )
            protein_totals = self.engine.nutrient_totals(protein_recipe)
            assessment["protein_equivalent_nutrients"] = protein_totals
            assessment["protein_equivalent_comparison"] = self._nutrient_rows(
                original_totals, protein_totals, original_amount, protein_amount,
                units, original["nutrient_names"],
            )
        return assessment

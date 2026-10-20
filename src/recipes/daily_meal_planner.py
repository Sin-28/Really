"""Joint, deterministic planning of three newly weighed Food.com meals."""

from copy import deepcopy
from itertools import product

from src.constraints.constraint_engine import ConstraintEngine
from src.constraints.recipe_nutrient_adapter import RecipeNutrientConstraintAdapter
from src.nutrition import NUTRIENT_UNITS
from src.recipes.ingredient_quantity_optimiser import NUTRIENTS
from src.recipes.recipe_nutrient_enrichment import assess_foodcom_nutrient_evidence


SLOTS = ("breakfast", "lunch", "dinner")
DATASET = "Food.com Recipes and Reviews (Kaggle)"


class DailyMealPlanner:
    """Search bounded slot pools and solve selected meals as one LP."""

    def __init__(self, kg, retriever, nutrient_service, optimiser, culinary_validator):
        self.kg = kg
        self.retriever = retriever
        self.nutrient_service = nutrient_service
        self.optimiser = optimiser
        self.culinary_validator = culinary_validator

    @staticmethod
    def _failure(status, reason, daily_constraints, issues=None):
        return {"status": status, "nutritional_status": "NOT_EVALUATED",
                "reason": reason, "meals": [], "daily_nutrient_totals": None,
                "applied_daily_constraints": daily_constraints, "candidate_issues": issues or [],
                "limitations": ["No clinical limit was inferred from a condition or recipe label.",
                                "Food.com source quantities were not used as weights."]}

    @staticmethod
    def _bounds_for_slot(value, slot):
        if isinstance(value, dict) and "min_g" not in value and "max_g" not in value:
            return value.get(slot)
        return value

    @staticmethod
    def _rules_for_slot(value, slot):
        if isinstance(value, dict) and "basis" not in value:
            return value.get(slot)
        return value

    @staticmethod
    def _numeric_constraints(constraints, scope):
        result = {}
        for key, rule in constraints.items():
            if not isinstance(rule, dict) or rule.get("scope") != scope:
                continue
            result[key] = {**rule, "scope": "meal"}
        return result

    def _pool(self, slot, query, *, role_bounds, weight_bounds, culinary_rules, issues):
        preference = "low sodium " if "low-sodium" in query.casefold() or "low sodium" in query.casefold() else ""
        hits = self.retriever.recommend_foodcom_recipes(preference + slot, k=7)
        if not hits and preference:
            hits = self.retriever.recommend_foodcom_recipes(slot, k=7)
        pool = []
        for hit in hits:
            rid = hit["recipe_id"]
            recipe = self.kg.recipe_details(rid) or self.kg.recipes.get(rid)
            if not recipe or recipe.get("source_dataset") != DATASET:
                continue
            try:
                calculation = self.nutrient_service.enrich(recipe, max_ingredients=20)
                listed = recipe.get("raw_ingredients") or recipe.get("raw_ingredient_names") or []
                if not listed or len(listed) != len(calculation.get("ingredients") or []):
                    issues.append({"slot": slot, "recipe_id": rid, "status": "MISSING_NUTRIENT_DATA",
                                   "reason": "The complete source ingredient list was not assessed."})
                    continue
                evidence = assess_foodcom_nutrient_evidence(calculation)
                if evidence["nutritional_status"] != "REQUIRES_OPTIMISATION":
                    issues.append({"slot": slot, "recipe_id": rid, "status": "MISSING_NUTRIENT_DATA",
                                   "missing_or_unresolved_ingredients": evidence["missing_or_unresolved_ingredients"]})
                    continue
                preliminary = self.optimiser.optimise(
                    recipe, calculation, {}, role_bounds=role_bounds,
                    total_weight_bounds=weight_bounds, allow_unconstrained=True)
                if preliminary["optimisation_status"] != "OPTIMISED":
                    issues.append({"slot": slot, "recipe_id": rid,
                                   "status": preliminary["optimisation_status"],
                                   "reason": preliminary["reason"],
                                   "missing_quantity_bounds": preliminary.get("missing_quantity_bounds")})
                    continue
                validation = self.culinary_validator.validate(recipe, preliminary, culinary_rules)
                nonnumeric = {item["code"] for item in validation["findings"]} - {
                    "ROLE_AMOUNT_LIMIT", "ROLE_PROPORTION", "INGREDIENT_RATIO"
                }
                if nonnumeric:
                    issues.append({"slot": slot, "recipe_id": rid, "status": "CULINARY_INVALID",
                                   "findings": validation["findings"]})
                    continue
                pool.append({"slot": slot, "recipe": recipe, "calculation": calculation,
                             "validation": validation, "score": hit["retrieval_score"]})
                if len(pool) == 4:
                    break
            except Exception as exc:
                issues.append({"slot": slot, "recipe_id": rid, "status": "OPTIMISATION_ERROR",
                               "reason": f"Candidate preparation failed ({type(exc).__name__})."})
        return pool

    @staticmethod
    def _append_rule(rules, coefficients, operator, limit, basis, code, slot):
        rules.append({"coefficients": coefficients, "operator": operator,
                      "limit": float(limit), "basis": basis, "code": code, "slot": slot})

    def _solve_combination(self, selected, daily_constraints, meal_constraints,
                           role_bounds, total_weight_bounds, culinary_rules):
        rows, names, boundaries = [], [], []
        for candidate in selected:
            start = len(rows)
            source_rows = candidate["calculation"]["ingredients"]
            for source in source_rows:
                row = deepcopy(source)
                row["position"] = len(rows)
                rows.append(row)
                names.append(row["ingredient_name"])
            boundaries.append((start, len(rows)))
        recipe = {"id": "constructed:daily", "name": "Constructed daily combination",
                  "source_dataset": DATASET, "raw_ingredients": names}
        calculation = {"calculation_status": "UNAVAILABLE", "ingredients": rows}
        count = len(rows)
        rules = []
        meal_bounds = RecipeNutrientConstraintAdapter._active_bounds(meal_constraints)
        for candidate, (start, end) in zip(selected, boundaries):
            slot = candidate["slot"]
            for rule in candidate["validation"]["_linear_constraints"]:
                vector = [0.0] * count
                vector[start:end] = rule["coefficients"]
                self._append_rule(rules, vector, rule["operator"], rule["limit"],
                                  rule["basis"], rule["code"], slot)
            weight = self._bounds_for_slot(total_weight_bounds, slot)
            if weight is not None:
                if not isinstance(weight, dict) or not weight.get("basis"):
                    return None, "MISSING_QUANTITY_BOUNDS"
                vector = [1.0 if start <= i < end else 0.0 for i in range(count)]
                for field, operator in (("min_g", ">="), ("max_g", "<=")):
                    if field not in weight:
                        return None, "MISSING_QUANTITY_BOUNDS"
                    self._append_rule(rules, vector, operator, weight[field],
                                      weight["basis"], "TOTAL_MEAL_WEIGHT", slot)
            for bound in meal_bounds:
                key = bound["nutrient_key"]
                if (key not in NUTRIENTS or bound["operator"] not in {"<=", ">="}
                        or bound.get("unit") != NUTRIENT_UNITS[key]):
                    return None, "OPTIMISATION_ERROR"
                vector = [0.0] * count
                for index in range(start, end):
                    vector[index] = rows[index]["nutrient_measurements_per_100g"][key]["value_per_100g"] / 100.0
                self._append_rule(rules, vector, bound["operator"], bound["threshold"],
                                  "explicit user meal limit", "MEAL_NUTRIENT_LIMIT", slot)
        result = self.optimiser.optimise(
            recipe, calculation, self._numeric_constraints(daily_constraints, "day"),
            role_bounds=role_bounds, culinary_linear_constraints=rules,
            allow_unconstrained=True)
        if result["optimisation_status"] != "OPTIMISED":
            return None, result["optimisation_status"]
        ingredients = result["ingredients"]
        meals = []
        for candidate, (start, end) in zip(selected, boundaries):
            source = candidate["recipe"]
            portion = ingredients[start:end]
            profile = {str(index): {
                "name": row["ingredient_name"],
                "min_amount_g": portion[index]["minimum_g"],
                "max_amount_g": portion[index]["maximum_g"],
                "nutrients_per_100g": {key: row["nutrient_measurements_per_100g"][key]["value_per_100g"]
                                       for key in NUTRIENTS},
            } for index, row in enumerate(candidate["calculation"]["ingredients"])}
            engine = ConstraintEngine(profile)
            evaluated = engine.evaluate([(str(i), item["quantity_g"]) for i, item in enumerate(portion)],
                                        meal_constraints, servings=1)
            if evaluated["passed"] is not True or evaluated["not_evaluated"]:
                return None, "OPTIMISATION_ERROR"
            validation = self.culinary_validator.validate(
                source, {"optimisation_status": "OPTIMISED", "ingredients": portion},
                self._rules_for_slot(culinary_rules, candidate["slot"]))
            if validation["status"] == "CULINARY_INVALID":
                return None, "CULINARY_INVALID"
            sourced_ingredients = [
                {**item, "mapping_status": row["mapping"].get("status"),
                 "usda_reference": row.get("usda_reference"),
                 "nutrient_profile_per_100g": row["nutrient_measurements_per_100g"]}
                for item, row in zip(portion, candidate["calculation"]["ingredients"])
            ]
            meals.append({"slot": candidate["slot"], "recipe_id": source["id"],
                          "name": source.get("name"), "retrieval_score": candidate["score"],
                          "ingredients": sourced_ingredients, "nutrient_totals": {
                              key: evaluated["totals"][key] for key in NUTRIENTS},
                          "culinary_status": validation["status"],
                          "missing_culinary_information": validation["missing_information"],
                          "applied_culinary_constraints": candidate["validation"]["_linear_constraints"],
                          "applied_identity_requirements": validation.get("applied_identity_requirements"),
                          "instructions": ([source["method"]] if isinstance(source.get("method"), str)
                                           else source.get("method") or []),
                          "serving_basis": "one newly constructed meal serving"})
        profiles = {str(i): {"name": item["name"],
                             "nutrients_per_100g": {
                                 key: row["nutrient_measurements_per_100g"][key]["value_per_100g"]
                                 for key in NUTRIENTS}}
                    for i, (item, row) in enumerate(zip(ingredients, rows))}
        daily_check = ConstraintEngine(profiles).evaluate(
            [(str(i), item["quantity_g"]) for i, item in enumerate(ingredients)],
            self._numeric_constraints(daily_constraints, "day"), servings=1)
        if daily_check["passed"] is not True or daily_check["not_evaluated"]:
            return None, "OPTIMISATION_ERROR"
        return {"meals": meals, "daily_nutrient_totals": {
                    key: daily_check["totals"][key] for key in NUTRIENTS},
                "applied_deterministic_constraints": rules}, None

    def plan(self, query, daily_constraints, *, meal_constraints=None, role_bounds=None,
             total_meal_weight_bounds=None, culinary_rules=None, servings=1):
        daily_constraints = daily_constraints or {}
        meal_constraints = meal_constraints or {}
        issues = []
        pools = []
        for slot in SLOTS:
            pool = self._pool(
                slot, query, role_bounds=role_bounds,
                weight_bounds=self._bounds_for_slot(total_meal_weight_bounds, slot),
                culinary_rules=self._rules_for_slot(culinary_rules, slot), issues=issues)
            pools.append(pool)
        if any(not pool for pool in pools):
            statuses = {item["status"] for item in issues}
            status = ("MISSING_NUTRIENT_DATA" if "MISSING_NUTRIENT_DATA" in statuses else
                      "MISSING_QUANTITY_BOUNDS" if "MISSING_QUANTITY_BOUNDS" in statuses else
                      "INSUFFICIENT_CANDIDATES")
            return self._failure(status, "At least one meal slot has no usable Food.com candidate.",
                                 daily_constraints, issues)
        last_failure = "INFEASIBLE_CONSTRAINTS"
        for selected in product(*pools):
            if len({item["recipe"]["id"] for item in selected}) != 3:
                continue
            solved, failure = self._solve_combination(
                selected, daily_constraints, meal_constraints,
                role_bounds, total_meal_weight_bounds, culinary_rules)
            if solved is None:
                last_failure = failure or last_failure
                continue
            culinary_unverified = any(meal["culinary_status"] != "CULINARY_VALID"
                                      for meal in solved["meals"])
            status = ("DAILY_LIMITS_UNSPECIFIED" if not daily_constraints else
                      "CULINARY_UNVERIFIED" if culinary_unverified else "FEASIBLE")
            return {"status": status,
                    "nutritional_status": "FEASIBLE" if daily_constraints else "NOT_EVALUATED",
                    "reason": ("All explicit daily nutrient limits pass for the constructed combination."
                               if daily_constraints else
                               "Constructed portions have totals, but no personal daily numeric limits were supplied."),
                    **solved, "applied_daily_constraints": daily_constraints,
                    "requested_servings": servings,
                    "daily_nutrient_totals_for_requested_servings": {
                        key: value * servings for key, value in solved["daily_nutrient_totals"].items()},
                    "applied_meal_constraints": meal_constraints,
                    "candidate_issues": issues,
                    "serving_basis": "one person; one newly constructed serving per meal",
                    "limitations": [
                        "Food.com unitless source quantities were not used as weights or ratios.",
                        "Unspecified culinary rules remain unverified.",
                        "Numeric feasibility does not establish medical safety.",
                    ]}
        return self._failure(last_failure, "No distinct breakfast, lunch, and dinner combination satisfies all supplied constraints.",
                             daily_constraints, issues)


__all__ = ["DailyMealPlanner"]

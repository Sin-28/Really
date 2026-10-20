"""Deterministic, bounded composition of independently chosen ingredient grams."""

import math

from src.constraints.constraint_engine import ConstraintEngine
from src.constraints.recipe_nutrient_adapter import RecipeNutrientConstraintAdapter
from src.nutrition import PRIMARY_RECIPE_NUTRIENTS, NUTRIENT_UNITS
from src.recipes.recipe_nutrient_enrichment import assess_foodcom_nutrient_evidence


NUTRIENTS = tuple(value[0] for value in PRIMARY_RECIPE_NUTRIENTS.values())
ROLE_NAMES = {
    "grain": "base", "base": "base", "starch": "base", "legume": "base",
    "protein": "protein_source", "protein_source": "protein_source",
    "vegetable": "vegetable", "vegetables": "vegetable",
    "sauce": "seasoning", "seasoning": "seasoning", "spice": "seasoning",
    "aromatic": "aromatic", "aromatics": "aromatic", "other": "other",
    "dairy": "other", "fruit": "other", "seed": "other", "nut": "other",
    "oil": "other", "prepared_reference": "other", "beverage": "other",
}


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value) or (value <= 0 if positive else value < 0):
        return None
    return float(value)


class _FeasibilitySimplex:
    """Small two-phase simplex for maximising c x with A x <= b, x >= 0."""

    EPS = 1e-9

    def __init__(self, matrix, limits, variable_count, objective=None):
        self.m, self.n = len(limits), variable_count
        self.basis = [self.n + i for i in range(self.m)]
        self.nonbasis = list(range(self.n)) + [-1]
        self.table = [[0.0] * (self.n + 2) for _ in range(self.m + 2)]
        for i, row in enumerate(matrix):
            self.table[i][:self.n] = row
            self.table[i][self.n] = -1.0
            self.table[i][self.n + 1] = limits[i]
        for j, value in enumerate(objective or [0.0] * self.n):
            self.table[self.m][j] = -value
        self.table[self.m + 1][self.n] = 1.0

    def _pivot(self, row, col):
        table = self.table
        inverse = 1.0 / table[row][col]
        for i in range(self.m + 2):
            if i == row:
                continue
            for j in range(self.n + 2):
                if j != col:
                    table[i][j] -= table[row][j] * table[i][col] * inverse
        for j in range(self.n + 2):
            if j != col:
                table[row][j] *= inverse
        for i in range(self.m + 2):
            if i != row:
                table[i][col] *= -inverse
        table[row][col] = inverse
        self.basis[row], self.nonbasis[col] = self.nonbasis[col], self.basis[row]

    def _run(self, phase):
        objective = self.m + 1 if phase == 1 else self.m
        for _ in range(10000):
            entering = min(
                (j for j in range(self.n + 1)
                 if phase == 1 or self.nonbasis[j] != -1),
                key=lambda j: (self.table[objective][j], self.nonbasis[j]),
            )
            if self.table[objective][entering] >= -self.EPS:
                return True
            eligible = [i for i in range(self.m)
                        if self.table[i][entering] > self.EPS]
            if not eligible:
                return False
            leaving = min(eligible, key=lambda i: (
                self.table[i][self.n + 1] / self.table[i][entering], self.basis[i],
            ))
            self._pivot(leaving, entering)
        raise ArithmeticError("Feasibility pivot limit exceeded.")

    def solve(self):
        if self.m == 0:
            return [0.0] * self.n
        row = min(range(self.m), key=lambda i: self.table[i][self.n + 1])
        if self.table[row][self.n + 1] < -self.EPS:
            self._pivot(row, self.n)
            if not self._run(1) or self.table[self.m + 1][self.n + 1] < -self.EPS:
                return None
            if abs(self.table[self.m + 1][self.n + 1]) > self.EPS:
                return None
            for i in range(self.m):
                if self.basis[i] == -1:
                    col = min(range(self.n + 1), key=lambda j: (
                        self.table[i][j], self.nonbasis[j],
                    ))
                    self._pivot(i, col)
        if not self._run(2):
            raise ArithmeticError("Feasibility tableau became unbounded.")
        result = [0.0] * self.n
        for i in range(self.m):
            if self.basis[i] < self.n:
                result[self.basis[i]] = self.table[i][self.n + 1]
        return result


class IngredientQuantityOptimiser:
    """Find one gram-weighted serving using graph bounds and verified profiles."""

    def __init__(self, kg):
        self.kg = kg

    @staticmethod
    def _result(status, reason, recipe, **details):
        return {"optimisation_status": status, "reason": reason,
                "recipe_id": recipe.get("id"), "name": recipe.get("name"),
                "ingredients": [], "nutrient_totals": None, **details}

    def optimise(self, recipe, calculation, constraints, *, serving_count=1,
                 role_bounds=None, total_weight_bounds=None, culinary_linear_constraints=None,
                 allow_unconstrained=False):
        """Treat Food.com amounts as metadata; construct one new serving."""
        try:
            return self._optimise(recipe, calculation, constraints, serving_count,
                                  {} if role_bounds is None else role_bounds,
                                  total_weight_bounds, culinary_linear_constraints or [], None,
                                  allow_unconstrained)
        except Exception as exc:
            return self._result("OPTIMISATION_ERROR", f"Optimisation failed ({type(exc).__name__}).", recipe)

    def optimise_ranges(self, recipe, calculation, constraints, *, recommended,
                        serving_count=1, role_bounds=None, total_weight_bounds=None,
                        culinary_linear_constraints=None):
        """Optimise each endpoint jointly while retaining a verified recommendation."""
        try:
            result = self._optimise(recipe, calculation, constraints, serving_count,
                                    {} if role_bounds is None else role_bounds,
                                    total_weight_bounds, culinary_linear_constraints or [], recommended, False)
            if result["optimisation_status"] != "OPTIMISED":
                return {"status": (result["optimisation_status"] if result["optimisation_status"] in {
                    "INFEASIBLE_CONSTRAINTS", "MISSING_NUTRIENT_DATA", "MISSING_QUANTITY_BOUNDS"
                } else "OPTIMISATION_ERROR"), "reason": result["reason"],
                    "recipe_id": recipe.get("id"), "name": recipe.get("name"),
                    "recommended_ingredients": [], "ingredient_ranges": [],
                    "missing_quantity_bounds": result.get("missing_quantity_bounds"),
                    "missing_or_unresolved_ingredients": result.get("missing_or_unresolved_ingredients")}
            return result["quantity_ranges"]
        except Exception as exc:
            return {"status": "OPTIMISATION_ERROR", "reason": f"Range optimisation failed ({type(exc).__name__}).",
                    "recipe_id": recipe.get("id"), "name": recipe.get("name"),
                    "recommended_ingredients": [], "ingredient_ranges": []}

    def _optimise(self, recipe, calculation, constraints, serving_count,
                  role_bounds, total_weight_bounds, culinary_linear_constraints, recommended,
                  allow_unconstrained):
        if recipe.get("source_dataset") != "Food.com Recipes and Reviews (Kaggle)":
            return self._result("OPTIMISATION_ERROR", "A Food.com recipe is required.", recipe)
        evidence = assess_foodcom_nutrient_evidence(calculation)
        if evidence["nutritional_status"] != "REQUIRES_OPTIMISATION":
            return self._result("MISSING_NUTRIENT_DATA", evidence["nutritional_reason"], recipe,
                                missing_or_unresolved_ingredients=evidence["missing_or_unresolved_ingredients"])
        count = _number(serving_count, positive=True)
        if count is None or count > 20 or int(count) != count:
            return self._result("OPTIMISATION_ERROR", "Serving count must be a whole number from 1 to 20.", recipe)
        active = RecipeNutrientConstraintAdapter._active_bounds(constraints)
        if not active and not allow_unconstrained:
            return self._result("MISSING_CONSTRAINTS", "At least one explicit numeric dietary limit is required.", recipe)
        for bound in active:
            if (bound["nutrient_key"] not in NUTRIENTS or bound["operator"] not in {"<=", ">="}
                    or _number(bound["threshold"]) is None
                    or str(bound.get("unit") or NUTRIENT_UNITS.get(bound["nutrient_key"])).casefold()
                    != NUTRIENT_UNITS[bound["nutrient_key"]]
                    or bound.get("scope") not in {"meal", "remaining"}):
                return self._result("OPTIMISATION_ERROR", "A dietary limit has an unsupported nutrient, unit, scope, or value.", recipe)

        rows = calculation["ingredients"]
        if not isinstance(role_bounds, dict):
            return self._result("MISSING_QUANTITY_BOUNDS", "Role bounds must be a mapping with stated provenance.", recipe)
        for role_name, configured in role_bounds.items():
            if (role_name not in set(ROLE_NAMES.values()) or not isinstance(configured, dict)
                    or _number(configured.get("min_g"), positive=True) is None
                    or _number(configured.get("max_g"), positive=True) is None
                    or not str(configured.get("basis") or "").strip()):
                return self._result("MISSING_QUANTITY_BOUNDS", "A role bound is invalid or lacks its stated basis.", recipe)
        ids = [row["mapping"]["fdc_id"] for row in rows]
        records = {int(item["food"]["fdc_id"]): item["food"]
                   for item in self.kg.get_usda_food_records_by_fdc_ids(ids)}
        missing_bounds, ingredients, assumptions = [], [], []
        for row in rows:
            name = row["ingredient_name"]
            fdc_id = int(row["mapping"]["fdc_id"])
            food = records.get(fdc_id) or {}
            role = ROLE_NAMES.get(str(food.get("role") or food.get("ingredient_type") or "").casefold())
            graph_min = _number(food.get("min_amount_g"), positive=True)
            graph_max = _number(food.get("max_amount_g"), positive=True)
            graph_basis = str(food.get("amount_range_basis") or "").strip()
            configured = role_bounds.get(role) if role else None
            role_min = _number(configured.get("min_g"), positive=True) if isinstance(configured, dict) else None
            role_max = _number(configured.get("max_g"), positive=True) if isinstance(configured, dict) else None
            role_basis = str(configured.get("basis") or "").strip() if isinstance(configured, dict) else ""
            if role is None or ((graph_min is None or graph_max is None or not graph_basis)
                                and (role_min is None or role_max is None or not role_basis)):
                missing_bounds.append({"ingredient": name, "fdc_id": fdc_id,
                                       "reason": "Known role and sourced positive gram bounds are required."})
                continue
            lower = max(value for value in (graph_min if graph_basis else None, role_min if role_basis else None)
                        if value is not None)
            upper = min(value for value in (graph_max if graph_basis else None, role_max if role_basis else None)
                        if value is not None)
            if lower > upper:
                return self._result("INFEASIBLE_CONSTRAINTS", f"Quantity bounds conflict for {name}.", recipe)
            sources = [basis for basis in (graph_basis if graph_min is not None or graph_max is not None else "",
                                          role_basis if role_min is not None and role_max is not None else "") if basis]
            assumptions.extend(source for source in sources if source not in assumptions)
            profile = {key: row["nutrient_measurements_per_100g"][key]["value_per_100g"]
                       for key in NUTRIENTS}
            ingredients.append({"name": name, "fdc_id": fdc_id, "role": role,
                                "min_g": lower, "max_g": upper, "bounds_basis": sources,
                                "nutrients_per_100g": profile})
        if missing_bounds:
            return self._result("MISSING_QUANTITY_BOUNDS", "One or more ingredients lack sourced positive gram bounds or reliable roles.",
                                recipe, missing_quantity_bounds=missing_bounds)
        weight_min = weight_max = None
        if total_weight_bounds is not None:
            if not isinstance(total_weight_bounds, dict):
                return self._result("MISSING_QUANTITY_BOUNDS", "Total meal weight bounds must be a mapping.", recipe)
            weight_min = _number(total_weight_bounds.get("min_g"), positive=True)
            weight_max = _number(total_weight_bounds.get("max_g"), positive=True)
            basis = str(total_weight_bounds.get("basis") or "").strip()
            if weight_min is None or weight_max is None or weight_min > weight_max or not basis:
                return self._result("MISSING_QUANTITY_BOUNDS", "Total meal weight bounds require positive min_g, max_g, and a stated basis.", recipe)
            assumptions.append(basis)

        minima = [item["min_g"] for item in ingredients]
        matrix, limits = [], []
        for index, item in enumerate(ingredients):
            vector = [0.0] * len(ingredients)
            vector[index] = 1.0
            matrix.append(vector)
            limits.append(item["max_g"] - item["min_g"])
        for bound in active:
            key = bound["nutrient_key"]
            multiplier = count if bound["scope"] == "remaining" else 1.0
            coefficients = [item["nutrients_per_100g"][key] * multiplier / 100.0
                            for item in ingredients]
            threshold = float(bound["threshold"])
            sign = 1.0 if bound["operator"] == "<=" else -1.0
            matrix.append([sign * value for value in coefficients])
            limits.append(sign * (threshold - sum(value * minimum for value, minimum
                                                   in zip(coefficients, minima))))
        if weight_max is not None:
            matrix.append([1.0] * len(ingredients))
            limits.append(weight_max - sum(minima))
            matrix.append([-1.0] * len(ingredients))
            limits.append(sum(minima) - weight_min)
        for rule in culinary_linear_constraints:
            if not isinstance(rule, dict) or not str(rule.get("basis") or "").strip():
                return self._result("OPTIMISATION_ERROR", "A culinary constraint lacks its stated basis.", recipe)
            coefficients = rule.get("coefficients")
            limit = _number(rule.get("limit"))
            operator = rule.get("operator")
            if (not isinstance(coefficients, list) or len(coefficients) != len(ingredients)
                    or any(not isinstance(value, (int, float)) or isinstance(value, bool)
                           or not math.isfinite(value) for value in coefficients)
                    or limit is None or operator not in {"<=", ">="}):
                return self._result("OPTIMISATION_ERROR", "A culinary constraint is malformed.", recipe)
            sign = 1.0 if operator == "<=" else -1.0
            matrix.append([sign * value for value in coefficients])
            limits.append(sign * (limit - sum(value * minimum for value, minimum
                                               in zip(coefficients, minima))))
            if rule["basis"] not in assumptions:
                assumptions.append(rule["basis"])
        solution = _FeasibilitySimplex(matrix, limits, len(ingredients)).solve()
        if solution is None:
            return self._result("INFEASIBLE_CONSTRAINTS", "No combination within the stated ingredient and meal bounds meets every numeric limit.",
                                recipe, applied_constraints=constraints, serving_count=int(count))
        raw_amounts = [minimum + max(0.0, value) for minimum, value in zip(minima, solution)]
        engine_records = {str(i): {"name": item["name"], "min_amount_g": item["min_g"],
                                   "max_amount_g": item["max_g"],
                                   "nutrients_per_100g": item["nutrients_per_100g"]}
                          for i, item in enumerate(ingredients)}
        engine = ConstraintEngine(engine_records)
        def verified_amounts(raw):
            """Accept only a full combination checked by the existing engine."""
            for places in (10, 9, 8, 7, 6, 5, 4, 3):
                candidate = [round(amount, places) for amount in raw]
                evaluation = check(candidate)
                if evaluation is not None:
                    return candidate, evaluation
            return None, None

        def check(candidate):
            if (any(amount < item["min_g"] or amount > item["max_g"]
                    for amount, item in zip(candidate, ingredients))
                    or (weight_min is not None and
                        not weight_min <= sum(candidate) <= weight_max)):
                return None
            if any((sum(value * amount for value, amount in zip(rule["coefficients"], candidate))
                    > rule["limit"] + 1e-8 if rule["operator"] == "<=" else
                    sum(value * amount for value, amount in zip(rule["coefficients"], candidate))
                    < rule["limit"] - 1e-8) for rule in culinary_linear_constraints):
                return None
            evaluation = engine.evaluate(
                [(str(i), amount) for i, amount in enumerate(candidate)], constraints,
                servings=int(count),
            )
            if evaluation["passed"] is True and not evaluation["not_evaluated"]:
                return evaluation
            return None

        amounts, evaluation = verified_amounts(raw_amounts)
        if amounts is None:
            return self._result("OPTIMISATION_ERROR", "The constraint engine could not verify the computed combination.", recipe)
        if recommended is not None:
            prior = recommended.get("ingredients") or []
            if (recommended.get("optimisation_status") != "OPTIMISED"
                    or len(prior) != len(ingredients)
                    or any(row.get("fdc_id") != item["fdc_id"] or row.get("name") != item["name"]
                           for row, item in zip(prior, ingredients))):
                return self._result("OPTIMISATION_ERROR", "The recommended combination has different ingredient identities.", recipe)
            retained = [row.get("quantity_g") for row in prior]
            if any(_number(amount, positive=True) is None for amount in retained):
                return self._result("OPTIMISATION_ERROR", "The recommended combination has invalid gram weights.", recipe)
            retained_evaluation = check(retained)
            if retained_evaluation is None:
                return self._result("OPTIMISATION_ERROR", "The recommended combination does not meet every range constraint.", recipe)
            amounts, evaluation = retained, retained_evaluation
        totals = {key: evaluation["totals"][key] for key in NUTRIENTS}
        result = self._result(
            "OPTIMISED", ("One constructed ingredient combination meets the supplied numeric limits."
                          if active else "One bounded ingredient combination was constructed; no numeric nutrient limits were supplied."), recipe,
            ingredients=[{"name": item["name"], "fdc_id": item["fdc_id"], "role": item["role"],
                          "quantity_g": amount, "minimum_g": item["min_g"],
                          "maximum_g": item["max_g"], "bounds_basis": item["bounds_basis"]}
                         for item, amount in zip(ingredients, amounts)],
            nutrient_totals=totals, applied_constraints=constraints,
            serving_count=int(count), serving_basis="one newly constructed meal serving",
            total_meal_weight_g=sum(amounts), assumptions=assumptions,
            limitations=["Prototype or user-supplied quantity bounds are not validated culinary standards.",
                         "Passing numeric limits does not establish medical or culinary safety.",
                         "Food.com source quantities were not used."],
        )
        if recommended is not None:
            ranges = []
            for index, item in enumerate(ingredients):
                witnesses = []
                for direction in (-1.0, 1.0):
                    objective = [0.0] * len(ingredients)
                    objective[index] = direction
                    endpoint = _FeasibilitySimplex(matrix, limits, len(ingredients), objective).solve()
                    if endpoint is None:
                        return self._result("OPTIMISATION_ERROR", "An endpoint became infeasible unexpectedly.", recipe)
                    raw_endpoint = [minimum + max(0.0, value)
                                    for minimum, value in zip(minima, endpoint)]
                    witness, verified = verified_amounts(raw_endpoint)
                    if witness is None:
                        # Move minimally into the segment between two feasible
                        # full combinations if decimal rounding hits a boundary.
                        for fraction in (1e-9, 1e-8, 1e-7, 1e-6, 1e-5, 1e-4):
                            interior = [(1 - fraction) * end + fraction * start
                                        for end, start in zip(raw_endpoint, amounts)]
                            witness, verified = verified_amounts(interior)
                            if witness is not None:
                                break
                    if witness is None or verified is None:
                        return self._result("OPTIMISATION_ERROR", "The constraint engine could not verify a range endpoint.", recipe)
                    if (direction < 0 and witness[index] > amounts[index]
                            or direction > 0 and witness[index] < amounts[index]):
                        witness = list(amounts)
                    witnesses.append(witness)
                ranges.append({
                    "name": item["name"], "fdc_id": item["fdc_id"], "role": item["role"],
                    "recommended_g": amounts[index],
                    "minimum_feasible_g": witnesses[0][index],
                    "maximum_feasible_g": witnesses[1][index],
                    "bounds_basis": item["bounds_basis"],
                    "minimum_witness_g": witnesses[0], "maximum_witness_g": witnesses[1],
                })
            result["quantity_ranges"] = {
                "status": "FEASIBLE_RANGES", "reason": "Conditional endpoints and one joint-feasible recommendation were verified.",
                "recipe_id": recipe.get("id"), "name": recipe.get("name"),
                "serving_count": int(count), "serving_basis": result["serving_basis"],
                "recommended_ingredients": result["ingredients"],
                "ingredient_ranges": ranges, "nutrient_totals": totals,
                "total_meal_weight_g": sum(amounts),
                "applied_nutritional_constraints": constraints,
                "applied_culinary_constraints": culinary_linear_constraints,
                "total_meal_weight_bounds": total_weight_bounds,
                "assumptions": assumptions,
                "conditional_range_explanation": (
                    "Each endpoint is feasible with its own adjustment of the other ingredients. "
                    "Minimum and maximum values cannot necessarily be combined arbitrarily."
                ),
                "limitations": result["limitations"] + [
                    "Reported endpoints are verified decimal gram values and may lie slightly inside the exact mathematical extrema."
                ],
            }
        return result


__all__ = ["IngredientQuantityOptimiser"]

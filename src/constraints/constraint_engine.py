import math

from src.nutrition import NUTRIENT_ALIASES, NUTRIENT_UNITS, normalize_nutrient_name


class ConstraintEngine:
    """Deterministic arithmetic; only explicit meal/remaining budgets are meal filters."""

    NUTRIENTS = NUTRIENT_UNITS
    ALIASES = NUTRIENT_ALIASES

    def __init__(self, ingredients):
        self.ingredients = ingredients

    def nutrient_totals(self, recipe, multiplier=1.0):
        totals = {name: 0.0 for name in self.NUTRIENTS}
        available = {name: True for name in self.NUTRIENTS}
        for iid, amount_g in recipe:
            data = self.ingredients.get(iid)
            if data is None:
                for nutrient in self.NUTRIENTS:
                    available[nutrient] = False
                continue
            nutrient_data = data.get("nutrients_per_100g", {})
            for nutrient in self.NUTRIENTS:
                value = nutrient_data.get(nutrient)
                if value is None:
                    available[nutrient] = False
                elif available[nutrient]:
                    totals[nutrient] += value * amount_g * multiplier / 100.0
        return {name: value for name, value in totals.items() if available[name]}

    @staticmethod
    def replace_ingredient(recipe, source_id, replacement_id, replacement_amount_g):
        """Return a recipe with every source occurrence replaced by one amount."""
        replaced, variant = False, []
        for ingredient_id, amount_g in recipe:
            if ingredient_id == source_id:
                if not replaced:
                    variant.append((replacement_id, replacement_amount_g))
                    replaced = True
            else:
                variant.append((ingredient_id, amount_g))
        return variant if replaced else None

    def find_feasible_substitution_amount(
        self, recipe, source_id, replacement_id, constraints, servings, source_amount_g,
    ):
        """Find a replacement portion that passes every entered numeric meal rule.

        The feasible interval is derived from the same nutrient records and rule
        scopes used by ``evaluate``; the candidate is then re-evaluated by that
        method before being reported as feasible.
        """
        replacement = self.ingredients.get(replacement_id)
        if replacement is None:
            return {
                "status": "not_verifiable", "reason": "The replacement ingredient record is missing.",
                "amount_g": None, "recipe": None, "evaluation": None,
            }
        minimum_amount = replacement.get("min_amount_g")
        maximum_amount = replacement.get("max_amount_g")
        if minimum_amount is None or maximum_amount is None:
            return {
                "status": "not_verifiable",
                "reason": "The substitute has no recorded prototype portion range, so an adjusted amount cannot be validated.",
                "amount_g": None, "recipe": None, "evaluation": None,
            }

        active_rules = []
        for name, rule in constraints.items():
            maximum = rule.get("max", rule.get("max_value"))
            minimum = rule.get("min", rule.get("min_value"))
            if maximum is None and minimum is None:
                continue
            nutrient = normalize_nutrient_name(name)
            if nutrient not in self.NUTRIENTS:
                return {
                    "status": "not_verifiable", "reason": f"The {name} limit is unsupported by this nutrient database.",
                    "amount_g": None, "recipe": None, "evaluation": None,
                }
            scope = rule.get("scope", "meal")
            if scope == "day":
                return {
                    "status": "not_verifiable", "reason": f"The {name} value is daily guidance, not an applied meal limit.",
                    "amount_g": None, "recipe": None, "evaluation": None,
                }
            active_rules.append((name, nutrient, maximum, minimum, servings if scope == "remaining" else 1))

        if not active_rules:
            return {
                "status": "not_verifiable", "reason": "No numeric meal or remaining-intake limit was provided.",
                "amount_g": None, "recipe": None, "evaluation": None,
            }

        lower, upper = float(minimum_amount), float(maximum_amount)
        replacement_nutrients = replacement.get("nutrients_per_100g", {})
        for name, nutrient, maximum, minimum, multiplier in active_rules:
            replacement_value = replacement_nutrients.get(nutrient)
            if replacement_value is None:
                return {
                    "status": "not_verifiable", "reason": f"The replacement has no recorded {name} value.",
                    "amount_g": None, "recipe": None, "evaluation": None,
                }

            other_total = 0.0
            for ingredient_id, amount_g in recipe:
                if ingredient_id == source_id:
                    continue
                ingredient = self.ingredients.get(ingredient_id)
                value = ingredient.get("nutrients_per_100g", {}).get(nutrient) if ingredient else None
                if value is None:
                    return {
                        "status": "not_verifiable",
                        "reason": f"A recipe ingredient has no recorded {name} value.",
                        "amount_g": None, "recipe": None, "evaluation": None,
                    }
                other_total += value * amount_g / 100.0 * multiplier

            rate_per_gram = replacement_value / 100.0 * multiplier
            if maximum is not None:
                remaining = float(maximum) - other_total
                if remaining < -1e-9:
                    return {
                        "status": "impossible",
                        "reason": f"The other recipe ingredients already exceed the {name} limit.",
                        "amount_g": None, "recipe": None, "evaluation": None,
                    }
                if rate_per_gram > 0:
                    upper = min(upper, remaining / rate_per_gram)
            if minimum is not None and other_total < float(minimum):
                if rate_per_gram <= 0:
                    return {
                        "status": "impossible",
                        "reason": f"The replacement contributes no recorded {name} amount to meet the minimum.",
                        "amount_g": None, "recipe": None, "evaluation": None,
                    }
                lower = max(lower, (float(minimum) - other_total) / rate_per_gram)

        if upper + 1e-9 < lower:
            return {
                "status": "impossible",
                "reason": "No portion within the substitute's prototype range can meet all entered limits.",
                "amount_g": None, "recipe": None, "evaluation": None,
            }

        # Use hundredth-gram increments while preserving both inclusive bounds.
        rounded_lower = math.ceil((lower - 1e-10) * 100.0) / 100.0
        rounded_upper = math.floor((upper + 1e-10) * 100.0) / 100.0
        if rounded_lower > rounded_upper:
            return {
                "status": "impossible",
                "reason": "No portion within the substitute's prototype range can meet all entered limits.",
                "amount_g": None, "recipe": None, "evaluation": None,
            }
        amount_g = min(max(float(source_amount_g), rounded_lower), rounded_upper)
        amount_g = round(amount_g, 2)

        candidate = self.replace_ingredient(recipe, source_id, replacement_id, amount_g)
        if candidate is None:
            return {
                "status": "not_verifiable", "reason": "The source ingredient was not present in the retrieved recipe.",
                "amount_g": None, "recipe": None, "evaluation": None,
            }
        evaluation = self.evaluate(candidate, constraints, servings=servings)
        if evaluation["passed"] is True and not evaluation["not_evaluated"]:
            return {
                "status": "feasible", "reason": None, "amount_g": amount_g,
                "recipe": candidate, "evaluation": evaluation,
            }
        status = "not_verifiable" if evaluation["not_evaluated"] else "impossible"
        reason = "; ".join(evaluation["not_evaluated"] or evaluation["reasons"])
        return {
            "status": status, "reason": reason or "The adjusted portion did not pass the entered limits.",
            "amount_g": None, "recipe": candidate, "evaluation": evaluation,
        }

    def evaluate(self, recipe, constraints, servings=1):
        reasons, not_evaluated = [], []
        unverifiable_active_rule = False
        for iid, amount_g in recipe:
            data = self.ingredients.get(iid)
            if data is None:
                not_evaluated.append(f"{iid}: ingredient record missing; nutrient totals unavailable")
                continue
            minimum_amount, maximum_amount = data.get("min_amount_g"), data.get("max_amount_g")
            outside_minimum = minimum_amount is not None and amount_g < minimum_amount
            outside_maximum = maximum_amount is not None and amount_g > maximum_amount
            if outside_minimum or outside_maximum:
                reasons.append(
                    f"{data.get('name', iid)}: {amount_g:g} g outside plausible demo range "
                    f"{minimum_amount if minimum_amount is not None else 'unspecified'}-"
                    f"{maximum_amount if maximum_amount is not None else 'unspecified'} g per serving"
                )
        totals = self.nutrient_totals(recipe)
        # The serving-one values are identical, but keep separate dictionaries
        # in the response so callers can use either field independently.
        totals_for_servings = (dict(totals) if servings == 1 else
                               self.nutrient_totals(recipe, multiplier=servings))

        for name, rule in constraints.items():
            key = normalize_nutrient_name(name)
            if key not in self.NUTRIENTS:
                not_evaluated.append(f"{name}: qualitative or unsupported nutrient; human review required")
                continue
            scope = rule.get("scope", "meal")
            if scope == "day":
                not_evaluated.append(f"{name}: daily guideline is not treated as a per-meal limit")
                continue
            if key not in totals:
                not_evaluated.append(f"{name}: data missing for one or more ingredients")
                if rule.get("max", rule.get("max_value")) is not None or rule.get("min", rule.get("min_value")) is not None:
                    unverifiable_active_rule = True
                continue
            observed = totals[key]
            if scope == "remaining":
                observed = totals_for_servings[key]
            limit = rule.get("max", rule.get("max_value"))
            minimum = rule.get("min", rule.get("min_value"))
            if limit is not None and observed > limit:
                reasons.append(f"{name}: {observed:.1f} > meal/remaining budget {limit:g} {rule.get('unit', self.NUTRIENTS[key])}")
            if minimum is not None and observed < minimum:
                reasons.append(f"{name}: {observed:.1f} < meal minimum {minimum:g} {rule.get('unit', self.NUTRIENTS[key])}")

        return {
            # Missing data cannot count as a pass against an explicit numeric
            # meal budget. Daily context and qualitative guidance stay unscored.
            "passed": False if reasons else (None if unverifiable_active_rule else True),
            "totals": totals,
            "totals_for_servings": totals_for_servings,
            "reasons": reasons,
            "not_evaluated": not_evaluated,
        }

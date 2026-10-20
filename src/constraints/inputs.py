"""Validation and normalization for user-supplied nutrient budgets."""
import math

from src.nutrition import NUTRIENT_UNITS, normalize_nutrient_name


def parse_budget(name, value, scope):
    """Normalize one numeric budget into the shape accepted by ConstraintEngine."""
    label = str(name).strip()
    if not label:
        raise ValueError("Enter a nutrient name.")
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"Enter a number for {label}.") from None
    if not math.isfinite(amount) or amount < 0:
        raise ValueError(f"{label} must be a finite, non-negative number.")

    nutrient = normalize_nutrient_name(label)
    return nutrient, {
        "max": amount,
        "scope": scope,
        "unit": NUTRIENT_UNITS.get(nutrient, "g"),
    }


def parse_budget_expression(expression, scope):
    """Parse the CLI form ``nutrient=value`` and return a normalized budget."""
    try:
        name, value = expression.split("=", 1)
    except (AttributeError, ValueError):
        raise ValueError("Use nutrient=value, for example sodium=700.") from None
    return parse_budget(name, value, scope)


def parse_budget_mapping(values, scope):
    """Parse a web form mapping, ignoring blank optional fields."""
    if not isinstance(values, dict):
        raise ValueError("Nutrient limits must be provided as an object.")
    budgets = {}
    for name, value in values.items():
        if value in (None, ""):
            continue
        if isinstance(value, dict):
            rule = {"scope": scope, "unit": NUTRIENT_UNITS.get(normalize_nutrient_name(name), "g")}
            for direction in ("min", "max"):
                if value.get(direction) not in (None, ""):
                    _, parsed = parse_budget(name, value[direction], scope)
                    rule[direction] = parsed["max"]
            if not any(key in rule for key in ("min", "max")):
                continue
            if "min" in rule and "max" in rule and rule["min"] > rule["max"]:
                raise ValueError(f"{name} minimum cannot exceed its maximum.")
            budgets[normalize_nutrient_name(name)] = rule
            continue
        nutrient, rule = parse_budget(name, value, scope)
        budgets[nutrient] = rule
    return budgets

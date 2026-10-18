import re

from src.nutrition import NUTRIENT_UNITS


class GroundedRecipeGenerator:
    """Template construction keeps quantities and ingredient membership deterministic."""

    def generate_method(self, query, recipe, ingredients, recipe_name):
        return (
            f"{recipe_name}: Cook the listed ingredients thoroughly using a suitable cooking method. "
            "Combine and serve. No additional ingredients or amounts are included."
        )

    def respond_missing_data(self, query, missing_data):
        if missing_data["kind"] == "unsupported_recipe":
            choices = "; ".join(missing_data.get("supported_recipes", []))
            return (f"I don't have recipe data for that dish, so I can't calculate its nutrients. "
                    f"Supported dishes in this prototype: {choices}.")
        missing = ", ".join(missing_data.get("missing_ingredients", []))
        return (f"I found {missing_data.get('recipe_name', 'the recipe')}, but nutrition data is missing for: "
                f"{missing}. I can't verify nutrient totals or dietary constraints until those ingredients "
                "have sourced nutrient records.")

    def generate(self, query, recipe, totals, ingredients, servings=1, recipe_data=None,
                 method_steps=None):
        labels = [f"- {ingredients[iid]['name']}: {amount:g} g" for iid, amount in recipe]
        if isinstance(method_steps, str):
            method_steps = method_steps.splitlines()
        method = list(method_steps or [
            "Prepare the listed ingredients using the cooking method appropriate to the dish.",
            "Combine and serve. Do not add unlisted ingredients or change the listed amounts.",
        ])
        lines = [
            f"{recipe_data['name'] if recipe_data else 'Recipe'} — {servings} serving(s)",
            "Ingredients (amounts are per serving):", *labels, "", "Method:",
        ]
        lines.extend(f"{index}. {step}" for index, step in enumerate(method, 1))
        lines.extend(["", "Calculated nutrients per serving:"])
        lines.extend(f"- {key.replace('_', ' ')}: {value:.1f} {NUTRIENT_UNITS[key]}"
                     for key, value in totals.items())
        return "\n".join(lines)


def check_recipe_faithfulness(text, recipe, ingredients):
    """Check exact list membership/amounts and scan method text for additions."""
    expected = [(ingredients[iid]["name"], float(amount)) for iid, amount in recipe]
    issues = []
    ingredient_section = text.split("Ingredients (amounts are per serving):", 1)
    if len(ingredient_section) != 2 or "\nMethod:" not in ingredient_section[1]:
        return {"passed": False, "issues": ["Could not parse the rendered ingredient list"]}
    ingredient_section = ingredient_section[1].split("\nMethod:", 1)[0]
    listed = []
    for line in ingredient_section.splitlines():
        match = re.match(r"- (.+): ([0-9.]+) g$", line.strip())
        if match:
            listed.append((match.group(1), float(match.group(2))))
    if listed != expected:
        issues.append(f"Rendered ingredients/amounts {listed!r} do not match validated candidate {expected!r}")

    method_part = text.split("\nMethod:\n", 1)
    method = method_part[1].split("\nCalculated nutrients per serving:", 1)[0] if len(method_part) == 2 else ""
    allowed = {name.casefold() for name, _ in expected}
    for iid, data in ingredients.items():
        if data["name"].casefold() in method.casefold() and data["name"].casefold() not in allowed:
            issues.append(f"Method mentions unvalidated ingredient: {data['name']}")
        for alias in data.get("aliases", []):
            alias = alias.casefold()
            if alias and alias in method.casefold() and alias not in allowed:
                issues.append(f"Method mentions unvalidated ingredient alias: {alias}")
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:g|mg|ml|tsp|tbsp|cups?)\b", method, re.I):
        issues.append("Method contains a quantity not present in the validated ingredient list")
    return {"passed": not issues, "issues": issues}


def check_method_faithfulness(text, recipe, ingredients):
    """Reject generated methods that add catalog foods or ingredient amounts."""
    method = str(text or "")
    selected_ids = {ingredient_id for ingredient_id, _ in recipe}
    allowed_labels = set()
    for ingredient_id in selected_ids:
        data = ingredients.get(ingredient_id, {})
        allowed_labels.update(
            str(label).strip().casefold()
            for label in [data.get("name", ""), *(data.get("aliases") or [])]
            if str(label).strip()
        )
    issues = []
    if not method.strip():
        issues.append("Method is empty")
    for ingredient_id, data in ingredients.items():
        if ingredient_id in selected_ids:
            continue
        labels = [data.get("name", ""), *(data.get("aliases") or [])]
        for label in labels:
            label = str(label).strip()
            if len(label) < 3 or label.casefold() in allowed_labels:
                continue
            if re.search(r"(?<!\w)" + re.escape(label) + r"(?!\w)", method, flags=re.I):
                issues.append(f"Method mentions unvalidated ingredient: {label}")
                break
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:g|mg|kg|oz|lb|ml|l|tsp|tbsp|cups?)\b", method, re.I):
        issues.append("Method contains an ingredient amount that was not in the validated composition")
    return {"passed": not issues, "issues": issues}

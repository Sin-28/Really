def resolve_reference_rules(conditions, profiles):
    """Merge condition references while keeping patient-specific renal K priority."""
    rules = [item for condition in conditions for item in profiles[condition]["reference"]]
    conflicts = []
    if "haemodialysis" in conditions and "hypertension" in conditions:
        rules = [
            item for item in rules
            if not (item["nutrient"] == "potassium_mg" and item["source"] == "NHLBI DASH Eating Plan")
        ]
        conflicts.append("Renal individualized potassium guidance takes precedence; generic DASH potassium target suppressed.")
    # Duplicate sources for the same nutrient/scope are shown once; retain their source trail.
    unique, seen = [], set()
    for item in rules:
        key = (item["nutrient"], item["type"], item.get("min"), item.get("max"), item["scope"])
        if key not in seen:
            unique.append(item)
            seen.add(key)
    return unique, conflicts

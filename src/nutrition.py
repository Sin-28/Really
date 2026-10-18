"""Shared nutrient names and units used by calculation and presentation layers."""

NUTRIENT_UNITS = {
    "energy_kcal": "kcal",
    "sodium_mg": "mg",
    "potassium_mg": "mg",
    "phosphorus_mg": "mg",
    "protein_g": "g",
    "carbohydrate_g": "g",
    "fiber_g": "g",
    "added_sugar_g": "g",
    "saturated_fat_g": "g",
}

NUTRIENT_ALIASES = {
    "sodium": "sodium_mg",
    "potassium": "potassium_mg",
    "phosphorus": "phosphorus_mg",
    "protein": "protein_g",
    "carbohydrate": "carbohydrate_g",
    "carbohydrates": "carbohydrate_g",
    "fiber": "fiber_g",
    "fibre": "fiber_g",
    "added sugar": "added_sugar_g",
    "saturated fat": "saturated_fat_g",
    "energy": "energy_kcal",
}

FDC_NUTRIENT_ID_KEYS = {
    "1008": "energy_kcal",
    "1003": "protein_g",
    "1005": "carbohydrate_g",
    "1063": "added_sugar_g",
    "1079": "fiber_g",
    "1091": "phosphorus_mg",
    "1092": "potassium_mg",
    "1093": "sodium_mg",
    "1258": "saturated_fat_g",
}

# Nutrients required by the recipe-level deterministic calculation engine.
# The values in Neo4j's HAS_NUTRIENT.amount relationships are stored on a
# per-100-g basis in the USDA catalog and retain their source units.
PRIMARY_RECIPE_NUTRIENTS = {
    "1093": ("sodium_mg", "mg"),
    "1092": ("potassium_mg", "mg"),
    "1091": ("phosphorus_mg", "mg"),
    "1003": ("protein_g", "g"),
}


def normalize_nutrient_name(name):
    """Return the canonical nutrient key for a display or user-entered name."""
    normalized = str(name).strip().casefold()
    return NUTRIENT_ALIASES.get(normalized, normalized)

"""Legacy exporter for the curated 55-record subset; not used by the Neo4j runtime.

Use scripts/import_foundation_neo4j.py to load the full Foundation Foods release.
"""
import argparse
import json
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
FDC_URL = "https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients"

# Culinary portion ranges are prototype controls. They are not USDA values.
CURATED = {
    331960: ("foundation_chicken_breast_braised", "Chicken breast, cooked", "protein", 80, 30, 180, ["chicken", "cooked chicken", "chicken breast"]),
    2512380: ("foundation_brown_rice_raw", "Brown rice, raw", "grain", 27, 10, 100, ["brown rice", "raw brown rice"]),
    2512381: ("foundation_white_rice_raw", "White rice, raw", "grain", 27, 10, 100, ["white rice", "raw white rice"]),
    2685572: ("foundation_bok_choy_raw", "Bok choy, raw", "vegetable", 60, 10, 180, ["bok choy", "xiao bai cai"]),
    2727585: ("foundation_scallion_raw", "Scallion, raw", "aromatic", 5, 1, 30, ["scallion", "spring onion", "green onion"]),
    790646: ("foundation_yellow_onion_raw", "Yellow onion, raw", "vegetable", 30, 5, 150, ["onion", "yellow onion"]),
    748967: ("foundation_whole_egg", "Whole egg", "protein", 50, 25, 120, ["egg", "whole egg"]),
    747447: ("foundation_broccoli_raw", "Broccoli, raw", "vegetable", 60, 10, 180, ["broccoli"]),
    321360: ("foundation_grape_tomatoes_raw", "Grape tomatoes, raw", "vegetable", 60, 10, 180, ["tomato", "tomatoes", "grape tomato"]),
    2346396: ("foundation_rolled_oats_dry", "Rolled oats, dry", "grain", 40, 20, 100, ["oats", "rolled oats", "oatmeal"]),
    2684440: ("foundation_sockeye_salmon_raw", "Sockeye salmon, raw", "protein", 90, 30, 200, ["salmon", "sockeye salmon"]),
    2684441: ("foundation_atlantic_salmon_raw", "Atlantic salmon, raw", "protein", 90, 30, 200, ["salmon", "atlantic salmon"]),
    2684442: ("foundation_tilapia_raw", "Tilapia, raw", "protein", 100, 30, 200, ["tilapia", "fish"]),
    2684443: ("foundation_shrimp_raw", "Shrimp, raw", "protein", 80, 20, 180, ["shrimp", "prawn"]),
    334536: ("foundation_restaurant_fried_rice", "Chinese restaurant fried rice, without meat", "prepared_reference", 133, 80, 400, ["fried rice", "restaurant fried rice"]),
    332282: ("foundation_marinara_sauce", "Marinara pasta sauce, ready to serve", "sauce", 50, 5, 200, ["tomato pasta sauce", "marinara sauce"]),
    2346407: ("foundation_green_cabbage_raw", "Green cabbage, raw", "vegetable", 60, 10, 180, ["cabbage", "green cabbage"]),
    2646170: ("foundation_chicken_breast_raw", "Chicken breast, raw", "protein", 100, 30, 250, ["raw chicken", "raw chicken breast"]),
    2685578: ("foundation_canned_tomato_salted", "Canned tomatoes, with salt", "vegetable", 100, 20, 250, ["canned tomatoes"]),
    2685580: ("foundation_tomato_paste_no_salt", "Tomato paste, no salt added", "sauce", 20, 5, 100, ["tomato paste", "no-salt tomato paste"]),
    2685582: ("foundation_tomato_puree", "Tomato puree, canned", "sauce", 50, 5, 200, ["tomato puree", "tomato purée"]),
    2258588: ("foundation_green_bell_pepper_raw", "Green bell pepper, raw", "vegetable", 50, 5, 180, ["green pepper", "green bell pepper"]),
    790577: ("foundation_red_onion_raw", "Red onion, raw", "vegetable", 30, 5, 150, ["red onion"]),
    331897: ("foundation_chicken_drumstick_braised", "Chicken drumstick, cooked", "protein", 80, 30, 180, ["chicken drumstick", "cooked chicken drumstick"]),
    333374: ("foundation_haddock_raw", "Haddock, raw", "protein", 100, 30, 200, ["haddock", "white fish"]),
    2684444: ("foundation_cod_raw", "Atlantic cod, raw", "protein", 100, 30, 200, ["cod", "atlantic cod"]),
    2684445: ("foundation_catfish_raw", "Catfish, raw", "protein", 100, 30, 200, ["catfish"]),
    2684446: ("foundation_crab_lump_pasteurized", "Blue swimming crab lump meat", "protein", 80, 30, 180, ["crab", "crab meat"]),
    323121: ("foundation_beef_frankfurter_unheated", "Beef frankfurter, unheated", "protein", 50, 20, 120, ["frankfurter", "beef hot dog"]),
    746770: ("foundation_cantaloupe_raw", "Cantaloupe, raw", "fruit", 80, 20, 250, ["cantaloupe", "melon"]),
    746771: ("foundation_orange_raw", "Navel orange, raw", "fruit", 130, 30, 250, ["orange", "navel orange"]),
    1999628: ("foundation_shiitake_raw", "Shiitake mushroom, raw", "vegetable", 40, 10, 150, ["shiitake", "shiitake mushroom"]),
    1999629: ("foundation_button_mushroom_raw", "White button mushroom, raw", "vegetable", 50, 10, 180, ["mushroom", "button mushroom"]),
    1999630: ("foundation_soymilk_unsweetened", "Unsweetened soy milk", "beverage", 150, 50, 300, ["soy milk", "soymilk"]),
    1999631: ("foundation_almondmilk_unsweetened", "Unsweetened almond milk", "beverage", 150, 50, 300, ["almond milk", "almondmilk"]),
    1999632: ("foundation_baby_spinach_raw", "Baby spinach, raw", "vegetable", 30, 5, 120, ["spinach", "baby spinach"]),
    1999634: ("foundation_roma_tomato_raw", "Roma tomato, raw", "vegetable", 50, 10, 180, ["roma tomato", "tomato"]),
    2258586: ("foundation_carrot_raw", "Carrot, mature, raw", "vegetable", 40, 10, 150, ["carrot", "carrots"]),
    2258590: ("foundation_red_bell_pepper_raw", "Red bell pepper, raw", "vegetable", 30, 5, 120, ["red pepper", "red bell pepper"]),
    2346388: ("foundation_iceberg_lettuce_raw", "Iceberg lettuce, raw", "vegetable", 50, 10, 150, ["lettuce", "iceberg lettuce"]),
    2346406: ("foundation_cucumber_raw", "Cucumber with peel, raw", "vegetable", 60, 10, 180, ["cucumber"]),
    2346409: ("foundation_strawberries_raw", "Strawberries, raw", "fruit", 50, 10, 180, ["strawberries", "strawberry"]),
    2346411: ("foundation_blueberries_raw", "Blueberries, raw", "fruit", 50, 10, 180, ["blueberries", "blueberry"]),
    2346412: ("foundation_red_grapes_raw", "Red seedless grapes, raw", "fruit", 50, 10, 180, ["grapes", "red grapes"]),
    2644285: ("foundation_black_beans_canned_rinsed", "Black beans, canned, drained and rinsed", "legume", 60, 20, 180, ["black beans", "rinsed black beans"]),
    2644288: ("foundation_chickpeas_canned_rinsed", "Chickpeas, canned, drained and rinsed", "legume", 60, 20, 180, ["chickpeas", "garbanzo beans"]),
    2685573: ("foundation_cauliflower_raw", "Cauliflower, raw", "vegetable", 60, 10, 180, ["cauliflower"]),
    2685575: ("foundation_brussels_sprouts_raw", "Brussels sprouts, raw", "vegetable", 60, 10, 180, ["brussels sprouts"]),
    2685581: ("foundation_crushed_tomatoes_canned", "Crushed tomatoes, canned", "vegetable", 60, 20, 200, ["crushed tomatoes", "tomatoes"]),
    2710819: ("foundation_chia_seeds_dry", "Chia seeds, dry", "seed", 10, 2, 30, ["chia seeds", "chia"]),
    2710824: ("foundation_avocado_raw", "Hass avocado, raw", "fruit", 25, 5, 100, ["avocado"]),
    2710833: ("foundation_mango_raw", "Mango, raw", "fruit", 50, 10, 180, ["mango"]),
    1105314: ("foundation_banana_raw", "Banana, ripe, raw", "fruit", 60, 20, 180, ["banana", "ripe banana"]),
    330137: ("foundation_greek_yogurt_nonfat", "Greek yogurt, plain, nonfat", "dairy", 150, 50, 250, ["plain greek yogurt", "nonfat greek yogurt", "greek yogurt"]),
    334194: ("foundation_tuna_light_canned", "Light tuna, canned in water, drained", "protein", 80, 30, 180, ["tuna", "canned tuna"]),
}

NUTRIENTS = {
    1008: ("energy_kcal", "kcal"),
    1003: ("protein_g", "g"),
    1005: ("carbohydrate_g", "g"),
    1079: ("fiber_g", "g"),
    1091: ("phosphorus_mg", "mg"),
    1092: ("potassium_mg", "mg"),
    1093: ("sodium_mg", "mg"),
    1258: ("saturated_fat_g", "g"),
}


def normalize(food, meta, release):
    ingredient_id, display_name, role, typical, minimum, maximum, aliases = meta
    amounts = {key: None for key, _ in NUTRIENTS.values()}
    units = {key: unit for key, unit in NUTRIENTS.values()}
    for item in food.get("foodNutrients", []):
        nutrient = item.get("nutrient", {})
        mapped = NUTRIENTS.get(nutrient.get("id"))
        if not mapped:
            continue
        key, _unit = mapped
        value = item.get("amount")
        # Retain true zeroes, but do not let anomalous negative computed values
        # become plausible-looking nutrient totals.
        amounts[key] = value if isinstance(value, (int, float)) and value >= 0 else None
    portions = []
    for portion in food.get("foodPortions", [])[:5]:
        measure = portion.get("measureUnit") or {}
        portions.append({
            "description": portion.get("modifier") or portion.get("portionDescription") or measure.get("name"),
            "amount": portion.get("amount"),
            "unit": measure.get("name"),
            "gram_weight": portion.get("gramWeight"),
        })
    return {
        "name": display_name,
        "fdc_description": food.get("description"),
        "aliases": aliases,
        "role": role,
        "nutrients_per_100g": amounts,
        "nutrient_units": units,
        "typical_amount_g": typical,
        "min_amount_g": minimum,
        "max_amount_g": maximum,
        "amount_range_basis": "RenAlly prototype plausibility range; not a clinical recommendation",
        "food_state": "as described in FDC description; see fdc_description",
        "portions": portions,
        "source": {
            "name": "USDA FoodData Central, Foundation Foods",
            "fdc_id": food.get("fdcId"),
            "release_date": release,
            "basis": "per 100 g; nutrient values copied from FoundationFoods JSON amount fields",
            "url": FDC_URL.format(fdc_id=food.get("fdcId")),
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("foundation_json", type=Path, help="USDA FoundationFoods JSON download")
    parser.add_argument("--release", default="2026-04-30")
    parser.add_argument("--output-dir", type=Path, default=DATA_DIR)
    args = parser.parse_args()
    data = json.loads(args.foundation_json.read_text())
    foods = {item.get("fdcId"): item for item in data.get("FoundationFoods", []) if isinstance(item, dict)}
    missing = sorted(set(CURATED) - set(foods))
    if missing:
        raise SystemExit(f"Selected FDC IDs not found in input: {missing}")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    selected = [foods[fdc_id] for fdc_id in CURATED]
    (args.output_dir / "foundation_foods_subset.json").write_text(json.dumps({
        "source_file": args.foundation_json.name,
        "data_type": "Foundation Foods",
        "release_date": args.release,
        "record_count_selected": len(selected),
        "fdc_ids": list(CURATED),
        "records": selected,
    }, ensure_ascii=False, indent=2) + "\n")
    normalized = {
        CURATED[food["fdcId"]][0]: normalize(food, CURATED[food["fdcId"]], args.release)
        for food in selected
    }
    (args.output_dir / "foundation_ingredients.json").write_text(
        json.dumps(normalized, ensure_ascii=False, indent=2) + "\n"
    )
    print(f"Selected {len(selected)} of {len(foods)} Foundation Foods records")
    print(f"Wrote normalized ingredients and source records to {args.output_dir}")


if __name__ == "__main__":
    main()

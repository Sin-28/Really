"""Replace the Food.com recipe catalog in Neo4j from the expanded Kaggle CSV.

Ingredient quantity strings and serving counts are preserved as source text.
Ingredient names are normalized for matching. Quantities remain source metadata.
"""
import argparse
import ast
import csv
import gzip
import hashlib
import json
import math
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.graph.neo4j_graph import load_project_env  # noqa: E402
from src.recipes.ingredient_parser import parse_foodcom_ingredient  # noqa: E402


DATASET_NAME = "Food.com Recipes and Reviews (Kaggle)"
DATASET_URL = "https://www.kaggle.com/datasets/irkaal/foodcom-recipes-and-reviews"
DATASET_VERSION = "irkaal-foodcom-recipes-and-reviews-v4"
BATCH_SIZE = 5000
REFERENCE_BATCH_SIZE = 5000
REQUIRED_COLUMNS = {
    "RecipeId", "Name", "RecipeIngredientParts", "RecipeIngredientQuantities",
    "RecipeServings", "RecipeInstructions",
}


def _list_field(value):
    """Decode a CSV list column, including the source's R c("...") encoding."""
    if isinstance(value, (list, tuple)):
        return list(value)
    if value is None:
        return []
    text = str(value).strip()
    if not text or text.casefold() in {"nan", "na", "null", "none", "character(0)"}:
        return []

    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError):
        parsed = None
    if isinstance(parsed, (list, tuple)):
        return list(parsed)

    match = re.fullmatch(r"c\s*\((.*)\)", text, flags=re.DOTALL)
    if match:
        contents = match.group(1).strip()
        if not contents:
            return []
        try:
            parsed = ast.literal_eval(f"[{contents}]")
        except (ValueError, SyntaxError):
            return []
        if isinstance(parsed, list):
            return parsed
    return []


def _source_strings(values):
    """Preserve each decoded source value without trimming or interpreting it."""
    return [None if value is None else str(value) for value in values]


def _has_complete_quantity_labels(ingredients, quantities):
    missing_values = {"", "nan", "na", "null", "none", "character(0)"}
    return (
        bool(ingredients)
        and len(ingredients) == len(quantities)
        and all(
            quantity is not None and str(quantity).strip().casefold() not in missing_values
            for quantity in quantities
        )
    )


def _optional_source_text(value):
    if value is None:
        return None
    text = str(value)
    return None if text.strip().casefold() in {"", "nan", "na", "null", "none"} else text


def _optional_positive_number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _recipe_row(raw):
    source_id = str(raw.get("RecipeId") or "").strip()
    name = " ".join(str(raw.get("Name") or "").split())
    if not source_id or not name:
        return None

    ingredients = [str(value) for value in _list_field(raw.get("RecipeIngredientParts"))
                   if value is not None and str(value).strip()]
    quantities = _source_strings(_list_field(raw.get("RecipeIngredientQuantities")))
    if not _has_complete_quantity_labels(ingredients, quantities):
        return None
    instructions = [str(value) for value in _list_field(raw.get("RecipeInstructions"))
                    if value is not None and str(value).strip()]
    keywords = [str(value) for value in _list_field(raw.get("Keywords"))
                if value is not None and str(value).strip()]
    description = " ".join(str(raw.get("Description") or "").split())
    category = " ".join(str(raw.get("RecipeCategory") or "").split())
    searchable = " ".join([name, description, category, *keywords, *ingredients])
    servings_raw = _optional_source_text(raw.get("RecipeServings"))
    servings = _optional_positive_number(servings_raw)
    recipe_id = f"foodcom:{source_id}"

    ingredient_items = []
    for position in range(max(len(ingredients), len(quantities))):
        ingredient_name = ingredients[position] if position < len(ingredients) else None
        quantity_raw = quantities[position] if position < len(quantities) else None
        parsed = parse_foodcom_ingredient(ingredient_name, quantity_raw)
        ingredient_items.append({
            "position": position,
            "name": ingredient_name,
            "quantity_raw": quantity_raw,
            "parsed": parsed,
        })

    source = {
        "name": DATASET_NAME,
        "dataset_url": DATASET_URL,
        "source_recipe_id": source_id,
        "license_status": "CC0: Public Domain (Kaggle dataset listing)",
        "nutrition_status": "Not used for RenAlly calculations",
    }
    details = {
        "ingredients": ingredients,
        "ingredient_quantities": quantities,
        "ingredient_items": ingredient_items,
        "servings_raw": servings_raw,
        "servings_numeric": servings,
        "instructions": instructions,
        "recipe_yield": _optional_source_text(raw.get("RecipeYield")),
        "cook_time": _optional_source_text(raw.get("CookTime")),
        "prep_time": _optional_source_text(raw.get("PrepTime")),
        "total_time": _optional_source_text(raw.get("TotalTime")),
    }
    properties = {
        "name": name,
        "description": description,
        "aliases": [],
        "search_text": searchable[:12000],
        "source_dataset": DATASET_NAME,
        "source_id": source_id,
        "source_json": json.dumps(source, ensure_ascii=False, separators=(",", ":")),
        "nutrition_complete": False,
        "nutrition_status": (
            "UNVERIFIED: raw source quantities and ingredient identities are not validated "
            "or normalized for nutrient calculation"
        ),
        "foodcom_tags": keywords,
        "foodcom_ingredient_names": ingredients,
        "foodcom_dataset_version": DATASET_VERSION,
        "foodcom_quantities_complete": True,
        "foodcom_servings_raw": servings_raw,
        "details_json": json.dumps(details, ensure_ascii=False, separators=(",", ":")),
        "servings": servings,
    }

    references = []
    for position, ingredient_name in enumerate(ingredients):
        normalized = " ".join(ingredient_name.casefold().split())
        if not normalized:
            continue
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        references.append({
            "id": f"foodcom-ingredient:{digest}",
            "name": ingredient_name,
            "normalized_name": normalized,
            "search_text": normalized,
            "recipe_id": recipe_id,
            "position": position,
            "quantity_raw": quantities[position] if position < len(quantities) else None,
        })
    return {"id": recipe_id, "properties": properties, "references": references}


def _flush(session, recipes, references):
    if recipes:
        session.run(
            "UNWIND $rows AS row "
            "MATCH (recipe:FoodComRecipe {id: row.id}) "
            "OPTIONAL MATCH (recipe)-[reference:LISTS_INGREDIENT_TEXT]->(:FoodComIngredient) "
            "WITH recipe, collect(reference) AS old_references "
            "FOREACH (reference IN old_references | DELETE reference)",
            rows=[{"id": row["id"]} for row in recipes],
        ).consume()
        session.run(
            "UNWIND $rows AS row "
            "MERGE (recipe:Recipe:FoodComRecipe {id: row.id}) "
            "SET recipe += row.properties",
            rows=recipes,
        ).consume()
    if references:
        for start in range(0, len(references), REFERENCE_BATCH_SIZE):
            session.run(
                "UNWIND $rows AS row "
                "MATCH (recipe:FoodComRecipe {id: row.recipe_id}) "
                "MERGE (ingredient:FoodComIngredient {id: row.id}) "
                "SET ingredient.name = row.name, "
                "ingredient.normalized_name = row.normalized_name, "
                "ingredient.search_text = row.search_text "
                "MERGE (recipe)-[reference:LISTS_INGREDIENT_TEXT {position: row.position}]->(ingredient) "
                "SET reference.quantity_raw = row.quantity_raw",
                rows=references[start:start + REFERENCE_BATCH_SIZE],
            ).consume()


def _delete_obsolete_records(session, batch_size=BATCH_SIZE):
    deleted = 0
    while True:
        result = session.run(
            "MATCH (recipe:FoodComRecipe) "
            "WHERE coalesce(recipe.foodcom_dataset_version, '') <> $dataset_version "
            "OR coalesce(recipe.foodcom_quantities_complete, false) <> true "
            "WITH recipe LIMIT $batch_size "
            "DETACH DELETE recipe RETURN count(*) AS deleted",
            dataset_version=DATASET_VERSION, batch_size=batch_size,
        )
        count = result.single()["deleted"]
        deleted += count
        if count < batch_size:
            break
    session.run(
        "MATCH (ingredient:FoodComIngredient) "
        "WHERE NOT (ingredient)<-[:LISTS_INGREDIENT_TEXT]-(:FoodComRecipe) "
        "DETACH DELETE ingredient"
    ).consume()
    return deleted


def import_recipes(driver, database, csv_path, limit=None, resume=False):
    csv_path = Path(csv_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"Recipe CSV not found: {csv_path}")

    recipes = []
    references = []
    imported = invalid_rows_skipped = incomplete_quantity_rows_skipped = 0
    skipped_existing = rows_seen = 0
    opener = gzip.open if csv_path.suffix.casefold() == ".gz" else open
    with opener(csv_path, "rt", encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        missing = REQUIRED_COLUMNS - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")

        with driver.session(database=database) as session:
            session.run(
                "CREATE CONSTRAINT renally_foodcom_recipe_id IF NOT EXISTS "
                "FOR (recipe:FoodComRecipe) REQUIRE recipe.id IS UNIQUE"
            ).consume()
            session.run(
                "CREATE CONSTRAINT renally_foodcom_ingredient_id IF NOT EXISTS "
                "FOR (ingredient:FoodComIngredient) REQUIRE ingredient.id IS UNIQUE"
            ).consume()
            existing_source_ids = set()
            if resume:
                existing_source_ids = {
                    row["source_id"] for row in session.run(
                        "MATCH (recipe:FoodComRecipe) "
                        "WHERE recipe.foodcom_dataset_version = $dataset_version "
                        "OPTIONAL MATCH (recipe)-[reference:LISTS_INGREDIENT_TEXT]->() "
                        "WITH recipe, count(reference) AS linked "
                        "WHERE linked >= size(coalesce(recipe.foodcom_ingredient_names, [])) "
                        "RETURN recipe.source_id AS source_id",
                        dataset_version=DATASET_VERSION,
                    ) if row.get("source_id") is not None
                }
            for raw in reader:
                rows_seen += 1
                source_id = str(raw.get("RecipeId") or "").strip()
                if resume and source_id in existing_source_ids:
                    skipped_existing += 1
                    continue
                name = " ".join(str(raw.get("Name") or "").split())
                if not source_id or not name:
                    invalid_rows_skipped += 1
                    continue
                row = _recipe_row(raw)
                if row is None:
                    incomplete_quantity_rows_skipped += 1
                    continue
                recipes.append({"id": row["id"], "properties": row["properties"]})
                references.extend(row["references"])
                imported += 1
                if len(recipes) >= BATCH_SIZE:
                    _flush(session, recipes, references)
                    recipes.clear()
                    references.clear()
                    if imported % 10000 == 0:
                        print(f"Imported {imported:,} Food.com recipes…", flush=True)
                if limit is not None and rows_seen >= limit:
                    break
            _flush(session, recipes, references)
            if rows_seen == 0:
                raise ValueError("The Food.com CSV contains a header but no recipe rows.")

            session.run(
                "CREATE FULLTEXT INDEX renally_recipe_search IF NOT EXISTS "
                "FOR (recipe:Recipe) ON EACH [recipe.search_text]"
            ).consume()
            session.run(
                "CREATE FULLTEXT INDEX renally_foodcom_ingredient_search IF NOT EXISTS "
                "FOR (ingredient:FoodComIngredient) ON EACH [ingredient.search_text]"
            ).consume()
            deleted = 0
            if limit is None:
                deleted = _delete_obsolete_records(session)
            session.run(
                "CALL db.index.fulltext.awaitEventuallyConsistentIndexRefresh()"
            ).consume()

    return {"recipes_imported_or_updated": imported,
            "invalid_rows_skipped": invalid_rows_skipped,
            "recipes_without_complete_quantity_labels_skipped": incomplete_quantity_rows_skipped,
            "complete_records_skipped_on_resume": skipped_existing,
            "obsolete_foodcom_recipes_removed": deleted,
            "source_rows_read": rows_seen}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recipes_csv", type=Path,
                        help="Path to expanded recipes.csv or a gzip-compressed recipes.csv.gz")
    parser.add_argument("--database", default=None, help="Neo4j database (defaults to NEO4J_DATABASE or neo4j)")
    parser.add_argument("--limit", type=int, default=None,
                        help="Optional maximum number of source rows; limited imports do not remove old records")
    parser.add_argument("--resume", action="store_true",
                        help="Skip records already imported from this dataset version")
    args = parser.parse_args()
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be a positive integer")

    load_project_env()
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise SystemExit("Install Neo4j support with: pip install -r requirements.txt") from exc

    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("Set NEO4J_PASSWORD in the project .env file before importing.")
    uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    username = os.environ.get("NEO4J_USERNAME", "neo4j")
    database = args.database or os.environ.get("NEO4J_DATABASE", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        summary = import_recipes(driver, database, args.recipes_csv, args.limit, args.resume)
    finally:
        driver.close()
    print("Food.com catalog replacement complete:")
    for key, value in summary.items():
        print(f"- {key.replace('_', ' ')}: {value:,}")
    print("Source quantities and servings remain verbatim; parsed weights are not nutrient-validated.")


if __name__ == "__main__":
    main()

"""Load USDA Foundation Foods and/or SR Legacy into Neo4j."""
import argparse
import json
import os
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.graph.neo4j_graph import load_project_env  # noqa: E402
from src.nutrition import FDC_NUTRIENT_ID_KEYS  # noqa: E402


BATCH_SIZE = 500
FOUNDATION_URL = "https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients"
DATASETS = {
    "foundation_foods": {
        "collection": "FoundationFoods",
        "food_label": "FoundationFood",
        "release_label": "FoundationRelease",
        "release_prefix": "usda-foundation",
        "name": "USDA FoodData Central, Foundation Foods",
        "default_data_type": "Foundation",
        "id_prefix": "fdc:",
    },
    "sr_legacy": {
        "collection": "SRLegacyFoods",
        "food_label": "SRLegacyFood",
        "release_label": "SRLegacyRelease",
        "release_prefix": "usda-sr-legacy",
        "name": "USDA FoodData Central, SR Legacy",
        "default_data_type": "SR Legacy",
        "id_prefix": "fdc-sr-legacy:",
    },
}
SCHEMA = [
    "CREATE CONSTRAINT renally_food_record_id IF NOT EXISTS "
    "FOR (node:FoodRecord) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT renally_foundation_id IF NOT EXISTS "
    "FOR (node:FoundationFood) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT renally_recipe_id IF NOT EXISTS "
    "FOR (node:Recipe) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT renally_type_name IF NOT EXISTS "
    "FOR (node:IngredientType) REQUIRE node.name IS UNIQUE",
    "CREATE CONSTRAINT renally_nutrient_id IF NOT EXISTS "
    "FOR (node:Nutrient) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT renally_release_id IF NOT EXISTS "
    "FOR (node:FoundationRelease) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT renally_food_data_release_id IF NOT EXISTS "
    "FOR (node:FoodDataRelease) REQUIRE node.id IS UNIQUE",
    "CREATE CONSTRAINT renally_unresolved_id IF NOT EXISTS "
    "FOR (node:UnresolvedIngredientReference) REQUIRE node.id IS UNIQUE",
]


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _nutrient_key(nutrient_id, name):
    nutrient_id = str(nutrient_id or "")
    if nutrient_id in FDC_NUTRIENT_ID_KEYS:
        return FDC_NUTRIENT_ID_KEYS[nutrient_id]
    return f"fdc_nutrient_{nutrient_id}" if nutrient_id else ""


def _food_rows(source, curated_by_fdc, release_date, dataset_key="foundation_foods"):
    dataset = DATASETS[dataset_key]
    foods = source.get(dataset["collection"])
    if not isinstance(foods, list):
        raise ValueError(
            f"Input is not a FoodData Central {dataset['collection']} JSON release."
        )
    rows, nutrient_rows, invalid_count = [], [], 0
    for raw in foods:
        if not isinstance(raw, dict) or raw.get("fdcId") is None:
            invalid_count += 1
            continue
        fdc_id = int(raw["fdcId"])
        curated = curated_by_fdc.get(fdc_id, {})
        description = str(raw.get("description") or f"FoodData Central food {fdc_id}").strip()
        category = raw.get("foodCategory") or {}
        category_name = category.get("description") if isinstance(category, dict) else str(category)
        aliases = [str(value).strip() for value in curated.get("aliases", []) if str(value).strip()]
        name = str(curated.get("name") or description)
        ingredient_type = curated.get("ingredient_type") or curated.get("role") or category_name
        properties = {
            "fdc_id": fdc_id,
            "name": name,
            "description": description,
            "food_state": description,
            "data_type": str(raw.get("dataType") or dataset["default_data_type"]),
            "food_class": str(raw.get("foodClass") or ""),
            "publication_date": str(raw.get("publicationDate") or ""),
            "food_category": str(category_name or ""),
            "aliases": aliases,
            "search_text": " ".join([name, description, *aliases]),
            "source_url": FOUNDATION_URL.format(fdc_id=fdc_id),
            "release_date": release_date,
            "nutrition_basis": "per 100 g",
            "food_portions_json": _json(raw.get("foodPortions") or []),
            "food_components_json": _json(raw.get("foodComponents") or []),
            "typical_amount_g": curated.get("typical_amount_g"),
            "min_amount_g": curated.get("min_amount_g"),
            "max_amount_g": curated.get("max_amount_g"),
            "amount_range_basis": curated.get("amount_range_basis", ""),
            "ingredient_type": str(ingredient_type or ""),
            "substitution_group": str(curated.get("substitution_group") or ""),
            "dataset_key": dataset_key,
            "source_dataset": dataset["name"],
        }
        # Neo4j properties cannot contain null values. Missing portion controls
        # remain absent; zero nutrient amounts are stored as explicit values.
        properties = {key: value for key, value in properties.items() if value is not None}
        food_id = f"{dataset['id_prefix']}{fdc_id}"
        rows.append({"id": food_id, "properties": properties})
        for item in raw.get("foodNutrients") or []:
            if not isinstance(item, dict):
                continue
            nutrient = item.get("nutrient") or {}
            nutrient_id = nutrient.get("id")
            amount = item.get("amount")
            if nutrient_id is None or not isinstance(amount, (int, float)) or amount < 0:
                continue
            nutrient_id = str(nutrient_id)
            nutrient_name = str(nutrient.get("name") or f"Nutrient {nutrient_id}")
            unit = str(nutrient.get("unitName") or "")
            nutrient_rows.append({
                "food_id": food_id,
                "nutrient_id": f"fdc-nutrient:{nutrient_id}",
                "fdc_nutrient_id": nutrient_id,
                "name": nutrient_name,
                "unit": unit,
                "key": _nutrient_key(nutrient_id, nutrient_name),
                "amount": float(amount),
            })
    return rows, nutrient_rows, invalid_count


def _load_curated_data(data_dir):
    foundation_ingredients = json.loads((data_dir / "foundation_ingredient_metadata.json").read_text())
    curated_by_fdc = {}
    canonical_to_fdc = {}
    for canonical_id, record in foundation_ingredients.items():
        fdc_id = record.get("fdc_id")
        if fdc_id is None:
            continue
        fdc_id = int(fdc_id)
        curated_by_fdc[fdc_id] = {
            **record,
            "canonical_id": canonical_id,
        }
        canonical_to_fdc[canonical_id] = f"fdc:{fdc_id}"
    return curated_by_fdc, canonical_to_fdc


def _substitution_rows(data_dir, canonical_to_fdc):
    source = json.loads((data_dir / "substitutions.json").read_text())
    rows = []
    for item in source:
        original = canonical_to_fdc.get(item.get("original"))
        substitute = canonical_to_fdc.get(item.get("substitute"))
        if not original or not substitute:
            continue
        rows.append({
            "original": original,
            "substitute": substitute,
            "compatibility": float(item.get("compatibility", 0.0)),
            "evidence": str(item.get("evidence") or "Prototype relationship; requires human review"),
        })
    return rows


def _chunks(values, size=BATCH_SIZE):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _run_batches(session, query, values):
    for batch in _chunks(values):
        session.run(query, rows=batch).consume()


def _skip_space(text, cursor):
    while cursor < len(text) and text[cursor].isspace():
        cursor += 1
    return cursor


def _iter_json_array(path, field_name):
    """Stream one top-level JSON array, retaining only the current JSON value."""
    decoder = json.JSONDecoder()
    with Path(path).open("r", encoding="utf-8") as source:
        buffer, cursor, eof = "", 0, False

        def fill_buffer():
            nonlocal buffer, cursor, eof
            buffer = buffer[cursor:]
            cursor = 0
            chunk = source.read(64 * 1024)
            if chunk:
                buffer += chunk
            else:
                eof = True

        def peek_non_whitespace():
            nonlocal cursor
            while True:
                cursor = _skip_space(buffer, cursor)
                if cursor < len(buffer):
                    return buffer[cursor]
                if eof:
                    return None
                fill_buffer()

        def decode_value():
            nonlocal cursor
            while True:
                if peek_non_whitespace() is None:
                    raise ValueError("Incomplete FoodData Central JSON value.")
                try:
                    value, end = decoder.raw_decode(buffer, cursor)
                except json.JSONDecodeError:
                    if eof:
                        raise
                    fill_buffer()
                    continue
                # A number can end exactly at a chunk boundary. Read ahead so
                # a following digit is not mistaken for another JSON value.
                if end == len(buffer) and not eof:
                    fill_buffer()
                    continue
                cursor = end
                return value

        if peek_non_whitespace() != "{":
            raise ValueError(f"Input is not a JSON object with a {field_name} array.")
        cursor += 1
        while True:
            if peek_non_whitespace() in (None, "}"):
                raise ValueError(f"Input does not contain a {field_name} array.")
            key = decode_value()
            if peek_non_whitespace() != ":":
                raise ValueError("Malformed FoodData Central JSON object.")
            cursor += 1
            if key == field_name:
                if peek_non_whitespace() != "[":
                    raise ValueError(f"FoodData Central {field_name} value is not an array.")
                cursor += 1
                while True:
                    char = peek_non_whitespace()
                    if char is None:
                        raise ValueError(f"FoodData Central {field_name} array is incomplete.")
                    if char == "]":
                        return
                    yield decode_value()
                    char = peek_non_whitespace()
                    if char == ",":
                        cursor += 1
                        continue
                    if char == "]":
                        return
                    raise ValueError(f"Malformed FoodData Central {field_name} array.")
            decode_value()
            if peek_non_whitespace() == ",":
                cursor += 1
                continue
            raise ValueError(f"Input does not contain a {field_name} array.")


def _iter_food_rows(path, curated_by_fdc, release_date, dataset_key):
    collection = DATASETS[dataset_key]["collection"]
    for raw in _iter_json_array(path, collection):
        rows, nutrients, invalid = _food_rows(
            {collection: [raw]}, curated_by_fdc, release_date, dataset_key,
        )
        yield (rows[0] if rows else None), nutrients, invalid


def _wait_for_indexes(session, timeout_seconds=120):
    wanted = {"renally_food_record_search", "renally_recipe_search"}
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        records = session.run(
            "SHOW FULLTEXT INDEXES YIELD name, state "
            "WHERE name IN $names RETURN name, state",
            names=list(wanted),
        )
        states = {record["name"]: record["state"] for record in records}
        if wanted.issubset(states) and all(states[name] == "ONLINE" for name in wanted):
            return
        time.sleep(0.5)
    raise RuntimeError("Neo4j full-text indexes did not become available in time.")


def import_data(driver, database, data_dir, foundation_json=None, release_date="2026-04-30",
                replace_existing=False, sr_legacy_json=None,
                sr_legacy_release_date="2018-04"):
    curated_by_fdc, canonical_to_fdc = _load_curated_data(data_dir)
    imports = []
    invalid_count = 0
    if foundation_json:
        raw_foundation = json.loads(Path(foundation_json).read_text(encoding="utf-8"))
        foods, nutrients, invalid = _food_rows(
            raw_foundation, curated_by_fdc, release_date, "foundation_foods",
        )
        imported_fdc_ids = {row["properties"]["fdc_id"] for row in foods}
        missing_curated_ids = sorted(set(curated_by_fdc) - imported_fdc_ids)
        if missing_curated_ids:
            raise ValueError(
                "The Foundation Foods input does not contain curated FDC IDs used by ingredient metadata: "
                f"{missing_curated_ids}"
            )
        imports.append(("foundation_foods", release_date, foods, nutrients, None))
        invalid_count += invalid
    if sr_legacy_json:
        imports.append(("sr_legacy", sr_legacy_release_date, None, None, Path(sr_legacy_json)))
    if not imports:
        raise ValueError("Provide at least one USDA dataset JSON file to import.")

    substitution_rows = _substitution_rows(data_dir, canonical_to_fdc)

    with driver.session(database=database) as session:
        if replace_existing:
            session.run("MATCH (node:RenAllyData) DETACH DELETE node").consume()
        else:
            # Remove legacy illustrative recipes while keeping the separately
            # imported Food.com catalog and USDA records intact.
            session.run("MATCH (recipe:Recipe:RenAllyData) DETACH DELETE recipe").consume()
            session.run(
                "MATCH (reference:UnresolvedIngredientReference:RenAllyData) "
                "DETACH DELETE reference"
            ).consume()
        for statement in SCHEMA:
            session.run(statement).consume()

        # Existing FoundationFood imports gain a shared label so the runtime
        # catalog can project either USDA dataset without merging their records.
        session.run(
            "MATCH (food:FoundationFood) "
            "SET food:FoodRecord, "
            "food.dataset_key = coalesce(food.dataset_key, 'foundation_foods'), "
            "food.source_dataset = coalesce(food.source_dataset, 'USDA FoodData Central, Foundation Foods'), "
            "food.data_type = coalesce(food.data_type, 'Foundation'), "
            "food.nutrition_basis = coalesce(food.nutrition_basis, 'per 100 g')"
        ).consume()

        record_counts, nutrient_count = {}, 0
        for dataset_key, dataset_release_date, food_rows, nutrient_rows, source_path in imports:
            dataset = DATASETS[dataset_key]
            release_id = f"{dataset['release_prefix']}-{dataset_release_date}"
            release_props = {
                "id": release_id,
                "name": dataset["name"],
                "dataset_key": dataset_key,
                "release_date": dataset_release_date,
                "source_url": "https://fdc.nal.usda.gov/download-datasets/",
            }
            if food_rows is not None:
                release_props["record_count"] = len(food_rows)
            session.run(
                f"MERGE (release:{dataset['release_label']}:FoodDataRelease:RenAllyData {{id: $id}}) "
                "SET release += $properties",
                id=release_id, properties=release_props,
            ).consume()
            food_query = (
                f"UNWIND $rows AS row "
                f"MERGE (food:{dataset['food_label']}:FoodRecord:RenAllyData {{id: row.id}}) "
                "SET food += row.properties "
                "WITH food, row "
                f"MATCH (release:{dataset['release_label']}:FoodDataRelease:RenAllyData {{id: row.release_id}}) "
                "MERGE (food)-[:FROM_RELEASE]->(release)"
            )
            nutrient_query = (
                "UNWIND $rows AS row "
                "MERGE (nutrient:Nutrient:RenAllyData {id: row.nutrient_id}) "
                "SET nutrient.fdc_nutrient_id = row.fdc_nutrient_id, "
                "nutrient.name = row.name, nutrient.unit = row.unit "
                "WITH row, nutrient "
                "MATCH (food:FoodRecord:RenAllyData {id: row.food_id}) "
                "MERGE (food)-[value:HAS_NUTRIENT {fdc_nutrient_id: row.fdc_nutrient_id}]->(nutrient) "
                "SET value.amount = row.amount, value.unit = row.unit, "
                "value.nutrient_key = row.key, value.basis = 'per 100 g'"
            )
            type_query = (
                "UNWIND $rows AS row "
                "MATCH (food:FoodRecord:RenAllyData {id: row.food_id}) "
                "MERGE (type:IngredientType:RenAllyData {name: row.name}) "
                "MERGE (food)-[:IS_A]->(type)"
            )

            if food_rows is not None:
                _run_batches(
                    session, food_query,
                    [{**row, "release_id": release_id} for row in food_rows],
                )
                _run_batches(session, nutrient_query, nutrient_rows)
                type_rows = [
                    {"food_id": row["id"], "name": row["properties"]["ingredient_type"]}
                    for row in food_rows if row["properties"].get("ingredient_type")
                ]
                _run_batches(session, type_query, type_rows)
                record_counts[dataset_key] = len(food_rows)
                nutrient_count += len(nutrient_rows)
            else:
                food_batch, nutrient_batch, type_batch = [], [], []
                dataset_record_count = 0

                def flush_batches():
                    if food_batch:
                        _run_batches(session, food_query, food_batch)
                        food_batch.clear()
                    if nutrient_batch:
                        _run_batches(session, nutrient_query, nutrient_batch)
                        nutrient_batch.clear()
                    if type_batch:
                        _run_batches(session, type_query, type_batch)
                        type_batch.clear()

                for food_row, nutrient_rows_for_food, invalid in _iter_food_rows(
                    source_path, {}, dataset_release_date, dataset_key,
                ):
                    invalid_count += invalid
                    if food_row is None:
                        continue
                    dataset_record_count += 1
                    food_batch.append({**food_row, "release_id": release_id})
                    nutrient_batch.extend(nutrient_rows_for_food)
                    nutrient_count += len(nutrient_rows_for_food)
                    category = food_row["properties"].get("ingredient_type")
                    if category:
                        type_batch.append({"food_id": food_row["id"], "name": category})
                    if len(food_batch) >= BATCH_SIZE:
                        flush_batches()
                flush_batches()
                if dataset_record_count == 0:
                    raise ValueError(f"No valid foods were found in {dataset['collection']}.")
                session.run(
                    "MATCH (release:FoodDataRelease {id: $id}) SET release.record_count = $count",
                    id=release_id, count=dataset_record_count,
                ).consume()
                record_counts[dataset_key] = dataset_record_count

        _run_batches(session,
            "UNWIND $rows AS row "
            "MATCH (substitute:FoundationFood:RenAllyData {id: row.substitute}) "
            "MATCH (original:FoundationFood:RenAllyData {id: row.original}) "
            "MERGE (substitute)-[rel:SUBSTITUTE_FOR]->(original) "
            "SET rel.compatibility = row.compatibility, rel.evidence = row.evidence",
            substitution_rows)

        session.run(
            "CREATE FULLTEXT INDEX renally_food_record_search IF NOT EXISTS "
            "FOR (food:FoodRecord) ON EACH [food.search_text]"
        ).consume()
        session.run(
            "CREATE FULLTEXT INDEX renally_recipe_search IF NOT EXISTS "
            "FOR (recipe:Recipe) ON EACH [recipe.search_text]"
        ).consume()
        _wait_for_indexes(session)

    return {
        "foods": sum(record_counts.values()),
        "foods_by_dataset": record_counts,
        "nutrient_measurements": nutrient_count,
        "substitutions": len(substitution_rows),
        "invalid_source_entries_skipped": invalid_count,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("foundation_json", nargs="?", type=Path,
                        help="USDA FoodData Central FoundationFoods JSON release")
    parser.add_argument("--release", default="2026-04-30", help="Release date represented by the input file")
    parser.add_argument("--sr-legacy-json", type=Path,
                        help="USDA FoodData Central SRLegacyFoods JSON release")
    parser.add_argument("--sr-legacy-release", default="2018-04",
                        help="Release date represented by the SR Legacy input file")
    parser.add_argument("--database", default=None, help="Neo4j database name (defaults to NEO4J_DATABASE or neo4j)")
    parser.add_argument("--replace-existing", action="store_true",
                        help="Delete all RenAllyData nodes before importing the selected USDA inputs")
    args = parser.parse_args()
    if not args.foundation_json and not args.sr_legacy_json:
        parser.error("provide a Foundation Foods JSON path, --sr-legacy-json, or both")
    load_project_env()
    try:
        from neo4j import GraphDatabase
    except ImportError as exc:
        raise SystemExit("Install Neo4j support with: pip install -r requirements.txt") from exc

    uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
    username = os.environ.get("NEO4J_USERNAME", "neo4j")
    password = os.environ.get("NEO4J_PASSWORD")
    if not password:
        raise SystemExit("Set NEO4J_PASSWORD in the project .env file before importing.")
    database = args.database or os.environ.get("NEO4J_DATABASE", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(username, password))
    try:
        driver.verify_connectivity()
        summary = import_data(
            driver, database, PROJECT_ROOT / "data", args.foundation_json,
            args.release, replace_existing=args.replace_existing,
            sr_legacy_json=args.sr_legacy_json,
            sr_legacy_release_date=args.sr_legacy_release,
        )
    finally:
        driver.close()
    print("RenAlly USDA food datasets imported:")
    for name, value in summary.items():
        print(f"- {name.replace('_', ' ')}: {value}")
    print("All imported nutrient measurements retain the USDA per-100-g basis and source FDC IDs.")


if __name__ == "__main__":
    main()

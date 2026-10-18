"""Neo4j-backed RenAlly graph and catalog projection.

Neo4j is the source of truth. The dictionaries exposed here are read-only
application projections used by deterministic nutrition arithmetic and the UI;
retrieval and graph traversal are executed against Neo4j.
"""
import atexit
import json
import os
import re
import shlex
import time
from pathlib import Path

from src.nutrition import FDC_NUTRIENT_ID_KEYS, NUTRIENT_UNITS


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SEARCH_INDEXES = ("renally_food_record_search", "renally_recipe_search")
RECIPE_CATALOG_PAGE_SIZE = 2000


def load_project_env(project_root=PROJECT_ROOT):
    """Load simple KEY=VALUE settings without overriding shell environment."""
    env_path = Path(project_root) / ".env"
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, raw_value = line.split("=", 1)
        key = key.strip()
        if not key or key in os.environ:
            continue
        try:
            parts = shlex.split(raw_value, comments=True, posix=True)
            os.environ[key] = parts[0] if parts else ""
        except ValueError:
            os.environ[key] = raw_value.strip().strip("\"'")


class Neo4jKnowledgeGraph:
    """Neo4j-backed recipes and USDA FoodData Central food catalogs."""

    def __init__(self, data_dir=None, driver=None, database=None):
        load_project_env()
        self.data_dir = Path(data_dir) if data_dir else PROJECT_ROOT / "data"
        self.database = database or os.environ.get("NEO4J_DATABASE", "neo4j")
        self._owns_driver = driver is None
        self._closed = False
        self._ingredient_mapping_cache_ready = False
        self._ingredient_reference_store_ready = False
        if driver is None:
            try:
                from neo4j import GraphDatabase
            except ImportError as exc:
                raise RuntimeError(
                    "Neo4j support is not installed. Run 'pip install -r requirements.txt'."
                ) from exc
            uri = os.environ.get("NEO4J_URI", "bolt://127.0.0.1:7687")
            username = os.environ.get("NEO4J_USERNAME", "neo4j")
            password = os.environ.get("NEO4J_PASSWORD")
            if not password:
                raise RuntimeError(
                    "Set NEO4J_PASSWORD in the project .env file before starting RenAlly."
                )
            driver = GraphDatabase.driver(uri, auth=(username, password))
        self.driver = driver
        try:
            self.driver.verify_connectivity()
            self._require_imported_catalog()
            self._wait_for_search_indexes()
            self._load_catalog()
            self._load_graph_projection()
        except Exception:
            if self._owns_driver:
                self.close()
            raise
        if self._owns_driver:
            atexit.register(self.close)

    def close(self):
        if self._owns_driver and not self._closed:
            self._closed = True
            atexit.unregister(self.close)
            self.driver.close()

    def _rows(self, query, **parameters):
        with self.driver.session(database=self.database) as session:
            return [record.data() for record in session.run(query, **parameters)]

    def _require_imported_catalog(self):
        rows = self._rows(
            "MATCH (f:FoodRecord) WITH count(f) AS foods "
            "OPTIONAL MATCH (r:Recipe) RETURN foods, count(r) AS recipes"
        )
        if not rows or rows[0]["foods"] == 0 or rows[0]["recipes"] == 0:
            raise RuntimeError(
                "RenAlly data is not loaded in Neo4j. Run "
                "'python scripts/import_foundation_neo4j.py <foundation-foods.json> --sr-legacy-json <sr-legacy.json>' first."
            )

    def _wait_for_search_indexes(self, timeout_seconds=45):
        deadline = time.monotonic() + timeout_seconds
        wanted = set(SEARCH_INDEXES)
        while time.monotonic() < deadline:
            rows = self._rows(
                "SHOW FULLTEXT INDEXES YIELD name, state "
                "WHERE name IN $names RETURN name, state",
                names=list(wanted),
            )
            states = {row["name"]: row["state"] for row in rows}
            if wanted.issubset(states) and all(states[name] == "ONLINE" for name in wanted):
                return
            time.sleep(0.5)
        raise RuntimeError(
            "RenAlly full-text indexes are missing or not online. Re-run "
            "'python scripts/import_foundation_neo4j.py <foundation-foods.json> --sr-legacy-json <sr-legacy.json>'."
        )

    @staticmethod
    def _source(food):
        fdc_id = food.get("fdc_id")
        return {
            "name": food.get("source_dataset") or "USDA FoodData Central",
            "dataset_key": food.get("dataset_key"),
            "data_type": food.get("data_type"),
            "fdc_id": fdc_id,
            "release_date": food.get("release_date"),
            "basis": food.get("nutrition_basis") or "per 100 g; source food state is stated in the USDA description",
            "url": food.get("source_url") or (
                f"https://fdc.nal.usda.gov/food-details/{fdc_id}/nutrients" if fdc_id else None
            ),
        }

    @staticmethod
    def _nutrient_key(record):
        fdc_id = str(record.get("fdc_nutrient_id", ""))
        return FDC_NUTRIENT_ID_KEYS.get(fdc_id) or f"fdc_nutrient_{fdc_id}"

    def _recipe_catalog_rows(self):
        """Stream recipe projections in bounded Neo4j transactions.

        Food.com now contributes over half a million recipes. Loading them and
        their ingredient relationships in one Cypher aggregation can exceed
        Neo4j's transaction memory limit, so use the indexed recipe ID as a
        keyset cursor and keep each query bounded.
        """
        after_id = ""
        while True:
            rows = self._rows(
                "MATCH (recipe:Recipe) "
                "WHERE recipe.id > $after_id "
                "WITH recipe ORDER BY recipe.id LIMIT $limit "
                "OPTIONAL MATCH (recipe)-[contains:CONTAINS]->(item) "
                "RETURN recipe.id AS cursor, "
                "{id: recipe.id, name: recipe.name, description: recipe.description, "
                "aliases: recipe.aliases, method: recipe.method, "
                "servings: recipe.servings, source_json: recipe.source_json, "
                "nutrition_complete: recipe.nutrition_complete, "
                "nutrition_status: recipe.nutrition_status, source_dataset: recipe.source_dataset, "
                "source_id: recipe.source_id, tags: recipe.foodcom_tags, "
                "ingredient_names: recipe.foodcom_ingredient_names, "
                "missing_ingredient_ids: recipe.missing_ingredient_ids} AS recipe, "
                "collect(CASE WHEN item IS NULL THEN null ELSE {id: coalesce(item.canonical_id, item.id), "
                "amount_g: contains.amount_g, position: contains.position, "
                "unresolved: 'UnresolvedIngredientReference' IN labels(item)} END) AS items "
                "ORDER BY cursor",
                after_id=after_id, limit=RECIPE_CATALOG_PAGE_SIZE,
            )
            if not rows:
                return
            after_id = rows[-1]["cursor"]
            yield from rows

    def _load_catalog(self):
        food_rows = self._rows(
            "MATCH (food:FoodRecord) "
            "OPTIONAL MATCH (food)-[value:HAS_NUTRIENT]->(nutrient:Nutrient) "
            "RETURN properties(food) AS food, "
            "collect(CASE WHEN nutrient IS NULL THEN null ELSE {"
            "fdc_nutrient_id: nutrient.fdc_nutrient_id, name: nutrient.name, "
            "unit: nutrient.unit, amount: value.amount, key: value.nutrient_key} END) AS nutrients"
        )
        self.ingredients = {}
        for row in food_rows:
            food = row["food"]
            values, units, names = {}, {}, {}
            for nutrient in row.get("nutrients", []):
                if not nutrient or not isinstance(nutrient.get("amount"), (int, float)):
                    continue
                key = nutrient.get("key") or self._nutrient_key(nutrient)
                values[key] = nutrient["amount"]
                units[key] = nutrient.get("unit") or NUTRIENT_UNITS.get(key, "")
                names[key] = nutrient.get("name") or key.replace("_", " ")
            try:
                portions = json.loads(food.get("food_portions_json") or "[]")
            except (TypeError, json.JSONDecodeError):
                portions = []
            ingredient_id = food["id"]
            self.ingredients[ingredient_id] = {
                "name": food.get("name") or food.get("description") or ingredient_id,
                "fdc_description": food.get("description", ""),
                "aliases": food.get("aliases", []),
                "role": food.get("role") or food.get("ingredient_type"),
                "ingredient_type": food.get("ingredient_type"),
                "substitution_group": food.get("substitution_group"),
                "nutrients_per_100g": values,
                "nutrient_units": units,
                "nutrient_names": names,
                "typical_amount_g": food.get("typical_amount_g"),
                "min_amount_g": food.get("min_amount_g"),
                "max_amount_g": food.get("max_amount_g"),
                "food_state": food.get("description", ""),
                "portions": portions,
                "source": self._source(food),
            }

        self.recipes = {}
        for row in self._recipe_catalog_rows():
            recipe = {key: value for key, value in row["recipe"].items()
                      if value is not None and value != []}
            try:
                source = json.loads(recipe.get("source_json") or "{}")
            except (TypeError, json.JSONDecodeError):
                source = {}
            recipe.pop("source_json", None)
            items = [item for item in row.get("items", []) if item]
            items.sort(key=lambda item: item.get("position") or 0)
            self.recipes[recipe["id"]] = {
                **recipe,
                "source": source,
                "ingredients": [{"id": item["id"], "amount_g": item["amount_g"]}
                                for item in items],
                "missing_ingredient_ids": [item["id"] for item in items if item.get("unresolved")],
            }

        substitution_rows = self._rows(
            "MATCH (substitute:FoundationFood)-[rel:SUBSTITUTE_FOR]->(original:FoundationFood) "
            "RETURN substitute.id AS substitute, original.id AS original, "
            "rel.compatibility AS compatibility, rel.evidence AS evidence"
        )
        self.substitutions = substitution_rows

    def _load_graph_projection(self):
        """Build the small graph-page projection from graph relationships."""
        recipe_rows = self._rows(
            "MATCH (recipe:Recipe)-[contains:CONTAINS]->(food:FoundationFood) "
            "RETURN properties(recipe) AS recipe, properties(food) AS food, "
            "contains.amount_g AS amount_g"
        )
        nodes, edges, recipe_ids, food_ids = {}, [], set(), set()
        for row in recipe_rows:
            recipe, food = row["recipe"], row["food"]
            rid, fid = recipe["id"], food["id"]
            recipe_ids.add(rid)
            food_ids.add(fid)
            nodes[rid] = {
                "kind": "recipe", "name": recipe.get("name", rid),
                "description": recipe.get("description", ""),
                "aliases": recipe.get("aliases", []),
            }
            nodes[fid] = {
                "kind": "ingredient", "name": food.get("name") or food.get("description") or fid,
                "description": food.get("description", ""), "aliases": food.get("aliases", []),
                "source": {"fdc_id": food.get("fdc_id")},
                "ingredient_type": food.get("ingredient_type"),
                "substitution_group": food.get("substitution_group"),
            }
            edges.append({"source": rid, "target": fid, "relation": "contains",
                          "amount_g": row.get("amount_g")})

        if food_ids:
            type_rows = self._rows(
                "MATCH (food:FoundationFood)-[:IS_A]->(type:IngredientType) "
                "WHERE food.id IN $food_ids RETURN food.id AS food_id, properties(type) AS type",
                food_ids=list(food_ids),
            )
            for row in type_rows:
                type_data = row["type"]
                type_id = f"type:{type_data['name']}"
                nodes.setdefault(type_id, {"kind": "ingredient_type", "name": type_data["name"],
                                           "description": f"Ingredient category: {type_data['name']}"})
                edges.append({"source": row["food_id"], "target": type_id, "relation": "is_a"})

            substitution_rows = self._rows(
                "MATCH (substitute:FoundationFood)-[rel:SUBSTITUTE_FOR]->(original:FoundationFood) "
                "WHERE substitute.id IN $food_ids AND original.id IN $food_ids "
                "RETURN substitute.id AS substitute, original.id AS original, "
                "rel.compatibility AS compatibility",
                food_ids=list(food_ids),
            )
            edges.extend({"source": row["substitute"], "target": row["original"],
                          "relation": "substitute_for", "compatibility": row.get("compatibility", 0)}
                         for row in substitution_rows)
        self.graph = {"nodes": nodes, "edges": edges}

    def search(self, index_name, search_query, k=5, dataset_scope="all"):
        if index_name not in SEARCH_INDEXES:
            raise ValueError("Unknown RenAlly full-text index.")
        dataset_keys = {
            "foundation_foods": ["foundation_foods"],
            "sr_legacy": ["sr_legacy"],
        }.get(str(dataset_scope or "all").casefold())
        if index_name == "renally_recipe_search":
            dataset_keys = None
        rows = self._rows(
            "CALL db.index.fulltext.queryNodes($index_name, $search_query) "
            "YIELD node, score "
            "WHERE $dataset_keys IS NULL OR node.dataset_key IN $dataset_keys "
            "RETURN node.id AS id, node.name AS name, node.aliases AS aliases, "
            "node.dataset_key AS dataset_key, score "
            "ORDER BY score DESC LIMIT $limit",
            index_name=index_name, search_query=search_query,
            dataset_keys=dataset_keys, limit=max(1, int(k)),
        )
        return rows

    def get_usda_food_records_by_fdc_ids(self, fdc_ids, *, max_records=256):
        """Read bounded USDA food and original nutrient records by FDC ID.

        Nutrient IDs, USDA names, and source units are returned unchanged so
        audit/reporting code can distinguish a missing nutrient from zero and
        avoid conflating similarly named nutrients such as phosphorus and
        phosphate.
        """
        normalized = set()
        for value in fdc_ids or []:
            if isinstance(value, bool) or not str(value or "").strip().isdigit() or int(value) <= 0:
                raise ValueError("USDA FDC IDs must be positive integers.")
            normalized.add(int(value))
        if len(normalized) > max_records:
            raise ValueError(f"At most {max_records} USDA FDC IDs may be read at once.")
        if not normalized:
            return []
        return self._rows(
            "MATCH (food:FoodRecord) WHERE food.fdc_id IN $fdc_ids "
            "OPTIONAL MATCH (food)-[measurement:HAS_NUTRIENT]->(nutrient:Nutrient) "
            "WITH food, collect(CASE WHEN nutrient IS NULL THEN null ELSE {"
            "fdc_nutrient_id: nutrient.fdc_nutrient_id, name: nutrient.name, "
            "unit: nutrient.unit, amount: measurement.amount, "
            "nutrient_key: measurement.nutrient_key} END) AS nutrients "
            "ORDER BY food.fdc_id "
            "RETURN properties(food) AS food, nutrients",
            fdc_ids=sorted(normalized),
        )

    def graph_context(self, recipe_id):
        """Retrieve a recipe's ingredient, nutrient, type, and substitution context."""
        rows = self._rows(
            "MATCH (recipe:Recipe {id: $recipe_id})-[:CONTAINS]->(food:FoundationFood) "
            "OPTIONAL MATCH (food)-[value:HAS_NUTRIENT]->(nutrient:Nutrient) "
            "OPTIONAL MATCH (food)-[:IS_A]->(type:IngredientType) "
            "OPTIONAL MATCH (substitute:FoundationFood)-[sub:SUBSTITUTE_FOR]->(food) "
            "RETURN food.id AS ingredient_id, food.name AS name, food.fdc_id AS fdc_id, "
            "type.name AS ingredient_type, collect(DISTINCT {key: value.nutrient_key, "
            "amount: value.amount, unit: nutrient.unit, name: nutrient.name}) AS nutrients, "
            "collect(DISTINCT {id: substitute.id, compatibility: sub.compatibility}) AS substitutes",
            recipe_id=recipe_id,
        )
        return [row for row in rows if row.get("ingredient_id")]

    def recipes_containing_ingredient(self, ingredient_id):
        """Return recipe IDs linked to an ingredient by Neo4j CONTAINS edges."""
        rows = self._rows(
            "MATCH (recipe:Recipe)-[:CONTAINS]->(food:FoundationFood {id: $ingredient_id}) "
            "RETURN recipe.id AS recipe_id ORDER BY recipe.name",
            ingredient_id=ingredient_id,
        )
        return [row["recipe_id"] for row in rows if row.get("recipe_id")]

    def recipe_ingredients(self, recipe_id):
        return [(item["id"], item["amount_g"])
                for item in self.recipes[recipe_id].get("ingredients", [])]

    def get_foodcom_recipe_batch(self, *, after_recipe_id="", limit=250):
        """Read one bounded, keyset-paginated batch of imported Food.com details.

        This is a read-only source for offline ingredient processing. It does
        not create indexes, alter source recipes, or load all details_json into
        memory at once.
        """
        bounded_limit = max(1, min(int(limit), 1000))
        return self._rows(
            "MATCH (recipe:FoodComRecipe) "
            "WHERE recipe.id > $after_recipe_id "
            "WITH recipe ORDER BY recipe.id LIMIT $limit "
            "RETURN recipe.id AS recipe_id, recipe.name AS recipe_name, "
            "recipe.details_json AS details_json ORDER BY recipe_id",
            after_recipe_id=str(after_recipe_id or ""), limit=bounded_limit,
        )

    def recipe_details(self, recipe_id):
        """Return full recipe details, lazily loading large source records."""
        recipe = self.recipes.get(recipe_id)
        if recipe is None or recipe.get("source_dataset") != "Food.com Recipes and Reviews (Kaggle)":
            return recipe
        rows = self._rows(
            "MATCH (recipe:FoodComRecipe {id: $recipe_id}) "
            "RETURN recipe.details_json AS details_json",
            recipe_id=recipe_id,
        )
        if not rows or not rows[0].get("details_json"):
            return recipe
        try:
            details = json.loads(rows[0]["details_json"])
        except (TypeError, json.JSONDecodeError):
            return recipe
        return {
            **recipe,
            "method": details.get("instructions", []),
            "raw_ingredients": details.get("ingredients", []),
            "raw_ingredient_quantities": details.get("ingredient_quantities", []),
            "raw_ingredient_items": details.get("ingredient_items", []),
            "recipe_servings": details.get("servings_numeric"),
            "recipe_servings_raw": details.get("servings_raw"),
            "recipe_yield": details.get("recipe_yield"),
            "cook_time": details.get("cook_time"),
            "prep_time": details.get("prep_time"),
            "total_time": details.get("total_time"),
        }

    @staticmethod
    def _decode_ingredient_mapping_record(record):
        if not record:
            return None
        decoded = dict(record)
        for property_name, result_name, fallback in (
            ("attributes_json", "attributes", {}),
            ("candidates_json", "candidates", []),
            ("selected_candidate_json", "selected_candidate", None),
            ("slm_evaluation_json", "slm_evaluation", None),
            ("verification_evidence_json", "verification_evidence", None),
            ("automatic_policy_evaluation_json", "automatic_policy_evaluation", None),
        ):
            try:
                decoded[result_name] = json.loads(decoded.get(property_name) or json.dumps(fallback))
            except (TypeError, json.JSONDecodeError):
                decoded[result_name] = fallback
            decoded.pop(property_name, None)
        return decoded

    def get_ingredient_mapping_cache(self, cache_key):
        """Read one persistent FoodData Central mapping record by context key."""
        rows = self._rows(
            "MATCH (mapping:IngredientMapping {cache_key: $cache_key}) "
            "RETURN properties(mapping) AS record",
            cache_key=cache_key,
        )
        return self._decode_ingredient_mapping_record(rows[0].get("record")) if rows else None

    def save_ingredient_mapping_cache(self, record):
        """Add or update an unverified mapping record without demoting a verified one.

        The unique constraint and label are additive. No recipe or USDA food
        records are changed by this cache operation.
        """
        status = record.get("status")
        if status not in {"VERIFIED", "CANDIDATE", "AMBIGUOUS", "UNRESOLVED"}:
            raise ValueError(f"Unsupported ingredient mapping status: {status!r}")
        candidates = record.get("candidates") or []
        if status == "VERIFIED":
            candidate_ids = {str(item.get("fdc_id")) for item in candidates if item.get("fdc_id") is not None}
            method = record.get("verification_method") or record.get("validation_method")
            has_candidate = (record.get("fdc_id") is not None
                             and str(record["fdc_id"]) in candidate_ids)
            if method == "human_review":
                valid_provenance = bool(str(record.get("reviewer") or record.get("validated_by") or "").strip())
            elif method == "DETERMINISTIC_POLICY":
                evidence = record.get("verification_evidence")
                policy_version = record.get("verification_policy_version")
                valid_provenance = (
                    isinstance(policy_version, int) and policy_version > 0
                    and not str(record.get("reviewer") or record.get("validated_by") or "").strip()
                    and isinstance(evidence, dict) and evidence.get("eligible") is True
                    and evidence.get("policy_version") == policy_version
                    and str(evidence.get("fdc_id")) == str(record.get("fdc_id"))
                )
            else:
                valid_provenance = False
            if not has_candidate or not valid_provenance:
                raise ValueError(
                    "VERIFIED mappings require a cached candidate and explicit human-review or "
                    "deterministic-policy provenance."
                )

        if not self._ingredient_mapping_cache_ready:
            self._rows(
                "CREATE CONSTRAINT renally_ingredient_mapping_cache_key IF NOT EXISTS "
                "FOR (mapping:IngredientMapping) REQUIRE mapping.cache_key IS UNIQUE"
            )
            self._ingredient_mapping_cache_ready = True

        created_at = record.get("created_at")
        properties = {
            "ingredient_name": record.get("ingredient_name"),
            "normalized_name": record.get("normalized_name"),
            "dataset_scope": record.get("dataset_scope"),
            "attributes_json": json.dumps(record.get("attributes") or {}, ensure_ascii=False,
                                           separators=(",", ":")),
            "candidates_json": json.dumps(candidates, ensure_ascii=False, separators=(",", ":")),
            "selected_candidate_json": json.dumps(record.get("selected_candidate"), ensure_ascii=False,
                                                   separators=(",", ":")),
            "status": status,
            "mapping_version": record.get("mapping_version"),
            "validation_method": record.get("validation_method"),
            "verification_method": record.get("verification_method") or record.get("validation_method"),
            "verification_policy_version": record.get("verification_policy_version"),
            "verification_evidence_json": json.dumps(record.get("verification_evidence"), ensure_ascii=False,
                                                        separators=(",", ":")),
            "automatic_policy_evaluation_json": json.dumps(record.get("automatic_policy_evaluation"), ensure_ascii=False,
                                                            separators=(",", ":")),
            "reviewer": record.get("reviewer") or record.get("validated_by"),
            "validated_by": record.get("validated_by"),
            "validation_note": record.get("validation_note"),
            "validated_at": record.get("validated_at"),
            "fdc_id": record.get("fdc_id"),
            "fdc_description": record.get("fdc_description"),
            "fdc_data_type": record.get("fdc_data_type"),
            "fdc_food_state": record.get("fdc_food_state"),
            "updated_at": record.get("updated_at"),
        }
        properties = {key: value for key, value in properties.items() if value is not None}
        rows = self._rows(
            "MERGE (mapping:IngredientMapping {cache_key: $cache_key}) "
            "ON CREATE SET mapping.created_at = $created_at "
            "FOREACH (_ IN CASE WHEN mapping.status <> 'VERIFIED' "
            "AND coalesce(mapping.candidates_json, '') <> $candidate_json THEN [1] ELSE [] END | "
            "SET mapping.slm_evaluation_json = null, mapping.slm_evaluation_updated_at = null, "
            "mapping.automatic_policy_evaluation_json = null, mapping.automatic_policy_evaluation_updated_at = null) "
            "FOREACH (_ IN CASE WHEN mapping.status = 'VERIFIED' THEN [] ELSE [1] END | "
            "SET mapping += $properties) "
            "RETURN properties(mapping) AS record",
            cache_key=record["cache_key"], created_at=created_at,
            status=status, properties=properties, candidate_json=properties["candidates_json"],
        )
        return self._decode_ingredient_mapping_record(rows[0].get("record")) if rows else None

    def save_ingredient_mapping_evaluation(self, cache_key, evaluation):
        """Store SLM audit data without writing mapping or reviewer fields.

        The candidate IDs must still match the current cached candidate set.
        Only the additive ``slm_evaluation_json`` and timestamp properties are
        updated; status, selected FDC ID, and human-review metadata are untouched.
        """
        cached = self.get_ingredient_mapping_cache(cache_key)
        if not cached:
            return None
        cached_ids = {str(item.get("fdc_id")) for item in cached.get("candidates", [])
                      if isinstance(item, dict) and item.get("fdc_id") is not None}
        evaluation_ids = {str(item) for item in evaluation.get("candidate_ids", [])
                          if item is not None}
        if cached_ids != evaluation_ids:
            raise ValueError("SLM evaluation candidate IDs do not match the current mapping cache.")
        evaluated_at = evaluation.get("evaluated_at")
        rows = self._rows(
            "MATCH (mapping:IngredientMapping {cache_key: $cache_key}) "
            "SET mapping.slm_evaluation_json = $evaluation_json, "
            "    mapping.slm_evaluation_updated_at = $evaluated_at "
            "RETURN properties(mapping) AS record",
            cache_key=cache_key,
            evaluation_json=json.dumps(evaluation, ensure_ascii=False, separators=(",", ":")),
            evaluated_at=evaluated_at,
        )
        return self._decode_ingredient_mapping_record(rows[0].get("record")) if rows else None

    def save_ingredient_mapping_policy_evaluation(self, cache_key, evaluation):
        """Save deterministic policy evidence separately from mapping status."""
        cached = self.get_ingredient_mapping_cache(cache_key)
        if not cached:
            return None
        cached_ids = {str(item.get("fdc_id")) for item in cached.get("candidates", [])
                      if isinstance(item, dict) and item.get("fdc_id") is not None}
        evaluation_ids = {str(item) for item in evaluation.get("candidate_ids", [])
                          if item is not None}
        if cached_ids != evaluation_ids:
            raise ValueError("Policy evaluation candidate IDs do not match the current mapping cache.")
        rows = self._rows(
            "MATCH (mapping:IngredientMapping {cache_key: $cache_key}) "
            "SET mapping.automatic_policy_evaluation_json = $evaluation_json, "
            "    mapping.automatic_policy_evaluation_updated_at = $evaluated_at "
            "RETURN properties(mapping) AS record",
            cache_key=cache_key,
            evaluation_json=json.dumps(evaluation, ensure_ascii=False, separators=(",", ":")),
            evaluated_at=evaluation.get("evaluated_at"),
        )
        return self._decode_ingredient_mapping_record(rows[0].get("record")) if rows else None

    def upgrade_automatic_mapping_policy(self, cache_key, fdc_id, policy_version, evidence):
        """Upgrade only the provenance of the same existing automatic FDC mapping."""
        cached = self.get_ingredient_mapping_cache(cache_key)
        if (not cached or cached.get("status") != "VERIFIED"
                or (cached.get("verification_method") or cached.get("validation_method")) != "DETERMINISTIC_POLICY"
                or str(cached.get("fdc_id")) != str(fdc_id)):
            raise ValueError("Only the same VERIFIED automatic mapping can be policy-upgraded.")
        if (not isinstance(policy_version, int) or policy_version < 1
                or not isinstance(evidence, dict) or evidence.get("eligible") is not True
                or evidence.get("policy_version") != policy_version
                or str(evidence.get("fdc_id")) != str(fdc_id)):
            raise ValueError("Automatic policy evidence is invalid.")
        rows = self._rows(
            "MATCH (mapping:IngredientMapping {cache_key: $cache_key}) "
            "WHERE mapping.status = 'VERIFIED' "
            "AND mapping.verification_method = 'DETERMINISTIC_POLICY' "
            "AND toString(mapping.fdc_id) = toString($fdc_id) "
            "SET mapping.verification_policy_version = $policy_version, "
            "    mapping.verification_evidence_json = $evidence_json, "
            "    mapping.automatic_policy_evaluation_json = $evidence_json, "
            "    mapping.validated_at = $updated_at, mapping.updated_at = $updated_at "
            "RETURN properties(mapping) AS record",
            cache_key=cache_key, fdc_id=fdc_id, policy_version=policy_version,
            evidence_json=json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
            updated_at=evidence.get("evaluated_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return self._decode_ingredient_mapping_record(rows[0].get("record")) if rows else None

    @staticmethod
    def _decode_ingredient_reference_record(record):
        if not record:
            return None
        decoded = dict(record)
        for property_name, result_name, fallback in (
            ("attributes_json", "attributes", {}),
            ("review_provenance_json", "review_provenance", None),
            ("provenance_json", "provenance", None),
            ("context_json", "context", {}),
            ("details_json", "details", {}),
        ):
            if property_name not in decoded:
                continue
            try:
                decoded[result_name] = json.loads(decoded.get(property_name) or json.dumps(fallback))
            except (TypeError, json.JSONDecodeError):
                decoded[result_name] = fallback
            decoded.pop(property_name, None)
        return decoded

    def list_ingredient_references(self, canonical_ingredient_name=None):
        """Read current preferred-reference records without creating schema."""
        rows = self._rows(
            "MATCH (reference:USDAIngredientReference) "
            "WHERE $canonical_ingredient_name IS NULL "
            "OR reference.canonical_ingredient_name = $canonical_ingredient_name "
            "RETURN properties(reference) AS record ORDER BY reference.updated_at DESC",
            canonical_ingredient_name=canonical_ingredient_name,
        )
        return [decoded for row in rows
                if (decoded := self._decode_ingredient_reference_record(row.get("record"))) is not None]

    def list_ingredient_reference_reviews(self, reference_id=None):
        """Read append-only review history for reference and queue actions."""
        rows = self._rows(
            "MATCH (review:USDAIngredientReferenceReview) "
            "WHERE $reference_id IS NULL OR review.reference_id = $reference_id "
            "RETURN properties(review) AS record ORDER BY review.created_at, review.event_id",
            reference_id=reference_id,
        )
        return [decoded for row in rows
                if (decoded := self._decode_ingredient_reference_record(row.get("record"))) is not None]

    def _ensure_ingredient_reference_store(self):
        if self._ingredient_reference_store_ready:
            return
        self._rows(
            "CREATE CONSTRAINT renally_usda_ingredient_reference_id IF NOT EXISTS "
            "FOR (reference:USDAIngredientReference) REQUIRE reference.reference_id IS UNIQUE"
        )
        self._rows(
            "CREATE CONSTRAINT renally_usda_ingredient_reference_review_id IF NOT EXISTS "
            "FOR (review:USDAIngredientReferenceReview) REQUIRE review.event_id IS UNIQUE"
        )
        self._ingredient_reference_store_ready = True

    def save_ingredient_reference_decision(self, reference, review_event):
        """Append a human review event and update a separate preferred reference.

        Reference records never share the ``IngredientMapping`` label. This
        additive write does not touch FoodRecord, Recipe, or nutrient nodes.
        """
        if reference.get("reference_status") not in {"APPROVED", "REJECTED"}:
            raise ValueError("A reference decision must be APPROVED or REJECTED.")
        if (not reference.get("reviewer_identity") or not reference.get("review_provenance")
                or not review_event.get("event_id") or not review_event.get("reviewer_identity")):
            raise ValueError("Reference decisions require reviewer identity and provenance.")
        self._ensure_ingredient_reference_store()
        reference_properties = {
            "canonical_ingredient_name": reference.get("canonical_ingredient_name"),
            "fdc_id": reference.get("fdc_id"),
            "fdc_description": reference.get("fdc_description"),
            "food_record_id": reference.get("food_record_id"),
            "dataset_key": reference.get("dataset_key"),
            "data_type": reference.get("data_type"),
            "attributes_json": json.dumps(reference.get("attributes") or {}, ensure_ascii=False,
                                           separators=(",", ":")),
            "preparation_state": reference.get("preparation_state"),
            "processing_state": reference.get("processing_state"),
            "species": reference.get("species"),
            "variety": reference.get("variety"),
            "farm_raised": reference.get("farm_raised"),
            "enrichment_state": reference.get("enrichment_state"),
            "reference_status": reference.get("reference_status"),
            "allow_as_default": reference.get("allow_as_default") is True,
            "reviewer_identity": reference.get("reviewer_identity"),
            "review_provenance_json": json.dumps(reference.get("review_provenance"), ensure_ascii=False,
                                                  separators=(",", ":")),
            "review_rationale": reference.get("review_rationale"),
            "reference_policy_version": reference.get("reference_policy_version"),
            "updated_at": reference.get("updated_at"),
        }
        reference_properties = {key: value for key, value in reference_properties.items() if value is not None}
        review_properties = {
            "action": review_event.get("action"),
            "reference_id": review_event.get("reference_id"),
            "canonical_ingredient_name": review_event.get("canonical_ingredient_name"),
            "fdc_id": review_event.get("fdc_id"),
            "reviewer_identity": review_event.get("reviewer_identity"),
            "rationale": review_event.get("rationale"),
            "provenance_json": json.dumps(review_event.get("provenance"), ensure_ascii=False,
                                           separators=(",", ":")),
            "details_json": json.dumps(review_event.get("details") or {}, ensure_ascii=False,
                                        separators=(",", ":")),
            "reference_policy_version": review_event.get("reference_policy_version"),
            "created_at": review_event.get("created_at"),
        }
        review_properties = {key: value for key, value in review_properties.items() if value is not None}
        rows = self._rows(
            "MERGE (reference:USDAIngredientReference {reference_id: $reference_id}) "
            "ON CREATE SET reference.created_at = $created_at "
            "SET reference += $reference_properties, "
            "    reference.review_revision = coalesce(reference.review_revision, 0) + 1 "
            "CREATE (review:USDAIngredientReferenceReview {event_id: $event_id}) "
            "SET review += $review_properties "
            "CREATE (review)-[:UPDATES]->(reference) "
            "RETURN properties(reference) AS record",
            reference_id=reference["reference_id"],
            created_at=reference.get("created_at") or reference.get("updated_at"),
            event_id=review_event["event_id"],
            reference_properties=reference_properties,
            review_properties=review_properties,
        )
        return self._decode_ingredient_reference_record(rows[0].get("record")) if rows else None

    def save_ingredient_reference_review_action(self, event):
        """Persist a non-reference review action as a separate audit event."""
        if not event.get("event_id") or not event.get("reviewer_identity"):
            raise ValueError("Reference review actions require an event and reviewer identity.")
        self._ensure_ingredient_reference_store()
        properties = {
            "action": event.get("action"),
            "reference_id": event.get("reference_id"),
            "canonical_ingredient_name": event.get("canonical_ingredient_name"),
            "reviewer_identity": event.get("reviewer_identity"),
            "provenance_json": json.dumps(event.get("provenance"), ensure_ascii=False,
                                           separators=(",", ":")),
            "context_json": json.dumps(event.get("context") or {}, ensure_ascii=False,
                                       separators=(",", ":")),
            "details_json": json.dumps(event.get("details") or {}, ensure_ascii=False,
                                        separators=(",", ":")),
            "reference_policy_version": event.get("reference_policy_version"),
            "created_at": event.get("created_at"),
        }
        properties = {key: value for key, value in properties.items() if value is not None}
        rows = self._rows(
            "CREATE (review:USDAIngredientReferenceReview {event_id: $event_id}) "
            "SET review += $properties RETURN properties(review) AS record",
            event_id=event["event_id"], properties=properties,
        )
        return self._decode_ingredient_reference_record(rows[0].get("record")) if rows else None

    def substitutions_for(self, ingredient_id):
        return [(item["substitute"], item.get("compatibility", 0.0))
                for item in self.substitutions if item.get("original") == ingredient_id]

    def substitution_candidates_for(self, ingredient_id, type_limit=2):
        rows = self._rows(
            "MATCH (target:FoundationFood {id: $ingredient_id}) "
            "CALL { WITH target "
            "  OPTIONAL MATCH (candidate:FoundationFood)-[rel:SUBSTITUTE_FOR]->(target) "
            "  RETURN collect(CASE WHEN candidate IS NULL THEN null ELSE {id: candidate.id, "
            "    compatibility: rel.compatibility, basis: 'explicit_substitute_for'} END) AS explicit } "
            "CALL { WITH target "
            "  OPTIONAL MATCH (candidate:FoundationFood) "
            "  WHERE target.substitution_group IS NOT NULL "
            "    AND candidate.substitution_group = target.substitution_group "
            "    AND candidate.id <> target.id AND candidate.typical_amount_g IS NOT NULL "
            "  WITH target, candidate ORDER BY candidate.name LIMIT $type_limit "
            "  RETURN collect(CASE WHEN candidate IS NULL THEN null ELSE {id: candidate.id, "
            "    compatibility: 0.55, basis: 'same_substitution_group:' + target.substitution_group} END) AS related } "
            "RETURN explicit, related",
            ingredient_id=ingredient_id, type_limit=max(0, int(type_limit)),
        )
        if not rows:
            return []
        explicit = [item for item in (rows[0].get("explicit") or []) if item]
        seen = {ingredient_id, *(item["id"] for item in explicit)}
        related = [item for item in (rows[0].get("related") or [])
                   if item and item["id"] not in seen]
        return explicit + related

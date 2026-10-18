"""Conservative recovery of Food.com ingredient quantities.

This module keeps source quantity recovery separate from ingredient identity
resolution. Weight units can be normalized here; volume and count units need
an ingredient-specific, source-labelled factor after the hybrid mapper selects
an authorized USDA FoodData Central record.
"""

import copy
import math
import re
from time import perf_counter

from src.recipes.ingredient_parser import (
    WEIGHT_GRAMS,
    normalize_ingredient_name,
    parse_foodcom_ingredient,
    parse_ingredient_text,
)


QUANTITY_RESOLUTION_VERSION = "foodcom-quantity-v1"


def _as_list(value):
    if isinstance(value, list):
        return value
    if value is None:
        return []
    return [value]


def _first_list(recipe, *keys):
    for key in keys:
        if key in recipe and recipe.get(key) is not None:
            return _as_list(recipe.get(key)), key
    return None, None


def extract_recipe_ingredient_rows(recipe):
    """Return ordered source rows and whether separate arrays safely align.

    Food.com importer records preserve parallel ingredient and quantity arrays.
    A mismatched length invalidates every positional pairing; no available
    quantity is then attached to an ingredient by index.
    """
    if not isinstance(recipe, dict):
        return [], "NO_RECIPE"

    names, _names_key = _first_list(recipe, "raw_ingredients", "raw_ingredient_names")
    quantities, _quantities_key = _first_list(
        recipe, "raw_ingredient_quantities", "ingredient_quantities",
    )
    raw_items = recipe.get("raw_ingredient_items")
    if not isinstance(raw_items, list) or not raw_items:
        raw_items = recipe.get("ingredient_items")

    if names is not None:
        if quantities is None:
            row_items_authoritative = (
                isinstance(raw_items, list) and len(raw_items) == len(names)
                and all(isinstance(item, dict)
                        and item.get("position", index) == index
                        and str(item.get("name") or item.get("ingredient_name") or "").strip()
                        == str(name or "").strip()
                        and (isinstance(item.get("parsed"), dict) or item.get("quantity_raw") is not None)
                        for index, (item, name) in enumerate(zip(raw_items, names))
            ))
            alignment = ("ORDERED_STEP1_PARSED_ITEMS" if row_items_authoritative
                         else "QUANTITY_ARRAY_MISSING")
            paired = False
        elif len(quantities) == 0 and len(names) > 0:
            alignment = "QUANTITY_ARRAY_EMPTY"
            paired = False
            row_items_authoritative = False
        elif len(names) != len(quantities):
            alignment = "MISMATCHED_ARRAY_LENGTHS"
            paired = False
            row_items_authoritative = False
        else:
            alignment = "ALIGNED_PARALLEL_ARRAYS"
            paired = True
            row_items_authoritative = False

        rows = []
        for index, name in enumerate(names):
            cached_item = (raw_items[index] if (paired or row_items_authoritative)
                           and isinstance(raw_items, list)
                           and index < len(raw_items) else None)
            if isinstance(cached_item, dict):
                cached_position = cached_item.get("position", index)
                cached_name = cached_item.get("name") or cached_item.get("ingredient_name")
                if cached_position == index and str(cached_name or "").strip() == str(name or "").strip():
                    source = copy.deepcopy(cached_item)
                    source_name = cached_name
                else:
                    source = {"name": name}
                    source_name = name
            elif isinstance(name, dict):
                source = copy.deepcopy(name)
                source_name = source.get("name") or source.get("ingredient_name")
            else:
                source_name = name
                source = {"name": name}
            # The importer may have generated ingredient_items by zipping
            # unequal source arrays. Only the arrays' own equal lengths prove
            # alignment, so discard any cached row quantity on mismatch.
            source["name"] = source_name
            source["quantity_raw"] = (quantities[index] if paired else
                                       source.get("quantity_raw") if row_items_authoritative else None)
            source["source_position"] = index
            source["quantity_alignment_status"] = alignment
            if not paired and not row_items_authoritative:
                source.pop("parsed", None)
            kind = "parsed_or_legacy" if isinstance(source, dict) else "invalid"
            if source_name is None or not str(source_name).strip():
                kind = "invalid"
            rows.append((index, source, kind))
        return rows, alignment

    if raw_items is not None:
        rows = []
        for index, item in enumerate(raw_items):
            if isinstance(item, dict) and item.get("id"):
                kind = "recipe_link"
            elif isinstance(item, dict):
                kind = "parsed_or_legacy"
            elif isinstance(item, str):
                kind = "text"
            else:
                kind = "invalid"
                item = {"name": "", "invalid_source_item": True}
            rows.append((index, copy.deepcopy(item), kind))
        return rows, "ORDERED_INGREDIENT_ITEMS"

    ingredients = recipe.get("ingredients")
    if not isinstance(ingredients, list):
        return [], "NO_INGREDIENTS"
    rows = []
    for index, item in enumerate(ingredients):
        if isinstance(item, dict) and item.get("id"):
            kind = "recipe_link"
        elif isinstance(item, dict):
            kind = "parsed_or_legacy"
        elif isinstance(item, str):
            kind = "text"
        else:
            kind = "invalid"
            item = {"name": "", "invalid_source_item": True}
        rows.append((index, copy.deepcopy(item), kind))
    if recipe.get("raw_ingredient_quantities") is not None:
        return rows, "QUANTITIES_WITHOUT_NAMES"
    return rows, "ORDERED_INGREDIENT_ITEMS"


def _finite_positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _egg_size_attributes(text):
    value = str(text or "").casefold()
    if not re.search(r"\beggs?\b", value):
        return {}
    for size in ("extra large", "jumbo", "large", "medium", "small", "peewee"):
        if re.search(rf"\b{re.escape(size)}\b", value):
            return {"egg_size": size.replace(" ", "_")}
    return {}


class IngredientQuantityResolver:
    """Resolve raw quantity representations without inventing weights."""

    def resolve_recipe(self, recipe):
        started = perf_counter()
        rows, alignment_status = extract_recipe_ingredient_rows(recipe)
        ingredients = []
        for position, source, kind in rows:
            ingredients.append(self.resolve_item(
                position, source, kind, alignment_status=alignment_status,
            ))
        return {
            "version": QUANTITY_RESOLUTION_VERSION,
            "recipe_id": recipe.get("id") if isinstance(recipe, dict) else None,
            "alignment_status": alignment_status,
            "ingredient_count": len(ingredients),
            "ingredients": ingredients,
            "source_fields_preserved": {
                key: copy.deepcopy(recipe[key])
                for key in ("raw_ingredients", "raw_ingredient_names",
                            "raw_ingredient_quantities", "ingredient_quantities",
                            "raw_ingredient_items", "ingredient_items")
                if isinstance(recipe, dict) and key in recipe
            },
            "quantity_resolution_ms": (perf_counter() - started) * 1000.0,
        }

    def resolve_item(self, position, source, kind="parsed_or_legacy", *, alignment_status="ORDERED_INGREDIENT_ITEMS"):
        if kind == "invalid" or not isinstance(source, (dict, str)):
            return self._unresolved(position, source, "INVALID_SOURCE_ITEM", alignment_status)

        if kind == "recipe_link":
            name = str(source.get("name") or source.get("ingredient_name") or source.get("id"))
            amount = source.get("amount_g")
            if _finite_positive(amount):
                return {
                    "position": position, "source_kind": kind,
                    "original_text": name, "original_name": name,
                    "normalized_name": normalize_ingredient_name(name),
                    "quantity_raw": None, "quantity": amount, "quantity_min": None,
                    "quantity_max": None, "unit": "g", "quantity_g": amount,
                    "quantity_g_min": None, "quantity_g_max": None,
                    "quantity_scope": "TOTAL", "quantity_status": "RECIPE_GRAPH_AMOUNT",
                    "quantity_alignment_status": alignment_status,
                    "conversion_reference": None, "explicit_attributes": {},
                    "source_preserved": {"food_record_id": source.get("id"), "amount_g": amount},
                    "block_reasons": [],
                }
            return self._unresolved(position, source, "MISSING_RECIPE_GRAPH_AMOUNT", alignment_status)

        if isinstance(source, str):
            source = {"name": source}
            kind = "text"

        source_name = source.get("name") or source.get("ingredient_name")
        quantity_raw = source.get("quantity_raw")
        parsed_source = source.get("parsed") if isinstance(source.get("parsed"), dict) else None
        original_text = source.get("original_text") or source.get("description")
        alignment = source.get("quantity_alignment_status", alignment_status)

        if quantity_raw is not None and str(quantity_raw).strip():
            if source_name is None or not str(source_name).strip():
                parsed = parse_ingredient_text(str(quantity_raw))
                source_name = parsed.get("ingredient_name")
                original_text = str(quantity_raw)
            else:
                parsed = parse_foodcom_ingredient(source_name, quantity_raw)
                original_text = parsed.get("original_text")
        elif original_text and (source_name is None or str(original_text).strip() != str(source_name).strip()):
            parsed = parse_ingredient_text(original_text)
            if source_name is not None and not str(source_name).strip():
                source_name = None
            # Combined descriptions are the source of both the name and amount.
            source_name = parsed.get("ingredient_name") or source_name
            quantity_raw = self._quantity_expression(parsed)
        elif source_name is not None and self._looks_combined(source_name):
            original_text = str(source_name)
            parsed = parse_ingredient_text(original_text)
            source_name = parsed.get("ingredient_name") or source_name
            quantity_raw = self._quantity_expression(parsed)
        elif parsed_source is not None:
            source_text = parsed_source.get("original_text")
            if source_text:
                parsed = parse_ingredient_text(source_text)
                original_text = source_text
                if (parsed.get("quantity") is None and parsed.get("quantity_min") is None
                        and parsed_source.get("quantity") is not None):
                    value = parsed_source.get("quantity")
                    unit = parsed_source.get("unit")
                    rebuilt = f"{value} {unit or ''} {source_name or ''}".strip()
                    parsed = parse_ingredient_text(rebuilt)
                    original_text = source_text
            elif parsed_source.get("quantity") is not None:
                # Step 1 parsed objects may omit their source string. Rebuild
                # from explicit structured fields and pass through the same
                # parser validation used for legacy rows.
                value = parsed_source.get("quantity")
                unit = parsed_source.get("unit")
                source_text = f"{value} {unit or ''} {source_name or ''}".strip()
                parsed = parse_ingredient_text(source_text)
                original_text = source_text
            else:
                parsed = copy.deepcopy(parsed_source)
        else:
            parsed = parse_foodcom_ingredient(source_name, None)
            original_text = str(source_name or "")

        # Source names remain authoritative for split fields. For a combined
        # description, parse_ingredient_text already isolated the name.
        if source.get("name") is not None and quantity_raw is not None and source.get("quantity_raw") is not None:
            source_name = str(source.get("name"))
            parsed["ingredient_name"] = source_name
            parsed["normalized_name"] = normalize_ingredient_name(source_name)

        source_context = " ".join(str(value or "") for value in (
            original_text, source_name, quantity_raw,
        ))
        explicit_attributes = {}
        if isinstance(source.get("attributes"), dict):
            explicit_attributes.update(copy.deepcopy(source["attributes"]))
        if isinstance(parsed_source, dict) and isinstance(parsed_source.get("attributes"), dict):
            explicit_attributes.update(copy.deepcopy(parsed_source["attributes"]))
        explicit_attributes.update(_egg_size_attributes(source_context))

        parsed_name = str(parsed.get("ingredient_name") or source_name or "")
        normalized = parsed.get("normalized_name") or normalize_ingredient_name(parsed_name)
        quantity = parsed.get("quantity")
        quantity_min = parsed.get("quantity_min")
        quantity_max = parsed.get("quantity_max")
        unit = parsed.get("unit")
        quantity_g = parsed.get("quantity_g")
        quantity_g_min = parsed.get("quantity_g_min")
        quantity_g_max = parsed.get("quantity_g_max")
        status = str(parsed.get("quantity_status") or "UNRESOLVED")
        scope = parsed.get("quantity_scope") or "UNSPECIFIED"
        block_reasons = []
        if status in {"UNRESOLVED", "UNRESOLVED_RANGE", "UNRESOLVED_PER_ITEM", "UNPARSED"}:
            block_reasons.append("QUANTITY_REQUIRES_SOURCE_UNIT_OR_CONVERSION")
        if status == "CONVERTED_RANGE":
            block_reasons.append("QUANTITY_IS_A_RANGE")
        if scope == "PER_ITEM":
            block_reasons.append("QUANTITY_IS_PER_ITEM_NOT_RECIPE_TOTAL")
        has_source_quantity = (quantity is not None or quantity_min is not None
                              or bool(source.get("quantity_raw"))
                              or self._looks_combined(source_name))
        says_to_taste = "to taste" in source_context.casefold()
        if not has_source_quantity and not says_to_taste:
            status = "MISSING"
            quantity = quantity_min = quantity_max = None
            quantity_g = quantity_g_min = quantity_g_max = None
            unit = None
            block_reasons = ["SOURCE_QUANTITY_MISSING"]

        # Treat only parser-provided standard weights as complete gram values.
        if unit in WEIGHT_GRAMS and _finite_positive(quantity) and status == "CONVERTED":
            quantity_g = float(quantity) * WEIGHT_GRAMS[unit]
        elif quantity_g is not None and not _finite_positive(quantity_g):
            quantity_g = None

        return {
            "position": position, "source_kind": kind,
            "original_text": str(original_text or " ".join(
                str(part) for part in (quantity_raw, parsed_name) if part is not None
            )),
            "original_name": parsed_name,
            "normalized_name": normalized,
            "quantity_raw": None if quantity_raw is None else str(quantity_raw),
            "quantity": quantity if _finite_positive(quantity) else None,
            "quantity_min": quantity_min if _finite_positive(quantity_min) else None,
            "quantity_max": quantity_max if _finite_positive(quantity_max) else None,
            "unit": unit,
            "quantity_g": quantity_g,
            "quantity_g_min": quantity_g_min,
            "quantity_g_max": quantity_g_max,
            "quantity_scope": scope,
            "quantity_status": status,
            "quantity_alignment_status": alignment,
            "conversion_reference": None,
            "explicit_attributes": explicit_attributes,
            "source_preserved": {
                "name": source.get("name"),
                "quantity_raw": source.get("quantity_raw"),
                "original_text": source.get("original_text"),
                "description": source.get("description"),
                "position": source.get("position", position),
            },
            "block_reasons": block_reasons,
        }

    @staticmethod
    def _looks_combined(value):
        text = str(value or "").strip()
        return bool(re.match(r"^[\s\d\u00bc-\u00be\u2150-\u215e]+", text))

    @staticmethod
    def _quantity_expression(parsed):
        if parsed.get("quantity_min") is not None:
            return "{}-{} {}".format(parsed["quantity_min"], parsed["quantity_max"], parsed.get("unit") or "").strip()
        if parsed.get("quantity") is not None:
            return "{} {}".format(parsed["quantity"], parsed.get("unit") or "").strip()
        return None

    @staticmethod
    def _unresolved(position, source, reason, alignment_status):
        return {
            "position": position, "source_kind": "invalid",
            "original_text": str(source or ""), "original_name": "",
            "normalized_name": "", "quantity_raw": None, "quantity": None,
            "quantity_min": None, "quantity_max": None, "unit": None,
            "quantity_g": None, "quantity_g_min": None, "quantity_g_max": None,
            "quantity_scope": "UNSPECIFIED", "quantity_status": "UNRESOLVED",
            "quantity_alignment_status": alignment_status,
            "conversion_reference": None, "explicit_attributes": {},
            "source_preserved": {}, "block_reasons": [reason],
        }


__all__ = [
    "IngredientQuantityResolver", "extract_recipe_ingredient_rows",
    "QUANTITY_RESOLUTION_VERSION",
]

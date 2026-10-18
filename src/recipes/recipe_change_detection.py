"""Conservative structured comparison of source and adapted recipes."""

import copy
import re

from src.recipes.ingredient_parser import normalize_ingredient_name, parse_foodcom_ingredient, parse_ingredient_text


CHANGE_DETECTION_VERSION = "recipe-change-detection-v1"

_PREPARATION = {
    "raw", "uncooked", "cooked", "boiled", "steamed", "baked", "roasted",
    "grilled", "broiled", "fried", "poached", "blanched",
}
_PROCESSING = {
    "smoked", "dried", "freeze-dried", "canned", "fermented", "pickled",
    "salted", "unsalted", "low-sodium", "reduced-sodium", "fresh", "frozen",
}
_MODIFIERS = _PREPARATION | _PROCESSING | {
    "low", "reduced", "sodium", "regular", "seasoning", "packet",
}


def _items(recipe):
    if not isinstance(recipe, dict):
        return None
    for key in ("raw_ingredient_items", "ingredient_items", "ingredients"):
        value = recipe.get(key)
        if isinstance(value, list):
            return value
    names = recipe.get("raw_ingredients", recipe.get("raw_ingredient_names"))
    if isinstance(names, list):
        quantities = recipe.get("raw_ingredient_quantities", recipe.get("ingredient_quantities"))
        aligned = isinstance(quantities, list) and len(quantities) == len(names)
        return [
            {"name": name, "quantity_raw": quantities[index] if aligned else None,
             "position": index}
            for index, name in enumerate(names)
        ]
    return None


def _state(value):
    return str(value or "").strip().casefold().replace("_", "-").replace(" ", "-")


def _item(item, position):
    if isinstance(item, str):
        item = {"name": item}
    if not isinstance(item, dict):
        return {"position": position, "valid": False, "source": copy.deepcopy(item)}
    name = str(item.get("name") or item.get("ingredient_name") or item.get("description") or "")
    quantity_raw = item.get("quantity_raw")
    parsed = (parse_foodcom_ingredient(name, quantity_raw)
              if quantity_raw is not None else parse_ingredient_text(name))
    attrs = copy.deepcopy(item.get("attributes") or parsed.get("attributes") or {})
    state_text = f"{name} {item.get('food_state') or ''}".casefold()
    state_text = re.sub(r"\blow\s+sodium\b", "low-sodium", state_text)
    state_text = re.sub(r"\breduced\s+sodium\b", "reduced-sodium", state_text)
    tokens = set(re.findall(r"[a-z]+(?:-[a-z]+)*", state_text))
    preparation = _state(item.get("preparation_state") or item.get("preparation"))
    processing = _state(item.get("processing_state") or item.get("processing"))
    detected_preparation = sorted(tokens & _PREPARATION)
    detected_processing = sorted(tokens & _PROCESSING)
    identity_name = normalize_ingredient_name(parsed.get("ingredient_name") or name)
    core_name = " ".join(token for token in identity_name.split() if token not in _MODIFIERS)
    explicit_position = item.get("position")
    stable_id = next((item.get(key) for key in ("ingredient_id", "food_id", "id")
                      if item.get(key) is not None), None)
    quantity_g = item.get("amount_g", item.get("quantity_g"))
    quantity = parsed.get("quantity")
    unit = parsed.get("unit") or item.get("unit")
    if quantity_g is not None:
        quantity_signature = ("g", quantity_g)
    else:
        quantity_signature = (unit, quantity, parsed.get("quantity_min"), parsed.get("quantity_max"))
    return {
        "position": explicit_position if isinstance(explicit_position, int) else position,
        "valid": bool(name.strip()) or stable_id is not None,
        "source": copy.deepcopy(item),
        "name": name,
        "normalized_name": identity_name,
        "core_name": core_name,
        "stable_id": str(stable_id) if stable_id is not None else None,
        "attributes": attrs,
        "preparation": preparation,
        "processing": processing,
        "detected_preparation": detected_preparation,
        "detected_processing": detected_processing,
        "quantity_raw": quantity_raw,
        "quantity_signature": quantity_signature,
        "quantity_g": quantity_g,
        "unit": unit,
        "explicit_replaces_position": item.get("replaces_position"),
        "explicit_replaces_id": item.get("replaces_id") or item.get("substitution_of"),
    }


def _same_identity(left, right):
    if left["stable_id"] and right["stable_id"]:
        if left["stable_id"] != right["stable_id"]:
            return False
        return left["core_name"] == right["core_name"]
    # Exact normalized descriptions are useful identity evidence. Similarity
    # or token overlap alone is deliberately insufficient.
    return bool(left["normalized_name"] and left["normalized_name"] == right["normalized_name"])


class RecipeChangeDetector:
    """Detect explicit recipe changes without guessing replacement pairs."""

    def compare(self, original_recipe, adapted_recipe):
        original_raw = _items(original_recipe)
        adapted_raw = _items(adapted_recipe)
        if original_raw is None or adapted_raw is None:
            return {
                "version": CHANGE_DETECTION_VERSION,
                "status": "UNRESOLVED",
                "changes": [],
                "unresolved_changes": [{"reason": "ORIGINAL_OR_ADAPTED_INGREDIENT_LIST_MISSING"}],
            }
        if not original_raw or not adapted_raw:
            return {
                "version": CHANGE_DETECTION_VERSION,
                "status": "UNRESOLVED",
                "changes": [],
                "unresolved_changes": [{"reason": "EMPTY_ORIGINAL_OR_ADAPTED_INGREDIENT_LIST"}],
            }
        original = [_item(value, index) for index, value in enumerate(original_raw)]
        adapted = [_item(value, index) for index, value in enumerate(adapted_raw)]
        if any(not item["valid"] for item in original + adapted):
            return {
                "version": CHANGE_DETECTION_VERSION,
                "status": "UNRESOLVED",
                "changes": [],
                "unresolved_changes": [{"reason": "INVALID_INGREDIENT_ROW"}],
            }

        unmatched_original = set(range(len(original)))
        unmatched_adapted = set(range(len(adapted)))
        pairs = []
        # Prefer stable IDs anywhere in the list, then exact identity at the
        # same position. This retains source positions when rows are reordered.
        for adapted_index, new in enumerate(adapted):
            if not new["stable_id"]:
                continue
            matches = [index for index in unmatched_original
                       if original[index]["stable_id"] == new["stable_id"]
                       and _same_identity(original[index], new)]
            if len(matches) == 1:
                old_index = matches[0]
                pairs.append((old_index, adapted_index, False))
                unmatched_original.remove(old_index)
                unmatched_adapted.remove(adapted_index)
        for adapted_index in list(unmatched_adapted):
            new = adapted[adapted_index]
            if adapted_index in unmatched_original and _same_identity(original[adapted_index], new):
                pairs.append((adapted_index, adapted_index, False))
                unmatched_original.remove(adapted_index)
                unmatched_adapted.remove(adapted_index)

        # Explicit replacement links may establish a pair. Never infer one
        # from fuzzy or merely similar ingredient names.
        for adapted_index in list(unmatched_adapted):
            new = adapted[adapted_index]
            matches = []
            for old_index in unmatched_original:
                old = original[old_index]
                by_position = new["explicit_replaces_position"] == old["position"]
                by_id = new["explicit_replaces_id"] is not None and str(new["explicit_replaces_id"]) == old["stable_id"]
                if by_position or by_id:
                    matches.append(old_index)
            if len(matches) == 1:
                old_index = matches[0]
                pairs.append((old_index, adapted_index, True))
                unmatched_original.remove(old_index)
                unmatched_adapted.remove(adapted_index)

        changes = []
        unresolved = []
        for old_index, new_index, explicit_replacement in pairs:
            old, new = original[old_index], adapted[new_index]
            same_identity = _same_identity(old, new)
            if explicit_replacement and not same_identity:
                changes.append(self._change("REPLACED", old, new, identity_unchanged=False))
                continue
            if not same_identity:
                unresolved.append({
                    "reason": "STABLE_ID_DESCRIPTION_CONFLICT",
                    "original_position": old["position"], "adapted_position": new["position"],
                })
                continue
            changed = False
            if old["quantity_signature"] != new["quantity_signature"]:
                old_unit = old["quantity_signature"][0]
                new_unit = new["quantity_signature"][0]
                kind = "UNIT_CHANGED" if old_unit != new_unit else "QUANTITY_CHANGED"
                changes.append(self._change(kind, old, new, identity_unchanged=True))
                changed = True
            if (old["preparation"], old["detected_preparation"]) != (new["preparation"], new["detected_preparation"]):
                changes.append(self._change("PREPARATION_CHANGED", old, new, identity_unchanged=True))
                changed = True
            if (old["processing"], old["detected_processing"]) != (new["processing"], new["detected_processing"]):
                changes.append(self._change("PROCESSING_CHANGED", old, new, identity_unchanged=True))
                changed = True
            if old["attributes"] != new["attributes"]:
                changes.append(self._change("ATTRIBUTES_CHANGED", old, new, identity_unchanged=True))
                changed = True
            if not changed:
                changes.append(self._change("UNCHANGED", old, new, identity_unchanged=True))

        for old_index in sorted(unmatched_original):
            old = original[old_index]
            changes.append(self._change("REMOVED", old, None, identity_unchanged=False))
        for new_index in sorted(unmatched_adapted):
            new = adapted[new_index]
            changes.append(self._change("ADDED", None, new, identity_unchanged=False))
        if unmatched_original and unmatched_adapted:
            unresolved.append({
                "reason": "REPLACEMENT_RELATIONSHIP_NOT_EXPLICIT",
                "original_positions": [original[index]["position"] for index in sorted(unmatched_original)],
                "adapted_positions": [adapted[index]["position"] for index in sorted(unmatched_adapted)],
            })
        return {
            "version": CHANGE_DETECTION_VERSION,
            "status": "UNRESOLVED" if unresolved else "RESOLVED",
            "changes": changes,
            "unresolved_changes": unresolved,
        }

    @staticmethod
    def _change(change_type, original, adapted, *, identity_unchanged):
        return {
            "change_type": change_type,
            "identity_unchanged": identity_unchanged,
            "original_position": original["position"] if original else None,
            "adapted_position": adapted["position"] if adapted else None,
            "original_ingredient_id": original["stable_id"] if original else None,
            "adapted_ingredient_id": adapted["stable_id"] if adapted else None,
            "original_ingredient": original["name"] if original else None,
            "adapted_ingredient": adapted["name"] if adapted else None,
            "original_quantity": original["quantity_raw"] if original else None,
            "adapted_quantity": adapted["quantity_raw"] if adapted else None,
            "original_quantity_g": original["quantity_g"] if original else None,
            "adapted_quantity_g": adapted["quantity_g"] if adapted else None,
            "original_unit": original["unit"] if original else None,
            "adapted_unit": adapted["unit"] if adapted else None,
            "original_attributes": original["attributes"] if original else {},
            "adapted_attributes": adapted["attributes"] if adapted else {},
        }


__all__ = ["CHANGE_DETECTION_VERSION", "RecipeChangeDetector"]

"""Reviewed USDA reference records and context-aware default policy.

Preferred references are a separate, explicitly reviewed data product from
``IngredientMapping``. A reference may supply a nutritional calculation
default only when its current review and default permission are both present.
"""

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone


# Version 2 adds farm-raised salmon and rice enrichment to identity-critical
# reference scope; older approvals cannot silently omit those distinctions.
REFERENCE_POLICY_VERSION = 2
REFERENCE_STATUSES = {"APPROVED", "REJECTED"}
REFERENCE_CATEGORIES = {
    "salt", "egg", "butter", "sugar", "milk", "olive oil",
    "chicken breast", "salmon", "rice",
}

_CATEGORY_REQUIREMENTS = {
    "salt": ("salt_type",),
    "egg": ("egg_part", "species", "preparation_state"),
    "butter": ("saltedness", "processing_state"),
    "sugar": ("sugar_type",),
    "milk": ("species", "fat_content", "flavor", "processing_state"),
    "olive oil": ("oil_type", "processing_state"),
    "chicken breast": ("preparation_state", "skin_state", "bone_state", "added_solution"),
    "salmon": ("species", "farm_raised", "preparation_state", "processing_state"),
    "rice": ("variety", "grain", "enrichment_state", "preparation_state"),
}
_STATES = {
    "raw", "uncooked", "cooked", "smoked", "dried", "canned", "frozen",
    "boiled", "grilled", "baked", "fried", "roasted", "steamed", "poached",
    "fermented", "pickled", "cured", "kippered", "fresh",
}
_PROCESSING = {
    "smoked", "dried", "canned", "frozen", "fermented", "pickled", "cured",
    "kippered", "powder", "powdered", "dry", "condensed", "evaporated",
    "clarified", "whipped",
}
_SALMON_SPECIES = {"atlantic", "sockeye", "pink", "chinook", "king", "coho", "chum", "steelhead"}
_MILK_SPECIES = {"cow", "bovine", "sheep", "goat", "buffalo", "camel", "human"}
_EGG_SPECIES = {"chicken", "duck", "goose", "quail", "turkey"}
_RICE_VARIETIES = {"white", "brown", "wild", "red", "black"}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _words(value):
    return set(re.findall(r"[a-z0-9]+", str(value or "").casefold()))


def _parts(ingredient):
    if not isinstance(ingredient, dict):
        raise TypeError("An ingredient mapping is required.")
    nested = ingredient.get("parsed")
    parsed = nested if isinstance(nested, dict) else ingredient
    outer = ingredient if parsed is not ingredient else {}
    name = (parsed.get("ingredient_name") or outer.get("name")
            or parsed.get("original_text") or parsed.get("normalized_name")
            or outer.get("normalized_name") or "")
    normalized = (parsed.get("normalized_name") or outer.get("normalized_name") or name)
    return parsed, outer, str(name), str(normalized)


def canonical_ingredient_name(ingredient):
    """Map supported ingredient wording to a broad canonical category."""
    if isinstance(ingredient, dict):
        _, _, name, normalized = _parts(ingredient)
        tokens = _words(name) | _words(normalized)
    else:
        tokens = _words(ingredient)
    if "oil" in tokens and "salmon" in tokens:
        return None
    if "butter" in tokens and tokens & {"peanut", "almond", "cashew", "hazelnut", "sesame", "sunflower", "seed", "nut", "cocoa"}:
        return None
    if "salt" in tokens and (
            {"salt", "substitute"}.issubset(tokens)
            or {"potassium", "chloride"}.issubset(tokens)):
        return None
    if "milk" in tokens and tokens & {"almond", "cashew", "coconut", "hemp", "oat", "rice", "soy", "soya"}:
        return None
    if "egg" in tokens and tokens & {"noodle", "noodles", "bread", "bagel", "bagels"}:
        return None
    if "rice" in tokens and tokens & {"noodle", "noodles", "flour"}:
        return None
    if {"olive", "oil"}.issubset(tokens):
        return "olive oil"
    if "chicken" in tokens and "breast" in tokens:
        return "chicken breast"
    for category in ("egg", "butter", "sugar", "milk", "salmon", "rice", "salt"):
        if category in tokens or (category == "egg" and "eggs" in tokens):
            return category
    if "oil" in tokens:
        return "oil"
    if "chicken" in tokens:
        return "chicken"
    return None


_ATTRIBUTE_KEY_ALIASES = {
    "food state": "preparation_state", "state": "preparation_state",
    "preparation state": "preparation_state", "processing": "processing_state",
    "processing state": "processing_state", "fat": "fat_content",
    "fat percentage": "fat_content", "milk fat": "fat_content",
    "milkfat": "fat_content", "rice variety": "variety",
    "species or variety": "species", "salt status": "saltedness",
    "salted": "saltedness", "salt type": "salt_type", "oil type": "oil_type",
    "sweetened": "sweetenedness", "sweetenedness": "sweetenedness",
    "part": "egg_part", "egg part": "egg_part", "grain size": "grain",
    "farm raised": "farm_raised", "farm raised status": "farm_raised",
    "enrichment": "enrichment_state", "enrichment state": "enrichment_state",
    "skin": "skin_state", "bone": "bone_state", "solution": "added_solution",
}


def _normalize_attribute_map(attributes):
    normalized = {}
    if not isinstance(attributes, dict):
        return normalized
    for raw_key, raw_value in attributes.items():
        key = " ".join(re.findall(r"[a-z0-9]+", str(raw_key).casefold()))
        key = _ATTRIBUTE_KEY_ALIASES.get(key, key.replace(" ", "_"))
        if raw_value is None:
            continue
        values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
        clean = sorted({_canonical_attribute_value(key, value)
                        for value in values if str(value).strip()})
        if clean:
            normalized[key] = clean[0] if len(clean) == 1 else clean
    return normalized


def _canonical_attribute_value(key, value):
    text = " ".join(re.findall(r"[a-z0-9.%]+", str(value).casefold())).strip()
    if key in {"farm_raised", "added_solution"}:
        if text in {"yes", "true", "1", "farm raised", "with solution", "added solution"}:
            return "yes"
        if text in {"no", "false", "0", "wild", "wild caught", "without solution", "no solution", "unenhanced"}:
            return "no"
    if text in {"unknown", "not stated", "unspecified"}:
        return "unspecified"
    if key == "preparation_state" and text == "uncooked":
        return "raw"
    if key == "saltedness":
        if text in {"unsalted", "without salt", "no salt", "salt free"}:
            return "unsalted"
        if text in {"salted", "with salt"}:
            return "salted"
    if key == "fat_content" and text in {"lowfat", "reducedfat"}:
        return text.replace("fat", " fat")
    return text


def extract_explicit_ingredient_attributes(ingredient, attributes=None):
    """Extract only details stated in the source ingredient or structured input."""
    parsed, outer, name, normalized = _parts(ingredient)
    text = f"{name} {normalized}"
    tokens = _words(text)
    result = {}
    if isinstance(parsed.get("attributes"), dict):
        result.update(_normalize_attribute_map(parsed["attributes"]))
    if isinstance(attributes, dict):
        result.update(_normalize_attribute_map(attributes))
    if isinstance(outer.get("attributes"), dict):
        result.update(_normalize_attribute_map(outer["attributes"]))

    states = sorted(tokens & _STATES)
    if "uncooked" in states:
        states = sorted((set(states) - {"uncooked"}) | {"raw"})
    if states and "preparation_state" not in result:
        result["preparation_state"] = states[0] if len(states) == 1 else states
    processing = sorted(tokens & _PROCESSING)
    if processing and "processing_state" not in result:
        result["processing_state"] = processing[0] if len(processing) == 1 else processing

    category = canonical_ingredient_name(ingredient)
    if category == "butter" and "saltedness" not in result:
        if "unsalted" in tokens or {"without", "salt"}.issubset(tokens) or {"no", "salt"}.issubset(tokens):
            result["saltedness"] = "unsalted"
        elif "salted" in tokens or {"with", "salt"}.issubset(tokens):
            result["saltedness"] = "salted"
    if category == "milk":
        if "species" not in result:
            species = tokens & _MILK_SPECIES
            if species:
                result["species"] = sorted(species)[0]
        if "fat_content" not in result:
            match = re.search(r"\b(\d+(?:\.\d+)?)\s*%", text.casefold())
            if match:
                result["fat_content"] = match.group(1) + "%"
            elif "whole" in tokens:
                result["fat_content"] = "whole"
            elif "nonfat" in tokens or "skim" in tokens:
                result["fat_content"] = "nonfat" if "nonfat" in tokens else "skim"
            elif "lowfat" in tokens or {"low", "fat"}.issubset(tokens):
                result["fat_content"] = "low fat"
            elif "reducedfat" in tokens or {"reduced", "fat"}.issubset(tokens):
                result["fat_content"] = "reduced fat"
        if "chocolate" in tokens and "flavor" not in result:
            result["flavor"] = "chocolate"
        if "unsweetened" in tokens and "sweetenedness" not in result:
            result["sweetenedness"] = "unsweetened"
        elif "sweetened" in tokens and "sweetenedness" not in result:
            result["sweetenedness"] = "sweetened"
    if category == "salmon" and "species" not in result:
        species = tokens & _SALMON_SPECIES
        if species:
            result["species"] = sorted(species)[0]
    if category == "salmon" and "farm_raised" not in result:
        if {"farm", "raised"}.issubset(tokens):
            result["farm_raised"] = "yes"
        elif "wild" in tokens:
            result["farm_raised"] = "no"
    if category == "egg":
        if "egg_part" not in result:
            parts = tokens & {"whole", "white", "yolk"}
            if parts:
                result["egg_part"] = sorted(parts)[0]
        if "species" not in result:
            species = tokens & _EGG_SPECIES
            if species:
                result["species"] = sorted(species)[0]
    if category == "rice":
        if "variety" not in result:
            varieties = tokens & _RICE_VARIETIES
            if varieties:
                result["variety"] = sorted(varieties)[0]
        if "grain" not in result:
            grains = tokens & {"long", "medium", "short"}
            if grains:
                result["grain"] = sorted(grains)[0]
        if "enrichment_state" not in result:
            if "unenriched" in tokens:
                result["enrichment_state"] = "unenriched"
            elif "enriched" in tokens:
                result["enrichment_state"] = "enriched"
    if category == "olive oil" and "oil_type" not in result:
        result["oil_type"] = "olive"
    if category == "salt" and "salt_type" not in result:
        types = tokens & {"table", "kosher", "sea", "rock", "iodized", "seasoned"}
        if types:
            result["salt_type"] = sorted(types)[0]
    if category == "sugar" and "sugar_type" not in result:
        types = tokens & {"brown", "white", "granulated", "cane", "turbinado", "raw", "maple"}
        if types:
            result["sugar_type"] = sorted(types)[0]
    if category == "chicken breast":
        if "skin_state" not in result:
            if "skinless" in tokens:
                result["skin_state"] = "skinless"
            elif "skin" in tokens:
                result["skin_state"] = "with skin"
        if "bone_state" not in result:
            if "boneless" in tokens:
                result["bone_state"] = "boneless"
            elif "bone" in tokens:
                result["bone_state"] = "bone-in"
        if "added_solution" not in result:
            if {"added", "solution"}.issubset(tokens) or {"with", "solution"}.issubset(tokens):
                result["added_solution"] = "yes"
            elif "unenhanced" in tokens or {"without", "solution"}.issubset(tokens) or {"no", "solution"}.issubset(tokens):
                result["added_solution"] = "no"
    return result


def _reference_attributes(reference):
    attrs = _normalize_attribute_map(reference.get("attributes") or {})
    for key in ("preparation_state", "processing_state", "species", "variety", "grain",
                "saltedness", "salt_type", "sugar_type", "fat_content", "flavor",
                "oil_type", "egg_part", "skin_state", "bone_state", "added_solution",
                "farm_raised", "enrichment_state"):
        if reference.get(key) is not None and key not in attrs:
            attrs[key] = _normalize_attribute_map({key: reference[key]}).get(key)
    return attrs


def _as_value(value):
    return tuple(sorted(value)) if isinstance(value, list) else str(value).casefold()


class ReferenceDefaultPolicy:
    """Require a reviewed, current reference and explicit permission to default."""

    VERSION = REFERENCE_POLICY_VERSION

    def evaluate(self, ingredient, reference, *, attributes=None, allow_defaults=True):
        category = canonical_ingredient_name(ingredient)
        explicit = extract_explicit_ingredient_attributes(ingredient, attributes)
        reference = reference if isinstance(reference, dict) else {}
        reference_category = reference.get("canonical_ingredient_name") or reference.get("canonical_ingredient")
        if category not in REFERENCE_CATEGORIES or reference_category != category:
            return self._decision(False, "INCOMPATIBLE", category, explicit,
                                  reasons=["The preferred reference is for a different ingredient identity."])
        if reference.get("reference_status") != "APPROVED":
            return self._decision(False, "UNAVAILABLE", category, explicit,
                                  reasons=["The reference has not been approved."])
        if reference.get("reference_policy_version") != self.VERSION:
            return self._decision(False, "STALE_POLICY", category, explicit,
                                  reasons=["The reference was reviewed under a different policy version."])

        ref_attrs = _reference_attributes(reference)
        conflicts = []
        for key, expected in explicit.items():
            actual = ref_attrs.get(key)
            if actual is None or _as_value(actual) != _as_value(expected):
                conflicts.append(key)
        if conflicts:
            return self._decision(False, "INCOMPATIBLE", category, explicit,
                                  unresolved=conflicts,
                                  reasons=["The reference does not match explicit ingredient attributes: "
                                           + ", ".join(sorted(conflicts)) + "."])

        assumed = {}
        unresolved = []
        for key in _CATEGORY_REQUIREMENTS[category]:
            if key in explicit:
                continue
            value = ref_attrs.get(key)
            if value is None:
                unresolved.append(key)
            else:
                assumed[key] = value
        if unresolved:
            return self._decision(False, "INCOMPLETE_REFERENCE", category, explicit,
                                  assumed=assumed, unresolved=unresolved,
                                  reasons=["The reviewed reference does not resolve all identity-critical attributes."])
        if assumed and (not allow_defaults or reference.get("allow_as_default") is not True):
            return self._decision(False, "DEFAULT_NOT_PERMITTED", category, explicit,
                                  assumed=assumed, unresolved=list(assumed),
                                  reasons=["The reference is compatible, but default use is not explicitly permitted."])
        return self._decision(True, "REFERENCE_DEFAULT", category, explicit,
                              assumed=assumed, unresolved=list(assumed), reasons=[])

    @staticmethod
    def _decision(eligible, status, category, explicit, *, assumed=None, unresolved=None, reasons):
        return {
            "eligible": bool(eligible), "status": status,
            "canonical_ingredient_name": category,
            "explicit_attributes": explicit,
            "assumed_attributes": assumed or {},
            "unresolved_attributes": sorted(set(unresolved or [])),
            "policy_version": REFERENCE_POLICY_VERSION,
            "reasons": reasons,
        }


class PreferredUSDAReferenceManager:
    """Manage explicitly reviewed references through the supplied Neo4j store."""

    def __init__(self, kg, *, policy_version=REFERENCE_POLICY_VERSION):
        self.kg = kg
        self.policy_version = int(policy_version)

    @staticmethod
    def _identity(ingredient, attributes, fdc_id, food_record_id):
        payload = json.dumps({
            "canonical": ingredient, "attributes": attributes,
            "fdc_id": str(fdc_id), "food_record_id": food_record_id,
        }, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def list_references(self, ingredient=None, *, include_rejected=False):
        canonical = canonical_ingredient_name(ingredient) if ingredient is not None else None
        rows = self.kg.list_ingredient_references(canonical)
        allowed = REFERENCE_STATUSES if include_rejected else {"APPROVED"}
        return [row for row in rows
                if row.get("reference_status") in allowed
                and row.get("reference_policy_version") == self.policy_version]

    def preferred_references(self, ingredient):
        category = canonical_ingredient_name(ingredient)
        if category not in REFERENCE_CATEGORIES:
            return []
        return self.list_references(category)

    def _local_food(self, fdc_id, food_record_id=None):
        if isinstance(fdc_id, bool) or not str(fdc_id or "").strip().isdigit() or int(fdc_id) <= 0:
            raise ValueError("A positive USDA FDC ID is required for a preferred reference.")
        matches = []
        for record_id, food in (getattr(self.kg, "ingredients", {}) or {}).items():
            source = food.get("source") or {}
            if str(source.get("fdc_id")) != str(fdc_id):
                continue
            if food_record_id is not None and str(record_id) != str(food_record_id):
                continue
            matches.append((record_id, food, source))
        if not matches:
            raise ValueError("The selected USDA FDC ID is not present in the locally imported catalog.")
        return sorted(matches, key=lambda item: str(item[0]))[0]

    def local_candidate(self, reference):
        """Build a candidate guard record only from the imported catalog projection."""
        try:
            record_id, food, source = self._local_food(
                reference.get("fdc_id"), reference.get("food_record_id"),
            )
        except ValueError:
            return None
        return {
            "fdc_id": source.get("fdc_id"),
            "food_record_id": str(record_id),
            "description": food.get("fdc_description") or food.get("name"),
            "food_state": food.get("food_state") or food.get("fdc_description") or food.get("name"),
            "approved_aliases": list(food.get("aliases") or []),
            "dataset_key": source.get("dataset_key"),
            "data_type": source.get("data_type"),
        }

    @staticmethod
    def _reviewer(reviewer_identity, provenance):
        reviewer = str(reviewer_identity or "").strip()
        if not reviewer:
            raise ValueError("An explicit reviewer identity is required.")
        if provenance is None or provenance == "" or provenance == {} or provenance == []:
            raise ValueError("Review provenance is required.")
        return reviewer

    def review_reference(self, ingredient, fdc_id, *, status, reviewer_identity,
                         provenance, attributes=None, allow_as_default=False,
                         rationale=None, food_record_id=None):
        """Append a review event and update one separate preferred-reference record."""
        category = canonical_ingredient_name(ingredient)
        if category not in REFERENCE_CATEGORIES:
            raise ValueError("This ingredient is not in the supported reference categories.")
        if status not in REFERENCE_STATUSES:
            raise ValueError("Reference status must be APPROVED or REJECTED.")
        reviewer = self._reviewer(reviewer_identity, provenance)
        if status == "APPROVED" and not isinstance(allow_as_default, bool):
            raise ValueError("allow_as_default must be an explicit boolean for approvals.")
        if status == "REJECTED" and allow_as_default:
            raise ValueError("A rejected reference cannot be enabled as a default.")
        record_id, food, source = self._local_food(fdc_id, food_record_id)
        reviewed_attributes = _normalize_attribute_map(attributes or {})
        ref_id = self._identity(category, reviewed_attributes, fdc_id, str(record_id))
        now = _now()
        reference = {
            "reference_id": ref_id,
            "canonical_ingredient_name": category,
            "fdc_id": source.get("fdc_id"),
            "fdc_description": food.get("fdc_description") or food.get("name"),
            "food_record_id": str(record_id),
            "dataset_key": source.get("dataset_key"),
            "data_type": source.get("data_type"),
            "attributes": reviewed_attributes,
            "preparation_state": reviewed_attributes.get("preparation_state"),
            "processing_state": reviewed_attributes.get("processing_state"),
            "species": reviewed_attributes.get("species"),
            "variety": reviewed_attributes.get("variety"),
            "farm_raised": reviewed_attributes.get("farm_raised"),
            "enrichment_state": reviewed_attributes.get("enrichment_state"),
            "reference_status": status,
            "allow_as_default": allow_as_default if status == "APPROVED" else False,
            "reviewer_identity": reviewer,
            "review_provenance": provenance,
            "review_rationale": str(rationale or "").strip() or None,
            "reference_policy_version": self.policy_version,
            "updated_at": now,
        }
        event = {
            "event_id": uuid.uuid4().hex,
            "reference_id": ref_id,
            "canonical_ingredient_name": category,
            "fdc_id": source.get("fdc_id"),
            "action": status,
            "reviewer_identity": reviewer,
            "provenance": provenance,
            "rationale": str(rationale or "").strip() or None,
            "reference_policy_version": self.policy_version,
            "created_at": now,
        }
        stored = self.kg.save_ingredient_reference_decision(reference, event)
        if not stored or stored.get("reference_status") != status:
            raise RuntimeError("The reviewed USDA reference decision was not confirmed by the store.")
        return stored

    def approve_reference(self, ingredient, fdc_id, **kwargs):
        kwargs["status"] = "APPROVED"
        return self.review_reference(ingredient, fdc_id, **kwargs)

    def reject_reference(self, ingredient, fdc_id, **kwargs):
        kwargs["status"] = "REJECTED"
        kwargs["allow_as_default"] = False
        return self.review_reference(ingredient, fdc_id, **kwargs)

    def record_review_action(self, action, ingredient, *, reviewer_identity,
                             provenance=None, context=None, details=None):
        if action not in {"REQUEST_ATTRIBUTES", "LEAVE_UNRESOLVED"}:
            raise ValueError("This method records REQUEST_ATTRIBUTES or LEAVE_UNRESOLVED actions only.")
        reviewer = str(reviewer_identity or "").strip()
        if not reviewer:
            raise ValueError("An explicit reviewer identity is required for review actions.")
        category = canonical_ingredient_name(ingredient)
        now = _now()
        event = {
            "event_id": uuid.uuid4().hex,
            "reference_id": None,
            "canonical_ingredient_name": category,
            "action": action,
            "reviewer_identity": reviewer,
            "provenance": provenance,
            "context": context or {},
            "details": details or {},
            "reference_policy_version": self.policy_version,
            "created_at": now,
        }
        return self.kg.save_ingredient_reference_review_action(event)

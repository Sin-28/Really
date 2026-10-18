"""Conservative Food.com ingredient parsing; no food identity or nutrient validation.

Volume and count weights require an explicit, source-labelled factor for the
*exact* normalized ingredient name and unit. No density or serving weight is
assumed by this module.
"""

import math
import re
import unicodedata
from dataclasses import dataclass
from fractions import Fraction


@dataclass(frozen=True)
class ConversionFactor:
    grams_per_unit: float
    source: str


WEIGHT_GRAMS = {
    "mg": 0.001, "g": 1.0, "kg": 1000.0,
    "oz": 28.349523125, "lb": 453.59237,
}
UNIT_ALIASES = {
    "mg": "mg", "milligram": "mg", "milligrams": "mg",
    "g": "g", "gram": "g", "grams": "g",
    "kg": "kg", "kilogram": "kg", "kilograms": "kg",
    "oz": "oz", "ounce": "oz", "ounces": "oz",
    "lb": "lb", "lbs": "lb", "pound": "lb", "pounds": "lb",
    "cup": "cup", "cups": "cup",
    "tbsp": "tbsp", "tablespoon": "tbsp", "tablespoons": "tbsp",
    "tsp": "tsp", "teaspoon": "tsp", "teaspoons": "tsp",
    "ml": "ml", "milliliter": "ml", "milliliters": "ml", "millilitre": "ml", "millilitres": "ml",
    "l": "l", "liter": "l", "liters": "l", "litre": "l", "litres": "l",
    "fl oz": "fl oz", "fluid ounce": "fl oz", "fluid ounces": "fl oz",
    "pint": "pint", "pints": "pint", "quart": "quart", "quarts": "quart",
    "gallon": "gallon", "gallons": "gallon", "stick": "stick", "sticks": "stick",
    "pinch": "pinch", "pinches": "pinch", "dash": "dash", "dashes": "dash",
    "clove": "clove", "cloves": "clove", "can": "can", "cans": "can",
    "slice": "slice", "slices": "slice", "piece": "piece", "pieces": "piece",
    "bunch": "bunch", "bunches": "bunch", "package": "package", "packages": "package",
}
_FRACTIONS = str.maketrans({"½": " 1/2", "¼": " 1/4", "¾": " 3/4",
                            "⅓": " 1/3", "⅔": " 2/3", "⅛": " 1/8",
                            "⅜": " 3/8", "⅝": " 5/8", "⅞": " 7/8"})
_NUMBER = r"(?:\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
_LEADING_QUANTITY = re.compile(
    rf"^\s*(?P<first>{_NUMBER})(?:\s*(?:-|–|—|to)\s*(?P<last>{_NUMBER}))?(?=\s|$)",
    re.IGNORECASE,
)
_UNITS = sorted(UNIT_ALIASES, key=len, reverse=True)
_UNIT_PATTERN = re.compile(r"^(?P<unit>" + "|".join(re.escape(unit) for unit in _UNITS) + r")\b", re.I)
_PER_ITEM = re.compile(r"(?:,\s*each\b|\bper\s+(?:piece|item|steak|breast|serving)\b)", re.I)


def _number(value):
    parts = value.split()
    if len(parts) == 2:
        result = int(parts[0]) + float(Fraction(parts[1]))
    elif "/" in value:
        result = float(Fraction(value))
    else:
        result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _expand_unicode_fractions(text):
    """Expand vulgar fraction glyphs while keeping mixed numbers readable."""
    expanded = text.translate(_FRACTIONS)
    output = []
    for char in expanded:
        if unicodedata.category(char) == "No":
            try:
                fraction = Fraction(unicodedata.numeric(char)).limit_denominator(100)
            except (TypeError, ValueError, ZeroDivisionError):
                output.append(char)
                continue
            if fraction.denominator != 1:
                output.append(f" {fraction.numerator}/{fraction.denominator}")
                continue
        output.append(char)
    return "".join(output)


def normalize_ingredient_name(name):
    """Normalize spelling conservatively, keeping source states and varieties."""
    normalized = " ".join(str(name or "").casefold().strip(" ,.").split())
    normalized = re.sub(r",\s*each$", "", normalized)
    if normalized == "salmon steaks":
        return "salmon"
    if normalized == "eggs":
        return "egg"
    return normalized


def parse_ingredient_text(text, conversion_factors=None):
    """Parse a combined ingredient line into JSON-compatible fields.

    ``conversion_factors`` maps ``(normalized_name, canonical_unit)`` to a
    :class:`ConversionFactor`. Factors are supplied by a future, verified data
    source; this parser ships with no guessed volume or count weights.
    """
    original = "" if text is None else str(text)
    working = re.sub(r"\s+", " ", _expand_unicode_fractions(original)).strip()
    match = _LEADING_QUANTITY.match(working)
    first = last = None
    remainder = working
    if match:
        try:
            first = _number(match.group("first"))
            last = _number(match.group("last")) if match.group("last") else None
        except (ValueError, ZeroDivisionError, OverflowError):
            first = last = None
        remainder = working[match.end():].strip()

    unit = None
    if match:
        unit_match = _UNIT_PATTERN.match(remainder)
        if unit_match:
            unit = UNIT_ALIASES[unit_match.group("unit").casefold()]
            remainder = remainder[unit_match.end():].strip()
            if remainder.casefold().startswith("of "):
                remainder = remainder[3:].strip()
        else:
            unit = "count"

    name = remainder if match else working
    normalized = normalize_ingredient_name(name)
    per_item = bool(_PER_ITEM.search(name))
    result = {
        "original_text": original,
        "ingredient_name": name,
        "normalized_name": normalized,
        "quantity": first if last is None else None,
        "quantity_min": first if last is not None else None,
        "quantity_max": last,
        "unit": unit,
        "quantity_g": None,
        "quantity_g_min": None,
        "quantity_g_max": None,
        "quantity_g_per_item": None,
        "quantity_scope": "PER_ITEM" if per_item else ("TOTAL" if match else "UNSPECIFIED"),
        "quantity_status": "UNRESOLVED",
        "conversion_source": None,
    }
    if not match or first is None or (last is not None and last < first):
        result["quantity_status"] = "UNPARSED" if match else "UNRESOLVED"
        return result
    if per_item:
        result["quantity_status"] = "UNRESOLVED_PER_ITEM"
        if unit in WEIGHT_GRAMS and last is None:
            result["quantity_g_per_item"] = first * WEIGHT_GRAMS[unit]
        return result

    grams_per_unit = WEIGHT_GRAMS.get(unit)
    if grams_per_unit is None and conversion_factors:
        factor = conversion_factors.get((normalized, unit))
        if (isinstance(factor, ConversionFactor)
                and math.isfinite(factor.grams_per_unit)
                and factor.grams_per_unit > 0
                and isinstance(factor.source, str) and factor.source.strip()):
            grams_per_unit = factor.grams_per_unit
            result["conversion_source"] = factor.source
    if grams_per_unit is None:
        result["quantity_status"] = "UNRESOLVED_RANGE" if last is not None else "UNRESOLVED"
    elif last is not None:
        result["quantity_g_min"] = first * grams_per_unit
        result["quantity_g_max"] = last * grams_per_unit
        result["quantity_status"] = "CONVERTED_RANGE"
    else:
        result["quantity_g"] = first * grams_per_unit
        result["quantity_status"] = "CONVERTED"
    return result


def parse_foodcom_ingredient(name, quantity_raw, conversion_factors=None):
    """Parse split Food.com fields without changing either raw source field."""
    name_text = "" if name is None else str(name)
    quantity_text = "" if quantity_raw is None else str(quantity_raw)
    combined = " ".join(part for part in (quantity_text, name_text) if part)
    parsed = parse_ingredient_text(combined, conversion_factors)
    # Source names are authoritative; a number in a name is not a quantity.
    parsed["ingredient_name"] = name_text
    parsed["normalized_name"] = normalize_ingredient_name(name_text)
    if not quantity_text.strip():
        parsed.update(quantity=None, quantity_min=None, quantity_max=None,
                      quantity_g=None, quantity_g_min=None, quantity_g_max=None,
                      quantity_g_per_item=None, quantity_scope="UNSPECIFIED",
                      unit=None, quantity_status="UNRESOLVED",
                      conversion_source=None)
    return parsed

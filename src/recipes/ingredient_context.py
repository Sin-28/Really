"""Small deterministic checks for context-sensitive USDA food identities."""

import re


_COLORS = {"red", "green", "yellow", "orange", "white", "black", "purple"}
_FAMILIES = {
    "cream": "cream", "flour": "flour", "oil": "oil", "salmon": "salmon",
    "onion": "onion", "onions": "onion", "chilli": "chilli",
    "chillies": "chilli", "chili": "chilli", "chilies": "chilli",
    "chile": "chilli", "chiles": "chilli",
}
_VARIANTS = {
    "dairy_milk": {"whole", "skim", "skimmed", "nonfat", "lowfat", "reduced", "2%", "1%"},
    "cream": {"heavy", "light", "sour", "whipping", "double", "single", "coconut"},
    "flour": {"wheat", "rice", "corn", "almond", "coconut", "whole", "enriched", "bleached",
              "all", "purpose", "bread", "cake", "semolina", "self", "rising"},
    "onion": _COLORS,
    "chilli": _COLORS,
}


def _tokens(value):
    return set(re.findall(r"[a-z0-9]+%?", str(value or "").casefold()))


def candidate_family(candidate):
    tokens = _tokens(candidate.get("description"))
    if "milk" in tokens:
        plant = tokens & {"almond", "soy", "oat", "coconut", "rice"}
        return f"{sorted(plant)[0]}_milk" if plant else "dairy_milk"
    if "pepper" in tokens or "peppers" in tokens:
        if "bell" in tokens or "capsicum" in tokens or "sweet" in tokens:
            return "bell_pepper"
        if tokens & {"chili", "chilli", "chile", "hot", "jalapeno", "habanero", "poblano", "serrano"}:
            return "chilli"
        if "black" in tokens or "white" in tokens or "spices" in tokens:
            return "pepper_spice"
        return "pepper"
    for token, family in _FAMILIES.items():
        if token in tokens:
            return family
    return None


def variant_gaps(ingredient, candidate, *, unit=None, recipe_context=None):
    """Report candidate specificity unsupported by the source or clear context."""
    source = _tokens(ingredient)
    candidate_tokens = _tokens(candidate.get("description"))
    family = candidate_family(candidate)
    gaps = set()
    if not family:
        return gaps
    if "milk" in source and family.endswith("_milk") and family != "dairy_milk":
        plant_type = family.removesuffix("_milk")
        if plant_type not in source:
            gaps.add("milk_type_conflict" if source & {"cow", "dairy"} else "milk_type")
    if family == "salmon" and "salmon" in source:
        source_origin = source & {"wild", "farm", "farmed"}
        candidate_origin = candidate_tokens & {"wild", "farm", "farmed"}
        if source_origin & {"farm", "farmed"}:
            source_origin = (source_origin - {"farm", "farmed"}) | {"farmed"}
        if candidate_origin & {"farm", "farmed"}:
            candidate_origin = (candidate_origin - {"farm", "farmed"}) | {"farmed"}
        if source_origin and candidate_origin and not source_origin.intersection(candidate_origin):
            gaps.add("salmon_origin_conflict")
        elif candidate_origin and not source_origin:
            gaps.add("salmon_origin")
    if family in {"bell_pepper", "chilli", "pepper_spice", "pepper"}:
        if "pepper" in source or "peppers" in source:
            context = recipe_context if isinstance(recipe_context, dict) else {}
            method = str(context.get("method") or context.get("instructions") or "").casefold()
            other = " ".join(str(value) for value in (context.get("ingredients") or []))
            evidence = " ".join((method, other, str(unit or ""))).casefold()
            source_family = ("bell_pepper" if "bell" in source or "capsicum" in source else
                             "chilli" if "chili" in source or "chilli" in source or "hot" in source else
                             "pepper_spice" if "ground" in source or "black" in source or "white" in source else None)
            if source_family is None:
                if re.search(r"\b(slic\w*|strip\w*)\b", evidence) and "tsp" not in evidence:
                    source_family = "bell_pepper"
                elif "salt and pepper" in evidence or "season with pepper" in evidence or "tsp" in evidence:
                    source_family = "pepper_spice"
            if source_family and family != source_family:
                gaps.add("pepper_family_conflict")
            elif source_family is None and family != "pepper":
                gaps.add("pepper_family")
        if family in {"bell_pepper", "chilli", "pepper_spice", "pepper"}:
            candidate_colors = candidate_tokens & _COLORS
            source_colors = source & _COLORS
            if candidate_colors and source_colors and not candidate_colors.intersection(source_colors):
                gaps.add("color_conflict")
            elif candidate_colors and not source_colors:
                # Common seasoning phrasing identifies black pepper without
                # explicitly naming the colour.
                if not (family == "pepper_spice" and "black" in candidate_tokens
                        and ("salt and pepper" in str(recipe_context or "").casefold()
                             or "ground" in source or "tsp" in evidence)):
                    gaps.add("color")
            if family == "pepper" and candidate_tokens & {"banana", "jalapeno", "habanero", "poblano", "serrano"} and not (source & candidate_tokens & {"banana", "jalapeno", "habanero", "poblano", "serrano"}):
                gaps.add("pepper_variety")
    variants = _VARIANTS.get(family, set())
    candidate_variants = candidate_tokens & variants
    source_variants = source & variants
    if candidate_variants and source_variants and not candidate_variants.intersection(source_variants):
        gaps.add("variant_conflict")
    elif candidate_variants - source_variants:
        gaps.add("variant")
    return gaps

"""Build a conservative, non-persistent proposal for initial USDA references."""

from collections import Counter

from src.recipes.ingredient_reference import REFERENCE_POLICY_VERSION
from src.recipes.reference_review_workflow import validate_approval_scope


INITIAL_REFERENCE_SELECTION = (
    {
        "category": "salt", "fdc_id": 173468, "description": "Salt, table",
        "scope": {"salt_type": "table"},
        "applies_to": ["table salt"],
        "excludes": ["generic salt without an approved default", "potassium-based salt substitutes", "seasoned salt", "other salt products"],
        "reasoning": "The local SR Legacy record explicitly identifies table salt and contains all four required nutrients.",
    },
    {
        "category": "egg", "fdc_id": 171287, "description": "Egg, whole, raw, fresh",
        "scope": {"egg_part": "whole", "species": "unspecified", "preparation_state": "raw"},
        "applies_to": ["raw whole egg with species left unspecified"],
        "excludes": ["egg whites", "egg yolks", "duck eggs", "cooked eggs"],
        "reasoning": "The local SR Legacy record identifies whole, raw egg but does not state species; the proposed scope preserves that uncertainty.",
    },
    {
        "category": "butter", "fdc_id": 173410, "description": "Butter, salted",
        "scope": {"saltedness": "salted", "processing_state": "unspecified"},
        "applies_to": ["salted dairy butter"],
        "excludes": ["unsalted butter", "nut butter", "ghee", "generic butter unless a reviewer explicitly permits that default"],
        "reasoning": "The source explicitly identifies salted butter. Its processing form is not stated and remains unspecified.",
    },
    {
        "category": "butter", "fdc_id": 173430, "description": "Butter, without salt",
        "scope": {"saltedness": "unsalted", "processing_state": "unspecified"},
        "applies_to": ["unsalted dairy butter"],
        "excludes": ["salted butter", "nut butter", "ghee", "generic butter unless a reviewer explicitly permits that default"],
        "reasoning": "The source explicitly identifies butter without salt. Its processing form is not stated and remains unspecified.",
    },
    {
        "category": "sugar", "fdc_id": 169655, "description": "Sugars, granulated",
        "scope": {"sugar_type": "granulated"},
        "applies_to": ["granulated sugar"],
        "excludes": ["brown sugar", "turbinado sugar", "artificial sweeteners"],
        "reasoning": "The local SR Legacy record explicitly identifies granulated sugar and contains all four required nutrients.",
    },
    {
        "category": "olive oil", "fdc_id": 171413, "description": "Oil, olive, salad or cooking",
        "scope": {"oil_type": "olive", "processing_state": "unspecified"},
        "applies_to": ["olive oil described as salad or cooking oil, with processing unspecified"],
        "excludes": ["explicit extra-virgin olive oil", "explicit extra-light olive oil", "blended oils"],
        "reasoning": "The local SR Legacy record is nutrient-complete but does not state all processing attributes; the narrower scope preserves that uncertainty.",
    },
    {
        "category": "chicken breast", "fdc_id": 2646170, "description": "Chicken, breast, boneless, skinless, raw",
        "scope": {"preparation_state": "raw", "skin_state": "skinless", "bone_state": "boneless", "added_solution": "unspecified"},
        "applies_to": ["raw boneless skinless chicken breast with solution status unspecified"],
        "excludes": ["cooked chicken", "skin-on chicken", "bone-in chicken", "chicken explicitly described with added solution"],
        "reasoning": "The local Foundation record establishes raw, boneless, skinless breast. It does not state solution status, so the proposal does not assume added_solution=no.",
    },
    {
        "category": "salmon", "fdc_id": 2684441,
        "description": "Fish, salmon, Atlantic, farm raised, raw",
        "scope": {"species": "atlantic", "farm_raised": "yes", "preparation_state": "raw", "processing_state": "unspecified"},
        "applies_to": ["raw farm-raised Atlantic salmon"],
        "excludes": ["wild Atlantic salmon", "sockeye salmon", "pink salmon", "cooked salmon", "smoked salmon", "generic salmon without an explicitly approved default"],
        "reasoning": "The local Foundation record states Atlantic, farm-raised, and raw. Farm-raising is preserved as a distinct identity attribute; processing beyond the stated raw form remains unspecified.",
    },
    {
        "category": "rice", "fdc_id": 2512381,
        "description": "Rice, white, long grain, unenriched, raw",
        "scope": {"variety": "white", "grain": "long", "enrichment_state": "unenriched", "preparation_state": "raw"},
        "applies_to": ["raw unenriched white long-grain rice"],
        "excludes": ["cooked rice", "brown rice", "glutinous rice", "enriched rice", "other grain varieties"],
        "reasoning": "The local Foundation record states white, long-grain, unenriched, and raw. Cooking state and enrichment are retained as identity attributes.",
    },
)

COMPARISON_IDS = {
    "butter": (173410, 173430),
    "salmon": (2684441, 173686),
    "chicken breast": (2646170, 171509),
    "rice": (2512381, 169757),
    "olive oil": (171413, 748608, 1750351),
}

_NUTRIENTS = (
    ("sodium", "1093", "mg"),
    ("potassium", "1092", "mg"),
    ("phosphorus", "1091", "mg"),
    ("protein", "1003", "g"),
)
_MASS_UNITS = {"mg", "milligram", "milligrams", "g", "gram", "grams", "µg", "μg", "mcg", "microgram", "micrograms"}


def _valid_number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _nutrient_validation(entry, name, fdc_nutrient_id, expected_unit):
    row = (entry.get("nutrients_per_100g") or {}).get(name) or {}
    result = {
        "fdc_nutrient_id": row.get("fdc_nutrient_id"),
        "name": row.get("name"),
        "amount": row.get("amount"),
        "unit": row.get("unit"),
        "basis": row.get("basis") or entry.get("nutrition_basis"),
        "normalized_amount_per_100g": row.get("normalized_amount_per_100g"),
        "normalized_unit": row.get("normalized_unit"),
    }
    if not row or row.get("availability") == "MISSING" or row.get("amount") is None:
        result["status"] = "MISSING_NUTRIENT"
    elif str(row.get("fdc_nutrient_id")) != fdc_nutrient_id:
        result["status"] = "INVALID_NUTRIENT_ID"
    elif result["basis"] != "per 100 g":
        result["status"] = "INVALID_NUTRITION_BASIS"
    elif row.get("availability") == "UNSUPPORTED_UNIT":
        result["status"] = "UNSUPPORTED_UNIT"
    elif row.get("availability") != "AVAILABLE":
        result["status"] = "UNSUPPORTED_UNIT"
    elif str(row.get("unit") or "").strip().casefold().replace("μ", "µ") not in _MASS_UNITS:
        result["status"] = "UNSUPPORTED_UNIT"
    elif (not _valid_number(row.get("amount"))
          or not _valid_number(row.get("normalized_amount_per_100g"))
          or row.get("normalized_unit") != expected_unit):
        result["status"] = "UNSUPPORTED_UNIT"
    else:
        result["status"] = "AVAILABLE"
    return result


def _comparison_value(row, nutrient):
    item = (row.get("nutrients_per_100g") or {}).get(nutrient) or {}
    if item.get("availability") != "AVAILABLE":
        return None
    return item.get("normalized_amount_per_100g")


def _format_comparison(value):
    return "missing" if value is None else f"{value:g}"


def _display_nutrient(value):
    return "—" if value is None else f"{value:g}"


def build_initial_reference_proposal(manifest, *, additional_comparisons=(),
                                     existing_approved_references=()):
    """Select the nine named candidates, validate source data, and keep decisions pending."""
    entries_by_id = {}
    for entry in manifest.get("entries", []):
        entries_by_id.setdefault(int(entry.get("proposed_fdc_id") or 0), []).append(entry)
    for entry in additional_comparisons:
        entries_by_id.setdefault(int(entry.get("proposed_fdc_id") or 0), []).append(entry)

    selected, problems = [], []
    for selection in INITIAL_REFERENCE_SELECTION:
        matches = entries_by_id.get(selection["fdc_id"], [])
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one local manifest entry for FDC ID {selection['fdc_id']}.")
        source = matches[0]
        if source.get("canonical_ingredient_category") != selection["category"]:
            raise ValueError(f"FDC ID {selection['fdc_id']} has an unexpected manifest category.")
        if source.get("usda_description") != selection["description"]:
            raise ValueError(f"FDC ID {selection['fdc_id']} has an unexpected USDA description.")

        nutrients = {
            name: _nutrient_validation(source, name, nutrient_id, unit)
            for name, nutrient_id, unit in _NUTRIENTS
        }
        incomplete = [name for name, row in nutrients.items() if row["status"] != "AVAILABLE"]
        scope_error = None
        try:
            validated_scope = validate_approval_scope(
                selection["category"], selection["description"], selection["scope"],
            )
        except ValueError as exc:
            scope_error = str(exc)
            validated_scope = selection["scope"]

        if source.get("identity_compatibility") == "incompatible":
            recommendation = "NOT_RECOMMENDED"
            reasoning = "The generated Step 4.8 candidate is explicitly marked incompatible with its search context."
        elif incomplete:
            recommendation = "INSUFFICIENT_NUTRIENT_DATA"
            reasoning = "Required local nutrient evidence is incomplete or invalid for: " + ", ".join(incomplete) + "."
        elif scope_error:
            recommendation = "REQUIRES_SCOPE_REFINEMENT"
            reasoning = scope_error
        else:
            recommendation = "READY_FOR_HUMAN_APPROVAL"
            reasoning = selection["reasoning"]

        if recommendation == "READY_FOR_HUMAN_APPROVAL":
            problems.append(None)
        else:
            problems.append(recommendation)
        selected.append({
            "review_entry_id": source["review_entry_id"],
            "canonical_ingredient_category": selection["category"],
            "proposed_fdc_id": selection["fdc_id"],
            "food_record_id": source["food_record_id"],
            "usda_description": source["usda_description"],
            "usda_data_type": source.get("usda_data_type"),
            "dataset_key": source.get("dataset_key"),
            "nutrition_basis": source.get("nutrition_basis"),
            "reference_policy_version": REFERENCE_POLICY_VERSION,
            "source_reference_policy_version": manifest.get("reference_policy_version"),
            "identity_compatibility": source.get("identity_compatibility"),
            "proposed_reference_scope": selection["applies_to"],
            "scope_attributes": validated_scope,
            "scope_exclusions": selection["excludes"],
            "missing_attributes": list(source.get("candidate_description_missing_attributes") or []),
            "scope_unknown_attributes": sorted(
                key for key, value in validated_scope.items() if value == "unspecified"
            ),
            "nutritional_completeness": "COMPLETE" if not incomplete else "INCOMPLETE",
            "nutrient_validation": nutrients,
            "nutrients_per_100g": source.get("nutrients_per_100g", {}),
            "review_recommendation": recommendation,
            "reasoning": reasoning,
            "required_human_decision": (
                "Confirm or reject this exact FDC ID and scope. If approving, explicitly choose whether it may be used "
                "as a default; generic use remains disabled unless the reviewer separately opts in."
            ),
            "decision": "PENDING_HUMAN_CONFIRMATION",
            "review_status": "PENDING_REVIEW",
            "reviewer_identity": None,
            "approval_timestamp": None,
            "approval_provenance": None,
            "allow_as_default": False,
        })

    all_rows = list(manifest.get("entries", [])) + list(additional_comparisons)
    comparison_entries = {}
    for category, fdc_ids in COMPARISON_IDS.items():
        comparison_entries[category] = []
        for fdc_id in fdc_ids:
            match = next((entry for entry in all_rows if int(entry.get("proposed_fdc_id") or 0) == fdc_id), None)
            if match is None:
                raise ValueError(f"Missing local comparison record FDC ID {fdc_id} for {category}.")
            comparison_entries[category].append(match)

    recommendation_counts = dict(Counter(row["review_recommendation"] for row in selected))
    for label in ("READY_FOR_HUMAN_APPROVAL", "REQUIRES_SCOPE_REFINEMENT",
                  "INSUFFICIENT_NUTRIENT_DATA", "NOT_RECOMMENDED"):
        recommendation_counts.setdefault(label, 0)
    return {
        "proposal_version": 1,
        "source_manifest_version": manifest.get("manifest_version"),
        "reference_policy_version": REFERENCE_POLICY_VERSION,
        "source_manifest_reference_policy_version": manifest.get("reference_policy_version"),
        "persistent_database_writes": False,
        "actually_approved_count": 0,
        "preexisting_approved_reference_count": len(list(existing_approved_references)),
        "entries": selected,
        "recommendation_counts": recommendation_counts,
        "comparisons": comparison_entries,
    }


def render_initial_reference_proposal(proposal):
    """Render the decision proposal as a grouped human-review document."""
    lines = [
        "# Initial USDA Reference Approval Proposal",
        "",
        f"Reference-policy version: {proposal.get('reference_policy_version')}",
        f"Source Step 4.8 manifest policy version: {proposal.get('source_manifest_reference_policy_version')}",
        f"Candidates reviewed: {len(proposal.get('entries', []))}",
        f"Ready for human approval: {proposal.get('recommendation_counts', {}).get('READY_FOR_HUMAN_APPROVAL', 0)}; "
        f"requiring scope refinement: {proposal.get('recommendation_counts', {}).get('REQUIRES_SCOPE_REFINEMENT', 0)}; "
        f"insufficient nutrient data: {proposal.get('recommendation_counts', {}).get('INSUFFICIENT_NUTRIENT_DATA', 0)}; "
        f"not recommended: {proposal.get('recommendation_counts', {}).get('NOT_RECOMMENDED', 0)}.",
        "Actual approvals: 0. Persistent database writes: 0.",
        f"Pre-existing approved references for the selected FDC IDs: {proposal.get('preexisting_approved_reference_count', 0)}.",
        "",
        "These are recommendations for a human reviewer. No entry is approved, and none may be used as a generic default unless a reviewer explicitly grants that permission in a later decision.",
        "Nutrients use local USDA FDC IDs 1093 (sodium), 1092 (potassium), 1091 (phosphorus), and 1003 (protein). Values are per 100 g. Recorded zero remains distinct from a missing measurement.",
        "",
    ]
    categories = ("READY_FOR_HUMAN_APPROVAL", "REQUIRES_SCOPE_REFINEMENT",
                  "INSUFFICIENT_NUTRIENT_DATA", "NOT_RECOMMENDED")
    labels = {
        "READY_FOR_HUMAN_APPROVAL": "1. Ready for human approval",
        "REQUIRES_SCOPE_REFINEMENT": "2. Requires scope refinement",
        "INSUFFICIENT_NUTRIENT_DATA": "3. Requires additional nutrient data",
        "NOT_RECOMMENDED": "4. Not recommended",
    }
    entries = proposal.get("entries", [])
    for recommendation in categories:
        lines.extend([f"## {labels[recommendation]}", ""])
        rows = [entry for entry in entries if entry["review_recommendation"] == recommendation]
        if not rows:
            lines.extend(["None.", ""])
            continue
        for entry in rows:
            lines.extend([
                f"### {entry['canonical_ingredient_category'].title()} — FDC {entry['proposed_fdc_id']}",
                "",
                f"- USDA description: {entry['usda_description']}",
                f"- Dataset: {entry.get('usda_data_type') or 'unknown'} (`{entry.get('dataset_key') or 'unknown'}`)",
                f"- Nutrients per 100 g: sodium {_display_nutrient(entry['nutrient_validation']['sodium']['normalized_amount_per_100g'])} mg; "
                f"potassium {_display_nutrient(entry['nutrient_validation']['potassium']['normalized_amount_per_100g'])} mg; "
                f"phosphorus {_display_nutrient(entry['nutrient_validation']['phosphorus']['normalized_amount_per_100g'])} mg; "
                f"protein {_display_nutrient(entry['nutrient_validation']['protein']['normalized_amount_per_100g'])} g.",
                f"- Proposed reference scope: {'; '.join(entry['proposed_reference_scope'])}.",
                f"- Scope attributes: `{entry['scope_attributes']}`.",
                f"- Attributes missing from the USDA description: {', '.join(entry['missing_attributes']) or 'none'}.",
                f"- Scope attributes deliberately left unspecified: {', '.join(entry['scope_unknown_attributes']) or 'none'}.",
                f"- Nutritional completeness: {entry['nutritional_completeness']}.",
                f"- Recommendation: `{entry['review_recommendation']}`. {entry['reasoning']}",
                f"- Required human decision: {entry['required_human_decision']}",
                "- Decision: `PENDING_HUMAN_CONFIRMATION`.",
                "",
            ])
    lines.extend(["## Nutritional and identity comparisons", ""])
    for category in ("butter", "salmon", "chicken breast", "rice", "olive oil"):
        lines.extend([f"### {category.title()}", ""])
        rows = proposal.get("comparisons", {}).get(category, [])
        lines.extend([
            "| FDC ID | Dataset | USDA description | Sodium mg | Potassium mg | Phosphorus mg | Protein g | Completeness |",
            "|---:|---|---|---:|---:|---:|---:|---|",
        ])
        for row in rows:
            nutrients = row.get("nutrients_per_100g", {})
            values = []
            for name in ("sodium", "potassium", "phosphorus", "protein"):
                item = nutrients.get(name, {})
                value = item.get("normalized_amount_per_100g")
                values.append("—" if item.get("availability") != "AVAILABLE" or value is None
                              else _display_nutrient(value))
            complete = all(nutrients.get(name, {}).get("availability") == "AVAILABLE"
                           for name in ("sodium", "potassium", "phosphorus", "protein"))
            lines.append(
                f"| {row.get('proposed_fdc_id')} | {row.get('usda_data_type')} | {row.get('usda_description')} "
                f"| {' | '.join(values)} | {'complete' if complete else 'incomplete'} |"
            )
        lines.append("")

    comparisons = proposal.get("comparisons", {})
    butter = {row.get("proposed_fdc_id"): row for row in comparisons.get("butter", [])}
    butter_salted = _comparison_value(butter.get(173410, {}), "sodium")
    butter_unsalted = _comparison_value(butter.get(173430, {}), "sodium")
    butter_delta = (butter_salted - butter_unsalted
                    if butter_salted is not None and butter_unsalted is not None else None)
    salmon = {row.get("proposed_fdc_id"): row for row in comparisons.get("salmon", [])}
    chicken = {row.get("proposed_fdc_id"): row for row in comparisons.get("chicken breast", [])}
    olive = comparisons.get("olive oil", [])
    olive_selected_complete = all(_comparison_value(
        next((row for row in olive if row.get("proposed_fdc_id") == 171413), {}), name,
    ) is not None for name in ("sodium", "potassium", "phosphorus", "protein"))
    foundation_oils = [row for row in olive if row.get("usda_data_type") == "Foundation"]
    foundation_oils_missing = all(all(_comparison_value(row, name) is None
                                      for name in ("sodium", "potassium", "phosphorus", "protein"))
                                  for row in foundation_oils)

    lines.extend([
        f"- **Butter:** salted and unsalted records contain {_format_comparison(butter_salted)} mg and {_format_comparison(butter_unsalted)} mg sodium per 100 g (difference {_format_comparison(butter_delta)} mg); retain separate references.",
        f"- **Salmon:** farm-raised Atlantic vs wild Atlantic records have potassium {_format_comparison(_comparison_value(salmon.get(2684441, {}), 'potassium'))} mg vs {_format_comparison(_comparison_value(salmon.get(173686, {}), 'potassium'))} mg, phosphorus {_format_comparison(_comparison_value(salmon.get(2684441, {}), 'phosphorus'))} mg vs {_format_comparison(_comparison_value(salmon.get(173686, {}), 'phosphorus'))} mg, and protein {_format_comparison(_comparison_value(salmon.get(2684441, {}), 'protein'))} g vs {_format_comparison(_comparison_value(salmon.get(173686, {}), 'protein'))} g. Preserve farm-raised status; these records also come from different datasets, so this is not a controlled causal comparison.",
        f"- **Chicken breast:** the raw added-solution alternative has {_format_comparison(_comparison_value(chicken.get(171509, {}), 'sodium'))} mg sodium per 100 g versus {_format_comparison(_comparison_value(chicken.get(2646170, {}), 'sodium'))} mg for the selected record. The selected USDA description does not establish solution status; it stays unspecified and cannot match an explicit added-solution ingredient. The records are from different USDA datasets, so the difference is not attributable only to solution treatment.",
        "- **Rice:** compare the selected raw, unenriched white long-grain record with the local cooked, unenriched white long-grain record. Their per-100-g values reflect different water content and source datasets; never reuse one preparation state as the other.",
        f"- **Olive oil:** the SR Legacy salad/cooking record has {'all four required nutrients' if olive_selected_complete else 'incomplete required nutrient data'}. The local Foundation extra-virgin and extra-light records have {'all four nutrients missing' if foundation_oils_missing else 'mixed nutrient availability'}. The incomplete Foundation records are not preferred just because they are Foundation; the selected SR Legacy scope leaves processing unspecified and is not an exact substitute for either explicit Foundation variant.",
        "",
        "## Decision and implementation boundary",
        "",
        "The nine machine-readable entries preserve the original Step 4.8 review-entry IDs and carry `decision: PENDING_HUMAN_CONFIRMATION`. Reviewer identity, approval timestamp, and approval provenance are null. `allow_as_default` is false for every entry.",
        "A reviewer must confirm the exact FDC ID and scope, then separately decide whether generic default use is permitted. These references remain separate from IngredientMapping and never create or overwrite a VERIFIED mapping.",
        "",
    ])
    return "\n".join(lines)

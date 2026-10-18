from itertools import islice, product

MAX_CANDIDATES_PER_RECIPE = 256


class CandidateGenerator:
    """Generate deterministic variants with a bound on Cartesian growth."""

    def __init__(self, kg, max_candidates=MAX_CANDIDATES_PER_RECIPE):
        if not isinstance(max_candidates, int) or max_candidates < 1:
            raise ValueError("max_candidates must be at least 1")
        self.kg = kg
        self.max_candidates = max_candidates

    def generate(self, recipe_id):
        base = self.kg.recipe_ingredients(recipe_id)
        option_groups = []

        for original, amount in base:
            options = [(original, amount, 1.0, "original")]
            for candidate in self.kg.substitution_candidates_for(original):
                substitute = candidate["id"]
                compatibility = candidate["compatibility"]
                substitute_data = self.kg.ingredients.get(substitute)
                if not substitute_data or substitute_data.get("typical_amount_g") is None:
                    continue
                typical = substitute_data["typical_amount_g"]
                options.append((substitute, typical, compatibility, candidate["basis"]))
            option_groups.append(options)

        candidates = []
        combinations = product(*option_groups)
        for combination in islice(combinations, self.max_candidates):
            recipe = [(iid, amount) for iid, amount, _, _ in combination]
            compatibility = (sum(x[2] for x in combination) / len(combination)
                             if combination else 0.0)
            changed = sum(iid != base_iid for (iid, _, _, _), (base_iid, _) in zip(combination, base))
            ingredient_changes = [
                {
                    "change_type": "REPLACED",
                    "original_position": position,
                    "adapted_position": position,
                    "original_ingredient_id": original_id,
                    "adapted_ingredient_id": adapted_id,
                    "original_quantity_g": original_amount,
                    "adapted_quantity_g": adapted_amount,
                    "substitution_basis": basis,
                }
                for position, ((adapted_id, adapted_amount, _, basis), (original_id, original_amount))
                in enumerate(zip(combination, base))
                if adapted_id != original_id
            ]
            candidates.append({
                "ingredients": recipe,
                "compatibility": compatibility,
                "modified_slots": changed,
                "modification_rate": changed / len(base) if base else 0.0,
                "substitution_basis": [basis for _, _, _, basis in combination],
                "ingredient_changes": ingredient_changes,
            })
        return candidates

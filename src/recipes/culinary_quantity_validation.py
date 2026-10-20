"""Evidence-bound culinary checks for newly constructed Food.com servings."""

import math


def _positive(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value > 0)


def _fraction(value):
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and 0 <= value <= 1)


def _name(value):
    return " ".join(str(value or "").casefold().split())


class CulinaryQuantityValidationService:
    """Keep hard graph/policy checks separate from optional model commentary."""

    def __init__(self, kg=None, model_provider=None):
        self.kg = kg
        self.model_provider = model_provider

    def validate(self, recipe, optimisation, rules=None, *, assess_with_model=False):
        rules = rules if isinstance(rules, dict) else {}
        source_names = recipe.get("raw_ingredients") or recipe.get("raw_ingredient_names") or []
        rows = optimisation.get("ingredients") or []
        findings, missing, linear = [], [], []
        if optimisation.get("optimisation_status") != "OPTIMISED":
            missing.append("NUTRITIONALLY_VERIFIED_COMBINATION_MISSING")
        if not source_names:
            missing.append("SOURCE_INGREDIENT_LIST_MISSING")
        if self.kg is not None and callable(getattr(self.kg, "foodcom_ingredient_links", None)):
            try:
                links = self.kg.foodcom_ingredient_links([recipe.get("id")])
                by_position = {link.get("position"): link.get("name") for link in links}
                if (len(by_position) != len(source_names)
                        or any(_name(by_position.get(index)) != _name(source)
                               for index, source in enumerate(source_names))):
                    missing.append("GRAPH_RECIPE_INGREDIENT_LINKS_INCOMPLETE")
            except Exception:
                missing.append("GRAPH_RECIPE_INGREDIENT_LINKS_UNAVAILABLE")
        if not recipe.get("method"):
            missing.append("COOKING_INSTRUCTIONS_MISSING")
        if len(source_names) != len(rows):
            findings.append({"code": "ESSENTIAL_INGREDIENT_MISSING", "detail":
                             "The constructed combination does not preserve every listed ingredient."})
        for index, source in enumerate(source_names):
            row = rows[index] if index < len(rows) else {}
            if _name(source) != _name(row.get("name")) or not _positive(row.get("quantity_g")):
                findings.append({"code": "ESSENTIAL_INGREDIENT_MISSING", "position": index,
                                 "detail": f"{source} is absent or has no positive gram weight."})
            if row and (not _positive(row.get("minimum_g")) or not _positive(row.get("maximum_g"))
                        or not row.get("bounds_basis")):
                missing.append(f"SOURCED_INGREDIENT_BOUNDS_MISSING:{index}")
            elif row and _positive(row.get("quantity_g")):
                if not row["minimum_g"] <= row["quantity_g"] <= row["maximum_g"]:
                    findings.append({"code": "SOURCED_INGREDIENT_BOUND_VIOLATION",
                                     "position": index,
                                     "detail": f"{source} falls outside its stated gram bounds."})
        if not rows:
            return self._report(findings, missing, [], None)

        basis = str(rules.get("basis") or "").strip()
        if not basis:
            missing.append("DISH_CULINARY_RULE_BASIS_MISSING")
        positions = len(rows)
        role_limits = rules.get("role_limits") if isinstance(rules.get("role_limits"), dict) else {}
        role_shares = rules.get("role_proportions") if isinstance(rules.get("role_proportions"), dict) else {}
        ratios = rules.get("ingredient_ratios") if isinstance(rules.get("ingredient_ratios"), list) else []
        required_roles = rules.get("required_roles") if isinstance(rules.get("required_roles"), list) else []
        essential_positions = rules.get("essential_positions")
        if essential_positions is None:
            missing.append("DISH_ESSENTIAL_INGREDIENT_POLICY_MISSING")
        elif not isinstance(essential_positions, list) or not all(
            isinstance(i, int) and not isinstance(i, bool) and 0 <= i < positions
            for i in essential_positions
        ):
            missing.append("DISH_ESSENTIAL_INGREDIENT_POLICY_INVALID")
        else:
            for index in essential_positions:
                if index >= len(rows) or not _positive(rows[index].get("quantity_g")):
                    findings.append({"code": "ESSENTIAL_INGREDIENT_MISSING", "position": index,
                                     "detail": "A dish-essential ingredient was removed."})
        if not required_roles:
            missing.append("DISH_IDENTITY_ROLE_POLICY_MISSING")
        else:
            present_roles = {row.get("role") for row in rows if _positive(row.get("quantity_g"))}
            for role in required_roles:
                if role not in present_roles:
                    findings.append({"code": "DISH_IDENTITY_ROLE_MISSING", "role": role,
                                     "detail": f"Required dish role {role} is absent."})

        if positions > 1 and not ratios and not role_shares:
            missing.append("DISH_PROPORTION_RULE_MISSING")
        for index, row in enumerate(rows):
            role = row.get("role")
            if (role in {"seasoning", "aromatic"}
                    and not ((isinstance(role_limits.get(role), dict)
                              and any(role_limits[role].get(key) is not None
                                      for key in ("min_g", "max_g")))
                             or (isinstance(role_shares.get(role), dict)
                                 and role_shares[role].get("max_fraction") is not None))):
                missing.append(f"{role.upper()}_CULINARY_AMOUNT_RULE_MISSING")
            rule = role_limits.get(role)
            if not isinstance(rule, dict):
                continue
            for field, operator in (("min_g", ">="), ("max_g", "<=")):
                value = rule.get(field)
                if value is None:
                    continue
                if not _positive(value) or not basis:
                    missing.append(f"ROLE_AMOUNT_RULE_INVALID:{role}:{field}")
                    continue
                vector = [0.0] * positions
                vector[index] = 1.0
                linear.append({"coefficients": vector, "operator": operator,
                               "limit": float(value), "basis": basis,
                               "code": "ROLE_AMOUNT_LIMIT", "position": index})

        for role, rule in role_shares.items():
            members = [i for i, row in enumerate(rows) if row.get("role") == role]
            if not members:
                missing.append(f"ROLE_PROPORTION_TARGET_MISSING:{role}")
                continue
            if not isinstance(rule, dict):
                missing.append(f"ROLE_PROPORTION_RULE_INVALID:{role}")
                continue
            for field, operator in (("min_fraction", ">="), ("max_fraction", "<=")):
                value = rule.get(field)
                if value is None:
                    continue
                if not _fraction(value) or not basis:
                    missing.append(f"ROLE_PROPORTION_RULE_INVALID:{role}:{field}")
                    continue
                vector = [(1.0 if i in members else 0.0) - float(value)
                          for i in range(positions)]
                linear.append({"coefficients": vector, "operator": operator,
                               "limit": 0.0, "basis": basis,
                               "code": "ROLE_PROPORTION", "role": role})

        for index, rule in enumerate(ratios):
            if not isinstance(rule, dict):
                missing.append(f"INGREDIENT_RATIO_RULE_INVALID:{index}")
                continue
            numerator, denominator = rule.get("numerator_position"), rule.get("denominator_position")
            if (not isinstance(numerator, int) or isinstance(numerator, bool)
                    or not isinstance(denominator, int) or isinstance(denominator, bool)
                    or not 0 <= numerator < positions or not 0 <= denominator < positions
                    or numerator == denominator):
                missing.append(f"INGREDIENT_RATIO_RULE_INVALID:{index}")
                continue
            for field, operator in (("min", ">="), ("max", "<=")):
                value = rule.get(field)
                if value is None:
                    continue
                if not _positive(value) or not basis:
                    missing.append(f"INGREDIENT_RATIO_RULE_INVALID:{index}:{field}")
                    continue
                vector = [0.0] * positions
                vector[numerator] = 1.0
                vector[denominator] = -float(value)
                linear.append({"coefficients": vector, "operator": operator,
                               "limit": 0.0, "basis": basis,
                               "code": "INGREDIENT_RATIO", "rule_index": index})
        if basis:
            for rule in linear:
                amount = sum(value * row["quantity_g"]
                             for value, row in zip(rule["coefficients"], rows))
                failed = (amount > rule["limit"] + 1e-8 if rule["operator"] == "<="
                          else amount < rule["limit"] - 1e-8)
                if failed:
                    findings.append({"code": rule["code"], "position": rule.get("position"),
                                     "role": rule.get("role"), "rule_index": rule.get("rule_index"),
                                     "detail": "Constructed gram weights violate a sourced culinary rule."})
        model = None
        if assess_with_model and missing and callable(self.model_provider):
            try:
                generator, error = self.model_provider()
                if generator is not None and callable(getattr(generator, "assess_quantity_plausibility", None)):
                    model = generator.assess_quantity_plausibility({
                        "dish": recipe.get("name"), "ingredients": [
                            {"name": row.get("name"), "role": row.get("role"),
                             "quantity_g": row.get("quantity_g")} for row in rows],
                        "instructions": recipe.get("method") or [],
                        "missing_deterministic_rules": missing,
                    })
                else:
                    model = {"status": "UNAVAILABLE", "reason": error or "Quantity assessment is unavailable."}
            except Exception as exc:
                model = {"status": "UNAVAILABLE", "reason": f"Model assessment failed ({type(exc).__name__})."}
        report = self._report(findings, missing, linear, model)
        report["rule_basis"] = basis or None
        report["applied_identity_requirements"] = {
            "preserve_all_source_ingredients": True,
            "essential_positions": essential_positions if isinstance(essential_positions, list)
                                   and "DISH_ESSENTIAL_INGREDIENT_POLICY_INVALID" not in missing else [],
            "required_roles": required_roles,
            "basis": basis or "source ingredient list",
        }
        report["limitations"] = [
            "A supplied culinary policy is not automatically an authoritative culinary standard.",
            "Numeric and culinary checks do not establish medical safety.",
        ]
        return report

    @staticmethod
    def _report(findings, missing, linear, model):
        status = ("CULINARY_INVALID" if findings else
                  "CULINARY_UNVERIFIED" if missing else "CULINARY_VALID")
        return {"status": status, "findings": findings,
                "missing_information": list(dict.fromkeys(missing)),
                "deterministic_rule_count": len(linear),
                "model_assessment": {"authoritative": False, "assessed_stage": "original_combination",
                                     "result": model} if model else None,
                "_linear_constraints": linear}

    def validate_and_reoptimise(self, recipe, calculation, constraints, original, optimiser, *,
                                rules=None, serving_count=1, role_bounds=None,
                                total_weight_bounds=None, assess_with_model=False):
        initial = self.validate(recipe, original, rules, assess_with_model=assess_with_model)
        linear = initial.pop("_linear_constraints")
        initial["applied_linear_constraints"] = linear
        initial["original_status"] = initial["status"]
        initial["original_findings"] = list(initial["findings"])
        initial["original_missing_information"] = list(initial["missing_information"])
        initial["adjusted_optimisation"] = None
        initial["adjustment_status"] = "NOT_NEEDED"
        if initial["status"] != "CULINARY_INVALID":
            return initial
        if len(original.get("ingredients") or []) != len(calculation.get("ingredients") or []):
            # Positional culinary inequalities cannot be carried across an
            # incomplete ingredient list. Rebuild the full positive list first.
            linear = []
            initial["applied_linear_constraints"] = []
        adjusted = optimiser.optimise(
            recipe, calculation, constraints, serving_count=serving_count,
            role_bounds=role_bounds, total_weight_bounds=total_weight_bounds,
            culinary_linear_constraints=linear,
        )
        initial["adjusted_optimisation"] = adjusted
        if adjusted["optimisation_status"] != "OPTIMISED":
            initial["adjustment_status"] = "NO_FEASIBLE_CULINARY_COMBINATION"
            return initial
        checked = self.validate(recipe, adjusted, rules, assess_with_model=False)
        checked.pop("_linear_constraints")
        initial["status"] = checked["status"]
        initial["findings"] = checked["findings"]
        initial["missing_information"] = checked["missing_information"]
        initial["applied_identity_requirements"] = checked["applied_identity_requirements"]
        initial["findings_after_adjustment"] = checked["findings"]
        initial["missing_information_after_adjustment"] = checked["missing_information"]
        initial["adjustment_status"] = "NUTRITIONALLY_RECHECKED" if checked["status"] != "CULINARY_INVALID" else "CULINARY_RECHECK_FAILED"
        return initial


__all__ = ["CulinaryQuantityValidationService"]

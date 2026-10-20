import json
import os
import time
from threading import Lock

import torch
from transformers import AutoTokenizer, AutoModelForCausalLM

from src.recipes.adapted_recipe_culinary import CULINARY_PROMPT_VERSION


class QwenInferenceBusyError(RuntimeError):
    """Raised when a task-specific inference request cannot acquire the model."""


class _TokenMetricsStreamer:
    """Minimal Transformers streamer that records counts without retaining text."""

    def __init__(self, on_token):
        self._on_token = on_token
        self._first_put = True

    def put(self, value):
        if self._first_put:
            # generate() sends the entire prompt before any generated tokens.
            self._first_put = False
            return
        self._on_token(int(value.shape[-1]))

    def end(self):
        return None


def _mps_available(torch_module=torch):
    backend = getattr(getattr(torch_module, "backends", None), "mps", None)
    try:
        return bool(backend and backend.is_built() and backend.is_available())
    except (AttributeError, RuntimeError):
        return False


def _env_bool(name, default=False):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().casefold() in {"1", "true", "yes", "on"}


def _positive_env_float(name, default, *, minimum=1.0, maximum=300.0):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return min(maximum, max(minimum, value))


def _configure_cpu_threads():
    configured_threads = os.environ.get("RENALLY_QWEN_CPU_THREADS")
    try:
        cpu_threads = int(configured_threads) if configured_threads else min(os.cpu_count() or 4, 8)
    except ValueError:
        cpu_threads = min(os.cpu_count() or 4, 8)
    torch.set_num_threads(max(1, cpu_threads))


class QwenRecipeGenerator:
    DEFAULT_MODEL_NAME = "Qwen/Qwen3-1.7B"

    METHOD_ACTIONS = {
        "prepare": "Prepare {foods}.",
        "steam": "Steam {foods} until thoroughly cooked.",
        "boil": "Boil {foods} until thoroughly cooked.",
        "bake": "Bake {foods} until thoroughly cooked.",
        "cook": "Cook {foods} until thoroughly cooked.",
        "combine": "Combine {foods}.",
        "serve": "Serve {foods}.",
    }

    def __init__(self, model_name=None):
        model_name = model_name or os.environ.get("RENALLY_QWEN_MODEL", self.DEFAULT_MODEL_NAME)
        self.model_name = model_name
        self.device = "mps" if _mps_available() else "cpu"
        self.device_fallback_reason = None
        if self.device == "cpu":
            _configure_cpu_threads()
        load_started = time.perf_counter()
        tokenizer_started = time.perf_counter()
        self.tokenizer = AutoTokenizer.from_pretrained(model_name)
        self.tokenizer_load_ms = (time.perf_counter() - tokenizer_started) * 1000.0
        model_started = time.perf_counter()
        self.mps_probe_ms = None
        if self.device == "mps":
            candidate = None
            probe_started = None
            try:
                # FP16 is the established broadly supported reduced precision
                # for MPS. CPU keeps the checkpoint's native dtype.
                candidate = AutoModelForCausalLM.from_pretrained(
                    model_name, torch_dtype=torch.float16,
                ).to(self.device)
                probe_started = time.perf_counter()
                self._probe_mps_generation(candidate, self.tokenizer)
                self.mps_probe_ms = (time.perf_counter() - probe_started) * 1000.0
                self.model = candidate
            except Exception as exc:  # fall back only during initial loading
                phase = "MODEL_LOAD" if candidate is None else "GENERATION_PROBE"
                self.device_fallback_reason = f"MPS_{phase}_FAILED_{type(exc).__name__}"
                if probe_started is not None:
                    self.mps_probe_ms = (time.perf_counter() - probe_started) * 1000.0
                candidate = None
                if getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
                    try:
                        torch.mps.empty_cache()
                    except Exception:
                        pass
                self.device = "cpu"
                _configure_cpu_threads()
                self.model = AutoModelForCausalLM.from_pretrained(model_name).to(self.device)
        else:
            # Preserve the checkpoint's native dtype; forcing FP16/BF16 on CPU
            # can make otherwise supported installations slower or less stable.
            self.model = AutoModelForCausalLM.from_pretrained(model_name).to(self.device)
        self.model_load_ms = (time.perf_counter() - model_started) * 1000.0
        self.load_duration_ms = (time.perf_counter() - load_started) * 1000.0
        self.dtype = str(next(self.model.parameters()).dtype)
        self._generation_lock = Lock()
        self._diagnostics_lock = Lock()
        self._last_inference_diagnostics = None
        self._active_inference_diagnostics = None

    @staticmethod
    def _probe_mps_generation(model, tokenizer):
        """Verify a one-token generation before accepting an MPS model instance."""
        token_id = getattr(tokenizer, "bos_token_id", None)
        if token_id is None:
            token_id = getattr(tokenizer, "eos_token_id", None)
        if token_id is None:
            token_id = 0
        input_ids = torch.tensor([[int(token_id)]], dtype=torch.long, device="mps")
        with torch.no_grad():
            model.generate(input_ids=input_ids, max_new_tokens=1, do_sample=False)

    def get_last_inference_diagnostics(self):
        """Return the latest safe summary; prompt text and user content are omitted."""
        with self._diagnostics_lock:
            active = self._active_inference_diagnostics
            return dict(active or self._last_inference_diagnostics or {})

    def _publish_diagnostics(self, values, *, active=False, finished=False):
        with self._diagnostics_lock:
            if active:
                self._active_inference_diagnostics = values
                self._last_inference_diagnostics = dict(values)
            elif self._active_inference_diagnostics is values or self._active_inference_diagnostics is None:
                self._last_inference_diagnostics = dict(values)
            if finished and self._active_inference_diagnostics is values:
                self._active_inference_diagnostics = None

    def _generate_text(self, messages, max_new_tokens=250, max_time=None,
                       enable_thinking=False, diagnostics=None, fail_if_busy=False):
        # The local HTTP server is threaded; serialize access to the shared model
        # to avoid overlapping inference and unnecessary peak memory use. The
        # culinary path can fail fast rather than leave requests queued behind
        # an unrelated long-running generation.
        if diagnostics is None and max_time is None and not fail_if_busy:
            # Preserve the established shared-model path for recipe generation,
            # request planning, and ingredient mapping. Only culinary calls opt
            # into the separately measured formatting and token stream below.
            with self._generation_lock:
                inputs = self.tokenizer.apply_chat_template(
                    messages, tokenize=True, add_generation_prompt=True,
                    enable_thinking=enable_thinking, return_tensors="pt", return_dict=True,
                ).to(self.device)
                with torch.no_grad():
                    output = self.model.generate(
                        **inputs, max_new_tokens=max_new_tokens, do_sample=False,
                    )
                generated = output[0][inputs["input_ids"].shape[-1]:]
                return self.tokenizer.decode(generated, skip_special_tokens=True)

        prompt_started = time.perf_counter()
        template_started = time.perf_counter()
        rendered_prompt = self.tokenizer.apply_chat_template(
            messages, tokenize=False, add_generation_prompt=True,
            enable_thinking=enable_thinking,
        )
        template_ms = (time.perf_counter() - template_started) * 1000.0
        tokenize_started = time.perf_counter()
        inputs = self.tokenizer(
            rendered_prompt, add_special_tokens=False,
            return_tensors="pt", return_dict=True,
        )
        tokenization_ms = (time.perf_counter() - tokenize_started) * 1000.0
        input_tokens = int(inputs["input_ids"].shape[-1])
        transfer_started = time.perf_counter()
        inputs = inputs.to(self.device)
        transfer_ms = (time.perf_counter() - transfer_started) * 1000.0
        diagnostics_data = {
            "model_id": self.model_name,
            "device": str(next(self.model.parameters()).device),
            "dtype": self.dtype,
            "device_fallback_reason": self.device_fallback_reason,
            "mps_generation_probe_ms": self.mps_probe_ms,
            "cpu_threads": torch.get_num_threads() if self.device == "cpu" else None,
            "tokenizer_load_ms": round(self.tokenizer_load_ms, 2),
            "model_load_ms": round(self.model_load_ms, 2),
            "model_total_load_ms": round(self.load_duration_ms, 2),
            "prompt_characters": sum(len(str(message.get("content") or "")) for message in messages),
            "input_tokens": input_tokens,
            "chat_template_ms": round(template_ms, 2),
            "tokenization_ms": round(tokenization_ms, 2),
            "device_transfer_ms": round(transfer_ms, 2),
            "generated_tokens": 0,
            "first_token_latency_ms": None,
            "generation_ms": None,
            "decoding_ms": None,
            "max_new_tokens": int(max_new_tokens),
            "max_time_seconds": max_time,
            "thinking_mode": bool(enable_thinking),
            "timed_out": False,
            "json_valid": None,
        }
        if diagnostics is not None:
            diagnostics.update(diagnostics_data)

        acquired = self._generation_lock.acquire(blocking=not fail_if_busy)
        if not acquired:
            diagnostics_data["busy"] = True
            self._publish_diagnostics(diagnostics_data)
            raise QwenInferenceBusyError("The shared Qwen model is already generating.")
        try:
            self._publish_diagnostics(diagnostics_data, active=True)
            def on_token(count):
                now = time.perf_counter()
                if diagnostics_data["first_token_latency_ms"] is None:
                    diagnostics_data["first_token_latency_ms"] = round((now - generation_started) * 1000.0, 2)
                diagnostics_data["generated_tokens"] += count
                diagnostics_data["last_token_latency_ms"] = round((now - generation_started) * 1000.0, 2)
                self._publish_diagnostics(diagnostics_data)

            streamer = _TokenMetricsStreamer(on_token)
            generation_started = time.perf_counter()
            with torch.no_grad():
                generation_options = {
                    "max_new_tokens": max_new_tokens,
                    "do_sample": False,
                    "streamer": streamer,
                }
                if max_time is not None:
                    generation_options["max_time"] = max_time
                output = self.model.generate(**inputs, **generation_options)
            generated = output[0][inputs["input_ids"].shape[-1]:]
            diagnostics_data["generated_tokens"] = int(generated.numel())
            eos_id = getattr(getattr(self.model, "generation_config", None), "eos_token_id", None)
            if isinstance(eos_id, (list, tuple, set)):
                diagnostics_data["ended_with_eos"] = bool(generated.numel() and int(generated[-1]) in eos_id)
            else:
                diagnostics_data["ended_with_eos"] = bool(
                    generated.numel() and eos_id is not None and int(generated[-1]) == eos_id
                )
            diagnostics_data["generation_ms"] = round((time.perf_counter() - generation_started) * 1000.0, 2)
            diagnostics_data["generation_tokens_per_second"] = round(
                int(generated.numel()) / max(diagnostics_data["generation_ms"] / 1000.0, 1e-9), 3,
            )
            first_token_ms = diagnostics_data.get("first_token_latency_ms")
            last_token_ms = diagnostics_data.get("last_token_latency_ms")
            diagnostics_data["decode_tokens_per_second"] = (
                round((int(generated.numel()) - 1) / ((last_token_ms - first_token_ms) / 1000.0), 3)
                if int(generated.numel()) > 1 and first_token_ms is not None
                and last_token_ms is not None and last_token_ms > first_token_ms else None
            )
            decode_started = time.perf_counter()
            raw = self.tokenizer.decode(generated, skip_special_tokens=True)
            diagnostics_data["decoding_ms"] = round((time.perf_counter() - decode_started) * 1000.0, 2)
            if diagnostics is not None:
                diagnostics.update(diagnostics_data)
            return raw
        except Exception:
            diagnostics_data["generation_ms"] = round((time.perf_counter() - generation_started) * 1000.0, 2) if "generation_started" in locals() else None
            raise
        finally:
            if "generation_started" in locals():
                diagnostics_data["total_inference_ms"] = round((time.perf_counter() - prompt_started) * 1000.0, 2)
            if diagnostics is not None:
                diagnostics.update(diagnostics_data)
            self._publish_diagnostics(diagnostics_data, finished=True)
            self._generation_lock.release()

    @staticmethod
    def _decode_json_object(raw, error_message="The local SLM did not return a usable request plan."):
        decoder = json.JSONDecoder()
        for index, char in enumerate(raw):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(raw[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        raise ValueError(error_message)

    def evaluate_ingredient_candidates(self, evaluation_input):
        """Evaluate only the retrieved USDA records for one source ingredient.

        The caller validates every returned ID and explicit food attribute.
        """
        schema = {
            "status": "RECOMMENDED | AMBIGUOUS | REJECTED | REVIEW_REQUIRED",
            "recommended_fdc_id": "one provided candidate ID or null",
            "compatible_candidate_ids": ["IDs copied from the provided candidate list only"],
            "unresolved_attributes": ["unsupported or unspecified attributes"],
            "reason": "brief evidence-based explanation",
            "requires_review": "boolean; false only for a clear match, true otherwise",
        }
        messages = [
            {"role": "system", "content":
             "You validate ingredient-to-food-record matches for RenAlly. Return exactly one JSON object matching "
             "the schema. Evaluate only the USDA candidates supplied by the application. Never create, alter, or "
             "guess an FDC ID, and never introduce a USDA candidate. Compare ingredient identity, food category, "
             "species or variety, raw/cooked state, preparation method, and processing state. Use only attributes "
             "supported by the original ingredient and recipe context. A generic ingredient does not imply a species, "
             "oil type, cooking state, or processing method. Reject known contradictions; preserve ambiguity when an "
             "attribute is unspecified or evidence is inconclusive. Recipe quantity, units, other ingredients, and "
             "cooking instructions may distinguish a pepper vegetable from a pepper seasoning. Sliced pepper in "
             "a stir-fry may identify bell pepper but does not establish its colour. Use RECOMMENDED only for one clearly compatible "
             "provided candidate; use AMBIGUOUS for multiple plausible candidates or unresolved distinctions; use "
             "REJECTED when no supplied candidate is compatible; use REVIEW_REQUIRED when output/evidence cannot be "
             "resolved safely. Set requires_review=false only for one clear match with no unresolved identity-critical "
             "attributes; otherwise set it true. Do not provide confidence "
             "probabilities."},
            {"role": "user", "content":
             f"Ingredient evaluation input:\n{json.dumps(evaluation_input, ensure_ascii=False)}"
             f"\n\nRequired JSON schema:\n{json.dumps(schema, ensure_ascii=False)}"},
        ]
        raw = self._generate_text(messages, max_new_tokens=260)
        return self._decode_json_object(
            raw, error_message="The local SLM did not return a usable ingredient evaluation."
        )

    def assess_quantity_plausibility(self, evaluation_input):
        """Give an optional, non-authoritative opinion on a constructed serving."""
        messages = [
            {"role": "system", "content":
             "Assess only the culinary plausibility of the supplied newly constructed gram quantities "
             "for the named dish. Do not infer Food.com quantity units, invent validated ranges or ratios, "
             "calculate nutrients, or claim medical safety. Return JSON with status PLAUSIBLE, "
             "QUESTIONABLE, or UNSURE and a brief reason. Your opinion is not verified evidence."},
            {"role": "user", "content": json.dumps(evaluation_input, ensure_ascii=False, default=str)},
        ]
        raw = self._generate_text(messages, max_new_tokens=180, max_time=45.0,
                                  fail_if_busy=True)
        result = self._decode_json_object(raw, "The local SLM did not return a quantity assessment.")
        status = str(result.get("status") or "").upper()
        if status not in {"PLAUSIBLE", "QUESTIONABLE", "UNSURE"}:
            raise ValueError("Unsupported quantity-assessment status.")
        return {"status": status, "reason": str(result.get("reason") or "")[:500]}

    def evaluate_culinary_compatibility(self, evaluation_input):
        """Evaluate an adapted recipe's culinary plausibility, without editing it."""
        prompt_started = time.perf_counter()
        compact_input = self._compact_culinary_evidence(evaluation_input)
        changes = compact_input.get("changes", [])
        change_ids = [row.get("id") for row in changes if row.get("id")]
        source_change_ids = [row.get("id") for row in changes
                             if row.get("id") and row.get("type") != "ADDED"]
        source_changes = [row for row in changes if row.get("id") in source_change_ids]
        required_function_map = {
            row.get("change_id"): row.get("functions", [])
            for row in compact_input.get("required_functions", []) if isinstance(row, dict)
        }
        adapted = compact_input.get("adapted", {})
        adapted_steps = adapted.get("steps", []) if isinstance(adapted, dict) else []
        example_step_ref = next((row.get("id") for row in adapted_steps
                                 if isinstance(row, dict) and row.get("id")), None)
        changed_support_refs = list(dict.fromkeys(
            row.get("new_ref") for row in changes if row.get("new_ref")
        ))
        coverage_by_change = {}
        for change in source_changes:
            source_id = change["id"]
            # Concrete rows and evidence references help the small model keep
            # the required taxonomy and copy schema-complete support IDs.
            functions = required_function_map.get(source_id) or ["OTHER"]
            coverage_by_change[source_id] = []
            for function in functions:
                if function == "FLAVOR_BASE":
                    cited_ingredients = changed_support_refs or (
                        [change.get("new_ref")] if change.get("new_ref") else []
                    )
                else:
                    cited_ingredients = [change.get("new_ref")] if change.get("new_ref") else []
                coverage_by_change[source_id].append({
                    "function": function,
                    "importance": "ESSENTIAL | SUPPORTING | OPTIONAL | INTENTIONALLY_REDUCED",
                    "coverage": "COVERED | PARTIALLY_COVERED | NOT_COVERED | UNKNOWN",
                    "supporting_evidence_ids": list(dict.fromkeys(
                        cited_ingredients + ([example_step_ref] if example_step_ref else [])
                    )),
                    "reason": "evidence-based reason, at most 8 words",
                })
        schema = {"evaluations": [
            {"change_id": change_id,
             "functional_compatibility": "COMPATIBLE | PARTIAL | INCOMPATIBLE | UNKNOWN",
             "quantity_appropriateness": "PLAUSIBLE | IMPLAUSIBLE | UNKNOWN",
             "cooking_compatibility": "COMPATIBLE | INCOMPATIBLE | UNKNOWN",
             "recipe_coherence": "COHERENT | INCOHERENT | UNCERTAIN",
             "reason": "evidence-based reason, at most 8 words",
             "functional_coverage": coverage_by_change[change_id]
                 if change_id in coverage_by_change else []}
            for change_id in change_ids
        ]}
        messages = [
            {"role": "system", "content":
             "Return raw JSON only: one top-level object matching the schema, no prose or markdown fences. Never return "
             "a top-level array. Judge the full adapted "
             "recipe, quantities, and steps for each change and relevant functions of its replaced or removed source. "
             "Use every category listed for a change in required_functions exactly once. Select only other roles supported "
             "by recipe context; do not default to FLAVOR_BASE. OTHER is only for a clear role outside the categories. "
             "Nested coverage belongs only under a non-added source change. For ADDED changes, coverage MUST be []; "
             "additions can support source rows through adapted evidence refs. For every coverage row, include both "
             "supporting_evidence_ids, using [] when none. This list may contain adapted ingredient and step refs. "
             "For broad FLAVOR_BASE, cite "
             "all contributing adapted ingredients, including additions; one narrow contribution alone is partial. "
             "A partial contribution is not full coverage; do not assume sensory equivalence. "
             "Functions: PRIMARY_PROTEIN, STARCH_BASE, FLAVOR_BASE, SALTINESS, UMAMI, AROMATICS, ACIDITY, SWEETNESS, "
             "FAT, LIQUID, THICKENING, BINDING, LEAVENING, TEXTURE, STRUCTURAL_INTEGRITY, OTHER. For concentrated "
             "soup/noodle seasoning, include separate FLAVOR_BASE, SALTINESS, UMAMI rows when relevant; umami alone "
             "does not establish flavor-base coverage. One narrow contribution alone means partial coverage of a broad "
             "function. Mark saltiness INTENTIONALLY_REDUCED only with explicit adaptation intent. Use only evidence IDs; "
             "cite adapted ingredients or steps for COVERED. Unknown evidence stays UNKNOWN. Do not invent facts or judge "
             "nutrition. Put coverage rows inside their change evaluation; additions with no source function use []. "
             "Copy every change ID in order. Limit each reason to 8 words. Python assigns function IDs. "
             f"Prompt version: {CULINARY_PROMPT_VERSION}."},
            {"role": "user", "content":
             f"Evidence:\n{json.dumps(compact_input, ensure_ascii=False, separators=(',', ':'), default=str)}"
             f"\nSchema:\n{json.dumps(schema, ensure_ascii=False, separators=(',', ':'))}"},
        ]
        prompt_construction_ms = (time.perf_counter() - prompt_started) * 1000.0
        max_new_tokens = self._culinary_output_token_budget(changes)
        max_time = _positive_env_float("RENALLY_QWEN_CULINARY_MAX_TIME_SECONDS", 90.0)
        thinking = _env_bool("RENALLY_QWEN_CULINARY_ENABLE_THINKING", False)
        diagnostics = {
            "task": "culinary_validation",
            "model_id": self.model_name,
            "device": self.device,
            "dtype": self.dtype,
            "thinking_mode": thinking,
            "prompt_version": CULINARY_PROMPT_VERSION,
            "prompt_construction_ms": round(prompt_construction_ms, 2),
            "timed_out": False,
            "json_valid": False,
        }
        try:
            raw = self._generate_text(
                messages, max_new_tokens=max_new_tokens, max_time=max_time,
                enable_thinking=thinking, diagnostics=diagnostics, fail_if_busy=True,
            )
        except QwenInferenceBusyError:
            diagnostics.update({"busy": True, "timed_out": False})
            self._publish_diagnostics(diagnostics)
            return {
                "status": "UNAVAILABLE", "error": "SLM_BUSY",
                "model_id": self.model_name, "prompt_version": CULINARY_PROMPT_VERSION,
                "inference_diagnostics": diagnostics,
            }
        diagnostics["total_model_evaluation_ms"] = round((time.perf_counter() - prompt_started) * 1000.0, 2)
        generated_tokens = int(diagnostics.get("generated_tokens") or 0)
        generation_ms = float(diagnostics.get("generation_ms") or 0.0)
        ended_with_eos = self._generated_output_ended_with_eos(diagnostics)
        deadline_exceeded = generation_ms >= max_time * 1000.0 and not ended_with_eos
        diagnostics["timed_out"] = bool(deadline_exceeded)
        if deadline_exceeded:
            diagnostics["timeout_phase"] = "transformers_max_time"
        diagnostics["output_truncated"] = bool(
            generated_tokens >= max_new_tokens and not ended_with_eos
        )
        if deadline_exceeded:
            diagnostics["json_valid"] = False
            self._publish_diagnostics(diagnostics)
            return {
                "status": "UNAVAILABLE", "error": "GENERATION_DEADLINE_EXCEEDED",
                "raw_output": raw, "model_id": self.model_name,
                "prompt_version": CULINARY_PROMPT_VERSION,
                "inference_diagnostics": diagnostics,
            }
        try:
            json_parse_started = time.perf_counter()
            result = self._decode_culinary_json_object(
                raw, error_message="The local SLM did not return a usable culinary evaluation."
            )
            diagnostics["json_parse_ms"] = round((time.perf_counter() - json_parse_started) * 1000.0, 2)
        except ValueError:
            diagnostics["json_parse_ms"] = round((time.perf_counter() - json_parse_started) * 1000.0, 2)
            diagnostics["json_valid"] = False
            self._publish_diagnostics(diagnostics)
            return {
                "status": "UNAVAILABLE",
                "error": "OUTPUT_TOKEN_LIMIT_REACHED" if diagnostics.get("output_truncated")
                        else "MALFORMED_CULINARY_JSON",
                "raw_output": raw, "model_id": self.model_name,
                "prompt_version": CULINARY_PROMPT_VERSION,
                "inference_diagnostics": diagnostics,
            }
        diagnostics["json_valid"] = True
        result["model_id"] = self.model_name
        result["prompt_version"] = CULINARY_PROMPT_VERSION
        result["inference_diagnostics"] = diagnostics
        self._publish_diagnostics(diagnostics)
        return result

    @staticmethod
    def _compact_culinary_evidence(evaluation_input):
        """Keep only recipe evidence needed for culinary review."""
        def recipe(source):
            if not isinstance(source, dict):
                return {}
            compact = {}
            if source.get("id") is not None:
                compact["id"] = source["id"]
            if source.get("name") is not None:
                compact["name"] = source["name"]
            if source.get("servings") is not None:
                compact["servings"] = source["servings"]
            ingredients = []
            for position, item in enumerate(source.get("ingredients", [])
                                            if isinstance(source.get("ingredients"), list) else []):
                if not isinstance(item, dict):
                    continue
                row = {}
                row["p"] = item.get("position", position)
                if item.get("ingredient_ref") is not None:
                    row["ref"] = item["ingredient_ref"]
                if item.get("description") is not None:
                    row["n"] = item["description"]
                quantity = item.get("quantity")
                if isinstance(quantity, dict) and any(value is not None for value in quantity.values()):
                    row["q"] = quantity
                for field, short in (("food_state", "state"), ("preparation", "prep"),
                                     ("processing", "processing"), ("ingredient_type", "type"),
                                     ("role", "role"), ("substitution_group", "group")):
                    if item.get(field) not in (None, "", [], {}):
                        row[short] = item[field]
                attributes = item.get("attributes")
                if isinstance(attributes, dict):
                    relevant = {
                        key: value for key, value in attributes.items()
                        if not any(term in str(key).casefold() for term in
                                   ("nutri", "fdc", "usda", "candidate", "source", "food_id"))
                    }
                    if relevant:
                        row["attributes"] = relevant
                if row:
                    ingredients.append(row)
            compact["ingredients"] = ingredients
            instructions = source.get("cooking_instructions")
            if isinstance(instructions, list) and instructions:
                compact["steps"] = [
                    {"id": row.get("step_ref"), "text": row.get("text")}
                    if isinstance(row, dict) else {"id": f"adapted-step-{index + 1}", "text": row}
                    for index, row in enumerate(instructions)
                ]
            return compact

        compact_changes = []
        for change in evaluation_input.get("structured_changes", []) if isinstance(evaluation_input, dict) else []:
            if not isinstance(change, dict):
                continue
            kind = str(change.get("change_type") or "").upper()
            if not kind or kind == "UNCHANGED":
                continue
            compact_changes.append({
                "id": change.get("change_id"),
                "type": kind,
                "old": change.get("original_ingredient"),
                "new": change.get("adapted_ingredient"),
                "op": change.get("original_position"),
                "np": change.get("adapted_position"),
                "old_ref": change.get("original_ingredient_ref"),
                "new_ref": change.get("adapted_ingredient_ref"),
            })
        compact = {
            "original": recipe(evaluation_input.get("original_recipe", {})),
            "adapted": recipe(evaluation_input.get("adapted_recipe", {})),
            "changes": compact_changes,
            "required_functions": evaluation_input.get("required_function_assessments", []),
            "missing": evaluation_input.get("missing_information", []),
        }
        if evaluation_input.get("adaptation_intent"):
            compact["adaptation_intent"] = evaluation_input["adaptation_intent"]
        return compact

    @staticmethod
    def _decode_culinary_json_object(raw, error_message="The local SLM did not return a usable culinary evaluation."):
        """Accept only a standalone JSON object; prose/fences are schema failures."""
        if not isinstance(raw, str):
            raise ValueError(error_message)
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(error_message) from exc
        if not isinstance(value, dict):
            raise ValueError(error_message)
        return value

    @staticmethod
    def _culinary_output_token_budget(changes):
        """Reserve a bounded JSON budget that grows with changed ingredients."""
        change_count = len(changes) if isinstance(changes, list) else 0
        return min(1024, max(448, 320 + 128 * change_count))

    def _generated_output_ended_with_eos(self, diagnostics):
        """Return the EOS state recorded from model token IDs before decoding."""
        return bool(diagnostics.get("ended_with_eos"))

    def classify_request(self, query, context=None):
        """Classify the request and resolve conversational references before retrieval."""
        app_context = dict(context or {})
        recent_conversation = app_context.pop("conversation_history", [])
        schema = {
            "intent": "single_recipe | new_recipe | meal_plan | ingredient_question | composition_question | substitution_question | catalog_question | unsupported",
            "question_type": "recipe | nutrition | suitability | substitution | catalog_existence | other",
            "recipe_query": "dish or recipe name, or null",
            "recipe_requests": [{"slot": "optional meal label", "recipe_id": None, "query": "dish phrase"}],
            "ingredient_queries": ["ingredient names the user wants checked or looked up"],
            "dataset_scope": "all | foundation_foods | sr_legacy",
            "composition_ingredients": [{"query": "food phrase", "amount_g": 40}],
            "original_ingredient_query": "ingredient currently used, or null",
            "substitute_ingredient_query": "proposed replacement, or null",
            "requested_count": 1,
            "clarification_question": "concise question if information is missing, or null",
        }
        messages = [
            {"role": "system", "content":
             "You are RenAlly's first-pass request classifier. Return exactly one JSON object matching the schema. "
             "Classify the task the user is asking for, before retrieval. Choose the intent from the requested action, "
             "not from whether a catalog record, nutrient value, quantity, or profile data is already available. "
             "A dish or food named in the current question is sufficient to ground a lookup request. Retrieval will "
             "check availability after classification; do not use unsupported just because retrieval has not happened. "
             "Use these intent rules: single_recipe for eating, suitability, evaluation, or nutrition questions about "
             "one named dish; copy its name into recipe_query and the first recipe_requests query, leaving recipe_id null. "
             "new_recipe means create or recommend one new dish. meal_plan means recommendations spanning multiple "
             "meals. ingredient_question means nutrient information for a named food. composition_question means "
             "calculate or assess foods with explicit user-stated weights; preserve only those weights. "
             "substitution_question means replace one named food with another and identify both. catalog_question "
             "means check named ingredients for dataset membership and use question_type catalog_existence. "
             "For a follow-up about ingredients of a prior dish, use every name, in order, from the most recent "
             "assistant context.recipe_ingredient_names. That field is the ingredient list; context.recipe is only "
             "the dish title. Source quantity strings and serving counts may be present as recipe context, but never "
             "convert them to weights, infer units, or treat them as verified nutrient inputs. If the dataset-membership request has neither a list in the question nor a resolvable "
             "prior list, leave ingredient_queries empty and ask for the names. Never search recipes for a membership "
             "question. "
             "Select dataset_scope only for food-record lookups, membership checks, weighted compositions, and "
             "new-recipe ingredient choices: use foundation_foods or sr_legacy when explicitly named, otherwise all. "
             "Reuse the prior assistant context.dataset_scope only when the user refers to that same dataset. "
             "Use unsupported only when the requested task or required target cannot be determined from the current "
             "question and relevant recent context, or the request is outside the app's supported tasks. For example, "
             "a question asking whether the user can eat a named dish is single_recipe even though retrieval has not "
             "yet confirmed that dish. Do not invent entities, amounts, catalog IDs, or facts."},
            {"role": "user", "content":
             f"Current question:\n{query}\n\nApp context:\n{json.dumps(app_context, ensure_ascii=False)}"
             f"\n\nRecent conversation, oldest to newest:\n{json.dumps(recent_conversation, ensure_ascii=False)}"
             f"\n\nRequired JSON schema:\n{json.dumps(schema, ensure_ascii=False)}"},
        ]
        return self._decode_json_object(self._generate_text(messages, max_new_tokens=300))

    def interpret_request(self, query, recipe_catalog, ingredient_catalog=None, context=None,
                          classified_request=None):
        """Turn free text into a bounded plan over the supplied local catalog."""
        schema = {
            "intent": "single_recipe | new_recipe | meal_plan | ingredient_question | composition_question | substitution_question | catalog_question | unsupported",
            "recipe_requests": [
                {"slot": "optional meal label", "recipe_id": "catalog ID or null", "query": "search phrase"}
            ],
            "ingredient_queries": ["ingredient name or phrase to look up"],
            "dataset_scope": "all | foundation_foods | sr_legacy",
            "composition_ingredients": [{"query": "food phrase", "amount_g": 40}],
            "generated_composition": [{"ingredient_id": "FoodData Central catalog ID", "amount_g": 100}],
            "original_ingredient_query": "ingredient currently in the recipe, for a substitution question",
            "substitute_ingredient_query": "proposed replacement ingredient, for a substitution question",
            "recipe_query": "optional recipe name or null",
            "requested_count": 1,
            "question_type": "recipe | nutrition | suitability | substitution | catalog_existence | other",
            "clarification_question": "ask for missing information, or null",
        }
        app_context = dict(context or {})
        recent_conversation = app_context.pop("conversation_history", [])
        messages = [
            {"role": "system", "content":
             "You are RenAlly's request planner. Interpret the user's overall goal and choose relevant local recipe "
             "or ingredient records for the application to retrieve. Return only one JSON object matching the schema. "
             "The first-pass SLM classification is authoritative for intent, resolved entities, and dataset_scope; preserve them. "
             "Use this pass to select IDs from the supplied relevant catalog, not to reclassify the request or replace "
             "resolved ingredient names. "
             "Use the recent conversation to resolve follow-ups and references to earlier foods or dishes. The current "
             "question determines the task; conversation history supplies context only. Never invent a missing food name "
             "or choose an unrelated catalog record to fill a gap. If a follow-up cannot be resolved from the history, "
             "set intent to unsupported and ask one concise clarification question. "
             "Use meal_plan when the user wants menu recommendations spanning multiple meals, including when no dish "
             "is named; use single_recipe when the user asks about one particular dish or recipe. For meal plans, infer "
             "the requested meal periods or count from the wording, create one distinct catalog recipe request for each, "
             "and use three requests for an otherwise unspecified day plan. Use each meal period as its slot label and "
             "search the catalog using the user's food preferences and constraints rather than the whole sentence. "
             "Use only recipe IDs and food IDs from the supplied catalogs. Never invent a catalog ID, nutrient value, "
             "or clinical limit. Only for a new_recipe request, you may propose food choices and gram portions, but "
             "every proposed food must reference an existing supplied FoodData Central ID and the application will "
             "validate the composition before displaying it. For a meal-plan request, choose distinct catalog recipe IDs and create one request per "
             "requested meal; use requested_count when the count is clear, otherwise use 3 for a day plan. "
             "For a general meal plan, prefer only catalog recipes marked nutrition_complete=true. If the user explicitly "
             "asks about a recipe marked nutrition_complete=false, keep that recipe so the application can report its "
             "missing Foundation Foods record instead of guessing. "
             "Preserve dataset_scope from the first-pass classification. Ingredient candidates include their USDA "
             "source dataset; never merge or treat separate source records as one food record. "
             "For a question about one ingredient's nutrient data, set intent to ingredient_question and put the "
             "ingredient name in ingredient_queries; do not substitute a related ingredient. "
             "For a question asking which named ingredients exist in or are missing from the ingredient dataset, "
             "set intent to catalog_question, set question_type to catalog_existence, and put every distinct "
             "ingredient name from the user's list in ingredient_queries. If the question refers to ingredients of a "
             "previously discussed recipe and its assistant context has recipe_ingredient_names, use that complete list "
             "in its original order. Prefer that component list over context.recipe, which is only the recipe title. "
             "If raw quantity strings or serving counts are in the context, preserve them as source details; do not "
             "infer units, convert them to grams, or treat them as validated nutrient inputs. "
             "Do not search recipe titles just because "
             "an ingredient phrase resembles a recipe name. The application will check each name against the "
             "selected FoodData Central dataset scope and report exact, possible, and missing records separately. "
             "If the user asks which ingredients are in the dataset but names none in the current question or recent "
             "conversation, keep ingredient_queries empty and ask them to provide the names. Never return a recipe for "
             "a dataset membership question. "
             "For a question about combining foods with explicit weights, set intent to composition_question and list "
             "each mentioned food and its user-stated weight in grams. Do not add foods or infer amounts. A local recipe "
             "may provide a cooking method, but the user's explicit weights determine the nutrient calculation. "
             "For an explicit weighted composition without a local recipe, a later cooking-step planner may suggest "
             "a method using only the validated foods. "
             "When the user asks you to create, make, or recommend a dish and no stored recipe matches, set intent to "
             "new_recipe. Propose a per-serving composition of at most 8 foods using only IDs in the supplied allowed "
             "ingredient catalog; put each catalog ID and a positive gram amount in generated_composition. Prefer foods "
             "the user explicitly requested and respect any recorded prototype portion ranges. Do not invent an ID, food, "
             "nutrient value, or clinical limit. The application "
             "will retrieve each record, calculate totals, and check explicit numeric limits before showing the suggestion. "
             "Use explicit numeric limits from app context as candidate preferences, but never say the candidate passes them. "
             "If the user only asks for nutrition data for an unavailable named recipe, do not create a substitute recipe; "
             "route it as unsupported. "
             "Use single_recipe only when a specific dish is named or clearly identified by the recent conversation. "
             "A vague follow-up, preference, or ingredient list is not a recipe title. If no specific dish can be grounded, "
             "ask which dish the user means instead of selecting the top search result. "
             "For a question asking whether one ingredient can replace another, set intent to substitution_question, "
             "identify the ingredient being replaced and the proposed replacement separately, and include a recipe "
             "name only if the user supplied one. Never decide whether the substitution is nutritionally acceptable; "
             "the application will retrieve both nutrient records and calculate that. "
             "If a named dish is absent from the catalog, keep its name in query and set recipe_id to null. "
             "Do not turn a condition's general daily reference into a meal limit. Numeric limits in the user's wording "
             "are not authoritative until confirmed through the app's explicit limits."},
            {"role": "user", "content":
             f"User question:\n{query}\n\nApp context (explicit controls):\n{json.dumps(app_context, ensure_ascii=False)}"
             f"\n\nRecent conversation, oldest to newest (context only):\n{json.dumps(recent_conversation, ensure_ascii=False)}"
             f"\n\nFirst-pass SLM classification (preserve its intent and resolved entities):\n"
             f"{json.dumps(classified_request, ensure_ascii=False)}"
             f"\n\nAllowed recipe catalog:\n{json.dumps(recipe_catalog, ensure_ascii=False)}"
             f"\n\nAllowed ingredient catalog:\n{json.dumps(ingredient_catalog or [], ensure_ascii=False)}"
             f"\n\nRequired JSON schema:\n{json.dumps(schema, ensure_ascii=False)}"}
        ]
        raw = self._generate_text(messages, max_new_tokens=420)
        return self._decode_json_object(raw)

    def respond_with_evidence(self, query, request_plan, evidence):
        messages = [
            {"role": "system", "content":
             "You are RenAlly's response layer. Answer the user's actual question using only the supplied tool results. "
             "The local recipe catalog, nutrient arithmetic, and constraint statuses are authoritative. Do not add "
             "ingredients, quantities, nutrients, substitutions, or medical advice. Clearly say when a lookup is missing, "
             "a limit fails, or data is incomplete. A condition profile is context, not a personal prescription. Keep the "
             "answer concise; detailed verified data is shown separately in the interface."},
            {"role": "user", "content":
             f"Question:\n{query}\n\nInterpreted request (routing only):\n"
             f"{json.dumps(request_plan, ensure_ascii=False)}\n\nExecuted local tool results (facts):\n"
             f"{json.dumps(evidence, ensure_ascii=False, default=str)}"}
        ]
        return self._generate_text(messages, max_new_tokens=220).strip()

    def respond_missing_data(self, query, missing_data):
        messages = [
            {"role": "system", "content":
             "You are the response layer of a nutrition prototype. The data lookup explicitly found missing data. "
             "Tell the user what record is unavailable, explain that you cannot calculate or verify nutrient values, "
             "and ask for the missing ingredient/recipe or package label. Do not guess, infer, or invent ingredients, "
             "quantities, nutrient values, substitutions, or medical advice. If supported recipes are listed, offer them."},
            {"role": "user", "content":
             f"User request: {query}\n\nMissing-data lookup result (treat as facts):\n"
             f"{json.dumps(missing_data, ensure_ascii=False, indent=2)}\n\n"
             "Respond briefly and clearly to the user. State that the requested data does not exist in this prototype."}
        ]
        return self._generate_text(messages, max_new_tokens=180)

    def generate(self, query, recipe, totals, ingredients, servings=1, recipe_data=None):
        verified = "\n".join(
            f"- {ingredients[iid]['name']}: {amount:g} g" for iid, amount in recipe
        )
        nutrient_text = ", ".join(f"{k}: {v:.1f}" for k, v in totals.items())

        messages = [
            {"role": "system", "content":
             "You are the recipe-construction layer of a dietary planning prototype. "
             "The ingredients and quantities were already validated deterministically. "
             "Do not add or remove ingredients. Do not change quantities. "
             "Do not invent nutrient values or medical advice. "
             "Construct a short, realistic cooking method using only the supplied recipe."},
            {"role": "user", "content":
             f"Request: {query}\nServing count: {servings}\nRecipe: {recipe_data.get('name') if recipe_data else 'Recipe'}\n\nVerified ingredients:\n{verified}\n\n"
             f"Computed nutrient totals (do not recalculate): {nutrient_text}\n\n"
             "Write the final recipe."}
        ]
        return self._generate_text(messages)

    def generate_method(self, query, recipe, ingredients, recipe_name, base_method=None):
        verified = "\n".join(
            f"- id={iid}; name={ingredients[iid]['name']}; amount={amount:g} g per serving"
            for iid, amount in recipe if iid in ingredients
        )
        if isinstance(base_method, str):
            base_method = [base_method]
        base_steps = "\n".join(f"- {step}" for step in (base_method or [])) or "(No stored method.)"
        allowed_ids = {str(iid) for iid, _ in recipe}
        if not allowed_ids or len(allowed_ids) != len(recipe):
            raise ValueError("The validated composition contains no foods or duplicate food IDs.")
        schema = {
            "steps": [
                {"action": "prepare | steam | boil | bake | cook | combine | serve",
                 "ingredient_ids": ["one or more IDs from the validated composition"]}
            ]
        }
        messages = [
            {"role": "system", "content":
             "You are RenAlly's cooking-step planner. Return only JSON matching the supplied schema. Choose 2 to 6 "
             "simple steps using only the listed action values and exact ingredient IDs. Every validated ingredient "
             "must appear in at least one step. Do not write free text, ingredient names, quantities, temperatures, "
             "times, seasonings, oil, water, garnishes, or other foods; the application renders the steps from your "
             "validated action and ingredient references. Use a thorough-cooking action for raw animal foods and do "
             "not suggest rinsing raw meat or seafood."},
            {"role": "user", "content":
             f"User request: {query}\nSuggested dish: {recipe_name}\nValidated foods and amounts (per serving):\n{verified}\n\n"
             f"Stored local method to adapt:\n{base_steps}\n\n"
             f"Allowed actions: {json.dumps(self.METHOD_ACTIONS)}\n"
             f"Required JSON schema: {json.dumps(schema)}"},
        ]
        raw = self._generate_text(messages, max_new_tokens=220)
        decoder = json.JSONDecoder()
        plan = None
        for index, char in enumerate(raw):
            if char != "{":
                continue
            try:
                value, _ = decoder.raw_decode(raw[index:])
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                plan = value
                break
        steps = plan.get("steps") if isinstance(plan, dict) else None
        if not isinstance(steps, list) or not 2 <= len(steps) <= 6:
            raise ValueError("The local SLM did not return a valid cooking-step plan.")

        used_ids = set()
        rendered = []
        for step in steps:
            if not isinstance(step, dict):
                raise ValueError("The cooking-step plan contains an invalid step.")
            action = step.get("action")
            ingredient_ids = step.get("ingredient_ids")
            if action not in self.METHOD_ACTIONS or not isinstance(ingredient_ids, list) or not ingredient_ids:
                raise ValueError("The cooking-step plan contains an unsupported action or missing foods.")
            identifiers = [str(iid) for iid in ingredient_ids]
            if any(iid not in allowed_ids for iid in identifiers):
                raise ValueError("The cooking-step plan references a food outside the validated composition.")
            used_ids.update(identifiers)
            foods = ", ".join(ingredients[iid]["name"] for iid in identifiers)
            rendered.append(self.METHOD_ACTIONS[action].format(foods=foods))
        if used_ids != allowed_ids:
            raise ValueError("The cooking-step plan omitted one or more validated foods.")
        return "\n".join(rendered)

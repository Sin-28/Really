"""Deterministic tests for shared Qwen configuration and culinary inference."""

import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import torch

from src.generation.qwen_generator import QwenInferenceBusyError, QwenRecipeGenerator, _mps_available
from src.recipes.adapted_recipe_culinary import AdaptedRecipeCulinaryValidationService, CULINARY_PROMPT_VERSION
from src.web.request_planner import RequestPlanner


_COMPACT_PASS = {
    "evaluations": [{
        "change_id": "change-1",
        "functional_compatibility": "COMPATIBLE",
        "quantity_appropriateness": "PLAUSIBLE",
        "cooking_compatibility": "COMPATIBLE",
        "recipe_coherence": "COHERENT",
        "reason": "The replacement serves the same recipe role.",
        "functional_coverage": [{
            "function": "SALTINESS", "importance": "ESSENTIAL", "coverage": "COVERED",
            "supporting_ingredient_ids": ["adapted-ingredient-1"],
            "supporting_process_steps": [],
            "reason": "The supplied replacement contributes seasoning to the soup.",
        }],
    }],
}


class _Inputs(dict):
    def to(self, _device):
        return self


class _FakeTokenizer:
    def __init__(self, raw="{}"):
        self.raw = raw
        self.template_calls = []
        self.rendered = None

    def apply_chat_template(self, messages, *, tokenize, **kwargs):
        self.template_calls.append({"tokenize": tokenize, **kwargs})
        self.rendered = "rendered:" + " ".join(item["content"] for item in messages)
        if tokenize:
            return _Inputs({"input_ids": torch.tensor([[1, 2, 3]])})
        return self.rendered

    def __call__(self, _text, **_kwargs):
        return _Inputs({"input_ids": torch.tensor([[1, 2, 3]])})

    def decode(self, _ids, skip_special_tokens=True):
        return self.raw


class _FakeModel:
    def __init__(self, *, device="cpu", dtype=torch.float32, block=None):
        self.param = SimpleNamespace(device=device, dtype=dtype)
        self.generation_config = SimpleNamespace(eos_token_id=2)
        self.block = block
        self.generate_calls = []

    def parameters(self):
        return iter([self.param])

    def to(self, device):
        self.param.device = device
        return self

    def generate(self, **kwargs):
        self.generate_calls.append(kwargs)
        if self.block is not None:
            self.block.wait()
        prompt = kwargs["input_ids"]
        token = torch.tensor([[9]])
        streamer = kwargs.get("streamer")
        if streamer is not None:
            streamer.put(prompt)
            streamer.put(token)
        return torch.cat((prompt, token), dim=-1)


def _bare_generator(raw=None, *, block=None):
    generator = object.__new__(QwenRecipeGenerator)
    generator.model_name = "test/Qwen"
    generator.device = "cpu"
    generator.dtype = "torch.float32"
    generator.device_fallback_reason = None
    generator.tokenizer_load_ms = 2.0
    generator.model_load_ms = 5.0
    generator.load_duration_ms = 7.0
    generator.mps_probe_ms = None
    generator.tokenizer = _FakeTokenizer(json.dumps(_COMPACT_PASS) if raw is None else raw)
    generator.model = _FakeModel(block=block)
    generator._generation_lock = threading.Lock()
    generator._diagnostics_lock = threading.Lock()
    generator._last_inference_diagnostics = None
    generator._active_inference_diagnostics = None
    return generator


def _payload(change_count=1):
    changes = [{"change_id": f"change-{index + 1}", "change_type": "REPLACED", "original_ingredient": "salt",
                "adapted_ingredient": "low-sodium soy sauce", "original_position": 0,
                "adapted_position": 0} for index in range(change_count)]
    return {
        "original_recipe": {
            "name": "Soup", "servings": 2,
            "ingredients": [{"position": 0, "ingredient_ref": "original-ingredient-1", "description": "salt",
                             "quantity": {"amount": 4, "unit": "g"},
                             "attributes": {"culinary_role": "seasoning", "fdc_id": 10}}],
            "cooking_instructions": [{"step_ref": "original-step-1", "text": "Simmer the soup."}],
        },
        "adapted_recipe": {
            "name": "Adapted soup", "servings": 2,
            "ingredients": [{"position": 0, "ingredient_ref": "adapted-ingredient-1", "description": "low-sodium soy sauce",
                             "quantity": {"amount": 6, "unit": "g"},
                             "preparation": "stirred in", "role": "seasoning"}],
            "cooking_instructions": [{"step_ref": "adapted-step-1", "text": "Simmer the soup and stir in the replacement."}],
        },
        "structured_changes": changes,
        "substitution_metadata": {"candidate_list": ["excluded from culinary prompt"]},
        "missing_information": [],
    }


class DeviceSelectionTests(unittest.TestCase):
    def test_mps_detection_requires_built_and_available_backend(self):
        with patch("src.generation.qwen_generator.torch.backends.mps.is_built", return_value=True), \
                patch("src.generation.qwen_generator.torch.backends.mps.is_available", return_value=True):
            self.assertTrue(_mps_available())
        with patch("src.generation.qwen_generator.torch.backends.mps.is_built", return_value=True), \
                patch("src.generation.qwen_generator.torch.backends.mps.is_available", return_value=False):
            self.assertFalse(_mps_available())

    def test_cpu_fallback_preserves_checkpoint_dtype(self):
        fake_model = _FakeModel(dtype=torch.bfloat16)
        with patch("src.generation.qwen_generator._mps_available", return_value=False), \
                patch("src.generation.qwen_generator.AutoTokenizer.from_pretrained", return_value=_FakeTokenizer()), \
                patch("src.generation.qwen_generator.AutoModelForCausalLM.from_pretrained", return_value=fake_model) as load, \
                patch("src.generation.qwen_generator.torch.set_num_threads"):
            generator = QwenRecipeGenerator("test/Qwen")
        self.assertEqual(generator.device, "cpu")
        self.assertEqual(generator.dtype, "torch.bfloat16")
        load.assert_called_once_with("test/Qwen")

    def test_mps_load_uses_fp16_and_falls_back_to_native_cpu_on_load_error(self):
        cpu_model = _FakeModel(dtype=torch.bfloat16)
        with patch("src.generation.qwen_generator._mps_available", return_value=True), \
                patch("src.generation.qwen_generator.AutoTokenizer.from_pretrained", return_value=_FakeTokenizer()), \
                patch("src.generation.qwen_generator.AutoModelForCausalLM.from_pretrained",
                      side_effect=[RuntimeError("unsupported MPS op"), cpu_model]) as load:
            generator = QwenRecipeGenerator("test/Qwen")
        self.assertEqual(generator.device, "cpu")
        self.assertEqual(generator.dtype, "torch.bfloat16")
        self.assertEqual(generator.device_fallback_reason, "MPS_MODEL_LOAD_FAILED_RuntimeError")
        self.assertEqual(load.call_args_list[0].kwargs["torch_dtype"], torch.float16)
        self.assertEqual(load.call_args_list[1].kwargs, {})

    def test_available_mps_selects_fp16_without_reloading_model(self):
        def load_model(_model_id, **kwargs):
            return _FakeModel(dtype=kwargs.get("torch_dtype", torch.bfloat16))

        with patch("src.generation.qwen_generator._mps_available", return_value=True), \
                patch("src.generation.qwen_generator.AutoTokenizer.from_pretrained", return_value=_FakeTokenizer()), \
                patch("src.generation.qwen_generator.AutoModelForCausalLM.from_pretrained",
                      side_effect=load_model) as load, \
                patch("src.generation.qwen_generator.QwenRecipeGenerator._probe_mps_generation"):
            generator = QwenRecipeGenerator("test/Qwen")
        self.assertEqual(generator.device, "mps")
        self.assertEqual(generator.dtype, "torch.float16")
        load.assert_called_once_with("test/Qwen", torch_dtype=torch.float16)

    def test_mps_generation_probe_failure_falls_back_before_serving_requests(self):
        with patch("src.generation.qwen_generator._mps_available", return_value=True), \
                patch("src.generation.qwen_generator.AutoTokenizer.from_pretrained", return_value=_FakeTokenizer()), \
                patch("src.generation.qwen_generator.AutoModelForCausalLM.from_pretrained",
                      side_effect=[_FakeModel(dtype=torch.float16), _FakeModel(dtype=torch.bfloat16)]) as load, \
                patch("src.generation.qwen_generator.QwenRecipeGenerator._probe_mps_generation",
                      side_effect=RuntimeError("unsupported generation kernel")), \
                patch("src.generation.qwen_generator.torch.set_num_threads"):
            generator = QwenRecipeGenerator("test/Qwen")
        self.assertEqual(generator.device, "cpu")
        self.assertEqual(generator.dtype, "torch.bfloat16")
        self.assertEqual(generator.device_fallback_reason,
                         "MPS_GENERATION_PROBE_FAILED_RuntimeError")
        self.assertEqual(load.call_count, 2)


class CulinaryGenerationTests(unittest.TestCase):
    def test_v6_prompt_compacts_context_and_versions_the_schema(self):
        generator = _bare_generator()
        result = generator.evaluate_culinary_compatibility(_payload())
        compact_user_message = generator.tokenizer.template_calls[0]
        self.assertEqual(CULINARY_PROMPT_VERSION, "adapted-recipe-culinary-v8")
        self.assertEqual(result["prompt_version"], CULINARY_PROMPT_VERSION)
        self.assertEqual(result["evaluations"][0]["change_id"], "change-1")
        self.assertNotIn("status", result)
        self.assertTrue(result["inference_diagnostics"]["json_valid"])
        rendered = generator.tokenizer.rendered
        self.assertNotIn("candidate_list", rendered)
        self.assertNotIn("fdc_id", rendered)
        self.assertNotIn('"ingredient_id"', rendered)
        self.assertIn('"q":{"amount":4,"unit":"g"}', rendered)
        self.assertIn('"id":"change-1"', rendered)
        self.assertIn('"functional_coverage"', rendered)
        self.assertIn('"supporting_evidence_ids"', rendered)
        self.assertIn('"ref":"adapted-ingredient-1"', rendered)
        self.assertEqual(compact_user_message["enable_thinking"], False)

    def test_schema_explicitly_templates_every_required_function(self):
        generator = _bare_generator()
        payload = _payload()
        payload["required_function_assessments"] = [{
            "change_id": "change-1", "functions": ["FLAVOR_BASE", "SALTINESS", "UMAMI"],
        }]
        generator.evaluate_culinary_compatibility(payload)
        rendered_schema = generator.tokenizer.rendered.split("Schema:", 1)[1]
        self.assertEqual(rendered_schema.count('"function":'), 3)
        self.assertIn('"functions":["FLAVOR_BASE","SALTINESS","UMAMI"]',
                      generator.tokenizer.rendered)

    def test_thinking_mode_is_culinary_specific_and_configurable(self):
        generator = _bare_generator()
        with patch.dict("os.environ", {"RENALLY_QWEN_CULINARY_ENABLE_THINKING": "true"}):
            result = generator.evaluate_culinary_compatibility(_payload())
        self.assertTrue(generator.tokenizer.template_calls[0]["enable_thinking"])
        self.assertTrue(result["inference_diagnostics"]["thinking_mode"])
        # Shared non-culinary tasks retain the prior default of explicit no-thinking mode.
        generator.tokenizer.raw = "{}"
        generator.evaluate_ingredient_candidates({"ingredient": "salt", "candidates": []})
        self.assertFalse(generator.tokenizer.template_calls[-1]["enable_thinking"])

    def test_output_token_budget_scales_with_changes_and_is_bounded(self):
        self.assertEqual(QwenRecipeGenerator._culinary_output_token_budget([{}]), 448)
        self.assertEqual(QwenRecipeGenerator._culinary_output_token_budget([{}] * 3), 704)
        self.assertEqual(QwenRecipeGenerator._culinary_output_token_budget([{}] * 8), 1024)

    def test_malformed_and_truncated_json_are_unavailable(self):
        for raw in ('not json', '{"s":"PASS"'):
            with self.subTest(raw=raw):
                result = _bare_generator(raw).evaluate_culinary_compatibility(_payload())
                self.assertEqual(result["status"], "UNAVAILABLE")
                self.assertEqual(result["error"], "MALFORMED_CULINARY_JSON")
                self.assertFalse(result["inference_diagnostics"]["json_valid"])

    def test_token_budget_truncation_is_reported_separately(self):
        generator = _bare_generator()

        def truncated(_messages, *, max_new_tokens, diagnostics, **_kwargs):
            diagnostics.update({"generated_tokens": max_new_tokens, "generation_ms": 2000,
                                "ended_with_eos": False})
            return '{"s":"PASS"'

        generator._generate_text = truncated
        result = generator.evaluate_culinary_compatibility(_payload())
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error"], "OUTPUT_TOKEN_LIMIT_REACHED")
        self.assertEqual(result["inference_diagnostics"]["generated_tokens"], 448)
        self.assertFalse(result["inference_diagnostics"]["json_valid"])

    def test_minimal_response_maps_to_public_validator_contract(self):
        generator = _bare_generator()
        original = {
            "id": "soup", "name": "Soup", "servings": 1,
            "ingredients": [{"id": "salt", "name": "salt", "amount_g": 4}],
            "method": ["Stir seasoning into soup."],
        }
        adapted = {
            "id": "soup-adapted", "name": "Soup", "servings": 1,
            "ingredients": [{"id": "soy", "name": "low-sodium soy sauce", "amount_g": 6,
                             "replaces_id": "salt"}],
            "method": ["Stir seasoning into soup."],
        }
        changes = [{
            "change_type": "REPLACED", "original_position": 0, "adapted_position": 0,
            "original_ingredient_id": "salt", "adapted_ingredient_id": "soy",
            "original_ingredient": "salt", "adapted_ingredient": "low-sodium soy sauce",
        }]
        service = AdaptedRecipeCulinaryValidationService(lambda: (generator, None))
        result = service.validate(original, adapted, changes)
        self.assertIsNone(result["slm_output_validation_error"])
        self.assertEqual(result["status"], "PASS")
        self.assertFalse(result["requires_review"])
        self.assertEqual(result["per_substitution_evaluations"][0]["change_id"], "change-1")
        self.assertTrue(result["inference_diagnostics"]["schema_valid"])

    def test_omitted_judgment_cannot_become_a_pass(self):
        incomplete = json.loads(json.dumps(_COMPACT_PASS))
        incomplete["evaluations"][0].pop("reason")
        generator = _bare_generator(json.dumps(incomplete))
        service = AdaptedRecipeCulinaryValidationService(lambda: (generator, None))
        source = {"id": "soup", "ingredients": [{"id": "salt", "name": "salt", "amount_g": 4}],
                  "method": ["Simmer soup."]}
        adapted = {"id": "soup-adapted", "ingredients": [{"id": "soy", "name": "soy", "amount_g": 6,
                     "replaces_id": "salt"}], "method": ["Simmer soup with soy."]}
        result = service.validate(source, adapted)
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("EVALUATION_0_MISSING_FIELD:reason", result["slm_output_schema_errors"])
        self.assertFalse(result["inference_diagnostics"]["model_schema_complete"])

    def test_json_prose_is_rejected_by_culinary_parser(self):
        generator = _bare_generator('Here is JSON: {"evaluations":[]}')
        result = generator.evaluate_culinary_compatibility(_payload())
        self.assertEqual(result["status"], "UNAVAILABLE")
        self.assertEqual(result["error"], "MALFORMED_CULINARY_JSON")

    def test_complete_json_at_output_limit_still_requires_review(self):
        generator = _bare_generator()

        def valid_but_at_limit(_messages, *, max_new_tokens, diagnostics, **_kwargs):
            diagnostics.update({"generated_tokens": max_new_tokens, "generation_ms": 1000,
                                "ended_with_eos": False})
            return json.dumps(_COMPACT_PASS)

        generator._generate_text = valid_but_at_limit
        source = {"id": "soup", "ingredients": [{"id": "salt", "name": "salt", "amount_g": 4}],
                  "method": ["Simmer soup."]}
        adapted = {"id": "soup-adapted", "ingredients": [{"id": "soy", "name": "soy", "amount_g": 6,
                     "replaces_id": "salt"}], "method": ["Simmer soup with soy."]}
        service = AdaptedRecipeCulinaryValidationService(lambda: (generator, None))
        result = service.validate(source, adapted)
        self.assertTrue(result["slm_output_valid"])
        self.assertTrue(result["inference_diagnostics"]["output_truncated"])
        self.assertEqual(result["status"], "REVIEW_REQUIRED")
        self.assertIn("MODEL_OUTPUT_TRUNCATED", result["review_reasons"])

    def test_shared_model_busy_path_fails_fast_and_does_not_overwrite_active_metrics(self):
        release = threading.Event()
        generator = _bare_generator(block=release)
        generator._generation_lock.acquire()
        generator._active_inference_diagnostics = {"generated_tokens": 7}
        try:
            with self.assertRaises(QwenInferenceBusyError):
                generator._generate_text([], fail_if_busy=True)
            self.assertEqual(generator.get_last_inference_diagnostics()["generated_tokens"], 7)
            self.assertNotIn("busy", generator.get_last_inference_diagnostics())
        finally:
            generator._active_inference_diagnostics = None
            generator._generation_lock.release()
        release.set()

    def test_qwen_methods_still_return_their_existing_shapes(self):
        generator = _bare_generator("{}")
        self.assertEqual(generator.evaluate_ingredient_candidates({"ingredient": "salt"}), {})
        # The cooking method still returns a rendered method string through the shared generator.
        generator.tokenizer.raw = '{"steps":[{"action":"boil","ingredient_ids":["rice"]},{"action":"serve","ingredient_ids":["rice"]}]}'
        result = generator.generate_method("cook rice", [("rice", 100)],
                                           {"rice": {"name": "Rice"}}, "Rice")
        self.assertIsInstance(result, str)
        self.assertIn("Boil Rice", result)
        self.assertFalse(generator.tokenizer.template_calls[-1]["enable_thinking"])


class TimeoutAndReuseTests(unittest.TestCase):
    def test_timeout_keeps_service_gate_until_worker_finishes_then_allows_retry(self):
        release = threading.Event()
        finished = threading.Event()

        class SlowGenerator:
            model_name = "mock/Qwen"
            diagnostics = {"generated_tokens": 2, "json_valid": False}

            def evaluate_culinary_compatibility(self, _payload):
                release.wait()
                finished.set()
                return {}

            def get_last_inference_diagnostics(self):
                return dict(self.diagnostics)

        service = AdaptedRecipeCulinaryValidationService(
            lambda: SlowGenerator(), inference_timeout_seconds=0.02,
        )
        result, _ = service._run_slm({})
        self.assertEqual(result["error"], "SLM_TIMEOUT")
        self.assertEqual(result["inference_diagnostics"]["generated_tokens"], 2)
        self.assertTrue(result["inference_diagnostics"]["timed_out"])
        busy, _ = service._run_slm({})
        self.assertEqual(busy["error"], "SLM_BUSY_AFTER_PRIOR_INFERENCE")
        release.set()
        self.assertTrue(finished.wait(1.0))
        retried, _ = service._run_slm({})
        self.assertEqual(retried, {})

    def test_request_planner_reuses_one_shared_generator(self):
        planner = RequestPlanner(SimpleNamespace(recipes={}, ingredients={}), object(), object())
        shared = object()
        with patch("src.generation.qwen_generator.QwenRecipeGenerator", return_value=shared):
            self.assertIs(planner.get_slm_generator()[0], shared)
            self.assertIs(planner.get_slm_generator()[0], shared)


if __name__ == "__main__":
    unittest.main()

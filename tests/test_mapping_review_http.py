import io
import json
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from src.web.http_server import make_handler


class MappingReviewHTTPTests(unittest.TestCase):
    def setUp(self):
        self.service = SimpleNamespace(
            chat=Mock(return_value={"answer": "Resolved in chat", "ingredient_mapping_enrichment": {"results": []}}),
            search_mapping_review_recipes=Mock(return_value={"results": []}),
            prepare_mapping_review=Mock(return_value={"results": []}),
        )
        self.handler_type = make_handler(self.service)

    def handler(self, path, body=None):
        handler = self.handler_type.__new__(self.handler_type)
        handler.path = path
        handler._send = Mock()
        if body is not None:
            encoded = json.dumps(body).encode("utf-8")
            handler.headers = {"Content-Length": str(len(encoded))}
            handler.rfile = io.BytesIO(encoded)
        return handler

    def test_chat_page_has_no_resolution_navigation(self):
        handler = self.handler("/")
        handler.do_GET()
        status, body, content_type = handler._send.call_args.args
        self.assertEqual(status, 200)
        self.assertNotIn("/ingredient-resolution", body)
        self.assertIn("text/html", content_type)

    def test_separate_resolution_page_is_removed(self):
        handler = self.handler("/ingredient-resolution?query=Salmon%20Steak")
        handler.do_GET()
        self.assertEqual(handler._send.call_args.args[0], 404)

    def test_separate_recipe_search_api_is_removed(self):
        handler = self.handler("/api/mapping-review/recipes?q=salmon%20steak")
        handler.do_GET()
        self.assertEqual(handler._send.call_args.args[0], 404)

    def test_separate_resolution_api_is_removed(self):
        handler = self.handler("/api/mapping-review/prepare", {"recipe_ids": ["foodcom:1"]})
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[0], 404)

    def test_chat_api_returns_ingredient_resolution(self):
        handler = self.handler("/api/chat", {"query": "Salmon Steak"})
        handler.do_POST()
        self.service.chat.assert_called_once_with({"query": "Salmon Steak"})
        status, body, _content_type = handler._send.call_args.args
        self.assertEqual(status, 200)
        self.assertIn("ingredient_mapping_enrichment", json.loads(body))

    def test_human_approval_api_is_removed(self):
        body = {"reviewer_identity": "Reviewer A", "decisions": [{"cache_key": "k", "fdc_id": 7}]}
        handler = self.handler("/api/mapping-review/approve", body)
        handler.do_POST()
        self.assertEqual(handler._send.call_args.args[0], 404)


if __name__ == "__main__":
    unittest.main()

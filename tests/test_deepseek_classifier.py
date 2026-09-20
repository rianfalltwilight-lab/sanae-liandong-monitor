"""Offline contract tests: synthetic credentials and an injected transport only."""

import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from deepseek_classifier import (  # noqa: E402
    CACHE_PREFIX, Classifier, ENDPOINT, MAX_DESCRIPTION_CHARS,
    MAX_RESPONSE_BYTES, MAX_TITLE_CHARS,
)


def api_response(rows, finish_reason="stop"):
    return json.dumps({"choices": [{"finish_reason": finish_reason,
                                   "message": {"content": json.dumps({"results": rows})}}]}).encode()


class FakeOpener:
    def __init__(self, answer=None):
        self.answer = answer
        self.calls = []

    def open(self, request, timeout):
        payload = json.loads(request.data)
        self.calls.append((request, timeout, payload))
        if isinstance(self.answer, Exception):
            raise self.answer
        if callable(self.answer):
            result = self.answer(payload)
        elif self.answer is None:
            items = json.loads(payload["messages"][1]["content"])["items"]
            result = api_response([{"id": item["id"], "is_target": True} for item in items])
        else:
            result = self.answer
        return io.BytesIO(result)


class ClassifierTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.fake_key = "synthetic-unit-test-credential"
        self.secret_file = Path(self.temp.name) / "secrets.json"
        self.secret_file.write_text(json.dumps({"deepseek_api_key": self.fake_key}), encoding="utf-8")
        self.config = {"sanae_root": self.temp.name, "deepseek_model": "deepseek-flash", "deepseek_timeout": 1}
        self.cache = {}
        self.opener = FakeOpener()

    def classifier(self, opener=None, **overrides):
        return Classifier(self.config | overrides, self.cache, opener=self.opener if opener is None else opener)

    def item(self, item_id="ctxy7n", title="team5x 3h速刷 到21.30T", description=""):
        return {"id": item_id, "title": title, "description": description, "price": 50, "stock": 7,
                "url": "https://wzyp.cn/item/ctxy7n", "chat_history": "PRIVATE CHAT MUST NOT LEAK"}

    def test_request_uses_public_fields_only_and_returns_original_ids(self):
        classifier = self.classifier()
        self.assertEqual(classifier.classify([self.item(123)]), {123: True})
        request, timeout, payload = self.opener.calls[0]
        self.assertEqual(request.full_url, ENDPOINT)
        self.assertEqual(request.get_header("Authorization"), "Bearer " + self.fake_key)
        self.assertGreater(timeout, 0)
        self.assertLessEqual(timeout, 1)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["max_tokens"], 1536)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertIn("json", payload["messages"][0]["content"].lower())
        classification_rules = payload["messages"][0]["content"]
        self.assertIn("质保或首登保障时长不等于实际使用期", classification_rules)
        self.assertIn("非轮转、用到空间死、长期普通号不能仅因质保 2h 等保障时长判为速刷", classification_rules)
        self.assertIn("Team5x 轮转号、夜车或过夜车、定点踢或 T、明确截止使用时间也属于短时使用", classification_rules)
        self.assertIn("如果描述表明只是教程，则为 false", classification_rules)
        items = json.loads(payload["messages"][1]["content"])["items"]
        self.assertEqual(set(items[0]), {"id", "title", "description"})
        self.assertEqual(items[0]["id"], "p0")
        self.assertNotIn("PRIVATE CHAT", request.data.decode())
        self.assertNotIn(self.fake_key, request.data.decode())
        self.assertNotIn(self.fake_key, json.dumps(self.cache))

    def test_success_cache_deduplicates_text_and_invalidates_on_text_or_model_change(self):
        classifier = self.classifier()
        self.assertEqual(classifier.classify([self.item("a"), self.item("b")]), {"a": True, "b": True})
        self.assertEqual(len(self.opener.calls), 1)
        classifier.classify([self.item("b")])
        self.assertEqual(len(self.opener.calls), 1)
        classifier.classify([self.item("b", description="new details")])
        self.assertEqual(len(self.opener.calls), 2)
        self.classifier(deepseek_model="deepseek-v4-pro").classify([self.item("b")])
        self.assertEqual(len(self.opener.calls), 3)
        with patch("deepseek_classifier.SYSTEM_PROMPT", "Updated json classification rules"):
            classifier.classify([self.item("b")])
        self.assertEqual(len(self.opener.calls), 4)
        self.assertTrue(all(key.startswith(CACHE_PREFIX) for key in self.cache))

    def test_false_is_a_successful_cached_result(self):
        self.opener.answer = api_response([{"id": "p0", "is_target": False}])
        classifier = self.classifier()
        self.assertEqual(classifier.classify([self.item()]), {"ctxy7n": False})
        classifier.classify([self.item()])
        self.assertEqual(len(self.opener.calls), 1)

    def test_batches_and_text_are_bounded(self):
        items = [self.item(str(index), title=str(index) + "x" * 1000, description="x" * 5000) for index in range(25)]
        result = self.classifier().classify(items)
        self.assertEqual(len(result), 25)
        sizes = []
        for _request, _timeout, payload in self.opener.calls:
            rows = json.loads(payload["messages"][1]["content"])["items"]
            sizes.append(len(rows))
            self.assertTrue(all(len(row["title"]) <= MAX_TITLE_CHARS for row in rows))
            self.assertTrue(all(len(row["description"]) <= MAX_DESCRIPTION_CHARS for row in rows))
        self.assertEqual(sizes, [12, 12, 1])

    def test_strict_response_schema_rejects_invented_or_duplicate_ids_and_non_booleans(self):
        cases = [
            [{"id": "unknown", "is_target": True}],
            [{"id": "p0", "is_target": "true"}],
            [{"id": "p0", "is_target": 1}],
            [{"id": "p0", "is_target": None}],
            [{"id": "p0", "is_target": True, "price": 1}],
            [],
        ]
        for rows in cases:
            with self.subTest(rows=rows):
                self.cache.clear()
                opener = FakeOpener(api_response(rows))
                with self.assertLogs("deepseek_classifier", level="WARNING"):
                    self.assertEqual(self.classifier(opener).classify([self.item()]), {"ctxy7n": None})
        self.cache.clear()
        opener = FakeOpener(api_response([{"id": "p0", "is_target": True}] * 2))
        with self.assertLogs("deepseek_classifier", level="WARNING"):
            result = self.classifier(opener).classify([self.item("a"), self.item("b", title="other")])
        self.assertEqual(result, {"a": None, "b": None})

    def test_http_failure_backs_off_and_never_logs_error_body_or_credentials(self):
        secret_error = self.fake_key + " PRIVATE PROVIDER BODY"
        error_body = io.BytesIO(secret_error.encode())
        self.opener.answer = urllib.error.HTTPError(ENDPOINT, 401, secret_error, {}, error_body)
        classifier = self.classifier()
        with self.assertLogs("deepseek_classifier", level="WARNING") as logs:
            self.assertEqual(classifier.classify([self.item()]), {"ctxy7n": None})
        self.assertIn("HTTPError", " ".join(logs.output))
        self.assertNotIn(self.fake_key, " ".join(logs.output))
        self.assertNotIn("PRIVATE PROVIDER BODY", " ".join(logs.output))
        self.assertTrue(error_body.closed)
        classifier.classify([self.item()])
        self.assertEqual(len(self.opener.calls), 1)
        for entry in self.cache.values():
            entry["retry_after"] = time.time() - 1
        self.opener.answer = None
        self.assertEqual(classifier.classify([self.item()]), {"ctxy7n": True})
        self.assertEqual(len(self.opener.calls), 2)

    def test_non_stop_finish_reason_rejects_even_complete_looking_json(self):
        for reason in ("length", "tool_calls", "content_filter", None):
            with self.subTest(reason=reason):
                self.cache.clear()
                opener = FakeOpener(api_response([{"id": "p0", "is_target": True}], reason))
                with self.assertLogs("deepseek_classifier", level="WARNING") as logs:
                    result = self.classifier(opener).classify([self.item()])
                self.assertEqual(result, {"ctxy7n": None})
                self.assertIn("IncompleteCompletionError", " ".join(logs.output))

    def test_missing_credentials_returns_none_without_transport_call(self):
        self.secret_file.unlink()
        with self.assertLogs("deepseek_classifier", level="WARNING") as logs:
            result = self.classifier().classify([self.item()])
        self.assertEqual(result, {"ctxy7n": None})
        self.assertEqual(self.opener.calls, [])
        self.assertIn("FileNotFoundError", " ".join(logs.output))
        self.assertNotIn(self.temp.name, " ".join(logs.output))

    def test_wall_timeout_and_inflight_guard_bound_slow_transport(self):
        release = threading.Event()
        def slow(payload):
            release.wait(2)
            return api_response([{"id": "p0", "is_target": True}])
        self.addCleanup(release.set)
        opener = FakeOpener(slow)
        classifier = self.classifier(opener, deepseek_timeout=0.04)
        start = time.monotonic()
        with self.assertLogs("deepseek_classifier", level="WARNING"):
            self.assertEqual(classifier.classify([self.item()]), {"ctxy7n": None})
            self.assertEqual(classifier.classify([self.item("new", title="different")]), {"new": None})
        self.assertLess(time.monotonic() - start, 0.3)
        self.assertEqual(len(opener.calls), 1)
        release.set()
        classifier._worker.join(timeout=1)

    def test_api_response_size_is_limited(self):
        self.opener.answer = b" " * (MAX_RESPONSE_BYTES + 1)
        with self.assertLogs("deepseek_classifier", level="WARNING"):
            self.assertEqual(self.classifier().classify([self.item()]), {"ctxy7n": None})

    def test_conflicting_duplicate_ids_and_invalid_inputs_do_not_call_api(self):
        items = [self.item("a"), self.item("a", title="conflicting"), self.item(None), self.item(True), {}, None]
        self.assertEqual(self.classifier().classify(items), {"a": None})
        self.assertEqual(self.opener.calls, [])

    def test_default_transport_explicitly_disables_environment_proxies(self):
        with patch("deepseek_classifier.urllib.request.ProxyHandler") as proxy, \
                patch("deepseek_classifier.urllib.request.build_opener") as build:
            Classifier(self.config, {})
        proxy.assert_called_once_with({})
        build.assert_called_once_with(proxy.return_value)


if __name__ == "__main__":
    unittest.main()

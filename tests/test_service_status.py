import io
import json
import tempfile
import unittest
from unittest.mock import Mock

from service_status import (
    OPENAI_STATUS_URL,
    StatusClient,
    failed_status,
    parse_summary,
    recommendation_line,
)


def response(payload):
    return io.BytesIO(json.dumps(payload).encode("utf-8"))


def summary(indicator="none", description="All Systems Operational", component_status="operational"):
    return {"status": {"indicator": indicator, "description": description},
            "components": [{"name": "API", "status": component_status}]}


class ServiceStatusTests(unittest.TestCase):
    def test_parse_operational_and_degraded_component(self):
        ok = parse_summary(summary(), service="openai", observed_at=10)
        self.assertTrue(ok["ok"])
        self.assertEqual(ok["fetched_at"], 10)
        bad = parse_summary(summary(component_status="degraded_performance"), service="openai", observed_at=11)
        self.assertTrue(bad["ok"])
        self.assertFalse(bad["healthy"])
        self.assertEqual(bad["degraded_components"], ["API"])

    def test_malformed_summary_and_unknown_indicator_fail_closed(self):
        with self.assertRaises(ValueError):
            parse_summary({}, service="openai")
        with self.assertRaises(ValueError):
            parse_summary(summary(indicator="surprise"), service="openai")
        self.assertFalse(failed_status("openai", "TimeoutError")["ok"])

    def test_client_cache_and_failure(self):
        opener = Mock()
        opener.open.side_effect = [response(summary()), response(summary())]
        client = StatusClient({"service_status": {"enabled": True, "poll_seconds": 60}}, opener=opener)
        first = client.poll(100)
        second = client.poll(120)
        self.assertTrue(first["openai"]["ok"])
        self.assertEqual(first, second)
        opener.open.assert_called_once()
        third = client.poll(161)
        self.assertEqual(opener.open.call_count, 2)
        self.assertTrue(third["openai"]["ok"])

    def test_failure_is_not_recommended_and_endpoint_is_restricted(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError()
        client = StatusClient({"service_status": {"enabled": True}}, opener=opener)
        status = client.poll(100)["openai"]
        self.assertIn("采集失败", recommendation_line({"openai": status}))
        with self.assertRaises(ValueError):
            client._request("openai", "http://status.openai.com/api/v2/summary.json", 1)

    def test_disabled_client_is_quiet(self):
        opener = Mock()
        self.assertEqual(StatusClient({}, opener=opener).poll(1), {})
        opener.open.assert_not_called()


if __name__ == "__main__":
    unittest.main()

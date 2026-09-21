import io
import json
from pathlib import Path
import tempfile
import unittest

from mdkj_source import MdkjError, fetch_shop, parse_item


class Opener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.timeout = None
    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return io.BytesIO(json.dumps(self.payload).encode("utf-8"))


class MdkjSourceTests(unittest.TestCase):
    def payload(self, items=None):
        return {"success": True, "data": {"enabled": True, "server_time": 1700000000,
                "rate": 10000, "minimum_seconds": 0,
                "items": items if items is not None else [{
                    "id": "batch-1", "title": "3h速刷 2FA", "name": "team5X 3h",
                    "quantity": 3, "created_at": 1699999000, "expires_at": 1700010800,
                    "base_cents": 5000, "base_seconds": 10800, "cap_cents": 5000,
                    "label": "常规",
                }]}}

    def test_fetch_normalizes_real_third_party_schema_and_dynamic_price(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "mdkj-token.txt"
            token_path.write_text("secret-token\n", encoding="utf-8")
            opener = Opener(self.payload())
            items = fetch_shop({"id": "mdkj", "name": "麻豆科技", "token_file": str(token_path)},
                               {"request_timeout": 3}, opener=opener)
        self.assertEqual(len(items), 1)
        item = items[0]
        self.assertEqual(item["id"], "mdkj:third-party:batch-1")
        self.assertEqual(item["stock"], 3)
        self.assertEqual(item["price"], "50.00")
        self.assertEqual(item["url"], "https://mdkj.team/shop/?view=third-party")
        self.assertTrue(item["target"])
        self.assertIn("到期：2023-11-15", item["description"])
        self.assertEqual(opener.request.get_method(), "GET")
        self.assertEqual(opener.request.get_header("X-customer-token"), "secret-token")
        self.assertEqual(opener.request.full_url, "https://mdkj.team/shop/api/third-party")
        self.assertEqual(opener.timeout, 3.0)

    def test_expired_or_zero_stock_is_inactive_but_snapshot_is_valid(self):
        row = self.payload()["data"]["items"][0]
        row["quantity"] = 0
        row["expires_at"] = 1699999999
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("t", encoding="utf-8")
            items = fetch_shop({"id": "m", "name": "麻豆", "token_file": str(token_path)},
                               {}, opener=Opener(self.payload([row])))
        self.assertFalse(items[0]["active"])
        self.assertEqual(items[0]["stock"], 0)

    def test_invalid_schema_and_token_do_not_leak_secret(self):
        with tempfile.TemporaryDirectory() as directory:
            token_path = Path(directory) / "token"
            token_path.write_text("secret", encoding="utf-8")
            with self.assertRaisesRegex(MdkjError, "invalid_mdkj_schema"):
                fetch_shop({"id": "m", "name": "麻豆", "token_file": str(token_path)},
                           {}, opener=Opener({"success": True, "data": {}}))
            with self.assertRaisesRegex(MdkjError, "token_file_unreadable"):
                fetch_shop({"id": "m", "name": "麻豆", "token_file": str(token_path)+"-missing"}, {})

    def test_parse_rejects_untrusted_batch_fields(self):
        row = self.payload()["data"]["items"][0]
        row["id"] = "bad id"
        with self.assertRaisesRegex(MdkjError, "invalid_mdkj_batch_id"):
            parse_item(row, {"id": "m", "name": "麻豆"}, self.payload()["data"])


if __name__ == "__main__":
    unittest.main()

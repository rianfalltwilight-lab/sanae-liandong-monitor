"""Sanitized public catalog fixtures and failures that must retain old state."""
import copy
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from shop_source import CatalogError, fetch_shop, parse_item, _SameSiteRedirect

SHOP = {"id": "K4AWDKG6", "token": "K4AWDKG6", "name": "商家6635"}
CONFIG = {"request_timeout": 10}


def fixture(token="K4AWDKG6", page=1):
    return json.loads((ROOT / "tests" / "fixtures" / "catalog" / f"{token}-page-{page}.json").read_text(encoding="utf-8"))


class FakeOpener:
    def __init__(self, *pages):
        self.pages = iter(pages)
        self.calls = []
        self.requests = []

    def open(self, request, timeout):
        self.calls.append((json.loads(request.data), timeout))
        self.requests.append(request)
        page = next(self.pages)
        if isinstance(page, Exception):
            raise page
        return io.BytesIO(json.dumps(page, ensure_ascii=False).encode("utf-8"))


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.page = fixture()
        self.row = copy.deepcopy(next(r for r in self.page["data"]["list"] if r["goods_key"] == "ctxy7n"))

    def test_all_four_real_shop_snapshots(self):
        for token, count in (("K4AWDKG6", 38), ("13QL6FLR", 140), ("oaoi", 11), ("hzapi", 14)):
            with self.subTest(token=token):
                pages = [fixture(token, p) for p in range(1, (count + 99) // 100 + 1)]
                opener = FakeOpener(*pages)
                items = fetch_shop({"id": token, "token": token, "name": token}, CONFIG, opener=opener)
                self.assertEqual(len(items), count)
                self.assertEqual(len({i["id"] for i in items}), count)
                self.assertTrue(all(i["stock"] is not None for i in items))
                self.assertEqual([c[0]["current"] for c in opener.calls], list(range(1, len(pages) + 1)))
                self.assertTrue(all(c[1] == 10 for c in opener.calls))
                self.assertTrue(all("category_id" not in c[0] for c in opener.calls))
                self.assertTrue(all(r.full_url == "https://wzyp.cn/shopApi/Shop/goodsList" for r in opener.requests))
                self.assertTrue(all(i["url"] == "https://wzyp.cn/item/" + i["id"] for i in items))

    def test_catfk_origin_controls_catalog_request_and_purchase_url(self):
        shop = {"id": "GPTsgrandpa", "token": "GPTsgrandpa", "name": "GPTsgrandpa",
                "base_url": "https://catfk.com"}
        opener = FakeOpener(fixture("GPTsgrandpa"))
        items = fetch_shop(shop, CONFIG, opener=opener)
        self.assertEqual(len(items), 3)
        self.assertEqual(opener.requests[0].full_url, "https://catfk.com/shopApi/Shop/goodsList")
        self.assertEqual(opener.requests[0].method, "POST")
        self.assertEqual(opener.calls[0][0]["token"], "GPTsgrandpa")
        self.assertEqual(items[0]["url"], "https://catfk.com/item/pfg6k3")
        self.assertEqual((items[0]["price"], items[0]["stock"]), ("45", 11))
        self.assertTrue(all(item["shop_id"] == shop["id"] for item in items))

    def test_invalid_origin_rejected_before_transport_creation_or_network(self):
        bad_origins = (None, 42, [], "", "http://catfk.com", "https://catfk.com/",
                       "https://catfk.com:443", "https://CATFK.COM", "https://www.catfk.com",
                       "https://catfk.com/item/pfg6k3", "https://catfk.com?x=1", "https://catfk.com#x",
                       "https://user@catfk.com", "https://catfk.com.evil.invalid", "https://evil.invalid",
                       "https://wzyp.cn/", " https://catfk.com")
        for origin in bad_origins:
            with self.subTest(origin=origin):
                shop = SHOP | {"base_url": origin}
                opener = FakeOpener(self.page)
                with self.assertRaisesRegex(CatalogError, "invalid_shop_base_url"):
                    fetch_shop(shop, CONFIG, opener=opener)
                self.assertEqual(opener.calls, [])
                with patch("shop_source.urllib.request.build_opener") as build:
                    with self.assertRaisesRegex(CatalogError, "invalid_shop_base_url"):
                        fetch_shop(shop, CONFIG)
                    build.assert_not_called()
                with self.assertRaisesRegex(CatalogError, "invalid_shop_base_url"):
                    parse_item(self.row, shop)

    def test_redirect_must_remain_on_selected_exact_origin(self):
        for origin, other in (("https://wzyp.cn", "https://catfk.com"),
                              ("https://catfk.com", "https://wzyp.cn")):
            with self.subTest(origin=origin):
                handler = _SameSiteRedirect(origin)
                request = urllib.request.Request(origin + "/shopApi/Shop/goodsList", data=b"{}", method="POST")
                redirected = handler.redirect_request(request, None, 302, "Found", {}, origin + "/new-catalog")
                self.assertEqual(redirected.full_url, origin + "/new-catalog")
                for target in (other + "/new-catalog", origin.replace("https:", "http:") + "/x",
                               origin + ".evil.invalid/x", origin + ":443/x",
                               origin.replace("https://", "https://user@") + "/x"):
                    with self.assertRaisesRegex(CatalogError, "unexpected_catalog_redirect"):
                        handler.redirect_request(request, None, 302, "Found", {}, target)

    def test_default_transport_uses_selected_origin_redirect_guard(self):
        for origin in ("https://wzyp.cn", "https://catfk.com"):
            with self.subTest(origin=origin):
                opener = FakeOpener(self.page)
                with patch("shop_source.urllib.request.build_opener", return_value=opener) as build:
                    fetch_shop(SHOP | {"base_url": origin}, CONFIG)
                handler = build.call_args.args[1]
                self.assertIsInstance(handler, _SameSiteRedirect)
                self.assertEqual(handler.base_url, origin)

    def test_actual_target_fields(self):
        item = parse_item(self.row, SHOP)
        self.assertEqual(item["id"], "ctxy7n")
        self.assertEqual(item["title"], "team5x 3h速刷 到21.30T")
        self.assertEqual(item["price"], "50")
        self.assertEqual(item["stock"], 0)
        self.assertTrue(item["active"])
        self.assertNotIn("<p>", item["description"])

    def test_stock_display_flag_does_not_fabricate_count(self):
        self.row["extend"] = {"stock_count": 40, "show_stock_type": 0}
        item = parse_item(self.row, SHOP)
        self.assertEqual((item["stock"], item["stock_display"]), (40, "band"))
        for value in (None, True, -1, "很多", 1.5):
            self.row["extend"]["stock_count"] = value
            self.assertIsNone(parse_item(self.row, SHOP)["stock"])
        self.row["extend"] = {"stock_count": 0}
        self.assertIsNone(parse_item(self.row, SHOP)["stock"])

    def test_missing_or_nonfinite_price_rejects_snapshot(self):
        for value in (None, True, -1, "NaN", "Infinity", "待定"):
            self.row["price"] = value
            with self.assertRaises(CatalogError):
                parse_item(self.row, SHOP)
        self.row["price"] = Decimal("0.123456789")
        self.assertEqual(parse_item(self.row, SHOP)["price"], "0.123456789")

    def test_wrong_shop_identity_rejects_snapshot(self):
        self.row["user"]["token"] = "another-shop"
        with self.assertRaisesRegex(CatalogError, "identity"):
            parse_item(self.row, SHOP)

    def test_url_is_derived_from_validated_key(self):
        self.row["link"] = "https://untrusted.invalid/example"
        self.assertEqual(parse_item(self.row, SHOP)["url"], "https://wzyp.cn/item/ctxy7n")
        self.row["goods_key"] = "../other"
        with self.assertRaises(CatalogError):
            parse_item(self.row, SHOP)

    def test_html_description_is_plain_data(self):
        self.row["description"] = "<p>A&amp;B</p><script>do not send</script><p>text</p>"
        self.assertEqual(parse_item(self.row, SHOP)["description"], "A&B text")

    def test_partial_second_page_raises(self):
        self.page["data"]["total"] += 1
        with self.assertRaisesRegex(CatalogError, "incomplete"):
            fetch_shop(SHOP, CONFIG, opener=FakeOpener(self.page,
                {"code": 1, "data": {"total": self.page["data"]["total"], "list": []}}))

    def test_failed_second_page_raises(self):
        self.page["data"]["total"] += 1
        with self.assertRaises(TimeoutError):
            fetch_shop(SHOP, CONFIG, opener=FakeOpener(self.page, TimeoutError()))

    def test_total_change_and_duplicate_reject(self):
        self.page["data"]["total"] += 1
        next_page = copy.deepcopy(self.page)
        next_page["data"]["list"] = [next_page["data"]["list"][0]]
        with self.assertRaisesRegex(CatalogError, "duplicate"):
            fetch_shop(SHOP, CONFIG, opener=FakeOpener(self.page, next_page))
        next_page["data"]["total"] += 1
        with self.assertRaisesRegex(CatalogError, "changed"):
            fetch_shop(SHOP, CONFIG, opener=FakeOpener(self.page, next_page))

    def test_page_limit_and_api_failure(self):
        self.page["data"]["total"] = 5000
        with self.assertRaises(CatalogError):
            fetch_shop(SHOP, CONFIG, opener=FakeOpener(self.page))
        with self.assertRaisesRegex(CatalogError, "business"):
            fetch_shop(SHOP, CONFIG, opener=FakeOpener({"code": 0, "msg": "busy"}))


if __name__ == "__main__":
    unittest.main()

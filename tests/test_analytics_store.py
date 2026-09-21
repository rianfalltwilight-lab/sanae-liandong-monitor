from datetime import datetime, timezone, timedelta
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from analytics_store import AnalyticsStore, HEARTBEAT_SECONDS

CST = timezone(timedelta(hours=8))


def epoch(value):
    return datetime.fromisoformat(value).timestamp()


def product(**changes):
    item = dict(id="item-1", shop_id="a", shop_name="店 A", title="team5x 3h速刷", description="三小时",
                url="https://example.test/item/one", price="45.00", stock=8, active=True, target=True,
                classification_pending=False)
    item.update(changes)
    return item


class AnalyticsStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "nested" / "history.sqlite3"
        self.store = AnalyticsStore(self.path)
        self.shops = [dict(id="a", name="店 A", token="do-not-store-config-secret"), dict(id="b", name="店 B")]
        self.start = epoch("2025-04-06T10:00:00+08:00")

    def record(self, item=None, *, offset=0, successful=("a",), attempted=("a",), items=None):
        if items is None:
            item = item if item is not None else product()
            items = {item["id"]: item}
        self.store.record_poll(items, self.shops, list(successful), list(attempted),
                              {"a": {"error": "do-not-store-error-secret"}}, self.start + offset)

    def day(self, value="2025-04-06"):
        return self.store.build_day(value)

    def test_success_failure_empty_and_skipped_are_distinct(self):
        self.record()
        self.record(product(price="1", stock=0), offset=20, successful=(), attempted=("a",))
        self.record(product(price="1"), offset=40, successful=(), attempted=("b",))
        self.record(items={}, offset=60)
        report = self.day()
        self.assertEqual([row["success"] for row in report["series"]], [True, False, False, True])
        self.assertEqual([row["min_price"] for row in report["series"]], [45, None, None, None])
        self.assertEqual([row["target_count"] for row in report["series"]], [1, None, None, 0])
        self.assertEqual(len(report["observations"]), 2)
        self.assertTrue(report["observations"][0]["active"])
        self.assertFalse(report["observations"][1]["active"])
        self.assertIsNone(report["observations"][1]["stock"])
        self.assertEqual(report["shops"][0]["min_price"], 45)
        self.assertEqual(report["shops"][0]["successes"], 2)

    def test_soldout_is_observed_zero_and_unknown_stock_not_zero(self):
        self.record()
        self.record(product(stock=0), offset=20)
        self.record(product(stock=None), offset=40)
        report = self.day()
        self.assertEqual([row["stock"] for row in report["observations"]], [8, 0, None])
        self.assertEqual([row["min_price"] for row in report["series"]], [45, None, None])
        self.assertEqual(report["products"][0]["first_stock"], 8)
        self.assertIsNone(report["products"][0]["last_stock"])

    def test_same_price_dedupes_until_heartbeat_but_shop_ticks_remain(self):
        self.record()
        self.record(offset=20)
        self.record(offset=HEARTBEAT_SECONDS - 1)
        self.record(offset=HEARTBEAT_SECONDS)
        report = self.day()
        self.assertEqual(len(report["series"]), 4)
        self.assertEqual(len(report["observations"]), 2)
        self.assertEqual(report["products"][0]["price_changes"], 0)

    def test_inactive_history_does_not_grow_by_heartbeat(self):
        self.record()
        self.record(product(active=False), offset=20)
        self.record(product(active=False), offset=400)
        self.assertEqual(len(self.day()["observations"]), 2)

    def test_recycled_title_or_description_creates_separate_editions(self):
        self.record()
        self.record(product(title="team5x 6h速刷", price="70"), offset=20)
        self.record(product(title="team5x 6h速刷", description="six hours", price="75"), offset=40)
        report = self.day()
        self.assertEqual(len(report["products"]), 3)
        self.assertEqual(len({row["edition"] for row in report["products"]}), 3)
        self.assertTrue(all(row["price_changes"] == 0 for row in report["products"]))
        self.assertEqual(sum(not row["active"] for row in report["observations"]), 2)

    def test_recycled_unrelated_title_only_closes_old_target(self):
        self.record()
        self.record(product(title="Unrelated Plus account", target=False), offset=20)
        self.record(product(title="Unrelated Plus account", target=False), offset=400)
        report = self.day()
        self.assertEqual(len(report["products"]), 1)
        self.assertEqual(len(report["observations"]), 2)
        self.assertFalse(report["observations"][-1]["active"])
        self.assertEqual(report["shops"][0]["latest_stock_targets"], 0)

    def test_pending_is_counted_without_unconfirmed_product_history(self):
        self.record(product(classification_pending=True))
        report = self.day()
        self.assertEqual(report["products"], [])
        self.assertEqual(report["series"][0]["pending_count"], 1)
        self.assertIsNone(report["series"][0]["min_price"])
        self.record(offset=20)
        self.record(product(classification_pending=True), offset=40)
        report = self.day()
        self.assertEqual(len(report["products"]), 1)
        self.assertEqual(len(report["observations"]), 2)
        self.assertTrue(report["observations"][-1]["classification_pending"])

    def test_rounds_decimal_half_up_and_rejects_invalid_prices(self):
        prices = ["40.005", "40.015", "NaN", "Infinity", "-1", None, True, "1e1000", "0"]
        for index, value in enumerate(prices):
            self.record(product(price=value), offset=index * 20)
        report = self.day()
        self.assertEqual([row["min_price"] for row in report["series"]],
                         [40.01, 40.02, None, None, None, None, None, None, 0])
        self.assertEqual(report["shops"][0]["min_price"], 0)
        with closing(sqlite3.connect(self.path)) as db:
            self.assertEqual(db.execute("SELECT min_price_cents FROM shop_ticks ORDER BY at LIMIT 1").fetchone()[0], 4001)
        json.dumps(report, allow_nan=False)

    def test_same_edition_price_movement_is_separate_from_shop_minimum(self):
        self.record(items={"item-1": product(), "other": product(id="other", title="team5x 5h速刷", price="70")})
        self.record(items={"item-1": product(price="46"), "other": product(id="other", title="team5x 5h速刷", price="30")}, offset=20)
        report = self.day()
        self.assertEqual(report["shops"][0]["first_price"], 45)
        self.assertEqual(report["shops"][0]["last_price"], 30)
        self.assertEqual([row["price_changes"] for row in report["products"]], [1, 1])
        self.assertTrue(any("同一商品版本" in line for line in report["analysis"]))
        self.assertTrue(any("最低价商品可能变化" in line for line in report["notes"]))

    def test_cst_midnight_boundary_and_recorded_dates(self):
        self.start = epoch("2025-04-06T23:59:59+08:00")
        self.record()
        self.record(product(price="39"), offset=1)
        self.assertEqual(self.day()["series"][0]["at"], "2025-04-06T23:59:59+08:00")
        tomorrow = self.day("2025-04-07")
        self.assertEqual(len(tomorrow["series"]), 1)
        self.assertEqual(tomorrow["series"][0]["at"], "2025-04-07T00:00:00+08:00")
        self.assertEqual(tomorrow["shops"][0]["min_price"], 39)
        self.assertEqual(self.store.recorded_dates(), ["2025-04-06", "2025-04-07"])

    def test_empty_day_includes_configured_roster_and_no_fabricated_history(self):
        self.shops[1].pop("name")
        self.record(items={}, attempted=(), successful=())
        report = self.day("2025-04-05")
        self.assertEqual([row["shop_name"] for row in report["shops"]], ["店 A", "b"])
        self.assertTrue(report["partial"])
        self.assertIsNone(report["first_observed"])
        self.assertTrue(all(row["attempts"] == 0 for row in report["shops"]))
        self.assertEqual(report["observations"], [])
        self.assertEqual(self.store.recorded_dates(), [])

    def test_restart_preserves_dedupe_and_registry(self):
        self.record()
        self.store = AnalyticsStore(self.path)
        self.record(offset=20)
        self.record(product(price="40"), offset=40)
        report = self.day()
        self.assertEqual(len(report["observations"]), 2)
        self.assertEqual(report["products"][0]["price_changes"], 1)
        self.assertEqual(report["recording_started_at"], "2025-04-06T10:00:00+08:00")

    def test_failed_transaction_does_not_leave_partial_tick_or_metadata(self):
        with patch.object(self.store, "_append", side_effect=RuntimeError("injected_write_failure")):
            with self.assertRaisesRegex(RuntimeError, "injected_write_failure"):
                self.record()
        self.assertEqual(self.store.recorded_dates(), [])
        self.assertEqual(self.day()["shops"], [])
        self.record()
        self.assertEqual(len(self.day()["observations"]), 1)

    def test_out_of_order_poll_rejected_without_overwriting_history(self):
        self.record(offset=20)
        with self.assertRaisesRegex(ValueError, "out_of_order"):
            self.record(offset=0)
        self.assertEqual(len(self.day()["series"]), 1)

    def test_malicious_text_is_data_and_secrets_are_not_stored(self):
        title = "=CMD('x'); DROP TABLE shops; <script>alert(1)</script>[CQ:at,qq=all]"
        self.record(product(title=title, description="do-not-store-description-secret",
                            url="https://example.test/item/one?token=do-not-store-url-secret#fragment"))
        report = self.day()
        self.assertEqual(report["observations"][0]["title"], title)
        self.assertEqual(report["observations"][0]["url"], "https://example.test/item/one")
        self.assertEqual(len(report["shops"]), 2)
        for candidate in self.path.parent.iterdir():
            data = candidate.read_bytes()
            for forbidden in (b"do-not-store-description-secret", b"do-not-store-config-secret",
                              b"do-not-store-error-secret", b"do-not-store-url-secret"):
                self.assertNotIn(forbidden, data)

    def test_daily_coverage_distinguishes_full_from_partial(self):
        self.shops = [self.shops[0]]
        self.start = epoch("2025-04-06T00:00:00+08:00")
        for offset in range(0, 86400, 600):
            self.record(offset=offset)
        self.assertFalse(self.day()["partial"])
        self.assertTrue(self.day("2025-04-07")["partial"])

    def test_future_and_noncanonical_dates(self):
        self.record()
        with self.assertRaises(ValueError):
            self.day("20250406")
        self.assertTrue(self.day("2999-01-01")["partial"])


if __name__ == "__main__":
    unittest.main()

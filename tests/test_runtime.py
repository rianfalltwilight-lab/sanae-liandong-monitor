import io
import json
from pathlib import Path
import tempfile
import time
import unittest

from shop_monitor import Monitor, load_state, send_onebot


def product(stock=4, price="50", title="team5x 3h速刷", target=True):
    return dict(id="ctxy7n", shop_id="6635", shop_name="6635", title=title,
                price=price, stock=stock, url="https://wzyp.cn/item/ctxy7n", active=True, target=target)


class NoAI:
    def __init__(self, config, cache):
        pass
    def classify(self, items):
        return {i["id"]: True for i in items}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "state.json"
        self.config = dict(shops=[{"id": "6635"}], poll_seconds=20, max_backoff_seconds=600,
                           pending_ttl_seconds=120, group_id=1092470719, onebot_url="http://127.0.0.1:3002")
        self.catalog = [product()]
        self.messages = []
        self.mentions = []
        self.monitor = Monitor(self.config, self.path, sender=self.send,
            fetcher=lambda s, c: [dict(i) for i in self.catalog], classifier_type=NoAI)

    def send(self, cfg, message, *, mentions=()):
        self.messages.append(message)
        self.mentions.append(tuple(mentions))
        return len(self.messages)

    def poll(self):
        for h in self.monitor.state["shop_health"].values():
            h["next_attempt"] = 0
        return self.monitor.poll()

    def test_initial_then_unchanged_restart_no_duplicate(self):
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.monitor = Monitor(self.config, self.path, sender=self.send,
            fetcher=lambda s, c: [dict(i) for i in self.catalog], classifier_type=NoAI)
        self.poll()
        self.assertEqual(len(self.messages), 1)

    def test_failed_catalog_retains_inventory_and_excludes_minimum(self):
        self.poll()
        def fail(*args):
            raise TimeoutError()
        self.monitor.fetcher = fail
        self.poll()
        self.assertTrue(self.monitor.state["items"]["ctxy7n"]["active"])
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.monitor.fresh_items(time.time()), {})
        self.assertGreater(self.monitor.state["shop_health"]["6635"]["next_attempt"], time.time())

    def test_delivery_failure_is_durable_and_retried(self):
        def fail(*args):
            raise RuntimeError("unavailable")
        self.monitor.sender = fail
        self.poll()
        self.assertEqual(len(load_state(self.path)["pending"]), 1)
        self.monitor.sender = self.send
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.monitor.state["pending"], [])

    def test_malformed_partial_catalog_does_not_commit_or_emit_removed(self):
        self.poll()
        self.catalog = [{"id": "bad", "shop_id": "6635"}]
        self.poll()
        self.assertTrue(self.monitor.state["items"]["ctxy7n"]["active"])
        self.assertNotIn("bad", self.monitor.state["items"])
        self.assertEqual(len(self.messages), 1)

    def test_sold_out_never_new_minimum_and_restock_notifies(self):
        self.catalog = [product(stock=0)]
        self.poll()
        self.assertEqual(len(self.messages), 0)
        self.catalog = [product(stock=3)]
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.assertIn("新上架", self.messages[0])
        self.assertIn("库存 3", self.messages[0])

    def test_stale_unsent_message_expires(self):
        self.monitor.state["pending"] = [dict(text="old", id="old", created=time.time()-121)]
        self.monitor.deliver(time.time())
        self.assertEqual(self.messages, [])
        self.assertEqual(self.monitor.state["pending"], [])

    def test_expired_notification_recovers_with_current_available_items(self):
        def fail(*args):
            raise RuntimeError()
        self.monitor.sender = fail
        self.poll()
        self.monitor.state["pending"][0]["created"] -= 130
        self.monitor.sender = self.send
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.assertIn("新上架·延迟送达", self.messages[0])

    def test_ambiguous_target_title_change_does_not_repeat_new(self):
        self.catalog = [product(title="5x-Team 19点踢")]
        self.poll()
        self.catalog = [product(title="5x-Team 20点踢")]
        self.poll()
        self.assertEqual(len(self.messages), 1)

    def test_price_ordinary_stock_and_removal_are_silent(self):
        self.poll()
        self.catalog = [product(stock=3, price="40")]
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.catalog = []
        self.poll()
        self.assertFalse(self.monitor.state["items"]["ctxy7n"]["active"])
        self.assertEqual(len(self.messages), 1)

    def test_ai_new_target_alert(self):
        self.catalog = [product(title="5x-Team-subjson格式 10点40左右踢", target=False)]
        self.poll()
        self.assertTrue(self.monitor.state["items"]["ctxy7n"]["target"])
        self.assertEqual(len(self.messages), 1)

    def test_utf8_group_literal_text_and_business_failure(self):
        class Opener:
            result = {"status": "ok", "retcode": 0, "data": {"message_id": 123}}
            def open(inner, request, timeout):
                inner.body = json.loads(request.data.decode("utf-8"))
                return io.BytesIO(json.dumps(inner.result).encode())
        opener = Opener()
        self.assertEqual(send_onebot(self.config, "中文[CQ:at,qq=all]", opener), 123)
        self.assertEqual(opener.body["group_id"], 1092470719)
        self.assertEqual(opener.body["message"][0]["type"], "text")
        opener.result = {"status": "failed", "retcode": 1}
        with self.assertRaises(RuntimeError):
            send_onebot(self.config, "text", opener)
        with self.assertRaises(ValueError):
            send_onebot(dict(self.config, group_id=1), "text", opener)

    def test_low_price_2fa_mentions_only_new_and_last_five(self):
        self.catalog = [product(title="team5x 3h速刷 账密2FA", price="39.99", stock=4)]
        self.poll()
        self.assertEqual(self.mentions, [("3294692833", "1920924896")])
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.catalog[0]["stock"] = 3
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.catalog[0]["stock"] = 6
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.catalog[0]["price"] = "38"
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.catalog[0]["stock"] = 5
        self.poll()
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.mentions[-1], ("3294692833", "1920924896"))
        self.assertIn("仅剩5个以内", self.messages[-1])
        self.catalog[0]["stock"] = 4
        self.poll()
        self.assertEqual(len(self.messages), 2)

    def test_policy_migration_discards_old_pending_without_resending_catalog(self):
        self.catalog = [product(title="team5x 3h速刷 账密2FA", price="39")]
        self.poll()
        self.monitor.state.pop("notification_policy")
        self.monitor.state["pending"] = [{"text":"旧库存变动", "id":"old", "created":time.time()}]
        self.monitor.save()
        self.monitor = Monitor(self.config, self.path, sender=self.send,
            fetcher=lambda s,c: [dict(i) for i in self.catalog], classifier_type=NoAI)
        self.poll()
        self.assertEqual(len(self.messages), 1)
        self.assertEqual(self.monitor.state["pending"], [])

    def test_pending_last_five_preserves_kind_or_drops_on_replenishment(self):
        self.catalog = [product(stock=10)]
        self.poll()
        def fail(*args, **kwargs):
            raise RuntimeError()
        self.monitor.sender = fail
        self.catalog[0]["stock"] = 5
        self.poll()
        self.catalog[0]["stock"] = 3
        self.monitor.sender = self.send
        self.poll()
        self.assertIn("仅剩5个以内", self.messages[-1])
        self.assertNotIn("新上架", self.messages[-1])
        self.catalog[0]["stock"] = 10
        self.poll()
        self.monitor.sender = fail
        self.catalog[0]["stock"] = 5
        self.poll()
        self.catalog[0]["stock"] = 8
        self.monitor.sender = self.send
        self.poll()
        self.assertEqual(len(self.messages), 2)
        self.assertEqual(self.monitor.state["pending"], [])

    def test_priority_pending_loses_mentions_after_price_reaches_40(self):
        def fail(*args, **kwargs):
            raise RuntimeError()
        self.monitor.sender = fail
        self.catalog = [product(title="team5x 3h速刷 账密2FA", price="39")]
        self.poll()
        self.catalog[0]["price"] = "40"
        self.monitor.sender = self.send
        self.poll()
        self.assertEqual(self.mentions, [()])

    def test_priority_pending_is_not_sent_after_sellout(self):
        def fail(*args, **kwargs):
            raise RuntimeError()
        self.monitor.sender = fail
        self.catalog = [product(title="team5x 3h速刷 账密2FA", price="39")]
        self.poll()
        self.catalog[0]["stock"] = 0
        self.monitor.sender = self.send
        self.poll()
        self.assertEqual(self.mentions, [])
        self.assertEqual(self.monitor.state["pending"], [])

    def test_real_at_segments_and_only_authorized_targets(self):
        class Opener:
            def open(inner, request, timeout):
                inner.body = json.loads(request.data)
                return io.BytesIO(b'{"status":"ok","retcode":0,"data":{"message_id":1}}')
        opener = Opener()
        send_onebot(self.config, "醒目提醒[CQ:at,qq=all]", opener,
                    mentions=("3294692833", "1920924896"))
        self.assertEqual([s["data"]["qq"] for s in opener.body["message"] if s["type"] == "at"],
                         ["3294692833", "1920924896"])
        self.assertIn("[CQ:at,qq=all]", opener.body["message"][-1]["data"]["text"])
        with self.assertRaises(ValueError):
            send_onebot(self.config, "x", opener, mentions=("all",))


if __name__ == "__main__":
    unittest.main()

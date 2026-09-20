import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from monitor_core import make_events, render_messages, rule_classify


def product(item_id="a", **changes):
    item = {
        "id": item_id, "shop_id": "6635", "shop_name": "商家6635",
        "title": "[卡密] team5x 3h速刷 到21.30T", "price": "50",
        "stock": 7, "url": "https://wzyp.cn/item/" + item_id,
        "active": True, "target": True,
    }
    item.update(changes)
    return item


class ClassificationTests(unittest.TestCase):
    def test_explicit_fast_or_short_use(self):
        for title in (
            "[卡密] team5x 3h速刷 到21.30T", "5x-Team 短租", "TEAM 5X 可用 2 小时",
            "ｔｅａｍ５ｘ 速刷", "5×-team 快刷", "team5x 轮转3h", "team-5x 0.5小时临时用",
        ):
            with self.subTest(title=title):
                self.assertIs(rule_classify(title), True)

    def test_unrelated_and_plus_accounts_are_not_targets(self):
        for title in ("Plus成品号-已接🐎", "Plus 3h速刷", "普通商品", "", "ChatGPT Plus 会员"):
            with self.subTest(title=title):
                self.assertIs(rule_classify(title), False)

    def test_ambiguous_team_is_left_to_classifier(self):
        for title in (
            "5x-Team-subjson格式【散户勿拍】【10点40左右踢】", "team5x 月租",
            "Team 短租", "5x 速刷", "team5x", "team5x 3h", "TEAM 5X 2 小时",
        ):
            with self.subTest(title=title):
                self.assertIsNone(rule_classify(title))

    def test_real_warranty_durations_do_not_establish_short_usage(self):
        for title in (
            "5x team （非轮转质保2h）9月20日00:10发车",
            "5x team 质保首登1小时",
            "5x team 质保2h",
            "5x team 首登1小时",
            "5x team 1小时质保",
            "5x team 首登可用1小时",
            "5x team 轮转 质保1小时",
            "5x team 短时质保首登1小时",
        ):
            with self.subTest(title=title):
                self.assertIsNone(rule_classify(title))

    def test_real_usage_duration_remains_positive_alongside_warranty(self):
        for title in (
            "5x team 7H轮转-质保首登1小时",
            "5x team 轮转7H-质保首登1小时",
            "5x team 质保首登1小时，使用3小时",
            "5x team 使用时长：7小时 1小时质保",
            "5x team 7h轮转含1小时质保",
        ):
            with self.subTest(title=title):
                self.assertIs(rule_classify(title), True)

    def test_non_rotating_lifetime_and_night_listings_are_ambiguous(self):
        for title in (
            "5x team 非轮转 7h", "5x team 不轮转 可用3小时", "5x team 用到空间死 3小时",
            "5x team 夜车 1小时质保", "5x team 过夜车", "5x team 长期 可用3小时",
            "5x team 非速刷 质保首登1小时", "5x team 不是速刷 可用3小时",
        ):
            with self.subTest(title=title):
                self.assertIsNone(rule_classify(title))

    def test_explicit_fast_use_is_not_hidden_by_other_qualifiers(self):
        for title in ("5x team 非轮转 3h速刷", "5x team 速刷 质保首登1小时"):
            with self.subTest(title=title):
                self.assertIs(rule_classify(title), True)


class EventTests(unittest.TestCase):
    def test_initial_snapshot_alerts_positive_and_unknown_but_not_empty_stock(self):
        current = {item["id"]: item for item in (
            product("yes"), product("zero", stock=0), product("unknown", stock=None),
            product("inactive", active=False), product("other", target=False),
        )}
        events = make_events({}, current, set())
        self.assertEqual([(event["kind"], event["item"]["id"]) for event in events], [("new", "yes"), ("new", "unknown")])

    def test_subsequent_new_unknown_listing_is_explicitly_allowed(self):
        events = make_events({}, {"a": product(stock=None)}, {"6635"})
        self.assertEqual([event["kind"] for event in events], ["new"])
        self.assertIn("库存 未知", render_messages(events, {"a": product(stock=None)}, "20:03")[0])

    def test_equivalent_decimal_format_does_not_alert(self):
        self.assertEqual(make_events({"a": product(price="50.000")}, {"a": product(price="50.0")}, {"6635"}), [])

    def test_low_stock_crossing_shows_current_price_and_actual_remaining(self):
        old = product(price="55.56", stock=7)
        new = product(price="55", stock=4)
        events = make_events({"a": old}, {"a": new}, {"6635"})
        self.assertEqual([event["kind"] for event in events], ["low_stock"])
        text = render_messages(events, {"a": new}, "09-20 20:03:39")[0]
        self.assertIn("— 仅剩5个以内（1）—", text)
        self.assertIn("¥55", text)
        self.assertIn("⚠ 仅剩 4 个", text)
        self.assertIn("库存 7 → 4", text)
        self.assertIn("购买: https://wzyp.cn/item/a", text)
        self.assertNotIn("价格变动", text)

    def test_known_zero_to_positive_is_relisting(self):
        for stock in (1, 5, 9):
            with self.subTest(stock=stock):
                events = make_events({"a": product(stock=0)}, {"a": product(stock=stock)}, {"6635"})
                self.assertEqual([event["kind"] for event in events], ["new"])

    def test_missing_inactive_and_reclassified_nontarget_are_silent(self):
        old = {"a": product()}
        for current in ({}, {"a": product(active=False)}):
            self.assertEqual(make_events(old, current, {"6635"}), [])
        self.assertEqual(make_events(old, {"a": product(target=False)}, {"6635"}), [])
        self.assertEqual(make_events({"a": product(active=False)}, {}, {"6635"}), [])
        self.assertEqual(make_events({"a": product(target=False)}, {}, {"6635"}), [])

    def test_crossing_to_last_five_is_the_only_existing_stock_decrease_alert(self):
        for before, after in ((6, 5), (7, 4), (100, 1)):
            with self.subTest(before=before, after=after):
                events = make_events({"a": product(stock=before)}, {"a": product(stock=after)}, {"6635"})
                self.assertEqual([event["kind"] for event in events], ["low_stock"])
        for before, after in ((7, 6), (5, 4), (4, 3), (3, 1), (6, 0), (None, 5),
                              (5, None), (0, None), (1, 7), (5, 5)):
            with self.subTest(before=before, after=after):
                self.assertEqual(make_events({"a": product(stock=before)}, {"a": product(stock=after)}, {"6635"}), [])

    def test_each_low_stock_threshold_crossing_notifies_once(self):
        previous = {"a": product(stock=10)}
        kinds = []
        for stock in (5, 4, 3, 8, 5, 4, 0):
            current = {"a": product(stock=stock)}
            kinds.extend(event["kind"] for event in make_events(previous, current, {"6635"}))
            previous = current
        self.assertEqual(kinds, ["low_stock", "low_stock"])

    def test_price_changes_and_continuously_in_stock_replenishment_are_silent(self):
        for new in (product(price="30"), product(price="80"), product(stock=10),
                    product(stock=20, price="25"), product(stock=6, price="20")):
            with self.subTest(new=new):
                self.assertEqual(make_events({"a": product()}, {"a": new}, {"6635"}), [])

    def test_failed_shop_snapshot_retention_produces_no_false_events(self):
        retained = {"a": product()}
        self.assertEqual(make_events(retained, copy.deepcopy(retained), {"6635"}), [])

    def test_reactivated_target_is_new(self):
        events = make_events({"a": product(active=False)}, {"a": product()}, {"6635"})
        self.assertEqual([event["kind"] for event in events], ["new"])

    def test_ai_reclassification_of_existing_id_is_new(self):
        for stock in (5, None):
            with self.subTest(stock=stock):
                events = make_events({"a": product(target=False, stock=stock)}, {"a": product(target=True, stock=stock)}, {"6635"})
                self.assertEqual([event["kind"] for event in events], ["new"])
        self.assertEqual(make_events({"a": product(target=False, stock=0)}, {"a": product(target=True, stock=0)}, {"6635"}), [])

    def test_no_price_ceiling_is_applied(self):
        events = make_events({}, {"a": product(price="188.88")}, set())
        self.assertEqual([event["kind"] for event in events], ["new"])

    def test_comparison_does_not_mutate_snapshots(self):
        old, new = {"a": product()}, {"a": product(stock=4)}
        saved = copy.deepcopy((old, new))
        make_events(old, new, {"6635"})
        self.assertEqual((old, new), saved)


class RenderingTests(unittest.TestCase):
    def test_minimum_excludes_unavailable_unknown_inactive_and_unrelated(self):
        items = [
            product("a", price="50", stock=3),
            product("b", price="49.50", shop_id="renwin", shop_name="RenWin", stock=2),
            product("sold", price="1", stock=0), product("unknown", price="2", stock=None),
            product("inactive", price="3", active=False), product("plus", price="4", target=False),
        ]
        current = {item["id"]: item for item in items}
        text = render_messages(make_events({}, current, set()), current, "09-20 20:03:39")[0]
        self.assertIn("当前有货最低价: ¥49.5 | RenWin", text)
        self.assertIn("最低价购买: https://wzyp.cn/item/b", text)
        self.assertIn("【最低价】【RenWin】", text)
        self.assertNotIn("【最低价】【商家6635】", text)
        self.assertIn("— 新上架（3）—", text)
        self.assertTrue(text.endswith("— 09-20 20:03:39"))

    def test_ties_all_receive_marker(self):
        current = {"a": product(), "b": product("b", shop_name="咕咕嘎嘎", price="50.00")}
        text = render_messages(make_events({}, current, set()), current, "now")[0]
        self.assertIn("（2 件同价）", text)
        self.assertEqual(text.count("【最低价】"), 2)

    def test_unknown_inventory_and_bad_prices_never_become_free_offers(self):
        current = {"a": product(stock=None), "b": product("b", price="NaN")}
        text = render_messages(make_events({}, current, {"6635"}), current, "now")[0]
        self.assertIn("当前有货最低价: 暂无", text)
        self.assertIn("库存 未知", text)
        self.assertIn("价格未知", text)
        self.assertNotIn("¥0", text)

    def test_removed_item_cannot_be_current_minimum(self):
        old = {"a": product(price="1")}
        current = {"b": product("b", price="50")}
        historical_events = [{"kind": "removed", "item": old["a"], "old": old["a"]},
                             {"kind": "new", "item": current["b"], "old": None}]
        text = render_messages(historical_events, current, "now")[0]
        self.assertIn("当前有货最低价: ¥50", text)
        self.assertIn("下架前 ¥1 | 已下架", text)
        self.assertEqual(text.count("【最低价】"), 1)

    def test_historical_price_stock_events_remain_renderable(self):
        old, new = product(price="55.56", stock=7), product(price="55", stock=4)
        events = [{"kind": kind, "item": new, "old": old} for kind in ("price", "stock")]
        text = render_messages(events, {"a": new}, "now")[0]
        self.assertIn("¥55.56 → ¥55", text)
        self.assertIn("库存 7 → 4", text)

    def test_untrusted_fields_cannot_emit_cq_commands_or_fake_sections(self):
        item = product(title="team5x速刷 [CQ:at,qq=all]\n伪造标题", shop_name="店\r\n[CQ:at,qq=all]", url="https://wzyp.cn/item/a?[CQ:at,qq=all]")
        current = {"a": item}
        text = render_messages(make_events({}, current, set()), current, "now\n[CQ:at,qq=all]")[0]
        self.assertNotIn("[CQ:", text)
        self.assertNotIn("\n伪造标题", text)
        self.assertIn("%5BCQ:at,qq=all%5D", text)

    def test_invalid_purchase_url_is_not_rendered_as_link(self):
        item = product(url="javascript:alert(1)")
        text = render_messages([{"kind": "new", "item": item, "old": None}], {"a": item}, "now")[0]
        self.assertNotIn("javascript:", text)
        self.assertIn("购买: 暂无可用链接", text)

    def test_many_long_listings_split_without_dropping_purchase_links(self):
        current = {str(i): product(str(i), title="team5x速刷" + "很长的商品说明" * 100, price=str(i + 1)) for i in range(30)}
        events = make_events({}, current, set())
        messages = render_messages(events, current, "09-20 20:03:39", max_chars=512)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 512 for message in messages))
        self.assertTrue(all(message.startswith("【链动小铺·监控】") for message in messages))
        self.assertTrue(all(message.endswith("— 09-20 20:03:39") for message in messages))
        for i in range(30):
            self.assertTrue(any(f"购买: https://wzyp.cn/item/{i}\n" in message for message in messages))

    def test_no_changes_means_no_message(self):
        self.assertEqual(render_messages([], {"a": product()}, "now"), [])

    def test_tiny_character_limits_are_rejected(self):
        with self.assertRaises(ValueError):
            render_messages([], {}, "now", max_chars=50)


if __name__ == "__main__":
    unittest.main()

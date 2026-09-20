import copy
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from priority_alert import HEADER, qualifies, render_priority, select_priority


def product(item_id="a", **changes):
    result = dict(id=item_id, shop_id="6635", shop_name="商家6635", title="team5x 3h速刷 2FA",
                  description="", price="39.99", stock=5, active=True, target=True,
                  url="https://wzyp.cn/item/" + item_id)
    result.update(changes)
    return result


class QualificationTests(unittest.TestCase):
    def test_strict_finite_nonnegative_price_boundary(self):
        for price in ("39.999", "39.99", "0", "0.00", "3.9e1"):
            with self.subTest(price=price):
                self.assertTrue(qualifies(product(price=price)))
        for price in ("40", "40.00", "40.01", "-1", "NaN", "Infinity", "-Infinity", "", None):
            with self.subTest(price=price):
                self.assertFalse(qualifies(product(price=price)))

    def test_known_stock_and_target_gates(self):
        for changes in (dict(stock=0), dict(stock=None), dict(stock=-1), dict(stock=True), dict(stock="5"),
                        dict(active=False), dict(target=False), dict(classification_pending=True),
                        dict(title="Plus 2FA", target=False)):
            with self.subTest(changes=changes):
                self.assertFalse(qualifies(product(**changes)))
        self.assertTrue(qualifies(product(classification_pending=False)))

    def test_title_2fa_aliases_are_explicit(self):
        for token in ("2FA", "2fa", "２ＦＡ", "2-factor", "2-factor authentication", "双重验证", "两步验证"):
            with self.subTest(token=token):
                self.assertTrue(qualifies(product(title="team5x 速刷 " + token)))
        self.assertTrue(qualifies(product(title="team5x 3h速刷 账密2FA")))
        for token in ("x2fancy", "2fast", "v2fa"):
            with self.subTest(token=token):
                self.assertFalse(qualifies(product(title="team5x 速刷 " + token)))

    def test_negation_in_either_field_overrides_positive_title(self):
        for refusal in ("无2FA", "不含2FA", "不提供2FA", "不发2FA", "不发送2FA", "没有双重验证",
                        "不带两步验证", "2FA不提供", "2FA密钥不发", "不提供相关2FA", "未提供2FA",
                        "不提供密码和2FA", "不提供验证码或2FA"):
            with self.subTest(refusal=refusal):
                self.assertFalse(qualifies(product(title="team5x 速刷 " + refusal)))
                self.assertFalse(qualifies(product(description=refusal)))

    def test_description_requires_delivery_context(self):
        for description in ("含2FA", "附带2FA密钥", "提供两步验证", "发货内容：账号+密码+2FA",
                            "格式：邮箱----密码----2FA", "账密+2FA", "账号、密码、2FA",
                            "2FA随号发货", "2FA格式：账号密码密钥", "<p>发货包含&nbsp;双重验证密钥</p>"):
            with self.subTest(description=description):
                self.assertTrue(qualifies(product(title="team5x 速刷", description=description)))
        for description in ("2FA", "登录需2FA", "需要提供2FA", "自行绑定2FA", "支持2FA绑定", "使用2FA登录",
                            "提供教程，需2FA", "提供2FA教程", "含2FA使用教程", "提供开启2FA的方法",
                            "2FA需自行绑定", "如需2FA可自行设置", "无需2FA即可登录", "提供2FA支持"):
            with self.subTest(description=description):
                self.assertFalse(qualifies(product(title="team5x 速刷", description=description)))

    def test_login_requirement_does_not_erase_separate_delivery_evidence(self):
        self.assertTrue(qualifies(product(title="team5x 速刷", description="登录需2FA；发货包含2FA密钥")))


class SelectionTests(unittest.TestCase):
    def test_initial_and_newly_qualified_offers(self):
        current = {"a": product()}
        self.assertEqual(len(select_priority({}, current)), 1)
        for old in (product(stock=0), product(active=False), product(target=False)):
            with self.subTest(old=old):
                events = select_priority({"a": old}, current)
                self.assertEqual(len(events), 1)
                self.assertEqual(events[0]["kind"], "new")
                self.assertIs(events[0]["old"], old)

    def test_stock_increase_and_further_price_drop_are_silent(self):
        old = {"a": product(stock=5, price="39")}
        for new in (product(stock=6, price="39"), product(stock=5, price="38"), product(stock=2, price="38")):
            with self.subTest(new=new):
                self.assertEqual(select_priority(old, {"a": new}), [])

    def test_last_five_crossing_keeps_event_kind(self):
        events = select_priority({"a": product(stock=9)}, {"a": product(stock=4)})
        self.assertEqual(events[0]["kind"], "low_stock")
        self.assertIn("仅剩5个以内", render_priority(events, "now")[0])

    def test_price_or_2fa_change_alone_does_not_create_alert(self):
        for old in (product(price="40"), product(title="team5x速刷"), product(stock=None)):
            self.assertEqual(select_priority({"a": old}, {"a": product()}), [])

    def test_pending_reclassification_waits_for_confirmation(self):
        old = {"a": product(classification_pending=True)}
        self.assertEqual(select_priority({}, old), [])
        self.assertEqual(select_priority(old, {"a": product(classification_pending=False)}), [])

    def test_ordinary_stock_decrease_unchanged_or_price_rise_does_not_repeat(self):
        old = {"a": product(stock=5, price="38")}
        for new in (product(stock=4, price="38"), product(stock=5, price="38.00"), product(stock=5, price="39"),
                    product(stock=0, price="37"), product(stock=None, price="37"), product(stock=7, price="40")):
            with self.subTest(new=new):
                self.assertEqual(select_priority(old, {"a": new}), [])

    def test_all_qualifying_items_are_returned_without_mutation(self):
        current = {"a": product(), "b": product("b", price="25"), "c": product("c", price="40")}
        saved = copy.deepcopy(current)
        self.assertEqual([event["item"]["id"] for event in select_priority({}, current)], ["a", "b"])
        self.assertEqual(current, saved)


class RenderingTests(unittest.TestCase):
    def test_renders_each_offer_price_stock_and_purchase_link(self):
        items = [product("a", price="39"), product("b", price="25", stock=12), product("c", price="40")]
        messages = render_priority(items, "09-20 23:01:02")
        text = "\n".join(messages)
        self.assertTrue(all(message.startswith(HEADER) for message in messages))
        self.assertIn("本批目标最低价: ¥25", text)
        self.assertNotIn("当前有货最低价:", text)
        self.assertIn("¥39 | 库存 5", text)
        self.assertIn("¥25 | 库存 12", text)
        self.assertIn("购买: https://wzyp.cn/item/a", text)
        self.assertIn("购买: https://wzyp.cn/item/b", text)
        self.assertNotIn("购买: https://wzyp.cn/item/c", text)
        self.assertIn("— 新上架（2）—", text)
        self.assertTrue(messages[-1].endswith("— 09-20 23:01:02"))

    def test_event_and_item_map_inputs_match(self):
        items = {"a": product(), "b": product("b", price="30")}
        self.assertEqual(render_priority(items, "now"), render_priority(select_priority({}, items), "now"))

    def test_no_qualifying_items_produces_no_message(self):
        self.assertEqual(render_priority([], "now"), [])
        self.assertEqual(render_priority([product(price="40")], "now"), [])

    def test_long_batches_are_bounded_and_preserve_all_links(self):
        items = [product(str(i), title="team5x速刷2FA " + "商品说明" * 200, price=str(i + 1)) for i in range(30)]
        # Keep an exact token boundary between the product name and 2FA.
        for item in items:
            item["title"] = item["title"].replace("速刷2FA", "速刷 2FA")
        messages = render_priority(items, "09-20 23:01:02", maxchars=640)
        self.assertGreater(len(messages), 1)
        self.assertTrue(all(len(message) <= 640 for message in messages))
        for i in range(30):
            self.assertTrue(any(f"购买: https://wzyp.cn/item/{i}\n" in message for message in messages))

    def test_renderer_never_inserts_real_at_or_cq_commands(self):
        item = product(title="team5x 速刷 2FA [CQ:at,qq=all]")
        text = "\n".join(render_priority([item], "now"))
        self.assertNotIn("[CQ:", text)
        self.assertNotIn("3294692833", text)
        self.assertNotIn("1920924896", text)


if __name__ == "__main__":
    unittest.main()

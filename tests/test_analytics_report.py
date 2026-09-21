import copy
import csv
from pathlib import Path
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from analytics_report import export_day


def sample_report():
    return {
        "date": "2026-09-21", "timezone": "Asia/Shanghai", "generated_at": "2026-09-21T10:12:00+08:00",
        "first_observed": "2026-09-21T10:00:00+08:00", "last_observed": "2026-09-21T10:10:00+08:00", "partial": True,
        "shops": [{"shop_id": "store", "shop_name": "商家6635", "attempts": 3, "successes": 3, "available_samples": 3,
                   "min_price": 39.99, "max_price": 55.56, "first_price": 55.56, "last_price": 39.99, "latest_stock_targets": 5,
                   "first_observed": "2026-09-21T10:00:00+08:00", "last_observed": "2026-09-21T10:10:00+08:00"}],
        "products": [{"shop_id": "store", "shop_name": "商家6635", "item_id": "a", "edition": "edition1", "title": "team5x 速刷 3h",
                      "url": "https://wzyp.cn/item/a", "first_observed": "2026-09-21T10:00:00+08:00", "last_observed": "2026-09-21T10:10:00+08:00",
                      "first_price": 55.56, "last_price": 39.99, "min_price": 39.99, "max_price": 55.56, "price_changes": 1,
                      "first_stock": 9, "last_stock": 5, "observations": 3}],
        "series": [{"at": "2026-09-21T10:00:00+08:00", "shop_id": "store", "shop_name": "商家6635", "success": True,
                    "min_price": 55.56, "target_count": 1, "pending_count": 0}],
        "observations": [{"at": "2026-09-21T10:00:00+08:00", "shop_id": "store", "shop_name": "商家6635", "item_id": "a", "edition": "edition1",
                          "title": "team5x 速刷 3h", "url": "https://wzyp.cn/item/a", "price": 55.56, "stock": 9,
                          "active": True, "target": True, "classification_pending": False}],
        "notes": ["mdkj 仅覆盖第三方上架区。"], "analysis": ["商家6635 观测到最低价 ¥39.99。"],
    }


def elements(path, tag):
    return ET.parse(path).getroot().findall(".//{http://www.w3.org/2000/svg}" + tag)


class AnalyticsReportTests(unittest.TestCase):
    def export(self, report):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        return export_day(report, temporary.name)

    def test_offline_bundle_yuan_values_and_atomic_replacement(self):
        report = sample_report()
        original = copy.deepcopy(report)
        root = self.export(report)
        self.assertEqual(root.name, "2026-09-21")
        for name in ("index.html", "analysis.md", "report.json", "shops.csv", "products.csv", "observations.csv", "series.csv", "charts/comparison.svg"):
            self.assertTrue((root / name).is_file(), name)
        page = (root / "index.html").read_text(encoding="utf-8")
        self.assertIn("¥55.56", page)
        self.assertNotIn("¥0.56", page)
        self.assertIn("当日部分数据", page)
        self.assertIn("10:00:00", page)
        self.assertIn('<svg xmlns=', page)
        self.assertNotIn("<script", page)
        self.assertNotIn("<link", page)
        self.assertIn('href="https://wzyp.cn/item/a"', page)
        self.assertTrue((root / "shops.csv").read_bytes().startswith(b"\xef\xbb\xbf"))
        with (root / "shops.csv").open(encoding="utf-8-sig", newline="") as input_file:
            row = next(csv.DictReader(input_file))
        self.assertEqual(row["min_price"], "39.99")
        self.assertEqual(report, original)
        report["analysis"] = ["更新后的分析"]
        self.assertEqual(export_day(report, root.parent), root)
        self.assertIn("更新后的分析", (root / "index.html").read_text(encoding="utf-8"))
        self.assertFalse(list(root.rglob("*.tmp")))

    def test_empty_day_includes_all_configured_shops_without_zero_prices(self):
        report = sample_report()
        report.update(first_observed=None, last_observed=None, series=[], observations=[], products=[], analysis=[])
        report["shops"] = [{"shop_id": str(index), "shop_name": f"店铺 {index}", "attempts": 0, "successes": 0,
                            "available_samples": 0, "min_price": None, "max_price": None, "first_price": None,
                            "last_price": None, "latest_stock_targets": None} for index in range(7)]
        root = self.export(report)
        page = (root / "index.html").read_text(encoding="utf-8")
        for index in range(7):
            self.assertIn(f"店铺 {index}", page)
        self.assertEqual(len(list((root / "charts").glob("shop-*.svg"))), 7)
        self.assertIn("暂无在售有库存的有效报价", page)
        self.assertIn("暂无记录", page)
        self.assertNotIn("¥0.00", page)
        self.assertEqual(elements(root / "charts/comparison.svg", "circle"), [])

    def test_refresh_removes_orphaned_renderer_charts(self):
        report = sample_report()
        root = self.export(report)
        chart_dir = root / "charts"
        old = next(chart_dir.glob("product-*.svg"))
        old_name = old.name
        refreshed = copy.deepcopy(report)
        refreshed["products"] = []
        refreshed["observations"] = []
        export_day(refreshed, root.parent)
        self.assertFalse((chart_dir / old_name).exists())

    def test_chart_breaks_failed_null_and_over_sixty_second_gaps(self):
        report = sample_report()
        rows = [
            ("10:00:00", True, 50), ("10:00:20", False, 50), ("10:00:40", True, 49),
            ("10:01:00", True, None), ("10:01:20", True, 48), ("10:03:00", True, 47),
        ]
        report["series"] = [{"at": f"2026-09-21T{at}+08:00", "shop_id": "store", "success": ok, "min_price": price} for at, ok, price in rows]
        root = self.export(report)
        self.assertEqual(elements(root / "charts/comparison.svg", "polyline"), [])
        self.assertEqual(len(elements(root / "charts/comparison.svg", "circle")), 4)
        report["series"].append({"at": "2026-09-21T10:03:20+08:00", "shop_id": "store", "success": True, "min_price": 46})
        root = export_day(report, root.parent)
        self.assertEqual(len(elements(root / "charts/comparison.svg", "polyline")), 1)

    def test_product_heartbeat_connects_only_within_tolerance_and_no_failure(self):
        report = sample_report()
        first = report["observations"][0]
        report["observations"] += [{**first, "at": "2026-09-21T10:05:00+08:00", "price": 52},
                                   {**first, "at": "2026-09-21T10:12:00+08:00", "price": 51}]
        root = self.export(report)
        path = next((root / "charts").glob("product-*.svg"))
        self.assertEqual(len(elements(path, "polyline")), 1)
        self.assertIn("360 秒", path.read_text(encoding="utf-8"))
        report["series"].append({"at": "2026-09-21T10:02:00+08:00", "shop_id": "store", "success": False, "min_price": None})
        export_day(report, root.parent)
        self.assertEqual(elements(path, "polyline"), [])

    def test_product_editions_and_unavailable_quotes_do_not_connect(self):
        report = sample_report()
        first = report["observations"][0]
        report["observations"] += [{**first, "at": "2026-09-21T10:00:10+08:00", "stock": 0},
                                   {**first, "at": "2026-09-21T10:00:20+08:00", "price": 54},
                                   {**first, "at": "2026-09-21T10:00:30+08:00", "edition": "different", "price": 1},
                                   {**first, "at": "2026-09-21T10:00:40+08:00", "stock": None},
                                   {**first, "at": "2026-09-21T10:00:50+08:00", "price": 53}]
        root = self.export(report)
        path = next((root / "charts").glob("product-*.svg"))
        self.assertEqual(elements(path, "polyline"), [])
        self.assertEqual(len(elements(path, "circle")), 3)

    def test_untrusted_strings_cannot_inject_html_links_paths_or_csv_formulas(self):
        report = sample_report()
        dangerous = '=HYPERLINK("http://evil.example","click")'
        script = '</text><script>alert(1)</script><text>'
        report["shops"][0].update(shop_id="../../outside", shop_name=script)
        report["series"][0].update(shop_id="../../outside")
        report["products"][0].update(shop_id="../../outside", shop_name=script, title=dangerous, url="javascript:alert(1)")
        report["observations"][0].update(shop_id="../../outside", title="\t=1+2")
        report["analysis"] = [script, '[click](javascript:alert(1))']
        root = self.export(report)
        page = (root / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("<script", page)
        self.assertIn("&lt;script&gt;", page)
        self.assertNotIn('href="javascript:', page)
        for path in (root / "charts").glob("*.svg"):
            ET.parse(path)
            self.assertNotIn("<script", path.read_text(encoding="utf-8"))
        self.assertFalse((root.parent / "outside").exists())
        with (root / "products.csv").open(encoding="utf-8-sig", newline="") as input_file:
            row = next(csv.DictReader(input_file))
        self.assertEqual(row["title"], "'" + dangerous)
        with (root / "observations.csv").open(encoding="utf-8-sig", newline="") as input_file:
            row = next(csv.DictReader(input_file))
        self.assertEqual(row["title"], "'\t=1+2")
        self.assertIn(r'\[click\]', (root / "analysis.md").read_text(encoding="utf-8"))

    def test_only_allowed_origins_become_purchase_links(self):
        report = sample_report()
        for url in ("https://wzyp.cn.evil.example/item/a", "https://user:password@wzyp.cn/item/a", "https://wzyp.cn:444/item/a", "//wzyp.cn/item/a", "https://wzyp.cn/\nitem/a"):
            with self.subTest(url=url):
                report["products"][0]["url"] = url
                root = self.export(report)
                self.assertNotIn('target="_blank"', (root / "index.html").read_text(encoding="utf-8"))
        report["products"][0]["url"] = "https://mdkj.team/shop/?batch=4&quote=%22"
        root = self.export(report)
        self.assertIn('href="https://mdkj.team/shop/?batch=4&amp;quote=%22"', (root / "index.html").read_text(encoding="utf-8"))

    def test_invalid_day_cannot_escape_output_directory(self):
        report = sample_report()
        with tempfile.TemporaryDirectory() as temporary:
            for day in ("../outside", "2026-02-30", "2026-9-21", "2026-09-21/../x"):
                report["date"] = day
                with self.subTest(day=day), self.assertRaises(ValueError):
                    export_day(report, temporary)
            self.assertFalse(list(Path(temporary).iterdir()))


if __name__ == "__main__":
    unittest.main()

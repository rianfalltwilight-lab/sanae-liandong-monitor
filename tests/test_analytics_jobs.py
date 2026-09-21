from datetime import datetime
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from analytics_jobs import CST, AnalyticsJobs, choose_report, deliver_report, onebot, read_json, export_job, write_json
from shop_monitor import Monitor


def epoch(value):
    return datetime.fromisoformat(value).replace(tzinfo=CST).timestamp()


class ScheduleTests(unittest.TestCase):
    def test_cst_cutoff_and_restart_receipt(self):
        dates = ["2026-09-20", "2026-09-21"]
        receipt = {"2026-09-21": {"completed_epoch": epoch("2026-09-21T00:00:00")}}
        self.assertIsNone(choose_report(epoch("2026-09-21T00:04:59"), dates, receipt, {}))
        self.assertEqual(choose_report(epoch("2026-09-21T00:05:00"), dates, receipt, {}), ("2026-09-20", True))
        receipt["2026-09-20"] = {"daily_complete": True}
        self.assertIsNone(choose_report(epoch("2026-09-21T00:05:20"), dates, receipt, {}))
        self.assertEqual(choose_report(epoch("2026-09-21T01:00:01"), dates, receipt, {}), ("2026-09-21", False))

    def test_backoff_and_catchup_bound(self):
        now = epoch("2026-09-21T12:00:00")
        self.assertIsNone(choose_report(now, ["2026-09-01"], {}, {}))
        self.assertIsNone(choose_report(now, ["2026-09-20"], {"2026-09-20": {"retry_after": now + 60}}, {}))

    def test_active_export_does_not_block_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = AnalyticsJobs({}, temp)
            jobs.process = Mock()
            jobs.process.poll.return_value = None
            jobs.started = epoch("2026-09-21T12:00:00")
            jobs.tick(jobs.started + 10)
            jobs.process.wait.assert_not_called()

    def test_scheduler_failure_does_not_stop_recording(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = AnalyticsJobs({}, temp)
            jobs.store = Mock()
            jobs.tick = Mock(side_effect=OSError())
            jobs.capture({}, [], [], [], {}, 100)
            jobs.store.record_poll.assert_called_once()

    def test_completed_daily_receipt_prevents_restart_duplicate(self):
        with tempfile.TemporaryDirectory() as temp:
            receipt = Path(temp) / "2026-09-20.json"
            write_json(receipt, {"daily_complete": True, "directory": temp})
            with patch("analytics_jobs._export_job") as worker:
                self.assertEqual(export_job({}, "unused", temp, "2026-09-20", receipt, daily=True), Path(temp))
                worker.assert_not_called()

    def test_worker_lock_conflict_skips_export(self):
        with tempfile.TemporaryDirectory() as temp:
            with patch("shop_monitor.instance_lock", side_effect=OSError()), patch("analytics_jobs._export_job") as worker:
                self.assertIsNone(export_job({}, "unused", temp, "2026-09-20", Path(temp) / "receipt.json", daily=True))
                worker.assert_not_called()

    def test_corrupt_receipt_blocks_only_its_own_date(self):
        with tempfile.TemporaryDirectory() as temp:
            jobs = AnalyticsJobs({}, temp)
            jobs.receipts.mkdir()
            (jobs.receipts / "2026-09-19.json").write_text("broken", encoding="utf-8")
            jobs.store = Mock()
            jobs.store.recorded_dates.return_value = ["2026-09-19", "2026-09-20"]
            with patch("analytics_jobs.subprocess.Popen") as popen:
                jobs.tick(epoch("2026-09-21T12:00:00"))
                self.assertIn("2026-09-20", popen.call_args.args[0])
            self.assertEqual(jobs.bad_receipts, {"2026-09-19"})


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        (self.directory / "daily.xlsx").write_bytes(b"test fixture")
        (self.directory / "chart.png").write_bytes(b"test fixture")
        self.receipt = self.directory / "receipt.json"
        self.config = {"onebot_url": "http://127.0.0.1:3002", "group_id": 1092470719,
                       "private_forward_qq": "2731538103", "analytics": {"send_daily": True}}
        self.report = {"date": "2026-09-20", "partial": True, "shops": [], "analysis": ["测试摘要"]}

    def test_failed_private_file_retries_only_missing_step(self):
        calls = []
        def caller(config, action, payload):
            calls.append((action, payload))
            if action == "upload_private_file":
                raise OSError()
            return {"message_id": len(calls)}
        with self.assertRaises(RuntimeError):
            deliver_report(self.config, self.report, self.directory, self.receipt, caller=caller)
        self.assertEqual(len(read_json(self.receipt)["sent"]), 5)
        self.assertEqual(calls[0][1]["message"][0]["type"], "text")
        self.assertIsInstance(calls[3][1]["message"], str)
        retry_calls = []
        deliver_report(self.config, self.report, self.directory, self.receipt,
                       caller=lambda c, a, p: retry_calls.append(a) or {"message_id": 99})
        self.assertEqual(retry_calls, ["upload_private_file"])
        self.assertEqual(len(read_json(self.receipt)["sent"]), 6)

    def test_disabled_and_wrong_recipient(self):
        caller = Mock()
        self.config["analytics"]["send_daily"] = False
        deliver_report(self.config, self.report, self.directory, self.receipt, caller=caller)
        caller.assert_not_called()
        self.config["analytics"]["send_daily"] = True
        self.config["private_forward_qq"] = "1"
        with self.assertRaises(ValueError):
            deliver_report(self.config, self.report, self.directory, self.receipt, caller=caller)
        caller.assert_not_called()

    def test_attachment_missing_sends_nothing(self):
        (self.directory / "chart.png").unlink()
        caller = Mock()
        with self.assertRaises(FileNotFoundError):
            deliver_report(self.config, self.report, self.directory, self.receipt, caller=caller)
        caller.assert_not_called()

    def test_receipt_write_failure_stops_further_remote_actions(self):
        caller = Mock(return_value={"message_id": 1})
        with patch("analytics_jobs.write_json", side_effect=OSError()), self.assertRaises(OSError):
            deliver_report(self.config, self.report, self.directory, self.receipt, caller=caller)
        self.assertEqual(caller.call_count, 1)

    def test_daily_needs_fresh_exporter_even_if_old_attachments_exist(self):
        from analytics_jobs import _export_job
        with patch("analytics_report.export_day", return_value=self.directory), patch("analytics_store.AnalyticsStore") as store:
            store.return_value.build_day.return_value = self.report
            with self.assertRaisesRegex(ValueError, "fresh_xlsx"):
                _export_job(self.config, "unused", self.directory, self.report["date"], self.receipt, daily=True)
        self.assertNotIn("sent", read_json(self.receipt))

    def test_business_failure_and_missing_message_receipt(self):
        opener = Mock()
        for result in ({"status": "failed", "retcode": 1}, {"status": "ok", "retcode": 0, "data": {}}):
            opener.open.return_value = io.BytesIO(json.dumps(result).encode())
            with self.assertRaises(RuntimeError):
                onebot(self.config, "send_group_msg", {}, opener=opener)


class MonitorIntegrationTests(unittest.TestCase):
    def test_failure_after_alert_and_dry_run_no_recording(self):
        class NoAI:
            def __init__(self, *args): pass
            def classify(self, items): return {}
        with tempfile.TemporaryDirectory() as temp:
            events = []
            config = dict(shops=[{"id": "s"}], poll_seconds=20, max_backoff_seconds=600,
                          analytics={"enabled": True})
            item = dict(id="p", shop_id="s", shop_name="S", title="team5x 3h速刷",
                        price="39", stock=8, active=True, url="https://wzyp.cn/item/p")
            def send(*args, **kwargs):
                events.append("sent")
                return 10
            monitor = Monitor(config, Path(temp) / "monitor.json", sender=send,
                              fetcher=lambda *a: [dict(item)], classifier_type=NoAI)
            monitor.analytics = Mock()
            def capture(*args, **kwargs):
                events.append("capture")
                raise OSError("locked")
            monitor.analytics.capture.side_effect = capture
            monitor.poll()
            self.assertEqual(events, ["sent", "capture"])
            self.assertEqual(monitor.state["analytics_error"], "OSError")
            self.assertEqual(monitor.state["pending"], [])
            dry = Monitor(config, Path(temp) / "dry.json", dry_run=True,
                          fetcher=lambda *a: [dict(item)], classifier_type=NoAI)
            dry.analytics = Mock()
            dry.poll()
            dry.analytics.capture.assert_not_called()


if __name__ == "__main__":
    unittest.main()

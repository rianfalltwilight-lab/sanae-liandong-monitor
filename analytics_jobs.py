"""Isolated daily exports and resumable QQ delivery; no report work in polling."""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request

LOG = logging.getLogger("liandong")
CST = timezone(timedelta(hours=8))


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {} if default is None else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


def choose_report(now, dates, receipts, options):
    """Previous days at 00:05 CST; hourly current-day preview is never sent."""
    local = datetime.fromtimestamp(now, CST)
    today = local.date()
    cutoff = local.replace(hour=int(options.get("report_hour", 0)),
                           minute=int(options.get("report_minute", 5)), second=0, microsecond=0)
    if local >= cutoff:
        earliest = today - timedelta(days=max(1, int(options.get("max_catchup_days", 3))))
        for date in sorted(dates):
            if earliest.isoformat() <= date < today.isoformat():
                receipt = receipts.get(date, {})
                if not receipt.get("daily_complete") and now >= receipt.get("retry_after", 0):
                    return date, True
    date = today.isoformat()
    receipt = receipts.get(date, {})
    if date in dates and now >= receipt.get("retry_after", 0) and (
            now - receipt.get("completed_epoch", 0) >= max(300, int(options.get("current_report_seconds", 3600)))):
        return date, False
    return None


class AnalyticsJobs:
    def __init__(self, config, state_dir, config_path=None):
        from analytics_store import AnalyticsStore
        self.config, self.options = config, config.get("analytics", {})
        self.state_dir = Path(state_dir)
        self.database = self.state_dir / "analytics.sqlite3"
        self.store = AnalyticsStore(self.database)
        self.output = self.state_dir.parent / "reports"
        self.receipts = self.state_dir / "report-jobs"
        self.config_path = Path(config_path or Path(__file__).with_name("config.json"))
        self.process = None
        self.started = 0
        self.running_date = None
        self.bad_receipts = set()

    def capture(self, items, shops, successful, attempted, health, observed_at):
        self.store.record_poll(items, shops, successful, attempted, health, observed_at)
        try:
            self.tick(time.time())
        except Exception as exc:
            # Export errors must not create holes in the market observations.
            LOG.warning("analytics_scheduler_failed type=%s", type(exc).__name__)

    def tick(self, now):
        if self.process is not None:
            result = self.process.poll()
            if result is None and now - self.started <= 600:
                return
            if result is None:
                self.process.kill()
                self.process.wait(timeout=5)
                result = -1
            if result != 0:
                receipt_path = self.receipts / (self.running_date + ".json")
                receipt = read_json(receipt_path)
                receipt.update(error="export_process_failed", retry_after=now + 300)
                write_json(receipt_path, receipt)
            self.process = None
        receipts = {}
        for path in self.receipts.glob("*.json"):
            try:
                receipts[path.stem] = read_json(path)
                if not isinstance(receipts[path.stem], dict):
                    raise ValueError("invalid_report_receipt")
            except (OSError, ValueError):
                # Fail closed for this date: an unreadable receipt is not proof
                # that QQ never received the report. Other dates remain usable.
                receipts[path.stem] = {"retry_after": now + 86400}
                if path.stem not in self.bad_receipts:
                    LOG.error("analytics_receipt_needs_repair date=%s", path.stem)
                    self.bad_receipts.add(path.stem)
        selected = choose_report(now, self.store.recorded_dates(), receipts, self.options)
        if selected is None:
            return
        date, daily = selected
        args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()),
                "--config", str(self.config_path), "--database", str(self.database),
                "--output", str(self.output), "--date", date,
                "--receipt", str(self.receipts / (date + ".json"))]
        if daily:
            args.append("--daily")
        self.state_dir.mkdir(parents=True, exist_ok=True)
        with (self.state_dir / "analytics-export.log").open("ab") as log:
            self.process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.started, self.running_date = now, date


def onebot(config, action, payload, *, opener=None):
    if config.get("onebot_url") != "http://127.0.0.1:3002":
        raise ValueError("unexpected_onebot_endpoint")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(config["onebot_url"] + "/" + action,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    with opener.open(request, timeout=45) as response:
        result = json.load(response)
    if result.get("status") != "ok" or result.get("retcode") not in (0, "0"):
        raise RuntimeError("onebot_report_failure")
    if action.startswith("send_") and (result.get("data") or {}).get("message_id") is None:
        raise RuntimeError("onebot_report_missing_receipt")
    if action.startswith("upload_") and not (result.get("data") or {}).get("file_id"):
        raise RuntimeError("onebot_report_missing_file_receipt")
    return result.get("data") or {"status": "ok"}


def summary_text(report):
    lines = ["【链动小铺·每日数据报告】", report["date"] + " · 速刷 team5x",
             "统计时区：北京时间" + ("｜当日采样不完整" if report.get("partial") else "")]
    for shop in report.get("shops", []):
        low, last = shop.get("min_price"), shop.get("last_price")
        if low is None:
            detail = "没有确认有货的价格样本"
        else:
            detail = f"日内最低 ¥{low:g}｜末次有货最低 ¥{last:g}" if last is not None else f"日内最低 ¥{low:g}"
        lines.append(f"{shop['shop_name']}：{detail}")
    lines.extend(["", "分析："] + [str(line) for line in report.get("analysis", [])[:5]])
    lines.extend(["", "不同套餐时长不等；库存变化不是销量。", "图表和 Excel 随后发送；详细采样时段、缺失情况见文件。"])
    return "\n".join(lines)[:6000]


def deliver_report(config, report, directory, receipt_path, *, caller=onebot):
    options = config.get("analytics", {})
    if not options.get("send_daily", False):
        return
    if int(config.get("group_id", 0)) != 1092470719 or config.get("private_forward_qq") != "2731538103":
        raise ValueError("unauthorized_report_targets")
    directory = Path(directory).resolve()
    workbook, chart = directory / "daily.xlsx", directory / "chart.png"
    if not workbook.is_file() or not chart.is_file():
        raise FileNotFoundError("daily_report_attachments_missing")
    receipt = read_json(receipt_path)
    sent = receipt.setdefault("sent", {})
    text = summary_text(report)
    steps = []
    for kind, recipient in (("group", {"group_id": 1092470719}), ("private", {"user_id": 2731538103})):
        steps.extend([
            (kind + "_summary", "send_" + kind + "_msg", dict(recipient,
                message=text if kind == "private" else [{"type": "text", "data": {"text": text}}], auto_escape=True)),
            (kind + "_chart", "send_" + kind + "_msg", dict(recipient,
                message=[{"type": "image", "data": {"file": chart.as_uri()}}])),
            (kind + "_workbook", "upload_" + kind + "_file", dict(recipient,
                file=str(workbook), name="链动小铺日报-" + report["date"] + ".xlsx")),
        ])
    failures = []
    for key, action, payload in steps:
        if key in sent:
            continue
        try:
            result = caller(config, action, payload)
        except Exception as exc:
            failures.append(type(exc).__name__)
            continue
        sent[key] = {"at": time.time(), "receipt": result}
        # Stop immediately if we cannot persist a successful remote send.
        write_json(receipt_path, receipt)
    if failures:
        raise RuntimeError("report_delivery_incomplete:" + ",".join(failures))


def export_job(config, database, output, date, receipt_path, *, daily=False):
    # A monitor restart may leave its exporter alive; the per-day OS lock
    # protects files and QQ receipts across both old and new worker processes.
    from shop_monitor import instance_lock
    receipt_path = Path(receipt_path)
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        lock = instance_lock(receipt_path.with_suffix(".lock"))
    except OSError:
        return None
    with lock:
        receipt = read_json(receipt_path)
        if daily and receipt.get("daily_complete"):
            return Path(receipt["directory"])
        return _export_job(config, database, output, date, receipt_path, daily=daily)


def _export_job(config, database, output, date, receipt_path, *, daily=False):
    from analytics_store import AnalyticsStore
    from analytics_report import export_day
    receipt = read_json(receipt_path)
    try:
        report = AnalyticsStore(database).build_day(date)
        directory = export_day(report, output)
        xlsx = config.get("analytics", {}).get("xlsx", {})
        if daily and config.get("analytics", {}).get("send_daily") and not xlsx.get("node_executable"):
            raise ValueError("daily_delivery_requires_fresh_xlsx_and_png")
        if xlsx.get("node_executable"):
            environment = dict(os.environ)
            if xlsx.get("artifact_tool_module"):
                environment["ARTIFACT_TOOL_MODULE"] = xlsx["artifact_tool_module"]
            command = [xlsx["node_executable"], str(Path(__file__).with_name("export_daily_xlsx.mjs")),
                "--input", str(directory / "report.json"), "--output", str(directory / "daily.xlsx"),
                "--chart-png", str(directory / "chart.png")]
            subprocess.run(command, env=environment, check=True, timeout=180,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        if daily:
            deliver_report(config, report, directory, receipt_path)
        receipt = read_json(receipt_path, receipt)
        receipt.update(date=date, completed_epoch=time.time(), daily_complete=daily,
                       directory=str(directory), error=None, retry_after=0)
        write_json(receipt_path, receipt)
        return directory
    except Exception as exc:
        receipt = read_json(receipt_path, receipt)
        receipt.update(date=date, error=type(exc).__name__, retry_after=time.time() + 300)
        write_json(receipt_path, receipt)
        raise


def main():
    parser = argparse.ArgumentParser()
    for name in ("config", "database", "output", "date", "receipt"):
        parser.add_argument("--" + name, required=True)
    parser.add_argument("--daily", action="store_true")
    args = parser.parse_args()
    export_job(read_json(args.config), args.database, args.output, args.date, args.receipt, daily=args.daily)


if __name__ == "__main__":
    main()

"""Sanae shop watcher: independent worker, public catalog input and explicit QQ target."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import urllib.request

from monitor_core import rule_classify, make_events, render_messages
from deepseek_classifier import Classifier
from shop_source import fetch_shop
from priority_alert import qualifies, render_priority

LOG = logging.getLogger("liandong")
CST = timezone(timedelta(hours=8))
PRIORITY_MENTIONS = ("3294692833", "1920924896")
NOTIFICATION_POLICY = "listed-or-last-five-v1"


def item_signature(item):
    return [item.get(k) for k in ("price", "stock", "active", "target", "title", "description")]


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def load_state(path):
    if not Path(path).exists():
        return {"version": 1, "items": {}, "initialized_shops": [], "shop_health": {},
                "pending": [], "classifier_cache": {}, "deliveries": []}
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("items"), dict):
        raise ValueError("invalid_state")
    return value


def send_onebot(config, text, opener=None, *, mentions=()):
    # Text segments prevent any shop title from injecting CQ actions/mentions.
    if int(config["group_id"]) != 1092470719:
        raise ValueError("unauthorized_group")
    if config["onebot_url"] != "http://127.0.0.1:3002":
        raise ValueError("unexpected_onebot_endpoint")
    if tuple(mentions) not in ((), PRIORITY_MENTIONS):
        raise ValueError("unauthorized_mentions")
    segments = []
    for qq in mentions:
        segments.extend([{"type": "at", "data": {"qq": qq}}, {"type": "text", "data": {"text": " "}}])
    segments.append({"type": "text", "data": {"text": ("\n" if mentions else "") + text}})
    payload = {"group_id": 1092470719,
               "message": segments, "auto_escape": True}
    request = urllib.request.Request(config["onebot_url"] + "/send_group_msg",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json; charset=utf-8"}, method="POST")
    opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=10) as response:
        result = json.load(response)
    if result.get("status") != "ok" or result.get("retcode") not in (0, "0"):
        raise RuntimeError("onebot_business_failure")
    message_id = (result.get("data") or {}).get("message_id")
    if message_id is None:
        raise RuntimeError("onebot_missing_receipt")
    return message_id


class Monitor:
    def __init__(self, config, state_path, *, sender=send_onebot, fetcher=fetch_shop,
                 classifier_type=Classifier, dry_run=False):
        self.config = config
        self.state_path = Path(state_path)
        self.state = load_state(state_path)
        if self.state.get("notification_policy") != NOTIFICATION_POLICY:
            self.state["suppressed_legacy_pending"] = len(self.state["pending"])
            self.state["pending"] = []
            self.state.pop("priority_seen", None)
            self.state["notification_policy"] = NOTIFICATION_POLICY
        self.sender, self.fetcher = sender, fetcher
        self.dry_run = dry_run
        self.classifier = classifier_type(config, self.state["classifier_cache"])

    def save(self):
        atomic_json(self.state_path, self.state)

    def fresh_items(self, now):
        return {key: item for key, item in self.state["items"].items()
                if not self.state["shop_health"].get(item["shop_id"], {}).get("error")
                and now - item.get("observed_epoch", 0) <= max(60, self.config["poll_seconds"] * 3)}

    def enqueue(self, events, fresh, now, *, priority=False, recovery=False):
        if events:
            stamp = datetime.fromtimestamp(now, CST).strftime("%m-%d %H:%M:%S")
            messages = render_priority(events, stamp) if priority else render_messages(events, fresh, stamp)
            for text in messages:
                if recovery:
                    text = text.replace("— 新上架", "— 新上架·延迟送达")
                self.state["pending"].append({"text": text, "created": now,
                    "id": hashlib.sha256((str(now) + text).encode()).hexdigest()[:20], "attempts": 0,
                    "mentions": list(PRIORITY_MENTIONS) if priority else [],
                    "events": [{"kind": e["kind"], "item_id": e["item"]["id"], "old": e.get("old")} for e in events],
                    "refs": {e["item"]["id"]: item_signature(e["item"]) for e in events}})
            LOG.info("events=%d messages=%d priority=%s", len(events), len(messages), priority)

    def queue_events(self, previous, now):
        fresh = self.fresh_items(now)
        allowed = make_events(previous, self.state["items"], set(self.state["initialized_shops"]))
        priority = [e for e in allowed if e["item"]["id"] in fresh and qualifies(e["item"])]
        priority_ids = {e["item"]["id"] for e in priority}
        events = [e for e in allowed if e["item"]["id"] not in priority_ids]
        self.enqueue(priority, fresh, now, priority=True)
        self.enqueue(events, fresh, now)
        self.state["pending"] = self.state["pending"][-100:]
        self.save()
        return len(events) + len(priority)

    def deliver(self, now):
        pending = self.state["pending"]
        ttl = self.config.get("pending_ttl_seconds", 120)
        refresh = any(now - e["created"] > ttl or any(
            item_signature(self.state["items"].get(key, {})) != sig
            for key, sig in e.get("refs", {}).items()) for e in pending)
        if refresh:
            ids = {key for e in pending for key in e.get("refs", {})}
            fresh = self.fresh_items(now)
            # A failed catalog cannot safely refresh an old purchase alert.
            if any(key not in fresh for key in ids):
                return
            queued = {(event["item_id"], event["kind"]): event
                      for entry in pending for event in entry.get("events", [])}
            events = []
            for (key, kind), event in queued.items():
                item = fresh.get(key, {})
                stock = item.get("stock")
                if not item.get("active") or not item.get("target") or stock == 0:
                    continue
                if kind == "low_stock" and not (type(stock) is int and 1 <= stock <= 5):
                    continue
                if kind in ("new", "low_stock"):
                    events.append({"kind": kind, "item": item, "old": event.get("old")})
            self.state["pending"] = []
            urgent = [e for e in events if qualifies(e["item"])]
            urgent_ids = {e["item"]["id"] for e in urgent}
            self.enqueue(urgent, fresh, now, priority=True, recovery=True)
            self.enqueue([e for e in events if e["item"]["id"] not in urgent_ids], fresh, now, recovery=True)
            self.save()
        while self.state["pending"]:
            entry = self.state["pending"][0]
            if now - entry["created"] > self.config.get("pending_ttl_seconds", 120):
                LOG.warning("expired_notification id=%s", entry["id"])
                self.state["pending"].pop(0)
                self.save()
                continue
            if self.dry_run:
                print(entry["text"], flush=True)
                receipt = "DRY_RUN"
            else:
                try:
                    if entry.get("mentions"):
                        receipt = self.sender(self.config, entry["text"], mentions=tuple(entry["mentions"]))
                    else:
                        receipt = self.sender(self.config, entry["text"])
                except Exception as exc:
                    entry["attempts"] += 1
                    self.state["delivery_error"] = type(exc).__name__
                    LOG.warning("delivery_failed type=%s", type(exc).__name__)
                    self.save()
                    break
            self.state.pop("delivery_error", None)
            self.state["deliveries"].append({"id": entry["id"], "message_id": receipt, "sent_at": now,
                                              "mentions": entry.get("mentions", [])})
            self.state["deliveries"] = self.state["deliveries"][-200:]
            self.state["pending"].pop(0)
            LOG.info("delivered id=%s message_id=%s", entry["id"], receipt)
            self.save()

    def poll(self, *, use_ai=True):
        now = time.time()
        previous = copy.deepcopy(self.state["items"])
        due = [shop for shop in self.config["shops"]
               if self.state["shop_health"].get(shop["id"], {}).get("next_attempt", 0) <= now]
        successful = []
        uncertain = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = {pool.submit(self.fetcher, shop, self.config): shop for shop in due}
            for future in as_completed(futures):
                shop = futures[future]
                sid = shop["id"]
                health = self.state["shop_health"].setdefault(sid, {})
                try:
                    items = future.result()
                    if not isinstance(items, list):
                        raise ValueError("invalid_catalog")
                    incoming = {item["id"]: item for item in items}
                    if len(incoming) != len(items):
                        raise ValueError("duplicate_catalog_id")
                    staged = {}
                    shop_uncertain = []
                    for key, old in list(self.state["items"].items()):
                        if old["shop_id"] == sid and key not in incoming:
                            staged[key] = dict(old, active=False, observed_epoch=now)
                    for key, item in incoming.items():
                        if item["shop_id"] != sid or not isinstance(item["title"], str):
                            raise ValueError("invalid_product_identity")
                        item["observed_epoch"] = now
                        result = rule_classify(item["title"])
                        old = previous.get(key, {})
                        if result is None:
                            item["target"] = bool(old.get("target"))
                            item["classification_pending"] = bool(old.get("classification_pending")) or (
                                old.get("title") != item["title"] or old.get("description") != item.get("description"))
                            shop_uncertain.append(item)
                        else:
                            item["target"] = result
                            item["classification_pending"] = False
                        staged[key] = item
                    self.state["items"].update(staged)
                    uncertain.extend(shop_uncertain)
                    health.update({"last_success": now, "failures": 0, "error": None,
                                   "next_attempt": now + self.config["poll_seconds"], "count": len(items)})
                    successful.append(sid)
                except Exception as exc:
                    failures = health.get("failures", 0) + 1
                    wait = min(self.config["max_backoff_seconds"], self.config["poll_seconds"] * 2 ** min(failures, 6))
                    health.update({"error": type(exc).__name__, "failures": failures,
                                   "next_attempt": now + wait, "last_failure": now})
                    LOG.warning("shop_failed shop=%s type=%s retry=%ss", sid, type(exc).__name__, wait)
        self.state["last_poll"] = now
        count = self.queue_events(previous, now)
        self.state["initialized_shops"] = sorted(set(self.state["initialized_shops"]) | set(successful))
        self.save()
        self.deliver(time.time())
        # Clear targets are dispatched first: model latency cannot delay those alerts.
        if use_ai and uncertain:
            before_ai = copy.deepcopy(self.state["items"])
            uncertain.sort(key=lambda item: (not bool(item.get("stock")), item["id"]))
            results = self.classifier.classify(uncertain)
            for key, target in results.items():
                if key in self.state["items"] and type(target) is bool:
                    self.state["items"][key]["target"] = target
                    self.state["items"][key]["classification_pending"] = False
            count += self.queue_events(before_ai, time.time())
            self.deliver(time.time())
        self.state["last_complete"] = time.time()
        self.save()
        LOG.info("poll shops=%d/%d targets=%d events=%d pending=%d", len(successful), len(due),
                 sum(1 for item in self.state["items"].values() if item.get("target") and item.get("active")),
                 count, len(self.state["pending"]))
        return {"shops_ok": len(successful), "shops_due": len(due), "events": count,
                "pending": len(self.state["pending"]), "dry_run": self.dry_run}


def instance_lock(path):
    # OS releases this lock on crash; no stale PID deletion required.
    stream = open(path, "a+b")
    stream.seek(0)
    if not stream.read(1):
        stream.write(b"0")
        stream.flush()
    stream.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    return stream


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default=str(Path(__file__).with_name("config.json")))
    parser.add_argument("--state")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-ai", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parent
    config = json.loads(Path(args.config).read_text(encoding="utf-8-sig"))
    if not config["shops"] or config["poll_seconds"] < 15:
        raise ValueError("invalid_monitor_config")
    state_path = Path(args.state or root / "state" / ("dry-run.json" if args.dry_run else "monitor.json"))
    state_path.parent.mkdir(parents=True, exist_ok=True)
    log_dir = state_path.parent
    handler = RotatingFileHandler(log_dir / "monitor.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.setLevel(logging.INFO)
    LOG.addHandler(handler)
    logging.getLogger("deepseek_classifier").addHandler(handler)
    try:
        lock = instance_lock(state_path.with_suffix(".lock"))
    except OSError:
        print("monitor already running", flush=True)
        return
    with lock:
        monitor = Monitor(config, state_path, dry_run=args.dry_run)
        atomic_json(state_path.parent / "process.json", {"pid": os.getpid(), "start": time.time(), "source": str(root)})
        while True:
            started = time.monotonic()
            result = monitor.poll(use_ai=not args.no_ai)
            if args.once:
                print(json.dumps(result, ensure_ascii=False), flush=True)
                if result["shops_ok"] != result["shops_due"]:
                    raise SystemExit(2)
                return
            time.sleep(max(1, config["poll_seconds"] - (time.monotonic() - started)))


if __name__ == "__main__":
    main()

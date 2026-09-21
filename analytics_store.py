"""Private, local SQLite observations for the shop monitor's daily reports.

The store accepts catalog results already obtained by the monitor. It never
fetches a catalog, reads a credential, calls a model, or sends a notification.
Prices are integer cents in SQLite; sparse product rows are changes plus a
five-minute heartbeat. Every attempted shop still has its own health tick.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import contextmanager
from datetime import date as Date, datetime, time as DayTime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import math
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlsplit, urlunsplit

CST = timezone(timedelta(hours=8))
HEARTBEAT_SECONDS = 300
COVERAGE_GAP_SECONDS = 600
SQLITE_MAX_INTEGER = 2 ** 63 - 1


def _iso(epoch):
    return datetime.fromtimestamp(epoch, CST).isoformat(timespec="seconds") if epoch is not None else None


def _cents(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        value = Decimal(str(value))
        if not value.is_finite() or value < 0:
            return None
        cents = int((value * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return cents if cents <= SQLITE_MAX_INTEGER else None
    except (InvalidOperation, ValueError, TypeError, OverflowError):
        return None


def _yuan(cents):
    return cents / 100 if cents is not None else None


def _stock(value):
    return value if type(value) is int and 0 <= value <= SQLITE_MAX_INTEGER else None


def _public_url(value):
    """Persist only a public product path, never URL credentials/query tokens."""
    try:
        parts = urlsplit(str(value or ""))
        if parts.scheme not in ("http", "https") or not parts.netloc or parts.username or parts.password:
            return ""
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "", ""))
    except ValueError:
        return ""


def _edition(item):
    # Exact identity is conservative: even a description edit starts a new
    # edition, so a recycled item URL cannot fabricate a same-product trend.
    identity = json.dumps([str(item.get("title", "")), str(item.get("description", ""))],
                          ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]


def _available(row):
    return bool(row["active"] and row["target"] and not row["classification_pending"]
                and row["stock"] is not None and row["stock"] > 0)


class AnalyticsStore:
    def __init__(self, db_path):
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS shops (
                    shop_id TEXT PRIMARY KEY, shop_name TEXT NOT NULL, configured INTEGER NOT NULL DEFAULT 1);
                CREATE TABLE IF NOT EXISTS shop_ticks (
                    at REAL NOT NULL, shop_id TEXT NOT NULL, shop_name TEXT NOT NULL,
                    success INTEGER NOT NULL, target_count INTEGER, pending_count INTEGER,
                    min_price_cents INTEGER, min_item_id TEXT, min_title TEXT, min_url TEXT,
                    PRIMARY KEY (shop_id, at));
                CREATE INDEX IF NOT EXISTS ticks_at ON shop_ticks(at);
                CREATE TABLE IF NOT EXISTS observations (
                    at REAL NOT NULL, shop_id TEXT NOT NULL, shop_name TEXT NOT NULL,
                    item_id TEXT NOT NULL, edition TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
                    price_cents INTEGER, stock INTEGER, active INTEGER NOT NULL,
                    target INTEGER NOT NULL, classification_pending INTEGER NOT NULL,
                    PRIMARY KEY (shop_id, item_id, edition, at));
                CREATE INDEX IF NOT EXISTS observations_at ON observations(at);
                CREATE TABLE IF NOT EXISTS latest_items (
                    shop_id TEXT NOT NULL, item_id TEXT NOT NULL, edition TEXT NOT NULL,
                    snapshot TEXT NOT NULL, recorded_at REAL NOT NULL,
                    PRIMARY KEY (shop_id, item_id));
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
            version = db.execute("SELECT value FROM metadata WHERE key='schema_version'").fetchone()[0]
            if version != "1":
                raise ValueError("unsupported_analytics_schema")

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=0.5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _append(db, snapshot, at):
        db.execute("""INSERT OR REPLACE INTO observations
            (at,shop_id,shop_name,item_id,edition,title,url,price_cents,stock,active,target,classification_pending)
            VALUES (:at,:shop_id,:shop_name,:item_id,:edition,:title,:url,:price_cents,:stock,:active,:target,:classification_pending)""",
            dict(snapshot, at=at))

    def record_poll(self, items, shops, successful, attempted, health, observed_at):
        """Atomically record one completed poll; failed/skipped data never leaks in.

        ``health`` is accepted for the monitor integration contract. Error text,
        retry credentials and arbitrary adapter responses are deliberately not
        persisted. Only membership of ``successful`` marks an attempt successful.
        """
        at = float(observed_at)
        if not math.isfinite(at):
            raise ValueError("invalid_observation_time")
        _iso(at)  # Reject epochs outside the supported datetime range up front.
        roster = {str(shop["id"]): str(shop.get("name") or shop["id"]) for shop in shops}
        attempted = set(map(str, attempted)) & roster.keys()
        successful = set(map(str, successful)) & attempted
        by_shop = defaultdict(dict)
        for key, item in items.items():
            sid = str(item.get("shop_id", ""))
            if sid in successful:
                by_shop[sid][str(item.get("id", key))] = item
        with self._db() as db:
            last = db.execute("SELECT value FROM metadata WHERE key='last_poll'").fetchone()
            if last is not None and at < float(last[0]):
                raise ValueError("out_of_order_analytics_poll")
            db.execute("UPDATE shops SET configured=0")
            for sid, name in roster.items():
                db.execute("INSERT INTO shops VALUES (?,?,1) ON CONFLICT(shop_id) DO UPDATE SET shop_name=excluded.shop_name,configured=1", (sid, name))
            if attempted:
                db.execute("INSERT OR IGNORE INTO metadata VALUES ('recording_started_at', ?)", (str(at),))
                db.execute("INSERT INTO metadata VALUES ('last_poll', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(at),))
            for sid in sorted(attempted):
                name = roster[sid]
                success = sid in successful
                snapshots = {}
                if success:
                    for item_id, item in by_shop[sid].items():
                        snapshots[item_id] = dict(shop_id=sid, shop_name=name, item_id=item_id,
                            edition=_edition(item), title=str(item.get("title", "")), url=_public_url(item.get("url")),
                            price_cents=_cents(item.get("price")), stock=_stock(item.get("stock")),
                            active=int(item.get("active") is True), target=int(item.get("target") is True),
                            classification_pending=int(bool(item.get("classification_pending"))))
                available = [snapshot for snapshot in snapshots.values() if _available(snapshot)]
                priced = [snapshot for snapshot in available if snapshot["price_cents"] is not None]
                minimum = min(priced, key=lambda row: (row["price_cents"], row["item_id"])) if priced else None
                pending = sum(row["active"] and row["classification_pending"] for row in snapshots.values()) if success else None
                db.execute("""INSERT OR REPLACE INTO shop_ticks
                    (at,shop_id,shop_name,success,target_count,pending_count,min_price_cents,min_item_id,min_title,min_url)
                    VALUES (?,?,?,?,?,?,?,?,?,?)""", (at, sid, name, int(success),
                        len(available) if success else None, pending,
                        minimum["price_cents"] if minimum else None, minimum["item_id"] if minimum else None,
                        minimum["title"] if minimum else None, minimum["url"] if minimum else None))
                if not success:
                    continue
                previous = {row["item_id"]: row for row in db.execute("SELECT * FROM latest_items WHERE shop_id=?", (sid,))}
                for item_id in sorted(previous.keys() | snapshots.keys()):
                    old_row = previous.get(item_id)
                    old = json.loads(old_row["snapshot"]) if old_row else None
                    current = snapshots.get(item_id)
                    if old and (current is None or current["edition"] != old["edition"]):
                        if old["active"]:
                            self._append(db, dict(old, active=0, stock=None), at)
                        db.execute("DELETE FROM latest_items WHERE shop_id=? AND item_id=?", (sid, item_id))
                        old_row = old = None
                    # Uncertain new listings are counted on the shop tick, but
                    # become a product history only once their target is known.
                    if current is None or (old is None and not (current["target"] and not current["classification_pending"])):
                        continue
                    payload = json.dumps(current, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                    changed = old is None or current != old
                    heartbeat = bool(current["active"] and (current["target"] or current["classification_pending"]))
                    heartbeat = heartbeat and at - old_row["recorded_at"] >= HEARTBEAT_SECONDS if old_row else False
                    if changed or heartbeat:
                        self._append(db, current, at)
                        db.execute("INSERT INTO latest_items VALUES (?,?,?,?,?) ON CONFLICT(shop_id,item_id) DO UPDATE SET edition=excluded.edition,snapshot=excluded.snapshot,recorded_at=excluded.recorded_at",
                                   (sid, item_id, current["edition"], payload, at))

    def recorded_dates(self):
        """Return local dates with real attempted shop observations only."""
        with self._db() as db:
            return [row[0] for row in db.execute("SELECT DISTINCT date(at,'unixepoch','+8 hours') AS day FROM shop_ticks ORDER BY day")]

    def build_day(self, date):
        day = Date.fromisoformat(date)
        if day.isoformat() != date:
            raise ValueError("date_must_be_yyyy_mm_dd")
        start = datetime.combine(day, DayTime.min, CST).timestamp()
        end = start + 86400
        generated = time.time()
        with self._db() as db:
            # A read transaction gives all parts of a report the same snapshot.
            db.execute("BEGIN")
            ticks = [dict(row) for row in db.execute("SELECT * FROM shop_ticks WHERE at>=? AND at<? ORDER BY at,shop_id", (start, end))]
            observations = [dict(row) for row in db.execute("SELECT * FROM observations WHERE at>=? AND at<? ORDER BY at,shop_id,item_id,edition", (start, end))]
            roster = {row["shop_id"]: row["shop_name"] for row in db.execute("SELECT * FROM shops WHERE configured=1 ORDER BY shop_id")}
            metadata = {row["key"]: row["value"] for row in db.execute("SELECT * FROM metadata")}
        for row in ticks:
            roster.setdefault(row["shop_id"], row["shop_name"])
        by_shop = defaultdict(list)
        for row in ticks:
            by_shop[row["shop_id"]].append(row)
        started = float(metadata["recording_started_at"]) if "recording_started_at" in metadata else None
        partial = generated < end or started is None or started > start or not roster
        shops = []
        incomplete = []
        for sid, name in sorted(roster.items()):
            rows = by_shop[sid]
            good = [row for row in rows if row["success"]]
            prices = [row["min_price_cents"] for row in good if row["min_price_cents"] is not None]
            endpoints = [start] + [row["at"] for row in good] + [min(end, max(start, generated))]
            has_gap = not good or max((b - a for a, b in zip(endpoints, endpoints[1:])), default=0) > COVERAGE_GAP_SECONDS
            if has_gap:
                partial = True
                incomplete.append(name)
            shops.append(dict(shop_id=sid, shop_name=name, attempts=len(rows), successes=len(good),
                available_samples=len(prices), min_price=_yuan(min(prices)) if prices else None,
                max_price=_yuan(max(prices)) if prices else None, first_price=_yuan(prices[0]) if prices else None,
                last_price=_yuan(prices[-1]) if prices else None,
                latest_stock_targets=rows[-1]["target_count"] if rows and rows[-1]["success"] else None,
                first_observed=_iso(rows[0]["at"]) if rows else None, last_observed=_iso(rows[-1]["at"]) if rows else None))
        by_product = defaultdict(list)
        for row in observations:
            by_product[(row["shop_id"], row["item_id"], row["edition"])].append(row)
        products = []
        for (sid, item_id, edition), rows in sorted(by_product.items()):
            prices = [row["price_cents"] for row in rows if _available(row) and row["price_cents"] is not None]
            last = rows[-1]
            products.append(dict(shop_id=sid, shop_name=last["shop_name"], item_id=item_id, edition=edition,
                title=last["title"], url=last["url"], first_observed=_iso(rows[0]["at"]), last_observed=_iso(last["at"]),
                first_price=_yuan(prices[0]) if prices else None, last_price=_yuan(prices[-1]) if prices else None,
                min_price=_yuan(min(prices)) if prices else None, max_price=_yuan(max(prices)) if prices else None,
                price_changes=sum(a != b for a, b in zip(prices, prices[1:])), first_stock=rows[0]["stock"],
                last_stock=last["stock"], observations=len(rows)))
        notes = ["金额均为商品标价（元）；仅确认属于速刷 team5x、在架且已知库存大于 0 的商品参与价格统计。",
                 "店铺价格曲线是每次成功采样的有货最低标价；最低价商品可能变化，不代表同一商品降价。",
                 "商品明细按商品 ID 与标题/描述指纹区分版本；描述只参与指纹，不保存原文。",
                 "商品观测按变化与每 5 分钟心跳记录；采样间的变化无法还原，缺失或失败不按 0 元或售罄处理。",
                 "库存变化不能推断销量；不比较每小时价格，也不假定使用时长、售后或商品质量相同。"]
        if partial:
            notes.append("本表为部分覆盖：当天尚未结束、监控启用较晚或存在采样缺口，不能当作完整全天行情。")
        if started is not None:
            notes.append("历史采集开始于 " + _iso(started) + "；此前没有历史数据，不补造历史。")
        else:
            notes.append("尚未采集到任何店铺观测。")
        if incomplete:
            notes.append("以下店铺无成功采样或存在超过 10 分钟的覆盖缺口：" + "、".join(incomplete) + "。")
        analysis = self._analysis(shops, products)
        return dict(date=date, timezone="Asia/Shanghai", generated_at=_iso(generated),
            first_observed=_iso(ticks[0]["at"]) if ticks else None, last_observed=_iso(ticks[-1]["at"]) if ticks else None,
            recording_started_at=_iso(started), coverage_gap_seconds=COVERAGE_GAP_SECONDS, partial=partial,
            shops=shops, products=products,
            series=[dict(at=_iso(row["at"]), shop_id=row["shop_id"], shop_name=row["shop_name"],
                success=bool(row["success"]), min_price=_yuan(row["min_price_cents"]),
                target_count=row["target_count"], pending_count=row["pending_count"]) for row in ticks],
            observations=[dict(at=_iso(row["at"]), shop_id=row["shop_id"], shop_name=row["shop_name"],
                item_id=row["item_id"], edition=row["edition"], title=row["title"], url=row["url"],
                price=_yuan(row["price_cents"]), stock=row["stock"], active=bool(row["active"]),
                target=bool(row["target"]), classification_pending=bool(row["classification_pending"])) for row in observations],
            notes=notes, analysis=analysis)

    @staticmethod
    def _analysis(shops, products):
        results = []
        priced = [shop for shop in shops if shop["min_price"] is not None]
        if priced:
            minimum = min(shop["min_price"] for shop in priced)
            names = "、".join(shop["shop_name"] for shop in priced if shop["min_price"] == minimum)
            results.append(f"已采样范围内最低有货标价为 ¥{minimum:.2f}，出现在 {names}；仅代表观测到的最低标价。")
            for shop in priced:
                results.append(f"{shop['shop_name']}：有货最低标价曲线范围 ¥{shop['min_price']:.2f}–¥{shop['max_price']:.2f}，"
                    f"首个有效采样 ¥{shop['first_price']:.2f}，最后有效采样 ¥{shop['last_price']:.2f}；"
                    f"采样成功 {shop['successes']}/{shop['attempts']} 次。")
        else:
            results.append("已采样范围内没有同时满足已确认目标、在架、有已知正库存及有效价格的商品，暂不能分析价格走势。")
        moved = [product for product in products if product["price_changes"]]
        for product in sorted(moved, key=lambda row: (-row["price_changes"], row["shop_id"], row["item_id"]))[:10]:
            results.append(f"同一商品版本「{product['title']}」（{product['shop_name']}）观测到 {product['price_changes']} 次标价变化，"
                f"首个有效价格 ¥{product['first_price']:.2f} → 最后有效价格 ¥{product['last_price']:.2f}。")
        failures = sum(shop["attempts"] - shop["successes"] for shop in shops)
        if failures:
            results.append(f"本日记录 {failures} 次店铺采集失败；失败时沿用的旧快照未计入行情，价格曲线保留缺口。")
        if not moved:
            results.append("暂未在同一商品版本的有效观测中记录到标价变化；不同商品间的最低价变化单独看待。")
        return results

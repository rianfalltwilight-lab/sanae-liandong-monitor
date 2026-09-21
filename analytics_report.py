"""Offline daily analytics: HTML, SVG, CSV and factual analysis, without dependencies.

Prices supplied by AnalyticsStore are yuan (not integer cents). This module does
not fetch shops, classify products, send QQ messages, or load credentials.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import csv
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import html
import io
import json
import math
import os
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit


_TZ = timezone(timedelta(hours=8))
_COLORS = ("#2563eb", "#b45309", "#059669", "#9333ea", "#e11d48", "#0891b2", "#475569")
_ALLOWED_HOSTS = frozenset(("wzyp.cn", "catfk.com", "mdkj.team"))
_GAP_SECONDS = 60
_PRODUCT_GAP_SECONDS = 360
_SHOP_FIELDS = (
    "shop_id", "shop_name", "attempts", "successes", "available_samples",
    "min_price", "max_price", "first_price", "last_price", "latest_stock_targets",
    "first_observed", "last_observed",
)
_PRODUCT_FIELDS = (
    "shop_id", "shop_name", "item_id", "edition", "title", "url", "first_observed",
    "last_observed", "first_price", "last_price", "min_price", "max_price",
    "price_changes", "first_stock", "last_stock", "observations",
)
_OBSERVATION_FIELDS = (
    "at", "shop_id", "shop_name", "item_id", "edition", "title", "url", "price",
    "stock", "active", "target", "classification_pending",
)
_SERIES_FIELDS = ("at", "shop_id", "shop_name", "success", "min_price", "target_count", "pending_count")
_METHOD_NOTES = (
    "所有时间均为北京时间（Asia/Shanghai），金额单位为人民币元。数据从启用采集后开始积累，不补造历史。",
    "店铺曲线表示当次采集中已确认速刷 team5x 且在售有库存商品的最低标价。不同时间可能对应不同商品，不是同款价格指数。",
    "同款曲线按店铺、商品编号和商品版本分别绘制。标题或描述变化后视为不同版本；单价不能代替每小时价格或质量比较。",
    "失败、无有效报价和缺货均断线。店铺曲线间隔超过 60 秒断线；商品曲线按 5 分钟心跳放宽至 360 秒。圆点与线段只表示实际观测，不推断缺失区间。",
    "商品明细保存变更记录及定期心跳，比店铺采样稀疏；折线连接有效观测点，不表示两次采样间曾逐步变价。库存减少不等于确认销量或成交量。",
)


def _escape(value: object) -> str:
    text = str(value if value is not None else "")
    # XML 1.0 forbids these characters even when an API JSON string accepts them.
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]", "�", text)
    return html.escape(text, quote=True)


def _number(value: object) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
        return result if result.is_finite() and result >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _money(value: object, symbol: bool = True) -> str:
    number = _number(value)
    return ("¥" if symbol else "") + f"{number:.2f}" if number is not None else "—"


def _openai_status_line(report: dict) -> str:
    status = (report.get("service_status") or {}).get("openai")
    if not status:
        return "⚪ OpenAI 状态未采集｜本时段不推荐买"
    if status.get("ok") is True:
        if status.get("healthy") is False:
            return "🟠 OpenAI 状态已采集但有异常提示｜本时段可以考虑买"
        return "🟢 OpenAI 状态正常｜本时段可以考虑买"
    return "⚪ OpenAI 状态采集失败或异常｜本时段不推荐买"


def _timestamp(value: object) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=_TZ) if parsed.tzinfo is None else parsed.astimezone(_TZ)
    except (ValueError, TypeError):
        return None


def _time(value: object, full: bool = False) -> str:
    parsed = _timestamp(value)
    return parsed.strftime("%Y-%m-%d %H:%M:%S" if full else "%H:%M:%S") if parsed else "暂无记录"


def _safe_url(value: object) -> str | None:
    if not isinstance(value, str) or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or parsed.hostname not in _ALLOWED_HOSTS:
            return None
        if parsed.username or parsed.password or parsed.port not in (None, 80 if parsed.scheme == "http" else 443):
            return None
        return value
    except ValueError:
        return None


def _slug(value: object) -> str:
    raw = str(value)
    prefix = re.sub(r"[^a-zA-Z0-9_-]+", "-", raw).strip("-_")[:28] or "shop"
    return prefix + "-" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]


def _atomic_write(path: Path, content: str, encoding: str = "utf-8") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as output:
            output.write(content)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if not isinstance(value, str):
        return value
    stripped = value.lstrip(" \t\r\n\ufeff")
    if value.startswith(("\t", "\r", "\n")) or stripped.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _csv_text(rows: list[dict], fields: tuple[str, ...]) -> str:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(fields)
    for row in rows:
        writer.writerow([_csv_cell(row.get(field)) for field in fields])
    return output.getvalue()


def _md(value: object) -> str:
    value = str(value if value is not None else "").replace("\r", " ").replace("\n", " ")
    return re.sub(r"([\\`*_{}\[\]<>!#|])", r"\\\1", value)


def _segments(points: list[tuple[datetime, Decimal | None]], gap_seconds: int = _GAP_SECONDS) -> list[list[tuple[datetime, Decimal]]]:
    segments: list[list[tuple[datetime, Decimal]]] = []
    previous: datetime | None = None
    current: list[tuple[datetime, Decimal]] = []
    for at, price in sorted(points, key=lambda point: point[0]):
        if price is None:
            if current:
                segments.append(current)
            current, previous = [], None
            continue
        if previous is not None and (at - previous).total_seconds() > gap_seconds:
            if current:
                segments.append(current)
            current = []
        current.append((at, price))
        previous = at
    if current:
        segments.append(current)
    return segments


def _svg_chart(title: str, day: str, lines: list[dict]) -> str:
    """Each line contains name/color/points. Null points are explicit gaps."""
    width, height = 1000, 360 + 24 * math.ceil(len(lines) / 2)
    left, right, top, bottom = 76, 26, 62, 76 + 24 * math.ceil(len(lines) / 2)
    plot_w, plot_h = width - left - right, height - top - bottom
    all_times = [at for line in lines for at, _ in line["points"]]
    values = [float(price) for line in lines for _, price in line["points"] if price is not None]
    start = min(all_times) if all_times else datetime.fromisoformat(day).replace(tzinfo=_TZ)
    end = max(all_times) if all_times else start + timedelta(days=1)
    if end <= start:
        start, end = start - timedelta(seconds=30), end + timedelta(seconds=30)
    duration = (end - start).total_seconds()
    if values:
        lower, upper = min(values), max(values)
        padding = max((upper - lower) * 0.12, 0.5)
        lower, upper = max(0, lower - padding), upper + padding
    else:
        lower, upper = 0.0, 1.0
    spread = upper - lower or 1

    def xy(at: datetime, price: Decimal) -> tuple[float, float]:
        return left + (at - start).total_seconds() / duration * plot_w, top + (upper - float(price)) / spread * plot_h

    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} {height}" role="img" aria-label="{_escape(title)}">',
        f'<title>{_escape(title)} · {_escape(day)} · 单位：元</title>',
        f'<desc>失败、缺货、未知价格和超过 {max((line.get("gap_seconds", _GAP_SECONDS) for line in lines), default=_GAP_SECONDS)} 秒的采样间隔断线。圆点代表实际观测。时间为北京时间。</desc>',
        '<rect width="100%" height="100%" rx="12" fill="#ffffff"/>',
        '<g font-family="Microsoft YaHei, Noto Sans CJK SC, sans-serif" fill="#334155">',
        f'<text x="{left}" y="28" font-size="17" font-weight="700">{_escape(title[:48] + ("…" if len(title) > 48 else ""))}</text>',
        f'<text x="{left}" y="48" font-size="12" fill="#64748b">{_escape(day)} · 人民币元 · 北京时间</text>',
    ]
    for tick in range(5):
        y = top + plot_h * tick / 4
        label = f"{upper - spread * tick / 4:.2f}" if values else "—"
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{width-right}" y2="{y:.2f}" stroke="#e2e8f0"/>')
        parts.append(f'<text x="{left-10}" y="{y+4:.2f}" text-anchor="end" font-size="12">{label}</text>')
    for tick in range(5):
        x = left + plot_w * tick / 4
        at = start + timedelta(seconds=duration * tick / 4)
        parts.append(f'<text x="{x:.2f}" y="{top+plot_h+23}" text-anchor="middle" font-size="12">{at:%H:%M:%S}</text>')
    if not values:
        parts.append(f'<text x="{left+plot_w/2:.2f}" y="{top+plot_h/2:.2f}" text-anchor="middle" font-size="17" fill="#64748b">暂无在售有库存的有效报价</text>')
    for line in lines:
        color = line["color"]
        for segment in _segments(line["points"], line.get("gap_seconds", _GAP_SECONDS)):
            # Preserve both ends of a flat run; avoid thousands of redundant vertices.
            kept = [segment[0]]
            for index in range(1, len(segment) - 1):
                if segment[index - 1][1] != segment[index][1] or segment[index][1] != segment[index + 1][1]:
                    kept.append(segment[index])
            if len(segment) > 1:
                kept.append(segment[-1])
            if len(kept) > 1:
                vertices = " ".join(f"{x:.2f},{y:.2f}" for x, y in (xy(at, price) for at, price in kept))
                parts.append(f'<polyline class="price-line" points="{vertices}" fill="none" stroke="{color}" stroke-width="2.3" stroke-linejoin="round"/>')
            for at, price in (kept if len(kept) == 1 else [kept[0], kept[-1]]):
                x, y = xy(at, price)
                parts.append(f'<circle class="price-point" cx="{x:.2f}" cy="{y:.2f}" r="3" fill="{color}"><title>{_escape(line["name"])} {at:%H:%M:%S} {_money(price)}</title></circle>')
    legend_y = top + plot_h + 54
    for index, line in enumerate(lines):
        x = left + (index % 2) * (plot_w / 2)
        y = legend_y + (index // 2) * 24
        parts.append(f'<rect x="{x:.2f}" y="{y-10}" width="12" height="4" fill="{line["color"]}"/>')
        parts.append(f'<text x="{x+20:.2f}" y="{y-4}" font-size="12">{_escape(str(line["name"])[:32])}</text>')
    parts.append('</g></svg>')
    return "".join(parts)


def _shop_lines(report: dict) -> list[dict]:
    lines = []
    for index, shop in enumerate(report.get("shops", [])):
        points = []
        for sample in report.get("series", []):
            if str(sample.get("shop_id")) != str(shop.get("shop_id")):
                continue
            at = _timestamp(sample.get("at"))
            if at is not None:
                points.append((at, _number(sample.get("min_price")) if sample.get("success") is True else None))
        lines.append({"name": shop.get("shop_name", shop.get("shop_id", "未知店铺")), "color": _COLORS[index % len(_COLORS)], "points": points})
    return lines


def _product_line(report: dict, product: dict, color: str, observations: list[dict] | None = None, failures: list[dict] | None = None) -> dict:
    points = []
    key = tuple(str(product.get(field, "")) for field in ("shop_id", "item_id", "edition"))
    for observation in observations if observations is not None else report.get("observations", []):
        if tuple(str(observation.get(field, "")) for field in ("shop_id", "item_id", "edition")) != key:
            continue
        at = _timestamp(observation.get("at"))
        stock = _number(observation.get("stock"))
        valid = observation.get("active") is True and observation.get("target") is True and not observation.get("classification_pending") and stock is not None and stock > 0
        if at is not None:
            points.append((at, _number(observation.get("price")) if valid else None))
    # A failure between two change records must also break a short segment.
    first_at = min((at for at, _ in points), default=None)
    last_at = max((at for at, _ in points), default=None)
    for sample in failures if failures is not None else report.get("series", []):
        if str(sample.get("shop_id")) == key[0] and (sample.get("success") is not True or _number(sample.get("min_price")) is None):
            at = _timestamp(sample.get("at"))
            if at is not None and first_at is not None and first_at <= at <= last_at:
                points.append((at, None))
    return {"name": str(product.get("title", "商品")), "color": color, "points": points, "gap_seconds": _PRODUCT_GAP_SECONDS}


_STYLE = """
:root{color-scheme:light;--ink:#14213b;--muted:#64748b;--line:#e2e8f0;--blue:#2563eb}
*{box-sizing:border-box}body{margin:0;background:#f1f5f9;color:var(--ink);font-family:'Microsoft YaHei','PingFang SC',sans-serif;line-height:1.6}
header{background:#14213b;color:#fff;padding:38px max(24px,calc((100vw - 1280px)/2)) 34px}header .eyebrow{font-size:12px;letter-spacing:2px;color:#93c5fd}
h1{margin:6px 0;font-size:30px}header p{margin:8px 0 0;color:#cbd5e1}main{max-width:1328px;margin:auto;padding:24px}
h2{font-size:21px;margin:0 0 16px}h3{font-size:17px;margin:0 0 12px}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:24px;margin-bottom:20px;overflow:hidden}
.metrics{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px;margin-bottom:20px}.metric{padding:20px;background:white;border-radius:14px;border:1px solid var(--line)}
.metric strong{font-size:25px;display:block;margin-top:6px}.label,.muted{color:var(--muted);font-size:13px}.badge{display:inline-block;background:#dbeafe;color:#1e40af;padding:3px 10px;border-radius:20px;font-size:12px;margin-left:8px}
.warning{background:#fff7ed;border-left:4px solid #f59e0b;padding:12px 16px;border-radius:6px;color:#92400e;margin-top:14px;font-size:14px}
table{width:100%;border-collapse:collapse;font-size:13px}th{text-align:left;background:#f8fafc;color:#475569;font-weight:600;white-space:nowrap}th,td{padding:12px;border-bottom:1px solid var(--line);vertical-align:top}td.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.table-wrap{overflow-x:auto}.shop-name{font-weight:600;min-width:115px}.product-title{min-width:260px;max-width:420px;overflow-wrap:anywhere}a{color:#1d4ed8;text-decoration:none}a:hover{text-decoration:underline}
svg{display:block;width:100%;height:auto;min-width:580px}.chart{overflow-x:auto}.shop-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:20px}.shop-grid .card{margin:0;padding:18px}.shop-grid svg{min-width:500px}
details{border-top:1px solid var(--line);padding:14px 0}summary{cursor:pointer;font-weight:600;overflow-wrap:anywhere}details .muted{margin:8px 0}.analysis li,.method li{margin-bottom:8px}.downloads{display:flex;flex-wrap:wrap;gap:10px}.downloads a{padding:7px 12px;background:#eff6ff;border-radius:8px;font-size:13px}
.coverage{margin-bottom:10px}.status-note{display:inline-block;background:#eff6ff;border-left:4px solid #2563eb;padding:8px 12px;border-radius:6px;margin-bottom:16px;font-size:14px}.empty{padding:35px 12px;color:var(--muted);text-align:center}footer{text-align:center;color:var(--muted);font-size:12px;padding:12px 24px 28px}
@media(max-width:900px){.shop-grid{grid-template-columns:1fr}.metrics{grid-template-columns:repeat(2,minmax(0,1fr))}main{padding:16px}.card{padding:18px}h1{font-size:25px}}
@media print{body{background:white}header{padding:20px;color:#14213b;background:white}header p{color:#475569}main{padding:0}.downloads{display:none}.card{break-inside:avoid}.shop-grid{display:block}.shop-grid .card{margin-bottom:16px}svg{min-width:0!important}.table-wrap{overflow:visible}details{break-inside:avoid}}
"""


def _html_report(report: dict, comparison: str, shop_svgs: list[str], product_svgs: list[str], chart_files: list[str]) -> str:
    shops, products = report.get("shops", []), report.get("products", [])
    day = report["date"]
    partial = bool(report.get("partial"))
    prices = [_number(shop.get("min_price")) for shop in shops]
    minimum = min((price for price in prices if price is not None), default=None)
    valid_shops = sum(bool(shop.get("available_samples")) for shop in shops)
    analysis = report.get("analysis") or ["当天尚无足够有效报价，暂不判断价格走势。"]
    result = [
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src data:; base-uri \'none\'; form-action \'none\'">',
        f'<title>链动小铺 · {_escape(day)} 每日价格观察</title><style>{_STYLE}</style></head><body>',
        f'<header><div class="eyebrow">LIANDONG / DAILY OBSERVATORY</div><h1>每日价格观察 <span class="badge">{"当日部分数据" if partial else "已结束日期"}</span></h1><p>{_escape(day)} · 速刷 team5x · 北京时间</p></header><main>',
        '<div class="metrics">',
        f'<div class="metric"><span class="label">纳入监控的店铺</span><strong>{len(shops)} 家</strong></div>',
        f'<div class="metric"><span class="label">有有效报价的店铺</span><strong>{valid_shops} 家</strong></div>',
        f'<div class="metric"><span class="label">日内观测最低价</span><strong>{_money(minimum)}</strong></div>',
        f'<div class="metric"><span class="label">已记录商品版本</span><strong>{len(products)}</strong></div></div>',
        '<section class="card"><h2>今日观察</h2>',
        f'<div class="muted coverage">采集覆盖：{_escape(_time(report.get("first_observed"), True))} → {_escape(_time(report.get("last_observed"), True))}；生成于 {_escape(_time(report.get("generated_at"), True))}</div>',
        f'<div class="status-note">{_escape(_openai_status_line(report))}</div>',
        '<ul class="analysis">' + ''.join(f'<li>{_escape(line)}</li>' for line in analysis) + '</ul>',
    ]
    if partial:
        result.append('<div class="warning">本表为部分时段数据。最低价、范围与变化均仅对应已采集记录，不能代表完整一天。</div>')
    result += [
        '</section><section class="card"><h2>各店铺最低报价对比</h2><p class="muted">每条曲线是该店铺当次在售有库存商品的最低标价；商品构成变化也会改变曲线。</p><div class="chart">',
        comparison, '</div></section><section class="card"><h2>店铺日表单</h2><div class="table-wrap"><table><thead><tr>',
        '<th>店铺</th><th>成功 / 尝试</th><th>有效报价采样</th><th>日内最低</th><th>日内最高最低报价</th><th>首个 / 末个最低报价</th><th>最新有货商品数</th><th>观测覆盖</th></tr></thead><tbody>',
    ]
    for shop in shops:
        stock = shop.get("latest_stock_targets")
        result.append('<tr>' + f'<td class="shop-name">{_escape(shop.get("shop_name", shop.get("shop_id")))}</td>' +
                      f'<td class="num">{_escape(shop.get("successes", 0))} / {_escape(shop.get("attempts", 0))}</td>' +
                      f'<td class="num">{_escape(shop.get("available_samples", 0))}</td>' +
                      f'<td class="num">{_money(shop.get("min_price"))}</td><td class="num">{_money(shop.get("max_price"))}</td>' +
                      f'<td class="num">{_money(shop.get("first_price"))} / {_money(shop.get("last_price"))}</td>' +
                      f'<td class="num">{_escape(stock) if stock is not None else "未知"}</td>' +
                      f'<td>{_escape(_time(shop.get("first_observed")))}<br>{_escape(_time(shop.get("last_observed")))}</td></tr>')
    if not shops:
        result.append('<tr><td colspan="8" class="empty">尚无配置店铺</td></tr>')
    result += ['</tbody></table></div><p class="muted">日内最高最低报价：店铺最低价曲线的最高值，不代表店内最贵商品；有货商品数按商品条目计数，不是库存件数；未知不会作为 0。</p></section>', '<section class="card"><h2>单店走势</h2><div class="shop-grid">']
    for shop, svg, filename in zip(shops, shop_svgs, chart_files):
        result.append(f'<article class="card"><h3>{_escape(shop.get("shop_name", shop.get("shop_id")))}</h3><div class="chart">{svg}</div><a download href="charts/{filename}">下载该店 SVG 图表</a></article>')
    if not shops:
        result.append('<p class="empty">暂无店铺记录</p>')
    result += ['</div></section><section class="card"><h2>同款商品价格与库存</h2><p class="muted">同一个商品编号如果复用上新，按商品版本分开统计。首末价格仅为观测值；展开商品可查看真实采样点。</p><div class="table-wrap"><table><thead><tr><th>店铺 / 商品</th><th>首价 → 末价</th><th>最低 / 最高</th><th>改价次数</th><th>首末库存</th><th>商品来源</th></tr></thead><tbody>']
    for product in products:
        url = _safe_url(product.get("url"))
        link = f'<a href="{_escape(url)}" target="_blank" rel="noopener noreferrer">查看商品 ↗</a>' if url else '无有效来源链接'
        stocks = ' → '.join(_escape(product.get(field)) if product.get(field) is not None else '未知' for field in ('first_stock', 'last_stock'))
        result.append(f'<tr><td class="product-title"><span class="muted">{_escape(product.get("shop_name"))}</span><br>{_escape(product.get("title"))}<br><span class="muted">版本 {_escape(product.get("edition"))}</span></td>' +
                      f'<td class="num">{_money(product.get("first_price"))} → {_money(product.get("last_price"))}</td><td class="num">{_money(product.get("min_price"))} / {_money(product.get("max_price"))}</td>' +
                      f'<td class="num">{_escape(product.get("price_changes", 0))}</td><td class="num">{stocks}</td><td>{link}</td></tr>')
    if not products:
        result.append('<tr><td colspan="6" class="empty">当日尚无已确认的目标商品记录</td></tr>')
    result += ['</tbody></table></div>']
    for product, svg in zip(products, product_svgs):
        result.append(f'<details><summary>{_escape(product.get("shop_name"))} · {_escape(product.get("title"))}</summary><p class="muted">版本 {_escape(product.get("edition"))} · {_escape(_time(product.get("first_observed")))} — {_escape(_time(product.get("last_observed")))}</p><div class="chart">{svg}</div></details>')
    result += ['</section><section class="card"><h2>统计口径与边界</h2><ul class="method">']
    result.extend(f'<li>{_escape(note)}</li>' for note in [*_METHOD_NOTES, *report.get("notes", [])])
    result += ['</ul></section><section class="card"><h2>导出数据与图表</h2><div class="downloads">']
    for filename, label in [('shops.csv', '店铺日表 CSV'), ('products.csv', '商品明细 CSV'), ('observations.csv', '原始观测 CSV'), ('series.csv', '店铺采样 CSV'), ('report.json', '完整 JSON'), ('analysis.md', '文字分析'), ('charts/comparison.svg', '店铺对比 SVG')]:
        result.append(f'<a download href="{filename}">{label}</a>')
    result += ['</div><p class="muted">此 HTML 可离线打开，内嵌全部图表。CSV 为带 BOM 的 UTF-8，可用 Excel 打开；下载链接需与本页所在文件夹一同保留。</p></section></main><footer>链动小铺 · 数据观察 / 只统计已观测标价，不构成成交或质量保证</footer></body></html>']
    return ''.join(result)


def export_day(report: dict, output_root: str | Path) -> Path:
    """Write one day beneath output_root; each output file is atomically replaced."""
    day = str(report.get("date", ""))
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
        raise ValueError("report date must be YYYY-MM-DD")
    date.fromisoformat(day)
    target = Path(output_root).resolve() / day
    target.mkdir(parents=True, exist_ok=True)
    charts_dir = target / 'charts'
    charts_dir.mkdir(parents=True, exist_ok=True)
    # Remove only renderer-owned files so a refreshed report cannot retain
    # orphaned charts for a shop removed from the active roster.
    for old in charts_dir.iterdir():
        if old.is_file() and (old.name == 'comparison.svg' or
                              old.name.startswith('shop-') or old.name.startswith('product-')):
            old.unlink()
    lines = _shop_lines(report)
    comparison = _svg_chart("店铺最低报价对比", day, lines)
    _atomic_write(target / 'charts' / 'comparison.svg', comparison)
    shop_svgs, filenames = [], []
    for shop, line in zip(report.get('shops', []), lines):
        svg = _svg_chart(str(line['name']) + ' · 最低报价', day, [line])
        filename = 'shop-' + _slug(shop.get('shop_id', '')) + '.svg'
        shop_svgs.append(svg)
        filenames.append(filename)
        _atomic_write(target / 'charts' / filename, svg)
    product_svgs = []
    colors = {str(shop.get('shop_id')): line['color'] for shop, line in zip(report.get('shops', []), lines)}
    by_product, failures_by_shop = defaultdict(list), defaultdict(list)
    for observation in report.get('observations', []):
        by_product[tuple(str(observation.get(field, '')) for field in ('shop_id', 'item_id', 'edition'))].append(observation)
    for sample in report.get('series', []):
        if sample.get('success') is not True or _number(sample.get('min_price')) is None:
            failures_by_shop[str(sample.get('shop_id'))].append(sample)
    for product in report.get('products', []):
        key = tuple(str(product.get(field, '')) for field in ('shop_id', 'item_id', 'edition'))
        line = _product_line(report, product, colors.get(key[0], _COLORS[0]), by_product[key], failures_by_shop[key[0]])
        svg = _svg_chart(str(product.get('shop_name', '')) + ' · ' + str(product.get('title', '商品')), day, [line])
        product_svgs.append(svg)
        identity = '\0'.join(str(product.get(field, '')) for field in ('shop_id', 'item_id', 'edition'))
        _atomic_write(target / 'charts' / ('product-' + _slug(identity) + '.svg'), svg)
    for filename, key, fields in (
        ('shops.csv', 'shops', _SHOP_FIELDS), ('products.csv', 'products', _PRODUCT_FIELDS),
        ('observations.csv', 'observations', _OBSERVATION_FIELDS), ('series.csv', 'series', _SERIES_FIELDS),
    ):
        _atomic_write(target / filename, _csv_text(report.get(key, []), fields), 'utf-8-sig')
    analysis = report.get('analysis') or ['当天尚无足够有效报价，暂不判断价格走势。']
    markdown = [f'# 链动小铺每日价格观察 · {day}', '', f'数据范围：{_time(report.get("first_observed"), True)} → {_time(report.get("last_observed"), True)}（北京时间）。', '',
                '部分时段数据；不可作为完整日数据。' if report.get('partial') else '日期已结束；统计仅覆盖实际采集时段。', '', '## 观察分析', '']
    markdown.extend('- ' + _md(line) for line in analysis)
    markdown += ['', '## 统计口径', '']
    markdown.extend('- ' + _md(line) for line in [*_METHOD_NOTES, *report.get('notes', [])])
    _atomic_write(target / 'analysis.md', '\n'.join(markdown) + '\n')
    _atomic_write(target / 'report.json', json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + '\n')
    _atomic_write(target / 'index.html', _html_report(report, comparison, shop_svgs, product_svgs, filenames))
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description='导出链动小铺每日价格表、分析和离线图表')
    parser.add_argument('--database', required=True, help='SQLite 采集数据库路径')
    parser.add_argument('--date', required=True, help='北京时间日期 YYYY-MM-DD')
    parser.add_argument('--output', required=True, help='导出根目录，自动创建日期子目录')
    args = parser.parse_args(argv)
    from analytics_store import AnalyticsStore
    report = AnalyticsStore(args.database).build_day(args.date)
    print(export_day(report, args.output))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

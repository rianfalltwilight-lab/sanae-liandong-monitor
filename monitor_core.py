"""Pure comparison and message formatting for the Liandong shop monitor.

No network, credentials, persistence, or message delivery live in this module.
The caller must retain a failed shop's last snapshot before calling make_events.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import re
import unicodedata
from urllib.parse import quote, urlsplit


_SECTION_NAMES = {
    "new": "新上架",
    "low_stock": "仅剩5个以内",
    "price": "价格变动",
    "stock": "库存变动",
    "removed": "下架",
}
_TEAM_5X = re.compile(r"(?:team[\s_\-/]*5\s*[x×]|5\s*[x×][\s_\-/]*team)", re.I)
_FAST_USE = re.compile(r"速刷|快刷|刷量")
_NEGATED_FAST_USE = re.compile(r"(?:非|不(?:是|用于)?)\s*(?:速刷|快刷|刷量)")
_NON_SHORT_USE = re.compile(r"非\s*轮转|不\s*轮转|用到\s*空间\s*(?:死|死亡)|长期|永久|无限期|月租|年租")
_DURATION = r"(?<![\d.])(?:[0-9]+(?:\.[0-9]+)?)\s*(?:小时|钟头|hours?|hrs?|h)(?![a-z])"
_SERVICE_DURATION = re.compile(
    r"(?:质保|保修|首登)[^，,。;；|【】\[\]()（）\r\n\d]{0,12}" + _DURATION
    + "|" + _DURATION + r"\s*(?:内)?\s*(?:质保|保修|首登)",
    re.I,
)
_SHORT_USE = re.compile(r"短租|短时(?:用|使用|租|体验)|小时租|临时(?:租|用|使用)")
_USAGE_DURATION = re.compile(
    r"(?:轮转|限用|限时使用|使用(?:时长|期限)?|可用|租用|有效(?:期|时长))\s*[:：=为是约共仅总-]?\s*"
    + _DURATION + "|" + _DURATION + r"\s*(?:轮转|临时用|临时使用|使用权|租用|短租)",
    re.I,
)


def rule_classify(title: str) -> bool | None:
    """Return True for explicit fast/short-use team5x, None if uncertain.

    Bare hours, warranty/first-login coverage, and kick/departure times do not
    establish a short usage period. Ambiguous team listings are left to the
    caller's classifier; unrelated Plus accounts are excluded directly.
    """
    value = unicodedata.normalize("NFKC", str(title)).lower()
    explicit_team5x = _TEAM_5X.search(value) is not None
    if explicit_team5x:
        if _FAST_USE.search(_NEGATED_FAST_USE.sub(" ", value)):
            return True
        if _NON_SHORT_USE.search(value) or _NEGATED_FAST_USE.search(value):
            return None
        usage = _SERVICE_DURATION.sub(" ", value)
        return True if _SHORT_USE.search(usage) or _USAGE_DURATION.search(usage) else None
    related = re.search(r"team|5\s*[x×]", value) is not None
    return None if related else False


def _price(item: dict) -> Decimal | None:
    try:
        value = Decimal(str(item.get("price", "")))
        return value if value.is_finite() and value >= 0 else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _stock(item: dict) -> int | None:
    value = item.get("stock")
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _tracked(item: dict | None) -> bool:
    return bool(item and item.get("target") and item.get("active"))


def make_events(
    previous: dict[str, dict],
    current: dict[str, dict],
    initialized_shop_ids: set[str],
) -> list[dict]:
    """Compare snapshots without mutating them.

    Emit only new listings/restocks and crossings from > 5 to 1..5 in stock.
    Initial discovery alerts for positive or unknown stock. Known zero stock
    becoming positive counts as relisting; unknown becoming known does not.
    Price changes, ordinary stock changes, sellouts and removals stay silent.
    The initialized-shop argument is retained for caller compatibility.
    """
    result = []
    for item_id, item in current.items():
        old = previous.get(item_id)
        if not _tracked(item):
            continue
        stock = _stock(item)
        if not _tracked(old):
            if stock is None or stock > 0:
                result.append({"kind": "new", "item": item, "old": old})
            continue
        old_stock = _stock(old)
        if old_stock == 0 and stock is not None and stock > 0:
            result.append({"kind": "new", "item": item, "old": old})
        elif old_stock is not None and old_stock > 5 and stock is not None and 1 <= stock <= 5:
            result.append({"kind": "low_stock", "item": item, "old": old})
    return result


def _safe_text(value: object) -> str:
    # Product text is untrusted. Keep it on one line and neutralize OneBot CQ
    # codes even when a caller sends the result as a plain string message.
    value = "".join(char for char in str(value) if not unicodedata.category(char).startswith("C") or char in "\r\n\t")
    value = " ".join(value.split())
    return re.sub(r"\[\s*cq\s*:", "［CQ:", value, flags=re.I)


def _clip(value: str, budget: int) -> str:
    if budget <= 0:
        return ""
    return value if len(value) <= budget else value[: max(0, budget - 1)] + "…"


def _money(item: dict) -> str:
    value = _price(item)
    if value is None:
        return "价格未知"
    # Avoid allocating a huge string for an untrusted scientific-notation value.
    if abs(value.adjusted()) > 100:
        formatted = str(value)
    else:
        formatted = format(value, "f")
        if "." in formatted:
            formatted = formatted.rstrip("0").rstrip(".")
    return "¥" + formatted if len(formatted) <= 64 else "价格数值过长"


def _stock_text(item: dict) -> str:
    value = _stock(item)
    if value is None:
        return "未知"
    formatted = str(value)
    return formatted if len(formatted) <= 64 else "数值过长"


def _url(value: object) -> str | None:
    raw = str(value)
    if any(char.isspace() or unicodedata.category(char).startswith("C") for char in raw):
        return None
    try:
        parsed = urlsplit(raw)
        if parsed.scheme not in ("https", "http") or not parsed.hostname:
            return None
        # Escaping square brackets also prevents a URL from containing CQ codes.
        return quote(raw, safe="%:/?&=#@+!$,;~*'()-._")
    except ValueError:
        return None


def _purchase_line(item: dict, budget: int, label: str = "购买") -> str:
    url = _url(item.get("url", ""))
    if not url:
        return f"{label}: 暂无可用链接"
    line = f"{label}: {url}"
    return line if len(line) <= budget else f"{label}: 链接过长，未展示"


def _minimum(current: dict[str, dict]) -> tuple[dict | None, set[str]]:
    eligible = [
        item for item in current.values()
        if _tracked(item) and (_stock(item) or 0) > 0 and _price(item) is not None
    ]
    if not eligible:
        return None, set()
    lowest = min(_price(item) for item in eligible)
    tied = [item for item in eligible if _price(item) == lowest]
    tied.sort(key=lambda item: (str(item.get("shop_name", "")), str(item.get("id", ""))))
    return tied[0], {str(item.get("id", "")) for item in tied}


def _minimum_summary(item: dict | None, ids: set[str], budget: int) -> str:
    if item is None:
        return "当前有货最低价: 暂无（未知库存不参与比较）"
    purchase = _purchase_line(item, max(35, budget // 2), "最低价购买")
    price_line = f"当前有货最低价: {_money(item)}"
    if len(ids) > 1:
        price_line += f"（{len(ids)} 件同价）"
    detail_budget = max(0, budget - len(price_line) - len(purchase) - 4)
    detail = _clip(_safe_text(item.get("shop_name", "未知店铺")) + " | " + _safe_text(item.get("title", "")), detail_budget)
    return price_line + (" | " + detail if detail else "") + "\n" + purchase


def _event_block(event: dict, cheapest_ids: set[str], budget: int) -> str:
    item = event["item"]
    old = event.get("old") or {}
    kind = event["kind"]
    marker = "【最低价】" if kind != "removed" and str(item.get("id", "")) in cheapest_ids else ""
    identity = marker + "【" + _clip(_safe_text(item.get("shop_name", "未知店铺")), 60) + "】" + _safe_text(item.get("title", ""))
    if kind == "low_stock":
        metrics = f" | {_money(item)} | ⚠ 仅剩 {_stock_text(item)} 个（库存 {_stock_text(old)} → {_stock_text(item)}）"
    elif kind == "price":
        metrics = f" | {_money(old)} → {_money(item)} | 库存 {_stock_text(item)}"
    elif kind == "stock":
        metrics = f" | {_money(item)} | 库存 {_stock_text(old)} → {_stock_text(item)}"
    elif kind == "removed":
        metrics = f" | 下架前 {_money(old or item)} | 已下架"
    else:
        metrics = f" | {_money(item)} | 库存 {_stock_text(item)}"
    purchase = _purchase_line(item, max(40, budget // 2))
    identity_budget = max(1, budget - len(metrics) - len(purchase) - 1)
    body = _clip(identity, identity_budget) + metrics + "\n" + purchase
    # Only pathological number fields can exhaust the reserved numeric budget.
    return body if len(body) <= budget else _clip(body, budget)


def render_messages(
    events: list[dict],
    current: dict[str, dict],
    observed_at: str,
    max_chars: int = 3000,
) -> list[str]:
    """Render bounded plain-text messages; no events means no notification.

    Prices are ranked only among active target listings with known stock > 0.
    Ties are all marked. Each split message has its own correct section counts,
    minimum-price summary, and observation time. max_chars must be at least 512.
    """
    if max_chars < 512:
        raise ValueError("max_chars must be at least 512")
    if not events:
        return []
    for event in events:
        if event.get("kind") not in _SECTION_NAMES:
            raise ValueError(f"Unsupported event kind: {event.get('kind')!r}")
    minimum, cheapest_ids = _minimum(current)
    prefix = "【链动小铺·监控】\n" + _minimum_summary(minimum, cheapest_ids, max_chars // 3)
    suffix = "\n— " + _clip(_safe_text(observed_at), 64)
    block_budget = max_chars - len(prefix) - len(suffix) - 35

    def format_chunk(chunk: list[tuple[str, str]]) -> str:
        sections = []
        for kind, title in _SECTION_NAMES.items():
            blocks = [block for block_kind, block in chunk if block_kind == kind]
            if blocks:
                sections.append(f"— {title}（{len(blocks)}）—\n" + "\n".join(blocks))
        return prefix + "\n" + "\n".join(sections) + suffix

    result: list[str] = []
    chunk: list[tuple[str, str]] = []
    ordered = sorted(events, key=lambda event: list(_SECTION_NAMES).index(event["kind"]))
    for event in ordered:
        block = (event["kind"], _event_block(event, cheapest_ids, block_budget))
        candidate = chunk + [block]
        if chunk and len(format_chunk(candidate)) > max_chars:
            result.append(format_chunk(chunk))
            chunk = [block]
        else:
            chunk = candidate
    if chunk:
        result.append(format_chunk(chunk))
    return result

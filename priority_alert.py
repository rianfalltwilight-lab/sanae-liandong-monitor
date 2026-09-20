"""Pure selection of stocked Team5x offers below CNY 40 with supplied 2FA.

This module never sends messages or inserts CQ/mention commands. The caller
adds authorized OneBot ``at`` segments separately from rendered text.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
import html
import re
import unicodedata

from monitor_core import make_events, render_messages


LIMIT = Decimal("40")
HEADER = "🚨【低价2FA速刷提醒｜低于¥40】"
_NORMAL_HEADER = "【链动小铺·监控】"
_TOKEN = re.compile(
    r"(?<![a-z0-9])(?:2[\s_-]*fa|2[\s_-]*factor(?:[\s_-]*authentication)?)(?![a-z0-9])"
    r"|双重验证|两步验证",
    re.I,
)
_NEGATIVE_BEFORE = re.compile(
    r"(?:不(?:含|带|附带|发(?:送)?|提供|包含|支持|配送)|无|没有|未(?:含|带|附带|提供|发(?:送)?|包含)|非)"
    r"\s*(?:(?:任何|相关|对应|有效|可用|的|账号|账户|密码|验证码|密钥|码|以及|及|和|与|或)|[\s:：=+,，、/|\-]){0,10}$"
)
_NEGATIVE_AFTER = re.compile(
    r"^\s*(?:密钥|验证码|码|信息)?\s*[:：=\-]?\s*"
    r"(?:不(?:含|带|附带|发(?:送)?|提供|包含|支持|配送)|没有|无|未提供|不给|给不了)"
)
_GENERIC_BEFORE = re.compile(
    r"(?:需(?:要)?|要求|必须|自行|自备|自己|无需|不需|免|开启|启用|绑定|设置|生成|输入|配置)"
    r"\s*(?:(?:用户|买家|自行|自己|绑定|开启|设置|生成|获取|输入|提供|购买|准备|启用|的)|\s){0,5}$"
)
_GENERIC_AFTER = re.compile(
    r"^\s*(?:(?:的|使用|绑定|设置|登录)\s*)?(?:教程|指南|说明|方法|帮助|工具|支持|功能|介绍)"
    r"|^\s*(?:密钥|验证码|码)?\s*(?:需(?:要)?|必须|请)?\s*(?:用户|买家)?\s*(?:自行|自备|自己)"
)
_PROVIDED_BEFORE = re.compile(
    r"(?:含|带|提供|发送|发货|交付|格式|账密|账号.{0,3}密码|帐号.{0,3}密码)"
    r"[^。；;\n!！?？]{0,32}$"
)
_PROVIDED_AFTER = re.compile(
    r"^\s*(?:密钥|验证码|码|信息)?\s*[:：=\-]?\s*"
    r"(?:随号|随单|一并)?(?:提供|发货|交付|附带|包含|格式|随单|随号)"
)


def _text(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", html.unescape(str(value or ""))).casefold()
    return re.sub(r"<[^>]+>", " ", normalized)


def _contexts(value: str):
    for match in _TOKEN.finditer(value):
        yield value[max(0, match.start() - 48):match.start()], value[match.end():match.end() + 48]


def _provided_2fa(item: dict) -> bool:
    title_contexts = list(_contexts(_text(item.get("title"))))
    description_contexts = list(_contexts(_text(item.get("description"))))
    # Explicit refusal in either field overrides a positive-looking title.
    if any(_NEGATIVE_BEFORE.search(before) or _NEGATIVE_AFTER.search(after)
           for before, after in title_contexts + description_contexts):
        return False
    for before, after in title_contexts:
        if not (_GENERIC_BEFORE.search(before) or _GENERIC_AFTER.search(after)):
            return True
    for before, after in description_contexts:
        if _GENERIC_BEFORE.search(before) or _GENERIC_AFTER.search(after):
            continue
        if _PROVIDED_BEFORE.search(before) or _PROVIDED_AFTER.search(after):
            return True
    return False


def _price(item: dict | None) -> Decimal | None:
    try:
        value = Decimal(str((item or {}).get("price", "")))
        return value if value.is_finite() and value >= 0 else None
    except (InvalidOperation, TypeError, ValueError):
        return None


def _stock(item: dict | None) -> int | None:
    value = (item or {}).get("stock")
    return value if type(value) is int and value >= 0 else None


def qualifies(item: dict | None) -> bool:
    """Require an active target, known positive stock, price < 40, and supplied 2FA."""
    if (not isinstance(item, dict) or not item.get("active") or not item.get("target")
            or item.get("classification_pending")):
        return False
    price, stock = _price(item), _stock(item)
    return bool(price is not None and price < LIMIT and stock is not None and stock > 0 and _provided_2fa(item))


def select_priority(previous: dict[str, dict], current: dict[str, dict]) -> list[dict]:
    """Mentions only accompany a new listing/restock or a last-five crossing."""
    return [event for event in make_events(previous, current, set()) if qualifies(event["item"])]


def render_priority(
    items_or_events: list[dict] | dict[str, dict],
    observed_at: str,
    maxchars: int = 3000,
) -> list[str]:
    """Render every qualifying input, with a loud header and bounded messages.

    Inputs may be items, selected events, or an item-ID map. The minimum shown
    is explicitly scoped to this alert batch, not the entire monitored catalog.
    The core renderer needs 512 characters plus this header's small overhead.
    """
    records = items_or_events.values() if isinstance(items_or_events, dict) else items_or_events
    selected = {}
    for record in records:
        item = record.get("item") if isinstance(record.get("item"), dict) else record
        if qualifies(item):
            key = str(item.get("id", len(selected)))
            selected[key] = {"kind": record.get("kind", "new"), "item": item, "old": record.get("old")}
    if not selected:
        return []
    events = sorted(selected.values(), key=lambda event: (_price(event["item"]), str(event["item"].get("id", ""))))
    current = {key: event["item"] for key, event in selected.items()}
    extra = max(0, len(HEADER) - len(_NORMAL_HEADER)) + len("符合提醒条件") - len("新上架")
    messages = render_messages(events, current, observed_at, max_chars=maxchars - extra)
    result = []
    for message in messages:
        message = HEADER + message[len(_NORMAL_HEADER):]
        message = message.replace("当前有货最低价:", "本批目标最低价:")
        result.append(message)
    return result

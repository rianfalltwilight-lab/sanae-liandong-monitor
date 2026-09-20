"""Read-only public shop catalog adapter; no login or purchase operations.

The shop page uses /shopApi/Shop/goodsList with goods_type=card and no
category_id to enumerate all listed cards. stock_count is numeric even when
show_stock_type=0 makes the browser display stock bands instead of a count.
"""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from html.parser import HTMLParser
import json
import re
import urllib.request
from urllib.parse import urlsplit

DEFAULT_BASE_URL = "https://wzyp.cn"
ALLOWED_BASE_URLS = frozenset((DEFAULT_BASE_URL, "https://catfk.com"))
CATALOG_PATH = "/shopApi/Shop/goodsList"
ENDPOINT = DEFAULT_BASE_URL + CATALOG_PATH
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
PAGE_SIZE = 100
MAX_PAGES = 20


class CatalogError(RuntimeError):
    """A shop snapshot is incomplete or does not match the public API schema."""


class _Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag.lower() in ("script", "style"):
            self.ignored += 1
        elif tag.lower() in ("p", "br", "div", "li", "h1", "h2", "h3"):
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag.lower() in ("script", "style") and self.ignored:
            self.ignored -= 1
        else:
            self.parts.append(" ")

    def handle_data(self, data):
        if not self.ignored:
            self.parts.append(data)


def _description(value):
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CatalogError("invalid_description")
    parser = _Text()
    parser.feed(value)
    return " ".join("".join(parser.parts).split())


def _count(value):
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _price(value):
    if value is None or isinstance(value, bool):
        raise CatalogError("missing_or_invalid_price")
    try:
        price = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise CatalogError("invalid_price") from None
    if not price.is_finite() or price < 0:
        raise CatalogError("invalid_price")
    return format(price, "f")


def _base_url(shop):
    base_url = shop.get("base_url", DEFAULT_BASE_URL)
    if not isinstance(base_url, str) or base_url not in ALLOWED_BASE_URLS:
        raise CatalogError("invalid_shop_base_url")
    return base_url


def parse_item(row, shop):
    base_url = _base_url(shop)
    if not isinstance(row, dict) or row.get("goods_type") != "card":
        raise CatalogError("invalid_card_row")
    key = row.get("goods_key")
    if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", key):
        raise CatalogError("invalid_goods_key")
    user = row.get("user")
    if not isinstance(user, dict) or user.get("token") != shop["token"]:
        raise CatalogError("unexpected_shop_identity")
    title = row.get("name")
    if not isinstance(title, str) or not title.strip():
        raise CatalogError("missing_title")
    extend = row.get("extend")
    extend = extend if isinstance(extend, dict) else {}
    mode = _count(extend.get("show_stock_type"))
    stock = _count(extend.get("stock_count")) if mode in (0, 1) else None
    if "status" in row and row["status"] not in (0, 1, "0", "1"):
        raise CatalogError("invalid_status")
    return {
        "id": key, "shop_id": shop["id"], "shop_name": shop["name"],
        "title": title.strip(), "price": _price(row.get("price")), "stock": stock,
        "url": base_url + "/item/" + key,
        "active": row.get("status", 1) in (1, "1"),
        "description": _description(row.get("description")),
        "stock_display": "band" if mode == 0 else "count" if mode == 1 else "unknown",
    }


class _SameSiteRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, base_url=DEFAULT_BASE_URL):
        super().__init__()
        self.base_url = _base_url({"base_url": base_url})
        self.netloc = urlsplit(self.base_url).netloc

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc != self.netloc:
            raise CatalogError("unexpected_catalog_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_shop(shop, config, *, opener=None):
    """Return a complete card catalog or raise; never accept a partial page set.

    Total changes and duplicate IDs can mean pages shifted during a new listing.
    The caller retains the last good snapshot and retries on its next poll.
    """
    base_url = _base_url(shop)
    for field in ("id", "name", "token"):
        if not isinstance(shop.get(field), str) or not shop[field].strip():
            raise CatalogError("invalid_shop_configuration")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", shop["token"]):
        raise CatalogError("invalid_shop_token")
    timeout = float(config.get("request_timeout", 10))
    if not 0 < timeout <= 30:
        raise CatalogError("invalid_request_timeout")
    opener = opener or urllib.request.build_opener(
        urllib.request.ProxyHandler({}), _SameSiteRedirect(base_url))
    result = []
    seen = set()
    total = None
    for current in range(1, MAX_PAGES + 1):
        payload = {"token": shop["token"], "keywords": "", "goods_type": "card",
                   "current": current, "pageSize": PAGE_SIZE}
        request = urllib.request.Request(base_url + CATALOG_PATH,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "Accept": "application/json", "User-Agent": "SanaeShopMonitor/1.0"}, method="POST")
        with opener.open(request, timeout=timeout) as response:
            body = response.read(MAX_RESPONSE_BYTES + 1)
        if len(body) > MAX_RESPONSE_BYTES:
            raise CatalogError("catalog_response_too_large")
        try:
            response_data = json.loads(body.decode("utf-8-sig"), parse_float=Decimal)
        except (ValueError, UnicodeError):
            raise CatalogError("invalid_catalog_json") from None
        if not isinstance(response_data, dict) or response_data.get("code") not in (1, "1"):
            raise CatalogError("catalog_business_failure")
        data = response_data.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("list"), list):
            raise CatalogError("invalid_catalog_schema")
        page_total = _count(data.get("total"))
        if page_total is None or page_total > PAGE_SIZE * MAX_PAGES:
            raise CatalogError("invalid_or_excessive_catalog_total")
        if total is None:
            total = page_total
        elif total != page_total:
            raise CatalogError("catalog_changed_during_pagination")
        rows = data["list"]
        if len(rows) > PAGE_SIZE:
            raise CatalogError("unexpected_page_size")
        for row in rows:
            item = parse_item(row, shop)
            if item["id"] in seen:
                raise CatalogError("duplicate_goods_during_pagination")
            seen.add(item["id"])
            result.append(item)
        if len(result) == total:
            return result
        if not rows or len(result) > total:
            raise CatalogError("incomplete_or_inconsistent_catalog")
    raise CatalogError("catalog_page_limit_reached")

"""Read-only authenticated adapter for the mdkj.team third-party shelf."""
from __future__ import annotations
from decimal import Decimal
from datetime import datetime, timezone, timedelta
import json
import re
import urllib.request
from urllib.parse import urlsplit

ORIGIN = "https://mdkj.team"
API_ORIGIN = ORIGIN + "/shop/api"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_TOKEN_BYTES = 8 * 1024
DEFAULT_TIMEOUT = 12.0
_ID = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
CST = timezone(timedelta(hours=8))

class MdkjError(RuntimeError):
    pass

def _int(value, name, minimum=0):
    if isinstance(value, bool): raise MdkjError("invalid_" + name)
    if isinstance(value, int): result = value
    elif isinstance(value, str) and re.fullmatch(r"[0-9]+", value): result = int(value)
    else: raise MdkjError("invalid_" + name)
    if result < minimum: raise MdkjError("invalid_" + name)
    return result

def _text(value, name, required=False, limit=400):
    if not isinstance(value, str):
        if required: raise MdkjError("missing_" + name)
        return ""
    value = " ".join(value.split())
    if required and not value: raise MdkjError("missing_" + name)
    if len(value) > limit: raise MdkjError("oversize_" + name)
    return value

def _token(shop):
    path = shop.get("token_file")
    if not isinstance(path, str) or not path.strip(): raise MdkjError("missing_token_file")
    try:
        with open(path, "rb") as stream: raw = stream.read(MAX_TOKEN_BYTES + 1)
    except OSError: raise MdkjError("token_file_unreadable") from None
    if len(raw) > MAX_TOKEN_BYTES: raise MdkjError("token_file_too_large")
    try: token = raw.decode("utf-8-sig").strip()
    except UnicodeDecodeError: raise MdkjError("token_file_encoding") from None
    if not token or len(token) > 4096 or any(ord(ch) < 0x20 or ord(ch) == 0x7f for ch in token):
        raise MdkjError("invalid_customer_token")
    return token

class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urlsplit(newurl)
        if target.scheme != "https" or target.netloc != "mdkj.team" or target.username or target.password:
            raise MdkjError("unexpected_mdkj_redirect")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def _opener(opener=None):
    return opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _SameOriginRedirect())

def _get_third_party(token, config, opener):
    try: timeout = float(config.get("request_timeout", DEFAULT_TIMEOUT))
    except (TypeError, ValueError): raise MdkjError("invalid_request_timeout") from None
    if not 0 < timeout <= 30: raise MdkjError("invalid_request_timeout")
    request = urllib.request.Request(API_ORIGIN + "/third-party", headers={
        "Accept":"application/json", "X-Customer-Token":token, "User-Agent":"SanaeShopMonitor/1.0"}, method="GET")
    try:
        with opener.open(request, timeout=timeout) as response: body=response.read(MAX_RESPONSE_BYTES + 1)
    except MdkjError: raise
    except Exception as exc: raise MdkjError("mdkj_request_failed") from exc
    if len(body) > MAX_RESPONSE_BYTES: raise MdkjError("mdkj_response_too_large")
    try: payload=json.loads(body.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError): raise MdkjError("invalid_mdkj_json") from None
    if not isinstance(payload, dict) or payload.get("success") is not True: raise MdkjError("mdkj_business_failure")
    data=payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list): raise MdkjError("invalid_mdkj_schema")
    return data

def _price_cents(row, seconds, rate):
    base=_int(row.get("base_cents"),"base_cents"); duration=_int(row.get("base_seconds"),"base_seconds",1)
    cap=_int(row.get("cap_cents"),"cap_cents"); seconds=_int(seconds,"remaining_seconds")
    amount=base*(seconds//60)*60; capped=min(amount,cap*duration); numerator=capped*rate; denominator=duration*10000
    return max(1,(2*numerator+denominator)//(2*denominator))

def _yuan(cents): return format((Decimal(cents)/Decimal(100)).quantize(Decimal("0.01")),"f")

def parse_item(row, shop, data):
    if not isinstance(row, dict): raise MdkjError("invalid_mdkj_item")
    raw_id=_text(row.get("id"),"batch_id",required=True,limit=160)
    if not _ID.fullmatch(raw_id): raise MdkjError("invalid_mdkj_batch_id")
    quantity=_int(row.get("quantity"),"quantity"); created=_int(row.get("created_at"),"created_at")
    expires=_int(row.get("expires_at"),"expires_at",1); server_time=_int(data.get("server_time"),"server_time")
    rate=_int(data.get("rate",shop.get("buyer_rate",10000)),"buyer_rate")
    title=_text(row.get("title"),"title",required=True); name=_text(row.get("name"),"name",required=True); label=_text(row.get("label"),"label")
    active=bool(data.get("enabled",True)) and quantity>0 and expires>=server_time
    price=_price_cents(row,max(0,expires-server_time),rate); expiry=datetime.fromtimestamp(expires,CST).strftime("%Y-%m-%d %H:%M")
    return {"id":"mdkj:third-party:"+raw_id,"shop_id":shop["id"],"shop_name":shop["name"],
      "title":"team5X速刷 · "+title,"source_title":title,"price":_yuan(price),"stock":quantity,"active":active,"target":True,
      "url":ORIGIN+"/shop/?view=third-party","description":f"麻豆科技 mdkj.team · {label or '第三方货源'} · 到期：{expiry}（北京时间）",
      "source":"third-party","created_at":created,"expires_at":expires}

def fetch_mdkj_shop(shop, config, *, opener=None):
    for field in ("id","name","token_file"):
        if not isinstance(shop.get(field),str) or not shop[field].strip(): raise MdkjError("invalid_shop_configuration")
    if shop.get("base_url",ORIGIN)!=ORIGIN: raise MdkjError("invalid_mdkj_base_url")
    data=_get_third_party(_token(shop),config,_opener(opener))
    return [parse_item(row,shop,data) for row in data["items"]]

fetch_shop=fetch_mdkj_shop

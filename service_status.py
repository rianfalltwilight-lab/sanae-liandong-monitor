"""Small, read-only status-page probe used as a purchase safety signal.

The attached portable executable is not imported or executed.  This module
uses the public Statuspage JSON endpoint, keeps a short in-memory cache, and
fails closed: an unavailable or malformed OpenAI status response produces a
"do not recommend" signal instead of silently treating the service as healthy.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request

OPENAI_STATUS_URL = "https://status.openai.com/api/v2/summary.json"
CLAUDE_STATUS_URL = "https://status.claude.com/api/v2/summary.json"
_ALLOWED_HOSTS = {"status.openai.com", "status.claude.com"}


def _safe_text(value, limit=160):
    text = " ".join(str(value or "").split())
    return text[:limit]


def parse_summary(payload, *, service, observed_at=None):
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), dict):
        raise ValueError("status_summary_missing_status")
    summary = payload["status"]
    indicator = _safe_text(summary.get("indicator"), 32).lower()
    description = _safe_text(summary.get("description"), 160)
    if indicator not in {"none", "minor", "major", "critical", "unknown"}:
        raise ValueError("status_summary_invalid_indicator")
    components = payload.get("components")
    if not isinstance(components, list):
        raise ValueError("status_summary_invalid_components")
    degraded = []
    for component in components:
        if not isinstance(component, dict):
            continue
        status = _safe_text(component.get("status"), 32).lower()
        if status and status != "operational":
            degraded.append(_safe_text(component.get("name"), 80) or "未命名组件")
    # ``ok`` means the status page was collected successfully.  The user's
    # purchase gate is data availability; the page's own incident signal is
    # retained separately so the message can show that context without
    # silently turning a successful collection into an unknown result.
    healthy = indicator == "none" and not degraded
    return {
        "service": service,
        "ok": True,
        "healthy": healthy,
        "indicator": indicator,
        "description": description or ("All Systems Operational" if healthy else "状态未知"),
        "degraded_components": degraded[:12],
        "fetched_at": float(observed_at if observed_at is not None else time.time()),
        "error": None,
    }


def failed_status(service, error, *, observed_at=None):
    return {
        "service": service,
        "ok": False,
        "indicator": "unknown",
        "description": "状态采集失败",
        "degraded_components": [],
        "fetched_at": float(observed_at if observed_at is not None else time.time()),
        "error": str(error)[:64],
    }


class StatusClient:
    def __init__(self, config, *, opener=None):
        self.config = config or {}
        self.options = self.config.get("service_status", {})
        self.opener = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self.cache = {}

    def _request(self, service, url, now):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_HOSTS or parsed.query or parsed.fragment:
            raise ValueError("status_endpoint_not_allowed")
        request = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "Sanae-Liandong-Monitor/1"})
        timeout = min(15, max(3, int(self.options.get("request_timeout", 10))))
        with self.opener.open(request, timeout=timeout) as response:
            body = response.read(2 * 1024 * 1024 + 1)
        if len(body) > 2 * 1024 * 1024:
            raise ValueError("status_response_too_large")
        return parse_summary(json.loads(body.decode("utf-8")), service=service, observed_at=now)

    def poll(self, now=None, *, force=False):
        now = float(now if now is not None else time.time())
        if not self.options.get("enabled", False):
            return {}
        interval = max(30, int(self.options.get("poll_seconds", 60)))
        current = self.cache.get("openai")
        if current and not force and now - current["fetched_at"] < interval:
            return dict(self.cache)
        url = self.options.get("openai_url", OPENAI_STATUS_URL)
        try:
            self.cache["openai"] = self._request("openai", url, now)
        except Exception as exc:
            self.cache["openai"] = failed_status("openai", type(exc).__name__, observed_at=now)
        if self.options.get("include_claude", False):
            url = self.options.get("claude_url", CLAUDE_STATUS_URL)
            try:
                self.cache["claude"] = self._request("claude", url, now)
            except Exception as exc:
                self.cache["claude"] = failed_status("claude", type(exc).__name__, observed_at=now)
        return dict(self.cache)


def recommendation_line(statuses):
    status = (statuses or {}).get("openai")
    if not status:
        return "⚪ OpenAI 状态未采集｜本时段不推荐买"
    if status.get("ok") is True:
        if status.get("healthy") is False:
            return f"🟠 OpenAI 状态已采集（{status.get('description') or '有异常提示'}）｜本时段可以考虑买"
        return "🟢 OpenAI 状态正常｜本时段可以考虑买"
    if status.get("error"):
        return "⚪ OpenAI 状态采集失败｜本时段不推荐买"
    detail = status.get("description") or "服务状态异常"
    components = status.get("degraded_components") or []
    if components:
        detail += "：" + "、".join(components[:3])
    return f"🟠 OpenAI {detail}｜本时段不推荐买"

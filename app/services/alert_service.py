from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
from threading import Lock, Thread
import time
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

from app.config import settings


logger = logging.getLogger(__name__)
_lock = Lock()
_last_sent: dict[str, float] = {}


def _payload(event: str, severity: str, details: dict[str, str | int | float | bool | None]) -> bytes:
    return json.dumps({
        "event": event,
        "severity": severity,
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "environment": settings.environment,
        "details": details,
    }, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _signature(body: bytes) -> str:
    return hmac.new((settings.alert_webhook_secret or "").encode("utf-8"), body, hashlib.sha256).hexdigest()


def _send(body: bytes) -> None:
    url = settings.alert_webhook_url or ""
    if urlsplit(url).scheme not in {"http", "https"}:
        # A misconfigured file:// or custom scheme must never be opened.
        logger.error(
            "operational alert webhook rejected",
            extra={"scheme": urlsplit(url).scheme or "missing"},
        )
        return
    request = Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-AI-Garden-Signature": f"sha256={_signature(body)}",
        },
    )
    try:
        with urlopen(request, timeout=5) as response:
            if response.status >= 300:
                raise RuntimeError(f"alert webhook returned {response.status}")
    except Exception:
        logger.exception("operational alert delivery failed")


def dispatch_operational_alert(
    event: str,
    *,
    severity: str = "error",
    details: dict[str, str | int | float | bool | None] | None = None,
) -> bool:
    if not settings.alert_webhook_url:
        return False
    now = time.monotonic()
    with _lock:
        # ``monotonic`` starts near zero on a freshly booted host, so a missing
        # entry must not be treated as "sent at uptime 0": that would silently
        # drop the first alert of every event for the whole interval after boot.
        previous = _last_sent.get(event)
        if previous is not None and now - previous < settings.alert_min_interval_seconds:
            return False
        _last_sent[event] = now
    body = _payload(event, severity, details or {})
    Thread(target=_send, args=(body,), daemon=True, name="ai-garden-alert").start()
    return True


def reset_alert_rate_limits() -> None:
    with _lock:
        _last_sent.clear()

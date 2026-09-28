import hashlib
import hmac
import json

import app.services.alert_service as alert_service
from app.config import settings


class ImmediateThread:
    """Run the alert delivery synchronously so the payload can be asserted."""

    def __init__(self, *, target, args, **_kwargs):
        self.target = target
        self.args = args

    def start(self):
        self.target(*self.args)


def test_operational_alert_is_signed_rate_limited_and_contains_no_request_payload(monkeypatch):
    secret = "alert-test-secret-that-is-longer-than-thirty-two"
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")
    monkeypatch.setattr(settings, "alert_webhook_secret", secret)
    monkeypatch.setattr(settings, "alert_min_interval_seconds", 300)
    captured: list[bytes] = []

    monkeypatch.setattr(alert_service, "Thread", ImmediateThread)
    monkeypatch.setattr(alert_service, "_send", captured.append)
    assert alert_service.dispatch_operational_alert(
        "api_unhandled_error", details={"request_id": "safe-id", "route": "/api/v1/example"},
    ) is True
    assert alert_service.dispatch_operational_alert("api_unhandled_error") is False
    body = captured[0]
    payload = json.loads(body)
    assert payload["details"] == {"request_id": "safe-id", "route": "/api/v1/example"}
    assert "symptoms" not in payload
    expected = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert alert_service._signature(body) == expected


def test_operational_alert_is_not_suppressed_by_low_monotonic_uptime(monkeypatch):
    # A freshly booted host reports a monotonic clock below the interval, so the
    # first alert of every event used to be dropped until uptime passed it.
    monkeypatch.setattr(settings, "alert_webhook_url", "https://alerts.example.test/hook")
    monkeypatch.setattr(settings, "alert_webhook_secret", "alert-test-secret-that-is-longer-than-thirty-two")
    monkeypatch.setattr(settings, "alert_min_interval_seconds", 300)
    monkeypatch.setattr(alert_service.time, "monotonic", lambda: 5.0)
    monkeypatch.setattr(alert_service, "Thread", ImmediateThread)
    monkeypatch.setattr(alert_service, "_send", lambda body: None)
    assert alert_service.dispatch_operational_alert("early_boot_error") is True

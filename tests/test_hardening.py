"""Regression tests for the hardening fixes from the code review."""

from datetime import datetime, timedelta

import httpx
import pytest
from fastapi import HTTPException

from app.admin_cli import set_admin
from app.config import settings, validate_runtime_settings
from app.database import SessionLocal
from app.models import Partner
from app.services import alert_service
from app.services import notification_service
from app.services import rate_limit_service as rate_limit
from app.services.partner_attribution_service import (
    hash_partner_key,
    legacy_partner_key_hash,
    verify_partner_key,
)
from app.services.weather_service import WeatherService
from tests.test_api import create_plant, register


# --- 1. Redis rate limiting: one pooled client, atomic INCR+EXPIRE -----------


class _FakeScript:
    def __init__(self, client):
        self.client = client

    def __call__(self, keys=None, args=None):
        key = keys[0]
        count = self.client.store.get(key, 0) + 1
        self.client.store[key] = count
        if count == 1:
            self.client.expiries[key] = int(args[0])
        return count


class _FakeRedis:
    def __init__(self):
        self.store = {}
        self.expiries = {}
        self.registered = 0

    def register_script(self, _script):
        self.registered += 1
        return _FakeScript(self)


def test_redis_rate_limit_reuses_client_and_sets_ttl_once(monkeypatch):
    client = _FakeRedis()
    rate_limit.reset_redis_clients()
    monkeypatch.setattr(settings, "diagnosis_execution_mode", "celery")
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: client)

    rate_limit.enforce_rate_limit("login", "user@example.com", limit=2, window=60)
    rate_limit.enforce_rate_limit("login", "user@example.com", limit=2, window=60)
    with pytest.raises(HTTPException) as error:
        rate_limit.enforce_rate_limit("login", "user@example.com", limit=2, window=60)

    assert error.value.status_code == 429
    assert client.registered == 1, "the script and client must be created once"
    assert list(client.store.values()) == [3]
    # INCR and EXPIRE run as one atomic step, so the TTL is set a single time.
    assert list(client.expiries.values()) == [60]
    assert not any("user@example.com" in key for key in client.store)


def test_redis_rate_limit_fails_closed(monkeypatch):
    class _BrokenRedis:
        def register_script(self, _script):
            def run(keys=None, args=None):
                raise RuntimeError("redis unavailable")

            return run

    rate_limit.reset_redis_clients()
    monkeypatch.setattr(settings, "diagnosis_execution_mode", "celery")
    monkeypatch.setattr(rate_limit, "_redis_client", lambda: _BrokenRedis())

    with pytest.raises(HTTPException) as error:
        rate_limit.enforce_rate_limit("login", "user@example.com", limit=5, window=60)
    assert error.value.status_code == 503


# --- 2. Notification deduplication only queries candidate keys ---------------


def test_due_notifications_check_only_candidate_keys(client, monkeypatch):
    plant, headers = create_plant(client)
    reminder = client.post(
        f"/api/v1/plants/{plant['id']}/reminders",
        headers=headers,
        json={
            "title": "Полить",
            "kind": "watering",
            "due_at": "2026-07-13T18:00:00+00:00",
            "timezone": "UTC",
        },
    ).json()
    now = datetime.fromisoformat(reminder["due_at"]) - timedelta(hours=2)

    checked = []
    original = notification_service._persisted_keys

    def spy(db, keys):
        checked.append(set(keys))
        return original(db, keys)

    monkeypatch.setattr(notification_service, "_persisted_keys", spy)

    with SessionLocal() as db:
        assert notification_service.generate_due_notifications(
            db, now=now, include_weather=False
        ) == 1
        assert notification_service.generate_due_notifications(
            db, now=now, include_weather=False
        ) == 0

    # Only this run's keys are looked up, never the whole table.
    assert checked[-1] == {f"reminder:{reminder['id']}"}
    assert all(len(keys) <= 1 for keys in checked)


# --- 3. Weather cache is bounded ---------------------------------------------


def _weather_service() -> WeatherService:
    def handler(request: httpx.Request) -> httpx.Response:
        if "geocoding-api" in request.url.host:
            return httpx.Response(200, json={"results": [{
                "name": request.url.params.get("name"),
                "country_code": "LV",
                "latitude": 56.95,
                "longitude": 24.1,
                "timezone": "Europe/Riga",
            }]})
        return httpx.Response(200, json={
            "timezone": "Europe/Riga",
            "daily": {
                "time": ["2026-07-12"],
                "weather_code": [3],
                "temperature_2m_max": [22],
                "temperature_2m_min": [14],
                "precipitation_sum": [0],
                "snowfall_sum": [0],
                "wind_speed_10m_max": [12],
                "et0_fao_evapotranspiration": [3.1],
            },
        })

    return WeatherService(httpx.Client(transport=httpx.MockTransport(handler)))


def test_weather_cache_never_exceeds_configured_entries(monkeypatch):
    monkeypatch.setattr(settings, "weather_cache_entries", 3)
    service = _weather_service()
    for index in range(6):
        service.get_forecast(f"City{index}", "ru")
    assert len(service._cache) == 3
    assert list(service._cache)[0] == "ru:city3"


# --- 4. Logout reports whether a session was really revoked ------------------


def test_logout_without_token_reports_no_revocation(client):
    register(client, "logout-no-token@example.com")
    client.cookies.clear()  # simulate a lost refresh cookie
    response = client.post("/api/v1/auth/logout")
    assert response.status_code == 204
    assert response.headers["x-session-revoked"] == "false"


def test_logout_revokes_once_and_reports_it(client):
    refresh_token = client.post("/api/v1/auth/register", json={
        "email": "logout-token@example.com",
        "name": "Садовод",
        "password": "strong-password",
    }).json()["refresh_token"]

    first = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert first.status_code == 204
    assert first.headers["x-session-revoked"] == "true"

    second = client.post("/api/v1/auth/logout", json={"refresh_token": refresh_token})
    assert second.status_code == 204
    assert second.headers["x-session-revoked"] == "false"


# --- 5. Oversized bodies, including chunked ones ------------------------------


def test_declared_oversized_body_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_request_body_mb", 1)
    response = client.post(
        "/api/v1/auth/register",
        content=b"x" * (1024 * 1024 + 10),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413


def test_chunked_body_without_content_length_is_rejected(client, monkeypatch):
    monkeypatch.setattr(settings, "max_request_body_mb", 1)

    def chunks():
        for _ in range(200):
            yield b"x" * 10_000

    response = client.post(
        "/api/v1/auth/register",
        content=chunks(),
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 413
    assert response.json()["detail"]


def test_normal_body_still_passes_the_limit_middleware(client):
    response = client.post("/api/v1/auth/register", json={
        "email": "normal-body@example.com",
        "name": "Садовод",
        "password": "strong-password",
    })
    assert response.status_code == 201


# --- 6. Partner postback keys are keyed hashes --------------------------------


def test_partner_key_hash_is_keyed_and_upgrades_legacy_digests(client):
    value = "postback-secret-value"
    assert hash_partner_key(value) != legacy_partner_key_hash(value)
    assert verify_partner_key(value, hash_partner_key(value)) == (True, False)
    assert verify_partner_key(value, legacy_partner_key_hash(value)) == (True, True)
    assert verify_partner_key("other", legacy_partner_key_hash(value)) == (False, False)
    assert verify_partner_key(value, None) == (False, False)

    admin_headers = register(client, "legacy-key-admin@example.com")
    assert set_admin("legacy-key-admin@example.com", True)
    owner_headers = register(client, "legacy-key-owner@example.com")
    customer_headers = register(client, "legacy-key-customer@example.com")
    partner = client.post("/api/v1/admin/partners", headers=admin_headers, json={
        "name": "Legacy Key Partner",
        "website_url": "https://legacy.example.com",
    }).json()
    assert client.post(
        f"/api/v1/admin/partners/{partner['id']}/members",
        headers=admin_headers,
        json={"email": "legacy-key-owner@example.com", "role": "owner"},
    ).status_code == 201
    product = client.post("/api/v1/admin/products", headers=admin_headers, json={
        "partner_id": partner["id"],
        "name": "Legacy tool",
        "category": "tools",
        "product_url": "https://legacy.example.com/item",
        "regions": [],
    }).json()
    lead = client.post(
        f"/api/v1/products/{product['id']}/lead", headers=customer_headers, json={}
    ).json()
    signed_click_id = lead["redirect_url"].rsplit("/", 1)[1]
    assert client.get(lead["redirect_url"], follow_redirects=False).status_code == 302

    api_key = client.post(
        "/api/v1/partner/postback-key", headers=owner_headers
    ).json()["api_key"]
    with SessionLocal() as db:
        stored = db.get(Partner, partner["id"])
        stored.postback_secret_hash = legacy_partner_key_hash(api_key)
        db.commit()

    # The legacy digest is still accepted, so the claim goes through.
    assert client.post(
        "/api/v1/partner/conversions/postback",
        headers={"X-Partner-Key": api_key},
        json={
            "signed_click_id": signed_click_id,
            "partner_reference": "order-legacy-001",
            "conversion_value_cents": 1999,
        },
    ).status_code == 200

    with SessionLocal() as db:
        stored = db.get(Partner, partner["id"])
        assert stored.postback_secret_hash == hash_partner_key(api_key)


# --- 7. Alert webhook never opens a non-HTTP scheme ---------------------------


def test_alert_webhook_rejects_file_scheme(monkeypatch):
    class ImmediateThread:
        def __init__(self, *, target, args, **_kwargs):
            self.target = target
            self.args = args

        def start(self):
            self.target(*self.args)

    monkeypatch.setattr(settings, "alert_webhook_url", "file:///tmp/ai-garden-alert")
    monkeypatch.setattr(settings, "alert_min_interval_seconds", 0)
    monkeypatch.setattr(alert_service, "Thread", ImmediateThread)
    alert_service.reset_alert_rate_limits()

    assert alert_service.dispatch_operational_alert("scheme_test") is True
    # Direct delivery must also refuse instead of reading a local path.
    alert_service._send(b"{}")


def test_runtime_settings_reject_unsafe_alert_scheme(monkeypatch):
    monkeypatch.setattr(settings, "alert_webhook_url", "file:///tmp/ai-garden-alert")
    with pytest.raises(RuntimeError, match="http"):
        validate_runtime_settings()


def test_runtime_settings_reject_bounded_weather_cache(monkeypatch):
    monkeypatch.setattr(settings, "weather_cache_entries", 4)
    with pytest.raises(RuntimeError, match="WEATHER_CACHE_ENTRIES"):
        validate_runtime_settings()

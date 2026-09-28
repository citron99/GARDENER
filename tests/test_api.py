from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from threading import Event, Lock

from PIL import Image
from sqlalchemy import select

import app.main as main_module
from app.ai.base import AIProviderError
from app.config import settings
from app.database import SessionLocal
from app.models import (
    AdminAuditLog,
    AIRequestLog,
    Diagnosis,
    DiagnosisJob,
    PartnerInvoice,
    UserNotification,
)
from app.schemas import WeatherDailyRead, WeatherForecastRead, WeatherWarningRead


def image_bytes(image_format="JPEG", size=(32, 32), exif=None):
    output = BytesIO()
    image = Image.new("RGB", size, color=(46, 139, 87))
    options = {"exif": exif} if exif else {}
    image.save(output, format=image_format, **options)
    return output.getvalue()


def register(client, email="user@example.com"):
    response = client.post(
        "/api/v1/auth/register",
        json={
            "email": email,
            "name": "Садовод",
            "password": "strong-password",
            "region": "Riga",
        },
    )
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def create_plant(client, headers=None):
    headers = headers or register(client)
    garden = client.post(
        "/api/v1/gardens",
        headers=headers,
        json={"name": "Мой сад", "kind": "greenhouse", "location": "Riga"},
    )
    assert garden.status_code == 201
    response = client.post(
        "/api/v1/plants",
        headers=headers,
        json={
            "garden_id": garden.json()["id"],
            "name": "Томат",
            "taxon_id": "solanum.lycopersicum",
            "growing_place": "greenhouse",
            "region": "Riga",
        },
    )
    assert response.status_code == 201
    return response.json(), headers


def resolve_diagnosis_job(client, headers, response):
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "succeeded", job
    diagnosis = client.get(f"/api/v1/diagnoses/{job['diagnosis_id']}", headers=headers)
    assert diagnosis.status_code == 200
    return diagnosis


def test_health(client):
    assert client.get("/health").json() == {
        "status": "ok",
        "ai_provider": "mock",
        "demo_mode": True,
        "diagnosis_execution_mode": "sync",
        "partner_commerce_enabled": True,
        "b2b_invoicing_enabled": True,
    }


def test_host_body_limit_and_production_hsts(client, monkeypatch):
    assert (
        client.get("/health", headers={"host": "untrusted.example"}).status_code == 400
    )
    too_large = client.post(
        "/api/v1/auth/login",
        content=b"{}",
        headers={
            "content-length": str((settings.max_request_body_mb + 1) * 1024 * 1024),
            "accept-language": "en",
        },
    )
    assert too_large.status_code == 413
    assert too_large.json()["detail"] == "The request is too large"

    monkeypatch.setattr(settings, "environment", "production")
    response = client.get("/health")
    assert response.headers["strict-transport-security"].startswith("max-age=31536000")


def test_public_privacy_config_exposes_only_public_retention_metadata(client):
    for path in ("/privacy", "/terms", "/b2b-terms"):
        page = client.get(path)
        assert page.status_code == 200
        assert "text/html" in page.headers["content-type"]
    response = client.get("/api/v1/public/privacy-config")
    assert response.status_code == 200
    payload = response.json()
    assert payload["policy_version"]
    assert payload["retention_days"]["ai_logs"] == settings.ai_log_retention_days
    assert payload["processors"]["weather"] == "Open-Meteo"
    serialized = response.text.casefold()
    assert "secret" not in serialized
    assert "api_key" not in serialized


def test_metrics_are_token_protected_and_use_bounded_route_labels(client, monkeypatch):
    token = "metrics-test-token-that-is-longer-than-thirty-two"
    monkeypatch.setattr(main_module.settings, "metrics_token", token)
    client.get("/health")
    assert client.get("/metrics").status_code == 401
    response = client.get("/metrics", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert (
        'ai_garden_http_requests_total{method="GET",route="/health",status="200"} 1'
        in response.text
    )
    assert "ai_garden_diagnosis_jobs_queued 0" in response.text
    assert "ai_garden_partner_invoices_outstanding_cents 0" in response.text


def test_profile_language_update_and_localized_errors(client):
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "language@example.com",
            "name": "Valodas lietotājs",
            "password": "strong-password",
            "language": "lv",
            "region": "Rīga",
        },
    )
    assert registered.status_code == 201
    headers = {"Authorization": f"Bearer {registered.json()['access_token']}"}
    assert client.get("/api/v1/users/me", headers=headers).json()["language"] == "lv"
    updated = client.patch("/api/v1/users/me", headers=headers, json={"language": "en"})
    assert updated.status_code == 200
    assert updated.json()["language"] == "en"
    assert client.patch("/api/v1/users/me", headers=headers, json={}).status_code == 422
    assert (
        client.post(
            "/api/v1/gardens", headers=headers, json={"name": "Dārzs"}
        ).status_code
        == 201
    )
    localized_headers = {**headers, "Accept-Language": "lv-LV,lv;q=0.9"}
    duplicate = client.post(
        "/api/v1/gardens", headers=localized_headers, json={"name": "Dārzs"}
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["detail"] == "Dārzs ar šādu nosaukumu jau pastāv"
    bad_login = client.post(
        "/api/v1/auth/login",
        headers={"Accept-Language": "en"},
        json={"email": "language@example.com", "password": "wrong-password"},
    )
    assert bad_login.status_code == 401
    assert bad_login.json()["detail"] == "Invalid email or password"


def test_web_interface_and_security_headers(client):
    page = client.get("/")
    assert page.status_code == 200
    assert "AI Garden" in page.text
    assert "Журнал растения" in page.text
    assert "Напоминания" in page.text
    assert "Прогноз для сада" in page.text
    assert 'id="editPlant"' in page.text
    assert 'id="deleteGarden"' in page.text
    assert 'id="calendarWorkspace"' in page.text
    assert 'id="adminWorkspace"' in page.text
    assert 'id="partnerWorkspace"' in page.text
    assert 'id="billingWorkspace"' in page.text
    assert 'id="telegramWorkspace"' in page.text
    assert 'id="adminPartnerForm"' in page.text
    assert 'id="adminLeads"' in page.text
    assert 'id="adminInvoiceForm"' in page.text
    assert 'id="adminInvoices"' in page.text
    assert 'id="partnerInvoices"' in page.text
    assert 'id="passwordResetRequestForm"' in page.text
    assert 'id="passwordResetConfirmForm"' in page.text
    assert "/static/app.js" in page.text
    assert "/static/i18n.js" in page.text
    assert "default-src 'self'" in page.headers["content-security-policy"]
    assert page.headers["x-frame-options"] == "DENY"
    assert page.headers["x-request-id"]
    assert client.get("/static/styles.css").status_code == 200
    assert client.get("/static/i18n.js").status_code == 200
    script = client.get("/static/app.js")
    assert script.status_code == 200
    assert "/reanalyze" in script.text
    assert "/care-events" in script.text
    assert "/reminders" in script.text
    assert "/weather" in script.text
    assert "include_weather" in script.text
    assert "Погода временно недоступна" in script.text
    assert "/diagnoses/async" in script.text
    assert "/diagnosis-jobs/" in script.text
    assert "/admin/overview" in script.text
    assert "/admin/products" in script.text
    assert "/admin/leads" in script.text
    assert "/partner/overview" in script.text
    assert "/partner/products" in script.text
    assert "/billing/checkout" in script.text
    assert "/billing/portal" in script.text
    assert "/telegram/status" in script.text
    assert "/telegram/link" in script.text
    assert "Реклама · предложения партнёров" in script.text
    privacy = client.get("/privacy")
    assert privacy.status_code == 200
    assert "store=False" in privacy.text


def test_create_plant(client):
    plant, headers = create_plant(client)
    assert plant["name"] == "Томат"
    assert (
        client.get(f"/api/v1/plants/{plant['id']}", headers=headers).status_code == 200
    )


def test_update_garden_and_plant_with_ownership(client):
    plant, headers = create_plant(client)
    garden_id = plant["garden_id"]
    garden = client.patch(
        f"/api/v1/gardens/{garden_id}",
        headers=headers,
        json={"name": "Обновлённый сад", "location": "Jūrmala"},
    )
    assert garden.status_code == 200
    assert garden.json()["location"] == "Jūrmala"

    updated = client.patch(
        f"/api/v1/plants/{plant['id']}",
        headers=headers,
        json={"name": "Черри", "species": "Solanum lycopersicum"},
    )
    assert updated.status_code == 200
    assert updated.json()["name"] == "Черри"
    assert (
        client.patch(
            f"/api/v1/plants/{plant['id']}", headers=headers, json={}
        ).status_code
        == 422
    )
    assert (
        client.patch(
            f"/api/v1/plants/{plant['id']}", headers=headers, json={"name": None}
        ).status_code
        == 422
    )

    duplicate = client.post(
        "/api/v1/gardens", headers=headers, json={"name": "Второй сад"}
    )
    assert duplicate.status_code == 201
    assert (
        client.patch(
            f"/api/v1/gardens/{duplicate.json()['id']}",
            headers=headers,
            json={"name": "Обновлённый сад"},
        ).status_code
        == 409
    )

    stranger_headers = register(client, "edit-stranger@example.com")
    foreign_garden = client.post(
        "/api/v1/gardens", headers=stranger_headers, json={"name": "Чужой сад"}
    ).json()
    assert (
        client.patch(
            f"/api/v1/gardens/{garden_id}",
            headers=stranger_headers,
            json={"name": "Взлом"},
        ).status_code
        == 404
    )
    assert (
        client.patch(
            f"/api/v1/plants/{plant['id']}",
            headers=headers,
            json={"garden_id": foreign_garden["id"]},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/plants/{plant['id']}", headers=stranger_headers
        ).status_code
        == 404
    )


def test_delete_plant_removes_related_data_and_photo_file(client):
    plant, headers = create_plant(client)
    uploaded = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]
    assert (
        client.post(
            f"/api/v1/plants/{plant['id']}/care-events",
            headers=headers,
            json={"event_type": "watering", "occurred_at": "2026-07-12T08:00:00Z"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/v1/plants/{plant['id']}/reminders",
            headers=headers,
            json={"title": "Осмотреть", "due_at": "2026-07-13T09:00:00Z"},
        ).status_code
        == 201
    )
    plant_directory = settings.upload_dir / str(plant["id"])
    assert any(plant_directory.iterdir())

    assert (
        client.delete(f"/api/v1/plants/{plant['id']}", headers=headers).status_code
        == 204
    )
    assert (
        client.get(f"/api/v1/plants/{plant['id']}", headers=headers).status_code == 404
    )
    assert (
        client.get(f"/api/v1/photos/{uploaded['id']}", headers=headers).status_code
        == 404
    )
    assert not plant_directory.exists()


def test_delete_garden_cascades_plants_and_files(client):
    plant, headers = create_plant(client)
    client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.png", image_bytes("PNG"), "image/png"))],
    )
    assert (
        client.delete(
            f"/api/v1/gardens/{plant['garden_id']}", headers=headers
        ).status_code
        == 204
    )
    assert client.get("/api/v1/gardens", headers=headers).json() == []
    assert client.get("/api/v1/plants", headers=headers).json() == []
    assert not (settings.upload_dir / str(plant["id"])).exists()


def test_care_journal_crud_history_and_ownership(client):
    plant, headers = create_plant(client)
    created = client.post(
        f"/api/v1/plants/{plant['id']}/care-events",
        headers=headers,
        json={
            "event_type": "watering",
            "occurred_at": "2026-07-12T08:30:00+03:00",
            "amount": 1.5,
            "unit": "l",
            "notes": "Полив после проверки влажности",
        },
    )
    assert created.status_code == 201
    event = created.json()
    assert event["event_type"] == "watering"
    assert event["plant_id"] == plant["id"]

    listed = client.get(
        f"/api/v1/plants/{plant['id']}/care-events",
        headers=headers,
        params={"start": "2026-07-01T00:00:00Z", "end": "2026-07-31T23:59:59Z"},
    )
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()] == [event["id"]]
    assert (
        client.get(
            f"/api/v1/plants/{plant['id']}/care-events",
            headers=headers,
            params={"offset": 1, "limit": 1},
        ).json()
        == []
    )

    updated = client.patch(
        f"/api/v1/care-events/{event['id']}",
        headers=headers,
        json={"event_type": "fertilizing", "product": "Органическое удобрение"},
    )
    assert updated.status_code == 200
    assert updated.json()["product"] == "Органическое удобрение"

    history = client.get(f"/api/v1/plants/{plant['id']}/history", headers=headers)
    assert history.status_code == 200
    assert history.json()["care_events"][0]["id"] == event["id"]

    stranger_headers = register(client, "journal-stranger@example.com")
    assert (
        client.patch(
            f"/api/v1/care-events/{event['id']}",
            headers=stranger_headers,
            json={"notes": "Чужая запись"},
        ).status_code
        == 404
    )
    assert (
        client.get(
            f"/api/v1/plants/{plant['id']}/care-events", headers=stranger_headers
        ).status_code
        == 404
    )

    assert (
        client.delete(f"/api/v1/care-events/{event['id']}", headers=headers).status_code
        == 204
    )
    assert (
        client.get(f"/api/v1/plants/{plant['id']}/care-events", headers=headers).json()
        == []
    )


def test_care_journal_validates_time_and_amount(client):
    plant, headers = create_plant(client)
    path = f"/api/v1/plants/{plant['id']}/care-events"
    assert (
        client.post(
            path,
            headers=headers,
            json={"event_type": "watering", "occurred_at": "2026-07-12T08:30:00"},
        ).status_code
        == 422
    )
    assert (
        client.post(
            path, headers=headers, json={"event_type": "watering", "unit": "l"}
        ).status_code
        == 422
    )


def test_reminders_calendar_completion_and_ownership(client):
    plant, headers = create_plant(client)
    care = client.post(
        f"/api/v1/plants/{plant['id']}/care-events",
        headers=headers,
        json={
            "event_type": "watering",
            "occurred_at": "2026-07-12T08:00:00Z",
            "notes": "Утренний полив",
        },
    )
    assert care.status_code == 201
    created = client.post(
        f"/api/v1/plants/{plant['id']}/reminders",
        headers=headers,
        json={
            "kind": "inspection",
            "title": "Проверить нижние листья",
            "due_at": "2026-07-13T09:00:00+03:00",
        },
    )
    assert created.status_code == 201
    reminder = created.json()
    assert reminder["completed"] is False

    calendar = client.get(
        "/api/v1/calendar",
        headers=headers,
        params={"start": "2026-07-01T00:00:00Z", "end": "2026-08-01T00:00:00Z"},
    )
    assert calendar.status_code == 200
    assert {item["item_type"] for item in calendar.json()} == {"care_event", "reminder"}
    assert all(item["plant_name"] == "Томат" for item in calendar.json())

    completed = client.patch(
        f"/api/v1/reminders/{reminder['id']}", headers=headers, json={"completed": True}
    )
    assert completed.status_code == 200
    assert completed.json()["completed"] is True
    assert (
        client.get(f"/api/v1/plants/{plant['id']}/reminders", headers=headers).json()
        == []
    )
    all_reminders = client.get(
        f"/api/v1/plants/{plant['id']}/reminders",
        headers=headers,
        params={"include_completed": True},
    ).json()
    assert all_reminders[0]["id"] == reminder["id"]
    assert (
        client.get(
            f"/api/v1/plants/{plant['id']}/reminders",
            headers=headers,
            params={"include_completed": True, "offset": 1, "limit": 1},
        ).json()
        == []
    )
    assert (
        client.get(f"/api/v1/plants/{plant['id']}/history", headers=headers).json()[
            "reminders"
        ][0]["completed"]
        is True
    )

    stranger_headers = register(client, "reminder-stranger@example.com")
    assert (
        client.patch(
            f"/api/v1/reminders/{reminder['id']}",
            headers=stranger_headers,
            json={"completed": False},
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/reminders/{reminder['id']}", headers=headers
        ).status_code
        == 204
    )


def test_reminders_and_calendar_validate_timezone_and_range(client):
    plant, headers = create_plant(client)
    path = f"/api/v1/plants/{plant['id']}/reminders"
    assert (
        client.post(
            path,
            headers=headers,
            json={"title": "Полить", "due_at": "2026-07-13T09:00:00"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/calendar",
            headers=headers,
            params={"start": "2026-08-01T00:00:00Z", "end": "2026-07-01T00:00:00Z"},
        ).status_code
        == 422
    )
    assert (
        client.get(
            "/api/v1/calendar",
            headers=headers,
            params={"start": "2026-01-01T00:00:00Z", "end": "2027-02-01T00:00:00Z"},
        ).status_code
        == 422
    )


def test_recurring_reminder_creates_next_occurrence_with_delivery_preferences(client):
    plant, headers = create_plant(client)
    created = client.post(
        f"/api/v1/plants/{plant['id']}/reminders",
        headers=headers,
        json={
            "kind": "watering",
            "title": "Weekly watering check",
            "due_at": "2026-07-13T09:00:00+03:00",
            "recurrence": "weekly",
            "timezone": "Europe/Riga",
            "preferred_channel": "telegram",
        },
    )
    assert created.status_code == 201
    assert (
        client.patch(
            f"/api/v1/reminders/{created.json()['id']}",
            headers=headers,
            json={"completed": True},
        ).status_code
        == 200
    )
    reminders = client.get(
        f"/api/v1/plants/{plant['id']}/reminders", headers=headers
    ).json()
    assert len(reminders) == 1
    assert reminders[0]["recurrence"] == "weekly"
    assert reminders[0]["preferred_channel"] == "telegram"
    assert reminders[0]["due_at"].startswith("2026-07-20")


def test_recurring_reminders_preserve_local_time_across_month_end_and_dst(client):
    plant, headers = create_plant(client)
    path = f"/api/v1/plants/{plant['id']}/reminders"
    monthly = client.post(
        path,
        headers=headers,
        json={
            "title": "Month-end check",
            "due_at": "2026-01-31T09:00:00+02:00",
            "recurrence": "monthly",
            "timezone": "Europe/Riga",
        },
    ).json()
    assert (
        client.patch(
            f"/api/v1/reminders/{monthly['id']}",
            headers=headers,
            json={"skip_occurrence": True},
        ).status_code
        == 200
    )
    current = client.get(path, headers=headers).json()
    next_monthly = next(item for item in current if item["title"] == "Month-end check")
    assert next_monthly["due_at"].startswith("2026-02-28T07:00:00")

    weekly = client.post(
        path,
        headers=headers,
        json={
            "title": "DST watering",
            "due_at": "2026-03-22T09:00:00+02:00",
            "recurrence": "weekly",
            "timezone": "Europe/Riga",
        },
    ).json()
    assert (
        client.patch(
            f"/api/v1/reminders/{weekly['id']}",
            headers=headers,
            json={"completed": True},
        ).status_code
        == 200
    )
    current = client.get(path, headers=headers).json()
    next_weekly = next(item for item in current if item["title"] == "DST watering")
    assert next_weekly["due_at"].startswith("2026-03-29T06:00:00")
    snoozed = client.patch(
        f"/api/v1/reminders/{next_weekly['id']}",
        headers=headers,
        json={"snooze_minutes": 60},
    )
    assert snoozed.status_code == 200
    assert snoozed.json()["due_at"].startswith("2026-03-29T07:00:00")


def test_calendar_includes_localized_seasonal_tasks(client):
    _plant, headers = create_plant(client)
    response = client.get(
        "/api/v1/calendar",
        headers=headers,
        params={
            "start": "2026-04-01T00:00:00Z",
            "end": "2026-05-01T00:00:00Z",
        },
    )
    assert response.status_code == 200
    seasonal = [
        item for item in response.json() if item["item_type"] == "seasonal_task"
    ]
    assert seasonal
    assert seasonal[0]["event_type"] == "soil-prep"
    assert seasonal[0]["garden_name"] == "Мой сад"


def test_due_notifications_are_idempotent_private_and_readable(client):
    plant, headers = create_plant(client)
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)
    reminder = client.post(
        f"/api/v1/plants/{plant['id']}/reminders",
        headers=headers,
        json={
            "kind": "watering",
            "title": "Проверить влажность",
            "due_at": (now + timedelta(hours=4)).isoformat(),
        },
    )
    assert reminder.status_code == 201
    from app.services.notification_service import generate_due_notifications

    with SessionLocal() as db:
        assert generate_due_notifications(db, now=now, include_weather=False) == 1
        assert generate_due_notifications(db, now=now, include_weather=False) == 0
        assert len(list(db.scalars(select(UserNotification)))) == 1

    listed = client.get("/api/v1/notifications?unread_only=true", headers=headers)
    assert listed.status_code == 200
    assert listed.json()[0]["kind"] == "reminder"
    assert (
        client.get(
            "/api/v1/notifications",
            headers=headers,
            params={"unread_only": True, "offset": 1, "limit": 1},
        ).json()
        == []
    )
    notification_id = listed.json()[0]["id"]
    stranger = register(client, "notification-stranger@example.com")
    assert (
        client.patch(
            f"/api/v1/notifications/{notification_id}/read", headers=stranger
        ).status_code
        == 404
    )
    marked = client.patch(
        f"/api/v1/notifications/{notification_id}/read", headers=headers
    )
    assert marked.status_code == 200
    assert marked.json()["read_at"] is not None


def test_garden_weather_and_calendar_warnings_are_private(client, monkeypatch):
    plant, headers = create_plant(client)
    garden_id = plant["garden_id"]

    class StubWeatherService:
        def get_forecast(self, location, language):
            assert location == "Riga"
            assert language == "ru"
            return WeatherForecastRead(
                location="Rīga",
                country_code="LV",
                latitude=56.95,
                longitude=24.1,
                timezone="Europe/Riga",
                fetched_at="2026-07-12T08:00:00Z",
                daily=[
                    WeatherDailyRead(
                        date="2026-07-13",
                        temperature_min_c=-1,
                        temperature_max_c=11,
                        precipitation_mm=2,
                        wind_speed_max_kmh=20,
                    )
                ],
                warnings=[
                    WeatherWarningRead(
                        date="2026-07-13",
                        kind="frost",
                        severity="critical",
                        title="Заморозок",
                        advice="Защитите чувствительные растения.",
                    )
                ],
            )

    monkeypatch.setattr(main_module, "weather_service", StubWeatherService())
    weather = client.get(f"/api/v1/gardens/{garden_id}/weather", headers=headers)
    assert weather.status_code == 200
    assert weather.json()["warnings"][0]["kind"] == "frost"

    calendar = client.get(
        "/api/v1/calendar",
        headers=headers,
        params={
            "start": "2026-07-01T00:00:00Z",
            "end": "2026-08-01T00:00:00Z",
            "include_weather": True,
        },
    )
    assert calendar.status_code == 200
    warning = next(
        item for item in calendar.json() if item["item_type"] == "weather_warning"
    )
    assert warning["garden_id"] == garden_id
    assert warning["event_type"] == "frost"
    assert warning["severity"] == "critical"
    assert "Защитите" in warning["description"]

    stranger_headers = register(client, "weather-stranger@example.com")
    assert (
        client.get(
            f"/api/v1/gardens/{garden_id}/weather", headers=stranger_headers
        ).status_code
        == 404
    )


def test_registration_login_and_profile(client):
    headers = register(client)
    profile = client.get("/api/v1/users/me", headers=headers)
    assert profile.status_code == 200
    assert profile.json()["email"] == "user@example.com"
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "USER@example.com", "password": "strong-password"},
    )
    assert login.status_code == 200


def test_refresh_rotation_logout_and_user_block(client):
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "session-user@example.com",
            "name": "Session user",
            "password": "strong-password",
        },
    )
    assert registered.status_code == 201
    pair = registered.json()
    rotated = client.post(
        "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
    )
    assert rotated.status_code == 200
    assert rotated.json()["refresh_token"] != pair["refresh_token"]
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": pair["refresh_token"]}
        ).status_code
        == 401
    )
    fresh = rotated.json()
    assert (
        client.post(
            "/api/v1/auth/refresh", json={"refresh_token": fresh["refresh_token"]}
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/logout", json={"refresh_token": fresh["refresh_token"]}
        ).status_code
        == 204
    )
    assert (
        client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {fresh['access_token']}"},
        ).status_code
        == 401
    )

    admin_headers = register(client, "block-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("block-admin@example.com", True)
    user_pair = client.post(
        "/api/v1/auth/login",
        json={
            "email": "session-user@example.com",
            "password": "strong-password",
        },
    ).json()
    user_headers = {"Authorization": f"Bearer {user_pair['access_token']}"}
    user_id = client.get("/api/v1/users/me", headers=user_headers).json()["id"]
    blocked = client.patch(
        f"/api/v1/admin/users/{user_id}/blocked?blocked=true", headers=admin_headers
    )
    assert blocked.status_code == 200
    assert client.get("/api/v1/users/me", headers=user_headers).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login",
            json={
                "email": "session-user@example.com",
                "password": "strong-password",
            },
        ).status_code
        == 401
    )
    audit = client.get("/api/v1/admin/audit-logs", headers=admin_headers)
    assert audit.status_code == 200
    assert audit.json()[0]["action"] == "user.block"
    with SessionLocal() as db:
        assert (
            db.scalar(
                select(AdminAuditLog).where(
                    AdminAuditLog.target_id == str(user_id),
                )
            )
            is not None
        )


def test_browser_refresh_cookie_rotates_and_logout_revokes_session(client):
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "cookie-session@example.com",
            "name": "Cookie session",
            "password": "strong-password",
        },
    )
    assert registered.status_code == 201
    assert "HttpOnly" in registered.headers["set-cookie"]
    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    access_token = refreshed.json()["access_token"]
    assert client.post("/api/v1/auth/logout").status_code == 204
    assert (
        client.get(
            "/api/v1/users/me",
            headers={"Authorization": f"Bearer {access_token}"},
        ).status_code
        == 401
    )


def test_gdpr_export_and_account_deletion_remove_private_photo(client):
    plant, headers = create_plant(client)
    photo = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]
    stored = settings.upload_dir / str(plant["id"])
    assert stored.exists()
    exported = client.get("/api/v1/users/me/export", headers=headers)
    assert exported.status_code == 200
    assert exported.json()["plants"][0]["id"] == plant["id"]
    assert (
        exported.json()["photos"][0]["download_url"] == f"/api/v1/photos/{photo['id']}"
    )
    assert {
        "diagnosis_revisions",
        "diagnosis_questions",
        "diagnosis_answers",
        "diagnosis_feedback",
        "notifications",
        "ai_request_logs",
        "diagnosis_jobs",
        "auth_sessions",
        "subscription",
        "telegram_account",
        "telegram_updates",
    }.issubset(exported.json())
    assert "refresh_token_hash" not in exported.text
    assert (
        client.request(
            "DELETE",
            "/api/v1/users/me",
            headers=headers,
            json={"password": "wrong-password"},
        ).status_code
        == 403
    )
    deleted = client.request(
        "DELETE",
        "/api/v1/users/me",
        headers=headers,
        json={"password": "strong-password"},
    )
    assert deleted.status_code == 204
    assert not stored.exists()
    assert client.get("/api/v1/users/me", headers=headers).status_code == 401


def test_login_rate_limit(client, monkeypatch):
    register(client, "rate-user@example.com")
    monkeypatch.setattr(main_module.settings, "auth_rate_limit_attempts", 2)
    payload = {"email": "rate-user@example.com", "password": "wrong-password"}
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post("/api/v1/auth/login", json=payload).status_code == 401
    assert client.post("/api/v1/auth/login", json=payload).status_code == 429


def test_email_verification_and_password_reset_revoke_sessions(client):
    registered = client.post(
        "/api/v1/auth/register",
        json={
            "email": "verify-user@example.com",
            "name": "Verify",
            "password": "strong-password",
        },
    ).json()
    from app.models import User
    from app.services.account_security_service import issue_action_token

    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "verify-user@example.com"))
        user.email_verified_at = None
        db.commit()
        verification = issue_action_token(db, user, "verify_email")
    unverified_headers = {"Authorization": f"Bearer {registered['access_token']}"}
    assert client.get("/api/v1/users/me", headers=unverified_headers).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/refresh",
            json={
                "refresh_token": registered["refresh_token"],
            },
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={
                "email": "verify-user@example.com",
                "password": "strong-password",
            },
        ).status_code
        == 403
    )
    assert client.post(
        "/api/v1/auth/email-verification/confirm", json={"token": verification}
    ).json() == {"verified": True}
    assert (
        client.post(
            "/api/v1/auth/email-verification/confirm", json={"token": verification}
        ).status_code
        == 400
    )
    assert (
        client.post(
            "/api/v1/auth/login",
            json={
                "email": "verify-user@example.com",
                "password": "strong-password",
            },
        ).status_code
        == 200
    )
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "verify-user@example.com"))
        reset = issue_action_token(db, user, "password_reset")
    assert client.post(
        "/api/v1/auth/password-reset/confirm",
        json={
            "token": reset,
            "new_password": "new-strong-password",
        },
    ).json() == {"password_reset": True}
    old_headers = {"Authorization": f"Bearer {registered['access_token']}"}
    assert client.get("/api/v1/users/me", headers=old_headers).status_code == 401
    assert (
        client.post(
            "/api/v1/auth/login",
            json={
                "email": "verify-user@example.com",
                "password": "new-strong-password",
            },
        ).status_code
        == 200
    )


def test_admin_api_is_forbidden_for_regular_users(client):
    headers = register(client, "regular-admin-check@example.com")
    for path in (
        "/api/v1/admin/overview",
        "/api/v1/admin/users",
        "/api/v1/admin/diagnosis-jobs",
        "/api/v1/admin/ai-requests",
        "/api/v1/admin/audit-logs",
        "/api/v1/admin/knowledge/sources",
    ):
        assert client.get(path, headers=headers).status_code == 403


def test_admin_overview_and_safe_operational_lists(client):
    admin_headers = register(client, "admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("ADMIN@example.com", True) is True
    profile = client.get("/api/v1/users/me", headers=admin_headers)
    assert profile.status_code == 200
    assert profile.json()["is_admin"] is True

    plant, user_headers = create_plant(
        client, register(client, "managed-user@example.com")
    )
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=user_headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    job = client.post(
        "/api/v1/diagnoses/async",
        headers=user_headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "Нижние листья начали желтеть неделю назад",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    )
    assert job.status_code == 202

    overview = client.get("/api/v1/admin/overview", headers=admin_headers)
    assert overview.status_code == 200
    metrics = overview.json()
    assert metrics["users"] == 2
    assert metrics["gardens"] == 1
    assert metrics["plants"] == 1
    assert metrics["diagnoses"] == 1
    assert metrics["ai_requests_successful"] == 1

    users = client.get("/api/v1/admin/users", headers=admin_headers)
    assert users.status_code == 200
    assert any(item["email"] == "managed-user@example.com" for item in users.json())
    assert all("password_hash" not in item for item in users.json())

    jobs = client.get("/api/v1/admin/diagnosis-jobs", headers=admin_headers)
    assert jobs.status_code == 200
    assert jobs.json()[0]["status"] == "succeeded"
    assert "payload" not in jobs.json()[0]

    requests = client.get("/api/v1/admin/ai-requests", headers=admin_headers)
    assert requests.status_code == 200
    assert requests.json()[0]["success"] is True
    assert "symptoms" not in requests.json()[0]
    assert "result" not in requests.json()[0]

    assert set_admin("admin@example.com", False) is True
    assert (
        client.get("/api/v1/admin/overview", headers=admin_headers).status_code == 403
    )


def test_admin_manages_verified_semantic_knowledge(client):
    admin_headers = register(client, "knowledge-admin@example.com")
    from app.admin_cli import set_admin
    from app.services.knowledge_service import retrieve_knowledge

    assert set_admin("knowledge-admin@example.com", True)
    synced = client.post("/api/v1/admin/knowledge/sync", headers=admin_headers)
    assert synced.status_code == 200
    assert synced.json()["total_sources"] >= 6
    assert synced.json()["embedding_model"] == "local-hash-concepts-v1"

    payload = {
        "id": "reviewed-powdery-mildew",
        "title": "Reviewed powdery mildew guide",
        "url": "https://example.org/reviewed/powdery-mildew",
        "summary": "Powdery mildew produces a characteristic white powder-like coating on leaves and young stems.",
        "keywords": ["powdery mildew", "white coating", "fungal disease"],
        "region": "EU",
        "language": ["en"],
        "plant_types": ["all"],
        "problem_types": ["disease"],
        "last_verified_at": "2026-07-13",
        "next_review_at": "2027-01-13",
        "reviewed_by": "test-agronomist",
        "review_role": "agronomist",
        "usage_basis": "licensed_content",
        "source_version": "2026-07-13",
        "active": True,
    }
    created = client.post(
        "/api/v1/admin/knowledge/sources", headers=admin_headers, json=payload
    )
    assert created.status_code == 200
    assert created.json()["chunks"] == 1
    assert created.json()["embedding_model"] == "local-hash-concepts-v1"
    assert created.json()["review_role"] == "agronomist"
    assert (
        retrieve_knowledge("white powdery mildew coating", language="en")[0].id
        == payload["id"]
    )

    listed = client.get("/api/v1/admin/knowledge/sources", headers=admin_headers)
    assert listed.status_code == 200
    assert any(item["id"] == payload["id"] for item in listed.json())
    assert (
        len(
            client.get(
                "/api/v1/admin/knowledge/sources",
                headers=admin_headers,
                params={"limit": 1},
            ).json()
        )
        <= 1
    )
    assert (
        client.get(
            "/api/v1/admin/knowledge/sources",
            headers=admin_headers,
            params={"offset": 10_000},
        ).json()
        == []
    )

    duplicate_url = payload | {"id": "another-reviewed-source"}
    assert (
        client.post(
            "/api/v1/admin/knowledge/sources", headers=admin_headers, json=duplicate_url
        ).status_code
        == 409
    )


def test_partner_catalog_recommendations_and_idempotent_leads(client):
    admin_headers = register(client, "catalog-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("catalog-admin@example.com", True)
    regular_headers = register(client, "catalog-user@example.com")

    assert (
        client.post(
            "/api/v1/admin/partners",
            headers=regular_headers,
            json={"name": "Forbidden", "website_url": "https://example.com"},
        ).status_code
        == 403
    )
    assert (
        client.post(
            "/api/v1/admin/partners",
            headers=admin_headers,
            json={"name": "Unsafe URL", "website_url": "http://example.com"},
        ).status_code
        == 422
    )

    partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={"name": "Riga Garden Shop", "website_url": "https://shop.example.com"},
    )
    assert partner.status_code == 201
    product = client.post(
        "/api/v1/admin/products",
        headers=admin_headers,
        json={
            "partner_id": partner.json()["id"],
            "name": "Измеритель влажности",
            "category": "tools",
            "description": "Инструмент для ручной проверки влажности грунта.",
            "product_url": "https://shop.example.com/moisture-meter",
            "regions": ["Riga", "Latvia"],
        },
    )
    assert product.status_code == 201
    assert product.json()["commercial_label"] == "Реклама партнёра"
    regulated_category_product = client.post(
        "/api/v1/admin/products",
        headers=admin_headers,
        json={
            "partner_id": partner.json()["id"],
            "name": "Концентрированное удобрение",
            "category": "nutrition",
            "description": "Товар не должен автоматически предлагаться по AI-диагнозу.",
            "product_url": "https://shop.example.com/fertilizer",
            "regions": ["Riga", "Latvia"],
        },
    )
    assert regulated_category_product.status_code == 201

    catalog = client.get("/api/v1/catalog/products", headers=regular_headers)
    assert catalog.status_code == 200
    assert catalog.json()[0]["partner_name"] == "Riga Garden Shop"
    assert (
        client.get(
            "/api/v1/catalog/products",
            headers=regular_headers,
            params={"region": "Tallinn"},
        ).json()
        == []
    )

    plant, regular_headers = create_plant(client, regular_headers)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=regular_headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    diagnosis_response = resolve_diagnosis_job(
        client,
        regular_headers,
        client.post(
            "/api/v1/diagnoses",
            headers=regular_headers,
            json={
                "plant_id": plant["id"],
                "symptoms": "Нижние листья начали желтеть неделю назад",
                "damaged_part": "leaf",
                "photo_ids": [photo_id],
            },
        ),
    )
    diagnosis_id = diagnosis_response.json()["id"]
    rule = client.post(
        "/api/v1/admin/product-recommendation-rules",
        headers=admin_headers,
        json={
            "product_id": product.json()["id"],
            "crop_name": "no-match",
            "plant_taxon_id": "solanum.lycopersicum",
            "problem_name": "no-match",
            "problem_code": "care.water_stress",
            "safe_action": "Проверить влажность грунта до полива",
            "region_code": "XX",
            "country_code": "LV",
            "expert_verified": True,
        },
    )
    assert rule.status_code == 201
    assert rule.json()["problem_code"] == "care.water_stress"
    assert (
        client.post(
            "/api/v1/admin/product-recommendation-rules",
            headers=admin_headers,
            json={
                "product_id": regulated_category_product.json()["id"],
                "crop_name": "*",
                "problem_name": "Нарушение режима полива",
                "safe_action": "Применять только после экспертного подтверждения",
                "region_code": "Riga",
                "expert_verified": True,
            },
        ).status_code
        == 201
    )
    recommendations = client.get(
        f"/api/v1/recommendations/{diagnosis_id}/products", headers=regular_headers
    )
    assert recommendations.status_code == 200
    assert [item["id"] for item in recommendations.json()] == [product.json()["id"]]

    payload = {"diagnosis_id": diagnosis_id}
    first_lead = client.post(
        f"/api/v1/products/{product.json()['id']}/lead",
        headers=regular_headers,
        json=payload,
    )
    second_lead = client.post(
        f"/api/v1/products/{product.json()['id']}/lead",
        headers=regular_headers,
        json=payload,
    )
    assert first_lead.status_code == 201
    assert second_lead.json()["id"] == first_lead.json()["id"]

    leads = client.get("/api/v1/admin/leads", headers=admin_headers)
    assert leads.status_code == 200
    assert leads.json()[0]["product_name"] == "Измеритель влажности"
    assert "symptoms" not in leads.json()[0]
    overview = client.get("/api/v1/admin/overview", headers=admin_headers).json()
    assert overview["partners"] == 1
    assert overview["active_products"] == 2
    assert overview["product_leads"] == 1

    assert (
        client.delete(
            f"/api/v1/admin/products/{product.json()['id']}", headers=admin_headers
        ).status_code
        == 204
    )
    assert (
        client.delete(
            f"/api/v1/admin/products/{regulated_category_product.json()['id']}",
            headers=admin_headers,
        ).status_code
        == 204
    )
    assert client.get("/api/v1/catalog/products", headers=regular_headers).json() == []


def test_regulated_product_registry_invalidates_revoked_recommendations(client):
    admin_headers = register(client, "registry-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("registry-admin@example.com", True)
    regular_headers = register(client, "registry-user@example.com")
    partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "Registry Partner",
            "website_url": "https://registry-partner.example",
        },
    ).json()
    product = client.post(
        "/api/v1/admin/products",
        headers=admin_headers,
        json={
            "partner_id": partner["id"],
            "name": "Registered protection product",
            "category": "protection",
            "product_url": "https://registry-partner.example/product",
            "regions": ["LV"],
        },
    ).json()
    rule_payload = {
        "product_id": product["id"],
        "crop_name": "tomato",
        "problem_name": "late blight",
        "safe_action": "Use only according to the approved label",
        "region_code": "LV",
        "expert_verified": True,
    }
    assert (
        client.post(
            "/api/v1/admin/product-recommendation-rules",
            headers=admin_headers,
            json=rule_payload,
        ).status_code
        == 422
    )
    snapshot = {
        "source_name": "vaad",
        "source_url": "https://registri.vaad.gov.lv/",
        "source_version": "2026-07-14",
        "complete_snapshot": True,
        "records": [
            {
                "jurisdiction": "LV",
                "registration_number": "VAAD-TEST-001",
                "product_name": "Registered protection product",
                "holder_name": "Registry Partner",
                "status": "active",
                "valid_from": "2025-01-01",
                "valid_until": "2027-12-31",
            }
        ],
    }
    assert (
        client.post(
            "/api/v1/admin/regulated-products/import",
            headers=regular_headers,
            json=snapshot,
        ).status_code
        == 403
    )
    imported = client.post(
        "/api/v1/admin/regulated-products/import",
        headers=admin_headers,
        json=snapshot,
    )
    assert imported.status_code == 200
    assert imported.json()["created"] == 1
    registry = client.get(
        "/api/v1/admin/regulated-products?jurisdiction=LV",
        headers=admin_headers,
    ).json()[0]
    created_rule = client.post(
        "/api/v1/admin/product-recommendation-rules",
        headers=admin_headers,
        json={**rule_payload, "registry_entry_id": registry["id"]},
    )
    assert created_rule.status_code == 201
    assert created_rule.json()["registration_number"] == "VAAD-TEST-001"
    assert created_rule.json()["registration_country"] == "LV"

    snapshot["records"][0]["status"] = "revoked"
    revoked = client.post(
        "/api/v1/admin/regulated-products/import",
        headers=admin_headers,
        json=snapshot,
    )
    assert revoked.status_code == 200
    assert revoked.json()["invalidated_rules"] == 1
    rules = client.get(
        f"/api/v1/admin/product-recommendation-rules?product_id={product['id']}",
        headers=admin_headers,
    ).json()
    assert rules[0]["active"] is False
    assert rules[0]["expert_verified"] is False


def test_partner_cabinet_isolated_products_leads_and_roles(client):
    admin_headers = register(client, "cabinet-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("cabinet-admin@example.com", True)
    owner_headers = register(client, "owner@partner.example")
    viewer_headers = register(client, "viewer@partner.example")
    customer_headers = register(client, "customer@example.com")

    first_partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "First Garden Centre",
            "website_url": "https://first.example.com",
        },
    ).json()
    second_partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "Second Garden Centre",
            "website_url": "https://second.example.com",
        },
    ).json()
    owner_member = client.post(
        f"/api/v1/admin/partners/{first_partner['id']}/members",
        headers=admin_headers,
        json={"email": "owner@partner.example", "role": "owner"},
    )
    assert owner_member.status_code == 201
    assert (
        client.post(
            f"/api/v1/admin/partners/{second_partner['id']}/members",
            headers=admin_headers,
            json={"email": "viewer@partner.example", "role": "viewer"},
        ).status_code
        == 201
    )
    assert (
        client.post(
            f"/api/v1/admin/partners/{second_partner['id']}/members",
            headers=admin_headers,
            json={"email": "owner@partner.example", "role": "editor"},
        ).status_code
        == 409
    )

    account = client.get("/api/v1/partner/me", headers=owner_headers)
    assert account.status_code == 200
    assert account.json()["partner_id"] == first_partner["id"]
    assert account.json()["can_manage_products"] is True
    customer_account = client.get("/api/v1/partner/me", headers=customer_headers)
    assert customer_account.status_code == 200
    assert customer_account.json() is None

    product = client.post(
        "/api/v1/partner/products",
        headers=owner_headers,
        json={
            "name": "Лейка садовая",
            "sku": "CAN-001",
            "category": "irrigation",
            "description": "Инструмент для ручного полива.",
            "product_url": "https://first.example.com/watering-can",
            "image_url": "https://first.example.com/images/watering-can.jpg",
            "price_cents": 2499,
            "currency": "eur",
            "in_stock": True,
            "regions": ["Latvia"],
        },
    )
    assert product.status_code == 201
    assert product.json()["partner_id"] == first_partner["id"]
    assert product.json()["sku"] == "CAN-001"
    assert product.json()["price_cents"] == 2499
    assert (
        client.post(
            "/api/v1/partner/products",
            headers=owner_headers,
            json={
                "name": "Duplicate SKU",
                "sku": "can-001",
                "category": "other",
                "product_url": "https://first.example.com/duplicate",
                "regions": [],
            },
        ).status_code
        == 409
    )

    csv_body = (
        b"sku,name,category,description,product_url,image_url,price_cents,currency,in_stock,regions\n"
        b"SOIL-001,Soil meter,tools,Manual soil meter,https://first.example.com/soil-meter,"
        b"https://first.example.com/images/soil-meter.jpg,3199,EUR,true,Latvia|Estonia\n"
    )
    imported = client.post(
        "/api/v1/partner/products/import/csv",
        headers=owner_headers,
        files={"file": ("products.csv", csv_body, "text/csv")},
    )
    assert imported.status_code == 200
    assert imported.json() == {"created": 1, "updated": 0}
    updated_csv = csv_body.replace(b"3199", b"2999").replace(b"true", b"false")
    imported_again = client.post(
        "/api/v1/partner/products/import/csv",
        headers=owner_headers,
        files={"file": ("products.csv", updated_csv, "text/csv")},
    )
    assert imported_again.json() == {"created": 0, "updated": 1}
    oversized_csv = (
        b"sku,name,category,product_url\n"
        + b"A" * 200_000
        + b",Oversized,tools,https://first.example.com/oversized\n"
    )
    oversized = client.post(
        "/api/v1/partner/products/import/csv",
        headers=owner_headers,
        files={"file": ("products.csv", oversized_csv, "text/csv")},
    )
    assert oversized.status_code == 422
    assert oversized.json()["detail"]["message"] == (
        "CSV contains an oversized or malformed field"
    )
    imported_product = next(
        item
        for item in client.get("/api/v1/partner/products", headers=owner_headers).json()
        if item["sku"] == "SOIL-001"
    )
    assert imported_product["price_cents"] == 2999
    assert imported_product["in_stock"] is False
    assert (
        client.post(
            "/api/v1/partner/products",
            headers=viewer_headers,
            json={
                "name": "Запрещённый товар",
                "category": "other",
                "product_url": "https://second.example.com/item",
                "regions": [],
            },
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"/api/v1/partner/products/{product.json()['id']}",
            headers=viewer_headers,
            json={"active": False},
        ).status_code
        == 403
    )

    submitted = client.post(
        f"/api/v1/partner/products/{product.json()['id']}/submit", headers=owner_headers
    )
    assert submitted.status_code == 200
    assert submitted.json()["moderation_status"] == "pending"
    approved = client.patch(
        f"/api/v1/admin/products/{product.json()['id']}/moderation",
        headers=admin_headers,
        json={"status": "approved", "note": "Проверено"},
    )
    assert approved.status_code == 200

    lead = client.post(
        f"/api/v1/products/{product.json()['id']}/lead",
        headers=customer_headers,
        json={},
    )
    assert lead.status_code == 201
    owner_leads = client.get("/api/v1/partner/leads", headers=owner_headers)
    assert owner_leads.status_code == 200
    assert owner_leads.json()[0]["product_name"] == "Лейка садовая"
    assert "user_id" not in owner_leads.json()[0]
    assert "diagnosis_id" not in owner_leads.json()[0]
    assert client.get("/api/v1/partner/leads", headers=viewer_headers).json() == []
    overview = client.get("/api/v1/partner/overview", headers=owner_headers).json()
    assert overview["total_products"] == 2
    assert overview["total_leads"] == 1

    members = client.get("/api/v1/admin/partner-members", headers=admin_headers)
    assert members.status_code == 200
    assert {item["email"] for item in members.json()} == {
        "owner@partner.example",
        "viewer@partner.example",
    }
    assert (
        client.delete(
            f"/api/v1/admin/partner-members/{owner_member.json()['id']}",
            headers=admin_headers,
        ).status_code
        == 204
    )
    deactivated_account = client.get("/api/v1/partner/me", headers=owner_headers)
    assert deactivated_account.status_code == 200
    assert deactivated_account.json() is None


def test_partner_conversion_review_attribution_and_invoice(client):
    admin_headers = register(client, "b2b-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("b2b-admin@example.com", True)
    owner_headers = register(client, "billing-owner@partner.example")
    customer_headers = register(client, "billing-customer@example.com")

    partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "Billable Garden Centre",
            "website_url": "https://billable.example.com",
            "billing_plan": "business",
            "monthly_fee_cents": 1000,
            "confirmed_lead_price_cents": 250,
            "billing_currency": "eur",
            "legal_name": "Billable Garden Centre SIA",
            "registration_number": "40200000000",
            "vat_number": "LV40200000000",
            "billing_address": "Dārza iela 1, Riga, LV-1000",
            "billing_email": "accounts@billable.example.com",
        },
    ).json()
    assert (
        client.post(
            f"/api/v1/admin/partners/{partner['id']}/members",
            headers=admin_headers,
            json={"email": "billing-owner@partner.example", "role": "owner"},
        ).status_code
        == 201
    )
    product = client.post(
        "/api/v1/admin/products",
        headers=admin_headers,
        json={
            "partner_id": partner["id"],
            "name": "Billable tool",
            "category": "tools",
            "product_url": "https://billable.example.com/tool",
            "regions": ["Latvia"],
        },
    ).json()
    lead = client.post(
        f"/api/v1/products/{product['id']}/lead",
        headers=customer_headers,
        json={},
    ).json()
    assert lead["status"] == "clicked"

    claimed = client.post(
        f"/api/v1/partner/leads/{lead['id']}/conversion",
        headers=owner_headers,
        json={"partner_reference": "sale-2026-0001", "conversion_value_cents": 3499},
    )
    assert claimed.status_code == 200
    assert claimed.json()["status"] == "conversion_claimed"
    assert "user_id" not in claimed.json()
    assert (
        client.post(
            f"/api/v1/partner/leads/{lead['id']}/conversion",
            headers=owner_headers,
            json={
                "partner_reference": "sale-2026-0002",
                "conversion_value_cents": 3499,
            },
        ).status_code
        == 409
    )

    reviewed = client.patch(
        f"/api/v1/admin/leads/{lead['id']}/conversion",
        headers=admin_headers,
        json={"approved": True, "note": "Сверено с отчётом партнёра"},
    )
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "confirmed"
    assert reviewed.json()["billable_amount_cents"] == 250
    overview = client.get("/api/v1/partner/overview", headers=owner_headers).json()
    assert overview["pending_conversion_claims"] == 0
    assert overview["confirmed_conversions"] == 1
    assert overview["uninvoiced_amount_cents"] == 250

    today = datetime.now(UTC).date()
    period_start = today.replace(day=1)
    period_end = (
        period_start.replace(year=period_start.year + 1, month=1)
        if period_start.month == 12
        else period_start.replace(month=period_start.month + 1)
    )
    invoice_payload = {
        "period_start": period_start.isoformat(),
        "period_end": period_end.isoformat(),
        "due_days": 14,
    }
    invoice = client.post(
        f"/api/v1/admin/partners/{partner['id']}/invoices",
        headers=admin_headers,
        json=invoice_payload,
    )
    assert invoice.status_code == 201
    assert invoice.json()["confirmed_leads_count"] == 1
    assert invoice.json()["lead_fees_cents"] == 250
    assert invoice.json()["total_cents"] == 1250
    assert invoice.json()["invoice_number"].startswith(f"AG-{today.year}-")
    assert invoice.json()["document_version"] == "b2b-invoice-v1"
    assert len(invoice.json()["snapshot_sha256"]) == 64
    assert len(invoice.json()["pdf_sha256"]) == 64
    assert invoice.json()["pdf_content_type"] == "application/pdf"
    assert invoice.json()["pdf_size_bytes"] > 1_000
    assert invoice.json()["pdf_generator_version"].startswith("reportlab-")
    issued_pdf = client.get(
        f"/api/v1/partner/invoices/{invoice.json()['id']}/pdf",
        headers=owner_headers,
    )
    assert issued_pdf.status_code == 200
    assert issued_pdf.content.startswith(b"%PDF-")
    assert sha256(issued_pdf.content).hexdigest() == invoice.json()["pdf_sha256"]
    assert issued_pdf.headers["content-type"] == "application/pdf"
    assert invoice.json()["invoice_number"] in issued_pdf.headers["content-disposition"]
    with SessionLocal() as db:
        stored_invoice = db.get(PartnerInvoice, invoice.json()["id"])
        stored_pdf_path = Path(stored_invoice.pdf_file_path)
        assert stored_invoice.pdf_storage_backend == "local"
        assert stored_pdf_path.read_bytes() == issued_pdf.content
        stored_pdf_path.write_bytes(b"tampered")
    assert (
        client.get(
            f"/api/v1/partner/invoices/{invoice.json()['id']}/pdf",
            headers=owner_headers,
        ).status_code
        == 409
    )
    stored_pdf_path.write_bytes(issued_pdf.content)
    assert (
        client.get(
            f"/api/v1/partner/invoices/{invoice.json()['id']}/pdf",
            headers=customer_headers,
        ).status_code
        == 403
    )
    admin_pdf = client.get(
        f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/pdf",
        headers=admin_headers,
    )
    assert admin_pdf.content == issued_pdf.content
    assert (
        client.patch(
            f"/api/v1/admin/partners/{partner['id']}",
            headers=admin_headers,
            json={"billing_address": "Changed address 99, Riga"},
        ).status_code
        == 200
    )
    assert (
        client.get(
            f"/api/v1/partner/invoices/{invoice.json()['id']}/pdf",
            headers=owner_headers,
        ).content
        == issued_pdf.content
    )
    sent = client.post(
        f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/send",
        headers=admin_headers,
        json={},
    )
    assert sent.status_code == 201
    assert sent.json()["status"] == "logged"
    assert sent.json()["recipient"] == "accounts@billable.example.com"
    assert (
        client.post(
            f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/send",
            headers=admin_headers,
            json={},
        ).status_code
        == 409
    )
    resent = client.post(
        f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/send",
        headers=admin_headers,
        json={"confirm_resend": True},
    )
    assert resent.status_code == 201
    assert resent.json()["attempt_number"] == 2
    deliveries = client.get(
        f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/deliveries",
        headers=admin_headers,
    )
    assert [item["attempt_number"] for item in deliveries.json()] == [2, 1]
    assert (
        client.get(
            f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/deliveries",
            headers=owner_headers,
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"/api/v1/admin/partners/{partner['id']}/invoices",
            headers=admin_headers,
            json=invoice_payload,
        ).status_code
        == 409
    )
    assert (
        client.patch(
            f"/api/v1/admin/partner-invoices/{invoice.json()['id']}",
            headers=admin_headers,
            json={"status": "void"},
        ).status_code
        == 422
    )
    cancellation_reason = "Reissued after a billing address correction"
    voided = client.patch(
        f"/api/v1/admin/partner-invoices/{invoice.json()['id']}",
        headers=admin_headers,
        json={"status": "void", "reason": cancellation_reason},
    )
    assert voided.status_code == 200
    assert voided.json()["status"] == "void"
    assert voided.json()["cancellation_number"].startswith(f"AG-CN-{today.year}-")
    assert voided.json()["cancellation_reason"] == cancellation_reason
    assert (
        voided.json()["cancellation_document_version"] == "b2b-invoice-cancellation-v1"
    )
    assert len(voided.json()["cancellation_snapshot_sha256"]) == 64
    assert len(voided.json()["cancellation_pdf_sha256"]) == 64
    assert voided.json()["cancellation_content_type"] == "application/pdf"
    assert voided.json()["cancellation_size_bytes"] > 1_000
    assert voided.json()["cancellation_generator_version"].startswith("reportlab-")
    cancellation_pdf = client.get(
        f"/api/v1/partner/invoices/{invoice.json()['id']}/cancellation.pdf",
        headers=owner_headers,
    )
    assert cancellation_pdf.status_code == 200
    assert cancellation_pdf.content.startswith(b"%PDF-")
    assert (
        sha256(cancellation_pdf.content).hexdigest()
        == voided.json()["cancellation_pdf_sha256"]
    )
    assert (
        voided.json()["cancellation_number"]
        in cancellation_pdf.headers["content-disposition"]
    )
    assert (
        client.get(
            f"/api/v1/partner/invoices/{invoice.json()['id']}/cancellation.pdf",
            headers=customer_headers,
        ).status_code
        == 403
    )
    admin_cancellation_pdf = client.get(
        f"/api/v1/admin/partner-invoices/{invoice.json()['id']}/cancellation.pdf",
        headers=admin_headers,
    )
    assert admin_cancellation_pdf.content == cancellation_pdf.content
    assert (
        client.get(
            f"/api/v1/partner/invoices/{invoice.json()['id']}/pdf",
            headers=owner_headers,
        ).content
        == issued_pdf.content
    )
    reissued = client.post(
        f"/api/v1/admin/partners/{partner['id']}/invoices",
        headers=admin_headers,
        json=invoice_payload,
    )
    assert reissued.status_code == 201
    assert reissued.json()["id"] != invoice.json()["id"]
    assert reissued.json()["invoice_number"] != invoice.json()["invoice_number"]
    reissued_pdf = client.get(
        f"/api/v1/partner/invoices/{reissued.json()['id']}/pdf",
        headers=owner_headers,
    ).content
    assert (
        client.get(
            f"/api/v1/partner/invoices/{invoice.json()['id']}/cancellation.pdf",
            headers=owner_headers,
        ).content
        == cancellation_pdf.content
    )
    partner_invoices = client.get("/api/v1/partner/invoices", headers=owner_headers)
    assert partner_invoices.status_code == 200
    assert {item["status"] for item in partner_invoices.json()} == {"issued", "void"}
    assert (
        client.patch(
            f"/api/v1/admin/partner-invoices/{reissued.json()['id']}",
            headers=owner_headers,
            json={"status": "paid"},
        ).status_code
        == 403
    )
    payment_payload = {
        "source": "test-bank",
        "records": [
            {
                "external_id": "bank-txn-0001",
                "booking_date": today.isoformat(),
                "amount_cents": 1250,
                "currency": "EUR",
                "reference": f"Payment {reissued.json()['invoice_number']}",
            },
            {
                "external_id": "bank-txn-0002",
                "booking_date": today.isoformat(),
                "amount_cents": 1200,
                "currency": "EUR",
                "reference": f"Wrong amount {reissued.json()['invoice_number']}",
            },
        ],
    }
    reconciled = client.post(
        "/api/v1/admin/partner-payments/import",
        headers=admin_headers,
        json=payment_payload,
    )
    assert reconciled.status_code == 200
    assert reconciled.json() == {
        "imported": 2,
        "matched": 1,
        "unmatched": 1,
        "duplicates": 0,
    }
    duplicate_import = client.post(
        "/api/v1/admin/partner-payments/import",
        headers=admin_headers,
        json=payment_payload,
    )
    assert duplicate_import.json() == {
        "imported": 0,
        "matched": 0,
        "unmatched": 0,
        "duplicates": 2,
    }
    payments = client.get("/api/v1/admin/partner-payments", headers=admin_headers)
    assert {item["status"] for item in payments.json()} == {"matched", "unmatched"}
    unmatched_payment = next(
        item for item in payments.json() if item["status"] == "unmatched"
    )
    rejected_payment = client.patch(
        f"/api/v1/admin/partner-payments/{unmatched_payment['id']}",
        headers=admin_headers,
        json={"action": "reject", "note": "Wrong amount confirmed by bank"},
    )
    assert rejected_payment.status_code == 200
    assert rejected_payment.json()["status"] == "rejected"
    assert rejected_payment.json()["resolved_at"] is not None
    assert (
        client.patch(
            f"/api/v1/admin/partner-payments/{unmatched_payment['id']}",
            headers=admin_headers,
            json={"action": "reject"},
        ).status_code
        == 409
    )
    assert (
        client.get(
            "/api/v1/admin/partner-payments",
            headers=owner_headers,
        ).status_code
        == 403
    )
    paid = client.get("/api/v1/partner/invoices", headers=owner_headers).json()
    assert (
        next(item for item in paid if item["id"] == reissued.json()["id"])["status"]
        == "paid"
    )

    manual_partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "Manual Match Centre",
            "website_url": "https://manual.example.com",
            "billing_plan": "business",
            "monthly_fee_cents": 700,
            "billing_currency": "eur",
            "legal_name": "Manual Match Centre SIA",
            "registration_number": "40200000001",
            "billing_address": "Bankas iela 2, Riga",
            "billing_email": "billing@manual.example.com",
        },
    ).json()
    manual_invoice = client.post(
        f"/api/v1/admin/partners/{manual_partner['id']}/invoices",
        headers=admin_headers,
        json=invoice_payload,
    ).json()
    manual_import = client.post(
        "/api/v1/admin/partner-payments/import",
        headers=admin_headers,
        json={
            "source": "test-bank",
            "records": [
                {
                    "external_id": "bank-txn-manual",
                    "booking_date": today.isoformat(),
                    "amount_cents": 700,
                    "currency": "EUR",
                    "reference": "Manual transfer",
                }
            ],
        },
    )
    assert manual_import.json()["unmatched"] == 1
    manual_payment = client.get(
        "/api/v1/admin/partner-payments?status=unmatched",
        headers=admin_headers,
    ).json()[0]
    resolved = client.patch(
        f"/api/v1/admin/partner-payments/{manual_payment['id']}",
        headers=admin_headers,
        json={
            "action": "match",
            "invoice_id": manual_invoice["id"],
            "note": "Verified",
        },
    )
    assert resolved.status_code == 200
    assert resolved.json()["status"] == "matched"
    assert resolved.json()["invoice_id"] == manual_invoice["id"]
    assert (
        client.get(
            f"/api/v1/partner/invoices/{reissued.json()['id']}/pdf",
            headers=owner_headers,
        ).content
        == reissued_pdf
    )
    admin_overview = client.get("/api/v1/admin/overview", headers=admin_headers).json()
    assert admin_overview["confirmed_partner_conversions"] == 1
    assert admin_overview["partner_invoices_issued_cents"] == 1950
    with SessionLocal() as db:
        stored_invoice = db.get(PartnerInvoice, reissued.json()["id"])
        stored_invoice.issuer_snapshot = {
            **stored_invoice.issuer_snapshot,
            "name": "Tampered",
        }
        db.commit()
        assert (
            client.get(
                f"/api/v1/partner/invoices/{reissued.json()['id']}/pdf",
                headers=owner_headers,
            ).status_code
            == 409
        )


def test_manual_invoice_payment_creates_audited_payment_record(client):
    admin_headers = register(client, "manual-payment-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("manual-payment-admin@example.com", True)
    partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "Manual Payment Partner",
            "website_url": "https://manual-payment.example.com",
            "billing_plan": "business",
            "monthly_fee_cents": 900,
            "billing_currency": "eur",
            "legal_name": "Manual Payment Partner SIA",
            "registration_number": "40209999999",
            "billing_address": "Maksājumu iela 1, Riga",
            "billing_email": "billing@manual-payment.example.com",
        },
    ).json()
    today = datetime.now(UTC).date()
    period_start = today.replace(day=1)
    period_end = (
        period_start.replace(year=period_start.year + 1, month=1)
        if period_start.month == 12
        else period_start.replace(month=period_start.month + 1)
    )
    invoice = client.post(
        f"/api/v1/admin/partners/{partner['id']}/invoices",
        headers=admin_headers,
        json={
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "due_days": 14,
        },
    ).json()

    direct_paid = client.patch(
        f"/api/v1/admin/partner-invoices/{invoice['id']}",
        headers=admin_headers,
        json={"status": "paid"},
    )
    assert direct_paid.status_code == 409
    payment = client.post(
        f"/api/v1/admin/partner-invoices/{invoice['id']}/manual-payment",
        headers=admin_headers,
        json={
            "external_id": "MANUAL-TRANSFER-001",
            "payment_method": "bank_transfer",
            "booking_date": today.isoformat(),
            "note": "Bank statement verified by administrator",
        },
    )
    assert payment.status_code == 201
    assert payment.json()["invoice_id"] == invoice["id"]
    assert payment.json()["source"] == "manual:bank_transfer"
    assert payment.json()["external_id"] == "MANUAL-TRANSFER-001"
    assert payment.json()["amount_cents"] == 900
    assert payment.json()["status"] == "matched"
    invoices = client.get(
        "/api/v1/admin/partner-invoices", headers=admin_headers
    ).json()
    assert (
        next(item for item in invoices if item["id"] == invoice["id"])["status"]
        == "paid"
    )
    assert (
        client.post(
            f"/api/v1/admin/partner-invoices/{invoice['id']}/manual-payment",
            headers=admin_headers,
            json={
                "external_id": "MANUAL-TRANSFER-002",
                "payment_method": "bank_transfer",
                "booking_date": today.isoformat(),
                "note": "Duplicate attempt",
            },
        ).status_code
        == 409
    )


def test_b2b_invoice_endpoints_are_hidden_when_feature_is_disabled(
    client, monkeypatch
):
    admin_headers = register(client, "disabled-invoicing-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("disabled-invoicing-admin@example.com", True)
    monkeypatch.setattr(settings, "b2b_invoicing_enabled", False)

    response = client.get("/api/v1/admin/partner-invoices", headers=admin_headers)

    assert response.status_code == 404
    assert response.json()["detail"] == "Модуль B2B-счетов отключён"


def test_signed_partner_redirect_and_authenticated_postback(client):
    admin_headers = register(client, "postback-admin@example.com")
    from app.admin_cli import set_admin

    assert set_admin("postback-admin@example.com", True)
    owner_headers = register(client, "postback-owner@example.com")
    customer_headers = register(client, "postback-customer@example.com")
    partner = client.post(
        "/api/v1/admin/partners",
        headers=admin_headers,
        json={
            "name": "Attributed Partner",
            "website_url": "https://partner.example.com",
        },
    ).json()
    assert (
        client.post(
            f"/api/v1/admin/partners/{partner['id']}/members",
            headers=admin_headers,
            json={"email": "postback-owner@example.com", "role": "owner"},
        ).status_code
        == 201
    )
    product = client.post(
        "/api/v1/admin/products",
        headers=admin_headers,
        json={
            "partner_id": partner["id"],
            "name": "Attributed tool",
            "category": "tools",
            "product_url": "https://partner.example.com/item?campaign=summer",
            "regions": [],
        },
    ).json()
    first = client.post(
        f"/api/v1/products/{product['id']}/lead",
        headers=customer_headers,
        json={},
    ).json()
    repeated = client.post(
        f"/api/v1/products/{product['id']}/lead",
        headers=customer_headers,
        json={},
    ).json()
    assert repeated["id"] == first["id"]
    assert first["redirect_url"].startswith("/product/go/")
    signed_click_id = first["redirect_url"].rsplit("/", 1)[1]
    key = client.post("/api/v1/partner/postback-key", headers=owner_headers).json()[
        "api_key"
    ]
    postback_payload = {
        "signed_click_id": signed_click_id,
        "partner_reference": "order-attributed-001",
        "conversion_value_cents": 4599,
    }
    assert (
        client.post(
            "/api/v1/partner/conversions/postback",
            headers={"X-Partner-Key": key},
            json=postback_payload,
        ).status_code
        == 409
    )

    redirect = client.get(first["redirect_url"], follow_redirects=False)
    assert redirect.status_code == 302
    assert redirect.headers["location"].startswith(
        "https://partner.example.com/item?campaign=summer&ai_garden_click_id="
    )
    assert (
        client.get("/product/go/not-a-token", follow_redirects=False).status_code == 404
    )
    assert (
        client.post(
            "/api/v1/partner/conversions/postback",
            headers={"X-Partner-Key": "wrong-key"},
            json=postback_payload,
        ).status_code
        == 401
    )
    converted = client.post(
        "/api/v1/partner/conversions/postback",
        headers={"X-Partner-Key": key},
        json=postback_payload,
    )
    assert converted.status_code == 200
    assert converted.json()["status"] == "conversion_claimed"


def test_requires_authentication(client):
    assert client.get("/api/v1/gardens").status_code == 401
    assert client.get("/api/v1/plants").status_code == 401


def test_safe_diagnosis_flow(client):
    plant, headers = create_plant(client)
    uploaded = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    )
    assert uploaded.status_code == 201
    photo_id = uploaded.json()[0]["id"]
    response = client.post(
        "/api/v1/diagnoses",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "Нижние листья начали желтеть неделю назад",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    )
    response = resolve_diagnosis_job(client, headers, response)
    result = response.json()["result"]
    assert response.json()["photo_ids"] == [photo_id]
    assert len(result["possible_causes"]) <= 3
    assert result["analysis_status"] == "needs_confirmation"
    assert result["input_status"] == "valid"
    assert "не подтверждённый диагноз" in result["disclaimer"]
    assert len(result["questions"]) <= 7
    assert response.json()["demo_mode"] is True
    assert response.json()["model_name"] == "mock-rule-based"
    with SessionLocal() as db:
        audit = db.scalar(
            select(AIRequestLog).where(
                AIRequestLog.diagnosis_id == response.json()["id"]
            )
        )
        assert audit is not None
        assert audit.success is True
        assert audit.request_id.startswith("mock_")


def test_background_diagnosis_job_completes_and_is_idempotent(client):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    response = client.post(
        "/api/v1/diagnoses/async",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "Нижние листья начали желтеть неделю назад",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    )
    assert response.status_code == 202
    job = response.json()
    assert job["status"] == "succeeded"
    assert job["attempts"] == 1
    assert job["diagnosis_id"] is not None
    assert (
        client.get(
            f"/api/v1/diagnoses/{job['diagnosis_id']}", headers=headers
        ).status_code
        == 200
    )
    assert (
        client.get(f"/api/v1/diagnosis-jobs/{job['id']}", headers=headers).status_code
        == 200
    )

    from app.services.diagnosis_job_service import process_diagnosis_job

    assert (
        process_diagnosis_job(job["id"], gateway=main_module.ai_gateway)
        == job["diagnosis_id"]
    )
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Diagnosis)))) == 1

    stranger_headers = register(client, "job-stranger@example.com")
    assert (
        client.get(
            f"/api/v1/diagnosis-jobs/{job['id']}", headers=stranger_headers
        ).status_code
        == 404
    )


def test_background_job_applies_and_audits_deterministic_safety_policy(
    client, monkeypatch
):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    monkeypatch.setattr(
        main_module, "dispatch_diagnosis_job", lambda *_args, **_kwargs: "queued"
    )
    job = client.post(
        "/api/v1/diagnoses/async",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "Нижние листья начали желтеть неделю назад",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    ).json()

    from app.ai.base import AIAnalysis
    from app.models import AISafetyAdjustment
    from app.services.diagnosis_job_service import process_diagnosis_job

    class UnsafeGateway:
        model_name = main_module.ai_gateway.model_name
        prompt_version = main_module.ai_gateway.prompt_version

        def analyze(self, *args, **kwargs):
            analysis = main_module.ai_gateway.analyze(*args, **kwargs)
            result = analysis.result.model_copy(
                update={
                    "safe_actions": [
                        "Изолируйте растение и наблюдайте за изменениями.",
                        "Примените фунгицид 25 мл и смешайте с инсектицидом.",
                    ],
                }
            )
            return AIAnalysis(result=result, usage=analysis.usage)

    diagnosis_id = process_diagnosis_job(job["id"], gateway=UnsafeGateway())
    diagnosis = client.get(f"/api/v1/diagnoses/{diagnosis_id}", headers=headers).json()
    assert diagnosis["result"]["expert_required"] is True
    assert diagnosis["result"]["analysis_status"] == "expert_required"
    assert all("25 мл" not in item for item in diagnosis["result"]["safe_actions"])
    with SessionLocal() as db:
        audit = db.scalar(
            select(AISafetyAdjustment).where(
                AISafetyAdjustment.diagnosis_id == diagnosis_id,
            )
        )
        assert audit is not None
        assert audit.removed_action_count == 1
        assert "unverified_dosage" in audit.reason_codes
        assert "chemical_mixing" in audit.reason_codes


def test_diagnosis_job_lease_prevents_two_paid_ai_calls(client, monkeypatch):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    monkeypatch.setattr(
        main_module, "dispatch_diagnosis_job", lambda *_args, **_kwargs: "queued"
    )
    job = client.post(
        "/api/v1/diagnoses/async",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "Нижние листья начали желтеть неделю назад",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    ).json()

    entered = Event()
    release = Event()
    count_lock = Lock()

    class BlockingGateway:
        model_name = main_module.ai_gateway.model_name
        prompt_version = main_module.ai_gateway.prompt_version

        def __init__(self):
            self.calls = 0

        def analyze(self, *args, **kwargs):
            with count_lock:
                self.calls += 1
            entered.set()
            assert release.wait(5)
            return main_module.ai_gateway.analyze(*args, **kwargs)

    from app.services.diagnosis_job_service import process_diagnosis_job

    gateway = BlockingGateway()
    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(process_diagnosis_job, job["id"], gateway=gateway)
        assert entered.wait(5)
        second = executor.submit(process_diagnosis_job, job["id"], gateway=gateway)
        second_result = second.result(timeout=5)
        release.set()
        diagnosis_id = first.result(timeout=5)
    assert second_result is None
    assert diagnosis_id is not None
    assert gateway.calls == 1
    with SessionLocal() as db:
        assert len(list(db.scalars(select(Diagnosis)))) == 1


def test_queued_jobs_count_toward_monthly_quota(client, monkeypatch):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    monkeypatch.setattr(main_module.settings, "free_diagnoses_per_month", 1)
    monkeypatch.setattr(
        main_module, "dispatch_diagnosis_job", lambda *_args, **_kwargs: "queued"
    )
    payload = {
        "plant_id": plant["id"],
        "symptoms": "На листьях появились заметные изменения",
        "damaged_part": "leaf",
        "photo_ids": [photo_id],
    }
    first = client.post("/api/v1/diagnoses/async", headers=headers, json=payload)
    assert first.status_code == 202
    assert first.json()["status"] == "queued"
    assert (
        client.post(
            "/api/v1/diagnoses/async", headers=headers, json=payload
        ).status_code
        == 429
    )


def test_background_job_retries_transient_provider_errors(client, monkeypatch):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    monkeypatch.setattr(
        main_module, "dispatch_diagnosis_job", lambda *_args, **_kwargs: "queued"
    )
    job_id = client.post(
        "/api/v1/diagnoses/async",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "На листьях появились заметные изменения",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    ).json()["id"]

    class UnavailableGateway:
        model_name = "unavailable-model"
        prompt_version = "retry-v1"

        def __init__(self):
            self.calls = 0

        def analyze(self, *_args, **_kwargs):
            self.calls += 1
            raise AIProviderError(
                "unavailable",
                http_status=503,
                error_type="provider_unavailable",
                request_id=f"retry_request_{self.calls}",
                response_ms=100,
            )

    from app.services.diagnosis_job_service import (
        RetryableDiagnosisJob,
        process_diagnosis_job,
    )

    gateway = UnavailableGateway()
    for _ in range(2):
        try:
            process_diagnosis_job(job_id, gateway=gateway, retry_transient=True)
        except RetryableDiagnosisJob:
            pass
        else:
            raise AssertionError("Временная ошибка не была передана на повтор")
    assert process_diagnosis_job(job_id, gateway=gateway, retry_transient=True) is None
    with SessionLocal() as db:
        job = db.get(DiagnosisJob, job_id)
        assert job.status == "failed"
        assert job.attempts == 3
        assert job.error_message == "AI-анализ временно недоступен"


def test_rejects_foreign_photo(client):
    first, headers = create_plant(client)
    second_garden = client.post(
        "/api/v1/gardens", headers=headers, json={"name": "Второй сад"}
    ).json()
    second = client.post(
        "/api/v1/plants",
        headers=headers,
        json={"garden_id": second_garden["id"], "name": "Роза"},
    ).json()
    uploaded = client.post(
        f"/api/v1/plants/{first['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.png", image_bytes("PNG"), "image/png"))],
    ).json()
    response = client.post(
        "/api/v1/diagnoses",
        headers=headers,
        json={
            "plant_id": second["id"],
            "symptoms": "На листьях появились заметные пятна",
            "damaged_part": "leaf",
            "photo_ids": [uploaded[0]["id"]],
        },
    )
    assert response.status_code == 400


def test_diagnosis_keeps_multiple_photo_links(client):
    plant, headers = create_plant(client)
    uploaded = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[
            ("files", ("top.jpg", image_bytes("JPEG"), "image/jpeg")),
            ("files", ("bottom.png", image_bytes("PNG"), "image/png")),
        ],
    )
    assert uploaded.status_code == 201
    photo_ids = [item["id"] for item in uploaded.json()]
    created = client.post(
        "/api/v1/diagnoses",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "На нескольких листьях появились тёмные пятна",
            "damaged_part": "leaf",
            "photo_ids": photo_ids,
        },
    )
    created = resolve_diagnosis_job(client, headers, created)
    diagnosis_id = created.json()["id"]
    fetched = client.get(f"/api/v1/diagnoses/{diagnosis_id}", headers=headers)
    assert fetched.status_code == 200
    assert set(fetched.json()["photo_ids"]) == set(photo_ids)


def test_users_cannot_access_each_others_gardens(client):
    first_headers = register(client, "first@example.com")
    garden = client.post(
        "/api/v1/gardens", headers=first_headers, json={"name": "Секретный сад"}
    ).json()
    second_headers = register(client, "second@example.com")
    response = client.post(
        "/api/v1/plants",
        headers=second_headers,
        json={"garden_id": garden["id"], "name": "Чужое растение"},
    )
    assert response.status_code == 404


def test_rejects_empty_and_fake_images(client):
    plant, headers = create_plant(client)
    for name, content, content_type in [
        ("fake.jpg", b"not-an-image", "image/jpeg"),
        ("empty.png", b"", "image/png"),
    ]:
        response = client.post(
            f"/api/v1/plants/{plant['id']}/photos",
            headers=headers,
            files=[("files", (name, content, content_type))],
        )
        assert response.status_code == 422


def test_photo_upload_rate_limit_and_storage_quota(client, monkeypatch):
    plant, headers = create_plant(client)
    path = f"/api/v1/plants/{plant['id']}/photos"
    monkeypatch.setattr(settings, "upload_rate_limit_attempts", 1)
    assert (
        client.post(
            path,
            headers=headers,
            files=[("files", ("first.jpg", image_bytes(), "image/jpeg"))],
        ).status_code
        == 201
    )
    assert (
        client.post(
            path,
            headers=headers,
            files=[("files", ("second.jpg", image_bytes(), "image/jpeg"))],
        ).status_code
        == 429
    )

    from app.services.rate_limit_service import reset_local_rate_limits

    reset_local_rate_limits()
    monkeypatch.setattr(settings, "upload_rate_limit_attempts", 20)
    monkeypatch.setattr(settings, "user_storage_quota_mb", 0)
    assert (
        client.post(
            path,
            headers=headers,
            files=[("files", ("over-quota.jpg", image_bytes(), "image/jpeg"))],
        ).status_code
        == 413
    )


def test_rejects_mime_mismatch_and_tiny_image(client):
    plant, headers = create_plant(client)
    mismatch = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("wrong.jpg", image_bytes("PNG"), "image/jpeg"))],
    )
    assert mismatch.status_code == 422
    tiny = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("tiny.png", image_bytes("PNG", (8, 8)), "image/png"))],
    )
    assert tiny.status_code == 422


def test_sanitizes_image_metadata(client):
    plant, headers = create_plant(client)
    exif = Image.Exif()
    exif[0x010E] = "private metadata"
    response = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[
            ("files", ("with-exif.jpg", image_bytes("JPEG", exif=exif), "image/jpeg"))
        ],
    )
    assert response.status_code == 201
    saved_files = list((settings.upload_dir / str(plant["id"])).iterdir())
    with Image.open(saved_files[0]) as saved:
        assert not saved.getexif()


def test_duplicate_garden_and_registration_return_conflict(client):
    headers = register(client)
    payload = {"name": "Одинаковый сад"}
    assert (
        client.post("/api/v1/gardens", headers=headers, json=payload).status_code == 201
    )
    assert (
        client.post("/api/v1/gardens", headers=headers, json=payload).status_code == 409
    )
    duplicate = client.post(
        "/api/v1/auth/register",
        json={
            "email": "USER@example.com",
            "name": "Другой",
            "password": "strong-password",
        },
    )
    assert duplicate.status_code == 409


def test_diagnosis_history_answers_reanalysis_and_feedback(client):
    plant, headers = create_plant(client)
    uploaded = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()
    created = client.post(
        "/api/v1/diagnoses",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "На листьях появились заметные тёмные пятна",
            "damaged_part": "leaf",
            "photo_ids": [uploaded[0]["id"]],
        },
    )
    created = resolve_diagnosis_job(client, headers, created)
    diagnosis_id = created.json()["id"]
    questions = client.get(
        f"/api/v1/diagnoses/{diagnosis_id}/questions", headers=headers
    ).json()
    answered = client.post(
        f"/api/v1/diagnoses/{diagnosis_id}/answers",
        headers=headers,
        json={
            "answers": [
                {
                    "question_id": questions[0]["id"],
                    "answer": "Повреждены старые листья",
                }
            ]
        },
    )
    assert answered.status_code == 202
    answered_diagnosis = resolve_diagnosis_job(client, headers, answered)
    assert answered_diagnosis.json()["current_revision"] == 2
    reanalyzed = client.post(
        f"/api/v1/diagnoses/{diagnosis_id}/reanalyze", headers=headers
    )
    assert reanalyzed.status_code == 202
    reanalyzed_diagnosis = resolve_diagnosis_job(client, headers, reanalyzed)
    assert reanalyzed_diagnosis.json()["current_revision"] == 3
    feedback = client.post(
        f"/api/v1/diagnoses/{diagnosis_id}/feedback",
        headers=headers,
        json={"rating": 4, "helpful": True, "comment": "Полезно"},
    )
    assert feedback.status_code == 200
    updated_feedback = client.post(
        f"/api/v1/diagnoses/{diagnosis_id}/feedback",
        headers=headers,
        json={"rating": 5, "helpful": True, "comment": "Обновлённый отзыв"},
    )
    assert updated_feedback.status_code == 200
    assert updated_feedback.json()["id"] == feedback.json()["id"]
    assert updated_feedback.json()["rating"] == 5
    diagnoses = client.get(f"/api/v1/plants/{plant['id']}/diagnoses", headers=headers)
    assert diagnoses.status_code == 200
    history = client.get(f"/api/v1/plants/{plant['id']}/history", headers=headers)
    assert history.status_code == 200
    assert len(history.json()["revisions"]) == 3
    assert history.json()["answers"] == [
        {
            "diagnosis_id": diagnosis_id,
            "question": questions[0]["text"],
            "answer": "Повреждены старые листья",
            "revision_number": 1,
        }
    ]

    assert (
        client.post(
            f"/api/v1/diagnoses/{diagnosis_id}/reanalyze", headers=headers
        ).status_code
        == 202
    )
    assert (
        client.post(
            f"/api/v1/diagnoses/{diagnosis_id}/reanalyze", headers=headers
        ).status_code
        == 429
    )


def test_monthly_diagnosis_quota(client):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]
    payload = {
        "plant_id": plant["id"],
        "symptoms": "На листьях появились заметные изменения окраски",
        "damaged_part": "leaf",
        "photo_ids": [photo_id],
    }
    for _ in range(5):
        assert (
            client.post("/api/v1/diagnoses", headers=headers, json=payload).status_code
            == 202
        )
    limited = client.post("/api/v1/diagnoses", headers=headers, json=payload)
    assert limited.status_code == 429


def test_ai_timeout_returns_504_and_is_audited(client, monkeypatch):
    plant, headers = create_plant(client)
    photo_id = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=headers,
        files=[("files", ("leaf.jpg", image_bytes(), "image/jpeg"))],
    ).json()[0]["id"]

    class TimeoutGateway:
        model_name = "timeout-test-model"
        prompt_version = "timeout-test-v1"
        demo_mode = False

        def analyze(self, *args, **kwargs):
            raise AIProviderError(
                "timeout",
                http_status=504,
                error_type="provider_timeout",
                request_id="req_timeout_test",
                response_ms=60_000,
            )

    monkeypatch.setattr(main_module, "ai_gateway", TimeoutGateway())
    response = client.post(
        "/api/v1/diagnoses",
        headers=headers,
        json={
            "plant_id": plant["id"],
            "symptoms": "На листьях появились заметные изменения",
            "damaged_part": "leaf",
            "photo_ids": [photo_id],
        },
    )
    assert response.status_code == 202
    assert response.json()["status"] == "failed"
    assert response.json()["error_type"] == "provider_timeout"
    with SessionLocal() as db:
        audit = db.scalar(
            select(AIRequestLog).where(AIRequestLog.request_id == "req_timeout_test")
        )
        assert audit is not None
        assert audit.success is False
        assert audit.error_type == "provider_timeout"


def test_photo_download_is_private(client):
    plant, owner_headers = create_plant(client)
    uploaded = client.post(
        f"/api/v1/plants/{plant['id']}/photos",
        headers=owner_headers,
        files=[("files", ("leaf.png", image_bytes("PNG"), "image/png"))],
    ).json()[0]
    downloaded = client.get(f"/api/v1/photos/{uploaded['id']}", headers=owner_headers)
    assert downloaded.status_code == 200
    assert downloaded.headers["content-type"] == "image/png"
    with Image.open(BytesIO(downloaded.content)) as image:
        assert image.size == (32, 32)

    stranger_headers = register(client, "stranger@example.com")
    assert (
        client.get(
            f"/api/v1/photos/{uploaded['id']}", headers=stranger_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/photos/{uploaded['id']}", headers=stranger_headers
        ).status_code
        == 404
    )
    assert (
        client.delete(
            f"/api/v1/photos/{uploaded['id']}", headers=owner_headers
        ).status_code
        == 204
    )
    assert (
        client.get(
            f"/api/v1/photos/{uploaded['id']}", headers=owner_headers
        ).status_code
        == 404
    )

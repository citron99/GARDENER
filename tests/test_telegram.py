from datetime import datetime
from datetime import time as datetime_time
from zoneinfo import ZoneInfo

from sqlalchemy import select

from app.config import settings
from app.database import SessionLocal
from app.models import TelegramAccount, TelegramLinkToken, TelegramUpdate
from app.services.telegram_service import TelegramServiceError


def register(client, email="telegram@example.com"):
    response = client.post("/api/v1/auth/register", json={
        "email": email,
        "name": "Садовод Telegram",
        "password": "strong-password",
        "region": "Riga",
    })
    assert response.status_code == 201
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def configure_telegram(monkeypatch):
    monkeypatch.setattr(settings, "telegram_enabled", True)
    monkeypatch.setattr(settings, "telegram_bot_token", "123456:test-token")
    monkeypatch.setattr(settings, "telegram_webhook_secret", "telegram_webhook_secret_2026")
    monkeypatch.setattr(settings, "telegram_bot_username", "ai_garden_test_bot")


def webhook(client, update_id, text, secret="telegram_webhook_secret_2026", chat_id=987654321):
    return client.post(
        "/api/v1/telegram/webhook",
        headers={"X-Telegram-Bot-Api-Secret-Token": secret},
        json={
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "from": {"id": chat_id, "username": "gardener", "language_code": "ru"},
                "chat": {"id": chat_id, "type": "private"},
                "text": text,
            },
        },
    )


def test_telegram_disabled_is_explicit(client):
    headers = register(client)
    assert client.get("/api/v1/telegram/status", headers=headers).json() == {
        "enabled": False,
        "linked": False,
        "username": None,
        "language": None,
    }
    assert client.post("/api/v1/telegram/link", headers=headers).status_code == 503
    assert client.post("/api/v1/telegram/webhook", json={}).status_code == 404


def test_one_time_link_commands_and_idempotent_updates(client, monkeypatch):
    import app.telegram as telegram_module

    configure_telegram(monkeypatch)
    sent = []
    monkeypatch.setattr(telegram_module, "send_message", lambda chat_id, text: sent.append((chat_id, text)))
    headers = register(client, "linked@example.com")
    garden = client.post("/api/v1/gardens", headers=headers, json={"name": "Telegram сад"}).json()
    plant = client.post("/api/v1/plants", headers=headers, json={
        "garden_id": garden["id"], "name": "Роза", "growing_place": "open_ground",
    }).json()
    local_today = datetime.now(ZoneInfo(settings.telegram_timezone)).date()
    due_at = datetime.combine(local_today, datetime_time(hour=12), tzinfo=ZoneInfo(settings.telegram_timezone))
    assert client.post(
        f"/api/v1/plants/{plant['id']}/reminders",
        headers=headers,
        json={"kind": "inspection", "title": "Проверить листья", "due_at": due_at.isoformat()},
    ).status_code == 201

    link = client.post("/api/v1/telegram/link", headers=headers)
    assert link.status_code == 201
    code = link.json()["code"]
    assert code in link.json()["deep_link"]
    with SessionLocal() as db:
        token = db.scalar(select(TelegramLinkToken))
        assert token.token_hash != code
        assert len(token.token_hash) == 64

    wrong_secret = webhook(client, 1, f"/start {code}", secret="wrong-secret")
    assert wrong_secret.status_code == 403
    linked = webhook(client, 1, f"/start {code}")
    assert linked.status_code == 200
    assert linked.json()["new_update"] is True
    assert "привязан" in sent[-1][1]
    assert client.get("/api/v1/telegram/status", headers=headers).json()["linked"] is True
    assert webhook(client, 1, f"/start {code}").json()["new_update"] is False
    assert len(sent) == 1

    plants = webhook(client, 2, "/plants")
    assert plants.status_code == 200
    assert "Роза" in sent[-1][1]
    today = webhook(client, 3, "/today")
    assert today.status_code == 200
    assert "Проверить листья" in sent[-1][1]
    assert webhook(client, 4, "/start invalid-code").status_code == 200
    assert "недействителен" in sent[-1][1]
    with SessionLocal() as db:
        assert db.query(TelegramAccount).count() == 1
        assert db.query(TelegramUpdate).count() == 4

    assert client.delete("/api/v1/telegram/link", headers=headers).status_code == 204
    assert client.get("/api/v1/telegram/status", headers=headers).json()["linked"] is False


def test_unsent_webhook_response_is_retried_without_reprocessing(client, monkeypatch):
    import app.telegram as telegram_module

    configure_telegram(monkeypatch)
    headers = register(client, "retry@example.com")
    code = client.post("/api/v1/telegram/link", headers=headers).json()["code"]
    calls = []

    def failing_send(chat_id, text):
        calls.append((chat_id, text))
        raise TelegramServiceError("Telegram временно недоступен")

    monkeypatch.setattr(telegram_module, "send_message", failing_send)
    assert webhook(client, 20, f"/start {code}").status_code == 503
    with SessionLocal() as db:
        update = db.scalar(select(TelegramUpdate).where(TelegramUpdate.update_id == 20))
        assert update.sent is False
        assert db.query(TelegramAccount).count() == 1

    monkeypatch.setattr(telegram_module, "send_message", lambda chat_id, text: calls.append((chat_id, text)))
    retry = webhook(client, 20, f"/start {code}")
    assert retry.status_code == 200
    assert retry.json()["new_update"] is False
    with SessionLocal() as db:
        assert db.scalar(select(TelegramUpdate).where(TelegramUpdate.update_id == 20)).sent is True
        assert db.query(TelegramAccount).count() == 1
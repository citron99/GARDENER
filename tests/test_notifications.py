from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from app.database import SessionLocal
from app.models import TelegramAccount, User, UserNotification
from app.services import notification_service


def test_pending_notification_is_delivered_to_telegram_once(client, monkeypatch):
    response = client.post("/api/v1/auth/register", json={
        "email": "proactive@example.com",
        "name": "Proactive",
        "password": "strong-password",
        "language": "en",
    })
    assert response.status_code == 201
    now = datetime(2026, 7, 13, 8, tzinfo=UTC)
    with SessionLocal() as db:
        user = db.scalar(select(User).where(User.email == "proactive@example.com"))
        db.add(TelegramAccount(user_id=user.id, chat_id=123456, language="en", active=True))
        db.add(UserNotification(
            user_id=user.id,
            kind="seasonal_task",
            title="Seasonal task",
            body="Prepare frost protection",
            event_at=now + timedelta(hours=2),
            deduplication_key="test:seasonal:2026",
        ))
        db.commit()

    sent_messages = []
    monkeypatch.setattr(notification_service, "send_message", lambda chat_id, text: sent_messages.append((chat_id, text)))
    with SessionLocal() as db:
        assert notification_service.deliver_pending_telegram_notifications(db, now=now) == (1, 0)
        assert notification_service.deliver_pending_telegram_notifications(db, now=now) == (0, 0)
        item = db.scalar(select(UserNotification))
        assert item.telegram_sent_at is not None
        assert item.delivery_attempts == 1
    assert sent_messages == [(123456, "Seasonal task\nPrepare frost protection")]
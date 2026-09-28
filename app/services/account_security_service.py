import hashlib
import logging
import secrets
import smtplib
import ssl
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import AccountActionToken, User

logger = logging.getLogger(__name__)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def issue_action_token(db: Session, user: User, purpose: str, ttl_minutes: int = 30) -> str:
    raw = secrets.token_urlsafe(48)
    db.add(AccountActionToken(
        id=str(uuid4()), user_id=user.id, purpose=purpose, token_hash=_hash(raw),
        expires_at=datetime.now(UTC) + timedelta(minutes=ttl_minutes),
    ))
    db.commit()
    return raw


def consume_action_token(db: Session, raw: str, purpose: str) -> User | None:
    now = datetime.now(UTC)
    item = db.scalar(select(AccountActionToken).where(
        AccountActionToken.token_hash == _hash(raw),
        AccountActionToken.purpose == purpose,
        AccountActionToken.used_at.is_(None),
        AccountActionToken.expires_at > now,
    ).with_for_update())
    if not item:
        return None
    db.query(AccountActionToken).filter(
        AccountActionToken.user_id == item.user_id,
        AccountActionToken.purpose == purpose,
        AccountActionToken.used_at.is_(None),
    ).update({AccountActionToken.used_at: now}, synchronize_session=False)
    return db.get(User, item.user_id)


def send_action_email(email: str, purpose: str, token: str) -> None:
    action = "verify-email" if purpose == "verify_email" else "reset-password"
    # The fragment is not sent in HTTP request logs; the web client exchanges it
    # for the one-time token through the confirmation POST endpoint.
    url = f"{settings.public_base_url.rstrip('/')}/#action={action}&token={token}"
    if settings.email_delivery_mode != "smtp":
        logger.info("development account action link", extra={"email": email, "purpose": purpose, "url": url})
        return
    message = EmailMessage()
    message["From"] = settings.smtp_from_email
    message["To"] = email
    message["Subject"] = "AI Garden account action"
    message.set_content(f"Open this one-time link within 30 minutes:\n{url}")
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=10) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(settings.smtp_username, settings.smtp_password)
        client.send_message(message)

import hmac
import json

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import TelegramUpdate, User
from app.schemas import TelegramLinkRead, TelegramStatusRead
from app.services.telegram_service import (
    TelegramServiceError,
    create_link_code,
    process_command,
    send_message,
    telegram_status,
    unlink_account,
)

router = APIRouter(prefix="/api/v1/telegram", tags=["telegram"])


@router.get("/status", response_model=TelegramStatusRead)
def status(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    account = telegram_status(db, user)
    return {
        "enabled": settings.telegram_enabled,
        "linked": account is not None,
        "username": account.username if account else None,
        "language": account.language if account else None,
    }


@router.post("/link", response_model=TelegramLinkRead, status_code=201)
def link(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not settings.telegram_enabled:
        raise HTTPException(503, "Telegram-бот пока не подключён")
    code, expires_at = create_link_code(db, user)
    username = (settings.telegram_bot_username or "").lstrip("@")
    return {
        "code": code,
        "deep_link": f"https://t.me/{username}?start={code}" if username else None,
        "expires_at": expires_at,
    }


@router.delete("/link", status_code=204)
def unlink(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> Response:
    unlink_account(db, user)
    return Response(status_code=204)


@router.post("/webhook")
async def webhook(
    request: Request,
    secret_header: str | None = Header(default=None, alias="X-Telegram-Bot-Api-Secret-Token"),
    db: Session = Depends(get_db),
) -> dict:
    if not settings.telegram_enabled:
        raise HTTPException(404, "Telegram webhook не настроен")
    expected_secret = settings.telegram_webhook_secret or ""
    if not secret_header or not hmac.compare_digest(secret_header, expected_secret):
        raise HTTPException(403, "Некорректный секрет Telegram webhook")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.telegram_webhook_max_bytes:
        raise HTTPException(413, "Telegram webhook слишком большой")
    raw_body = await request.body()
    if len(raw_body) > settings.telegram_webhook_max_bytes:
        raise HTTPException(413, "Telegram webhook слишком большой")
    try:
        update_payload = json.loads(raw_body)
        update_id = int(update_payload["update_id"])
    except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        raise HTTPException(400, "Некорректное обновление Telegram") from exc

    existing = db.scalar(select(TelegramUpdate).where(TelegramUpdate.update_id == update_id))
    if existing:
        if existing.sent or not existing.response_text or existing.chat_id is None:
            return {"received": True, "new_update": False}
        try:
            send_message(existing.chat_id, existing.response_text)
        except TelegramServiceError as exc:
            raise HTTPException(503, str(exc)) from exc
        existing.sent = True
        db.commit()
        return {"received": True, "new_update": False}

    message = update_payload.get("message")
    chat_id = None
    response_text = None
    sent = True
    if isinstance(message, dict):
        chat = message.get("chat") or {}
        sender = message.get("from") or {}
        text = message.get("text")
        if chat.get("type") == "private" and isinstance(chat.get("id"), int) and isinstance(text, str):
            chat_id = chat["id"]
            response_text = process_command(
                db,
                chat_id,
                text,
                sender.get("username") if isinstance(sender.get("username"), str) else None,
                sender.get("language_code") if isinstance(sender.get("language_code"), str) else None,
            )
            sent = False
    record = TelegramUpdate(
        update_id=update_id,
        chat_id=chat_id,
        response_text=response_text,
        sent=sent,
    )
    db.add(record)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return {"received": True, "new_update": False}
    if response_text and chat_id is not None:
        try:
            send_message(chat_id, response_text)
        except TelegramServiceError as exc:
            raise HTTPException(503, str(exc)) from exc
        record.sent = True
        db.commit()
    return {"received": True, "new_update": True}
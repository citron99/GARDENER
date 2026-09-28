import hashlib
import secrets
from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Garden, Plant, Reminder, TelegramAccount, TelegramLinkToken, User


class TelegramServiceError(RuntimeError):
    pass


def _token_hash(code: str) -> str:
    return hashlib.sha256(code.encode()).hexdigest()


def create_link_code(db: Session, user: User) -> tuple[str, datetime]:
    now = datetime.now(UTC)
    db.execute(
        update(TelegramLinkToken)
        .where(TelegramLinkToken.user_id == user.id, TelegramLinkToken.used_at.is_(None))
        .values(used_at=now)
    )
    code = secrets.token_urlsafe(24)
    expires_at = now + timedelta(minutes=settings.telegram_link_ttl_minutes)
    db.add(TelegramLinkToken(user_id=user.id, token_hash=_token_hash(code), expires_at=expires_at))
    db.commit()
    return code, expires_at


def telegram_status(db: Session, user: User) -> TelegramAccount | None:
    return db.scalar(select(TelegramAccount).where(
        TelegramAccount.user_id == user.id, TelegramAccount.active.is_(True)))


def unlink_account(db: Session, user: User) -> bool:
    account = telegram_status(db, user)
    if not account:
        return False
    account.active = False
    db.commit()
    return True


def consume_link_code(
    db: Session,
    code: str,
    chat_id: int,
    username: str | None,
    language: str | None,
) -> User | None:
    now = datetime.now(UTC)
    token = db.scalar(select(TelegramLinkToken).where(
        TelegramLinkToken.token_hash == _token_hash(code),
        TelegramLinkToken.used_at.is_(None),
        TelegramLinkToken.expires_at >= now,
    ))
    if not token:
        return None
    user = db.get(User, token.user_id)
    if not user:
        return None
    by_chat = db.scalar(select(TelegramAccount).where(TelegramAccount.chat_id == chat_id))
    by_user = db.scalar(select(TelegramAccount).where(TelegramAccount.user_id == user.id))
    if by_chat and by_chat.user_id != user.id:
        if by_chat.active:
            raise TelegramServiceError("Этот Telegram уже связан с другим активным аккаунтом")
        db.delete(by_chat)
        db.flush()
    if by_user:
        by_user.chat_id = chat_id
        by_user.username = username
        by_user.language = (language or user.language or "ru")[:10]
        by_user.active = True
    else:
        db.add(TelegramAccount(
            user_id=user.id,
            chat_id=chat_id,
            username=username,
            language=(language or user.language or "ru")[:10],
        ))
    token.used_at = now
    db.commit()
    return user


def _language(account: TelegramAccount | None) -> str:
    language = (account.language if account else "ru").split("-", 1)[0].lower()
    return language if language in {"ru", "lv", "en"} else "ru"


def _help(language: str) -> str:
    return {
        "ru": "Команды:\n/plants — мои растения\n/today — задачи на сегодня\n/unlink — отключить Telegram\n/help — помощь",
        "lv": "Komandas:\n/plants — mani augi\n/today — šodienas uzdevumi\n/unlink — atvienot Telegram\n/help — palīdzība",
        "en": "Commands:\n/plants — my plants\n/today — today's tasks\n/unlink — disconnect Telegram\n/help — help",
    }[language]


def _plants(db: Session, user_id: int, language: str) -> str:
    plants = db.scalars(
        select(Plant).join(Garden).where(Garden.user_id == user_id).order_by(Plant.name).limit(30)
    ).all()
    if not plants:
        return {"ru": "Растений пока нет.", "lv": "Augu vēl nav.", "en": "No plants yet."}[language]
    heading = {"ru": "Ваши растения:", "lv": "Jūsu augi:", "en": "Your plants:"}[language]
    return heading + "\n" + "\n".join(f"• {plant.name}" for plant in plants)


def _today(db: Session, user_id: int, language: str) -> str:
    zone = ZoneInfo(settings.telegram_timezone)
    local_now = datetime.now(zone)
    start = datetime.combine(local_now.date(), time.min, tzinfo=zone).astimezone(UTC)
    end = start + timedelta(days=1)
    rows = db.execute(
        select(Reminder, Plant.name)
        .join(Plant, Plant.id == Reminder.plant_id)
        .join(Garden, Garden.id == Plant.garden_id)
        .where(
            Garden.user_id == user_id,
            Reminder.completed_at.is_(None),
            Reminder.due_at >= start,
            Reminder.due_at < end,
        )
        .order_by(Reminder.due_at).limit(30)
    ).all()
    if not rows:
        return {"ru": "На сегодня активных задач нет.", "lv": "Šodien nav aktīvu uzdevumu.", "en": "No active tasks for today."}[language]
    heading = {"ru": "Задачи на сегодня:", "lv": "Šodienas uzdevumi:", "en": "Today's tasks:"}[language]
    lines = []
    for reminder, plant_name in rows:
        due = reminder.due_at
        if due.tzinfo is None:
            due = due.replace(tzinfo=UTC)
        lines.append(f"• {due.astimezone(zone):%H:%M} · {plant_name} · {reminder.title}")
    return heading + "\n" + "\n".join(lines)


def process_command(
    db: Session,
    chat_id: int,
    text: str,
    username: str | None,
    language_code: str | None,
) -> str:
    parts = text.strip().split(maxsplit=1)
    command = parts[0].split("@", 1)[0].lower() if parts else ""
    argument = parts[1].strip() if len(parts) > 1 else ""
    if command == "/start" and argument:
        requested_language = (language_code or "ru").split("-", 1)[0].lower()
        if requested_language not in {"ru", "lv", "en"}:
            requested_language = "ru"
        try:
            user = consume_link_code(db, argument, chat_id, username, language_code)
        except TelegramServiceError as exc:
            if "другим активным аккаунтом" in str(exc):
                return {
                    "ru": str(exc),
                    "lv": "Šis Telegram jau ir saistīts ar citu aktīvu kontu.",
                    "en": "This Telegram account is already linked to another active account.",
                }[requested_language]
            return str(exc)
        if user:
            language = (user.language or "ru").split("-", 1)[0].lower()
            if language not in {"ru", "lv", "en"}:
                language = "ru"
            linked = {
                "ru": f"Аккаунт AI Garden привязан, {user.name}.",
                "lv": f"AI Garden konts ir sasaistīts, {user.name}.",
                "en": f"AI Garden account linked, {user.name}.",
            }[language]
            return linked + "\n" + _help(language)
        return {
            "ru": "Код недействителен или истёк. Создайте новый код в AI Garden.",
            "lv": "Kods nav derīgs vai ir beidzies. Izveidojiet jaunu kodu AI Garden.",
            "en": "The code is invalid or expired. Create a new code in AI Garden.",
        }[requested_language]
    account = db.scalar(select(TelegramAccount).where(
        TelegramAccount.chat_id == chat_id, TelegramAccount.active.is_(True)))
    language = _language(account)
    if not account:
        fallback_language = (language_code or "ru").split("-", 1)[0].lower()
        if fallback_language not in {"ru", "lv", "en"}:
            fallback_language = "ru"
        return {
            "ru": "Telegram не привязан. Создайте одноразовую ссылку в AI Garden и нажмите Start.",
            "lv": "Telegram nav sasaistīts. Izveidojiet vienreizēju saiti AI Garden un nospiediet Start.",
            "en": "Telegram is not linked. Create a one-time link in AI Garden and press Start.",
        }[fallback_language]
    if command == "/plants":
        return _plants(db, account.user_id, language)
    if command == "/today":
        return _today(db, account.user_id, language)
    if command == "/unlink":
        account.active = False
        db.commit()
        return {"ru": "Telegram отключён.", "lv": "Telegram ir atvienots.", "en": "Telegram disconnected."}[language]
    return _help(language)


def send_message(chat_id: int, text: str) -> None:
    if not settings.telegram_bot_token:
        raise TelegramServiceError("Telegram-бот не настроен")
    safe_text = text[:4096]
    try:
        response = httpx.post(
            f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": chat_id, "text": safe_text},
            timeout=10,
        )
    except httpx.HTTPError as exc:
        raise TelegramServiceError("Telegram временно недоступен") from exc
    if response.status_code >= 400:
        raise TelegramServiceError("Telegram отклонил отправку сообщения")
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("ok") is not True:
        raise TelegramServiceError("Некорректный ответ Telegram")
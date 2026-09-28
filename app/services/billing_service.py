import hashlib
import hmac
import json
import time
from datetime import UTC, datetime
from uuid import uuid4

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import BillingEvent, Subscription, User


class BillingProviderError(RuntimeError):
    pass


def _stripe_post(path: str, data: dict[str, str], idempotency_key: str | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Stripe-Version": settings.stripe_api_version,
    }
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    try:
        response = httpx.post(
            f"{settings.stripe_api_base.rstrip('/')}/{path.lstrip('/')}",
            data=data,
            headers=headers,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise BillingProviderError("Платёжный сервис временно недоступен") from exc
    if response.status_code >= 400:
        raise BillingProviderError("Платёжный сервис отклонил запрос")
    payload = response.json()
    if not isinstance(payload, dict):
        raise BillingProviderError("Некорректный ответ платёжного сервиса")
    return payload


def cancel_stripe_subscription(subscription_id: str, user_id: int) -> None:
    headers = {
        "Authorization": f"Bearer {settings.stripe_secret_key}",
        "Stripe-Version": settings.stripe_api_version,
        "Idempotency-Key": f"account-delete-{user_id}-{subscription_id}",
    }
    try:
        response = httpx.delete(
            f"{settings.stripe_api_base.rstrip('/')}/subscriptions/{subscription_id}",
            headers=headers,
            timeout=15,
        )
    except httpx.HTTPError as exc:
        raise BillingProviderError("Платёжный сервис временно недоступен") from exc
    if response.status_code >= 400 and response.status_code != 404:
        raise BillingProviderError("Не удалось отменить подписку")


def create_checkout_session(user: User, subscription: Subscription | None) -> str:
    data = {
        "mode": "subscription",
        "success_url": settings.billing_success_url,
        "cancel_url": settings.billing_cancel_url,
        "client_reference_id": str(user.id),
        "line_items[0][price]": str(settings.stripe_price_pro_monthly),
        "line_items[0][quantity]": "1",
        "subscription_data[metadata][user_id]": str(user.id),
        "metadata[user_id]": str(user.id),
        "allow_promotion_codes": "true",
    }
    if subscription and subscription.provider_customer_id:
        data["customer"] = subscription.provider_customer_id
    else:
        data["customer_email"] = user.email
    payload = _stripe_post("checkout/sessions", data, f"checkout-{user.id}-{uuid4()}")
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingProviderError("Платёжный сервис не вернул безопасную ссылку")
    return url


def create_portal_session(subscription: Subscription) -> str:
    if not subscription.provider_customer_id:
        raise BillingProviderError("Платёжный профиль ещё не создан")
    payload = _stripe_post("billing_portal/sessions", {
        "customer": subscription.provider_customer_id,
        "return_url": settings.billing_success_url.split("?", 1)[0],
    })
    url = payload.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise BillingProviderError("Платёжный сервис не вернул безопасную ссылку")
    return url


def verify_stripe_signature(payload: bytes, signature_header: str | None, now: int | None = None) -> dict:
    if not signature_header or not settings.stripe_webhook_secret:
        raise BillingProviderError("Отсутствует подпись webhook")
    parts: dict[str, list[str]] = {}
    for item in signature_header.split(","):
        key, separator, value = item.strip().partition("=")
        if separator:
            parts.setdefault(key, []).append(value)
    try:
        timestamp = int(parts["t"][0])
    except (KeyError, ValueError, IndexError) as exc:
        raise BillingProviderError("Некорректная подпись webhook") from exc
    current = int(time.time()) if now is None else now
    if abs(current - timestamp) > settings.stripe_webhook_tolerance_seconds:
        raise BillingProviderError("Подпись webhook просрочена")
    signed_payload = f"{timestamp}.".encode() + payload
    expected = hmac.new(settings.stripe_webhook_secret.encode(), signed_payload, hashlib.sha256).hexdigest()
    if not any(hmac.compare_digest(expected, candidate) for candidate in parts.get("v1", [])):
        raise BillingProviderError("Подпись webhook не подтверждена")
    try:
        event = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BillingProviderError("Некорректный JSON webhook") from exc
    if not isinstance(event, dict) or not isinstance(event.get("id"), str) or not isinstance(event.get("type"), str):
        raise BillingProviderError("Некорректное событие webhook")
    return event


def _unix_datetime(value) -> datetime | None:
    if not isinstance(value, (int, float)):
        return None
    return datetime.fromtimestamp(value, UTC)


def _utc(value: datetime | None) -> datetime | None:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def _metadata_user_id(obj: dict) -> int | None:
    raw = (obj.get("metadata") or {}).get("user_id") or obj.get("client_reference_id")
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _find_subscription(db: Session, obj: dict) -> Subscription | None:
    external_subscription = obj.get("subscription") or obj.get("id")
    customer = obj.get("customer")
    clauses = []
    if isinstance(external_subscription, str):
        clauses.append(Subscription.provider_subscription_id == external_subscription)
    if isinstance(customer, str):
        clauses.append(Subscription.provider_customer_id == customer)
    for clause in clauses:
        subscription = db.scalar(select(Subscription).where(clause))
        if subscription:
            return subscription
    return None


def _upsert_subscription(db: Session, obj: dict) -> Subscription | None:
    subscription = _find_subscription(db, obj)
    user_id = _metadata_user_id(obj)
    if not subscription and user_id and db.get(User, user_id):
        subscription = db.scalar(select(Subscription).where(Subscription.user_id == user_id))
        if not subscription:
            subscription = Subscription(user_id=user_id, plan="pro", provider="stripe", status="pending")
            db.add(subscription)
    return subscription


def process_stripe_event(db: Session, event: dict) -> bool:
    event_id = event["id"]
    if db.scalar(select(BillingEvent.id).where(BillingEvent.provider_event_id == event_id)):
        return False
    event_type = event["type"]
    event_created_at = _unix_datetime(event.get("created"))
    obj = ((event.get("data") or {}).get("object") or {})
    processed = False
    if isinstance(obj, dict) and event_type == "checkout.session.completed" and obj.get("mode") == "subscription":
        subscription = _upsert_subscription(db, obj)
        if subscription:
            subscription.provider_customer_id = obj.get("customer") or subscription.provider_customer_id
            subscription.provider_subscription_id = obj.get("subscription") or subscription.provider_subscription_id
            processed = True
    elif isinstance(obj, dict) and event_type in {
        "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    }:
        subscription = _upsert_subscription(db, obj)
        if subscription:
            last_event_at = _utc(subscription.last_event_created_at)
            if last_event_at and event_created_at and event_created_at < last_event_at:
                db.add(BillingEvent(provider_event_id=event_id, event_type=event_type, processed=False))
                db.commit()
                return True
            items = ((obj.get("items") or {}).get("data") or [])
            price_id = None
            if items and isinstance(items[0], dict):
                price_id = (items[0].get("price") or {}).get("id")
            subscription.provider_customer_id = obj.get("customer") or subscription.provider_customer_id
            subscription.provider_subscription_id = obj.get("id") or subscription.provider_subscription_id
            subscription.plan = "pro"
            if event_type == "customer.subscription.deleted":
                subscription.status = "canceled"
                from app.services.account_deletion_service import finalize_account_deletion
                finalize_account_deletion(db, subscription.user_id)
            elif price_id != settings.stripe_price_pro_monthly:
                subscription.status = "unsupported_price"
            else:
                subscription.status = str(obj.get("status") or "pending")
            subscription.current_period_end = _unix_datetime(obj.get("current_period_end"))
            if subscription.current_period_end is None and items and isinstance(items[0], dict):
                subscription.current_period_end = _unix_datetime(items[0].get("current_period_end"))
            subscription.last_event_created_at = event_created_at or subscription.last_event_created_at
            subscription.cancel_at_period_end = bool(obj.get("cancel_at_period_end", False))
            processed = True
    elif isinstance(obj, dict) and event_type == "invoice.payment_failed":
        subscription = _find_subscription(db, obj)
        if subscription:
            last_event_at = _utc(subscription.last_event_created_at)
            if not last_event_at or not event_created_at or event_created_at >= last_event_at:
                subscription.status = "past_due"
                subscription.last_event_created_at = event_created_at or subscription.last_event_created_at
                processed = True
    db.add(BillingEvent(provider_event_id=event_id, event_type=event_type, processed=processed))
    db.commit()
    return True

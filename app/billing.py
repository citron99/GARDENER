from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth import get_current_user
from app.config import settings
from app.database import get_db
from app.models import Subscription, User
from app.schemas import BillingPlanRead, BillingRedirectRead, CheckoutCreate, SubscriptionRead
from app.services.billing_service import (
    BillingProviderError,
    create_checkout_session,
    create_portal_session,
    process_stripe_event,
    verify_stripe_signature,
)

router = APIRouter(prefix="/api/v1/billing", tags=["billing"])


def _billing_enabled() -> bool:
    return settings.billing_provider.strip().lower() == "stripe"


@router.get("/plans", response_model=list[BillingPlanRead])
def billing_plans() -> list[dict]:
    enabled = _billing_enabled()
    return [
        {
            "code": "free",
            "name": "Бесплатный",
            "monthly_amount": 0,
            "currency": settings.billing_currency,
            "features": [f"До {settings.free_diagnoses_per_month} диагностик в месяц", "История и календарь"],
            "checkout_available": False,
        },
        {
            "code": "pro",
            "name": "Pro",
            "monthly_amount": settings.billing_pro_monthly_cents,
            "currency": settings.billing_currency,
            "features": [f"До {settings.pro_diagnoses_per_month} диагностик в месяц", "Расширенная история"],
            "checkout_available": enabled,
        },
    ]


@router.get("/subscription", response_model=SubscriptionRead)
def current_subscription(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if not subscription:
        return {
            "plan": "free",
            "status": "active",
            "current_period_end": None,
            "cancel_at_period_end": False,
            "can_manage": False,
            "billing_enabled": _billing_enabled(),
        }
    return {
        "plan": subscription.plan,
        "status": subscription.status,
        "current_period_end": subscription.current_period_end,
        "cancel_at_period_end": subscription.cancel_at_period_end,
        "can_manage": bool(subscription.provider_customer_id and _billing_enabled()),
        "billing_enabled": _billing_enabled(),
    }


@router.post("/checkout", response_model=BillingRedirectRead)
def checkout(
    payload: CheckoutCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not _billing_enabled():
        raise HTTPException(503, "Оплата пока не подключена")
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if subscription and subscription.status in {"active", "trialing"}:
        raise HTTPException(409, "Подписка уже активна; используйте управление подпиской")
    try:
        return {"url": create_checkout_session(user, subscription)}
    except BillingProviderError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/portal", response_model=BillingRedirectRead)
def portal(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    if not _billing_enabled():
        raise HTTPException(503, "Оплата пока не подключена")
    subscription = db.scalar(select(Subscription).where(Subscription.user_id == user.id))
    if not subscription or not subscription.provider_customer_id:
        raise HTTPException(404, "Платёжный профиль не найден")
    try:
        return {"url": create_portal_session(subscription)}
    except BillingProviderError as exc:
        raise HTTPException(503, str(exc)) from exc


@router.post("/stripe/webhook")
async def stripe_webhook(
    request: Request,
    stripe_signature: str | None = Header(default=None, alias="Stripe-Signature"),
    db: Session = Depends(get_db),
) -> dict:
    if not _billing_enabled():
        raise HTTPException(404, "Webhook не настроен")
    content_length = request.headers.get("content-length")
    if content_length and content_length.isdigit() and int(content_length) > settings.stripe_webhook_max_bytes:
        raise HTTPException(413, "Webhook слишком большой")
    raw_body = await request.body()
    if len(raw_body) > settings.stripe_webhook_max_bytes:
        raise HTTPException(413, "Webhook слишком большой")
    try:
        event = verify_stripe_signature(raw_body, stripe_signature)
        accepted = process_stripe_event(db, event)
    except BillingProviderError as exc:
        raise HTTPException(400, str(exc)) from exc
    except IntegrityError:
        db.rollback()
        accepted = False
    return {"received": True, "new_event": accepted}
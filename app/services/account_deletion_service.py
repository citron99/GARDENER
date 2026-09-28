from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AccountDeletionRequest, AuthSession, Garden, Plant, PlantPhoto, User
from app.services.storage_outbox_service import enqueue_photo_deletions


def stage_account_deletion(
    db: Session,
    user: User,
    provider_subscription_id: str,
) -> AccountDeletionRequest:
    request = db.scalar(select(AccountDeletionRequest).where(
        AccountDeletionRequest.user_id == user.id,
    ))
    if request is None:
        request = AccountDeletionRequest(
            user_id=user.id,
            provider_subscription_id=provider_subscription_id,
        )
        db.add(request)
    request.provider_subscription_id = provider_subscription_id
    request.status = "pending_cancellation"
    request.last_error = None
    user.is_blocked = True
    db.query(AuthSession).filter(
        AuthSession.user_id == user.id,
        AuthSession.revoked_at.is_(None),
    ).update({AuthSession.revoked_at: datetime.now(UTC)}, synchronize_session=False)
    db.commit()
    db.refresh(request)
    return request


def mark_cancellation_requested(db: Session, request: AccountDeletionRequest) -> None:
    request.attempts += 1
    request.status = "awaiting_webhook"
    request.last_error = None
    request.cancellation_requested_at = datetime.now(UTC)
    db.commit()


def mark_cancellation_retry(db: Session, request: AccountDeletionRequest, error: Exception) -> None:
    request.status = "pending_cancellation"
    request.last_error = str(error)[:300]
    db.commit()


def finalize_account_deletion(db: Session, user_id: int) -> bool:
    user = db.get(User, user_id)
    if user is None:
        return False
    request = db.scalar(select(AccountDeletionRequest).where(
        AccountDeletionRequest.user_id == user_id,
    ))
    if request is None or request.status not in {"awaiting_webhook", "pending_cancellation"}:
        return False
    photos = list(db.scalars(
        select(PlantPhoto).join(Plant).join(Garden).where(Garden.user_id == user_id)
    ))
    enqueue_photo_deletions(db, photos)
    db.delete(user)
    return True

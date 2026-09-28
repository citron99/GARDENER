from datetime import UTC, datetime, timedelta
from uuid import uuid4

from sqlalchemy import and_, delete, or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import (
    AccountActionToken,
    AIRequestLog,
    AuthSession,
    BillingEvent,
    DiagnosisJob,
    PlantPhoto,
    TelegramLinkToken,
    TelegramUpdate,
    UserNotification,
)
from app.services.product_registry_service import revalidate_registry_rules
from app.services.storage_outbox_service import enqueue_photo_deletions


def recover_stale_diagnosis_jobs(
    db: Session,
    *,
    now: datetime | None = None,
) -> dict[str, list[str] | dict[str, str]]:
    """Reserve expired or orphaned jobs for redelivery after this transaction commits."""
    now = now or datetime.now(UTC)
    orphaned_before = now - timedelta(seconds=settings.diagnosis_job_recovery_grace_seconds)
    jobs = list(db.scalars(
        select(DiagnosisJob)
        .where(or_(
            and_(
                DiagnosisJob.status == "running",
                DiagnosisJob.lease_expires_at.is_not(None),
                DiagnosisJob.lease_expires_at < now,
            ),
            and_(
                DiagnosisJob.status == "queued",
                DiagnosisJob.celery_task_id.is_(None),
                DiagnosisJob.created_at < orphaned_before,
            ),
        ))
        .order_by(DiagnosisJob.created_at)
        .with_for_update(skip_locked=True)
    ))
    task_ids: dict[str, str] = {}
    failed: list[str] = []
    for job in jobs:
        if job.attempts >= settings.diagnosis_job_max_attempts:
            job.status = "failed"
            job.execution_token = None
            job.lease_expires_at = None
            job.celery_task_id = None
            job.error_type = "lease_expired"
            job.error_message = "Фоновый анализ не завершился после повторных попыток"
            job.completed_at = now
            failed.append(job.id)
            continue
        task_id = str(uuid4())
        job.status = "queued"
        job.execution_token = None
        job.lease_expires_at = None
        job.celery_task_id = task_id
        job.error_type = None
        job.error_message = None
        job.completed_at = None
        task_ids[job.id] = task_id
    db.commit()
    return {"task_ids": task_ids, "failed": failed}


def cleanup_expired_records(db: Session, *, now: datetime | None = None) -> dict[str, int]:
    now = now or datetime.now(UTC)
    cutoffs = {
        "jobs": now - timedelta(days=settings.completed_job_retention_days),
        "notifications": now - timedelta(days=settings.notification_retention_days),
        "ai_logs": now - timedelta(days=settings.ai_log_retention_days),
        "integration_events": now - timedelta(days=settings.integration_event_retention_days),
    }
    unattached_photos = list(db.scalars(
        select(PlantPhoto)
        .where(
            PlantPhoto.created_at < now - timedelta(hours=settings.unattached_photo_retention_hours),
            ~PlantPhoto.diagnoses.any(),
        )
        .order_by(PlantPhoto.id)
        .limit(500)
    ))
    enqueue_photo_deletions(db, unattached_photos)
    for photo in unattached_photos:
        db.delete(photo)
    counts = {
        "jobs": db.execute(delete(DiagnosisJob).where(
            DiagnosisJob.status.in_(("succeeded", "failed")),
            DiagnosisJob.completed_at < cutoffs["jobs"],
        )).rowcount,
        "notifications": db.execute(delete(UserNotification).where(
            UserNotification.created_at < cutoffs["notifications"],
        )).rowcount,
        "ai_logs": db.execute(delete(AIRequestLog).where(
            AIRequestLog.created_at < cutoffs["ai_logs"],
        )).rowcount,
        "telegram_updates": db.execute(delete(TelegramUpdate).where(
            TelegramUpdate.created_at < cutoffs["integration_events"],
        )).rowcount,
        "telegram_tokens": db.execute(delete(TelegramLinkToken).where(
            TelegramLinkToken.expires_at < now,
        )).rowcount,
        "billing_events": db.execute(delete(BillingEvent).where(
            BillingEvent.created_at < cutoffs["integration_events"],
        )).rowcount,
        "auth_sessions": db.execute(delete(AuthSession).where(
            AuthSession.expires_at < now,
        )).rowcount,
        "account_tokens": db.execute(delete(AccountActionToken).where(
            AccountActionToken.expires_at < now,
        )).rowcount,
        "unattached_photos": len(unattached_photos),
        "invalidated_product_rules": revalidate_registry_rules(db, today=now.date()),
    }
    db.commit()
    return {name: int(count or 0) for name, count in counts.items()}

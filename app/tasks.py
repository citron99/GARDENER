from celery import Celery
from sqlalchemy import select

from app.config import settings, validate_runtime_settings
from app.database import SessionLocal
from app.models import AccountDeletionRequest, DiagnosisJob
from app.services.account_deletion_service import (
    mark_cancellation_requested,
    mark_cancellation_retry,
)
from app.services.alert_service import dispatch_operational_alert
from app.services.billing_service import (
    BillingProviderError,
    cancel_stripe_subscription,
)
from app.services.diagnosis_job_service import (
    RetryableDiagnosisJob,
    process_diagnosis_job,
)
from app.services.invoice_delivery_service import (
    mark_stale_invoice_deliveries_unknown,
    pending_invoice_delivery_ids,
    process_invoice_delivery,
)
from app.services.maintenance_service import (
    cleanup_expired_records,
    recover_stale_diagnosis_jobs,
)
from app.services.notification_service import (
    deliver_pending_telegram_notifications,
    generate_due_notifications,
)
from app.services.storage_outbox_service import process_storage_deletion_outbox

validate_runtime_settings()


celery_app = Celery(
    "ai_garden",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_time_limit=settings.celery_task_timeout_seconds,
    task_soft_time_limit=max(30, settings.celery_task_timeout_seconds - 15),
    result_expires=3600,
    broker_transport_options={"visibility_timeout": 600},
    result_backend_transport_options={
        "visibility_timeout": 600,
        "global_keyprefix": "ai-garden:",
    },
)
celery_app.conf.beat_schedule = {
    "generate-and-deliver-notifications-hourly": {
        "task": "notifications.generate_and_deliver",
        "schedule": 3600.0,
    },
    "cleanup-expired-records-daily": {
        "task": "maintenance.cleanup_expired_records",
        "schedule": 86400.0,
    },
    "recover-stale-diagnosis-jobs": {
        "task": "maintenance.recover_stale_diagnosis_jobs",
        "schedule": 300.0,
    },
    "process-storage-deletion-outbox": {
        "task": "maintenance.process_storage_deletion_outbox",
        "schedule": 60.0,
    },
    "retry-account-deletion-cancellations": {
        "task": "maintenance.retry_account_deletions",
        "schedule": 300.0,
    },
    "dispatch-pending-invoice-deliveries": {
        "task": "maintenance.dispatch_pending_invoice_deliveries",
        "schedule": 60.0,
    },
}


@celery_app.task(bind=True, name="diagnoses.analyze", max_retries=2)
def analyze_diagnosis_task(self, job_id: str) -> int | None:
    try:
        return process_diagnosis_job(
            job_id,
            retry_transient=True,
            task_id=self.request.id,
        )
    except RetryableDiagnosisJob as exc:
        countdown = min(60, 5 * (2**self.request.retries))
        raise self.retry(exc=exc, countdown=countdown) from exc


@celery_app.task(name="invoices.deliver")
def send_partner_invoice_delivery_task(delivery_id: int) -> str | None:
    return process_invoice_delivery(delivery_id)


@celery_app.task(name="maintenance.dispatch_pending_invoice_deliveries")
def dispatch_pending_invoice_deliveries_task() -> dict[str, int]:
    stale = mark_stale_invoice_deliveries_unknown()
    published = 0
    publish_failed = 0
    for delivery_id in pending_invoice_delivery_ids():
        try:
            send_partner_invoice_delivery_task.apply_async(args=[delivery_id])
            published += 1
        except Exception:
            publish_failed += 1
    return {
        "published": published,
        "publish_failed": publish_failed,
        "stale_marked_unknown": stale,
    }


@celery_app.task(name="notifications.generate_and_deliver")
def generate_and_deliver_notifications_task() -> dict[str, int]:
    with SessionLocal() as db:
        created = generate_due_notifications(db)
        sent, failed = deliver_pending_telegram_notifications(db)
    return {"created": created, "sent": sent, "failed": failed}


@celery_app.task(name="maintenance.cleanup_expired_records")
def cleanup_expired_records_task() -> dict[str, int]:
    with SessionLocal() as db:
        return cleanup_expired_records(db)


@celery_app.task(name="maintenance.process_storage_deletion_outbox")
def process_storage_deletion_outbox_task() -> dict[str, int]:
    with SessionLocal() as db:
        return process_storage_deletion_outbox(db)


@celery_app.task(name="maintenance.retry_account_deletions")
def retry_account_deletions_task() -> dict[str, int]:
    with SessionLocal() as db:
        request_ids = list(
            db.scalars(
                select(AccountDeletionRequest.id)
                .where(AccountDeletionRequest.status == "pending_cancellation")
                .order_by(AccountDeletionRequest.id)
                .limit(50)
            )
        )
    requested = 0
    failed = 0
    for request_id in request_ids:
        with SessionLocal() as db:
            request = db.get(AccountDeletionRequest, request_id)
            if (
                not request
                or request.status != "pending_cancellation"
                or not request.provider_subscription_id
            ):
                continue
            mark_cancellation_requested(db, request)
            try:
                cancel_stripe_subscription(
                    request.provider_subscription_id, request.user_id
                )
            except BillingProviderError as exc:
                mark_cancellation_retry(db, request, exc)
                failed += 1
            else:
                requested += 1
    if failed:
        dispatch_operational_alert(
            "account_deletion_subscription_retry_failed",
            details={"failed_count": failed},
        )
    return {"requested": requested, "failed": failed}


@celery_app.task(name="maintenance.recover_stale_diagnosis_jobs")
def recover_stale_diagnosis_jobs_task() -> dict[str, int]:
    with SessionLocal() as db:
        recovery = recover_stale_diagnosis_jobs(db)
    task_ids = recovery["task_ids"]
    published = 0
    publish_failed = 0
    for job_id, task_id in task_ids.items():
        try:
            analyze_diagnosis_task.apply_async(args=[job_id], task_id=task_id)
            published += 1
        except Exception:
            publish_failed += 1
            with SessionLocal() as db:
                job = db.get(DiagnosisJob, job_id)
                if job and job.status == "queued" and job.celery_task_id == task_id:
                    job.celery_task_id = None
                    job.error_type = "queue_publish_failed"
                    job.error_message = (
                        "Не удалось повторно отправить фоновый анализ в очередь"
                    )
                    db.commit()
    failed = recovery["failed"]
    if failed:
        dispatch_operational_alert(
            "diagnosis_jobs_recovery_exhausted",
            details={"failed_count": len(failed), "sample_job_id": failed[0]},
        )
    if publish_failed:
        dispatch_operational_alert(
            "diagnosis_jobs_republish_failed",
            details={"failed_count": publish_failed},
        )
    return {
        "published": published,
        "publish_failed": publish_failed,
        "failed": len(failed),
    }

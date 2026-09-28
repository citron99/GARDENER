from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import (
    AccountActionToken,
    AIRequestLog,
    AuthSession,
    BillingEvent,
    DiagnosisJob,
    Garden,
    Plant,
    PlantPhoto,
    StorageDeletionOutbox,
    TelegramLinkToken,
    TelegramUpdate,
    User,
    UserNotification,
)
from app.services.maintenance_service import cleanup_expired_records, recover_stale_diagnosis_jobs


def test_cleanup_removes_only_expired_operational_records():
    now = datetime(2026, 7, 14, 12, tzinfo=UTC)
    old = now - timedelta(days=400)
    future = now + timedelta(days=30)

    with SessionLocal() as db:
        user = User(email="retention@example.com", name="Retention", password_hash="unused")
        db.add(user)
        db.flush()
        garden = Garden(user_id=user.id, name="Retention garden")
        db.add(garden)
        db.flush()
        plant = Plant(garden_id=garden.id, name="Retention plant")
        db.add(plant)
        db.flush()
        db.add_all([
            PlantPhoto(
                plant_id=plant.id, storage_key="retention/old.jpg",
                content_type="image/jpeg", size_bytes=100, created_at=old,
            ),
            PlantPhoto(
                plant_id=plant.id, storage_key="retention/fresh.jpg",
                content_type="image/jpeg", size_bytes=100, created_at=now,
            ),
        ])

        for suffix, created_at, completed_at in (("old", old, old), ("fresh", now, now)):
            db.add(DiagnosisJob(
                id=f"00000000-0000-0000-0000-{1 if suffix == 'old' else 2:012d}",
                user_id=user.id,
                plant_id=plant.id,
                status="succeeded",
                operation="create",
                payload={},
                created_at=created_at,
                completed_at=completed_at,
            ))
            db.add(AIRequestLog(
                user_id=user.id,
                request_id=f"request-{suffix}",
                provider="mock",
                model_name="mock",
                prompt_version="test",
                success=True,
                created_at=created_at,
            ))
            db.add(UserNotification(
                user_id=user.id,
                kind="reminder",
                title=suffix,
                body=suffix,
                event_at=created_at,
                deduplication_key=f"notification-{suffix}",
                created_at=created_at,
            ))
            db.add(TelegramUpdate(update_id=1 if suffix == "old" else 2, created_at=created_at))
            db.add(BillingEvent(
                provider_event_id=f"billing-{suffix}",
                event_type="test",
                created_at=created_at,
            ))

        db.add_all([
            TelegramLinkToken(user_id=user.id, token_hash="telegram-old", expires_at=old, created_at=old),
            TelegramLinkToken(user_id=user.id, token_hash="telegram-fresh", expires_at=future, created_at=now),
            AuthSession(
                id="10000000-0000-0000-0000-000000000001",
                user_id=user.id,
                refresh_token_hash="session-old",
                expires_at=old,
                created_at=old,
            ),
            AuthSession(
                id="10000000-0000-0000-0000-000000000002",
                user_id=user.id,
                refresh_token_hash="session-fresh",
                expires_at=future,
                created_at=now,
            ),
            AccountActionToken(
                id="20000000-0000-0000-0000-000000000001",
                user_id=user.id,
                purpose="verify_email",
                token_hash="account-old",
                expires_at=old,
                created_at=old,
            ),
            AccountActionToken(
                id="20000000-0000-0000-0000-000000000002",
                user_id=user.id,
                purpose="verify_email",
                token_hash="account-fresh",
                expires_at=future,
                created_at=now,
            ),
        ])
        db.commit()

        counts = cleanup_expired_records(db, now=now)

        assert counts | {
            "jobs": 1,
            "notifications": 1,
            "ai_logs": 1,
            "telegram_updates": 1,
            "telegram_tokens": 1,
            "billing_events": 1,
            "auth_sessions": 1,
            "account_tokens": 1,
            "unattached_photos": 1,
        } == counts
        for model in (
            DiagnosisJob,
            AIRequestLog,
            UserNotification,
            TelegramUpdate,
            BillingEvent,
            TelegramLinkToken,
            AuthSession,
            AccountActionToken,
        ):
            assert db.scalar(select(func.count()).select_from(model)) == 1
        assert db.scalar(select(func.count()).select_from(PlantPhoto)) == 1
        assert db.scalar(select(func.count()).select_from(StorageDeletionOutbox)) == 1


def test_recovery_reserves_expired_and_orphaned_jobs_and_fails_exhausted_attempts():
    now = datetime(2026, 7, 15, 12, tzinfo=UTC)
    old = now - timedelta(hours=1)
    future = now + timedelta(hours=1)
    with SessionLocal() as db:
        user = User(email="recovery@example.com", name="Recovery", password_hash="unused")
        db.add(user)
        db.flush()
        garden = Garden(user_id=user.id, name="Recovery garden")
        db.add(garden)
        db.flush()
        plant = Plant(garden_id=garden.id, name="Recovery plant")
        db.add(plant)
        db.flush()
        jobs = [
            DiagnosisJob(
                id="30000000-0000-0000-0000-000000000001", user_id=user.id, plant_id=plant.id,
                status="running", operation="create", payload={}, attempts=1,
                execution_token="expired-token", lease_expires_at=old, created_at=old,
            ),
            DiagnosisJob(
                id="30000000-0000-0000-0000-000000000002", user_id=user.id, plant_id=plant.id,
                status="running", operation="create", payload={}, attempts=3,
                execution_token="exhausted-token", lease_expires_at=old, created_at=old,
            ),
            DiagnosisJob(
                id="30000000-0000-0000-0000-000000000003", user_id=user.id, plant_id=plant.id,
                status="running", operation="create", payload={}, attempts=1,
                execution_token="fresh-token", lease_expires_at=future, created_at=old,
            ),
            DiagnosisJob(
                id="30000000-0000-0000-0000-000000000004", user_id=user.id, plant_id=plant.id,
                status="queued", operation="create", payload={}, attempts=0, created_at=old,
            ),
        ]
        db.add_all(jobs)
        db.commit()

        result = recover_stale_diagnosis_jobs(db, now=now)
        assert set(result["task_ids"]) == {jobs[0].id, jobs[3].id}
        assert result["failed"] == [jobs[1].id]
        db.expire_all()
        assert db.get(DiagnosisJob, jobs[0].id).status == "queued"
        assert db.get(DiagnosisJob, jobs[0].id).execution_token is None
        assert db.get(DiagnosisJob, jobs[0].id).celery_task_id == result["task_ids"][jobs[0].id]
        assert db.get(DiagnosisJob, jobs[1].id).status == "failed"
        assert db.get(DiagnosisJob, jobs[1].id).error_type == "lease_expired"
        assert db.get(DiagnosisJob, jobs[2].id).status == "running"
        assert db.get(DiagnosisJob, jobs[3].id).celery_task_id == result["task_ids"][jobs[3].id]

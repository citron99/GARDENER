from datetime import UTC, datetime, timedelta

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PlantPhoto, StorageDeletionOutbox
from app.services.storage_service import delete_storage_reference


def enqueue_photo_deletions(db: Session, photos: list[PlantPhoto]) -> int:
    for photo in photos:
        db.add(StorageDeletionOutbox(
            backend="s3" if photo.storage_key and settings.storage_backend == "s3" else "local",
            storage_key=photo.storage_key,
            file_path=photo.file_path,
        ))
    return len(photos)


def process_storage_deletion_outbox(
    db: Session,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> dict[str, int]:
    now = now or datetime.now(UTC)
    items = list(db.scalars(
        select(StorageDeletionOutbox)
        .where(
            StorageDeletionOutbox.status.in_(("pending", "retry")),
            or_(
                StorageDeletionOutbox.next_attempt_at.is_(None),
                StorageDeletionOutbox.next_attempt_at <= now,
            ),
        )
        .order_by(StorageDeletionOutbox.id)
        .limit(limit)
        .with_for_update(skip_locked=True)
    ))
    completed = 0
    failed = 0
    for item in items:
        item.attempts += 1
        try:
            delete_storage_reference(item.backend, item.storage_key, item.file_path)
        except Exception as exc:
            item.status = "retry"
            item.last_error = str(exc)[:300]
            item.next_attempt_at = now + timedelta(minutes=min(60, 2 ** min(item.attempts, 6)))
            failed += 1
        else:
            item.status = "completed"
            item.last_error = None
            item.next_attempt_at = None
            item.completed_at = now
            completed += 1
    db.commit()
    return {"completed": completed, "failed": failed}

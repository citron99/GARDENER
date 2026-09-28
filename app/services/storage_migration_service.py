import hashlib
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import PlantPhoto, StorageMigrationRecord
from app.services.storage_service import StorageError, _s3_client


def _local_photo(photo: PlantPhoto) -> tuple[Path, bytes]:
    if not photo.file_path:
        raise StorageError("У фотографии нет локального пути")
    path = Path(photo.file_path).resolve()
    try:
        path.relative_to(settings.upload_dir.resolve())
    except ValueError as exc:
        raise StorageError("Локальный путь находится вне UPLOAD_DIR") from exc
    try:
        return path, path.read_bytes()
    except OSError as exc:
        raise StorageError("Локальный файл фотографии недоступен") from exc


def _object_key(photo: PlantPhoto, path: Path, checksum: str) -> str:
    suffix = path.suffix.casefold() if path.suffix.casefold() in {".jpg", ".jpeg", ".png", ".webp"} else ""
    return f"legacy/{photo.plant_id}/{photo.id}-{checksum[:20]}{suffix}"


def _verify_object(client, key: str, size: int, checksum: str) -> None:
    metadata = client.head_object(Bucket=settings.s3_bucket, Key=key)
    if int(metadata.get("ContentLength", -1)) != size:
        raise StorageError("Размер загруженного S3-объекта не совпадает")
    if (metadata.get("Metadata") or {}).get("sha256") != checksum:
        raise StorageError("Checksum загруженного S3-объекта не совпадает")


def migrate_local_photos_to_s3(
    db: Session,
    *,
    execute: bool = False,
    verify_only: bool = False,
    client=None,
) -> dict[str, int | bool]:
    if execute and verify_only:
        raise ValueError("execute и verify_only нельзя использовать одновременно")
    if not settings.s3_bucket:
        raise StorageError("S3_BUCKET не настроен")
    client = client or _s3_client()
    photos = list(db.scalars(
        select(PlantPhoto).where(PlantPhoto.file_path.is_not(None)).order_by(PlantPhoto.id)
    ))
    report = {"eligible": len(photos), "planned": 0, "uploaded": 0, "verified": 0, "failed": 0}
    for photo in photos:
        try:
            path, content = _local_photo(photo)
            checksum = hashlib.sha256(content).hexdigest()
            key = _object_key(photo, path, checksum)
            record = db.scalar(select(StorageMigrationRecord).where(
                StorageMigrationRecord.photo_id == photo.id,
            ))
            if verify_only:
                if not record or record.status != "completed":
                    raise StorageError("Фотография ещё не была успешно мигрирована")
                _verify_object(client, record.storage_key, record.size_bytes, record.checksum_sha256)
                report["verified"] += 1
                continue
            if not execute:
                report["planned"] += 1
                continue
            if (
                record
                and record.status == "completed"
                and record.checksum_sha256 == checksum
                and record.size_bytes == len(content)
            ):
                _verify_object(client, record.storage_key, record.size_bytes, record.checksum_sha256)
                photo.storage_key = record.storage_key
                db.commit()
                report["verified"] += 1
                continue
            if record is None:
                record = StorageMigrationRecord(
                    photo_id=photo.id,
                    source_path=str(path),
                    storage_key=key,
                    checksum_sha256=checksum,
                    size_bytes=len(content),
                )
                db.add(record)
            record.source_path = str(path)
            record.storage_key = key
            record.checksum_sha256 = checksum
            record.size_bytes = len(content)
            record.status = "uploading"
            record.attempts = (record.attempts or 0) + 1
            record.last_error = None
            db.commit()
            client.put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=content,
                ContentType=photo.content_type,
                ServerSideEncryption="AES256",
                Metadata={"private": "true", "sha256": checksum},
            )
            _verify_object(client, key, len(content), checksum)
            photo.storage_key = key
            record.status = "completed"
            record.verified_at = datetime.now(UTC)
            db.commit()
            report["uploaded"] += 1
            report["verified"] += 1
        except Exception as exc:
            db.rollback()
            report["failed"] += 1
            if execute:
                record = db.scalar(select(StorageMigrationRecord).where(
                    StorageMigrationRecord.photo_id == photo.id,
                ))
                if record:
                    record.status = "failed"
                    record.last_error = str(exc)[:500]
                    db.commit()
    completed_count = len(list(db.scalars(select(StorageMigrationRecord.id).where(
        StorageMigrationRecord.photo_id.in_([photo.id for photo in photos]),
        StorageMigrationRecord.status == "completed",
    )))) if photos else 0
    report["remaining"] = len(photos) - completed_count
    report["ready_to_switch"] = report["failed"] == 0 and report["remaining"] == 0
    return report

import contextlib
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

from app.config import settings
from app.models import PlantPhoto


class StorageError(RuntimeError):
    pass


def _safe_local_path(key: str) -> Path:
    path = (settings.upload_dir / key).resolve()
    root = settings.upload_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StorageError("Некорректный ключ приватного объекта") from exc
    return path


def _s3_client():
    import boto3

    return boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint_url,
        region_name=settings.s3_region,
        aws_access_key_id=settings.s3_access_key_id,
        aws_secret_access_key=settings.s3_secret_access_key,
    )


def store_photo(
    plant_id: int, content: bytes, extension: str, content_type: str
) -> tuple[str, str | None]:
    key = f"{plant_id}/{uuid4().hex}{extension}"
    if settings.storage_backend == "s3":
        try:
            _s3_client().put_object(
                Bucket=settings.s3_bucket,
                Key=key,
                Body=content,
                ContentType=content_type,
                ServerSideEncryption="AES256",
                Metadata={"private": "true"},
            )
        except Exception as exc:
            raise StorageError("Не удалось сохранить фотографию") from exc
        return key, None
    path = _safe_local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return key, str(path)


def store_private_document(
    namespace: str,
    identifier: str,
    content: bytes,
    content_type: str = "application/pdf",
) -> tuple[str, str, str | None]:
    digest = sha256(content).hexdigest()
    key = f"private-documents/{namespace}/{identifier}/{digest}.pdf"
    if settings.storage_backend == "s3":
        try:
            from botocore.exceptions import ClientError

            client = _s3_client()
            try:
                existing = client.get_object(Bucket=settings.s3_bucket, Key=key)[
                    "Body"
                ].read()
            except ClientError as exc:
                code = str(exc.response.get("Error", {}).get("Code", ""))
                if code not in {"NoSuchKey", "NotFound", "404"}:
                    raise
                existing = None
            if existing is not None:
                if existing != content:
                    raise StorageError("Ключ приватного документа уже занят")
            else:
                client.put_object(
                    Bucket=settings.s3_bucket,
                    Key=key,
                    Body=content,
                    ContentType=content_type,
                    ServerSideEncryption="AES256",
                    Metadata={"private": "true", "sha256": digest},
                )
        except StorageError:
            raise
        except Exception as exc:
            raise StorageError("Не удалось сохранить приватный документ") from exc
        return "s3", key, None

    path = _safe_local_path(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        try:
            if path.read_bytes() != content:
                raise StorageError("Ключ приватного документа уже занят")
        except OSError as exc:
            raise StorageError("Не удалось проверить приватный документ") from exc
    else:
        try:
            path.write_bytes(content)
        except OSError as exc:
            raise StorageError("Не удалось сохранить приватный документ") from exc
    return "local", key, str(path)


def read_private_document(
    backend: str,
    storage_key: str | None,
    file_path: str | None,
) -> bytes:
    if backend == "s3" and storage_key:
        try:
            return (
                _s3_client()
                .get_object(Bucket=settings.s3_bucket, Key=storage_key)["Body"]
                .read()
            )
        except Exception as exc:
            raise StorageError("Не удалось прочитать приватный документ") from exc
    if not file_path:
        raise StorageError("Приватный документ не найден")
    path = Path(file_path).resolve()
    try:
        path.relative_to(settings.upload_dir.resolve())
    except ValueError as exc:
        raise StorageError("Некорректный путь приватного документа") from exc
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StorageError("Не удалось прочитать приватный документ") from exc


def read_photo(photo: PlantPhoto) -> bytes:
    if settings.storage_backend == "s3" and photo.storage_key:
        try:
            return (
                _s3_client()
                .get_object(Bucket=settings.s3_bucket, Key=photo.storage_key)["Body"]
                .read()
            )
        except Exception as exc:
            raise StorageError("Не удалось прочитать фотографию") from exc
    if not photo.file_path:
        raise StorageError("Фотография не найдена")
    path = Path(photo.file_path).resolve()
    root = settings.upload_dir.resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise StorageError("Некорректный путь фотографии") from exc
    try:
        return path.read_bytes()
    except OSError as exc:
        raise StorageError("Не удалось прочитать фотографию") from exc


def photo_download(photo: PlantPhoto) -> str | Path:
    if settings.storage_backend == "s3" and photo.storage_key:
        try:
            return _s3_client().generate_presigned_url(
                "get_object",
                Params={
                    "Bucket": settings.s3_bucket,
                    "Key": photo.storage_key,
                    "ResponseContentType": photo.content_type,
                },
                ExpiresIn=settings.s3_presigned_url_seconds,
            )
        except Exception as exc:
            raise StorageError("Не удалось создать временную ссылку") from exc
    if not photo.file_path:
        raise StorageError("Фотография не найдена")
    path = Path(photo.file_path).resolve()
    try:
        path.relative_to(settings.upload_dir.resolve())
    except ValueError as exc:
        raise StorageError("Некорректный путь фотографии") from exc
    if not path.is_file():
        raise StorageError("Фотография не найдена")
    return path


def delete_photo(photo: PlantPhoto) -> None:
    delete_storage_reference(
        settings.storage_backend, photo.storage_key, photo.file_path
    )


def delete_storage_reference(
    backend: str,
    storage_key: str | None,
    file_path: str | None,
) -> None:
    """Delete a stored object using the backend captured when the outbox row was created."""
    if backend == "s3" and storage_key:
        try:
            _s3_client().delete_object(Bucket=settings.s3_bucket, Key=storage_key)
        except Exception as exc:
            raise StorageError("Не удалось удалить фотографию") from exc
        return
    if file_path:
        path = Path(file_path).resolve()
        try:
            path.relative_to(settings.upload_dir.resolve())
        except ValueError as exc:
            raise StorageError("Некорректный путь фотографии") from exc
        path.unlink(missing_ok=True)
        with contextlib.suppress(OSError):
            path.parent.rmdir()

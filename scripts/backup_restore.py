from __future__ import annotations

import argparse
import hashlib
import json
import os
import secrets
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import boto3
from sqlalchemy.engine import make_url

MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class PostgresTarget:
    host: str
    port: int
    database: str
    username: str
    password: str | None

    @property
    def confirmation_name(self) -> str:
        return f"{self.host}:{self.port}/{self.database}"


def parse_postgres_target(database_url: str) -> PostgresTarget:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("DATABASE_URL must use PostgreSQL")
    if not url.host or not url.database or not url.username:
        raise ValueError("DATABASE_URL must include host, database, and username")
    return PostgresTarget(
        host=url.host,
        port=url.port or 5432,
        database=url.database,
        username=url.username,
        password=url.password,
    )


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _postgres_env(target: PostgresTarget) -> dict[str, str]:
    environment = os.environ.copy()
    if target.password:
        environment["PGPASSWORD"] = target.password
    else:
        environment.pop("PGPASSWORD", None)
    return environment


def _connection_arguments(target: PostgresTarget) -> list[str]:
    return [
        "--host", target.host,
        "--port", str(target.port),
        "--username", target.username,
        "--dbname", target.database,
    ]


def create_postgres_backup(
    database_url: str,
    output_dir: Path,
    *,
    pg_dump: str = "pg_dump",
    now: datetime | None = None,
) -> tuple[Path, Path]:
    target = parse_postgres_target(database_url)
    created_at = (now or datetime.now(UTC)).astimezone(UTC)
    output_dir.mkdir(parents=True, exist_ok=True)
    timestamp = created_at.strftime("%Y%m%dT%H%M%SZ")
    backup_path = output_dir / f"{target.database}-{timestamp}-{secrets.token_hex(4)}.dump"
    partial_path = backup_path.with_suffix(".dump.partial")
    manifest_path = backup_path.with_suffix(".dump.manifest.json")
    partial_manifest = manifest_path.with_suffix(".json.partial")

    command = [
        pg_dump,
        "--format=custom",
        "--compress=9",
        "--no-owner",
        "--no-acl",
        "--file", str(partial_path),
        *_connection_arguments(target),
    ]
    try:
        subprocess.run(command, check=True, env=_postgres_env(target))
        if not partial_path.is_file() or partial_path.stat().st_size == 0:
            raise RuntimeError("pg_dump did not create a non-empty backup")
        os.replace(partial_path, backup_path)
        manifest = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "created_at": created_at.isoformat(),
            "database": target.database,
            "host": target.host,
            "port": target.port,
            "format": "postgresql-custom",
            "backup_file": backup_path.name,
            "size_bytes": backup_path.stat().st_size,
            "sha256": _sha256(backup_path),
        }
        partial_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(partial_manifest, manifest_path)
    except BaseException:
        partial_path.unlink(missing_ok=True)
        partial_manifest.unlink(missing_ok=True)
        raise
    return backup_path, manifest_path


def verify_postgres_backup(backup_path: Path) -> dict[str, Any]:
    manifest_path = backup_path.with_suffix(".dump.manifest.json")
    if not backup_path.is_file() or not manifest_path.is_file():
        raise ValueError("Backup and its manifest must both exist")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise ValueError("Backup manifest is unreadable") from exc
    if manifest.get("schema_version") != MANIFEST_SCHEMA_VERSION:
        raise ValueError("Unsupported backup manifest version")
    if manifest.get("backup_file") != backup_path.name:
        raise ValueError("Backup filename does not match its manifest")
    if manifest.get("size_bytes") != backup_path.stat().st_size:
        raise ValueError("Backup size does not match its manifest")
    if not secrets.compare_digest(str(manifest.get("sha256", "")), _sha256(backup_path)):
        raise ValueError("Backup checksum does not match its manifest")
    return manifest


def restore_postgres_backup(
    database_url: str,
    backup_path: Path,
    *,
    confirm_target: str,
    pg_restore: str = "pg_restore",
) -> dict[str, Any]:
    target = parse_postgres_target(database_url)
    if not secrets.compare_digest(confirm_target, target.confirmation_name):
        raise ValueError(
            "Destructive restore refused: --confirm-target must equal "
            f"{target.confirmation_name}"
        )
    manifest = verify_postgres_backup(backup_path)
    command = [
        pg_restore,
        "--clean",
        "--if-exists",
        "--no-owner",
        "--no-acl",
        "--exit-on-error",
        *_connection_arguments(target),
        str(backup_path),
    ]
    subprocess.run(command, check=True, env=_postgres_env(target))
    return manifest


def audit_s3_bucket(client: Any, bucket: str, *, sample_size: int = 10) -> dict[str, Any]:
    checks: dict[str, dict[str, Any]] = {}

    def record(name: str, operation: Any, validator: Any) -> Any:
        try:
            response = operation()
            passed, detail = validator(response)
            checks[name] = {"passed": bool(passed), "detail": detail}
            return response
        except Exception as exc:  # boto3 exposes provider-specific exception types
            checks[name] = {"passed": False, "detail": type(exc).__name__}
            return None

    record(
        "versioning",
        lambda: client.get_bucket_versioning(Bucket=bucket),
        lambda value: (value.get("Status") == "Enabled", value.get("Status", "Disabled")),
    )
    def validate_encryption(value: dict[str, Any]) -> tuple[bool, str]:
        rules = value.get("ServerSideEncryptionConfiguration", {}).get("Rules", [])
        algorithms = {
            rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm")
            for rule in rules
        }
        allowed = {"AES256", "aws:kms", "aws:kms:dsse"}
        valid = bool(algorithms & allowed)
        return valid, ",".join(sorted(algorithm for algorithm in algorithms if algorithm)) or "missing"

    record(
        "default_encryption",
        lambda: client.get_bucket_encryption(Bucket=bucket),
        validate_encryption,
    )

    def validate_public_access(value: dict[str, Any]) -> tuple[bool, str]:
        config = value.get("PublicAccessBlockConfiguration", {})
        fields = ("BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets")
        return all(config.get(field) is True for field in fields), "all-blocked" if all(
            config.get(field) is True for field in fields
        ) else "incomplete"

    record(
        "public_access_block",
        lambda: client.get_public_access_block(Bucket=bucket),
        validate_public_access,
    )
    record(
        "lifecycle",
        lambda: client.get_bucket_lifecycle_configuration(Bucket=bucket),
        lambda value: (
            any(rule.get("Status") == "Enabled" for rule in value.get("Rules", [])),
            "enabled-rule" if any(
                rule.get("Status") == "Enabled" for rule in value.get("Rules", [])
            ) else "missing",
        ),
    )
    listing = record(
        "object_listing",
        lambda: client.list_objects_v2(Bucket=bucket, MaxKeys=max(1, min(sample_size, 100))),
        lambda value: (True, f"sampled={len(value.get('Contents', []))}"),
    )
    sampled_objects = 0
    metadata_error = None
    if listing:
        try:
            for item in listing.get("Contents", []):
                client.head_object(Bucket=bucket, Key=item["Key"])
                sampled_objects += 1
        except Exception as exc:  # provider-specific errors should remain a structured audit result
            metadata_error = type(exc).__name__
    checks["object_metadata"] = {
        "passed": listing is not None and metadata_error is None,
        "detail": metadata_error or f"verified={sampled_objects}",
    }
    return {
        "bucket": bucket,
        "ready": all(check["passed"] for check in checks.values()),
        "checks": checks,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AI Garden backup and restore operations")
    subparsers = parser.add_subparsers(dest="command", required=True)

    backup = subparsers.add_parser("postgres-backup")
    backup.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    backup.add_argument("--output-dir", type=Path, required=True)
    backup.add_argument("--pg-dump", default="pg_dump")

    restore = subparsers.add_parser("postgres-restore")
    restore.add_argument("--database-url", default=os.getenv("DATABASE_URL"))
    restore.add_argument("--backup", type=Path, required=True)
    restore.add_argument("--confirm-target", required=True)
    restore.add_argument("--pg-restore", default="pg_restore")

    s3 = subparsers.add_parser("s3-audit")
    s3.add_argument("--bucket", default=os.getenv("S3_BUCKET"))
    s3.add_argument("--endpoint-url", default=os.getenv("S3_ENDPOINT_URL"))
    s3.add_argument("--region", default=os.getenv("S3_REGION", "eu-central-1"))
    s3.add_argument("--sample-size", type=int, default=10)
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    if args.command in {"postgres-backup", "postgres-restore"} and not args.database_url:
        raise SystemExit("DATABASE_URL or --database-url is required")
    if args.command == "postgres-backup":
        backup, manifest = create_postgres_backup(
            args.database_url,
            args.output_dir,
            pg_dump=args.pg_dump,
        )
        print(json.dumps({"backup": str(backup), "manifest": str(manifest)}))
        return 0
    if args.command == "postgres-restore":
        manifest = restore_postgres_backup(
            args.database_url,
            args.backup,
            confirm_target=args.confirm_target,
            pg_restore=args.pg_restore,
        )
        print(json.dumps({"restored": str(args.backup), "sha256": manifest["sha256"]}))
        return 0

    if not args.bucket:
        raise SystemExit("S3_BUCKET or --bucket is required")
    client = boto3.client("s3", endpoint_url=args.endpoint_url, region_name=args.region)
    report = audit_s3_bucket(client, args.bucket, sample_size=args.sample_size)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["ready"] else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

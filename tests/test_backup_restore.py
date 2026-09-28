import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from scripts.backup_restore import (
    audit_s3_bucket,
    create_postgres_backup,
    parse_postgres_target,
    restore_postgres_backup,
    verify_postgres_backup,
)

DATABASE_URL = "postgresql+psycopg://gardener:private-password@db:5432/gardener"


def test_postgres_target_rejects_non_postgres_and_hides_password():
    with pytest.raises(ValueError, match="PostgreSQL"):
        parse_postgres_target("sqlite:///garden.db")
    target = parse_postgres_target(DATABASE_URL)
    assert target.confirmation_name == "db:5432/gardener"
    assert "private-password" not in target.confirmation_name


def test_backup_is_atomic_and_has_checksum_manifest(tmp_path, monkeypatch):
    captured = {}

    def fake_run(command, *, check, env):
        captured.update(command=command, check=check, env=env)
        output = command[command.index("--file") + 1]
        with open(output, "wb") as target:
            target.write(b"valid-custom-postgres-backup")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    backup, manifest = create_postgres_backup(
        DATABASE_URL,
        tmp_path,
        now=datetime(2026, 7, 15, 12, 30, tzinfo=UTC),
    )
    metadata = verify_postgres_backup(backup)
    assert backup.is_file() and manifest.is_file()
    assert metadata["backup_file"] == backup.name
    assert metadata["size_bytes"] == backup.stat().st_size
    assert len(metadata["sha256"]) == 64
    assert captured["env"]["PGPASSWORD"] == "private-password"
    assert "private-password" not in " ".join(captured["command"])
    assert not list(tmp_path.glob("*.partial"))
    assert "private-password" not in manifest.read_text(encoding="utf-8")


def test_restore_requires_exact_confirmation_and_verified_checksum(tmp_path, monkeypatch):
    def _write_backup_file(command):
        with open(command[command.index("--file") + 1], "wb") as handle:
            return handle.write(b"backup")

    monkeypatch.setattr(subprocess, "run", lambda command, **kwargs: (
        _write_backup_file(command)
        if "--file" in command else subprocess.CompletedProcess(command, 0)
    ))
    backup, _manifest = create_postgres_backup(DATABASE_URL, tmp_path)
    with pytest.raises(ValueError, match="confirm-target"):
        restore_postgres_backup(DATABASE_URL, backup, confirm_target="wrong")

    backup.write_bytes(b"tampered")
    with pytest.raises(ValueError, match=r"size|checksum"):
        restore_postgres_backup(
            DATABASE_URL,
            backup,
            confirm_target="db:5432/gardener",
        )


def test_restore_uses_safe_pg_restore_arguments(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, *, check, env):
        calls.append((command, env))
        if "--file" in command:
            with open(command[command.index("--file") + 1], "wb") as target:
                target.write(b"backup")
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    backup, _manifest = create_postgres_backup(DATABASE_URL, tmp_path)
    metadata = restore_postgres_backup(
        DATABASE_URL,
        backup,
        confirm_target="db:5432/gardener",
    )
    command, environment = calls[-1]
    assert command[:6] == [
        "pg_restore", "--clean", "--if-exists", "--no-owner", "--no-acl", "--exit-on-error",
    ]
    assert "private-password" not in " ".join(command)
    assert environment["PGPASSWORD"] == "private-password"
    assert metadata["sha256"] == verify_postgres_backup(backup)["sha256"]


class HealthyS3:
    def get_bucket_versioning(self, **_kwargs):
        return {"Status": "Enabled"}

    def get_bucket_encryption(self, **_kwargs):
        return {"ServerSideEncryptionConfiguration": {"Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]}}

    def get_public_access_block(self, **_kwargs):
        return {"PublicAccessBlockConfiguration": {
            "BlockPublicAcls": True,
            "IgnorePublicAcls": True,
            "BlockPublicPolicy": True,
            "RestrictPublicBuckets": True,
        }}

    def get_bucket_lifecycle_configuration(self, **_kwargs):
        return {"Rules": [{"Status": "Enabled"}]}

    def list_objects_v2(self, **_kwargs):
        return {"Contents": [{"Key": "photos/example.webp"}]}

    def head_object(self, **_kwargs):
        return {"ContentLength": 123}


def test_s3_audit_requires_all_protection_controls():
    report = audit_s3_bucket(HealthyS3(), "private-garden")
    assert report["ready"] is True
    assert report["checks"]["object_metadata"]["detail"] == "verified=1"
    json.dumps(report)

    class UnsafeS3(HealthyS3):
        def get_bucket_versioning(self, **_kwargs):
            return {}

        def get_bucket_lifecycle_configuration(self, **_kwargs):
            raise RuntimeError("not configured")

    unsafe = audit_s3_bucket(UnsafeS3(), "unsafe-garden")
    assert unsafe["ready"] is False
    assert unsafe["checks"]["versioning"]["passed"] is False
    assert unsafe["checks"]["lifecycle"]["detail"] == "RuntimeError"

    class UnreadableObjectS3(HealthyS3):
        def head_object(self, **_kwargs):
            raise PermissionError("head forbidden")

    unreadable = audit_s3_bucket(UnreadableObjectS3(), "unreadable-garden")
    assert unreadable["ready"] is False
    assert unreadable["checks"]["object_metadata"]["detail"] == "PermissionError"


def test_backups_are_excluded_and_ci_audits_both_locks():
    assert "backups/" in Path(".gitignore").read_text(encoding="utf-8").splitlines()
    assert "backups" in Path(".dockerignore").read_text(encoding="utf-8").splitlines()
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "pip-audit -r requirements.lock" in workflow
    assert "pip-audit -r requirements-dev.lock" in workflow

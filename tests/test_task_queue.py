from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from app.config import settings
from app.database import SessionLocal
from app.models import Partner, PartnerInvoice, PartnerInvoiceDelivery, User
from app.tasks import celery_app


def test_celery_uses_json_late_ack_and_time_limits():
    assert celery_app.conf.broker_url == settings.celery_broker_url
    assert celery_app.conf.result_backend == settings.celery_result_backend
    assert celery_app.conf.task_serializer == "json"
    assert celery_app.conf.accept_content == ["json"]
    assert celery_app.conf.task_acks_late is True
    assert celery_app.conf.task_reject_on_worker_lost is True
    assert celery_app.conf.task_time_limit == settings.celery_task_timeout_seconds
    assert celery_app.conf.broker_transport_options["visibility_timeout"] == 600
    assert (
        celery_app.conf.beat_schedule["dispatch-pending-invoice-deliveries"]["task"]
        == "maintenance.dispatch_pending_invoice_deliveries"
    )


def test_invoice_delivery_claim_prevents_duplicate_email(monkeypatch):
    from app.services import invoice_delivery_service

    with SessionLocal() as db:
        admin = User(
            email="queue-admin@example.test",
            name="Queue Admin",
            password_hash="not-used",
            is_admin=True,
        )
        partner = Partner(name="Queue test partner", website_url="https://example.test")
        db.add_all([admin, partner])
        db.flush()
        invoice = PartnerInvoice(
            invoice_number="AG-2026-00000001",
            partner_id=partner.id,
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            due_at=datetime.now(UTC) + timedelta(days=14),
            customer_snapshot={"email": "billing@example.test"},
        )
        db.add(invoice)
        db.flush()
        delivery = PartnerInvoiceDelivery(
            invoice_id=invoice.id,
            requested_by_admin_id=admin.id,
            attempt_number=1,
            recipient="billing@example.test",
            status="pending",
        )
        db.add(delivery)
        db.commit()
        delivery_id = delivery.id
        invoice_id = invoice.id

    sent = []
    monkeypatch.setattr(
        invoice_delivery_service,
        "load_verified_invoice_pdf",
        lambda _invoice, _settings: b"%PDF-test",
    )
    monkeypatch.setattr(
        invoice_delivery_service,
        "send_invoice_email",
        lambda invoice, document, _settings: (
            sent.append((invoice.id, document)) or "logged"
        ),
    )

    assert invoice_delivery_service.process_invoice_delivery(delivery_id) == "logged"
    assert invoice_delivery_service.process_invoice_delivery(delivery_id) is None
    assert sent == [(invoice_id, b"%PDF-test")]
    with SessionLocal() as db:
        stored = db.get(PartnerInvoiceDelivery, delivery_id)
        assert stored.status == "logged"
        assert stored.completed_at is not None


def test_production_compose_contains_worker_and_persistent_redis():
    compose = Path("compose.prod.yaml").read_text(encoding="utf-8")
    assert "DIAGNOSIS_EXECUTION_MODE: celery" in compose
    assert '"celery", "-A", "app.tasks.celery_app", "worker"' in compose
    assert '"celery", "-A", "app.tasks.celery_app", "beat"' in compose
    assert 'command: ["alembic", "upgrade", "head"]' in compose
    assert "condition: service_completed_successfully" in compose
    assert "environment: *runtime-environment" in compose
    assert "EMAIL_DELIVERY_MODE: smtp" in compose
    assert "STORAGE_BACKEND: s3" in compose
    assert "pgvector/pgvector:0.8.2-pg17" in compose
    assert "redis:7.4-alpine" in compose
    assert '"--maxmemory-policy", "noeviction"' in compose
    assert "redis_data:/data" in compose
    assert "caddy:2.10-alpine" in compose
    assert 'max-size: "10m"' in compose
    assert "mem_limit:" in compose
    assert "TRUSTED_HOSTS:" in compose
    caddyfile = Path("Caddyfile").read_text(encoding="utf-8")
    assert "request_body" in caddyfile
    assert "reverse_proxy api:8000" in caddyfile


def test_docker_image_contains_web_and_ci_runs_smoke_test():
    dockerfile = Path("Dockerfile").read_text(encoding="utf-8")
    workflow = Path(".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "COPY web ./web" in dockerfile
    assert "--require-hashes -r requirements.lock" in dockerfile
    assert "/health/ready" in dockerfile
    assert "docker build -t ai-garden:smoke" in workflow
    assert "curl --fail --silent http://127.0.0.1:18000/health" in workflow
    assert "npm run test:e2e" in workflow
    assert "playwright install --with-deps chromium" in workflow
    assert "pip install --require-hashes -r requirements-dev.lock" in workflow
    lockfile = Path("requirements.lock").read_text(encoding="utf-8")
    assert "--hash=sha256:" in lockfile
    assert "openai==" in lockfile


def test_recovery_task_republishes_reserved_jobs_and_alerts_on_exhaustion(monkeypatch):
    from app import tasks

    published = []
    alerts = []
    monkeypatch.setattr(
        tasks,
        "recover_stale_diagnosis_jobs",
        lambda _db: {
            "task_ids": {"job-1": "task-1"},
            "failed": ["job-exhausted"],
        },
    )
    monkeypatch.setattr(
        tasks.analyze_diagnosis_task,
        "apply_async",
        lambda *, args, task_id: published.append((args, task_id)),
    )
    monkeypatch.setattr(
        tasks,
        "dispatch_operational_alert",
        lambda event, **kwargs: alerts.append((event, kwargs)) or True,
    )

    result = tasks.recover_stale_diagnosis_jobs_task.run()

    assert result == {"published": 1, "publish_failed": 0, "failed": 1}
    assert published == [(["job-1"], "task-1")]
    assert alerts[0][0] == "diagnosis_jobs_recovery_exhausted"

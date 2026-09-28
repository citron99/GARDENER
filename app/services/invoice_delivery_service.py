from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update

from app.config import settings
from app.database import SessionLocal
from app.models import AdminAuditLog, PartnerInvoice, PartnerInvoiceDelivery
from app.services.invoice_service import (
    InvoiceDocumentError,
    load_verified_invoice_pdf,
    send_invoice_email,
)


def process_invoice_delivery(delivery_id: int) -> str | None:
    """Claim and deliver one invoice exactly once per persisted attempt."""
    with SessionLocal() as db:
        claimed = db.execute(
            update(PartnerInvoiceDelivery)
            .where(
                PartnerInvoiceDelivery.id == delivery_id,
                PartnerInvoiceDelivery.status == "pending",
            )
            .values(status="sending", error_type=None)
        )
        if claimed.rowcount != 1:
            db.rollback()
            return None
        db.commit()

    try:
        with SessionLocal() as db:
            delivery = db.get(PartnerInvoiceDelivery, delivery_id)
            if not delivery or delivery.status != "sending":
                return None
            invoice = db.get(PartnerInvoice, delivery.invoice_id)
            if not invoice:
                raise InvoiceDocumentError("Счёт для отправки не найден")
            document = load_verified_invoice_pdf(invoice, settings)
            final_status = send_invoice_email(invoice, document, settings)
            invoice_id = invoice.id
            invoice_number = invoice.invoice_number
            admin_id = delivery.requested_by_admin_id
            recipient = delivery.recipient
    except Exception as exc:
        with SessionLocal() as db:
            delivery = db.get(PartnerInvoiceDelivery, delivery_id)
            if delivery and delivery.status == "sending":
                delivery.status = "failed"
                delivery.error_type = type(exc).__name__[:120]
                delivery.completed_at = datetime.now(UTC)
                db.add(
                    AdminAuditLog(
                        admin_user_id=delivery.requested_by_admin_id,
                        action="partner_invoice.send_failed",
                        target_type="partner_invoice",
                        target_id=str(delivery.invoice_id),
                        details={
                            "delivery_id": delivery.id,
                            "error_type": delivery.error_type,
                        },
                    )
                )
                db.commit()
        return "failed"

    with SessionLocal() as db:
        delivery = db.get(PartnerInvoiceDelivery, delivery_id)
        if not delivery or delivery.status != "sending":
            return None
        delivery.status = final_status
        delivery.completed_at = datetime.now(UTC)
        db.add(
            AdminAuditLog(
                admin_user_id=admin_id,
                action="partner_invoice.send",
                target_type="partner_invoice",
                target_id=str(invoice_id),
                details={
                    "invoice_number": invoice_number,
                    "delivery_id": delivery.id,
                    "status": final_status,
                    "recipient": recipient,
                },
            )
        )
        db.commit()
    return final_status


def dispatch_invoice_delivery(delivery_id: int) -> str | None:
    if settings.diagnosis_execution_mode == "celery":
        from app.tasks import send_partner_invoice_delivery_task

        send_partner_invoice_delivery_task.apply_async(args=[delivery_id])
        return None
    return process_invoice_delivery(delivery_id)


def pending_invoice_delivery_ids(*, limit: int = 100) -> list[int]:
    """Return persisted outbox records for safe periodic republishing."""
    cutoff = datetime.now(UTC) - timedelta(seconds=30)
    with SessionLocal() as db:
        return list(
            db.scalars(
                select(PartnerInvoiceDelivery.id)
                .where(
                    PartnerInvoiceDelivery.status == "pending",
                    PartnerInvoiceDelivery.created_at <= cutoff,
                )
                .order_by(PartnerInvoiceDelivery.id)
                .limit(limit)
            )
        )


def mark_stale_invoice_deliveries_unknown(*, limit: int = 100) -> int:
    cutoff = datetime.now(UTC) - timedelta(minutes=15)
    with SessionLocal() as db:
        stale_ids = list(
            db.scalars(
                select(PartnerInvoiceDelivery.id)
                .where(
                    PartnerInvoiceDelivery.status == "sending",
                    PartnerInvoiceDelivery.created_at <= cutoff,
                )
                .order_by(PartnerInvoiceDelivery.id)
                .limit(limit)
            )
        )
        if not stale_ids:
            return 0
        db.execute(
            update(PartnerInvoiceDelivery)
            .where(
                PartnerInvoiceDelivery.id.in_(stale_ids),
                PartnerInvoiceDelivery.status == "sending",
            )
            .values(
                status="unknown",
                error_type="DeliveryWorkerInterrupted",
                completed_at=datetime.now(UTC),
            )
        )
        db.commit()
        return len(stale_ids)

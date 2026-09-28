from __future__ import annotations

import json
import logging
import smtplib
import ssl
from collections import Counter
from dataclasses import dataclass
from datetime import UTC
from email.message import EmailMessage
from hashlib import sha256
from hmac import compare_digest
from io import BytesIO
from pathlib import Path

from reportlab import Version as REPORTLAB_VERSION
from reportlab.lib.pagesizes import A4
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas

from app.config import Settings
from app.models import Partner, PartnerInvoice
from app.services.storage_service import StorageError, read_private_document

DOCUMENT_VERSION = "b2b-invoice-v1"
CANCELLATION_DOCUMENT_VERSION = "b2b-invoice-cancellation-v1"
logger = logging.getLogger(__name__)


class InvoiceDocumentError(ValueError):
    pass


@dataclass(frozen=True)
class InvoiceSnapshots:
    issuer: dict[str, str | None]
    customer: dict[str, str | None]
    line_items: list[dict[str, int | str]]


def _clean(value: str | None) -> str | None:
    normalized = (value or "").strip()
    return normalized or None


def build_invoice_snapshots(
    app_settings: Settings,
    partner: Partner,
    *,
    monthly_fee_cents: int,
    lead_fee_amounts: list[int],
) -> InvoiceSnapshots:
    issuer = {
        "name": _clean(app_settings.b2b_invoice_issuer_name),
        "registration_number": _clean(
            app_settings.b2b_invoice_issuer_registration_number
        ),
        "vat_number": _clean(app_settings.b2b_invoice_issuer_vat_number),
        "address": _clean(app_settings.b2b_invoice_issuer_address),
        "email": _clean(app_settings.b2b_invoice_issuer_email),
        "bank_name": _clean(app_settings.b2b_invoice_bank_name),
        "iban": _clean(app_settings.b2b_invoice_iban),
        "swift": _clean(app_settings.b2b_invoice_swift),
    }
    customer = {
        "name": _clean(partner.legal_name),
        "registration_number": _clean(partner.registration_number),
        "vat_number": _clean(partner.vat_number),
        "address": _clean(partner.billing_address),
        "email": _clean(partner.billing_email),
    }
    required_issuer = ("name", "registration_number", "address", "email", "iban")
    required_customer = ("name", "registration_number", "address", "email")
    missing = [f"issuer.{key}" for key in required_issuer if not issuer[key]]
    missing.extend(f"customer.{key}" for key in required_customer if not customer[key])
    if missing:
        raise InvoiceDocumentError(
            "Не заполнены реквизиты счёта: " + ", ".join(missing)
        )

    line_items: list[dict[str, int | str]] = []
    if monthly_fee_cents:
        line_items.append(
            {
                "code": "monthly_fee",
                "description": "Partner platform monthly fee",
                "quantity": 1,
                "unit_price_cents": monthly_fee_cents,
                "total_cents": monthly_fee_cents,
            }
        )
    for unit_price, quantity in sorted(Counter(lead_fee_amounts).items()):
        line_items.append(
            {
                "code": "confirmed_leads",
                "description": "Verified partner conversions",
                "quantity": quantity,
                "unit_price_cents": unit_price,
                "total_cents": unit_price * quantity,
            }
        )
    return InvoiceSnapshots(issuer=issuer, customer=customer, line_items=line_items)


def _font_path(app_settings: Settings) -> Path:
    candidates = [
        app_settings.b2b_invoice_font_path,
        Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
        Path("C:/Windows/Fonts/arial.ttf"),
        Path("C:/Windows/Fonts/calibri.ttf"),
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_file():
            return Path(candidate)
    raise InvoiceDocumentError(
        "Не найден Unicode-шрифт для PDF; задайте B2B_INVOICE_FONT_PATH"
    )


def _font_name(app_settings: Settings) -> str:
    path = _font_path(app_settings)
    name = "AI-Garden-Invoice-Font"
    if name not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont(name, str(path)))
    return name


def invoice_pdf_generator_version(app_settings: Settings) -> str:
    font_digest = sha256(_font_path(app_settings).read_bytes()).hexdigest()[:16]
    return f"reportlab-{REPORTLAB_VERSION};font-{font_digest}"


def _money(cents: int, currency: str) -> str:
    return f"{cents / 100:,.2f} {currency.upper()}"


def _party_lines(title: str, snapshot: dict[str, str | None]) -> list[str]:
    lines = [title, str(snapshot["name"])]
    lines.append(f"Registration no.: {snapshot['registration_number']}")
    if snapshot.get("vat_number"):
        lines.append(f"VAT no.: {snapshot['vat_number']}")
    lines.extend((str(snapshot["address"]), str(snapshot["email"])))
    if snapshot.get("bank_name"):
        lines.append(f"Bank: {snapshot['bank_name']}")
    if snapshot.get("iban"):
        lines.append(f"IBAN: {snapshot['iban']}")
    if snapshot.get("swift"):
        lines.append(f"SWIFT/BIC: {snapshot['swift']}")
    return lines


def render_invoice_pdf(invoice: PartnerInvoice, app_settings: Settings) -> bytes:
    if not all(
        (
            invoice.invoice_number,
            invoice.issuer_snapshot,
            invoice.customer_snapshot,
            invoice.line_items is not None,
            invoice.document_version,
        )
    ):
        raise InvoiceDocumentError(
            "Для старого счёта отсутствует неизменяемый снимок документа"
        )
    font = _font_name(app_settings)
    buffer = BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1, pageCompression=1)
    pdf.setTitle(f"Invoice {invoice.invoice_number}")
    pdf.setAuthor(str(invoice.issuer_snapshot["name"]))
    pdf.setCreator("AI Garden B2B billing")
    page_number = 0

    def draw_footer() -> None:
        pdf.setFont(font, 7)
        pdf.drawString(48, 52, f"Document version: {invoice.document_version}")
        pdf.drawRightString(page_width - 48, 52, f"Page {page_number}")
        pdf.drawString(
            48,
            38,
            "Operational B2B invoice. Accounting and tax compliance must be reviewed "
            "for the applicable jurisdiction.",
        )

    def finish_page() -> None:
        draw_footer()
        pdf.showPage()

    def draw_table_header(*, continuation: bool) -> float:
        nonlocal page_number
        page_number += 1
        if continuation:
            pdf.setFont(font, 16)
            pdf.drawString(48, page_height - 58, "INVOICE — CONTINUED")
            pdf.setFont(font, 9)
            pdf.drawRightString(
                page_width - 48, page_height - 52, str(invoice.invoice_number)
            )
            table_y = page_height - 92
        else:
            pdf.setFont(font, 20)
            pdf.drawString(48, page_height - 58, "INVOICE")
            pdf.setFont(font, 10)
            pdf.drawRightString(
                page_width - 48, page_height - 48, str(invoice.invoice_number)
            )
            pdf.drawRightString(
                page_width - 48,
                page_height - 64,
                f"Issued: {invoice.issued_at.date().isoformat()}",
            )
            pdf.drawRightString(
                page_width - 48,
                page_height - 80,
                f"Due: {invoice.due_at.date().isoformat()}",
            )
            pdf.drawRightString(
                page_width - 48,
                page_height - 96,
                f"Period: {invoice.period_start} — {invoice.period_end}",
            )
            pdf.drawRightString(
                page_width - 48,
                page_height - 112,
                f"Payment reference: {invoice.invoice_number}",
            )
            parties_y = page_height - 150
            for x, snapshot, title in (
                (48, invoice.issuer_snapshot, "ISSUER"),
                (page_width / 2 + 15, invoice.customer_snapshot, "CUSTOMER"),
            ):
                line_y = parties_y
                for index, line in enumerate(_party_lines(title, snapshot)):
                    pdf.setFont(font, 10 if index else 11)
                    pdf.drawString(x, line_y, line[:65])
                    line_y -= 15
            table_y = parties_y - 145

        pdf.setFont(font, 9)
        headings = (
            (48, "Description"),
            (330, "Qty"),
            (390, "Unit price"),
            (485, "Total"),
        )
        for x, heading in headings:
            pdf.drawString(x, table_y, heading)
        table_y -= 8
        pdf.line(48, table_y, page_width - 48, table_y)
        return table_y - 20

    y = draw_table_header(continuation=False)
    for item in invoice.line_items:
        if y < 92:
            finish_page()
            y = draw_table_header(continuation=True)
        pdf.setFont(font, 9)
        pdf.drawString(48, y, str(item["description"])[:48])
        pdf.drawRightString(370, y, str(item["quantity"]))
        pdf.drawRightString(
            470, y, _money(int(item["unit_price_cents"]), invoice.currency)
        )
        pdf.drawRightString(
            page_width - 48, y, _money(int(item["total_cents"]), invoice.currency)
        )
        y -= 20

    if y < 105:
        finish_page()
        y = draw_table_header(continuation=True)
    pdf.line(330, y, page_width - 48, y)
    y -= 22
    pdf.setFont(font, 12)
    pdf.drawRightString(
        page_width - 48, y, f"TOTAL: {_money(invoice.total_cents, invoice.currency)}"
    )
    finish_page()
    pdf.save()
    return buffer.getvalue()


def document_sha256(document: bytes) -> str:
    return sha256(document).hexdigest()


def _canonical_datetime(value) -> str:
    if value.tzinfo is not None:
        value = value.astimezone(UTC).replace(tzinfo=None)
    return value.isoformat()


def invoice_snapshot_sha256(invoice: PartnerInvoice) -> str:
    snapshot = {
        "invoice_number": invoice.invoice_number,
        "partner_id": invoice.partner_id,
        "period_start": invoice.period_start.isoformat(),
        "period_end": invoice.period_end.isoformat(),
        "currency": invoice.currency,
        "monthly_fee_cents": invoice.monthly_fee_cents,
        "confirmed_leads_count": invoice.confirmed_leads_count,
        "lead_fees_cents": invoice.lead_fees_cents,
        "total_cents": invoice.total_cents,
        "issuer_snapshot": invoice.issuer_snapshot,
        "customer_snapshot": invoice.customer_snapshot,
        "line_items": invoice.line_items,
        "document_version": invoice.document_version,
        "issued_at": _canonical_datetime(invoice.issued_at),
        "due_at": _canonical_datetime(invoice.due_at),
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def cancellation_snapshot_sha256(invoice: PartnerInvoice) -> str:
    snapshot = {
        "invoice_snapshot_sha256": invoice.snapshot_sha256,
        "cancellation_number": invoice.cancellation_number,
        "cancellation_reason": invoice.cancellation_reason,
        "cancellation_document_version": invoice.cancellation_document_version,
        "cancelled_at": (
            _canonical_datetime(invoice.cancelled_at) if invoice.cancelled_at else None
        ),
    }
    encoded = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def verified_invoice_pdf(invoice: PartnerInvoice, app_settings: Settings) -> bytes:
    document = render_invoice_pdf(invoice, app_settings)
    if not invoice.pdf_sha256 or not compare_digest(
        document_sha256(document), invoice.pdf_sha256
    ):
        raise InvoiceDocumentError(
            "Контрольная сумма PDF не совпадает со снимком счёта"
        )
    return document


def load_verified_invoice_pdf(invoice: PartnerInvoice, app_settings: Settings) -> bytes:
    """Load the immutable invoice artifact and verify both data and bytes."""
    if not invoice.pdf_storage_backend:
        return verified_invoice_pdf(invoice, app_settings)
    if not invoice.snapshot_sha256 or not compare_digest(
        invoice_snapshot_sha256(invoice), invoice.snapshot_sha256
    ):
        raise InvoiceDocumentError("Снимок данных счёта не прошёл проверку целостности")
    try:
        document = read_private_document(
            invoice.pdf_storage_backend,
            invoice.pdf_storage_key,
            invoice.pdf_file_path,
        )
    except StorageError as exc:
        raise InvoiceDocumentError(str(exc)) from exc
    if (
        invoice.pdf_content_type != "application/pdf"
        or invoice.pdf_size_bytes != len(document)
        or not invoice.pdf_sha256
        or not compare_digest(document_sha256(document), invoice.pdf_sha256)
    ):
        raise InvoiceDocumentError(
            "Сохранённый PDF счёта не прошёл проверку целостности"
        )
    return document


def render_invoice_cancellation_pdf(
    invoice: PartnerInvoice, app_settings: Settings
) -> bytes:
    if not all(
        (
            invoice.invoice_number,
            invoice.cancellation_number,
            invoice.cancellation_reason,
            invoice.cancellation_document_version,
            invoice.cancelled_at,
            invoice.issuer_snapshot,
            invoice.customer_snapshot,
            invoice.line_items is not None,
        )
    ):
        raise InvoiceDocumentError(
            "Для аннулированного счёта отсутствует неизменяемый снимок документа"
        )

    font = _font_name(app_settings)
    buffer = BytesIO()
    page_width, page_height = A4
    pdf = canvas.Canvas(buffer, pagesize=A4, invariant=1, pageCompression=1)
    pdf.setTitle(f"Invoice cancellation {invoice.cancellation_number}")
    pdf.setAuthor(str(invoice.issuer_snapshot["name"]))
    pdf.setCreator("AI Garden B2B billing")
    pdf.setFont(font, 20)
    pdf.drawString(48, page_height - 58, "INVOICE CANCELLATION")
    pdf.setFont(font, 10)
    pdf.drawRightString(
        page_width - 48, page_height - 48, str(invoice.cancellation_number)
    )
    pdf.drawRightString(
        page_width - 48,
        page_height - 64,
        f"Original invoice: {invoice.invoice_number}",
    )
    pdf.drawRightString(
        page_width - 48,
        page_height - 80,
        f"Issued: {invoice.issued_at.date().isoformat()}",
    )
    pdf.drawRightString(
        page_width - 48,
        page_height - 96,
        f"Cancelled: {invoice.cancelled_at.date().isoformat()}",
    )

    y = page_height - 140
    for x, snapshot, title in (
        (48, invoice.issuer_snapshot, "ISSUER"),
        (page_width / 2 + 15, invoice.customer_snapshot, "CUSTOMER"),
    ):
        line_y = y
        for index, line in enumerate(_party_lines(title, snapshot)):
            pdf.setFont(font, 10 if index else 11)
            pdf.drawString(x, line_y, line[:65])
            line_y -= 15

    y -= 145
    pdf.setFont(font, 10)
    pdf.drawString(48, y, "Reason:")
    y -= 17
    reason = str(invoice.cancellation_reason)
    while reason:
        line, reason = reason[:88], reason[88:]
        pdf.drawString(48, y, line)
        y -= 15
    y -= 10

    pdf.setFont(font, 9)
    headings = (
        (48, "Description"),
        (330, "Qty"),
        (390, "Unit price"),
        (485, "Cancelled"),
    )
    for x, heading in headings:
        pdf.drawString(x, y, heading)
    y -= 8
    pdf.line(48, y, page_width - 48, y)
    y -= 20
    for item in invoice.line_items:
        pdf.drawString(48, y, str(item["description"])[:48])
        pdf.drawRightString(370, y, str(item["quantity"]))
        pdf.drawRightString(
            470, y, _money(int(item["unit_price_cents"]), invoice.currency)
        )
        pdf.drawRightString(
            page_width - 48,
            y,
            _money(-int(item["total_cents"]), invoice.currency),
        )
        y -= 20
    pdf.line(330, y, page_width - 48, y)
    y -= 22
    pdf.setFont(font, 12)
    pdf.drawRightString(
        page_width - 48,
        y,
        f"CANCELLED TOTAL: {_money(-invoice.total_cents, invoice.currency)}",
    )

    pdf.setFont(font, 7)
    pdf.drawString(48, 52, f"Document version: {invoice.cancellation_document_version}")
    pdf.drawString(
        48,
        38,
        "Operational cancellation document. Accounting and tax compliance must be "
        "reviewed for the applicable jurisdiction.",
    )
    pdf.showPage()
    pdf.save()
    return buffer.getvalue()


def verified_invoice_cancellation_pdf(
    invoice: PartnerInvoice, app_settings: Settings
) -> bytes:
    document = render_invoice_cancellation_pdf(invoice, app_settings)
    if not invoice.cancellation_pdf_sha256 or not compare_digest(
        document_sha256(document), invoice.cancellation_pdf_sha256
    ):
        raise InvoiceDocumentError(
            "Контрольная сумма PDF аннулирования не совпадает со снимком документа"
        )
    return document


def load_verified_invoice_cancellation_pdf(
    invoice: PartnerInvoice, app_settings: Settings
) -> bytes:
    """Load and verify the immutable cancellation artifact."""
    if not invoice.cancellation_number:
        raise InvoiceDocumentError("Документ аннулирования не найден")
    if not invoice.cancellation_storage_backend:
        return verified_invoice_cancellation_pdf(invoice, app_settings)
    if (
        not invoice.snapshot_sha256
        or not compare_digest(invoice_snapshot_sha256(invoice), invoice.snapshot_sha256)
        or not invoice.cancellation_snapshot_sha256
        or not compare_digest(
            cancellation_snapshot_sha256(invoice),
            invoice.cancellation_snapshot_sha256,
        )
    ):
        raise InvoiceDocumentError(
            "Снимок данных аннулирования не прошёл проверку целостности"
        )
    try:
        document = read_private_document(
            invoice.cancellation_storage_backend,
            invoice.cancellation_storage_key,
            invoice.cancellation_file_path,
        )
    except StorageError as exc:
        raise InvoiceDocumentError(str(exc)) from exc
    if (
        invoice.cancellation_content_type != "application/pdf"
        or invoice.cancellation_size_bytes != len(document)
        or not invoice.cancellation_pdf_sha256
        or not compare_digest(
            document_sha256(document), invoice.cancellation_pdf_sha256
        )
    ):
        raise InvoiceDocumentError(
            "Сохранённый PDF аннулирования не прошёл проверку целостности"
        )
    return document


def send_invoice_email(
    invoice: PartnerInvoice,
    document: bytes,
    app_settings: Settings,
) -> str:
    recipient = str((invoice.customer_snapshot or {}).get("email") or "").strip()
    if not recipient:
        raise InvoiceDocumentError("В снимке счёта отсутствует email получателя")
    if app_settings.email_delivery_mode != "smtp":
        logger.info(
            "development invoice email",
            extra={
                "invoice_id": invoice.id,
                "invoice_number": invoice.invoice_number,
                "recipient": recipient,
                "pdf_sha256": invoice.pdf_sha256,
            },
        )
        return "logged"

    message = EmailMessage()
    message["From"] = app_settings.smtp_from_email
    message["To"] = recipient
    message["Subject"] = f"AI Garden invoice {invoice.invoice_number}"
    message.set_content(
        f"Invoice {invoice.invoice_number} is attached. "
        f"Payment due: {invoice.due_at.date().isoformat()}."
    )
    message.add_attachment(
        document,
        maintype="application",
        subtype="pdf",
        filename=f"{invoice.invoice_number}.pdf",
    )
    with smtplib.SMTP(
        app_settings.smtp_host, app_settings.smtp_port, timeout=10
    ) as client:
        client.starttls(context=ssl.create_default_context())
        client.login(app_settings.smtp_username, app_settings.smtp_password)
        client.send_message(message)
    return "sent"

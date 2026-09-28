import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

from app.config import settings
from app.services.invoice_service import render_invoice_pdf


def _invoice_with_line_items(count: int):
    now = datetime(2026, 8, 31, 12, 0, tzinfo=UTC)
    line_items = [
        {
            "description": f"Confirmed partner lead #{index + 1}",
            "quantity": 1,
            "unit_price_cents": 250,
            "total_cents": 250,
        }
        for index in range(count)
    ]
    return SimpleNamespace(
        invoice_number="AG-2026-00000042",
        issuer_snapshot={
            "name": "AI Garden Test SIA",
            "registration_number": "LV-TEST-1",
            "vat_number": None,
            "address": "Testa iela 1, Riga",
            "email": "billing@ai-garden.test",
            "bank_name": None,
            "iban": "LV00TEST0000000000000",
            "swift": None,
        },
        customer_snapshot={
            "name": "Example Partner SIA",
            "registration_number": "LV-TEST-2",
            "vat_number": None,
            "address": "Partneru iela 2, Riga",
            "email": "accounts@example.test",
        },
        line_items=line_items,
        document_version="b2b-invoice-v1",
        issued_at=now,
        due_at=now + timedelta(days=14),
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        currency="eur",
        total_cents=count * 250,
    )


def test_long_invoice_is_paginated_and_deterministic():
    invoice = _invoice_with_line_items(70)

    first = render_invoice_pdf(invoice, settings)
    second = render_invoice_pdf(invoice, settings)

    pages = re.findall(rb"/Type\s*/Page(?:\s|/)", first)
    assert len(pages) >= 3
    assert first.startswith(b"%PDF-")
    assert first == second

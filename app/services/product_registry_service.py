import hashlib
import json
from datetime import UTC, date, datetime

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.models import ProductRecommendationRule, RegulatedProductRegistration
from app.schemas import RegistrationSnapshotImport


def _checksum(record: dict) -> str:
    canonical = json.dumps(record, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def revalidate_registry_rules(db: Session, *, today: date | None = None) -> int:
    today = today or date.today()
    invalid_entries = select(RegulatedProductRegistration.id).where(or_(
        RegulatedProductRegistration.status != "active",
        RegulatedProductRegistration.valid_until < today,
    ))
    rules = list(db.scalars(select(ProductRecommendationRule).where(
        ProductRecommendationRule.registry_entry_id.in_(invalid_entries),
        or_(ProductRecommendationRule.active.is_(True), ProductRecommendationRule.expert_verified.is_(True)),
    )))
    for rule in rules:
        rule.active = False
        rule.expert_verified = False
    return len(rules)


def import_registration_snapshot(db: Session, payload: RegistrationSnapshotImport) -> dict[str, int]:
    now = datetime.now(UTC)
    created = 0
    updated = 0
    seen: set[tuple[str, str]] = set()
    jurisdictions = {record.jurisdiction for record in payload.records}
    for record in payload.records:
        values = record.model_dump()
        checksum = _checksum(record.model_dump(mode="json"))
        key = (record.jurisdiction, record.registration_number)
        seen.add(key)
        item = db.scalar(select(RegulatedProductRegistration).where(
            RegulatedProductRegistration.jurisdiction == record.jurisdiction,
            RegulatedProductRegistration.registration_number == record.registration_number,
        ).with_for_update())
        if item is None:
            item = RegulatedProductRegistration(
                **values,
                source_name=payload.source_name,
                source_url=payload.source_url,
                source_version=payload.source_version,
                record_checksum=checksum,
                last_synced_at=now,
            )
            db.add(item)
            created += 1
        else:
            for field, value in values.items():
                setattr(item, field, value)
            item.source_name = payload.source_name
            item.source_url = payload.source_url
            item.source_version = payload.source_version
            item.record_checksum = checksum
            item.last_synced_at = now
            updated += 1

    marked_not_listed = 0
    if payload.complete_snapshot:
        existing = list(db.scalars(select(RegulatedProductRegistration).where(
            RegulatedProductRegistration.source_name == payload.source_name,
            RegulatedProductRegistration.jurisdiction.in_(jurisdictions),
        )))
        for item in existing:
            if (item.jurisdiction, item.registration_number) not in seen and item.status != "not_listed":
                item.status = "not_listed"
                item.last_synced_at = now
                marked_not_listed += 1
    db.flush()
    invalidated_rules = revalidate_registry_rules(db)
    return {
        "created": created,
        "updated": updated,
        "marked_not_listed": marked_not_listed,
        "invalidated_rules": invalidated_rules,
    }

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.config import settings
from app.database import Base
from app.vector import Vector


def utcnow() -> datetime:
    return datetime.now(UTC)


diagnosis_photos = Table(
    "diagnosis_photos",
    Base.metadata,
    Column(
        "diagnosis_id", ForeignKey("diagnoses.id", ondelete="CASCADE"), primary_key=True
    ),
    Column(
        "photo_id", ForeignKey("plant_photos.id", ondelete="CASCADE"), primary_key=True
    ),
)


class Plant(Base):
    __tablename__ = "plants"

    id: Mapped[int] = mapped_column(primary_key=True)
    garden_id: Mapped[int] = mapped_column(
        ForeignKey("gardens.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    species: Mapped[str | None] = mapped_column(String(160), nullable=True)
    taxon_id: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    growing_place: Mapped[str] = mapped_column(String(40), default="indoor")
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    photos: Mapped[list["PlantPhoto"]] = relationship(cascade="all, delete-orphan")
    diagnoses: Mapped[list["Diagnosis"]] = relationship(cascade="all, delete-orphan")
    care_events: Mapped[list["CareEvent"]] = relationship(cascade="all, delete-orphan")
    reminders: Mapped[list["Reminder"]] = relationship(cascade="all, delete-orphan")


class CareEvent(Base):
    __tablename__ = "care_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(
        ForeignKey("plants.id", ondelete="CASCADE"), index=True
    )
    event_type: Mapped[str] = mapped_column(String(30), index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    unit: Mapped[str | None] = mapped_column(String(30), nullable=True)
    product: Mapped[str | None] = mapped_column(String(160), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(
        ForeignKey("plants.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), index=True)
    title: Mapped[str] = mapped_column(String(160))
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    recurrence: Mapped[str | None] = mapped_column(String(20), nullable=True)
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    preferred_channel: Mapped[str] = mapped_column(String(20), default="web")
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    @property
    def completed(self) -> bool:
        return self.completed_at is not None


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(120))
    password_hash: Mapped[str] = mapped_column(String(255))
    language: Mapped[str] = mapped_column(String(5), default="ru")
    region: Mapped[str | None] = mapped_column(String(120), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    gardens: Mapped[list["Garden"]] = relationship(cascade="all, delete-orphan")


class AuthSession(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    refresh_token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    previous_refresh_token_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AccountDeletionRequest(Base):
    __tablename__ = "account_deletion_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(
        String(40), default="pending_cancellation", index=True
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    cancellation_requested_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class StorageDeletionOutbox(Base):
    __tablename__ = "storage_deletion_outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    backend: Mapped[str] = mapped_column(String(20))
    storage_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class StorageMigrationRecord(Base):
    __tablename__ = "storage_migration_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    photo_id: Mapped[int | None] = mapped_column(
        ForeignKey("plant_photos.id", ondelete="SET NULL"),
        nullable=True,
        unique=True,
        index=True,
    )
    source_path: Mapped[str] = mapped_column(String(500))
    storage_key: Mapped[str] = mapped_column(String(500), unique=True)
    checksum_sha256: Mapped[str] = mapped_column(String(64))
    size_bytes: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    verified_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AdminAuditLog(Base):
    __tablename__ = "admin_audit_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    admin_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    action: Mapped[str] = mapped_column(String(80), index=True)
    target_type: Mapped[str] = mapped_column(String(80))
    target_id: Mapped[str] = mapped_column(String(120), index=True)
    details: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class AccountActionToken(Base):
    __tablename__ = "account_action_tokens"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    purpose: Mapped[str] = mapped_column(String(30), index=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Garden(Base):
    __tablename__ = "gardens"
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_garden_user_name"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    kind: Mapped[str] = mapped_column(String(30), default="garden")
    location: Mapped[str | None] = mapped_column(String(160), nullable=True)
    soil_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    plants: Mapped[list[Plant]] = relationship(cascade="all, delete-orphan")


class PlantPhoto(Base):
    __tablename__ = "plant_photos"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(
        ForeignKey("plants.id", ondelete="CASCADE"), index=True
    )
    file_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    storage_key: Mapped[str | None] = mapped_column(
        String(500), nullable=True, unique=True, index=True
    )
    content_type: Mapped[str] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    diagnoses: Mapped[list["Diagnosis"]] = relationship(
        secondary=diagnosis_photos,
        back_populates="photos",
    )


class Diagnosis(Base):
    __tablename__ = "diagnoses"

    id: Mapped[int] = mapped_column(primary_key=True)
    plant_id: Mapped[int] = mapped_column(
        ForeignKey("plants.id", ondelete="CASCADE"), index=True
    )
    symptoms: Mapped[str] = mapped_column(Text)
    damaged_part: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict] = mapped_column(JSON)
    model_name: Mapped[str] = mapped_column(String(120), default="mock-rule-based")
    prompt_version: Mapped[str] = mapped_column(String(80), default="mock-v1")
    current_revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    photos: Mapped[list[PlantPhoto]] = relationship(
        secondary=diagnosis_photos,
        back_populates="diagnoses",
    )
    questions: Mapped[list["DiagnosisQuestion"]] = relationship(
        cascade="all, delete-orphan"
    )
    revisions: Mapped[list["DiagnosisRevision"]] = relationship(
        cascade="all, delete-orphan"
    )
    feedback: Mapped[list["DiagnosisFeedback"]] = relationship(
        cascade="all, delete-orphan"
    )

    @property
    def photo_ids(self) -> list[int]:
        return [photo.id for photo in self.photos]

    @property
    def demo_mode(self) -> bool:
        return self.model_name.startswith("mock-")


class DiagnosisRevision(Base):
    __tablename__ = "diagnosis_revisions"
    __table_args__ = (
        UniqueConstraint(
            "diagnosis_id", "version", name="uq_diagnosis_revision_version"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="CASCADE"), index=True
    )
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(30))
    result: Mapped[dict] = mapped_column(JSON)
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class DiagnosisQuestion(Base):
    __tablename__ = "diagnosis_questions"
    __table_args__ = (
        UniqueConstraint(
            "diagnosis_id",
            "revision_number",
            "position",
            name="uq_diagnosis_question_position",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="CASCADE"), index=True
    )
    revision_number: Mapped[int] = mapped_column(Integer)
    position: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    answer: Mapped["DiagnosisAnswer | None"] = relationship(
        cascade="all, delete-orphan", uselist=False
    )


class DiagnosisAnswer(Base):
    __tablename__ = "diagnosis_answers"

    id: Mapped[int] = mapped_column(primary_key=True)
    question_id: Mapped[int] = mapped_column(
        ForeignKey("diagnosis_questions.id", ondelete="CASCADE"),
        unique=True,
        index=True,
    )
    answer: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DiagnosisFeedback(Base):
    __tablename__ = "diagnosis_feedback"
    __table_args__ = (
        UniqueConstraint("diagnosis_id", "user_id", name="uq_diagnosis_feedback_user"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    diagnosis_id: Mapped[int] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    rating: Mapped[int] = mapped_column(Integer)
    helpful: Mapped[bool] = mapped_column(Boolean)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AIRequestLog(Base):
    __tablename__ = "ai_request_logs"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    diagnosis_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    request_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model_name: Mapped[str] = mapped_column(String(120))
    prompt_version: Mapped[str] = mapped_column(String(80))
    success: Mapped[bool] = mapped_column(Boolean)
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    http_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    response_ms: Mapped[int] = mapped_column(Integer, default=0)
    estimated_cost: Mapped[float] = mapped_column(Float, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class AISafetyAdjustment(Base):
    __tablename__ = "ai_safety_adjustments"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    diagnosis_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("diagnosis_jobs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    reason_codes: Mapped[list] = mapped_column(JSON, default=list)
    removed_action_count: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, index=True
    )


class DiagnosisJob(Base):
    __tablename__ = "diagnosis_jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plant_id: Mapped[int] = mapped_column(
        ForeignKey("plants.id", ondelete="CASCADE"), index=True
    )
    diagnosis_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), index=True, default="queued")
    operation: Mapped[str] = mapped_column(String(20), default="create")
    execution_token: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    payload: Mapped[dict] = mapped_column(JSON)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class Partner(Base):
    __tablename__ = "partners"
    __table_args__ = (
        CheckConstraint(
            "monthly_fee_cents >= 0", name="ck_partner_monthly_fee_nonnegative"
        ),
        CheckConstraint(
            "confirmed_lead_price_cents >= 0",
            name="ck_partner_lead_price_nonnegative",
        ),
        CheckConstraint(
            "length(billing_currency) = 3 AND billing_currency = lower(billing_currency)",
            name="ck_partner_currency_iso",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    website_url: Mapped[str] = mapped_column(String(500))
    billing_plan: Mapped[str] = mapped_column(String(30), default="free", index=True)
    monthly_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_lead_price_cents: Mapped[int] = mapped_column(Integer, default=0)
    billing_currency: Mapped[str] = mapped_column(String(3), default="eur")
    legal_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    registration_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    vat_number: Mapped[str | None] = mapped_column(String(80), nullable=True)
    billing_address: Mapped[str | None] = mapped_column(String(500), nullable=True)
    billing_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    postback_secret_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PartnerMember(Base):
    __tablename__ = "partner_members"
    __table_args__ = (
        UniqueConstraint("user_id", name="uq_partner_member_user"),
        UniqueConstraint(
            "partner_id", "user_id", name="uq_partner_member_partner_user"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(
        ForeignKey("partners.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(20), default="editor")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        UniqueConstraint("partner_id", "sku", name="uq_product_partner_sku"),
        CheckConstraint(
            "price_cents IS NULL OR price_cents >= 0",
            name="ck_product_price_nonnegative",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = lower(currency)",
            name="ck_product_currency_iso",
        ),
        CheckConstraint(
            "moderation_status IN ('draft', 'pending', 'approved', 'rejected')",
            name="ck_product_moderation_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    partner_id: Mapped[int] = mapped_column(
        ForeignKey("partners.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    sku: Mapped[str | None] = mapped_column(String(100), nullable=True, index=True)
    category: Mapped[str] = mapped_column(String(40), index=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    product_url: Mapped[str] = mapped_column(String(500))
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    price_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    currency: Mapped[str] = mapped_column(String(3), default="eur")
    in_stock: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    moderation_status: Mapped[str] = mapped_column(
        String(20), default="draft", index=True
    )
    moderation_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    moderated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class RegulatedProductRegistration(Base):
    __tablename__ = "regulated_product_registrations"
    __table_args__ = (
        UniqueConstraint(
            "jurisdiction",
            "registration_number",
            name="uq_regulated_registration_number",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_name: Mapped[str] = mapped_column(String(40), index=True)
    jurisdiction: Mapped[str] = mapped_column(String(2), index=True)
    registration_number: Mapped[str] = mapped_column(String(100), index=True)
    product_name: Mapped[str] = mapped_column(String(300))
    holder_name: Mapped[str | None] = mapped_column(String(300), nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    valid_from: Mapped[date | None] = mapped_column(nullable=True)
    valid_until: Mapped[date | None] = mapped_column(nullable=True, index=True)
    source_url: Mapped[str] = mapped_column(String(1_000))
    source_version: Mapped[str] = mapped_column(String(120))
    record_checksum: Mapped[str] = mapped_column(String(64))
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class ProductRecommendationRule(Base):
    __tablename__ = "product_recommendation_rules"
    __table_args__ = (
        UniqueConstraint(
            "product_id",
            "crop_name",
            "problem_name",
            "region_code",
            name="uq_product_recommendation_context",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    registry_entry_id: Mapped[int | None] = mapped_column(
        ForeignKey("regulated_product_registrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    crop_name: Mapped[str] = mapped_column(String(160), index=True)
    plant_taxon_id: Mapped[str | None] = mapped_column(
        String(100), nullable=True, index=True
    )
    problem_name: Mapped[str] = mapped_column(String(200), index=True)
    problem_code: Mapped[str | None] = mapped_column(
        String(80), nullable=True, index=True
    )
    safe_action: Mapped[str] = mapped_column(String(500))
    region_code: Mapped[str] = mapped_column(String(20), index=True)
    country_code: Mapped[str | None] = mapped_column(
        String(2), nullable=True, index=True
    )
    registration_country: Mapped[str | None] = mapped_column(
        String(2), nullable=True, index=True
    )
    registration_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    registration_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    registration_expires_on: Mapped[date | None] = mapped_column(
        nullable=True, index=True
    )
    expert_verified: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class CatalogTaxonomyAlias(Base):
    __tablename__ = "catalog_taxonomy_aliases"
    __table_args__ = (
        UniqueConstraint(
            "alias_type", "locale", "normalized_alias", name="uq_catalog_taxonomy_alias"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    alias_type: Mapped[str] = mapped_column(String(20), index=True)
    stable_code: Mapped[str] = mapped_column(String(100), index=True)
    locale: Mapped[str] = mapped_column(String(5), default="*", index=True)
    alias: Mapped[str] = mapped_column(String(200))
    normalized_alias: Mapped[str] = mapped_column(String(200), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PartnerInvoice(Base):
    __tablename__ = "partner_invoices"
    __table_args__ = (
        CheckConstraint("period_start <= period_end", name="ck_invoice_period"),
        CheckConstraint(
            "monthly_fee_cents >= 0", name="ck_invoice_monthly_fee_nonnegative"
        ),
        CheckConstraint(
            "confirmed_leads_count >= 0", name="ck_invoice_lead_count_nonnegative"
        ),
        CheckConstraint(
            "lead_fees_cents >= 0", name="ck_invoice_lead_fees_nonnegative"
        ),
        CheckConstraint("total_cents >= 0", name="ck_invoice_total_nonnegative"),
        CheckConstraint(
            "total_cents = monthly_fee_cents + lead_fees_cents",
            name="ck_invoice_total_consistent",
        ),
        CheckConstraint(
            "length(currency) = 3 AND currency = lower(currency)",
            name="ck_invoice_currency_iso",
        ),
        CheckConstraint(
            "status IN ('issued', 'paid', 'void')", name="ck_invoice_status"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_number: Mapped[str | None] = mapped_column(
        String(40), nullable=True, unique=True, index=True
    )
    partner_id: Mapped[int] = mapped_column(
        ForeignKey("partners.id", ondelete="CASCADE"), index=True
    )
    period_start: Mapped[date] = mapped_column(index=True)
    period_end: Mapped[date] = mapped_column(index=True)
    currency: Mapped[str] = mapped_column(String(3), default="eur")
    monthly_fee_cents: Mapped[int] = mapped_column(Integer, default=0)
    confirmed_leads_count: Mapped[int] = mapped_column(Integer, default=0)
    lead_fees_cents: Mapped[int] = mapped_column(Integer, default=0)
    total_cents: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="issued", index=True)
    issuer_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    customer_snapshot: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    line_items: Mapped[list | None] = mapped_column(JSON, nullable=True)
    document_version: Mapped[str | None] = mapped_column(String(40), nullable=True)
    snapshot_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pdf_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    pdf_storage_backend: Mapped[str | None] = mapped_column(String(20), nullable=True)
    pdf_storage_key: Mapped[str | None] = mapped_column(String(700), nullable=True)
    pdf_file_path: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    pdf_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pdf_content_type: Mapped[str | None] = mapped_column(String(100), nullable=True)
    pdf_generator_version: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    cancellation_number: Mapped[str | None] = mapped_column(
        String(50), nullable=True, unique=True, index=True
    )
    cancellation_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    cancellation_document_version: Mapped[str | None] = mapped_column(
        String(40), nullable=True
    )
    cancellation_snapshot_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    cancellation_pdf_sha256: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
    cancellation_storage_backend: Mapped[str | None] = mapped_column(
        String(20), nullable=True
    )
    cancellation_storage_key: Mapped[str | None] = mapped_column(
        String(700), nullable=True
    )
    cancellation_file_path: Mapped[str | None] = mapped_column(
        String(1000), nullable=True
    )
    cancellation_size_bytes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cancellation_content_type: Mapped[str | None] = mapped_column(
        String(100), nullable=True
    )
    cancellation_generator_version: Mapped[str | None] = mapped_column(
        String(160), nullable=True
    )
    cancelled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancelled_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    paid_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class PartnerInvoiceDelivery(Base):
    __tablename__ = "partner_invoice_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "invoice_id", "attempt_number", name="uq_invoice_delivery_attempt"
        ),
        CheckConstraint(
            "attempt_number > 0", name="ck_invoice_delivery_attempt_positive"
        ),
        CheckConstraint(
            "status IN ('pending', 'sending', 'sent', 'logged', 'failed', 'unknown')",
            name="ck_invoice_delivery_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int] = mapped_column(
        ForeignKey("partner_invoices.id", ondelete="CASCADE"), index=True
    )
    requested_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer)
    recipient: Mapped[str] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    error_type: Mapped[str | None] = mapped_column(String(120), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class PartnerPayment(Base):
    __tablename__ = "partner_payments"
    __table_args__ = (
        UniqueConstraint(
            "source", "external_id", name="uq_partner_payment_source_external"
        ),
        CheckConstraint("amount_cents > 0", name="ck_partner_payment_amount_positive"),
        CheckConstraint(
            "length(currency) = 3 AND currency = lower(currency)",
            name="ck_partner_payment_currency_iso",
        ),
        CheckConstraint(
            "status IN ('matched', 'unmatched', 'rejected')",
            name="ck_partner_payment_status",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("partner_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    imported_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    source: Mapped[str] = mapped_column(String(60))
    external_id: Mapped[str] = mapped_column(String(160))
    booking_date: Mapped[date] = mapped_column(index=True)
    amount_cents: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String(3))
    reference: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(20), index=True)
    resolution_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    resolved_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class ProductLead(Base):
    __tablename__ = "product_leads"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "product_id", "diagnosis_id", name="uq_product_lead_context"
        ),
        UniqueConstraint(
            "product_id", "partner_reference", name="uq_product_lead_partner_reference"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    product_id: Mapped[int] = mapped_column(
        ForeignKey("products.id", ondelete="CASCADE"), index=True
    )
    diagnosis_id: Mapped[int | None] = mapped_column(
        ForeignKey("diagnoses.id", ondelete="SET NULL"), nullable=True, index=True
    )
    click_id: Mapped[str | None] = mapped_column(
        String(36), nullable=True, unique=True, index=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True, index=True
    )
    source: Mapped[str] = mapped_column(String(40), default="diagnosis")
    status: Mapped[str] = mapped_column(String(30), default="clicked", index=True)
    partner_reference: Mapped[str | None] = mapped_column(String(120), nullable=True)
    conversion_value_cents: Mapped[int | None] = mapped_column(Integer, nullable=True)
    claimed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    review_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    reviewed_by_admin_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    billable_amount_cents: Mapped[int] = mapped_column(Integer, default=0)
    invoice_id: Mapped[int | None] = mapped_column(
        ForeignKey("partner_invoices.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    redirected_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    redirect_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    plan: Mapped[str] = mapped_column(String(30), default="pro")
    provider: Mapped[str] = mapped_column(String(30), default="stripe")
    provider_customer_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, unique=True
    )
    provider_subscription_id: Mapped[str | None] = mapped_column(
        String(120), nullable=True, unique=True
    )
    status: Mapped[str] = mapped_column(String(30), default="pending", index=True)
    current_period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_event_created_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    cancel_at_period_end: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class BillingEvent(Base):
    __tablename__ = "billing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    provider_event_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String(100), index=True)
    processed: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), unique=True, index=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(String(10), default="ru")
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TelegramLinkToken(Base):
    __tablename__ = "telegram_link_tokens"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class TelegramUpdate(Base):
    __tablename__ = "telegram_updates"

    id: Mapped[int] = mapped_column(primary_key=True)
    update_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    response_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )


class KnowledgeSourceRecord(Base):
    __tablename__ = "knowledge_sources"

    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    url: Mapped[str] = mapped_column(String(1_000), unique=True)
    summary: Mapped[str] = mapped_column(Text)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    region: Mapped[str] = mapped_column(String(30), default="global", index=True)
    languages: Mapped[list] = mapped_column(JSON, default=list)
    plant_types: Mapped[list] = mapped_column(JSON, default=list)
    problem_types: Mapped[list] = mapped_column(JSON, default=list)
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    next_review_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), index=True
    )
    reviewed_by: Mapped[str] = mapped_column(String(160))
    review_role: Mapped[str] = mapped_column(
        String(30), default="editorial", index=True
    )
    usage_basis: Mapped[str] = mapped_column(
        String(80), default="linked_factual_summary"
    )
    source_version: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        cascade="all, delete-orphan", back_populates="source"
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (
        UniqueConstraint("source_id", "position", name="uq_knowledge_chunk_position"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("knowledge_sources.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer)
    content: Mapped[str] = mapped_column(Text)
    embedding_model: Mapped[str] = mapped_column(String(100))
    embedding: Mapped[list] = mapped_column(
        Vector(settings.knowledge_embedding_dimensions)
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    source: Mapped[KnowledgeSourceRecord] = relationship(back_populates="chunks")


class UserNotification(Base):
    __tablename__ = "user_notifications"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(200))
    body: Mapped[str] = mapped_column(Text)
    delivery_channel: Mapped[str] = mapped_column(
        String(20), default="both", index=True
    )
    event_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    deduplication_key: Mapped[str] = mapped_column(String(240), unique=True, index=True)
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    telegram_sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    delivery_attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_delivery_error: Mapped[str | None] = mapped_column(String(300), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

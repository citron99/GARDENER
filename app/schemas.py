from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_validator,
)


class GrowingPlace(StrEnum):
    open_ground = "open_ground"
    greenhouse = "greenhouse"
    balcony = "balcony"
    indoor = "indoor"


class DamagedPart(StrEnum):
    leaf = "leaf"
    stem = "stem"
    root = "root"
    fruit = "fruit"
    flower = "flower"
    whole_plant = "whole_plant"


class PlantCreate(BaseModel):
    garden_id: int
    name: str = Field(min_length=1, max_length=120)
    species: str | None = Field(default=None, max_length=160)
    taxon_id: str | None = Field(
        default=None, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.:-]{1,99}$"
    )
    growing_place: GrowingPlace = GrowingPlace.indoor
    region: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class PlantUpdate(BaseModel):
    garden_id: int | None = None
    name: str | None = Field(default=None, min_length=1, max_length=120)
    species: str | None = Field(default=None, max_length=160)
    taxon_id: str | None = Field(
        default=None, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.:-]{1,99}$"
    )
    growing_place: GrowingPlace | None = None
    region: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_changes_and_required_values(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for field in ("garden_id", "name", "growing_place"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PlantRead(PlantCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    created_at: datetime


class CareEventType(StrEnum):
    watering = "watering"
    fertilizing = "fertilizing"
    treatment = "treatment"
    pruning = "pruning"
    transplanting = "transplanting"
    observation = "observation"


class CareEventCreate(BaseModel):
    event_type: CareEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    amount: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: str | None = Field(default=None, max_length=30)
    product: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include timezone")
        return value

    @model_validator(mode="after")
    def validate_amount_unit(self):
        if self.unit and self.amount is None:
            raise ValueError("unit requires amount")
        return self


class CareEventUpdate(BaseModel):
    event_type: CareEventType | None = None
    occurred_at: datetime | None = None
    amount: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: str | None = Field(default=None, max_length=30)
    product: str | None = Field(default=None, max_length=160)
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("occurred_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("occurred_at must include timezone")
        return value

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        return self


class CareEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    plant_id: int
    event_type: CareEventType
    occurred_at: datetime
    amount: float | None
    unit: str | None
    product: str | None
    notes: str | None
    created_at: datetime

    @field_validator("occurred_at", "created_at")
    @classmethod
    def assume_utc_for_sqlite(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class ReminderKind(StrEnum):
    watering = "watering"
    fertilizing = "fertilizing"
    treatment = "treatment"
    pruning = "pruning"
    transplanting = "transplanting"
    inspection = "inspection"
    other = "other"


class ReminderCreate(BaseModel):
    kind: ReminderKind = ReminderKind.other
    title: str = Field(min_length=1, max_length=160)
    due_at: datetime
    notes: str | None = Field(default=None, max_length=2000)
    recurrence: Literal["daily", "weekly", "monthly"] | None = None
    timezone: str = Field(default="UTC", max_length=64)
    preferred_channel: Literal["web", "telegram", "both"] = "web"

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("due_at must include timezone")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value


class ReminderUpdate(BaseModel):
    kind: ReminderKind | None = None
    title: str | None = Field(default=None, min_length=1, max_length=160)
    due_at: datetime | None = None
    notes: str | None = Field(default=None, max_length=2000)
    completed: bool | None = None
    recurrence: Literal["daily", "weekly", "monthly"] | None = None
    timezone: str | None = Field(default=None, max_length=64)
    preferred_channel: Literal["web", "telegram", "both"] | None = None
    snooze_minutes: int | None = Field(default=None, ge=5, le=10_080)
    skip_occurrence: bool | None = None

    @field_validator("due_at")
    @classmethod
    def require_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("due_at must include timezone")
        return value

    @field_validator("timezone")
    @classmethod
    def validate_timezone_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("unknown timezone") from exc
        return value

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        controls = sum(
            value is not None
            for value in (
                self.completed,
                self.snooze_minutes,
                self.skip_occurrence,
            )
        )
        if controls > 1:
            raise ValueError(
                "completed, snooze_minutes and skip_occurrence are mutually exclusive"
            )
        return self


class ReminderRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    plant_id: int
    kind: ReminderKind
    title: str
    due_at: datetime
    notes: str | None
    recurrence: str | None
    timezone: str
    preferred_channel: str
    completed: bool
    completed_at: datetime | None
    created_at: datetime

    @field_validator("due_at", "completed_at", "created_at")
    @classmethod
    def assume_utc_for_sqlite(cls, value: datetime | None) -> datetime | None:
        return (
            value.replace(tzinfo=UTC)
            if value is not None and value.tzinfo is None
            else value
        )


class CalendarItemRead(BaseModel):
    item_type: str = Field(
        pattern="^(reminder|care_event|weather_warning|seasonal_task)$"
    )
    reference_id: int | str
    plant_id: int | None = None
    plant_name: str | None = None
    garden_id: int | None = None
    garden_name: str | None = None
    event_type: str
    title: str
    severity: str | None = None
    description: str | None = None
    starts_at: datetime
    completed: bool

    @field_validator("starts_at")
    @classmethod
    def assume_utc_for_sqlite(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value


class NotificationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    kind: str
    title: str
    body: str
    delivery_channel: str
    event_at: datetime
    read_at: datetime | None
    telegram_sent_at: datetime | None
    created_at: datetime

    @field_validator("event_at", "read_at", "telegram_sent_at", "created_at")
    @classmethod
    def notification_assume_utc(cls, value: datetime | None) -> datetime | None:
        return (
            value.replace(tzinfo=UTC)
            if value is not None and value.tzinfo is None
            else value
        )


class WeatherDailyRead(BaseModel):
    date: date
    weather_code: int | None = None
    temperature_min_c: float
    temperature_max_c: float
    precipitation_mm: float
    precipitation_probability_percent: int | None = None
    snowfall_cm: float = 0
    wind_speed_max_kmh: float
    wind_gusts_max_kmh: float | None = None
    et0_mm: float | None = None


class WeatherWarningRead(BaseModel):
    date: date
    kind: str = Field(
        pattern="^(frost|heat|heavy_rain|strong_wind|snow|irrigation_check)$"
    )
    severity: str = Field(pattern="^(info|warning|critical)$")
    title: str
    advice: str


class WeatherForecastRead(BaseModel):
    provider: str = "Open-Meteo"
    source_url: str = "https://open-meteo.com/"
    location: str
    country_code: str | None = None
    latitude: float
    longitude: float
    timezone: str
    fetched_at: datetime
    daily: list[WeatherDailyRead]
    warnings: list[WeatherWarningRead]


class PhotoRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    plant_id: int
    content_type: str
    created_at: datetime


class DiagnosisCreate(BaseModel):
    plant_id: int
    symptoms: str = Field(min_length=10, max_length=3000)
    damaged_part: DamagedPart
    photo_ids: list[int] = Field(min_length=1, max_length=5)


class PossibleCause(BaseModel):
    name: str
    cause_code: str | None = Field(default=None, pattern=r"^[a-z0-9][a-z0-9_.-]{1,79}$")
    confidence: str = Field(pattern="^(high|medium|low)$")
    matched_signs: list[str]
    missing_signs: list[str]
    checks: list[str]
    source_ids: list[str] = Field(default_factory=list)


class DiagnosisInputStatus(StrEnum):
    valid = "valid"
    poor_quality = "poor_quality"
    not_a_plant = "not_a_plant"
    insufficient_data = "insufficient_data"


class ImageQuality(StrEnum):
    good = "good"
    acceptable = "acceptable"
    poor = "poor"


class AnalysisOutcome(StrEnum):
    no_visible_problem = "no_visible_problem"
    possible_problem = "possible_problem"
    cannot_analyze = "cannot_analyze"


class DiagnosisResult(BaseModel):
    input_status: DiagnosisInputStatus = DiagnosisInputStatus.valid
    plant_detected: bool = True
    image_quality: ImageQuality = ImageQuality.acceptable
    cannot_analyze_reason: str | None = None
    analysis_outcome: AnalysisOutcome
    disclaimer: str
    analysis_status: str = Field(
        pattern="^(needs_confirmation|preliminary|expert_required|cannot_analyze)$"
    )
    possible_causes: list[PossibleCause] = Field(default_factory=list, max_length=3)
    safe_actions: list[str] = Field(min_length=1)
    questions: list[str] = Field(max_length=7)
    expert_required: bool
    sources: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_diagnostic_decision(self):
        if self.input_status == DiagnosisInputStatus.valid:
            if not self.plant_detected:
                raise ValueError("valid input requires plant_detected=true")
            if self.image_quality == ImageQuality.poor:
                raise ValueError("valid input cannot have poor image quality")
            if self.cannot_analyze_reason is not None:
                raise ValueError("valid input cannot have cannot_analyze_reason")
            if self.analysis_outcome == AnalysisOutcome.cannot_analyze:
                raise ValueError("valid input requires a completed analysis outcome")
            if (
                self.analysis_outcome == AnalysisOutcome.no_visible_problem
                and self.possible_causes
            ):
                raise ValueError("no_visible_problem must not contain possible causes")
            if (
                self.analysis_outcome == AnalysisOutcome.possible_problem
                and not self.possible_causes
            ):
                raise ValueError(
                    "possible_problem requires at least one possible cause"
                )
        else:
            if self.possible_causes:
                raise ValueError("non-valid input must not contain possible causes")
            if not self.cannot_analyze_reason:
                raise ValueError("non-valid input requires cannot_analyze_reason")
            if self.analysis_status != "cannot_analyze":
                raise ValueError(
                    "non-valid input requires analysis_status=cannot_analyze"
                )
            if self.analysis_outcome != AnalysisOutcome.cannot_analyze:
                raise ValueError(
                    "non-valid input requires analysis_outcome=cannot_analyze"
                )
            if (
                self.input_status == DiagnosisInputStatus.not_a_plant
                and self.plant_detected
            ):
                raise ValueError("not_a_plant requires plant_detected=false")
            if (
                self.input_status == DiagnosisInputStatus.poor_quality
                and self.image_quality != ImageQuality.poor
            ):
                raise ValueError("poor_quality requires image_quality=poor")
        return self


class DiagnosisRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    plant_id: int
    photo_ids: list[int]
    symptoms: str
    damaged_part: str
    status: str
    result: DiagnosisResult
    model_name: str
    prompt_version: str
    current_revision: int
    demo_mode: bool
    created_at: datetime


class DiagnosisJobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    plant_id: int
    diagnosis_id: int | None
    status: str = Field(pattern="^(queued|running|succeeded|failed)$")
    operation: str = Field(pattern="^(create|reanalyze)$")
    attempts: int
    error_type: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class DiagnosisQuestionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    revision_number: int
    position: int
    text: str
    created_at: datetime


class DiagnosisAnswerItem(BaseModel):
    question_id: int
    answer: str = Field(min_length=1, max_length=3000)


class DiagnosisAnswersCreate(BaseModel):
    answers: list[DiagnosisAnswerItem] = Field(min_length=1, max_length=7)


class DiagnosisRevisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    diagnosis_id: int
    version: int
    status: str
    result: DiagnosisResult
    model_name: str
    prompt_version: str
    created_at: datetime


class DiagnosisFeedbackCreate(BaseModel):
    rating: int = Field(ge=1, le=5)
    helpful: bool
    comment: str | None = Field(default=None, max_length=2000)


class DiagnosisFeedbackRead(DiagnosisFeedbackCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    diagnosis_id: int
    created_at: datetime


class DiagnosisAnswerHistoryRead(BaseModel):
    diagnosis_id: int
    question: str
    answer: str
    revision_number: int


class PlantHistoryRead(BaseModel):
    plant: PlantRead
    diagnoses: list[DiagnosisRead]
    revisions: list[DiagnosisRevisionRead]
    answers: list[DiagnosisAnswerHistoryRead]
    care_events: list[CareEventRead]
    reminders: list[ReminderRead]


class ProductRead(BaseModel):
    id: int
    name: str
    sku: str | None
    category: str
    description: str | None
    product_url: str
    image_url: str | None
    price_cents: int | None
    currency: str
    in_stock: bool
    regions: list[str]
    partner_name: str
    commercial_label: str = "Реклама партнёра"
    moderation_status: str


class PartnerCreate(BaseModel):
    name: str = Field(min_length=2, max_length=160)
    website_url: str = Field(max_length=500, pattern=r"^https://")
    billing_plan: Literal["free", "lead", "business"] = "free"
    monthly_fee_cents: int = Field(default=0, ge=0, le=10_000_000)
    confirmed_lead_price_cents: int = Field(default=0, ge=0, le=1_000_000)
    billing_currency: str = Field(default="eur", pattern=r"^[a-z]{3}$")
    legal_name: str | None = Field(default=None, min_length=2, max_length=200)
    registration_number: str | None = Field(default=None, min_length=2, max_length=80)
    vat_number: str | None = Field(default=None, min_length=2, max_length=80)
    billing_address: str | None = Field(default=None, min_length=5, max_length=500)
    billing_email: str | None = Field(
        default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )


class PartnerUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=160)
    website_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    active: bool | None = None
    billing_plan: Literal["free", "lead", "business"] | None = None
    monthly_fee_cents: int | None = Field(default=None, ge=0, le=10_000_000)
    confirmed_lead_price_cents: int | None = Field(default=None, ge=0, le=1_000_000)
    billing_currency: str | None = Field(default=None, pattern=r"^[a-z]{3}$")
    legal_name: str | None = Field(default=None, min_length=2, max_length=200)
    registration_number: str | None = Field(default=None, min_length=2, max_length=80)
    vat_number: str | None = Field(default=None, min_length=2, max_length=80)
    billing_address: str | None = Field(default=None, min_length=5, max_length=500)
    billing_email: str | None = Field(
        default=None, max_length=320, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$"
    )

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for field in (
            "name",
            "website_url",
            "active",
            "billing_plan",
            "monthly_fee_cents",
            "confirmed_lead_price_cents",
            "billing_currency",
            "legal_name",
            "registration_number",
            "vat_number",
            "billing_address",
            "billing_email",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class PartnerRead(PartnerCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool
    created_at: datetime


class PartnerMemberRole(StrEnum):
    owner = "owner"
    editor = "editor"
    viewer = "viewer"


class PartnerMemberAssign(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    role: PartnerMemberRole = PartnerMemberRole.editor


class PartnerMemberRead(BaseModel):
    id: int
    partner_id: int
    partner_name: str
    user_id: int
    email: str
    name: str
    role: PartnerMemberRole
    active: bool
    created_at: datetime


class PartnerAccountRead(BaseModel):
    partner_id: int
    partner_name: str
    website_url: str
    role: PartnerMemberRole
    can_manage_products: bool
    billing_plan: str
    billing_currency: str


class PartnerOverviewRead(BaseModel):
    partner_id: int
    partner_name: str
    active_products: int
    total_products: int
    total_leads: int
    leads_last_30_days: int
    pending_conversion_claims: int
    confirmed_conversions: int
    uninvoiced_amount_cents: int
    billing_currency: str


class ProductCategory(StrEnum):
    tools = "tools"
    nutrition = "nutrition"
    protection = "protection"
    soil = "soil"
    irrigation = "irrigation"
    other = "other"


class ProductModerationStatus(StrEnum):
    draft = "draft"
    pending = "pending"
    approved = "approved"
    rejected = "rejected"


class ProductCreate(BaseModel):
    partner_id: int
    name: str = Field(min_length=2, max_length=200)
    sku: str | None = Field(default=None, max_length=100, pattern=r"^[A-Z0-9._:-]+$")
    category: ProductCategory
    description: str | None = Field(default=None, max_length=500)
    product_url: str = Field(max_length=500, pattern=r"^https://")
    image_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    price_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: str = Field(default="eur", pattern=r"^[a-z]{3}$")
    in_stock: bool = True
    regions: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("regions")
    @classmethod
    def normalize_regions(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if any(len(value) > 80 for value in cleaned):
            raise ValueError("region is too long")
        return list(dict.fromkeys(cleaned))

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_product_currency(cls, value: str) -> str:
        return value.strip().lower()


class PartnerProductCreate(BaseModel):
    name: str = Field(min_length=2, max_length=200)
    sku: str | None = Field(default=None, max_length=100, pattern=r"^[A-Z0-9._:-]+$")
    category: ProductCategory
    description: str | None = Field(default=None, max_length=500)
    product_url: str = Field(max_length=500, pattern=r"^https://")
    image_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    price_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: str = Field(default="eur", pattern=r"^[a-z]{3}$")
    in_stock: bool = True
    regions: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("regions")
    @classmethod
    def normalize_regions(cls, values: list[str]) -> list[str]:
        cleaned = [value.strip() for value in values if value.strip()]
        if any(len(value) > 80 for value in cleaned):
            raise ValueError("region is too long")
        return list(dict.fromkeys(cleaned))

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_product_currency(cls, value: str) -> str:
        return value.strip().lower()


class ProductUpdate(BaseModel):
    partner_id: int | None = None
    name: str | None = Field(default=None, min_length=2, max_length=200)
    sku: str | None = Field(default=None, max_length=100, pattern=r"^[A-Z0-9._:-]+$")
    category: ProductCategory | None = None
    description: str | None = Field(default=None, max_length=500)
    product_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    image_url: str | None = Field(default=None, max_length=500, pattern=r"^https://")
    price_cents: int | None = Field(default=None, ge=0, le=100_000_000)
    currency: str | None = Field(default=None, pattern=r"^[a-z]{3}$")
    in_stock: bool | None = None
    regions: list[str] | None = Field(default=None, max_length=30)
    active: bool | None = None

    @field_validator("regions")
    @classmethod
    def normalize_regions(cls, values: list[str] | None) -> list[str] | None:
        if values is None:
            return None
        cleaned = [value.strip() for value in values if value.strip()]
        if any(len(value) > 80 for value in cleaned):
            raise ValueError("region is too long")
        return list(dict.fromkeys(cleaned))

    @field_validator("sku", mode="before")
    @classmethod
    def normalize_sku(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None

    @field_validator("currency", mode="before")
    @classmethod
    def normalize_product_currency(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for field in (
            "partner_id",
            "name",
            "category",
            "product_url",
            "regions",
            "currency",
            "in_stock",
            "active",
        ):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class ProductModerationUpdate(BaseModel):
    status: Literal["approved", "rejected"]
    note: str | None = Field(default=None, max_length=500)


class ProductRecommendationRuleCreate(BaseModel):
    product_id: int
    registry_entry_id: int | None = None
    crop_name: str = Field(min_length=1, max_length=160)
    plant_taxon_id: str | None = Field(
        default=None, max_length=100, pattern=r"^[a-z0-9][a-z0-9_.:-]{1,99}$"
    )
    problem_name: str = Field(min_length=1, max_length=200)
    problem_code: str | None = Field(
        default=None, max_length=80, pattern=r"^[a-z0-9][a-z0-9_.-]{1,79}$"
    )
    safe_action: str = Field(min_length=5, max_length=500)
    region_code: str = Field(min_length=2, max_length=20)
    country_code: str | None = Field(default=None, pattern=r"^[A-Z]{2}$")
    registration_country: str | None = Field(default=None, min_length=2, max_length=2)
    registration_number: str | None = Field(default=None, max_length=100)
    registration_url: str | None = Field(
        default=None, max_length=500, pattern=r"^https://"
    )
    registration_expires_on: date | None = None
    expert_verified: bool = False

    @field_validator("crop_name", "problem_name", "region_code")
    @classmethod
    def normalize_rule_text(cls, value: str) -> str:
        return value.strip()

    @field_validator(
        "taxon_id", "plant_taxon_id", "problem_code", check_fields=False, mode="before"
    )
    @classmethod
    def normalize_stable_code(cls, value: str | None) -> str | None:
        return value.strip().lower() if value else None

    @field_validator("country_code", "registration_country", mode="before")
    @classmethod
    def normalize_country_code(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else None


class ProductRecommendationRuleRead(ProductRecommendationRuleCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool
    created_at: datetime


class CatalogTaxonomyAliasCreate(BaseModel):
    alias_type: Literal["plant", "problem", "country", "region"]
    stable_code: str = Field(min_length=2, max_length=100)
    locale: Literal["ru", "lv", "en"] | None = None
    alias: str = Field(min_length=2, max_length=200)

    @field_validator("stable_code", mode="before")
    @classmethod
    def normalize_alias_code(cls, value: str) -> str:
        return value.strip()

    @field_validator("alias", mode="before")
    @classmethod
    def strip_alias(cls, value: str) -> str:
        return value.strip()

    @model_validator(mode="after")
    def validate_stable_code(self):
        import re

        if self.alias_type == "country":
            self.stable_code = self.stable_code.upper()
            if not re.fullmatch(r"[A-Z]{2}", self.stable_code):
                raise ValueError("country stable_code must be ISO alpha-2")
        else:
            self.stable_code = self.stable_code.lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9_.:-]{1,99}", self.stable_code):
                raise ValueError("stable_code has invalid format")
        return self


class CatalogTaxonomyAliasRead(CatalogTaxonomyAliasCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    active: bool
    created_at: datetime


class RegulatedProductRegistrationUpsert(BaseModel):
    jurisdiction: str = Field(min_length=2, max_length=2, pattern=r"^[A-Z]{2}$")
    registration_number: str = Field(min_length=1, max_length=100)
    product_name: str = Field(min_length=2, max_length=300)
    holder_name: str | None = Field(default=None, max_length=300)
    status: Literal["active", "expired", "suspended", "revoked", "not_listed"]
    valid_from: date | None = None
    valid_until: date | None = None


class RegistrationSnapshotImport(BaseModel):
    source_name: Literal["vaad", "eu_pesticides", "manual_verified"]
    source_url: str = Field(max_length=1000, pattern=r"^https://")
    source_version: str = Field(min_length=1, max_length=120)
    complete_snapshot: bool = False
    records: list[RegulatedProductRegistrationUpsert] = Field(
        min_length=1, max_length=5000
    )


class RegulatedProductRegistrationRead(RegulatedProductRegistrationUpsert):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source_name: str
    source_url: str
    source_version: str
    record_checksum: str
    last_synced_at: datetime
    created_at: datetime
    updated_at: datetime


class RegistrationImportResult(BaseModel):
    created: int
    updated: int
    marked_not_listed: int
    invalidated_rules: int


class ProductLeadCreate(BaseModel):
    diagnosis_id: int | None = None


class ProductLeadRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    product_id: int
    diagnosis_id: int | None
    source: str
    status: str
    redirect_url: str
    created_at: datetime


class PartnerPostbackKeyRead(BaseModel):
    api_key: str


class PartnerConversionPostback(BaseModel):
    signed_click_id: str = Field(min_length=40, max_length=500)
    partner_reference: str = Field(
        min_length=3, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    conversion_value_cents: int = Field(ge=0, le=100_000_000)


class AdminProductRead(ProductRead):
    partner_id: int
    active: bool
    created_at: datetime


class AdminLeadRead(BaseModel):
    id: int
    product_id: int
    product_name: str
    partner_name: str
    diagnosis_id: int | None
    source: str
    status: str
    partner_reference: str | None
    conversion_value_cents: int | None
    billable_amount_cents: int
    claimed_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime


class PartnerLeadRead(BaseModel):
    id: int
    product_id: int
    product_name: str
    source: str
    status: str
    partner_reference: str | None
    conversion_value_cents: int | None
    billable_amount_cents: int
    claimed_at: datetime | None
    confirmed_at: datetime | None
    created_at: datetime


class PartnerConversionClaim(BaseModel):
    partner_reference: str = Field(
        min_length=3, max_length=120, pattern=r"^[A-Za-z0-9._:-]+$"
    )
    conversion_value_cents: int = Field(ge=0, le=100_000_000)


class AdminConversionReview(BaseModel):
    approved: bool
    note: str | None = Field(default=None, max_length=500)


class PartnerInvoiceCreate(BaseModel):
    period_start: date
    period_end: date
    due_days: int = Field(default=14, ge=1, le=90)

    @model_validator(mode="after")
    def validate_period(self):
        if self.period_start.day != 1:
            raise ValueError("invoice period must start on the first day of a month")
        next_month = (
            date(self.period_start.year + 1, 1, 1)
            if self.period_start.month == 12
            else date(self.period_start.year, self.period_start.month + 1, 1)
        )
        if self.period_end != next_month:
            raise ValueError("invoice period must cover exactly one calendar month")
        return self


class PartnerInvoiceStatusUpdate(BaseModel):
    status: Literal["paid", "void"]
    reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_void_reason(self):
        if self.status == "void" and (not self.reason or len(self.reason.strip()) < 3):
            raise ValueError("reason is required when voiding an invoice")
        if self.status == "paid" and self.reason is not None:
            raise ValueError("reason is not allowed when marking an invoice paid")
        return self


class PartnerInvoiceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_number: str | None
    partner_id: int
    period_start: date
    period_end: date
    currency: str
    monthly_fee_cents: int
    confirmed_leads_count: int
    lead_fees_cents: int
    total_cents: int
    status: str
    document_version: str | None
    snapshot_sha256: str | None
    pdf_sha256: str | None
    pdf_size_bytes: int | None
    pdf_content_type: str | None
    pdf_generator_version: str | None
    cancellation_number: str | None
    cancellation_reason: str | None
    cancellation_document_version: str | None
    cancellation_snapshot_sha256: str | None
    cancellation_pdf_sha256: str | None
    cancellation_size_bytes: int | None
    cancellation_content_type: str | None
    cancellation_generator_version: str | None
    cancelled_at: datetime | None
    issued_at: datetime
    due_at: datetime
    paid_at: datetime | None
    created_at: datetime


class PartnerInvoiceSend(BaseModel):
    confirm_resend: bool = False


class PartnerInvoiceDeliveryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_id: int
    attempt_number: int
    recipient: str
    status: Literal["pending", "sending", "sent", "logged", "failed", "unknown"]
    error_type: str | None
    created_at: datetime
    completed_at: datetime | None


class PartnerPaymentRecord(BaseModel):
    external_id: str = Field(min_length=1, max_length=160)
    booking_date: date
    amount_cents: int = Field(gt=0, le=1_000_000_000)
    currency: str = Field(pattern=r"^[a-zA-Z]{3}$")
    reference: str = Field(min_length=1, max_length=500)


class PartnerPaymentImport(BaseModel):
    source: str = Field(min_length=2, max_length=60, pattern=r"^[a-zA-Z0-9_.-]+$")
    records: list[PartnerPaymentRecord] = Field(min_length=1, max_length=500)


class PartnerPaymentImportRead(BaseModel):
    imported: int
    matched: int
    unmatched: int
    duplicates: int


class PartnerPaymentResolve(BaseModel):
    action: Literal["match", "reject"]
    invoice_id: int | None = Field(default=None, ge=1)
    note: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_resolution(self):
        if self.action == "match" and self.invoice_id is None:
            raise ValueError("invoice_id is required for match")
        if self.action == "reject" and self.invoice_id is not None:
            raise ValueError("invoice_id is not allowed for reject")
        return self


class PartnerManualPaymentCreate(BaseModel):
    external_id: str = Field(
        min_length=3, max_length=160, pattern=r"^[A-Za-z0-9._:/-]+$"
    )
    payment_method: Literal["bank_transfer", "cash", "card", "other"]
    booking_date: date
    note: str = Field(min_length=3, max_length=500)


class PartnerPaymentRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    invoice_id: int | None
    source: str
    external_id: str
    booking_date: date
    amount_cents: int
    currency: str
    reference: str
    status: Literal["matched", "unmatched", "rejected"]
    resolution_note: str | None
    resolved_at: datetime | None
    created_at: datetime


class BillingPlanRead(BaseModel):
    code: str
    name: str
    monthly_amount: int
    currency: str
    features: list[str]
    checkout_available: bool


class CheckoutCreate(BaseModel):
    plan: str = Field(pattern="^pro$")


class BillingRedirectRead(BaseModel):
    url: str


class SubscriptionRead(BaseModel):
    plan: str
    status: str
    current_period_end: datetime | None
    cancel_at_period_end: bool
    can_manage: bool
    billing_enabled: bool


class TelegramStatusRead(BaseModel):
    enabled: bool
    linked: bool
    username: str | None = None
    language: str | None = None


class TelegramLinkRead(BaseModel):
    code: str
    deep_link: str | None
    expires_at: datetime


class UserRegister(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)
    name: str = Field(min_length=2, max_length=120)
    password: str = Field(min_length=8, max_length=128)
    language: str = Field(default="ru", pattern="^(ru|lv|en)$")
    region: str | None = Field(default=None, max_length=120)


class UserRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    name: str
    language: str
    region: str | None
    is_admin: bool
    created_at: datetime


class UserProfileUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=2, max_length=120)
    language: str | None = Field(default=None, pattern="^(ru|lv|en)$")
    region: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def require_changes(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for field in ("name", "language"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class AdminOverviewRead(BaseModel):
    users: int
    gardens: int
    plants: int
    diagnoses: int
    jobs_queued: int
    jobs_running: int
    jobs_failed: int
    ai_requests_successful: int
    ai_requests_failed: int
    input_tokens: int
    output_tokens: int
    estimated_cost: float
    average_feedback_rating: float | None
    partners: int
    active_products: int
    product_leads: int
    pending_partner_conversions: int
    confirmed_partner_conversions: int
    partner_invoices_issued_cents: int


class AdminUserSummaryRead(BaseModel):
    id: int
    email: str
    name: str
    region: str | None
    is_admin: bool
    created_at: datetime
    gardens_count: int
    plants_count: int
    diagnoses_count: int


class AdminJobSummaryRead(BaseModel):
    id: str
    user_id: int
    plant_id: int
    diagnosis_id: int | None
    status: str
    attempts: int
    error_type: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None


class AdminAIRequestRead(BaseModel):
    request_id: str
    diagnosis_id: int | None
    provider: str
    model_name: str
    prompt_version: str
    success: bool
    error_type: str | None
    http_status: int | None
    input_tokens: int
    output_tokens: int
    response_ms: int
    estimated_cost: float
    created_at: datetime


class AdminAuditLogRead(BaseModel):
    id: int
    admin_user_id: int
    action: str
    target_type: str
    target_id: str
    details: dict
    created_at: datetime


class KnowledgeSourceUpsert(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{2,99}$")
    title: str = Field(min_length=3, max_length=300)
    url: HttpUrl
    summary: str = Field(min_length=20, max_length=10_000)
    keywords: list[str] = Field(min_length=1, max_length=100)
    region: str = Field(default="global", min_length=2, max_length=30)
    language: list[Literal["ru", "lv", "en"]] = Field(min_length=1)
    plant_types: list[str] = Field(min_length=1, max_length=50)
    problem_types: list[str] = Field(min_length=1, max_length=50)
    last_verified_at: date
    next_review_at: date
    reviewed_by: str = Field(min_length=3, max_length=160)
    review_role: Literal["editorial", "agronomist", "regulatory"]
    usage_basis: Literal["linked_factual_summary", "licensed_content"]
    source_version: str = Field(min_length=1, max_length=100)
    active: bool = True

    @model_validator(mode="after")
    def review_must_follow_verification(self):
        if self.next_review_at <= self.last_verified_at:
            raise ValueError("next_review_at must be after last_verified_at")
        return self


class KnowledgeSourceAdminRead(KnowledgeSourceUpsert):
    chunks: int
    embedding_model: str | None
    updated_at: datetime


class KnowledgeSyncRead(BaseModel):
    refreshed_sources: int
    total_sources: int
    embedding_model: str


class LoginRequest(BaseModel):
    email: str
    password: str


class TokenRead(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    email_verification_required: bool = False


class RefreshTokenRequest(BaseModel):
    refresh_token: str = Field(min_length=32, max_length=500)


class AccountDeleteRequest(BaseModel):
    password: str = Field(min_length=8, max_length=128)


class EmailVerificationRequest(BaseModel):
    email: str = Field(pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$", max_length=320)


class ActionTokenRequest(BaseModel):
    token: str = Field(min_length=32, max_length=500)


class PasswordResetConfirm(ActionTokenRequest):
    new_password: str = Field(min_length=8, max_length=128)


class GardenKind(StrEnum):
    garden = "garden"
    vegetable_garden = "vegetable_garden"
    greenhouse = "greenhouse"
    balcony = "balcony"
    indoor = "indoor"
    client_plot = "client_plot"


class GardenCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    kind: GardenKind = GardenKind.garden
    location: str | None = Field(default=None, max_length=160)
    soil_type: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)


class GardenUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    kind: GardenKind | None = None
    location: str | None = Field(default=None, max_length=160)
    soil_type: str | None = Field(default=None, max_length=120)
    notes: str | None = Field(default=None, max_length=2000)

    @model_validator(mode="after")
    def require_changes_and_required_values(self):
        if not self.model_fields_set:
            raise ValueError("at least one field is required")
        for field in ("name", "kind"):
            if field in self.model_fields_set and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self


class GardenRead(GardenCreate):
    model_config = ConfigDict(from_attributes=True)
    id: int
    user_id: int
    created_at: datetime

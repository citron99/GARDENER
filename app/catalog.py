from datetime import date, datetime, timedelta, timezone
import csv
import hmac
from io import StringIO
import re
import secrets
from uuid import uuid4

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Response,
    UploadFile,
)
from fastapi.responses import RedirectResponse
from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from pydantic import ValidationError

from app.auth import get_admin_user, get_current_user
from app.catalog_taxonomy import (
    country_code_from_region,
    normalize_alias,
    resolve_catalog_code,
)
from app.config import settings
from app.database import get_db
from app.models import (
    AdminAuditLog,
    CatalogTaxonomyAlias,
    Diagnosis,
    Garden,
    Partner,
    PartnerInvoice,
    PartnerInvoiceDelivery,
    PartnerMember,
    Plant,
    Product,
    PartnerPayment,
    ProductLead,
    ProductRecommendationRule,
    RegulatedProductRegistration,
    User,
)
from app.schemas import (
    AdminConversionReview,
    AdminLeadRead,
    AdminProductRead,
    CatalogTaxonomyAliasCreate,
    CatalogTaxonomyAliasRead,
    PartnerAccountRead,
    PartnerConversionClaim,
    PartnerConversionPostback,
    PartnerPostbackKeyRead,
    PartnerCreate,
    PartnerInvoiceCreate,
    PartnerInvoiceDeliveryRead,
    PartnerInvoiceRead,
    PartnerInvoiceSend,
    PartnerInvoiceStatusUpdate,
    PartnerLeadRead,
    PartnerMemberAssign,
    PartnerMemberRead,
    PartnerOverviewRead,
    PartnerManualPaymentCreate,
    PartnerPaymentImport,
    PartnerPaymentImportRead,
    PartnerPaymentRead,
    PartnerPaymentResolve,
    PartnerProductCreate,
    PartnerRead,
    PartnerUpdate,
    ProductCreate,
    ProductLeadCreate,
    ProductLeadRead,
    ProductModerationUpdate,
    ProductRead,
    ProductRecommendationRuleCreate,
    ProductRecommendationRuleRead,
    ProductUpdate,
    RegistrationImportResult,
    RegistrationSnapshotImport,
    RegulatedProductRegistrationRead,
)
from app.services.product_registry_service import import_registration_snapshot
from app.services.invoice_service import (
    CANCELLATION_DOCUMENT_VERSION,
    DOCUMENT_VERSION,
    InvoiceDocumentError,
    build_invoice_snapshots,
    cancellation_snapshot_sha256,
    document_sha256,
    invoice_snapshot_sha256,
    invoice_pdf_generator_version,
    load_verified_invoice_cancellation_pdf,
    load_verified_invoice_pdf,
    render_invoice_cancellation_pdf,
    render_invoice_pdf,
)
from app.services.invoice_delivery_service import dispatch_invoice_delivery
from app.services.partner_attribution_service import (
    AttributionTokenError,
    add_click_id,
    hash_partner_key,
    lead_idempotency_key,
    sign_click_id,
    verify_click_token,
)
from app.services.storage_service import (
    StorageError,
    store_private_document,
)


router = APIRouter(prefix="/api/v1")
public_router = APIRouter()
INVOICE_NUMBER_PATTERN = re.compile(r"\bAG-\d{4}-\d{8}\b", re.IGNORECASE)


def _require_b2b_invoicing() -> None:
    if not settings.b2b_invoicing_enabled:
        raise HTTPException(404, "Модуль B2B-счетов отключён")


def _product_dict(product: Product, partner_name: str) -> dict:
    return {
        "id": product.id,
        "partner_id": product.partner_id,
        "name": product.name,
        "sku": product.sku,
        "category": product.category,
        "description": product.description,
        "product_url": product.product_url,
        "image_url": product.image_url,
        "price_cents": product.price_cents,
        "currency": product.currency,
        "in_stock": product.in_stock,
        "regions": product.regions or [],
        "partner_name": partner_name,
        "active": product.active,
        "moderation_status": product.moderation_status,
        "created_at": product.created_at,
        "commercial_label": "Реклама партнёра",
    }


def _lead_dict(lead: ProductLead) -> dict:
    if not lead.click_id:
        raise HTTPException(409, "Для старого перехода отсутствует click ID")
    return {
        "id": lead.id,
        "product_id": lead.product_id,
        "diagnosis_id": lead.diagnosis_id,
        "source": lead.source,
        "status": lead.status,
        "redirect_url": f"/product/go/{sign_click_id(lead.click_id)}",
        "created_at": lead.created_at,
    }


def _region_matches(product: Product, region: str | None) -> bool:
    if not product.regions or not region:
        return True
    normalized = region.casefold()
    region_country = country_code_from_region(region)
    return any(
        (
            region_country is not None
            and country_code_from_region(item) == region_country
        )
        or item.casefold() in normalized
        or normalized in item.casefold()
        for item in product.regions
    )


def _member_dict(member: PartnerMember, partner: Partner, user: User) -> dict:
    return {
        "id": member.id,
        "partner_id": partner.id,
        "partner_name": partner.name,
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "role": member.role,
        "active": member.active,
        "created_at": member.created_at,
    }


def get_partner_member(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> PartnerMember:
    member = db.scalar(
        select(PartnerMember)
        .join(Partner)
        .where(
            PartnerMember.user_id == user.id,
            PartnerMember.active.is_(True),
            Partner.active.is_(True),
        )
    )
    if not member:
        raise HTTPException(403, "Нет доступа к кабинету партнёра")
    return member


def _require_product_manager(member: PartnerMember) -> None:
    if member.role not in {"owner", "editor"}:
        raise HTTPException(403, "Недостаточно прав для управления товарами")


@router.get("/catalog/products", response_model=list[ProductRead])
def catalog_products(
    category: str | None = None,
    region: str | None = Query(default=None, max_length=120),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=100),
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = (
        select(Product, Partner.name)
        .join(Partner)
        .where(
            Product.active.is_(True),
            Product.moderation_status == "approved",
            Partner.active.is_(True),
        )
    )
    if category:
        query = query.where(Product.category == category)
    rows = db.execute(query.order_by(Product.id.desc()).limit(200)).all()
    selected_region = region or user.region
    matching = [
        _product_dict(product, partner_name)
        for product, partner_name in rows
        if _region_matches(product, selected_region)
    ]
    return matching[offset : offset + limit]


@router.get(
    "/recommendations/{diagnosis_id}/products", response_model=list[ProductRead]
)
def recommendation_products(
    diagnosis_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    diagnosis = db.scalar(
        select(Diagnosis)
        .join(Plant)
        .join(Garden)
        .where(Diagnosis.id == diagnosis_id, Garden.user_id == user.id)
    )
    if not diagnosis:
        raise HTTPException(404, "Диагностика не найдена")
    if diagnosis.result.get("analysis_outcome") != "possible_problem":
        return []
    plant = db.get(Plant, diagnosis.plant_id)
    cause_names = {
        str(item.get("name", "")).strip().casefold()
        for item in diagnosis.result.get("possible_causes", [])
        if item.get("name") and item.get("confidence") in {"high", "medium"}
    }
    cause_codes = {
        str(item.get("cause_code", "")).strip().lower()
        for item in diagnosis.result.get("possible_causes", [])
        if item.get("cause_code") and item.get("confidence") in {"high", "medium"}
    }
    if not cause_names and not cause_codes:
        return []
    region_value = (plant.region or user.region or "").strip()
    region = region_value.casefold()
    country_code = resolve_catalog_code(db, "country", region_value, user.language)
    crop = (plant.species or plant.name).strip().casefold()
    taxon_id = (
        plant.taxon_id.lower()
        if plant.taxon_id
        else resolve_catalog_code(
            db, "plant", plant.species or plant.name, user.language
        )
    )
    for cause_name in cause_names:
        resolved = resolve_catalog_code(db, "problem", cause_name, user.language)
        if resolved:
            cause_codes.add(resolved)
    rows = db.execute(
        select(Product, Partner.name, ProductRecommendationRule)
        .join(Partner, Partner.id == Product.partner_id)
        .join(
            ProductRecommendationRule,
            ProductRecommendationRule.product_id == Product.id,
        )
        .outerjoin(
            RegulatedProductRegistration,
            RegulatedProductRegistration.id
            == ProductRecommendationRule.registry_entry_id,
        )
        .where(
            Product.active.is_(True),
            Product.in_stock.is_(True),
            Product.moderation_status == "approved",
            Product.category.in_(("tools", "irrigation")),
            Partner.active.is_(True),
            ProductRecommendationRule.active.is_(True),
            ProductRecommendationRule.expert_verified.is_(True),
            or_(
                ProductRecommendationRule.registry_entry_id.is_(None),
                and_(
                    RegulatedProductRegistration.status == "active",
                    or_(
                        RegulatedProductRegistration.valid_until.is_(None),
                        RegulatedProductRegistration.valid_until >= date.today(),
                    ),
                ),
            ),
            (
                ProductRecommendationRule.registration_expires_on.is_(None)
                | (ProductRecommendationRule.registration_expires_on >= date.today())
            ),
        )
        .order_by(Product.id.desc())
        .limit(200)
    ).all()
    selected: list[dict] = []
    seen: set[int] = set()
    for product, partner_name, rule in rows:
        rule_crop = rule.crop_name.casefold()
        rule_problem = rule.problem_name.casefold()
        rule_region = rule.region_code.casefold()
        crop_matches = (
            taxon_id is not None and taxon_id == rule.plant_taxon_id
            if rule.plant_taxon_id
            else rule_crop == "*" or rule_crop in crop or crop in rule_crop
        )
        problem_matches = (
            rule.problem_code in cause_codes
            if rule.problem_code
            else rule_problem == "*"
            or any(
                rule_problem in cause or cause in rule_problem for cause in cause_names
            )
        )
        region_matches = (
            country_code is not None and country_code == rule.country_code
            if rule.country_code
            else rule_region == "*"
            or (region and (rule_region in region or region in rule_region))
        )
        if (
            product.id not in seen
            and crop_matches
            and problem_matches
            and region_matches
            and _region_matches(product, region)
        ):
            selected.append(_product_dict(product, partner_name))
            seen.add(product.id)
    return selected[:10]


@router.post(
    "/products/{product_id}/lead", response_model=ProductLeadRead, status_code=201
)
def create_product_lead(
    product_id: int,
    payload: ProductLeadCreate,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict:
    product = db.scalar(
        select(Product)
        .join(Partner)
        .where(
            Product.id == product_id,
            Product.active.is_(True),
            Product.moderation_status == "approved",
            Partner.active.is_(True),
        )
    )
    if not product:
        raise HTTPException(404, "Товар не найден")
    if payload.diagnosis_id is not None:
        owned = db.scalar(
            select(Diagnosis.id)
            .join(Plant)
            .join(Garden)
            .where(Diagnosis.id == payload.diagnosis_id, Garden.user_id == user.id)
        )
        if not owned:
            raise HTTPException(404, "Диагностика не найдена")
    idempotency_key = lead_idempotency_key(user.id, product.id, payload.diagnosis_id)
    existing = db.scalar(
        select(ProductLead).where(
            ProductLead.idempotency_key == idempotency_key,
        )
    )
    if existing:
        return _lead_dict(existing)
    lead = ProductLead(
        user_id=user.id,
        product_id=product.id,
        diagnosis_id=payload.diagnosis_id,
        click_id=str(uuid4()),
        idempotency_key=idempotency_key,
        source="diagnosis" if payload.diagnosis_id is not None else "catalog",
    )
    db.add(lead)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        lead = db.scalar(
            select(ProductLead).where(
                ProductLead.idempotency_key == idempotency_key,
            )
        )
        if not lead:
            raise
        return _lead_dict(lead)
    db.refresh(lead)
    return _lead_dict(lead)


@public_router.get("/product/go/{signed_click_id}", include_in_schema=False)
def product_redirect(
    signed_click_id: str, db: Session = Depends(get_db)
) -> RedirectResponse:
    try:
        click_id = verify_click_token(signed_click_id)
    except AttributionTokenError as exc:
        raise HTTPException(404, "Переход недействителен или устарел") from exc
    row = db.execute(
        select(ProductLead, Product, Partner)
        .join(Product, Product.id == ProductLead.product_id)
        .join(Partner, Partner.id == Product.partner_id)
        .where(
            ProductLead.click_id == click_id,
            Product.active.is_(True),
            Product.moderation_status == "approved",
            Partner.active.is_(True),
        )
        .with_for_update()
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Переход недействителен или устарел")
    lead, product, _partner = row
    lead.redirected_at = datetime.now(timezone.utc)
    lead.redirect_count = (lead.redirect_count or 0) + 1
    db.commit()
    return RedirectResponse(
        add_click_id(product.product_url, signed_click_id), status_code=302
    )


@router.post("/partner/postback-key", response_model=PartnerPostbackKeyRead)
def rotate_partner_postback_key(
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict:
    if member.role != "owner":
        raise HTTPException(403, "Только владелец может обновить postback key")
    partner = db.get(Partner, member.partner_id)
    api_key = secrets.token_urlsafe(40)
    partner.postback_secret_hash = hash_partner_key(api_key)
    db.commit()
    return {"api_key": api_key}


@router.post("/partner/conversions/postback", response_model=PartnerLeadRead)
def partner_conversion_postback(
    payload: PartnerConversionPostback,
    partner_key: str | None = Header(default=None, alias="X-Partner-Key"),
    db: Session = Depends(get_db),
) -> dict:
    try:
        click_id = verify_click_token(payload.signed_click_id)
    except AttributionTokenError as exc:
        raise HTTPException(404, "Переход не найден") from exc
    row = db.execute(
        select(ProductLead, Product, Partner)
        .join(Product, Product.id == ProductLead.product_id)
        .join(Partner, Partner.id == Product.partner_id)
        .where(ProductLead.click_id == click_id)
        .with_for_update()
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Переход не найден")
    lead, product, partner = row
    supplied_hash = hash_partner_key(partner_key or "")
    if not partner.postback_secret_hash or not hmac.compare_digest(
        supplied_hash,
        partner.postback_secret_hash,
    ):
        raise HTTPException(401, "Некорректный partner key")
    if lead.redirected_at is None:
        raise HTTPException(409, "Переход ещё не был зарегистрирован")
    if lead.status not in {"clicked", "rejected"} or lead.invoice_id is not None:
        raise HTTPException(409, "Конверсия уже обрабатывается или включена в счёт")
    lead.status = "conversion_claimed"
    lead.partner_reference = payload.partner_reference
    lead.conversion_value_cents = payload.conversion_value_cents
    lead.claimed_at = datetime.now(timezone.utc)
    lead.confirmed_at = None
    lead.rejected_at = None
    lead.review_note = None
    lead.reviewed_by_admin_id = None
    lead.billable_amount_cents = 0
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Такая ссылка на продажу уже использована для товара")
    db.refresh(lead)
    return {
        "id": lead.id,
        "product_id": lead.product_id,
        "product_name": product.name,
        "source": lead.source,
        "status": lead.status,
        "partner_reference": lead.partner_reference,
        "conversion_value_cents": lead.conversion_value_cents,
        "billable_amount_cents": lead.billable_amount_cents,
        "claimed_at": lead.claimed_at,
        "confirmed_at": lead.confirmed_at,
        "created_at": lead.created_at,
    }


@router.get("/admin/partners", response_model=list[PartnerRead])
def admin_partners(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[Partner]:
    return list(
        db.scalars(
            select(Partner).order_by(Partner.id.desc()).offset(offset).limit(limit)
        )
    )


@router.get("/admin/partner-members", response_model=list[PartnerMemberRead])
def admin_partner_members(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.execute(
        select(PartnerMember, Partner, User)
        .join(Partner, Partner.id == PartnerMember.partner_id)
        .join(User, User.id == PartnerMember.user_id)
        .order_by(PartnerMember.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_member_dict(member, partner, user) for member, partner, user in rows]


@router.post(
    "/admin/partners/{partner_id}/members",
    response_model=PartnerMemberRead,
    status_code=201,
)
def assign_partner_member(
    partner_id: int,
    payload: PartnerMemberAssign,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    partner = db.get(Partner, partner_id)
    if not partner or not partner.active:
        raise HTTPException(404, "Активный партнёр не найден")
    user = db.scalar(
        select(User).where(func.lower(User.email) == payload.email.strip().lower())
    )
    if not user:
        raise HTTPException(404, "Пользователь с таким email не найден")
    existing = db.scalar(select(PartnerMember).where(PartnerMember.user_id == user.id))
    if existing and existing.active:
        raise HTTPException(409, "Пользователь уже привязан к партнёру")
    if existing:
        existing.partner_id = partner.id
        existing.role = payload.role.value
        existing.active = True
        member = existing
    else:
        member = PartnerMember(
            partner_id=partner.id, user_id=user.id, role=payload.role.value
        )
        db.add(member)
    db.commit()
    db.refresh(member)
    return _member_dict(member, partner, user)


@router.delete("/admin/partner-members/{member_id}", status_code=204)
def deactivate_partner_member(
    member_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    member = db.get(PartnerMember, member_id)
    if not member:
        raise HTTPException(404, "Сотрудник партнёра не найден")
    member.active = False
    db.commit()
    return Response(status_code=204)


@router.post("/admin/partners", response_model=PartnerRead, status_code=201)
def create_partner(
    payload: PartnerCreate,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Partner:
    partner = Partner(**payload.model_dump())
    db.add(partner)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Партнёр с таким названием уже существует")
    db.refresh(partner)
    return partner


@router.patch("/admin/partners/{partner_id}", response_model=PartnerRead)
def update_partner(
    partner_id: int,
    payload: PartnerUpdate,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Partner:
    partner = db.get(Partner, partner_id)
    if not partner:
        raise HTTPException(404, "Партнёр не найден")
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(partner, field, value)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Партнёр с таким названием уже существует")
    db.refresh(partner)
    return partner


@router.delete("/admin/partners/{partner_id}", status_code=204)
def deactivate_partner(
    partner_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    partner = db.get(Partner, partner_id)
    if not partner:
        raise HTTPException(404, "Партнёр не найден")
    partner.active = False
    db.query(Product).filter(Product.partner_id == partner.id).update(
        {Product.active: False}
    )
    db.commit()
    return Response(status_code=204)


@router.get("/admin/products", response_model=list[AdminProductRead])
def admin_products(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.execute(
        select(Product, Partner.name)
        .join(Partner)
        .order_by(Product.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_product_dict(product, partner_name) for product, partner_name in rows]


@router.post("/admin/products", response_model=AdminProductRead, status_code=201)
def create_product(
    payload: ProductCreate,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    partner = db.get(Partner, payload.partner_id)
    if not partner or not partner.active:
        raise HTTPException(404, "Активный партнёр не найден")
    product = Product(
        **payload.model_dump(mode="json"),
        moderation_status="approved",
        moderated_at=datetime.now(timezone.utc),
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "SKU товара уже используется этим партнёром")
    db.refresh(product)
    return _product_dict(product, partner.name)


@router.patch("/admin/products/{product_id}", response_model=AdminProductRead)
def update_product(
    product_id: int,
    payload: ProductUpdate,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Товар не найден")
    changes = payload.model_dump(exclude_unset=True, mode="json")
    if "partner_id" in changes:
        partner = db.get(Partner, changes["partner_id"])
        if not partner or not partner.active:
            raise HTTPException(404, "Активный партнёр не найден")
    for field, value in changes.items():
        setattr(product, field, value)
    if changes.keys() & {
        "name",
        "sku",
        "category",
        "description",
        "product_url",
        "image_url",
        "price_cents",
        "currency",
        "in_stock",
        "regions",
    }:
        product.moderation_status = "draft"
        product.moderation_note = None
        product.moderated_at = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "SKU товара уже используется этим партнёром")
    db.refresh(product)
    partner_name = db.scalar(
        select(Partner.name).where(Partner.id == product.partner_id)
    )
    return _product_dict(product, partner_name)


@router.patch(
    "/admin/products/{product_id}/moderation", response_model=AdminProductRead
)
def moderate_product(
    product_id: int,
    payload: ProductModerationUpdate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Товар не найден")
    product.moderation_status = payload.status
    product.moderation_note = payload.note
    product.moderated_at = datetime.now(timezone.utc)
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action="product.moderate",
            target_type="product",
            target_id=str(product.id),
            details={"status": payload.status, "note": payload.note},
        )
    )
    db.commit()
    db.refresh(product)
    partner_name = db.scalar(
        select(Partner.name).where(Partner.id == product.partner_id)
    )
    return _product_dict(product, partner_name)


@router.post(
    "/admin/product-recommendation-rules",
    response_model=ProductRecommendationRuleRead,
    status_code=201,
)
def create_product_recommendation_rule(
    payload: ProductRecommendationRuleCreate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> ProductRecommendationRule:
    product = db.get(Product, payload.product_id)
    if not product:
        raise HTTPException(404, "Товар не найден")
    registration = (
        db.get(RegulatedProductRegistration, payload.registry_entry_id)
        if payload.registry_entry_id
        else None
    )
    if product.category == "protection" and registration is None:
        raise HTTPException(
            422, "Для средства защиты обязательна запись проверенного реестра"
        )
    if registration and (
        registration.status != "active"
        or (registration.valid_until and registration.valid_until < date.today())
    ):
        raise HTTPException(422, "Регистрация товара не действует")
    if (
        payload.registration_expires_on
        and payload.registration_expires_on < date.today()
    ):
        raise HTTPException(422, "Срок регистрации товара истёк")
    values = payload.model_dump()
    if registration:
        values.update(
            {
                "registration_country": registration.jurisdiction,
                "registration_number": registration.registration_number,
                "registration_url": registration.source_url,
                "registration_expires_on": registration.valid_until,
            }
        )
    rule = ProductRecommendationRule(**values)
    db.add(rule)
    try:
        db.flush()
        db.add(
            AdminAuditLog(
                admin_user_id=admin.id,
                action="product_rule.create",
                target_type="product_recommendation_rule",
                target_id=str(rule.id),
                details={
                    "product_id": rule.product_id,
                    "region_code": rule.region_code,
                },
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Такое правило рекомендации уже существует")
    db.refresh(rule)
    return rule


@router.post(
    "/admin/regulated-products/import", response_model=RegistrationImportResult
)
def import_regulated_products(
    payload: RegistrationSnapshotImport,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    result = import_registration_snapshot(db, payload)
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action="regulated_registry.import",
            target_type="regulated_product_registry",
            target_id=payload.source_name,
            details={"source_version": payload.source_version, **result},
        )
    )
    db.commit()
    return result


@router.get(
    "/admin/regulated-products", response_model=list[RegulatedProductRegistrationRead]
)
def list_regulated_products(
    jurisdiction: str | None = Query(default=None, min_length=2, max_length=2),
    status_filter: str | None = Query(
        default=None, pattern="^(active|expired|suspended|revoked|not_listed)$"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[RegulatedProductRegistration]:
    query = select(RegulatedProductRegistration)
    if jurisdiction:
        query = query.where(
            RegulatedProductRegistration.jurisdiction == jurisdiction.upper()
        )
    if status_filter:
        query = query.where(RegulatedProductRegistration.status == status_filter)
    return list(
        db.scalars(
            query.order_by(RegulatedProductRegistration.product_name)
            .offset(offset)
            .limit(limit)
        )
    )


@router.get(
    "/admin/product-recommendation-rules",
    response_model=list[ProductRecommendationRuleRead],
)
def list_product_recommendation_rules(
    product_id: int | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[ProductRecommendationRule]:
    query = select(ProductRecommendationRule)
    if product_id is not None:
        query = query.where(ProductRecommendationRule.product_id == product_id)
    return list(
        db.scalars(
            query.order_by(ProductRecommendationRule.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )


def _taxonomy_alias_dict(item: CatalogTaxonomyAlias) -> dict:
    return {
        "id": item.id,
        "alias_type": item.alias_type,
        "stable_code": item.stable_code,
        "locale": None if item.locale == "*" else item.locale,
        "alias": item.alias,
        "active": item.active,
        "created_at": item.created_at,
    }


@router.post(
    "/admin/catalog-taxonomy-aliases",
    response_model=CatalogTaxonomyAliasRead,
    status_code=201,
)
def create_catalog_taxonomy_alias(
    payload: CatalogTaxonomyAliasCreate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    item = CatalogTaxonomyAlias(
        alias_type=payload.alias_type,
        stable_code=payload.stable_code,
        locale=payload.locale or "*",
        alias=payload.alias,
        normalized_alias=normalize_alias(payload.alias),
    )
    db.add(item)
    try:
        db.flush()
        db.add(
            AdminAuditLog(
                admin_user_id=admin.id,
                action="catalog_taxonomy_alias.create",
                target_type="catalog_taxonomy_alias",
                target_id=str(item.id),
                details={
                    "alias_type": item.alias_type,
                    "stable_code": item.stable_code,
                    "locale": item.locale,
                },
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Такой синоним каталога уже существует")
    db.refresh(item)
    return _taxonomy_alias_dict(item)


@router.get(
    "/admin/catalog-taxonomy-aliases", response_model=list[CatalogTaxonomyAliasRead]
)
def list_catalog_taxonomy_aliases(
    alias_type: str | None = Query(
        default=None, pattern="^(plant|problem|country|region)$"
    ),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=500),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    query = select(CatalogTaxonomyAlias)
    if alias_type:
        query = query.where(CatalogTaxonomyAlias.alias_type == alias_type)
    items = db.scalars(
        query.order_by(
            CatalogTaxonomyAlias.alias_type,
            CatalogTaxonomyAlias.stable_code,
            CatalogTaxonomyAlias.id,
        )
        .offset(offset)
        .limit(limit)
    )
    return [_taxonomy_alias_dict(item) for item in items]


@router.delete("/admin/catalog-taxonomy-aliases/{alias_id}", status_code=204)
def deactivate_catalog_taxonomy_alias(
    alias_id: int,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    item = db.get(CatalogTaxonomyAlias, alias_id)
    if not item:
        raise HTTPException(404, "Синоним каталога не найден")
    item.active = False
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action="catalog_taxonomy_alias.deactivate",
            target_type="catalog_taxonomy_alias",
            target_id=str(item.id),
            details={"alias_type": item.alias_type, "stable_code": item.stable_code},
        )
    )
    db.commit()
    return Response(status_code=204)


@router.delete("/admin/products/{product_id}", status_code=204)
def deactivate_product(
    product_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(404, "Товар не найден")
    product.active = False
    db.commit()
    return Response(status_code=204)


@router.get("/admin/leads", response_model=list[AdminLeadRead])
def admin_leads(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.execute(
        select(ProductLead, Product.name, Partner.name)
        .join(Product, Product.id == ProductLead.product_id)
        .join(Partner, Partner.id == Product.partner_id)
        .order_by(ProductLead.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": lead.id,
            "product_id": lead.product_id,
            "product_name": product_name,
            "partner_name": partner_name,
            "diagnosis_id": lead.diagnosis_id,
            "source": lead.source,
            "status": lead.status,
            "partner_reference": lead.partner_reference,
            "conversion_value_cents": lead.conversion_value_cents,
            "billable_amount_cents": lead.billable_amount_cents,
            "claimed_at": lead.claimed_at,
            "confirmed_at": lead.confirmed_at,
            "created_at": lead.created_at,
        }
        for lead, product_name, partner_name in rows
    ]


@router.get("/partner/me", response_model=PartnerAccountRead | None)
def partner_account(
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> dict | None:
    member = db.scalar(
        select(PartnerMember)
        .join(Partner)
        .where(
            PartnerMember.user_id == user.id,
            PartnerMember.active.is_(True),
            Partner.active.is_(True),
        )
    )
    if member is None:
        return None
    partner = db.get(Partner, member.partner_id)
    return {
        "partner_id": partner.id,
        "partner_name": partner.name,
        "website_url": partner.website_url,
        "role": member.role,
        "can_manage_products": member.role in {"owner", "editor"},
        "billing_plan": partner.billing_plan,
        "billing_currency": partner.billing_currency,
    }


@router.get("/partner/overview", response_model=PartnerOverviewRead)
def partner_overview(
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(days=30)
    lead_base = (
        select(func.count(ProductLead.id))
        .join(Product)
        .where(Product.partner_id == member.partner_id)
    )
    amount_base = (
        select(func.coalesce(func.sum(ProductLead.billable_amount_cents), 0))
        .join(Product)
        .where(
            Product.partner_id == member.partner_id,
            ProductLead.status == "confirmed",
            ProductLead.invoice_id.is_(None),
        )
    )
    return {
        "partner_id": member.partner_id,
        "partner_name": db.scalar(
            select(Partner.name).where(Partner.id == member.partner_id)
        ),
        "active_products": db.scalar(
            select(func.count(Product.id)).where(
                Product.partner_id == member.partner_id, Product.active.is_(True)
            )
        )
        or 0,
        "total_products": db.scalar(
            select(func.count(Product.id)).where(
                Product.partner_id == member.partner_id
            )
        )
        or 0,
        "total_leads": db.scalar(lead_base) or 0,
        "leads_last_30_days": db.scalar(
            lead_base.where(ProductLead.created_at >= cutoff)
        )
        or 0,
        "pending_conversion_claims": db.scalar(
            lead_base.where(ProductLead.status == "conversion_claimed")
        )
        or 0,
        "confirmed_conversions": db.scalar(
            lead_base.where(ProductLead.status == "confirmed")
        )
        or 0,
        "uninvoiced_amount_cents": db.scalar(amount_base) or 0,
        "billing_currency": db.scalar(
            select(Partner.billing_currency).where(Partner.id == member.partner_id)
        ),
    }


@router.get("/partner/products", response_model=list[AdminProductRead])
def partner_products(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> list[dict]:
    partner = db.get(Partner, member.partner_id)
    products = db.scalars(
        select(Product)
        .where(Product.partner_id == member.partner_id)
        .order_by(Product.id.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [_product_dict(product, partner.name) for product in products]


def _csv_boolean(value: str | None, *, default: bool = True) -> bool:
    normalized = (value or "").strip().casefold()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "y", "да", "jā"}:
        return True
    if normalized in {"0", "false", "no", "n", "нет", "nē"}:
        return False
    raise ValueError("in_stock must be true or false")


def _csv_row_payload(row_number: int, row: dict) -> PartnerProductCreate:
    """Validate one CSV row and translate errors into a 422 with the row number."""
    try:
        payload = PartnerProductCreate.model_validate(
            {
                "sku": (row.get("sku") or "").strip(),
                "name": (row.get("name") or "").strip(),
                "category": (row.get("category") or "").strip(),
                "description": (row.get("description") or "").strip() or None,
                "product_url": (row.get("product_url") or "").strip(),
                "image_url": (row.get("image_url") or "").strip() or None,
                "price_cents": (row.get("price_cents") or "").strip() or None,
                "currency": (row.get("currency") or "eur").strip(),
                "in_stock": _csv_boolean(row.get("in_stock")),
                "regions": [
                    value.strip()
                    for value in (row.get("regions") or "").split("|")
                    if value.strip()
                ],
            }
        )
        if not payload.sku:
            raise ValueError("sku is required")
        return payload
    except (ValidationError, ValueError) as exc:
        errors = (
            exc.errors(include_url=False)
            if isinstance(exc, ValidationError)
            else [{"type": "value_error", "msg": str(exc)}]
        )
        raise HTTPException(
            422,
            {
                "message": "Invalid CSV row",
                "row": row_number,
                "errors": errors,
            },
        ) from exc


@router.post("/partner/products/import/csv")
async def import_partner_products_csv(
    file: UploadFile = File(...),
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Atomically create or update partner products by SKU from a bounded UTF-8 CSV."""
    _require_product_manager(member)
    if file.content_type not in {
        "text/csv",
        "application/csv",
        "application/vnd.ms-excel",
    }:
        raise HTTPException(415, "Поддерживается только CSV")
    raw = await file.read(2 * 1024 * 1024 + 1)
    if len(raw) > 2 * 1024 * 1024:
        raise HTTPException(413, "CSV-файл слишком большой")
    try:
        reader = csv.DictReader(StringIO(raw.decode("utf-8-sig")))
    except UnicodeDecodeError as exc:
        raise HTTPException(422, {"message": "CSV must be UTF-8"}) from exc
    required = {"sku", "name", "category", "product_url"}
    if not reader.fieldnames or not required.issubset(set(reader.fieldnames)):
        raise HTTPException(
            422, {"message": "CSV headers are missing", "required": sorted(required)}
        )

    payloads: list[PartnerProductCreate] = []
    row_number = 2
    try:
        for row_number, row in enumerate(reader, start=2):
            if row_number > 1_001:
                raise HTTPException(
                    422, {"message": "CSV row limit exceeded", "limit": 1_000}
                )
            payloads.append(_csv_row_payload(row_number, row))
    except csv.Error as exc:
        # csv.Error is not a ValueError: an oversized field would otherwise
        # escape the validation branch and surface as an HTTP 500.
        raise HTTPException(
            422,
            {
                "message": "CSV contains an oversized or malformed field",
                "row": row_number,
            },
        ) from exc
    if not payloads:
        raise HTTPException(422, {"message": "CSV contains no products"})

    skus = {payload.sku for payload in payloads}
    existing = {
        product.sku: product
        for product in db.scalars(
            select(Product).where(
                Product.partner_id == member.partner_id, Product.sku.in_(skus)
            )
        )
    }
    created = 0
    updated = 0
    for payload in payloads:
        values = payload.model_dump(mode="json")
        product = existing.get(payload.sku)
        if product is None:
            product = Product(
                partner_id=member.partner_id, **values, moderation_status="draft"
            )
            db.add(product)
            existing[payload.sku] = product
            created += 1
        else:
            for field, value in values.items():
                setattr(product, field, value)
            product.moderation_status = "draft"
            product.moderation_note = None
            product.moderated_at = None
            updated += 1
    db.add(
        AdminAuditLog(
            admin_user_id=member.user_id,
            action="partner_products.import_csv",
            target_type="partner",
            target_id=str(member.partner_id),
            details={"created": created, "updated": updated},
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "CSV содержит повторяющийся SKU")
    return {"created": created, "updated": updated}


@router.post("/partner/products", response_model=AdminProductRead, status_code=201)
def create_partner_product(
    payload: PartnerProductCreate,
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict:
    _require_product_manager(member)
    partner = db.get(Partner, member.partner_id)
    product = Product(
        partner_id=member.partner_id,
        **payload.model_dump(mode="json"),
        moderation_status="draft",
    )
    db.add(product)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "SKU товара уже используется этим партнёром")
    db.refresh(product)
    return _product_dict(product, partner.name)


@router.patch("/partner/products/{product_id}", response_model=AdminProductRead)
def update_partner_product(
    product_id: int,
    payload: ProductUpdate,
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict:
    _require_product_manager(member)
    product = db.scalar(
        select(Product).where(
            Product.id == product_id, Product.partner_id == member.partner_id
        )
    )
    if not product:
        raise HTTPException(404, "Товар не найден")
    changes = payload.model_dump(exclude_unset=True, mode="json")
    if "partner_id" in changes:
        raise HTTPException(422, "Нельзя перенести товар в другую организацию")
    for field, value in changes.items():
        setattr(product, field, value)
    if changes.keys() & {
        "name",
        "sku",
        "category",
        "description",
        "product_url",
        "image_url",
        "price_cents",
        "currency",
        "in_stock",
        "regions",
    }:
        product.moderation_status = "draft"
        product.moderation_note = None
        product.moderated_at = None
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "SKU товара уже используется этим партнёром")
    db.refresh(product)
    partner_name = db.scalar(
        select(Partner.name).where(Partner.id == member.partner_id)
    )
    return _product_dict(product, partner_name)


@router.post("/partner/products/{product_id}/submit", response_model=AdminProductRead)
def submit_partner_product(
    product_id: int,
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict:
    _require_product_manager(member)
    product = db.scalar(
        select(Product).where(
            Product.id == product_id, Product.partner_id == member.partner_id
        )
    )
    if not product:
        raise HTTPException(404, "Товар не найден")
    if product.moderation_status not in {"draft", "rejected"}:
        raise HTTPException(409, "Товар уже отправлен на модерацию")
    product.moderation_status = "pending"
    product.moderation_note = None
    db.commit()
    db.refresh(product)
    partner_name = db.scalar(
        select(Partner.name).where(Partner.id == member.partner_id)
    )
    return _product_dict(product, partner_name)


@router.get("/partner/leads", response_model=list[PartnerLeadRead])
def partner_leads(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> list[dict]:
    rows = db.execute(
        select(ProductLead, Product.name)
        .join(Product, Product.id == ProductLead.product_id)
        .where(Product.partner_id == member.partner_id)
        .order_by(ProductLead.created_at.desc())
        .offset(offset)
        .limit(limit)
    ).all()
    return [
        {
            "id": lead.id,
            "product_id": lead.product_id,
            "product_name": product_name,
            "source": lead.source,
            "status": lead.status,
            "partner_reference": lead.partner_reference,
            "conversion_value_cents": lead.conversion_value_cents,
            "billable_amount_cents": lead.billable_amount_cents,
            "claimed_at": lead.claimed_at,
            "confirmed_at": lead.confirmed_at,
            "created_at": lead.created_at,
        }
        for lead, product_name in rows
    ]


@router.post("/partner/leads/{lead_id}/conversion", response_model=PartnerLeadRead)
def claim_partner_conversion(
    lead_id: int,
    payload: PartnerConversionClaim,
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> dict:
    _require_product_manager(member)
    row = db.execute(
        select(ProductLead, Product.name)
        .join(Product, Product.id == ProductLead.product_id)
        .where(ProductLead.id == lead_id, Product.partner_id == member.partner_id)
        .with_for_update()
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Переход не найден")
    lead, product_name = row
    if lead.status not in {"clicked", "rejected"} or lead.invoice_id is not None:
        raise HTTPException(409, "Конверсия уже обрабатывается или включена в счёт")
    lead.status = "conversion_claimed"
    lead.partner_reference = payload.partner_reference
    lead.conversion_value_cents = payload.conversion_value_cents
    lead.claimed_at = datetime.now(timezone.utc)
    lead.confirmed_at = None
    lead.rejected_at = None
    lead.review_note = None
    lead.reviewed_by_admin_id = None
    lead.billable_amount_cents = 0
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Такая ссылка на продажу уже использована для товара")
    db.refresh(lead)
    return {
        "id": lead.id,
        "product_id": lead.product_id,
        "product_name": product_name,
        "source": lead.source,
        "status": lead.status,
        "partner_reference": lead.partner_reference,
        "conversion_value_cents": lead.conversion_value_cents,
        "billable_amount_cents": lead.billable_amount_cents,
        "claimed_at": lead.claimed_at,
        "confirmed_at": lead.confirmed_at,
        "created_at": lead.created_at,
    }


@router.patch("/admin/leads/{lead_id}/conversion", response_model=AdminLeadRead)
def review_partner_conversion(
    lead_id: int,
    payload: AdminConversionReview,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict:
    row = db.execute(
        select(ProductLead, Product, Partner)
        .join(Product, Product.id == ProductLead.product_id)
        .join(Partner, Partner.id == Product.partner_id)
        .where(ProductLead.id == lead_id)
        .with_for_update()
    ).one_or_none()
    if not row:
        raise HTTPException(404, "Переход не найден")
    lead, product, partner = row
    if lead.status != "conversion_claimed" or lead.invoice_id is not None:
        raise HTTPException(409, "Нет ожидающей проверки конверсии")
    now = datetime.now(timezone.utc)
    lead.reviewed_by_admin_id = admin.id
    lead.review_note = payload.note
    if payload.approved:
        lead.status = "confirmed"
        lead.confirmed_at = now
        lead.rejected_at = None
        lead.billable_amount_cents = partner.confirmed_lead_price_cents
    else:
        lead.status = "rejected"
        lead.confirmed_at = None
        lead.rejected_at = now
        lead.billable_amount_cents = 0
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action="partner_conversion.review",
            target_type="product_lead",
            target_id=str(lead.id),
            details={"approved": payload.approved, "partner_id": partner.id},
        )
    )
    db.commit()
    db.refresh(lead)
    return {
        "id": lead.id,
        "product_id": lead.product_id,
        "product_name": product.name,
        "partner_name": partner.name,
        "diagnosis_id": lead.diagnosis_id,
        "source": lead.source,
        "status": lead.status,
        "partner_reference": lead.partner_reference,
        "conversion_value_cents": lead.conversion_value_cents,
        "billable_amount_cents": lead.billable_amount_cents,
        "claimed_at": lead.claimed_at,
        "confirmed_at": lead.confirmed_at,
        "created_at": lead.created_at,
    }


@router.post(
    "/admin/partners/{partner_id}/invoices",
    response_model=PartnerInvoiceRead,
    status_code=201,
)
def create_partner_invoice(
    partner_id: int,
    payload: PartnerInvoiceCreate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> PartnerInvoice:
    _require_b2b_invoicing()
    partner = db.scalar(
        select(Partner).where(Partner.id == partner_id).with_for_update()
    )
    if not partner:
        raise HTTPException(404, "Партнёр не найден")
    overlapping = db.scalar(
        select(PartnerInvoice.id).where(
            PartnerInvoice.partner_id == partner_id,
            PartnerInvoice.status != "void",
            PartnerInvoice.period_start < payload.period_end,
            PartnerInvoice.period_end > payload.period_start,
        )
    )
    if overlapping:
        raise HTTPException(409, "За пересекающийся период уже существует счёт")
    start = datetime.combine(
        payload.period_start, datetime.min.time(), tzinfo=timezone.utc
    )
    end = datetime.combine(payload.period_end, datetime.min.time(), tzinfo=timezone.utc)
    leads = list(
        db.scalars(
            select(ProductLead)
            .join(Product, Product.id == ProductLead.product_id)
            .where(
                Product.partner_id == partner_id,
                ProductLead.status == "confirmed",
                ProductLead.invoice_id.is_(None),
                ProductLead.confirmed_at >= start,
                ProductLead.confirmed_at < end,
            )
            .with_for_update()
        )
    )
    lead_fees = sum(item.billable_amount_cents for item in leads)
    total = partner.monthly_fee_cents + lead_fees
    if total <= 0:
        raise HTTPException(422, "За выбранный период нет начислений")
    try:
        snapshots = build_invoice_snapshots(
            settings,
            partner,
            monthly_fee_cents=partner.monthly_fee_cents,
            lead_fee_amounts=[item.billable_amount_cents for item in leads],
        )
    except InvoiceDocumentError as exc:
        raise HTTPException(422, str(exc)) from exc
    now = datetime.now(timezone.utc)
    invoice = PartnerInvoice(
        partner_id=partner.id,
        period_start=payload.period_start,
        period_end=payload.period_end,
        currency=partner.billing_currency,
        monthly_fee_cents=partner.monthly_fee_cents,
        confirmed_leads_count=len(leads),
        lead_fees_cents=lead_fees,
        total_cents=total,
        status="issued",
        issued_at=now,
        due_at=now + timedelta(days=payload.due_days),
        issuer_snapshot=snapshots.issuer,
        customer_snapshot=snapshots.customer,
        line_items=snapshots.line_items,
        document_version=DOCUMENT_VERSION,
    )
    db.add(invoice)
    try:
        db.flush()
        invoice.invoice_number = f"AG-{now.year}-{invoice.id:08d}"
        try:
            invoice.snapshot_sha256 = invoice_snapshot_sha256(invoice)
            document = render_invoice_pdf(invoice, settings)
            invoice.pdf_sha256 = document_sha256(document)
            (
                invoice.pdf_storage_backend,
                invoice.pdf_storage_key,
                invoice.pdf_file_path,
            ) = store_private_document(
                "invoices", invoice.invoice_number, document, "application/pdf"
            )
            invoice.pdf_size_bytes = len(document)
            invoice.pdf_content_type = "application/pdf"
            invoice.pdf_generator_version = invoice_pdf_generator_version(settings)
        except (InvoiceDocumentError, StorageError) as exc:
            db.rollback()
            raise HTTPException(503, str(exc)) from exc
        for lead in leads:
            lead.invoice_id = invoice.id
        db.add(
            AdminAuditLog(
                admin_user_id=admin.id,
                action="partner_invoice.issue",
                target_type="partner_invoice",
                target_id=str(invoice.id),
                details={
                    "partner_id": partner.id,
                    "invoice_number": invoice.invoice_number,
                    "total_cents": total,
                    "pdf_sha256": invoice.pdf_sha256,
                },
            )
        )
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Счёт за этот период уже существует")
    db.refresh(invoice)
    return invoice


@router.get("/admin/partner-invoices", response_model=list[PartnerInvoiceRead])
def admin_partner_invoices(
    partner_id: int | None = None,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[PartnerInvoice]:
    _require_b2b_invoicing()
    query = select(PartnerInvoice)
    if partner_id is not None:
        query = query.where(PartnerInvoice.partner_id == partner_id)
    return list(
        db.scalars(
            query.order_by(PartnerInvoice.created_at.desc()).offset(offset).limit(limit)
        )
    )


def _invoice_pdf_response(invoice: PartnerInvoice) -> Response:
    try:
        document = load_verified_invoice_pdf(invoice, settings)
    except InvoiceDocumentError as exc:
        raise HTTPException(409, str(exc)) from exc
    return Response(
        content=document,
        media_type="application/pdf",
        headers={
            "Content-Disposition": f'attachment; filename="{invoice.invoice_number}.pdf"',
            "Cache-Control": "private, no-store",
            "ETag": f'"{invoice.pdf_sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


def _invoice_cancellation_pdf_response(invoice: PartnerInvoice) -> Response:
    try:
        document = load_verified_invoice_cancellation_pdf(invoice, settings)
    except InvoiceDocumentError as exc:
        status_code = 404 if not invoice.cancellation_number else 409
        raise HTTPException(status_code, str(exc)) from exc
    return Response(
        content=document,
        media_type="application/pdf",
        headers={
            "Content-Disposition": (
                f'attachment; filename="{invoice.cancellation_number}.pdf"'
            ),
            "Cache-Control": "private, no-store",
            "ETag": f'"{invoice.cancellation_pdf_sha256}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/admin/partner-invoices/{invoice_id}/pdf")
def admin_partner_invoice_pdf(
    invoice_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    _require_b2b_invoicing()
    invoice = db.get(PartnerInvoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    return _invoice_pdf_response(invoice)


@router.get("/admin/partner-invoices/{invoice_id}/cancellation.pdf")
def admin_partner_invoice_cancellation_pdf(
    invoice_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> Response:
    _require_b2b_invoicing()
    invoice = db.get(PartnerInvoice, invoice_id)
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    return _invoice_cancellation_pdf_response(invoice)


@router.get(
    "/admin/partner-invoices/{invoice_id}/deliveries",
    response_model=list[PartnerInvoiceDeliveryRead],
)
def admin_partner_invoice_deliveries(
    invoice_id: int,
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[PartnerInvoiceDelivery]:
    _require_b2b_invoicing()
    if not db.get(PartnerInvoice, invoice_id):
        raise HTTPException(404, "Счёт не найден")
    return list(
        db.scalars(
            select(PartnerInvoiceDelivery)
            .where(
                PartnerInvoiceDelivery.invoice_id == invoice_id,
            )
            .order_by(PartnerInvoiceDelivery.attempt_number.desc())
        )
    )


@router.post(
    "/admin/partner-invoices/{invoice_id}/send",
    response_model=PartnerInvoiceDeliveryRead,
    status_code=201,
)
def send_partner_invoice(
    invoice_id: int,
    payload: PartnerInvoiceSend,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> PartnerInvoiceDelivery:
    _require_b2b_invoicing()
    invoice = db.scalar(
        select(PartnerInvoice)
        .where(
            PartnerInvoice.id == invoice_id,
        )
        .with_for_update()
    )
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    if invoice.status == "void":
        raise HTTPException(409, "Аннулированный счёт отправлять нельзя")
    try:
        load_verified_invoice_pdf(invoice, settings)
    except InvoiceDocumentError as exc:
        raise HTTPException(409, str(exc)) from exc

    latest = db.scalar(
        select(PartnerInvoiceDelivery)
        .where(
            PartnerInvoiceDelivery.invoice_id == invoice.id,
        )
        .order_by(PartnerInvoiceDelivery.attempt_number.desc())
        .limit(1)
    )
    now = datetime.now(timezone.utc)
    latest_created_at = latest.created_at if latest else None
    if latest_created_at and latest_created_at.tzinfo is None:
        latest_created_at = latest_created_at.replace(tzinfo=timezone.utc)
    pending_is_fresh = bool(
        latest
        and latest.status == "pending"
        and latest_created_at
        and now - latest_created_at < timedelta(minutes=15)
    )
    if pending_is_fresh:
        raise HTTPException(409, "Отправка счёта уже выполняется")
    if (
        latest
        and latest.status in {"pending", "sent", "logged", "unknown"}
        and not payload.confirm_resend
    ):
        raise HTTPException(409, "Счёт уже отправлялся; подтвердите повторную отправку")
    if latest and latest.status == "pending":
        latest.status = "unknown"
        latest.error_type = "DeliveryStateUnknown"
        latest.completed_at = now
    recipient = str((invoice.customer_snapshot or {}).get("email") or "").strip()
    delivery = PartnerInvoiceDelivery(
        invoice_id=invoice.id,
        requested_by_admin_id=admin.id,
        attempt_number=(latest.attempt_number + 1) if latest else 1,
        recipient=recipient,
        status="pending",
        created_at=now,
    )
    db.add(delivery)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Отправка счёта уже выполняется")

    try:
        dispatch_invoice_delivery(delivery.id)
    except Exception:
        # The persisted pending row is the outbox record. Celery beat can safely
        # republish it; duplicate task delivery is guarded by an atomic claim.
        pass
    delivery = db.get(PartnerInvoiceDelivery, delivery.id)
    if delivery.status == "failed" and settings.diagnosis_execution_mode != "celery":
        raise HTTPException(502, "Не удалось отправить счёт")
    db.refresh(delivery)
    return delivery


@router.post("/admin/partner-payments/import", response_model=PartnerPaymentImportRead)
def import_partner_payments(
    payload: PartnerPaymentImport,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    _require_b2b_invoicing()
    source = payload.source.lower()
    external_ids = [record.external_id for record in payload.records]
    existing = set(
        db.scalars(
            select(PartnerPayment.external_id).where(
                PartnerPayment.source == source,
                PartnerPayment.external_id.in_(external_ids),
            )
        )
    )
    seen = set(existing)
    imported = matched = unmatched = duplicates = 0
    matched_invoice_ids: set[int] = set()
    for record in payload.records:
        if record.external_id in seen:
            duplicates += 1
            continue
        seen.add(record.external_id)
        invoice_numbers = {
            value.upper() for value in INVOICE_NUMBER_PATTERN.findall(record.reference)
        }
        invoice = None
        if len(invoice_numbers) == 1:
            invoice = db.scalar(
                select(PartnerInvoice)
                .where(
                    PartnerInvoice.invoice_number == next(iter(invoice_numbers)),
                )
                .with_for_update()
            )
        is_match = bool(
            invoice
            and invoice.id not in matched_invoice_ids
            and invoice.status == "issued"
            and invoice.total_cents == record.amount_cents
            and invoice.currency.lower() == record.currency.lower()
        )
        if is_match:
            invoice.status = "paid"
            invoice.paid_at = datetime.combine(
                record.booking_date, datetime.min.time(), tzinfo=timezone.utc
            )
            matched_invoice_ids.add(invoice.id)
            matched += 1
        else:
            unmatched += 1
        db.add(
            PartnerPayment(
                invoice_id=invoice.id if invoice else None,
                imported_by_admin_id=admin.id,
                source=source,
                external_id=record.external_id,
                booking_date=record.booking_date,
                amount_cents=record.amount_cents,
                currency=record.currency.lower(),
                reference=record.reference,
                status="matched" if is_match else "unmatched",
            )
        )
        imported += 1
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action="partner_payments.import",
            target_type="partner_payment_batch",
            target_id=source,
            details={
                "imported": imported,
                "matched": matched,
                "unmatched": unmatched,
                "duplicates": duplicates,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Банковская операция уже импортирована")
    return {
        "imported": imported,
        "matched": matched,
        "unmatched": unmatched,
        "duplicates": duplicates,
    }


@router.get("/admin/partner-payments", response_model=list[PartnerPaymentRead])
def admin_partner_payments(
    status: str | None = Query(default=None, pattern="^(matched|unmatched|rejected)$"),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    _admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[PartnerPayment]:
    _require_b2b_invoicing()
    query = select(PartnerPayment)
    if status:
        query = query.where(PartnerPayment.status == status)
    return list(
        db.scalars(
            query.order_by(PartnerPayment.booking_date.desc(), PartnerPayment.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.patch("/admin/partner-payments/{payment_id}", response_model=PartnerPaymentRead)
def resolve_partner_payment(
    payment_id: int,
    payload: PartnerPaymentResolve,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> PartnerPayment:
    _require_b2b_invoicing()
    payment = db.scalar(
        select(PartnerPayment)
        .where(
            PartnerPayment.id == payment_id,
        )
        .with_for_update()
    )
    if not payment:
        raise HTTPException(404, "Банковская операция не найдена")
    if payment.status != "unmatched":
        raise HTTPException(409, "Решение по банковской операции уже принято")
    now = datetime.now(timezone.utc)
    if payload.action == "reject":
        payment.status = "rejected"
        payment.invoice_id = None
    else:
        invoice = db.scalar(
            select(PartnerInvoice)
            .where(
                PartnerInvoice.id == payload.invoice_id,
            )
            .with_for_update()
        )
        if not invoice:
            raise HTTPException(404, "Счёт не найден")
        if invoice.status != "issued":
            raise HTTPException(
                409, "Сопоставить можно только выставленный неоплаченный счёт"
            )
        if (
            invoice.total_cents != payment.amount_cents
            or invoice.currency != payment.currency
        ):
            raise HTTPException(
                422, "Сумма или валюта банковской операции не совпадает со счётом"
            )
        existing_match = db.scalar(
            select(PartnerPayment.id).where(
                PartnerPayment.invoice_id == invoice.id,
                PartnerPayment.status == "matched",
                PartnerPayment.id != payment.id,
            )
        )
        if existing_match:
            raise HTTPException(409, "Счёт уже сопоставлен с банковской операцией")
        payment.invoice_id = invoice.id
        payment.status = "matched"
        invoice.status = "paid"
        invoice.paid_at = datetime.combine(
            payment.booking_date, datetime.min.time(), tzinfo=timezone.utc
        )
    payment.resolution_note = payload.note
    payment.resolved_by_admin_id = admin.id
    payment.resolved_at = now
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action=f"partner_payment.{payload.action}",
            target_type="partner_payment",
            target_id=str(payment.id),
            details={"invoice_id": payment.invoice_id, "status": payment.status},
        )
    )
    db.commit()
    db.refresh(payment)
    return payment


@router.patch("/admin/partner-invoices/{invoice_id}", response_model=PartnerInvoiceRead)
def update_partner_invoice_status(
    invoice_id: int,
    payload: PartnerInvoiceStatusUpdate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> PartnerInvoice:
    _require_b2b_invoicing()
    invoice = db.scalar(
        select(PartnerInvoice).where(PartnerInvoice.id == invoice_id).with_for_update()
    )
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    if invoice.status == payload.status:
        return invoice
    if invoice.status != "issued":
        raise HTTPException(409, "Статус завершённого счёта изменить нельзя")
    if payload.status == "paid":
        raise HTTPException(
            409,
            "Прямая отметка оплаты запрещена; зарегистрируйте ручной платёж",
        )
    else:
        now = datetime.now(timezone.utc)
        invoice.status = "void"
        invoice.cancelled_at = now
        invoice.cancelled_by_admin_id = admin.id
        invoice.cancellation_reason = payload.reason.strip()
        invoice.cancellation_number = f"AG-CN-{now.year}-{invoice.id:08d}"
        invoice.cancellation_document_version = CANCELLATION_DOCUMENT_VERSION
        try:
            invoice.cancellation_snapshot_sha256 = cancellation_snapshot_sha256(invoice)
            cancellation_document = render_invoice_cancellation_pdf(invoice, settings)
            invoice.cancellation_pdf_sha256 = document_sha256(cancellation_document)
            (
                invoice.cancellation_storage_backend,
                invoice.cancellation_storage_key,
                invoice.cancellation_file_path,
            ) = store_private_document(
                "invoice-cancellations",
                invoice.cancellation_number,
                cancellation_document,
                "application/pdf",
            )
            invoice.cancellation_size_bytes = len(cancellation_document)
            invoice.cancellation_content_type = "application/pdf"
            invoice.cancellation_generator_version = invoice_pdf_generator_version(
                settings
            )
        except (InvoiceDocumentError, StorageError) as exc:
            db.rollback()
            raise HTTPException(503, str(exc)) from exc
        db.query(ProductLead).filter(ProductLead.invoice_id == invoice.id).update(
            {ProductLead.invoice_id: None}, synchronize_session=False
        )
    audit_details = {"partner_id": invoice.partner_id}
    if payload.status == "void":
        audit_details.update(
            {
                "cancellation_number": invoice.cancellation_number,
                "cancellation_reason": invoice.cancellation_reason,
                "cancellation_pdf_sha256": invoice.cancellation_pdf_sha256,
            }
        )
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action=f"partner_invoice.{payload.status}",
            target_type="partner_invoice",
            target_id=str(invoice.id),
            details=audit_details,
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Документ аннулирования уже существует")
    db.refresh(invoice)
    return invoice


@router.post(
    "/admin/partner-invoices/{invoice_id}/manual-payment",
    response_model=PartnerPaymentRead,
    status_code=201,
)
def create_manual_partner_payment(
    invoice_id: int,
    payload: PartnerManualPaymentCreate,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> PartnerPayment:
    _require_b2b_invoicing()
    invoice = db.scalar(
        select(PartnerInvoice).where(PartnerInvoice.id == invoice_id).with_for_update()
    )
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    if invoice.status != "issued":
        raise HTTPException(409, "Оплатить можно только выставленный неоплаченный счёт")
    now = datetime.now(timezone.utc)
    source = f"manual:{payload.payment_method}"
    payment = PartnerPayment(
        invoice_id=invoice.id,
        imported_by_admin_id=admin.id,
        source=source,
        external_id=payload.external_id,
        booking_date=payload.booking_date,
        amount_cents=invoice.total_cents,
        currency=invoice.currency,
        reference=f"Manual payment for {invoice.invoice_number}",
        status="matched",
        resolution_note=payload.note,
        resolved_by_admin_id=admin.id,
        resolved_at=now,
    )
    invoice.status = "paid"
    invoice.paid_at = datetime.combine(
        payload.booking_date, datetime.min.time(), tzinfo=timezone.utc
    )
    db.add(payment)
    db.add(
        AdminAuditLog(
            admin_user_id=admin.id,
            action="partner_payment.manual",
            target_type="partner_invoice",
            target_id=str(invoice.id),
            details={
                "source": source,
                "external_id": payload.external_id,
                "amount_cents": invoice.total_cents,
                "currency": invoice.currency,
            },
        )
    )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Платёж с таким внешним ID уже зарегистрирован")
    db.refresh(payment)
    return payment


@router.get("/partner/invoices", response_model=list[PartnerInvoiceRead])
def partner_invoices(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=50, ge=1, le=200),
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> list[PartnerInvoice]:
    _require_b2b_invoicing()
    return list(
        db.scalars(
            select(PartnerInvoice)
            .where(PartnerInvoice.partner_id == member.partner_id)
            .order_by(PartnerInvoice.created_at.desc())
            .offset(offset)
            .limit(limit)
        )
    )


@router.get("/partner/invoices/{invoice_id}/pdf")
def partner_invoice_pdf(
    invoice_id: int,
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> Response:
    _require_b2b_invoicing()
    invoice = db.scalar(
        select(PartnerInvoice).where(
            PartnerInvoice.id == invoice_id,
            PartnerInvoice.partner_id == member.partner_id,
        )
    )
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    return _invoice_pdf_response(invoice)


@router.get("/partner/invoices/{invoice_id}/cancellation.pdf")
def partner_invoice_cancellation_pdf(
    invoice_id: int,
    member: PartnerMember = Depends(get_partner_member),
    db: Session = Depends(get_db),
) -> Response:
    _require_b2b_invoicing()
    invoice = db.scalar(
        select(PartnerInvoice).where(
            PartnerInvoice.id == invoice_id,
            PartnerInvoice.partner_id == member.partner_id,
        )
    )
    if not invoice:
        raise HTTPException(404, "Счёт не найден")
    return _invoice_cancellation_pdf_response(invoice)
